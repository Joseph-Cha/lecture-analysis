#!/usr/bin/env python3
"""s40: 질문·uptake·타임라인·화법 관찰·재사용 멘트·커버리지 (claude -p, 교시당 1회).

s30이 확립한 순서 — 입력 조립 → call_claude → 인용·ts 검증 → 증거 id 부여 → 저장 — 을 그대로
따른다. 다른 점은 배열이 여섯 개라 검증 규칙을 표로 돌린다는 것과, 인용이 아닌 두 배열
(timeline·coverage)에 예외 규칙이 붙는다는 것뿐이다.

교시 병렬(sub-agent) 구조도 s30과 같다: prepare(메인 스레드 — 가드·입력 조립) → analyze(워커 —
call_claude 이후) → 메인이 교시 순서대로 출력. LA_PARALLEL이 동시 호출 수다."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import llm
from lib.clova import fmt_ts, mask_text, parse_ts
from lib.manifest import load_manifest
from lib.stage import (cli, stage_guard_llm, write_json, read_json, run,
                       parallel_workers, map_jobs)

PROMPT = pathlib.Path(__file__).resolve().parents[1] / "prompts" / "structure.md"
REQUIRED = ["period", "questions", "uptake", "timeline", "speech_observations",
            "reusable", "demo_events", "coverage"]
KINDS = {"지목형", "거수", "열린", "이해확인"}
# (배열, 인용 키) — 인용·ts 실재 검증 대상. 이 순서가 곧 증거 id 순서다.
# uptake는 instructor_quote가 기준이다: student_quote는 단일 마이크라 애초에 자주 누락돼
# 원문 대조가 성립하지 않는다(있으면 그대로 보존만 한다).
QUOTE_KEYS = [("questions", "quote"), ("uptake", "instructor_quote"),
              ("speech_observations", "quote"), ("reusable", "quote"), ("demo_events", "quote")]

def validate(data):
    """스키마 검증 — 실패 목록을 돌려주면 call_claude가 오류를 붙여 1회 재시도한다.

    call_claude는 validate를 재시도 except 블록 *밖*에서 부른다. 그래서 이 함수가 예외를
    내면 재시도 한 번 없이 스테이지가 트레이스백으로 멈춘다 — 어떤 타입이 와도 오류 목록만
    돌려주도록 모든 접근 전에 형을 확인한다(LLM은 배열 자리에 객체·null을, 항목 자리에
    문자열을 실제로 낸다).

    여기서 보는 건 스키마와 열거값뿐이다. 근거의 실재성(인용·ts)은 뒤의 clean 단계에서
    항목 단위로 폐기한다 — 항목 하나 때문에 교시 전체를 실패시키지 않기 위해서다."""
    if not isinstance(data, dict):
        return ["최상위는 JSON 객체여야 함"]
    errs = [f"필수 키 없음: {k}" for k in REQUIRED if k not in data]
    if errs:
        return errs
    errs = [f"{k}는 배열이어야 함(없으면 [])" for k in REQUIRED[1:] if not isinstance(data[k], list)]
    if errs:
        return errs
    for i, q in enumerate(data["questions"], 1):
        if not isinstance(q, dict):
            errs.append(f"questions[{i}]는 {{quote, ts, kind, answered}} 객체여야 함: {str(q)[:50]!r}")
            continue
        if q.get("kind") not in KINDS:
            errs.append(f"questions[{i}] kind 오류: {str(q.get('kind'))[:30]!r} "
                        f"(허용: {'/'.join(sorted(KINDS))})")
        # 리포트 B3이 '무응답 관측'을 answered로 센다. "false"·"아니오" 같은 문자열은
        # 파이썬에서 전부 참이라 무응답이 조용히 응답으로 뒤집힌다 — 세는 값은 형을 잠근다.
        if not isinstance(q.get("answered"), bool):
            errs.append(f"questions[{i}] answered는 true/false 불리언이어야 함: "
                        f"{str(q.get('answered'))[:30]!r}")
    return errs

def _ts_parses(item, keys) -> bool:
    if not isinstance(item, dict):
        return False
    for k in keys:
        try:
            parse_ts(str(item.get(k)))
        except ValueError:
            return False
    return True

def clean_timeline(items):
    """타임라인 — ts 실재 검증에서 빼고 형식만 본다.

    start_ts·end_ts는 인용 지점이 아니라 구간 경계다(예: 마지막 블록 뒤 강의 종료 시각).
    실재 블록 ts를 요구하면 멀쩡한 구간이 통째로 폐기된다. 대신 파싱 가능한 시각인지는
    확인한다 — 하류 리포트가 이 값으로 구간 길이를 계산하고, "처음"·null이 섞이면 거기서
    깨진다. 인용이 없으니 증거 id도 붙이지 않는다(B층 앵커의 목적지가 아니다)."""
    kept = [it for it in items if _ts_parses(it, ("start_ts", "end_ts"))]
    return kept, len(items) - len(kept)

def clean_coverage(items, source, valid_ts):
    """계획 커버리지 — 근거 인용을 단 항목만 다른 배열과 같은 검증을 받는다.

    '스킵' 판정은 근거가 될 발화가 아예 없는 게 정상이라(그래서 스킵이다) 빈 인용을 면제한다.
    대신 그 항목의 ts는 지운다 — 검증하지 못한 시각을 남기면 하류가 그걸로 앵커를 만든다."""
    kept, dropped = [], 0
    for it in items:
        if not isinstance(it, dict):        # assign_ids가 문자열 항목에 id를 넣다 터지는 것도 막는다
            dropped += 1
        elif it.get("quote"):
            k, d = llm.clean([it], source, valid_ts)
            kept += k
            dropped += d
        else:
            it["ts"] = ""
            kept.append(it)
    return kept, dropped

def prepare(m, per, force, plan_text):
    """가드 + 입력 조립(메인 스레드) — 호출할 교시면 job, 아니면 None(SKIP·no_clova)."""
    out = m.artifacts_dir / "llm" / f"{per.id}_structure.json"
    # LLM 스테이지 전용 가드 — 직전 실행의 llm_failed 기록은 SKIP 사유가 아니다.
    if not stage_guard_llm(out, force):
        return None
    src = m.artifacts_dir / "parsed" / f"{per.id}.json"
    # 스테이지 순서가 어긋난 것뿐이므로 트레이스백이 아니라 고칠 방법을 알려준다.
    if not src.is_file():
        raise ValueError(f"{per.id}: artifacts/parsed/{per.id}.json 없음 — s20_parse.py를 먼저 실행하세요")
    parsed = read_json(src)
    blocks = parsed.get("masked_blocks")
    if not blocks:
        write_json(out, {"period": per.id, "skipped": "no_clova"})
        print(f"{per.id}: 전사 없음(no_clova) → 구조 분석 건너뜀")
        return None
    payload = {"period": per.id,
               "instructor_label": parsed["instructor_label"],
               # s30과 같은 규칙 — ts를 초 정수로 보내면 모델이 인용 ts도 정수로 되돌려 준다.
               # 출력 스키마와 하류(s70의 parse_ts(str(ts)))는 "MM:SS" 표기를 쓴다.
               "blocks": [{"speaker": b["speaker"], "ts": fmt_ts(b["ts"]), "text": b["text"]}
                          for b in blocks]}
    if plan_text:
        payload["plan_text"] = plan_text        # 없으면 키 자체를 빼 커버리지를 요구하지 않는다
    return {"pid": per.id, "out": out, "payload": payload, "has_plan": bool(plan_text),
            "source": "\n".join(b["text"] for b in blocks),
            "valid_ts": {b["ts"] for b in blocks}}          # 초 — 인용 ts 실재 검증용

def analyze(job):
    """워커: call_claude → 인용·ts 검증 → 증거 id → 저장. (성공 여부, 로그 한 줄)을 돌려준다.

    print는 여기서 하지 않는다 — 워커가 찍으면 교시 줄이 섞인다(map_jobs 규약: 메인이 찍는다)."""
    pid, out, source, valid_ts = job["pid"], job["out"], job["source"], job["valid_ts"]
    try:
        data = llm.call_claude(PROMPT, job["payload"], validate)
    except llm.LLMError as e:
        write_json(out, {"period": pid, "llm_failed": str(e)})
        return False, f"[{pid}] LLM 실패: {e}"
    dropped, seq = 0, 1
    for key, quote_key in QUOTE_KEYS:
        kept, d = llm.clean(data[key], source, valid_ts, quote_key=quote_key)
        data[key] = llm.assign_ids(kept, pid, seq)
        seq += len(kept)
        dropped += d
    data["timeline"], d = clean_timeline(data["timeline"])
    dropped += d
    if job["has_plan"]:
        cov, d = clean_coverage(data["coverage"], source, valid_ts)
        data["coverage"] = llm.assign_ids(cov, pid, seq)
    else:
        # 계획 파일이 없으면 커버리지는 계약상 빈 배열이다(브리프·프롬프트 과업 7).
        # 모델이 그래도 판정을 내면 무엇을 '계획'으로 삼았는지 알 수 없는 항목이므로
        # — 인용이 진짜여도 기준이 지어낸 목차다 — 통째로 버린다(폐기 수에 계상).
        d = len(data["coverage"])
        data["coverage"] = []
    dropped += d
    # 모델이 다른 교시 id를 적어도 파일명과 어긋나지 않게 고정한다.
    data["period"] = pid
    # 인용 위반뿐 아니라 형식 위반으로 버린 timeline·coverage 항목까지 센다
    # (DESIGN §7 "위반 항목 폐기 + 비고 카운트" — 리포트 A5의 측정 신뢰도 한 줄).
    data["dropped_quotes"] = dropped
    # 모델이 최상위에 흘린 파이프라인 예약 키를 지운다(s30·s60·s65 공통 규약).
    # 남겨두면 성공한 교시가 실패로 재분류돼 관측이 버려지고 매 실행 재호출된다.
    data.pop("llm_failed", None)
    data.pop("skipped", None)
    write_json(out, data)
    return True, (f"{pid}: 질문 {len(data['questions'])} uptake {len(data['uptake'])} "
                  f"타임라인 {len(data['timeline'])} 커버리지 {len(data['coverage'])} (폐기 {dropped})")

def main():
    args = cli("구조 분석 (claude -p)")
    m = load_manifest(args.manifest)
    workers = parallel_workers()        # 호출 전에 읽는다 — 오타면 LLM을 한 번도 부르지 않고 exit 2
    # 계획 파일은 전사와 나란한 **두 번째 전송 경로**다(DESIGN §10.1) — 같은 마스킹을 건다.
    # 계획 문서에는 담당자·수강생 실명이 전사보다 더 자주 적혀 있어, 여기를 빼면 "외부로
    # 나가는 것은 마스킹된 텍스트뿐"이라는 불변식이 조용히 깨진다(전사만 보면 멀쩡하다).
    plan_text = mask_text(m.plan.read_text(encoding="utf-8"), m.mask_names) if m.plan else None
    # 가드·입력 조립을 전 교시에 대해 먼저 끝낸다 — 상류 산출물이 하나라도 없으면 호출 0회로 멈춘다.
    jobs = [j for j in (prepare(m, per, args.force, plan_text) for per in m.periods) if j]
    failed = False
    for ok, msg in map_jobs(analyze, jobs, workers):
        print(msg, file=sys.stdout if ok else sys.stderr)
        failed |= not ok
    sys.exit(4 if failed else 0)

if __name__ == "__main__":
    run(main)
