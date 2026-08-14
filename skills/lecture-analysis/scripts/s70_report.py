#!/usr/bin/env python3
"""s70: 3층 리포트(A 요약 / B 상세 / C 증거) 발행 + 추세 CSV upsert(opt-in).

추세 CSV(trend_metrics.csv)는 LA_GROWTH_DIR(--growth-dir)을 지정했을 때만 쓴다 —
지정이 없으면 upsert를 건너뛰고 C3에 '미기록'으로 남긴다(스펙 §5).

설계 근거: DESIGN §8 (AAR 골격·피드백 3질문·자기 비교 프레임·무점수).

LLM을 부르지 않는다. 여기서 새로 생기는 문장은 GUIDE·FIXED_NOTES의 고정 문구뿐이고 나머지는
전부 상류 산출물의 값이다 — 해석을 자유 생성하면 검증받지 않은 총평이 리포트에 들어오고(그게
DESIGN이 막으려는 것), 회차마다 문장이 달라져 추세 비교도 깨진다.

저하 모드가 정상 경로다: 교시 산출물이 실패·전사 없음일 수 있고(load_analysis), 오디오 지표는
아직 없고(audio: null), bounds.end는 길이 미상일 때 null이며, 리뷰·판정 자체가 llm_failed일 수
있다. 그 어느 것도 리포트를 못 내는 사유가 아니다 — 빠진 것은 A5·C2에 '미가용'으로 남긴다.
"""
import argparse, csv, datetime, os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib.aggregate import (LOW_SPM, normalize_share_pcts, pct_str, question_stats,
                           speech_obs_stats, speed_summary, time_allocation, wait_stats)
from lib.clova import fmt_ts, parse_ts
from lib.manifest import load_manifest
import re
from lib.prev import _FIELD_SEP, resolve_prev
from lib.stage import PIPELINE_VERSION, stage_guard, load_analysis, require_json, run

TREND_COLS = ["date", "project", "session", "bounds_h", "apology_n", "selfqa_max",
              "hedging_n", "hedging_per_h", "trouble_n", "trouble_min",
              "speech_rate_spm", "silence_min", "filler_min_n", "notes"]
A_LINE_CAP = 30          # DESIGN §8.3: A층은 1화면(약 30행) 상한

# 해석 가이드는 이 사전에서만 — metrics.md 정의에서 온 고정 문구다(자유 생성 금지, DESIGN §8.3).
# 톤: 독자는 강사 본인이다 — 규제 문구("금지", "판정")가 아니라 읽는 법을 권하는 어조로 쓴다
# (Shute 2008의 supportive — 뜻과 한계 고지는 그대로, 어휘만 코치의 것으로).
GUIDE = {
    "hedging": "'것 같-'류 단정 회피의 대리 지표 — 오늘 값보다 추세로 보는 게 정확합니다",
    "apology": "협의 사과만 집계(습관성·사전 양해는 따로 모음) — 예시 인용으로 맥락을 봐 주세요",
    "self_qa": "단일 마이크 특성상 상한값 — 녹음 환경이 같으면 추세 비교는 유효합니다",
    "trouble": "진행이 실제 멈춘 장애만 — 다음 회차 리허설 체크리스트의 재료입니다",
    "speech_rate": "분당 한글 음절 수 — 단일 레코더라 강사 발화 위주로 잡힙니다",
    "silence": "3초 이상 무발화 — 실습 순회인지 침묵인지는 B4 타임라인과 함께 봐 주세요",
    "wait": "물음표 종료 세그먼트 뒤 간격 — 구두점에 기댄 근사치입니다",
    "filler": "어·음 단독 토큰의 하한선 — ASR 정규화로 실제보다 적게 잡힙니다",
}
# 정확도 한계 사전 고지 — 미고지 도구는 신뢰가 무너진다(DESIGN §8.1 원리 8).
FIXED_NOTES = [
    "① 수강생 발화는 오프마이크 누락·ASR 오인식이 있을 수 있어요 — 관측치는 하한이고, 개인별 판단에는 쓰지 않습니다",
    "② 전달 지표는 청중 경험·습관 지표이며 학습 성과 지표가 아님 — 지표가 움직여도 수업의 가치 판단이 아닙니다(문헌 충돌)",
    "③ 자동 관측은 사람의 판단과 다를 수 있어요 — 단회가 아니라 추세로 해석해 주세요",
    "④ 인용 원문에는 ASR 오인식이 섞일 수 있어요 — 원문은 보존하고 보정은 병기로만 합니다(발화가 아니라 전사 품질 문제)",
]
# 리포트가 독자에게 직접 말을 거는 유일한 자리 — 평가 문서가 아님을 첫 줄에서 못박는다.
PREFACE = ("> 이 리포트는 평가가 아니라 다음 회차를 위한 복기 노트입니다. "
           "수치는 잘잘못이 아니라 추세의 재료로, 인용은 그날의 기록으로 읽어 주세요.")

def anchor(ref: str) -> str:
    """증거 참조 → 마크다운 앵커 id. "sem:q_p01_001" → "sem-q_p01_001".

    접두(sem/str/goal)가 출처를 가른다 — s30과 s40이 같은 q_pNN_NNN 네임스페이스를 쓰므로
    접두를 빼면 링크가 엉뚱한 인용으로 간다."""
    return str(ref).replace(":", "-")

def delta_mark(cur, prev) -> str:
    """직전 회차 대비 방향 표시. 좋음/나쁨 판정이 아니다(DESIGN §8.3 — 색·이모지 판정 금지).

    비교 대상이 없거나(첫 측정) 어느 한쪽이 수치가 아니면(정밀 지표 미측정 회차의 빈 칸)
    표시를 붙이지 않는다 — 없는 비교를 '→'로 그리면 '변화 없음'이라는 가짜 정보가 된다."""
    if prev is None or prev == "":
        return ""
    try:
        cur, prev = float(cur), float(prev)
    except (TypeError, ValueError):
        return ""
    return "▲" if cur > prev else ("▼" if cur < prev else "→")

def _read_trend(csv_path: pathlib.Path) -> list:
    if not csv_path.exists():
        return []
    with csv_path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))

def trend_upsert(csv_path, row: dict) -> None:
    """세션 1행 upsert — 같은 (project, session)이 있으면 교체한다.

    append가 아니라 교체인 이유: --force 재발행이 같은 회차를 두 번 쌓으면 추세 미니표에
    같은 회차가 두 줄로 나오고 직전 대비 비교가 자기 자신과의 비교가 된다.
    None은 빈 칸으로 적는다 — "None" 문자열이 들어가면 다음 회차의 delta_mark가
    숫자로 읽으려다 실패해 비교가 조용히 사라진다."""
    csv_path = pathlib.Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [r for r in _read_trend(csv_path)
            if not (r.get("project") == row.get("project")
                    and r.get("session") == row.get("session"))]
    rows.append({k: "" if row.get(k) is None else str(row.get(k, "")) for k in TREND_COLS})
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TREND_COLS, restval="", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

