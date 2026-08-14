#!/usr/bin/env python3
"""s65: 잘된 것/아쉬운 것/액션 후보 — 검증된 관측·지표 위에서만 합성 (claude -p, 세션당 1회).

s30·s40·s60과 결정적으로 다른 점: **원 전사록을 입력하지 않는다**(DESIGN §6 s65). 앞 단계들이
인용 대조·ts 실재 검증을 통과시킨 관측과 결정론 지표만 싣는다. 전사를 다시 실어 주면 모델은
검증받지 않은 문장을 새로 인용하며 총평을 쓰기 시작하고 — "LLM 제로샷 코칭 제안 82% 중복"이
바로 그 산출물이다 — 이 스테이지의 존재 이유가 사라진다.

그래서 여기서 하는 검증은 인용 대조가 아니라 **참조 실재성**이다: 항목의 evidence가 실제
산출물 id·지표 키를 가리키지 않으면 그 항목을 폐기한다(dropped_items).
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import llm
from lib.manifest import load_manifest
from lib.stage import cli, stage_guard_llm, write_json, run, load_analysis, require_json

PROMPT = pathlib.Path(__file__).resolve().parents[1] / "prompts" / "review.md"
# 항목 배열의 개수 범위와 필수 필드 — 리포트 B6·B7이 이 필드를 그대로 렌더한다.
SPEC = (("good", 3, 5, ("point", "cause")),
        ("bad", 3, 5, ("point", "cause")),
        ("action_candidates", 3, 6, ("action", "when", "criteria", "prep")))

def validate(data):
    """스키마 검증 — 오류 목록을 돌려주면 call_claude가 오류를 붙여 1회 재시도한다.

    call_claude는 validate를 재시도 except 블록 *밖*에서 부른다. 이 함수가 예외를 내면
    재시도 한 번 없이 스테이지가 트레이스백으로 멈추므로, 어떤 타입이 와도 오류 목록만
    돌려준다(모델은 배열 자리에 객체·null을, 항목 자리에 문자열을 실제로 낸다).

    개수·필드는 여기서(=재시도), evidence의 실재성은 뒤의 항목 단위 폐기에서 가른다.
    근거 하나가 가짜라고 세션 전체를 재시도시키면 나머지 멀쩡한 항목까지 날아간다.
    반대로 point·cause·prep이 비는 건 폐기가 아니라 재시도 대상이다 — 그 항목은 리포트에
    '- — 원인: ' 같은 빈 줄로 렌더돼 강사가 읽을 게 없다."""
    if not isinstance(data, dict):
        return [f"최상위는 JSON 객체여야 함: {str(data)[:50]!r}"]
    errs = []
    for key, lo, hi, fields in SPEC:
        arr = data.get(key)
        if not isinstance(arr, list) or not (lo <= len(arr) <= hi):
            n = len(arr) if isinstance(arr, list) else f"{type(arr).__name__}"
            errs.append(f"{key}는 {lo}~{hi}개 배열이어야 함(받은 값: {n})")
            continue
        for i, it in enumerate(arr, 1):
            if not isinstance(it, dict):
                errs.append(f"{key}[{i}]는 {{{', '.join(fields)}}} 객체여야 함: {str(it)[:50]!r}")
                continue
            for f in fields:
                v = it.get(f)
                if not isinstance(v, str) or not v.strip():
                    errs.append(f"{key}[{i}] {f}은(는) 비어 있지 않은 문자열이어야 함: "
                                f"{str(v)[:50]!r}")
            if key == "action_candidates":
                continue
            # 빈 배열은 통과시킨다 — '근거 없는 일반론'은 재시도가 아니라 폐기로 처리한다.
            ev = it.get("evidence")
            if not isinstance(ev, list) or any(not isinstance(r, str) for r in ev):
                errs.append(f"{key}[{i}] evidence는 참조 문자열 배열이어야 함: {str(ev)[:50]!r}")
    return errs

def collect_ids(sem: dict, struct: dict, metrics: dict, goal: dict) -> set:
    """참조 가능한 evidence 전체 — 전 파이프라인 공통 계약의 단일 정의.

    s30·s40이 같은 `q_pNN_NNN` 네임스페이스를 쓰므로 접두(`sem:`/`str:`)가 출처를 가른다
    — 이걸 빼면 s30의 001과 s40의 001이 같은 참조가 되고 s70 앵커가 엉뚱한 인용으로 간다.
    `goal:`은 판정 액션의 앞 20자, `metric:`은 metrics.json의 text·audio 키다.

    저하 모드가 정상 경로다(audio: null, 판정 없음, 실패 교시). 모든 조회는 형을 먼저
    확인한다 — 여기서 TypeError가 나면 판정을 다 받아놓고 저장 직전에 스테이지가 죽는다."""
    known = set()
    def walk(obj, prefix):
        if isinstance(obj, dict):
            if isinstance(obj.get("id"), str):
                known.add(f"{prefix}:{obj['id']}")
            for v in obj.values():
                walk(v, prefix)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, prefix)
    for d in sem.values():
        walk(d, "sem")
    for d in struct.values():
        walk(d, "str")
    for part in (metrics.get("text"), metrics.get("audio")):
        if isinstance(part, dict):
            known.update(f"metric:{k}" for k in part)
    for v in goal.get("verdicts") or []:
        if isinstance(v, dict) and isinstance(v.get("action"), str):
            known.add(f"goal:{v['action'][:20]}")
    return known

def main():
    args = cli("잘된 것/아쉬운 것 합성 (claude -p)")
    m = load_manifest(args.manifest)
    out = m.artifacts_dir / "llm" / "review.json"
    # LLM 스테이지 전용 가드 — 직전 실행의 llm_failed 기록은 SKIP 사유가 아니다.
    if not stage_guard_llm(out, args.force):
        return
    sem, un_sem = load_analysis(m, "semantic")
    struct, un_struct = load_analysis(m, "structure")
    kinds = {k for _, k in un_sem + un_struct}      # goal_check 사유가 섞이기 전에 굳힌다
    unavailable = [t for t, _ in un_sem + un_struct]
    metrics = require_json(m.artifacts_dir / "metrics.json", "s50_metrics.py")
    goal = require_json(m.artifacts_dir / "llm" / "goal_check.json", "s60_goalcheck.py")
    if "llm_failed" in goal:
        # 판정이 없는데 있는 척하지 않는다 — verdicts=[]를 그대로 보내면 모델은 그것을
        # '직전 액션이 하나도 없었다'로 읽고 회차 간 연속성을 지어낸다.
        unavailable.append(f"goal_check: llm_failed({str(goal['llm_failed'])[:60]})")
        goal = {}
    if not sem and not struct:
        # 관측이 한 건도 없는 세션. 지표만 실어 부르면 모델은 숫자 몇 개로 '잘된 것 3개'를
        # 지어낼 수밖에 없다 — 그게 DESIGN이 막으려는 제로샷 총평 그 자체다. 호출 없이 빈
        # 리뷰를 남긴다(s60의 전사 없는 세션 처리와 같은 규약).
        if kinds == {"missing"}:                    # s30·s40을 아예 안 돌린 실행
            raise ValueError("artifacts/llm/pNN_semantic.json·pNN_structure.json 없음 — "
                             "s30_semantic.py·s40_structure.py를 먼저 실행하세요")
        rec = {"good": [], "bad": [], "action_candidates": [], "dropped_items": 0,
               "unavailable": unavailable,
               "note": "관측 산출물 없음(s30·s40) — 합성할 근거가 없어 생략"}
        if kinds - {"skipped"}:
            # 상류가 미완일 뿐이라 다음 실행에 관측이 생길 수 있다. 이 빈 리뷰를 '완료'로
            # 굳히면 s30·s40이 재시도로 성공해도 s65는 영원히 SKIP이고 리포트 B6·B7이 빈
            # 채로 남는다 — 파일을 손으로 지우기 전엔 복구되지 않는다(s30·s40이 같은 함정을
            # 겪었다). llm_failed 표식이 stage_guard_llm에게 '미완'을 알리는 유일한 신호다.
            rec["llm_failed"] = "상류 관측 미완(s30·s40) — 재실행 후 다시 합성 필요"
            # llm_failed는 stage_guard_llm에게 '미완'을 알리는 내부 표식일 뿐, 여기서
            # LLM을 부른 적은 없다. s70이 그 문자열만 보고 "미생성(LLM 실패)"로 렌더하면
            # 강사는 s65만 다시 돌리고 — 아무것도 달라지지 않는다. 고칠 곳은 s30·s40이다.
            # 갈래를 명시 키로 넘겨 리포트가 둘을 구분해 말하게 한다.
            rec["failed_kind"] = "upstream_incomplete"
            write_json(out, rec)
            print("상류 관측 미완 — 리뷰 생략(다음 실행에서 재시도): "
                  + "; ".join(unavailable), file=sys.stderr)
            sys.exit(4)
        # 전 교시 전사 없음(no_clova) — 몇 번을 돌려도 같은 결과인 저하 모드의 정상 종료다.
        write_json(out, rec)
        print(f"전사 없음 — 리뷰 생략 (미가용 {len(unavailable)}건)")
        return
    payload = {"metrics": metrics, "semantic": sem, "structure": struct,
               "goal_check": goal, "unavailable": unavailable}
    try:
        data = llm.call_claude(PROMPT, payload, validate)
    except llm.LLMError as e:
        # 실패도 기록으로 남긴다 — 다음 실행이 stage_guard_llm으로 재시도하고,
        # s70은 빈 배열을 읽어 리포트를 렌더할 수 있다(B6·B7만 빈 채로).
        write_json(out, {"llm_failed": str(e), "good": [], "bad": [],
                         "action_candidates": [], "dropped_items": 0,
                         "unavailable": unavailable})
        print(f"LLM 실패: {e}", file=sys.stderr)
        sys.exit(4)
    known = collect_ids(sem, struct, metrics, goal)
    dropped = 0
    for key in ("good", "bad"):
        kept = []
        for item in data[key]:
            # evidence가 비면 근거 없는 일반론이고, 없는 id를 가리키면 지어낸 근거다.
            # 둘 다 '근거 있는 관측'이라는 이 리포트의 유일한 약속을 깨므로 항목째 폐기한다.
            if item.get("evidence") and all(ref in known for ref in item["evidence"]):
                kept.append(item)
            else:
                dropped += 1
        data[key] = kept
    data["dropped_items"] = dropped
    data["unavailable"] = unavailable        # 리뷰가 왜 얇은지 리포트가 설명할 수 있게
    # 모델이 최상위에 흘린 파이프라인 예약 키를 지운다(s30·s40·s60 공통 규약).
    # 남겨두면 합성에 성공한 리뷰가 실패로 재분류돼 B6·B7이 '미생성'으로 렌더되고
    # 매 실행 재호출된다.
    data.pop("llm_failed", None)
    data.pop("skipped", None)
    write_json(out, data)
    print(f"잘된 것 {len(data['good'])} 아쉬운 것 {len(data['bad'])} "
          f"후보 {len(data['action_candidates'])} (폐기 {dropped}, 미가용 {len(unavailable)})")

if __name__ == "__main__":
    run(main)
