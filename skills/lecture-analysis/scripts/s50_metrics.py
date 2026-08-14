#!/usr/bin/env python3
"""s50: 결정론 지표. 텍스트 지표(클로바 전사) + 정밀 오디오 지표(whisper 세그먼트).
정의 SSOT: references/metrics.md — 여기 로직을 바꾸면 추세가 리셋된다."""
import statistics, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import korean
from lib.clova import fmt_ts
from lib.manifest import load_manifest, check_span
from lib.stage import cli, stage_guard, write_json, read_json, run

HEDGING_EXAMPLE_N = 5   # 리포트에 싣는 대표 예시 수 — 근거 없는 지표 금지(브리프 P1-4)

# 길이를 알 수 없는 교시(클로바 없음 + bounds 미지정)의 상한 센티넬.
# 구간 필터는 전부 통과시키되, 측정 시간 합계에서는 제외한다(measured_hours 참조).
UNKNOWN_END = 10 ** 9
WINDOW_SEC = 300        # 말속도 곡선의 창 크기(5분) — DESIGN §6

def in_bounds(ts: float, b: dict) -> bool:
    """구간 필터 — 텍스트 지표(클로바 블록, 초 정수)와 오디오 지표(whisper 세그먼트,
    초 실수)가 같이 쓴다. 한 metrics.json 안에서 두 지표가 서로 다른 경계 규칙을 쓰면
    같은 발화가 한쪽에만 잡혀 리포트의 두 절이 어긋난다."""
    # 휴식은 [s, e) 반열림 — 길이 계산이 e-s이므로 필터도 같아야 한다.
    # 닫힌 구간으로 두면 휴식이 끝나는 시각에 재개한 블록(휴식 끝을 재개 블록 시각으로
    # 적는 게 자연스럽다)이 통째로 지표에서 사라진다.
    if not (b["start"] <= ts <= b["end"]):
        return False
    return not any(s <= ts < e for s, e in b["breaks"])

def _llm_bounds(sem_p, pid: str) -> dict | None:
    """artifacts/llm/pNN_semantic.json의 bounds_proposal(스키마: 초 정수) → 검증된 구간. 없으면 None.

    파이프라인에서 가장 덜 믿을 수 있는 입력인데 수동 bounds와 달리 사람 눈을 거치지 않는다.
    같은 파일의 다른 시각 필드가 전부 "MM:SS" 문자열이라 모델이 문자열을 섞어 쓰기 쉬운데,
    그대로 통과시키면 exit 1 트레이스백으로 새거나 뒤집힌 구간이 측정 시간을 조용히 부풀린다."""
    if not sem_p.exists():
        return None
    sem = read_json(sem_p)
    if not isinstance(sem, dict):
        raise ValueError(f"{sem_p.name}: JSON 객체여야 함 — s30_semantic을 다시 실행하세요")
    prop = sem.get("bounds_proposal")
    if not prop:
        return None
    where = f"{sem_p.name} bounds_proposal({pid})"
    if not isinstance(prop, dict) or "start" not in prop or "end" not in prop:
        raise ValueError(f"{where}: start·end 필수 — 받은 값: {prop!r}")

    def sec(field, v):
        if not isinstance(v, int) or isinstance(v, bool):   # bool은 int의 서브클래스
            raise ValueError(f"{where}.{field}: 초 정수여야 함 — 받은 값: {v!r}")
        return v

    start, end = sec("start", prop["start"]), sec("end", prop["end"])
    breaks = []
    for i, pair in enumerate(prop.get("breaks") or [], 1):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError(f"{where}.breaks[{i}]: [시작초, 끝초] 2원소여야 함 — 받은 값: {pair!r}")
        breaks.append((sec(f"breaks[{i}][0]", pair[0]), sec(f"breaks[{i}][1]", pair[1])))
    check_span(where, start, end, breaks)
    return {"start": start, "end": end, "breaks": breaks, "source": "llm"}

