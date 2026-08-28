#!/usr/bin/env python3
"""s30: 측정 구간 제안 + 사과·자문자답·트러블 (claude -p, 교시당 1회).

여기서 확립하는 순서 — 입력 조립 → call_claude → 인용·ts 검증 → 증거 id 부여 → 저장 —
를 s40·s60·s65가 그대로 반복한다.

교시는 서로 독립이라(공유 상태 없음, 산출물은 교시별 파일) 호출을 **병렬 sub-agent**로 돌린다:
가드·입력 조립은 메인 스레드에서 전부 먼저 끝내고(SKIP/RETRY 출력, 입력 오류는 호출 전에
exit 2), call_claude 이후만 워커에서 돈다(LA_PARALLEL, stage.map_jobs). s40도 같은 구조다."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import llm
from lib.clova import fmt_ts
from lib.manifest import check_span, load_manifest
from lib.stage import (cli, stage_guard_llm, write_json, read_json, run,
                       HardStop, parallel_workers, map_jobs)

PROMPT = pathlib.Path(__file__).resolve().parents[1] / "prompts" / "semantic.md"
REQUIRED = ["period", "bounds_proposal", "instructor_check", "apologies", "self_qa", "troubles"]
_sec = llm.is_sec          # 초 단위 정수 술어 — s40과 공유하려 lib으로 옮겼다

def validate(data):
    """스키마 검증 — 실패 목록을 돌려주면 call_claude가 오류를 붙여 1회 재시도한다.

    call_claude는 validate를 재시도 except 블록 *밖*에서 부른다. 그래서 이 함수가 예외를
    내면 재시도 한 번 없이 스테이지가 트레이스백으로 멈춘다 — 어떤 타입이 와도 오류 목록만
    돌려주도록 모든 접근 전에 형을 확인한다(LLM은 배열 자리에 객체·null을 실제로 낸다).

    타입 검사가 곧 하류 계약이다: 여기서 통과한 배열은 drop_bad_quotes가, start/end/breaks는
    s50의 _llm_bounds가 무검증에 가깝게 소비한다. s50에서 걸리면 ValueError → exit 2로 지표
    스테이지가 통째로 멈추지만, 여기서 걸면 재시도로 살릴 기회가 있다."""
    if not isinstance(data, dict):
        return ["최상위는 JSON 객체여야 함"]
    errs = [f"필수 키 없음: {k}" for k in REQUIRED if k not in data]
    if errs:
        return errs
    if not isinstance(data["instructor_check"], dict):
        errs.append("instructor_check는 {agrees, note} 객체여야 함")
    ap = data["apologies"]
    if not isinstance(ap, dict) or not isinstance(ap.get("counted"), list) \
            or not isinstance(ap.get("excluded"), list):
        errs.append("apologies.counted/excluded는 배열이어야 함")
    for key in ("self_qa", "troubles"):
        if not isinstance(data[key], list):
            errs.append(f"{key}는 배열이어야 함(없으면 [])")
    bp = data["bounds_proposal"]
    if not isinstance(bp, dict):
        return errs + ["bounds_proposal은 객체여야 함"]
    if not isinstance(bp.get("quotes"), list):
        errs.append("bounds_proposal.quotes는 배열이어야 함(없으면 [])")
    if not (_sec(bp.get("start")) and _sec(bp.get("end"))):
        errs.append('bounds_proposal.start/end는 초 단위 정수여야 함("05:13"이 아니라 313)')
    breaks = bp.get("breaks")
    if not isinstance(breaks, list):
        errs.append("bounds_proposal.breaks는 배열이어야 함(휴식이 없으면 [])")
    else:
        for i, pair in enumerate(breaks, 1):
            if not (isinstance(pair, list) and len(pair) == 2 and all(_sec(x) for x in pair)):
                errs.append(f"bounds_proposal.breaks[{i}]는 [시작초, 끝초] 정수 2원소여야 함 "
                            f'("30:00"이 아니라 1800) — 받은 값: {pair!r}')
    if not errs:
        # 형이 맞은 뒤에야 산술 불변식(manifest 수동 구간과 같은 규칙)을 본다.
        # 여기서 통과시키면 s50이 같은 검사에서 ValueError → exit 2로 죽어 지표가 통째로
        # 안 나온다. 재시도로 살릴 수 있는 자리는 여기뿐이다.
        try:
            check_span("bounds_proposal", bp["start"], bp["end"], breaks)
        except ValueError as e:
            errs.append(str(e))
    return errs

def prepare(m, per, force):
    """가드 + 입력 조립(메인 스레드) — 호출할 교시면 job, 아니면 None(SKIP·no_clova)."""
    out = m.artifacts_dir / "llm" / f"{per.id}_semantic.json"
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
        print(f"{per.id}: 전사 없음(no_clova) → 의미 판별 건너뜀")
        return None
    payload = {"period": per.id,
               "instructor_label": parsed["instructor_label"],
               # ts를 초 정수 그대로 보내면 모델이 인용 ts도 정수로 되돌려 준다 —
               # 출력 스키마와 하류(s70의 parse_ts(str(ts)))는 "MM:SS" 표기를 쓴다.
               # 보내는 쪽에서 표기를 맞춰야 "블록 ts 값 그대로"라는 프롬프트 규칙이 성립한다.
               "blocks": [{"speaker": b["speaker"], "ts": fmt_ts(b["ts"]), "text": b["text"]}
                          for b in blocks]}
    return {"pid": per.id, "out": out, "payload": payload,
            "source": "\n".join(b["text"] for b in blocks),
            "valid_ts": {b["ts"] for b in blocks}}          # 초 — 인용 ts 실재 검증용

def analyze(job):
    """워커: call_claude → 인용·ts 검증 → 증거 id → 저장. (성공 여부, 로그 한 줄)을 돌려준다.

    print는 여기서 하지 않는다 — 워커가 찍으면 교시 줄이 섞인다(map_jobs 규약: 메인이 찍는다).
    하드 중단은 HardStop으로 올린다(워커 안의 sys.exit은 그 스레드만 끝낸다)."""
    pid, out = job["pid"], job["out"]
    try:
        data = llm.call_claude(PROMPT, job["payload"], validate)
    except llm.LLMError as e:
        write_json(out, {"period": pid, "llm_failed": str(e)})
        return False, f"[{pid}] LLM 실패: {e}"
    if not data["instructor_check"].get("agrees", True):
        # 반쪽 산출물 금지 — 파일을 쓰지 않고 올린다(다른 교시의 미시작 호출은 취소된다).
        raise HardStop(f"[{pid}] 강사 라벨 불일치(LLM): {data['instructor_check'].get('note')}\n"
                       "manifest [speakers].instructor_label을 확인하세요.")
    # (컨테이너, 키, 인용 키, 시각 키들) — 인용·ts 검증과 증거 id 부여를 한 규칙으로 돈다.
    # 순서가 곧 id 순서다(bounds → 사과 → 자문자답 → 트러블).
    targets = [(data["bounds_proposal"], "quotes", "quote", ("ts",)),
               (data["apologies"], "counted", "quote", ("ts",)),
               (data["apologies"], "excluded", "quote", ("ts",)),
               (data, "self_qa", "question_quote", ("ts",)),
               (data, "troubles", "quote", ("start_ts", "end_ts"))]
    dropped, seq = 0, 1
    for obj, key, quote_key, ts_keys in targets:
        kept, d = llm.clean(obj[key], job["source"], job["valid_ts"], quote_key, ts_keys)
        obj[key] = llm.assign_ids(kept, pid, seq)
        seq += len(kept)
        dropped += d
    # 모델이 다른 교시 id를 적어도 파일명과 어긋나지 않게 고정한다.
    data["period"] = pid
    data["dropped_quotes"] = dropped
    # 모델이 최상위에 흘린 파이프라인 예약 키를 지운다(s40·s60·s65 공통 규약).
    # 이 둘은 '미완'의 표식이라(stage_guard_llm의 재시도·load_analysis의 관측 제외),
    # 남겨두면 성공한 교시가 실패로 재분류돼 관측이 통째로 버려지고 매 실행 재호출된다.
    data.pop("llm_failed", None)
    data.pop("skipped", None)
    write_json(out, data)
    return True, (f"{pid}: 사과 {len(data['apologies']['counted'])} 자문자답≤{len(data['self_qa'])} "
                  f"트러블 {len(data['troubles'])} (폐기 {dropped})")

def main():
    args = cli("의미 판별 지표 (claude -p)")
    m = load_manifest(args.manifest)
    workers = parallel_workers()        # 호출 전에 읽는다 — 오타면 LLM을 한 번도 부르지 않고 exit 2
    # 가드·입력 조립을 전 교시에 대해 먼저 끝낸다 — 상류 산출물이 하나라도 없으면 호출 0회로 멈춘다.
    jobs = [j for j in (prepare(m, per, args.force) for per in m.periods) if j]
    failed = False
    for ok, msg in map_jobs(analyze, jobs, workers):
        print(msg, file=sys.stdout if ok else sys.stderr)
        failed |= not ok
    sys.exit(4 if failed else 0)

if __name__ == "__main__":
    run(main)