def trend_tail(csv_path, project: str, session: str, n: int = 5) -> list:
    """같은 프로젝트의 다른 회차 최근 n행 — 추세 미니표의 유일한 입력.

    자기 비교 프레임(DESIGN §8.1 원리 5)이라 다른 프로젝트 행은 섞지 않는다.
    이번 회차를 빼는 건 재발행 시 자기 자신이 '직전'이 되는 것을 막기 위해서다."""
    rows = [r for r in _read_trend(pathlib.Path(csv_path))
            if r.get("project") == project and r.get("session") != session]
    rows.sort(key=lambda r: (r.get("date") or "", r.get("session") or ""))
    return rows[-n:]

def parse_any(ts) -> int:
    """시각 1건 → 초. 파싱 불가면 0 — 지속 시간 계산이 리포트 발행을 죽이지 않게 한다."""
    try:
        return parse_ts(str(ts))
    except (ValueError, AttributeError):
        return 0

def _int(v, alt: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return alt

def _num(v, alt: str = "미산출") -> str:
    """수치 렌더 — None은 "None"이 아니라 미산출로 적는다(전사 없는 세션의 시간당 값)."""
    return alt if v is None else str(v)

def _q(text, limit: int | None = None) -> str:
    """자유 텍스트 1건을 한 줄로 접는다(필요하면 잘라서 — 잘림은 …로 드러낸다).

    전사 인용에는 줄바꿈이 섞여 있다(클로바 블록은 여러 줄이다). 그대로 렌더하면 마크다운
    목록 항목이 중간에서 끊기고, A층에서는 한 항목이 여러 행으로 세어져 1화면 상한 계산까지
    어긋난다. 자르되 표시 없이 자르지 않는다 — '…로, 실'처럼 문장이 소리 없이 끊긴 것이
    브리프 P1-1의 신뢰 훼손 사례다. 사유·설명 필드는 아예 limit 없이 부른다."""
    s = " ".join(str(text if text is not None else "").split())
    if limit and len(s) > limit:
        return s[: limit - 1].rstrip() + "…"
    return s

def _ts(ts) -> str:
    """시각 1건 표기 — 빈 값은 '[p02 ]' 같은 빈 괄호가 아니라 '시각 미상'으로(브리프 P1-2)."""
    s = str(ts if ts is not None else "").strip()
    return s or "시각 미상"

_pct = pct_str   # 캐논 % 표기(소수 1자리·half-up) — 규칙은 lib/aggregate.pct_str 한 곳뿐(P1-3)

def _span(b: dict) -> str:
    """측정 구간 1건. end는 null일 수 있다(길이 미상) — 센티넬을 그대로 찍으면 31년짜리 구간이 된다."""
    start = fmt_ts(_int(b.get("start")))
    end = fmt_ts(_int(b.get("end"))) if b.get("end") is not None else "(끝 미상)"
    breaks = b.get("breaks") or []
    tail = f" · 휴식 {len(breaks)}건" if breaks else ""
    return f"{start}~{end} · 출처 {b.get('source', '?')}{tail}"

def collect_evidence(sem: dict, struct: dict, goal: dict) -> list:
    """C1용 (앵커 id, 인용, 시각, 교시) 목록 — B층 링크가 갈 수 있는 목적지 전체.

    인용이 빈 항목(커버리지 '스킵' 판정)은 목적지가 되지 못한다: id는 붙어 있지만 보여줄
    원문이 없다. 그래서 B층은 이 목록에 실린 id로만 링크를 건다 — 앵커 무결성(DESIGN §8.3)은
    렌더 규칙이지 사후 검사가 아니다."""
    out = {}

    def walk(obj, prefix, period):
        if isinstance(obj, dict):
            if obj.get("id"):
                q = (obj.get("quote") or obj.get("question_quote")
                     or obj.get("instructor_quote") or "")
                if isinstance(q, str) and q:
                    # ts가 없는 항목(트러블은 start_ts만 갖는다)은 구간 시작 시각으로 대신한다
                    # — 빈 시각을 그대로 실으면 C1에 '[p02 ]'가 찍힌다(브리프 P1-2).
                    ts = obj.get("ts") or obj.get("start_ts") or ""
                    out.setdefault(f"{prefix}-{obj['id']}", (q, ts, period))
            for v in obj.values():
                walk(v, prefix, period)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, prefix, period)

    for pid, d in sem.items():
        walk(d, "sem", pid)
    for pid, d in struct.items():
        walk(d, "str", pid)
    for v in goal.get("verdicts") or []:
        if not isinstance(v, dict):
            continue
        for q in v.get("quotes") or []:
            if isinstance(q, dict) and q.get("id") and q.get("quote"):
                out.setdefault(f"goal-{q['id']}",
                               (q["quote"], q.get("ts", ""), q.get("period", "")))
    return [(aid, quote, ts, pid) for aid, (quote, ts, pid) in out.items()]

def summarize(metrics: dict, sem: dict, struct: dict, goal: dict, review: dict) -> dict:
    """리포트와 추세 CSV가 함께 쓰는 세션 합계 — 한 곳에서만 센다.

    두 곳에서 따로 세면 리포트의 사과 수와 CSV의 apology_n이 언젠가 갈라지고, 그때 추세선은
    설명 불가능해진다(어느 쪽이 맞는지 알 방법이 없다)."""
    troubles = [(pid, tr) for pid, d in sem.items() for tr in (d.get("troubles") or [])]
    dropped = (sum(_int(d.get("dropped_quotes")) for d in list(sem.values()) + list(struct.values()))
               + _int(goal.get("dropped_quotes")) + _int(review.get("dropped_items")))
    return {
        "apology_n": sum(len((d.get("apologies") or {}).get("counted") or []) for d in sem.values()),
        "selfqa_max": sum(len(d.get("self_qa") or []) for d in sem.values()),
        "troubles": troubles,
        "trouble_min": sum(max(0, parse_any(tr.get("end_ts")) - parse_any(tr.get("start_ts")))
                           for _, tr in troubles) // 60,
        "dropped": dropped,
    }

