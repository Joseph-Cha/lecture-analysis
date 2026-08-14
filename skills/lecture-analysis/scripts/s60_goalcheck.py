#!/usr/bin/env python3
"""s60: 직전 액션 판정 (claude -p, 세션당 1회).

s30이 확립한 순서 — 입력 조립 → call_claude → 인용·ts 검증 → 증거 id 부여 → 저장 — 을 그대로
따른다. 다른 점은 단위가 교시가 아니라 세션이라는 것이다: 액션 하나의 근거는 어느 교시에서든
나올 수 있어 전 교시 블록을 한 번에 싣고, 근거 검증도 인용이 스스로 밝힌 교시를 기준으로 한다.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import llm
from lib.clova import fmt_ts
from lib.manifest import load_manifest
from lib.prev import resolve_prev
from lib.stage import cli, stage_guard_llm, write_json, read_json, run

PROMPT = pathlib.Path(__file__).resolve().parents[1] / "prompts" / "goalcheck.md"
VERDICTS = {"실행", "부분", "미실행", "판정불가"}

def validate(data):
    """스키마 검증 — 실패 목록을 돌려주면 call_claude가 오류를 붙여 1회 재시도한다.

    call_claude는 validate를 재시도 except 블록 *밖*에서 부른다. 그래서 이 함수가 예외를
    내면 재시도 한 번 없이 스테이지가 트레이스백으로 멈춘다 — 어떤 타입이 와도 오류 목록만
    돌려주도록 모든 접근 전에 형을 확인한다(LLM은 배열 자리에 객체·null을, 항목 자리에
    문자열을 실제로 낸다).

    근거의 실재성(인용·ts·교시)은 여기가 아니라 뒤의 clean_quotes에서 항목 단위로 폐기한다
    — 근거 하나 때문에 세션 전체 판정을 실패시키지 않기 위해서다."""
    if not isinstance(data, dict):
        return ["최상위는 JSON 객체여야 함"]
    vs = data.get("verdicts")
    if not isinstance(vs, list):
        return ["verdicts는 배열이어야 함(판정이 없으면 [])"]
    errs = []
    for i, v in enumerate(vs, 1):
        if not isinstance(v, dict):
            errs.append(f"verdicts[{i}]는 {{action, verdict, reason, quotes}} 객체여야 함: "
                        f"{str(v)[:50]!r}")
            continue
        # action은 리포트 A1의 행 제목이자 회차 간 액션 추적의 키다 — 비면 빈 줄이 렌더된다.
        act = v.get("action")
        if not isinstance(act, str) or not act.strip():
            errs.append(f"verdicts[{i}] action은 입력 액션 text 그대로의 문자열이어야 함: "
                        f"{str(act)[:50]!r}")
        if v.get("verdict") not in VERDICTS:
            errs.append(f"verdicts[{i}] verdict 값 오류: {str(v.get('verdict'))[:30]!r} "
                        f"(허용: {'/'.join(sorted(VERDICTS))})")
        # '판정불가'의 본체는 왜 확인 불가인지다(DESIGN §6 s60 — 추측 금지, 사유 명시).
        # 사유 없는 판정불가는 정보가 0이라 강사가 다음 회차에 무엇을 바꿔야 할지 알 수 없다.
        reason = v.get("reason")
        if v.get("verdict") == "판정불가" and not (isinstance(reason, str) and reason.strip()):
            errs.append(f"verdicts[{i}] 판정불가는 reason(왜 녹취로 확인 불가인지) 필수")
        if not isinstance(v.get("quotes", []), list):
            errs.append(f"verdicts[{i}] quotes는 배열이어야 함(근거가 없으면 [])")
    return errs

def clean_quotes(quotes, sources: dict, valid_ts: dict):
    """근거 인용 — 교시 실재 → 인용 원문 대조 → ts 실재 순으로 거른 (남은 항목, 폐기 수).

    교시를 먼저 보는 이유는 나머지 두 검증의 기준 자체가 교시별이기 때문이다(대조할 원문도,
    실재 ts 집합도 교시마다 다르다). 없는 교시를 적은 항목은 대조할 대상이 없으니 폐기한다
    — 리포트는 이 (period, ts)로 C1 인용 전문 앵커를 만든다."""
    kept, dropped = [], 0
    for q in quotes:
        pid = q.get("period") if isinstance(q, dict) else None
        # 문자열인지 먼저 본다 — 모델이 period에 배열·null을 넣으면 dict 조회가 TypeError로
        # 터지고(해시 불가), 그 예외는 판정을 다 받아놓은 뒤 저장 직전에 스테이지를 죽인다.
        if not isinstance(pid, str) or pid not in sources:
            dropped += 1
            continue
        k, d = llm.clean([q], sources[pid], valid_ts[pid])
        kept += k
        dropped += d
    return kept, dropped

def main():
    args = cli("직전 액션 판정 (claude -p)")
    m = load_manifest(args.manifest)
    out = m.artifacts_dir / "llm" / "goal_check.json"
    # LLM 스테이지 전용 가드 — 직전 실행의 llm_failed 기록은 SKIP 사유가 아니다.
    if not stage_guard_llm(out, args.force):
        return
    prev = resolve_prev(m)
    if not prev["actions"]:
        # 직전 산출물이 없거나(첫 분석) 액션 절이 비어 판정할 것이 없는 경우.
        # 여기서 LLM을 부르면 판정 대상 없이 녹취만 던지는 꼴이라 모델이 액션을 지어낸다.
        write_json(out, {"source": prev["source"], "verdicts": [],
                         "first_analysis": prev["source"] == "none", "dropped_quotes": 0})
        print("첫 분석 — 직전 액션 없음" if prev["source"] == "none"
              else f"직전 산출물({prev['source']})에 액션 없음 — 판정 생략")
        return
    periods, sources, valid_ts = [], {}, {}
    for per in m.periods:
        src = m.artifacts_dir / "parsed" / f"{per.id}.json"
        # 스테이지 순서가 어긋난 것뿐이므로 트레이스백이 아니라 고칠 방법을 알려준다.
        if not src.is_file():
            raise ValueError(f"{per.id}: artifacts/parsed/{per.id}.json 없음 — s20_parse.py를 먼저 실행하세요")
        parsed = read_json(src)
        blocks = parsed.get("masked_blocks")
        if not blocks:                      # 전사 없는 교시(no_clova) — 실을 근거가 없다
            continue
        # ts를 초 정수로 보내면 모델이 인용 ts도 정수로 되돌려 준다 — 출력 스키마와
        # 하류(s70의 parse_ts(str(ts)))는 "MM:SS" 표기를 쓴다(s30·s40과 같은 규칙).
        periods.append({"period": per.id, "instructor_label": parsed["instructor_label"],
                        "blocks": [{"speaker": b["speaker"], "ts": fmt_ts(b["ts"]),
                                    "text": b["text"]} for b in blocks]})
        sources[per.id] = "\n".join(b["text"] for b in blocks)
        valid_ts[per.id] = {b["ts"] for b in blocks}    # 초 — 인용 ts 실재 검증용
    if not periods:
        # 전사가 한 교시도 없는 세션(오디오만 있는 저하 모드). 빈 blocks를 실어 보내면 모델은
        # 근거 없이 실행/미실행을 지어낼 수 있고 — 가짜 인용은 폐기돼도 판정은 남는다 —
        # 그게 리포트 A1의 목표 점검 줄이 된다. '녹취로 확인 불가'는 결정론으로 아는
        # 사실이므로 프롬프트 규칙 2를 그대로 여기서 적용한다(호출 자체가 불필요).
        write_json(out, {"source": prev["source"],
                         "verdicts": [{"action": a["text"], "verdict": "판정불가",
                                       "reason": "전사(클로바) 없는 세션 — 녹취 근거로 판정할 수 없음",
                                       "quotes": []} for a in prev["actions"]],
                         "first_analysis": False, "dropped_quotes": 0})
        print(f"전사 없음 — 액션 {len(prev['actions'])}건 전부 판정불가")
        return
    payload = {"actions": [{"text": a["text"], "criteria": a["criteria"]}
                           for a in prev["actions"]],
               "periods": periods}
    try:
        data = llm.call_claude(PROMPT, payload, validate)
    except llm.LLMError as e:
        # 실패도 기록으로 남긴다 — 다음 실행이 stage_guard_llm으로 재시도하고,
        # s70은 verdicts=[]를 읽어 리포트를 렌더할 수 있다(A1만 빈 채로).
        write_json(out, {"source": prev["source"], "llm_failed": str(e), "verdicts": [],
                         "first_analysis": False, "dropped_quotes": 0})
        print(f"LLM 실패: {e}", file=sys.stderr)
        sys.exit(4)
    dropped, seq = 0, 1
    for v in data["verdicts"]:
        kept, d = clean_quotes(v.get("quotes") or [], sources, valid_ts)
        for q in kept:
            # 증거 id의 교시 prefix는 인용 자신의 교시다. 판정 첫 인용의 교시로 뭉뚱그리면
            # 다른 교시 인용이 엉뚱한 교시 앵커를 갖는다(seq는 파일 전체 연속 — id 유일성).
            llm.assign_ids([q], q["period"], seq)
            seq += 1
        v["quotes"] = kept
        dropped += d
    data.update({"source": prev["source"], "first_analysis": False, "dropped_quotes": dropped})
    # 모델이 최상위에 흘린 파이프라인 예약 키를 지운다(s30·s40·s65 공통 규약).
    # 남겨두면 판정이 다 나온 산출물이 실패로 재분류돼 A1이 '미생성'으로 렌더되고
    # 매 실행 재호출된다.
    data.pop("llm_failed", None)
    data.pop("skipped", None)
    write_json(out, data)
    # 판정 수와 액션 수를 함께 찍는다 — 모델이 액션을 빠뜨리면 여기서만 드러난다.
    print(f"판정 {len(data['verdicts'])}건/액션 {len(prev['actions'])}건 "
          f"({prev['source']}, 근거 폐기 {dropped})")

if __name__ == "__main__":
    run(main)
