"""claude -p 래퍼 — DESIGN §7 호출 계약의 단일 구현.
모든 LLM 산출은 여기서 봉투 해석·스키마 검증·인용 검증을 통과해야 한다."""
import json, os, re, subprocess
from pathlib import Path
from .clova import fmt_ts, parse_ts

class LLMError(Exception):
    pass

_FENCE = re.compile(r"^```[a-zA-Z]*\n|\n```$")
# 문자 클래스의 둘째 원소는 리터럴 NBSP(U+00A0)다 — 눈에 안 보이니 지우지 말 것.
# 파이썬 \s가 유니코드 모드에서 NBSP·U+3000을 이미 포함해 실제로는 중복이지만,
# 클로바·LLM 출력의 비파괴 공백을 인용 대조에서 지운다는 의도를 소스에 남겨 둔다.
_WS = re.compile(r"[\s ]+")

def norm(s: str) -> str:
    return _WS.sub("", s)

def _run_once(prompt_text: str, payload: dict) -> dict:
    cmd = [os.environ.get("LA_CLAUDE_BIN", "claude"), "-p", prompt_text,
           "--output-format", "json", "--disallowedTools", "*"]
    model = os.environ.get("LA_MODEL")
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(cmd, input=json.dumps(payload, ensure_ascii=False),
                              capture_output=True, text=True, timeout=600)
    except OSError as e:
        # claude 미설치·LA_CLAUDE_BIN 오타·실행 권한 없음(스텁 chmod 누락)이 여기로 온다.
        # 그냥 새면 OSError라서 스테이지 래퍼(stage.run)가 '입력 오류 exit 2'로 오해하고,
        # LLM 실패 산출물(llm_failed)도 남지 않아 어느 단계가 왜 멈췄는지 사라진다.
        raise LLMError(f"claude 실행 실패({cmd[0]}): {e} — 설치 여부·LA_CLAUDE_BIN을 확인하세요")
    if proc.returncode != 0:
        raise LLMError(f"claude exit {proc.returncode}: {proc.stderr[:400]}")
    envelope = json.loads(proc.stdout)
    if envelope.get("is_error"):
        raise LLMError(f"봉투 is_error: {str(envelope)[:400]}")
    body = _FENCE.sub("", (envelope.get("result") or "").strip())
    return json.loads(body)

def call_claude(prompt_file: Path, payload: dict, validate, retries: int = 1) -> dict:
    prompt_text = Path(prompt_file).read_text(encoding="utf-8")
    last_errs = []
    for attempt in range(retries + 1):
        pt = prompt_text if not last_errs else (
            prompt_text + "\n\n## 직전 응답 오류 — 반드시 수정\n- " + "\n- ".join(last_errs))
        try:
            data = _run_once(pt, payload)
        # TimeoutExpired(=_run_once의 timeout=600)도 재시도 대상이다. 이걸 빼면
        # 600초 먹통 1회가 LLMError가 아닌 생짜 예외로 새어 스테이지 종료 코드 규약을 깬다.
        except (json.JSONDecodeError, LLMError, subprocess.TimeoutExpired) as e:
            # 400자 절단 필수 — TimeoutExpired의 str()에는 cmd가 통째로 들어가고
            # cmd에는 프롬프트 전문이 있다. 자르지 않으면 다음 시도 프롬프트에
            # 직전 프롬프트가 통째로 되먹여져 토큰이 폭주한다.
            last_errs = [f"JSON 파싱/호출 실패: {str(e)[:400]}"]
            continue
        errs = validate(data)
        if not errs:
            return data
        last_errs = errs
    raise LLMError("재시도 후에도 검증 실패: " + "; ".join(last_errs))