def render(ctx: dict) -> str:
    m, metrics = ctx["manifest"], ctx["metrics"]
    sem, struct = ctx["sem"], ctx["struct"]
    goal, review = ctx["goal"], ctx["review"]
    s, t = ctx["summary"], (metrics.get("text") or {})
    au = metrics.get("audio") or {}
    prev_rows = ctx["prev_rows"]
    prev = prev_rows[-1] if prev_rows else {}
    unavailable, orphans = ctx["unavailable"], ctx["orphans"]
    review_failed = "llm_failed" in review
    # 리뷰가 없는 사유는 둘이고 강사가 할 일이 다르다. s65 호출이 실패한 것이라면 s65를
    # 다시 돌리면 되지만, 상류 관측이 미완이라 s65가 아예 호출하지 않은 경우는 s65를 몇 번
    # 돌려도 같다 — 고칠 곳은 s30·s40이다. 둘을 같은 문구로 적으면 리포트가 헛수고를 시킨다.
    # failed_kind가 정식 신호이고, 문자열 검사는 이 키가 없던 시절의 산출물을 위한 하위 호환이다.
    upstream_gap = review_failed and (
        review.get("failed_kind") == "upstream_incomplete"
        or str(review.get("llm_failed", "")).startswith("상류 관측 미완"))
    review_why = ("미생성(상류 관측 미완 — s30/s40 재실행 필요)" if upstream_gap
                  else "미생성(LLM 실패)")
    anchors = {aid for aid, *_ in ctx["evidence"]}
    good, bad = review.get("good") or [], review.get("bad") or []
    cands = review.get("action_candidates") or []
    bounds = metrics.get("bounds_used") or {}
    hours = metrics.get("bounds_hours")
    hours_s = f"{hours:.2f}" if isinstance(hours, (int, float)) else "미상"

    def ref(aid: str, label: str = "근거") -> str:
        """C1에 원문이 실재하는 id만 링크한다 — 인용 없는 항목(커버리지 스킵)·지표 참조는 링크 없음."""
        return f" [{label}](#{aid})" if aid in anchors else ""

    def evidence_refs(item: dict) -> str:
        out = []
        for r in item.get("evidence") or []:
            aid = anchor(r)
            out.append(f"[근거](#{aid})" if aid in anchors else f"`{r}`")
        return " ".join(out)

    def dm(key: str, cur) -> str:
        return f"{_num(cur)}{delta_mark(cur, prev.get(key))}"

    # 지표 한 줄 — metrics.md 기록 형식 그대로(DESIGN §6 s70). 형식이 바뀌면 추세가 리셋된다.
    unmeasured = [pid for pid, b in bounds.items()
                  if b.get("source") == "full" or b.get("end") is None]
    metric_line = (
        f"사과 {s['apology_n']}회 · 자문자답 ≤{s['selfqa_max']} · "
        f"헤징 {_num(t.get('hedging_n'))}문장(시간당 {_num(t.get('hedging_per_h'))}) · "
        f"트러블 {len(s['troubles'])}건/약 {s['trouble_min']}분 "
        f"(측정 구간: {hours_s}h, 누락: "
        f"{'없음' if not unmeasured else ', '.join(unmeasured) + ' 구간 미확정'})")

    L = []
    A = L.append
    # 제목은 label에서만 온다 — 배포판 뼈대 manifest에는 project·id가 없어서
    # 옛 표기는 '#  강의 분석 리포트 —  (날짜)'가 됐다. label은 name→project→id로
    # 강등하며 절대 비지 않고, 산출물 파일명과도 같은 강의명을 쓴다(스펙 §4).
    A(f"# {m.label} 강의 분석 리포트 ({m.date})")
    A("")
    A(PREFACE)
    A("")
    A("## A. 한눈 요약")
    A("### A1. 목표 점검")
    verdicts = goal.get("verdicts") or []
    if "llm_failed" in goal:
        A("- 판정 미생성(LLM 실패) — s60_goalcheck.py를 다시 실행하면 재시도됩니다")
    elif goal.get("first_analysis"):
        A("- 첫 분석 — 직전 액션 없음")
    elif not verdicts:
        A(f"- 직전 산출물({goal.get('source', 'none')})에 판정할 액션 없음")
    else:
        for v in verdicts:
            qs = [q for q in (v.get("quotes") or []) if isinstance(q, dict)]
            aid = f"goal-{qs[0].get('id')}" if qs else ""
            A(f"- **{_q(v.get('verdict'))}** {_q(v.get('action'), 40)} — "
              f"{_q(v.get('reason'), 40)}{ref(aid)}")
    A("### A2. 전달 지표 ([상세](#b1))  ·  직전 대비: "
      + (f"헤징 {dm('hedging_n', t.get('hedging_n'))} · 사과 {dm('apology_n', s['apology_n'])}"
         if prev_rows else "첫 측정(비교 대상 없음)"))
    A(metric_line)
    A("### A3. 하이라이트")
    if review_failed:
        A(f"- {review_why} — [B6](#b6) 참조")
    elif not good and not bad:
        A(f"- 합성 없음 — {_q(review.get('note', '근거로 삼을 관측 없음'))} ([B6](#b6))")
    else:
        # A3는 현상 1줄만 — 원인·근거는 B6에만 둔다(브리프 P2-3: 요약·상세 정보량 차등).
        for g in good[:2]:
            A(f"- 잘된 것: {_q(g.get('point'))} ([상세](#b6))")
        for b in bad[:2]:
            A(f"- 아쉬운 것: {_q(b.get('point'))} ([상세](#b6))")
    A("### A4. 다음 액션 후보 ([확정은 B7](#b7))")
    if review_failed:
        A(f"- {review_why}")
    elif not cands:
        A("- 후보 없음")
    for a in cands:
        A(f"- {_q(a.get('action'))}")
    if ctx["stale_goal"]:
        A("- 직전 목표 설정 후 2주 이상 경과 — 갱신 검토(효과 감쇠 2~3주 근거)")
    A("### A5. 측정 신뢰도")
    bounds_src = ", ".join(f"{pid}:{b.get('source', '?')}" for pid, b in bounds.items()) or "없음"
    A(f"- bounds 출처: {bounds_src} · 인용/항목 폐기 {s['dropped']}건"
      f" · 오디오 {'있음' if au else '없음(정밀 지표 미측정)'}")
    if unavailable:
        head = "; ".join(unavailable[:2])
        more = f" 외 {len(unavailable) - 2}건" if len(unavailable) > 2 else ""
        A(f"- 미가용 {len(unavailable)}건: {head}{more} — 전체는 [C2](#c2)")
    if orphans:
        A(f"- manifest 제외 교시 {', '.join(orphans)} 존재(리포트 미포함) — [C2](#c2)")
    A("")

    A('<a id="b1"></a>')
    A("## B1. 전달 지표 상세")
    text_h = t.get("text_hours")
    # 시간당 값의 분모는 '전사 있는 구간'이라 측정 구간 합계와 다를 수 있다(s50 계약).
    # 분모를 숨기면 클로바 없는 교시가 낀 회차의 시간당 값이 왜 튀는지 읽을 수 없다.
    denom = f" · 분모 전사 구간 {text_h:.2f}h" if isinstance(text_h, (int, float)) else ""
    A(f"- 헤징 {_num(t.get('hedging_n'))}문장(시간당 {_num(t.get('hedging_per_h'))}{denom})"
      f" — {GUIDE['hedging']}")
    # 핵심 지표에 예시 인용 0건이면 독자가 '무엇을 셌는지' 검증할 수 없다(브리프 P1-4).
    hx = t.get("hedging_examples") or []
    for ex in hx:
        A(f"  - [예시] [{ex.get('period', '?')} {_ts(ex.get('ts'))}] "
          f"“{_q(ex.get('sentence'), 80)}”")
    if hx:
        A(f"  - (예시 {len(hx)}건은 전 구간 고른 표본 — 전량·원값은 metrics.json)")
    A(f"- 필러(하한) {_num(t.get('filler_min_n'))} — {GUIDE['filler']}")
    A(f"- 자문자답 ≤{s['selfqa_max']} — {GUIDE['self_qa']}")
    # 집계분과 제외분을 분리한다(브리프 P2-1) — 한 목록에 섞으면 헤드라인 "6회"와
    # 목록 길이 14건이 어긋나 보인다. 제외 사유는 자르지 않는다(P1-1).
    counted = [(pid, it) for pid, d in sem.items()
               for it in ((d.get("apologies") or {}).get("counted") or [])]
    excluded = [(pid, it) for pid, d in sem.items()
                for it in ((d.get("apologies") or {}).get("excluded") or [])]
    A(f"- 사과 {s['apology_n']}회 — {GUIDE['apology']}")
    for pid, it in counted:
        A(f"  - [집계] [{pid} {_ts(it.get('ts'))}] “{_q(it.get('quote'), 60)}”"
          f"{ref('sem-' + str(it.get('id')), '원문')}")
    if excluded:
        A(f"- 사과 제외 {len(excluded)}건 (집계 밖 — 습관성 추임새·사전 양해·자기 정정·"
          "측정 구간 밖):")
        for pid, it in excluded:
            A(f"  - [제외] ({_q(it.get('reason'))}) “{_q(it.get('quote'), 60)}”"
              f"{ref('sem-' + str(it.get('id')), '원문')}")
    share = t.get("speaker_share") or {}
    A(f"- 발화 비율(측정 구간 내): 강사 {_pct(share.get('instructor'))} · "
      f"수강생 {_pct(share.get('students'))} — 단일 마이크 관측이라 수강생 쪽은 하한")
    if prev_rows:
        A("")
        A("| 회차 | 헤징 | 시간당 | 사과 | 자문자답≤ | 트러블 |")
        A("|---|---|---|---|---|---|")
        for r in prev_rows:
            A(f"| {r.get('session', '')} | {r.get('hedging_n', '')} | {r.get('hedging_per_h', '')} "
              f"| {r.get('apology_n', '')} | {r.get('selfqa_max', '')} | {r.get('trouble_n', '')} |")
        # 이 행만 label이 아니라 session_id다 — 위 행들이 CSV의 session 열이라 회차 키를
        # 섞으면 같은 열에 다른 뜻의 값이 선다(추세 CSV는 LA_GROWTH_DIR 옵트인 레거시).
        A(f"| **{m.session_id}** | {_num(t.get('hedging_n'))} | {_num(t.get('hedging_per_h'))} "
          f"| {s['apology_n']} | {s['selfqa_max']} | {len(s['troubles'])} |")
    else:
        A("- 추세 미니표: 첫 측정 — 비교할 직전 회차 없음")
    A("")

    A("## B2. 정밀 음성 지표")
    if not au:
        A("- 미측정(오디오 없음)")
    else:
        A(f"- 말속도 평균 {_num(au.get('speech_rate_spm'))} 음절/분 — {GUIDE['speech_rate']}")
        # 본문은 교시별 요약, 64행 원자료 표는 접이식 부록으로(브리프 P2-2).
        # 평균·최저를 유효 창으로만 계산하는 이유: 18.0 같은 저속 창은 침묵·개별 실습의
        # 파편 발화라 실제 말속도 축과 다른 값이다 — 섞으면 '말이 느려졌다'로 오독된다.
        speed_rows = speed_summary(au)
        if speed_rows:
            A(f"- 교시별 요약 (5분 창 · 평균/최고/최저는 유효 창 = {LOW_SPM} 음절/분 이상만):")
            A("")
            A("| 교시 | 창 | 평균 | 최고 | 최저(유효) | 저속·무발화 창 | 추이 |")
            A("|---|---|---|---|---|---|---|")
            for r in speed_rows:
                A(f"| {r['period']} | {r['windows']} | {_num(r['avg'])} | {_num(r['max'])} "
                  f"| {_num(r['min_valid'])} | {r['low_n']} | {r['spark']} |")
            A("")
            A("<details><summary>원자료 — 5분 창 말속도 전체 표 "
              f"({sum(r['windows'] for r in speed_rows)}행 · 기본 접힘)</summary>")
            A("")
            A("| 구간 | 음절/분 |")
            A("|---|---|")
            for w in au["speech_rate_windows"]:
                spm = w.get("spm")
                if spm is None:
                    A(f"| {w.get('label', '')} | 무발화(세그먼트 없음) |")
                elif isinstance(spm, (int, float)) and spm < LOW_SPM:
                    A(f"| {w.get('label', '')} | {spm} † |")
                else:
                    A(f"| {w.get('label', '')} | {spm} |")
            A("")
            A(f"† = {LOW_SPM} 음절/분 미만(침묵·개별 실습 추정 — 실제 말속도로 읽지 않기)")
            A("</details>")
            A("")
        A(f"- 침묵 총 {_num(au.get('silence_min'))}분/{_num(au.get('silence_n'))}건 — {GUIDE['silence']}")
        for si in au.get("silence_top") or []:
            A(f"  - {si.get('period', '')} {fmt_ts(_int(si.get('start')))} ~ {si.get('dur', '')}초")
        # 대기 시간은 건수만 적으면 근거 없는 지표가 된다(브리프 P1-4) — 실측값 전건 +
        # 다음 회차 판정 기준("gap 중앙값 3초")이 비교할 이번 회차 기준값을 함께 싣는다.
        ws = wait_stats(au)
        med = (f" · gap 중앙값 {ws['median']}초(다음 회차 판정 기준의 이번 값)"
               if ws["median"] is not None else "")
        A(f"- 대기 시간 근사 {len(ws['items'])}건{med} — {GUIDE['wait']}")
        for wt in ws["items"]:
            A(f"  - [{wt.get('period', '?')} {fmt_ts(_int(wt.get('after_ts')))} 질문 직후] "
              f"{wt.get('gap_sec', '?')}초")
    A("")

    A("## B3. 질문·참여")
    # 유형별 응답/무응답 집계 — HTML 질문 차트의 원천 수치(브리프 2-5·3-B SSOT).
    qs = question_stats(struct)
    if qs["total"]:
        kinds_s = " · ".join(
            f"{r['kind']} {r['answered'] + r['unanswered']}건(응답 {r['answered']})"
            for r in qs["kinds"])
        A(f"- 집계: {kinds_s} — 계 {qs['total']}건 중 응답 관측 {qs['answered']}"
          f" · uptake {qs['uptake_n']}건")
    for pid, d in struct.items():
        for q in d.get("questions") or []:
            mark = "응답 관측" if q.get("answered") else "무응답"
            # 오인식 의심 인용은 원문을 덮지 않고 추정을 병기한다(브리프 P3-2).
            guess = f" (추정: {_q(q.get('asr_guess'), 30)})" if q.get("asr_guess") else ""
            A(f"- [{pid} {_ts(q.get('ts'))}] ({_q(q.get('kind'))}·{mark}) "
              f"“{_q(q.get('quote'), 50)}”{guess}{ref('str-' + str(q.get('id')), '원문')}")
        for u in d.get("uptake") or []:
            A(f"- uptake({_q(u.get('kind'))}) [{pid} {_ts(u.get('ts'))}] "
              f"“{_q(u.get('instructor_quote'), 50)}”{ref('str-' + str(u.get('id')), '원문')}")
    A("- 관측 한계: uptake·무응답은 단일 마이크로 보인 구간만의 기록이라 비율로 읽지 않는 게 "
      "안전합니다")
    A("- 전사 참고: 이해확인 발화의 오인식(예: '널리긴'→'열리긴' 추정류)은 발화가 아니라 "
      "전사 품질 문제예요 — 원문은 보존하고 보정은 병기로만 합니다")
    A("")

    A("## B4. 시간 배분·커버리지")
    # 배분 요약표를 타임라인 앞에 — "실습 시간이 충분했나"에 수기 계산 없이 답하게 한다
    # (브리프 P2-4). 이 표가 HTML 시간 배분 스택 바의 원천 수치다(SSOT).
    ta = time_allocation(struct)
    if ta["per"]:
        A(f"- 활동 유형별 배분 요약 (타임라인 자동 집계 · 총 {ta['grand']:.0f}분):")
        A("")
        A("| 교시 | " + " | ".join(ta["order"]) + " | 계 |")
        A("|---" * (len(ta["order"]) + 2) + "|")
        for pid, row in ta["per"].items():
            A(f"| {pid} | " + " | ".join(str(row.get(l, 0)) for l in ta["order"])
              + f" | {round(sum(row.values()), 1)} |")
        A("| **계(분)** | " + " | ".join(str(ta["total"][l]) for l in ta["order"])
          + f" | {ta['grand']} |")
        g = ta["grand"] or 1
        A("| **비중** | " + " | ".join(pct_str(ta["total"][l] / g) for l in ta["order"])
          + " | 100% |")
        A("")
    for pid, d in struct.items():
        for c in d.get("coverage") or []:
            # 스킵 판정은 근거 인용이 없는 게 정상이라 C1에 목적지가 없다 — 링크 없이 판정만.
            A(f"- 계획 대비: {_q(c.get('planned'), 60)} → **{_q(c.get('verdict'))}**"
              f"{ref('str-' + str(c.get('id')))}")
    n_seg = sum(len(d.get("timeline") or []) for d in struct.values())
    if n_seg:
        A(f"<details><summary>원자료 — 세그먼트 타임라인 전체 ({n_seg}개 · 기본 접힘)</summary>")
        A("")
        for pid, d in struct.items():
            for seg in d.get("timeline") or []:
                A(f"- [{pid}] {_ts(seg.get('start_ts'))}~{_ts(seg.get('end_ts'))} "
                  f"{_q(seg.get('label'))}: {_q(seg.get('topic'))}")
        A("")
        A("</details>")
    if not any((d.get("timeline") or d.get("coverage")) for d in struct.values()):
        A("- 관측 없음(구조 분석 미가용)")
    A("")

    A("## B5. 트러블 타임라인")
    A(f"- 트러블 {len(s['troubles'])}건/약 {s['trouble_min']}분 — {GUIDE['trouble']}")
    for pid, tr in s["troubles"]:
        # 설명은 자르지 않는다(브리프 P1-1 — B5 설명 미완성 사례) · 건별 지속 시간 병기.
        dur = max(0, parse_any(tr.get("end_ts")) - parse_any(tr.get("start_ts")))
        dur_s = f" (약 {max(1, round(dur / 60))}분)" if dur else ""
        A(f"- [{pid}] {_ts(tr.get('start_ts'))}~{_ts(tr.get('end_ts'))}{dur_s} "
          f"{_q(tr.get('summary'))}{ref('sem-' + str(tr.get('id')), '원문')}")
    A("")

    A('<a id="b6"></a>')
    A("## B6. 잘된 것 / 아쉬운 것")
    if upstream_gap:
        A(f"- {review_why}: {_q(review['llm_failed'], 80)} — "
          "s30_semantic.py·s40_structure.py가 관측을 낸 뒤 다시 실행하면 합성됩니다")
    elif review_failed:
        A(f"- 미생성(LLM 실패: {_q(review['llm_failed'], 80)}) — "
          "s65_review.py를 다시 실행하면 재시도됩니다")
    elif not good and not bad:
        A(f"- 미생성 — {_q(review.get('note', '근거로 삼을 관측 없음'))}")
    else:
        A("**잘된 것 (다음에도 그대로 가져갈 것):**")
        for g in good:
            A(f"- {_q(g.get('point'))} — 원인: {_q(g.get('cause'))} {evidence_refs(g)}".rstrip())
            if g.get("principle"):
                A(f"  - → 원리: {_q(g.get('principle'))}")
        A("")
        A("**아쉬운 것 (다음을 위한 원인 메모):**")
        for b in bad:
            A(f"- {_q(b.get('point'))} — 원인: {_q(b.get('cause'))} {evidence_refs(b)}".rstrip())
            if b.get("principle"):
                A(f"  - → 원리: {_q(b.get('principle'))}")
    A("")

    A('<a id="b7"></a>')
    A("## B7. 다음 액션 (강사가 고르는 자리 — 화법 목표는 회차당 1개면 충분합니다)")
    # 체크 표시의 뜻을 섹션 서두에 정의한다(브리프 P3-1) — 자동 생성은 항상 `[ ]`이고,
    # `[x]`는 확정 대화·직접 편집으로만 생긴다. --force 재발행 시 기존 확정을 보존한다.
    A("- 표기: `[ ]` = 추천(파이프라인이 낸 후보 — 고르기 전까지는 제안일 뿐) · "
      "`[x]` = 강사 확정(확정 대화·직접 편집으로만 기록 · 재발행에도 보존)")
    confirmed = ctx.get("confirmed_actions") or {}
    if review_failed:
        A(f"- {review_why} — 확정할 후보가 없습니다")
    elif not cands and not confirmed:
        A("- 후보 없음")
    matched = set()
    for a in cands:
        key = " ".join(_q(a.get("action")).split())
        mark = " "
        if key in confirmed:
            mark = "x"
            matched.add(key)
        A(f"- [{mark}] **액션**: {_q(a.get('action'))} · **적용 시점**: {_q(a.get('when'))} · "
          f"**판정 기준**: {_q(a.get('criteria'))} · **전략·도구·준비**: {_q(a.get('prep'))}")
    carried = [line for key, line in confirmed.items() if key not in matched]
    if carried:
        # 재합성으로 후보 문구가 바뀌어도 강사 확정은 소실되지 않는다 — 원본 줄 그대로 보존.
        A("- (아래 `[x]`는 직전 발행에서 강사가 확정한 액션 — 이번 후보 목록에는 없지만 보존)")
        for line in carried:
            A(line)
    A("")

    A("## B8. 화법 관찰 후보 · 재사용 후보 멘트")
    A("- (진단하지 않고 인용만 모아 둡니다 — 분기 회고 때 추세를 그릴 재료예요)")
    # 교시×유형 집계 — HTML 화법 관찰 차트의 원천 수치(SSOT).
    ob = speech_obs_stats(struct)
    if ob["grand"]:
        A(f"- 집계: 총 {ob['grand']}건 — "
          + " · ".join(f"{k} {ob['total'][k]}" for k in ob["kinds"]))
        A("")
        A("| 교시 | " + " | ".join(ob["kinds"]) + " | 계 |")
        A("|---" * (len(ob["kinds"]) + 2) + "|")
        for pid in sorted(ob["per"]):
            row = ob["per"][pid]
            A(f"| {pid} | " + " | ".join(str(row.get(k, 0)) for k in ob["kinds"])
              + f" | {sum(row.values())} |")
        A("")
    for pid, d in struct.items():
        for o in d.get("speech_observations") or []:
            guess = f" (추정: {_q(o.get('asr_guess'), 30)})" if o.get("asr_guess") else ""
            A(f"- ({_q(o.get('kind'))}) [{pid} {_ts(o.get('ts'))}] “{_q(o.get('quote'), 50)}”"
              f"{guess}{ref('str-' + str(o.get('id')), '원문')}")
        for rm in d.get("reusable") or []:
            A(f"- 재사용: “{_q(rm.get('quote'), 50)}” — {_q(rm.get('why'), 40)}"
              f"{ref('str-' + str(rm.get('id')), '원문')}")
    A("")

    A("## C1. 인용 증거 전문")
    if not ctx["evidence"]:
        A("- 없음(검증을 통과한 인용 0건)")
    for aid, quote, ts, pid in ctx["evidence"]:
        A(f'<a id="{aid}"></a>')
        A(f"- `{aid}` [{pid} {_ts(ts)}] “{_q(quote)}”")
    A("")

    A('<a id="c2"></a>')
    A("## C2. 측정 정의·비고")
    A("- 정의 SSOT: `.claude/skills/lecture-analysis/references/metrics.md`")
    for n in FIXED_NOTES:
        A(f"- {n}")
    for pid, b in bounds.items():
        A(f"- 측정 구간 {pid}: {_span(b)}")
    for n in metrics.get("notes") or []:
        A(f"- {_q(n)}")
    A(f"- 폐기 합계 {s['dropped']}건 "
      f"(인용 검증 위반·근거 없는 항목 — 상류 스테이지가 항목 단위로 버린 수)")
    if ctx.get("norm_n"):
        A(f"- 표기 정규화: 산문의 발화 비율 % 표기 {ctx['norm_n']}건을 캐논 표기"
          "(소수 1자리·half-up)로 통일 — 원값은 metrics.json speaker_share, 인용 원문은 미변경")
    for u in unavailable:
        A(f"- 미가용: {u}")
    if orphans:
        A(f"- manifest 제외 교시 {', '.join(orphans)}: parsed 산출물이 디스크에 남아 있으나 "
          "manifest에 없어 이 리포트의 어떤 값에도 반영되지 않았다(지표·추세 포함). "
          "의도한 제외가 아니면 manifest에 되돌리고 다시 실행한다")
    A("")

    A("## C3. 실행 메타")
    # 파이프라인 버전을 남기는 이유: 지표 정의가 바뀐 회차 앞뒤는 같은 뜻의 값이 아니다.
    # 추세선이 튀었을 때 정의 변경 때문인지 강의가 달라진 건지 되짚을 수 있는 유일한 표식이다.
    A(f"- 생성: {ctx['now']} · 파이프라인 v{PIPELINE_VERSION} · "
      f"LA_MODEL={ctx['model']} · whisper={ctx['whisper_model']}")
    A(f"- 스테이지 상태: 의미 {len(sem)}교시 · 구조 {len(struct)}교시 · "
      f"판정 {'미생성' if 'llm_failed' in goal else str(len(verdicts)) + '건'} · "
      f"리뷰 {'미생성' if review_failed else '생성'}")
    A(f"- 추세 CSV: {ctx['csv_path'] or '미기록(LA_GROWTH_DIR 미지정)'}")
    return "\n".join(L) + "\n"