def resolve_bounds(m, parsed_docs: dict) -> tuple[dict, list]:
    """우선순위: manifest > llm 제안(sem 파일 존재 시) > 전체 구간(full)."""
    used, notes = {}, []
    for pid, doc in parsed_docs.items():
        if pid in m.bounds:
            b = dict(m.bounds[pid]); b["source"] = "manifest"
        else:
            b = _llm_bounds(m.artifacts_dir / "llm" / f"{pid}_semantic.json", pid) or {
                "start": 0, "end": doc.get("duration_sec", 0) or UNKNOWN_END,
                "breaks": [], "source": "full"}
        b["breaks"] = [tuple(x) for x in b["breaks"]]
        used[pid] = b
        notes.append(f"{pid}: bounds 출처 = {b['source']}")
    return used, notes

def _publish(b: dict) -> dict:
    """산출물용 직렬화. 내부 센티넬은 null로 낸다 — s70이 bounds_used를 측정 구간 줄로
    렌더하므로 그대로 두면 리포트에 31년짜리 구간이 찍힌다."""
    return {k: (None if k == "end" and v >= UNKNOWN_END else
                [list(x) for x in v] if k == "breaks" else v)
            for k, v in b.items()}

def measured_hours(b: dict) -> float | None:
    """측정 구간 길이(시간). 길이 미상이면 None.

    센티넬(10^9초)을 그대로 더하면 bounds_hours가 27만 시간이 되고
    시간당 지표가 전부 0으로 무너진다 — 합계에서 빼고 비고로 남긴다."""
    if b["end"] >= UNKNOWN_END:
        return None
    return max(0, (b["end"] - b["start"]) - sum(e - s for s, e in b["breaks"])) / 3600

# ── 정밀 오디오 지표 (whisper 세그먼트 기반) ────────────────────────────────
# 구간 필터는 텍스트 지표와 같은 in_bounds다(바깥 [start, end] 닫힘 · 휴식 [s, e) 반열림).

def _audio_bounds(b: dict, segments: list) -> tuple[dict, bool]:
    """오디오 계산용 실효 구간 — (구간, 대체했는가).

    bounds.end 센티넬은 '길이를 모른다'는 뜻이지 실제 구간이 아니다(전사 없는 오디오 전용
    교시: parsed duration_sec = 0이라 full 폴백이 센티넬을 쓴다). 오디오는 클로바가 아니라
    whisper 세그먼트로 계산하므로 그 교시도 지표를 낼 수 있다 — 세그먼트 범위를 실효 구간
    으로 삼고, 대체 사실을 비고로 남긴다. metrics.json의 bounds_used.end는 null 그대로라
    (측정 구간 합계에서 빼야 하므로) 비고가 없으면 둘이 어긋나 보인다."""
    if b["end"] < UNKNOWN_END or not segments:
        return b, False
    # 세그먼트 end는 실수다 — int로 자르면 그 뒤 소수점 시작 세그먼트가 구간 밖이 된다.
    return {**b, "end": max(s["end"] for s in segments)}, True

def _spans_break(t0: float, t1: float, b: dict) -> bool:
    """간격 [t0, t1)이 휴식과 겹치는가.

    양끝 세그먼트가 둘 다 구간 안이면 세그먼트 필터로는 걸러지지 않는다 — 그대로 두면
    10분짜리 휴식이 '최장 침묵' 1위로 리포트에 올라오고 침묵 총량을 통째로 지배한다
    (DESIGN §6: 침묵은 breaks 구간 제외)."""
    return any(t0 < e and s < t1 for s, e in b["breaks"])

def _talk(segments: list, b: dict) -> tuple[float, int]:
    """구간 내 발화 시간(초)·한글 음절 수 — 말속도의 분모·분자."""
    talk, syl = 0.0, 0
    for s in segments:
        if in_bounds(s["start"], b):
            talk += max(0.0, s["end"] - s["start"])
            syl += korean.count_syllables(s["text"])
    return talk, syl

def _spm(syl: int, talk: float) -> float:
    return round(syl / (talk / 60), 1) if talk else 0.0