def _quote_hit(it, quote_key: str, src_norm: str) -> bool:
    """인용 1건이 원문에 있는지 — check_quotes와 drop_bad_quotes가 공유하는 단일 술어.
    두 함수가 술어를 따로 쓰면 '오류 없음인데 항목이 버려진다'가 언제든 생긴다.

    isinstance 검사가 핵심이다. LLM은 quote에 null·숫자를 넣어 오기도 하고, 항목 객체
    자리에 문자열을 그대로 넣기도 한다([{"quote": "..."}] 대신 ["..."]). 그때 it.get이
    AttributeError로, norm(None)이 TypeError로 죽으면 예외가 call_claude의 validate 호출부
    (재시도 except 밖)로 새어 재시도 한 번 못 해보고 스테이지가 멈춘다.
    비정상 인용은 크래시가 아니라 '재시도 가능한 검증 오류'다.
    (문자열 항목을 통과시키는 것도 답이 아니다 — assign_ids가 it["id"]를 넣다가 터지고,
     ts·reason 같은 형제 필드가 통째로 없어 하류 리포트가 빈칸을 렌더한다.)"""
    if not isinstance(it, dict):
        return False
    q = it.get(quote_key)
    return isinstance(q, str) and bool(q) and norm(q) in src_norm

def check_quotes(items, source_text: str, quote_key: str = "quote"):
    src = norm(source_text)
    errs = []
    for i, it in enumerate(items):
        if _quote_hit(it, quote_key, src):
            continue
        if not isinstance(it, dict):
            errs.append(f"items[{i}]가 객체가 아님(인용은 {{{quote_key}, ts}} 객체여야 함): "
                        f"{str(it)[:50]!r}")
            continue
        q = it.get(quote_key)
        why = "원문에 없음" if isinstance(q, str) and q else "비었거나 문자열이 아님"
        errs.append(f"items[{i}] 인용이 {why}: {str(q)[:50]!r}")
    return errs

def drop_bad_quotes(items, source_text: str, quote_key: str = "quote"):
    src = norm(source_text)
    kept = [it for it in items if _quote_hit(it, quote_key, src)]
    return kept, len(items) - len(kept)

def assign_ids(items, period_id: str, start: int = 1):
    for n, it in enumerate(items, start):
        it["id"] = f"q_{period_id}_{n:03d}"
    return items

def is_sec(v) -> bool:
    """초 단위 정수 1건인지. bool을 따로 막는 건 int의 서브클래스라서다(True가 1초로 통과한다)."""
    return isinstance(v, int) and not isinstance(v, bool)

def ts_ok(item, keys, valid_ts) -> bool:
    """항목의 시각 필드가 실재 블록 타임스탬프인지(DESIGN §7) — 맞으면 블록 표기로 고쳐 넣는다.

    문자열이 아니라 초로 대조하는 이유: 모델이 같은 시각을 "5:13"·"05:13"·"0:05:13"으로
    제각각 쓴다. 실재하지 않는 ts는 인용이 진짜여도 다른 지점에 갖다 붙인 것이므로 폐기한다
    — 하류 리포트가 이 ts로 앵커와 중단 시간을 계산한다.
    (예외는 구간 경계인 s40 timeline뿐 — 블록 ts와 같을 수 없어 형식만 본다.)"""
    if not isinstance(item, dict):
        return False
    for k in keys:
        v = item.get(k)
        try:
            sec = v if is_sec(v) else parse_ts(str(v))
        except ValueError:
            return False
        if sec not in valid_ts:
            return False
        item[k] = fmt_ts(sec)
    return True

def clean(items, source_text: str, valid_ts, quote_key: str = "quote", ts_keys=("ts",)):
    """인용 검증 → ts 검증 순으로 거른 (남은 항목, 폐기 수) — s30·s40 공용 단일 구현.

    두 단계를 한 함수로 묶는 건 순서가 계약이기 때문이다: 인용이 가짜면 ts를 볼 필요가 없고,
    ts 표기 표준화는 '인용이 진짜인 항목'에만 적용돼야 한다."""
    kept, dropped = drop_bad_quotes(items, source_text, quote_key=quote_key)
    with_ts = [it for it in kept if ts_ok(it, ts_keys, valid_ts)]
    return with_ts, dropped + len(kept) - len(with_ts)