def a_layer_rows(md: str) -> list:
    """A층의 '보이는 행' — 1화면 상한(DESIGN §8.3)이 세는 대상의 단일 정의.

    빈 줄과 앵커 타깃(<a id="b1"></a>)은 화면에 아무것도 그리지 않는다. B1 앵커는 A층과
    B1 헤딩 사이에 놓이므로 단순히 줄을 세면 보이지 않는 것이 1화면 예산을 먹는다."""
    a = md.split("## A. 한눈 요약")[1].split("## B1.")[0]
    return [l for l in a.splitlines() if l.strip() and not l.strip().startswith("<a id=")]

def growth_dir(m, override):
    """추세 CSV 폴더 — LA_GROWTH_DIR(--growth-dir) 지정 시에만 켜지는 개인 기능(스펙 §5).

    저장소 4단계 규약 파생을 지운 이유: 배포판 세션 폴더(LECTURE_ANALYSIS/…)는 그 규약
    위에 있지 않아 파생이 항상 실패했고, 조용히 엉뚱한 곳에 쌓는 것은 더 나쁘다 —
    추세 파일이 흩어지면 회차 비교가 리셋되고 다음 회차에야 '첫 측정'으로 드러난다."""
    return pathlib.Path(override) if override else None

def orphan_periods(m) -> list:
    """manifest에 없는 `artifacts/parsed/pNN.json` — 교시를 뺀 뒤 디스크에 남은 흔적.

    manifest에서 교시를 빼는 것은 LLM이 계속 실패하는 교시를 포기하는 공식 절차다
    (SKILL.md exit 4). 그런데 리포트의 미가용 목록은 manifest에 실린 교시만 센다 —
    뺀 교시는 '미측정'조차 아닌 존재하지 않는 교시가 되고, 그 회차 리포트는 남은
    교시만으로 '전 교시를 분석했다'처럼 읽힌다. 빠진 사실이 어디에도 남지 않는
    유일한 경로라 여기서 흔적을 되살린다(값에는 반영하지 않는다 — 알리기만 한다)."""
    known = {p.id for p in m.periods}
    d = m.artifacts_dir / "parsed"
    if not d.is_dir():
        return []
    return sorted(f.stem for f in d.glob("*.json") if f.stem not in known)