def _pairs(segments: list, b: dict):
    """구간 안 인접 세그먼트 쌍과 그 사이 간격 — 휴식을 낀 쌍은 뺀다."""
    inb = [s for s in segments if in_bounds(s["start"], b)]
    for a, c in zip(inb, inb[1:]):
        if not _spans_break(a["end"], c["start"], b):
            yield a, c, c["start"] - a["end"]

def speech_rate(segments: list, b: dict) -> tuple[float, list]:
    """(전체 SPM, 5분 창 목록). SPM = 한글 음절 수 ÷ 발화 시간(세그먼트 duration 합, 분).

    발화 시간이 분모라 무음은 애초에 들어오지 않는다 — 창은 녹음 시각 0 기준으로 끊는다.

    세그먼트가 하나도 없는 중간 창은 spm=None으로 채운다(브리프 P1/2-2: 누락 구간 사유 명시).
    창을 아예 빼면 리포트 독자에게는 '측정 안 한 구간'과 '무발화 구간'이 구분되지 않고,
    실제로 s03 p06 50:00~60:00이 표에서 소리 없이 사라졌다. None은 0.0과 다르다 —
    0.0은 '발화 시간이 0인데 창은 존재'가 아니라 계산 불능의 폴백이라 섞어 쓰면 안 된다."""
    windows = {}
    for s in segments:
        if not in_bounds(s["start"], b):
            continue
        acc = windows.setdefault(int(s["start"] // WINDOW_SEC), [0.0, 0])
        acc[0] += max(0.0, s["end"] - s["start"])
        acc[1] += korean.count_syllables(s["text"])

    def label(w):
        return f"{w * WINDOW_SEC // 60:02d}:00~{(w + 1) * WINDOW_SEC // 60:02d}:00"

    win = []
    if windows:
        for w in range(min(windows), max(windows) + 1):
            if w in windows:
                d, n = windows[w]
                win.append({"label": label(w), "spm": _spm(n, d)})
            else:
                win.append({"label": label(w), "spm": None})
    talk, syl = _talk(segments, b)
    return _spm(syl, talk), win

def silences(segments: list, b: dict, min_gap: float = 3.0) -> list:
    """구간 내 인접 세그먼트 간격 중 min_gap초 이상 — 임계값은 고정(DESIGN §6)."""
    return [{"start": int(a["end"]), "dur": int(gap)}
            for a, _c, gap in _pairs(segments, b) if gap >= min_gap]

def wait_times(segments: list, b: dict, min_gap: float = 0.5) -> list:
    """물음표로 끝나는 세그먼트 뒤의 간격 = 질문 후 기다림의 근사.

    whisper의 구두점에 기대는 근사다(DESIGN §6) — 리포트가 항상 '근사'라고 병기한다."""
    return [{"after_ts": int(a["end"]), "gap_sec": round(gap, 1)}
            for a, _c, gap in _pairs(segments, b)
            if a["text"].rstrip().endswith("?") and gap > min_gap]

def _read_whisper(f) -> dict:
    """artifacts/whisper/pNN.json 읽기 — 계약(segments 배열)을 지키지 않으면 계산하지 않고 멈춘다.

    조용히 0으로 넘기면 안 되는 이유: 말속도 0.0·침묵 0건은 '측정했더니 0'과 구분되지 않고,
    s70이 그대로 추세 CSV에 한 줄 적는다. 다음 회차부터 이 세션이 기준선이 되므로 되돌릴 수
    없다 — 망가진 파일은 '느려졌다/조용해졌다'는 거짓 개선으로 굳는다.
    (반대로 segments: []는 전 무음 녹음의 정직한 0이므로 그대로 통과시킨다.)"""
    where = f"artifacts/whisper/{f.name}"
    fix = "이 파일을 지우고 s10_transcribe.py --force로 다시 전사하세요"
    try:
        wj = read_json(f)
    except ValueError as e:
        # 전사는 교시당 수 분 걸린다 — 도중에 끊기면 쓰다 만 JSON이 남는다.
        raise ValueError(f"{where}: JSON 파싱 실패 — {fix} ({e})") from e
    if not isinstance(wj, dict) or "segments" not in wj:
        raise ValueError(f"{where}: s10 산출물 형식이 아님(segments 배열 필요) — {fix}")
    if not isinstance(wj["segments"], list):
        raise ValueError(f"{where}: segments가 배열이 아님 — {fix}")
    return wj

def _audio_metrics(m, bounds: dict, notes: list) -> dict | None:
    """artifacts/whisper/pNN.json이 하나라도 있으면 audio 필드를 만든다. 없으면 None(미측정)."""
    files = [(p.id, m.artifacts_dir / "whisper" / f"{p.id}.json") for p in m.periods]
    files = [(pid, f) for pid, f in files if f.is_file()]
    if not files:
        return None
    win_all, sil_all, wait_all, model = [], [], [], ""
    talk_tot, syl_tot = 0.0, 0
    for pid, f in files:
        wj = _read_whisper(f)
        model = wj.get("model") or model    # 한 교시만 모델이 비어도 헤더가 통째로 비지 않게
        segs = wj["segments"]
        b, substituted = _audio_bounds(bounds[pid], segs)
        if substituted:
            notes.append(f"{pid}: 오디오 실효 구간 = 세그먼트 범위(bounds 미확정)")
        _, wins = speech_rate(segs, b)
        # 교시 라벨을 앞에 붙인다 — 창 라벨은 교시마다 00:00부터 다시 시작한다.
        win_all += [{"label": f"{pid} {w['label']}", "spm": w["spm"]} for w in wins]
        gap_n = sum(1 for w in wins if w["spm"] is None)
        if gap_n:
            notes.append(f"{pid}: 무발화 5분 창 {gap_n}개(whisper 세그먼트 없음) — "
                         "말속도 표에 '무발화'로 표기")
        talk, syl = _talk(segs, b)
        talk_tot += talk
        syl_tot += syl
        sil_all += [{"period": pid, **g} for g in silences(segs, b)]
        wait_all += [{"period": pid, **g} for g in wait_times(segs, b)]
    notes.append("정밀 지표: 단일 레코더 강사 지배 가정 · 대기 시간은 구두점 근사")
    return {"model": model,
            "speech_rate_spm": _spm(syl_tot, talk_tot),
            "speech_rate_windows": win_all,
            "silence_min": round(sum(g["dur"] for g in sil_all) / 60, 1),
            "silence_n": len(sil_all),
            "silence_top": sorted(sil_all, key=lambda g: -g["dur"])[:5],
            "wait_times": wait_all,
            # 다음 회차 액션 판정 기준("gap 중앙값 3초")의 이번 회차 기준값 — 리포트 본문에
            # 실려야 다음 회차가 비교할 수 있다(브리프 P1-4).
            "wait_median_sec": (round(statistics.median(g["gap_sec"] for g in wait_all), 1)
                                if wait_all else None)}

def _spread(items: list, n: int) -> list:
    """전 구간에서 고르게 n개 — 결정론 표본(앞쪽 몰림 방지). n 이하면 전부."""
    if len(items) <= n:
        return items
    step = (len(items) - 1) / (n - 1)
    return [items[round(i * step)] for i in range(n)]

def _load_parsed(m) -> dict:
    docs = {}
    for per in m.periods:
        p = m.artifacts_dir / "parsed" / f"{per.id}.json"
        # 스테이지 순서가 어긋난 것뿐이므로 트레이스백이 아니라 고칠 방법을 알려준다.
        if not p.is_file():
            raise ValueError(f"{per.id}: artifacts/parsed/{per.id}.json 없음 — s20_parse.py를 먼저 실행하세요")
        docs[per.id] = read_json(p)
    return docs

def main():
    args = cli("결정론 지표 계산")
    m = load_manifest(args.manifest)
    out = m.artifacts_dir / "metrics.json"
    if not stage_guard(out, args.force):
        return
    parsed = _load_parsed(m)
    bounds, notes = resolve_bounds(m, parsed)
    hedging = fillers = inst_sent = 0
    hedging_ex = []   # 카운트와 같은 루프에서 수집 — 따로 세면 언젠가 카운트와 어긋난다
    # 화자 라벨(참석자 N)은 클로바가 파일마다 새로 매긴다 — 같은 강사가 교시마다 다른 번호를
    # 받으므로 라벨 키로 합산하면 뜻이 없다. 교시별 instructor_label로 강사/수강생에 귀속한다.
    share_totals = {"instructor": 0, "students": 0}
    total_h = text_h = 0.0   # text_h = 전사(클로바)가 있는 구간만 — 시간당 값의 분모
    for pid, doc in parsed.items():
        b = bounds[pid]
        h = measured_hours(b)
        if h is None:
            notes.append(f"{pid}: 길이 미상(bounds·전사 길이 없음) — 측정 구간 합계에서 제외")
        else:
            total_h += h
            # 클로바 없는 교시는 텍스트 지표의 분자에 한 건도 기여하지 못한다.
            # 그 시간을 분모에 넣으면 헤징 시간당 값이 교시 수만큼 묽어진다(추세가 조용히 좋아진다).
            if not doc.get("no_clova"):
                text_h += h
        if doc.get("no_clova"):
            notes.append(f"{pid}: 클로바 txt 없음 — 화자 미분리(강사 한정 지표는 혼합 상한)")
        if doc.get("low_share_override"):
            # 강사 점유율이 60% 미만인 교시다. 강사 한정 지표(헤징·필러)의 분자가
            # 다른 교시와 같은 뜻이 아니므로 시간당 값을 나란히 놓고 비교하면 안 된다.
            notes.append(f"{pid}: 수동 라벨 저점유율 오버라이드 — 시간당 지표 비교 주의")
        for blk in doc.get("blocks", []):
            # 발화 비율도 다른 지표와 같은 범위(측정 구간 내부)에서만 센다 —
            # 쉬는 시간·종료 후 잡담이 비율을 끌고 가지 않도록(DESIGN §5·§6).
            if not in_bounds(blk["ts"], b):
                continue
            is_instructor = blk["speaker"] == doc.get("instructor_label")
            share_totals["instructor" if is_instructor else "students"] += len(blk["text"])
            if not is_instructor:
                continue
            for sent in korean.split_sentences(blk["text"]):
                inst_sent += 1
                if korean.is_hedging(sent):
                    hedging += 1
                    hedging_ex.append({"period": pid, "ts": fmt_ts(blk["ts"]),
                                       "sentence": sent})
            fillers += korean.count_fillers(blk["text"])
    whole = sum(share_totals.values()) or 1
    if round(text_h, 4) != round(total_h, 4):
        notes.append(f"시간당 값 분모는 전사 있는 구간 {text_h:.2f}h "
                     f"— 측정 구간 합계 {total_h:.2f}h와 다름(클로바 없는 교시 제외)")
    notes.append("발화 비율은 측정 구간 내부 블록만 집계 · 교시별 강사 라벨 기준 강사/수강생 2분류")
    notes.append("필러는 하한선(ASR 정규화로 실제보다 적게 잡힘)")
    audio = _audio_metrics(m, bounds, notes)
    write_json(out, {
        "session": m.session_id,
        "bounds_used": {k: _publish(v) for k, v in bounds.items()},
        "bounds_hours": round(total_h, 4),
        "text": {"hedging_n": hedging,
                 "hedging_per_h": round(hedging / text_h, 1) if text_h else None,
                 # 대표 예시(전 구간 고른 표본) — 헤징이 '무엇'인지 리포트가 보여주기 위한
                 # 최소 근거. 전량은 여기 싣지 않는다(210건을 실으면 부록이 리포트를 삼킨다).
                 "hedging_examples": _spread(hedging_ex, HEDGING_EXAMPLE_N),
                 "text_hours": round(text_h, 4),
                 "filler_min_n": fillers,
                 "speaker_share": {k: round(v / whole, 4) for k, v in share_totals.items()},
                 "instructor_sentences": inst_sent},
        "audio": audio,
        "notes": notes,
    })
    spm = f" spm={audio['speech_rate_spm']}" if audio else ""
    print(f"metrics: hedging={hedging} fillers>={fillers} hours={total_h:.2f}{spm}")

if __name__ == "__main__":
    run(main)