def normalize_prose(review: dict, goal: dict, share: dict) -> int:
    """리뷰·판정 '산문' 필드의 발화 비율 표기를 캐논으로 통일(브리프 P1-3) — 교체 수 반환.

    인용(quote) 필드는 목록에 없다 — 원문 보존이 검증 가능성의 전제라 절대 건드리지 않는다.
    LLM 재호출 없이 표기만 고치는 결정론 후처리이므로 재현성도 해치지 않는다."""
    n = 0

    def fix(d: dict, keys: tuple):
        nonlocal n
        for k in keys:
            if isinstance(d.get(k), str):
                d[k], c = normalize_share_pcts(d[k], share)
                n += c

    for item in (review.get("good") or []) + (review.get("bad") or []):
        if isinstance(item, dict):
            fix(item, ("point", "cause"))
    for a in review.get("action_candidates") or []:
        if isinstance(a, dict):
            fix(a, ("action", "when", "criteria", "prep"))
    for v in goal.get("verdicts") or []:
        if isinstance(v, dict):
            fix(v, ("action", "reason"))
    return n

def confirmed_actions_of(report_path: pathlib.Path) -> dict:
    """재발행 직전, 기존 report.md B7의 강사 확정(`- [x]`)을 건진다 —
    {공백 정규화한 액션 텍스트: 원본 줄 전체}.

    확정은 강사의 입력이지 파이프라인 산출물이 아니므로 재생성으로 잃으면 안 된다.
    원본 줄까지 보관하는 이유: s65를 다시 합성하면 후보 문구가 통째로 바뀔 수 있는데,
    텍스트 매칭만 하면 그 순간 확정이 조용히 소실된다(다음 회차 s60이 판정할 목표가
    사라진다). 매칭되는 후보가 있으면 그 후보에 [x]를 옮기고, 없으면 원본 줄을 그대로
    B7 끝에 되살린다 — 어느 쪽이든 강사가 확정한 문장은 리포트에 남는다."""
    if not report_path.exists():
        return {}
    out, in_b7 = {}, False
    for ln in report_path.read_text(encoding="utf-8").splitlines():
        if re.match(r"^##\s*B7", ln):
            in_b7 = True
            continue
        if in_b7 and ln.startswith("#"):
            break
        if not in_b7:
            continue
        mm = re.match(r"^- \[[xX]\] \*\*액션\*\*:\s*(.+)$", ln.strip())
        if mm:
            text = _FIELD_SEP.split(mm.group(1))[0].strip()
            out[" ".join(text.split())] = ln.strip()
    return out

def _prev_named_md(m) -> pathlib.Path | None:
    """개명 전 산출물 — outputs/의 최신 `*_분석데이터_*.md`(현재 이름 제외). 없으면 None.

    산출물 파일명은 manifest 파생(`{강의명}_분석데이터_{날짜}.md`)이다. 그래서 사용자가
    안내대로 `date`를 실제 강의일로 고치거나 강의명을 다듬는 순간, s70이 확정을 건지러 보는
    파일이 '아직 없는 새 이름'으로 바뀐다 — confirmed_actions_of가 {}를 돌려주고 강사가
    직접 찍은 `[x]`가 조용히 사라진다(다음 회차 s60이 판정할 목표가 없어진다). 확정은 강사의
    입력이지 파이프라인 산출물이라는 원칙(confirmed_actions_of 참조)은 이름이 바뀌어도 같다 —
    이름 대신 위치와 패턴으로 되찾는다.

    mtime 최신 하나만 보는 이유: 개명을 반복하면 옛 이름 파일이 여러 개 쌓이는데, 확정을
    옮겨 담을 원본은 가장 최근 발행분 하나뿐이다. 여러 파일의 확정을 합치면 이미 철회한
    옛 목표가 되살아난다."""
    cands = [p for p in m.outputs_dir.glob("*_분석데이터_*.md") if p != m.report_md_path]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None

def stale_goal(m, prev_rows: list) -> bool:
    """목표 신선도 배너 — 직전 회차로부터 2주 이상 지났고 판정할 액션이 있었는가.

    효과 감쇠가 2~3주라(DESIGN §8.1 원리 7) 그 전에 갱신을 권한다. 날짜는 추세 CSV에서
    읽는다 — prev.resolve_prev는 액션 목록만 알고 날짜는 모른다(prev_date는 빈 슬롯)."""
    if not prev_rows or not resolve_prev(m)["actions"]:
        return False
    try:
        gap = datetime.date.fromisoformat(m.date) - datetime.date.fromisoformat(
            prev_rows[-1].get("date") or "")
    except ValueError:
        return False
    return gap.days >= 14

def main():
    ap = argparse.ArgumentParser(description="3층 리포트 발행 + 추세 CSV upsert(opt-in)")
    ap.add_argument("manifest")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--growth-dir", default=None,
                    help="추세 CSV 폴더 — 지정할 때만 회차 추세를 기록한다"
                         "(analyze.sh가 LA_GROWTH_DIR을 이 인자로 넘긴다)")
    args = ap.parse_args()
    m = load_manifest(args.manifest)
    out = m.report_md_path
    if not stage_guard(out, args.force):
        return
    # 덮어쓰기 전에 기존 B7의 강사 확정을 건진다 — stage_guard 뒤·write 앞이어야 한다.
    confirmed = confirmed_actions_of(out)
    if not out.exists():
        # 현재 이름의 파일이 없다 = 첫 발행이거나 개명(날짜·강의명 수정) 직후다.
        # 후자면 옛 이름 파일에 확정이 남아 있다 — 이관한다(_prev_named_md 참조).
        prev_md = _prev_named_md(m)
        if prev_md:
            confirmed = confirmed_actions_of(prev_md)
            # 0건이면 알리지 않는다 — 첫 발행 뒤 개명한 흔한 경우까지 안내를 띄우면
            # 정작 이관이 일어난 순간의 안내가 묻힌다.
            if confirmed:
                print(f"옛 이름 산출물에서 B7 확정 {len(confirmed)}건 이관: {prev_md.name}",
                      file=sys.stderr)
    gdir = growth_dir(m, args.growth_dir)
    csv_path = (gdir / "trend_metrics.csv") if gdir else None
    metrics = require_json(m.artifacts_dir / "metrics.json", "s50_metrics.py")
    goal = require_json(m.artifacts_dir / "llm" / "goal_check.json", "s60_goalcheck.py")
    review = require_json(m.artifacts_dir / "llm" / "review.json", "s65_review.py")
    norm_n = normalize_prose(review, goal,
                             (metrics.get("text") or {}).get("speaker_share") or {})
    # 실패·전사 없음 교시는 렌더에서 뺀다(s65와 같은 규칙) — 대신 뺀 사실을 A5·C2에 남긴다.
    sem, un_sem = load_analysis(m, "semantic")
    struct, un_struct = load_analysis(m, "structure")
    unavailable = [t for t, _ in un_sem + un_struct]
    # s65가 본 미가용도 합친다 — B6·B7이 왜 얇은지 설명하는 재료이고, goal_check 실패처럼
    # 교시 산출물에는 없는 사유도 여기에만 있다. 출처를 표시하는 건 두 목록이 어긋날 수
    # 있어서다(s65 실행 뒤 상류를 다시 돌리면 같은 교시가 다른 사유로 두 번 나온다).
    for u in review.get("unavailable") or []:
        if u not in unavailable:
            unavailable.append(f"{u} (리뷰 합성 시점)")
    prev_rows = trend_tail(csv_path, m.project, m.session_id) if csv_path else []
    summary = summarize(metrics, sem, struct, goal, review)
    t = metrics.get("text") or {}
    au = metrics.get("audio") or {}
    # 추세 행을 먼저 쓴다. 리포트 쓰기가 실패하면 다음 실행이 SKIP 없이 다시 돌아 같은 행을
    # 교체하지만(upsert), 순서를 뒤집으면 리포트만 남고 그 회차의 추세가 영영 빠진다
    # (report.md가 존재해 다음 실행이 SKIP된다).
    # 전사가 한 교시도 없으면(text_hours == 0) 텍스트 파생 지표는 '측정했더니 0'이 아니라
    # 미측정이다. 0을 그대로 적으면 다음 회차가 그것을 기준선으로 읽고 ▲를 그린다 —
    # 추세 CSV는 회차가 쌓이면 되돌리기 어려워 거짓 개선으로 굳는다. 빈 칸(None)이면
    # delta_mark가 비교 자체를 붙이지 않는다(없는 비교를 그리지 않는 것이 규약).
    # 오디오 지표는 전사와 무관하게 실측한 값이라 함께 지우지 않는다.
    text_measured = bool(t.get("text_hours"))
    def tx(v):
        return v if text_measured else None
    if csv_path:
        trend_upsert(csv_path, {
            "date": m.date, "project": m.project, "session": m.session_id,
            "bounds_h": metrics.get("bounds_hours"), "apology_n": tx(summary["apology_n"]),
            "selfqa_max": tx(summary["selfqa_max"]),
            "hedging_n": tx(t.get("hedging_n")), "hedging_per_h": tx(t.get("hedging_per_h")),
            "trouble_n": tx(len(summary["troubles"])), "trouble_min": tx(summary["trouble_min"]),
            "speech_rate_spm": au.get("speech_rate_spm", ""),
            "silence_min": au.get("silence_min", ""),
            "filler_min_n": tx(t.get("filler_min_n")), "notes": "",
        })
    evidence = collect_evidence(sem, struct, goal)
    md = render({
        "manifest": m, "metrics": metrics, "sem": sem, "struct": struct,
        "goal": goal, "review": review, "prev_rows": prev_rows, "summary": summary,
        "evidence": evidence, "unavailable": unavailable, "orphans": orphan_periods(m),
        "stale_goal": stale_goal(m, prev_rows),
        "confirmed_actions": confirmed, "norm_n": norm_n,
        "now": datetime.datetime.now().isoformat(timespec="seconds"),
        "model": os.environ.get("LA_MODEL", "(기본)"),
        "whisper_model": au.get("model", "미사용"),
        # 렌더러에는 문자열만 넘긴다 — 빈 값이면 C3가 '미기록'으로 적는다(None이 새면 안 된다).
        "csv_path": str(csv_path) if csv_path else "",
    })
    a_lines = a_layer_rows(md)
    if len(a_lines) > A_LINE_CAP:
        # 상한 위반은 발행 실패가 아니다 — 리포트는 내되 A층이 1화면을 넘었다고 알린다.
        print(f"경고: A층 {len(a_lines)}행 — 1화면 상한({A_LINE_CAP}행) 초과, "
              "요약 항목을 B층으로 밀어낼 것", file=sys.stderr)
    out.write_text(md, encoding="utf-8")
    print(f"{out.name} 발행(A층 {len(a_lines)}행, 증거 {len(evidence)}건) · "
          + (f"trend upsert → {csv_path}" if csv_path
             else "추세 CSV 미기록(LA_GROWTH_DIR 미지정)"))

if __name__ == "__main__":
    run(main)
