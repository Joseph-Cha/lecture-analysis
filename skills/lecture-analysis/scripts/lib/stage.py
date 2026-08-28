"""스테이지 공통: 이어달리기 가드·JSON IO·CLI 파서·교시 병렬 실행."""
import argparse, json, os, sys, threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 리포트 C3에 찍히는 파이프라인 버전(DESIGN §8 C3). 지표 정의·계산 규칙이 바뀌면 올린다 —
# 그때 추세 CSV의 앞 회차와 뒤 회차는 같은 뜻의 값이 아니게 되므로, 버전이 리포트에 남아
# 있어야 나중에 "왜 여기서 값이 튀었나"를 되짚을 수 있다(metrics.md의 '정의 변경 = 추세 리셋').
# 렌더 문구·오탈자 수정처럼 값이 달라지지 않는 변경은 올리지 않는다.
# 1.1.0 (2026-08-11, 개선 브리프 P1~P3): 헤징 예시·대기 중앙값·무발화 창 필드 추가(s50),
# % 표기 소수 1자리 통일·사과 집계/제외 분리·B4 배분 요약(s70), report.html 발행(s80).
# 지표 정의(metrics.md)는 불변 — 사과·헤징·트러블 카운트 값은 1.0.0과 같은 뜻이다.
PIPELINE_VERSION = "1.1.0"

def cli(desc: str):
    ap = argparse.ArgumentParser(description=desc)
    ap.add_argument("manifest")
    ap.add_argument("--force", action="store_true")
    return ap.parse_args()

def stage_guard(out_path: Path, force: bool) -> bool:
    """True면 실행, False면 SKIP(이미 산출물 존재)."""
    if out_path.exists() and not force:
        print(f"SKIP {out_path.name} (exists; --force로 재생성)")
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return True

def stage_guard_llm(out_path: Path, force: bool) -> bool:
    """LLM 스테이지용 이어달리기 가드 — 실패 기록은 '완료된 산출물'이 아니다.

    LLM 실패 교시는 {"period":…, "llm_failed":…}를 남기고 exit 4로 끝난다. 파일 존재만
    보는 stage_guard로는 그 파일이 다음 실행에서 SKIP을 만들고 스테이지가 exit 0으로
    성공 둔갑한다 — 교시는 영원히 빈 채로 남고(s50은 full 구간으로 폴백) 사용자에게
    보이는 건 '성공'뿐이라, 파일을 손으로 지우기 전엔 복구되지 않는다.
    실패 기록·깨진 JSON(쓰다 만 산출물)이면 --force 없이 그 교시만 다시 실행한다.

    s40·s60·s65가 같은 실패 산출물 규약을 쓰므로 스테이지마다 복제하지 말고 이걸 쓴다."""
    if out_path.exists() and not force:
        try:
            prev = read_json(out_path)
            done = not (isinstance(prev, dict) and "llm_failed" in prev)
        except ValueError:      # 깨진 JSON = 쓰다 만 산출물
            done = False
        if not done:
            print(f"RETRY {out_path.name} (직전 실행이 미완 — 재시도)")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            return True
    return stage_guard(out_path, force)

def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def require_json(path: Path, script: str):
    """상류 산출물 읽기 — 없으면 스테이지 순서가 어긋난 것뿐이므로 고칠 방법을 알려준다.

    트레이스백(exit 1) 대신 ValueError → 래퍼의 '입력 오류 exit 2'로 내보낸다."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"{path.name} 없음 — {script}를 먼저 실행하세요")
    return read_json(path)

def load_analysis(m, kind: str):
    """교시별 {kind}(semantic·structure) 산출물 — (쓸 수 있는 것, [(사유 문구, 갈래), …]).

    s30·s40은 전사 없는 교시에 {"skipped": …}, LLM 실패 교시에 {"llm_failed": …}를 남긴다.
    둘 다 '관측이 없다'는 뜻이라 소비하는 쪽에서 빼야 한다 — 실패 사유 문자열을 관측인 양
    s65가 실어 보내면 모델이 그걸 근거로 삼고, s70이 렌더하면 리포트가 실패 로그를 관측처럼
    보여준다.

    다만 조용히 빼면 더 나쁘다. s65에게 p03은 그냥 존재하지 않는 교시가 되고, 남은 교시만
    보고 '세션 전체가 매끄러웠다'고 쓴다 — 정작 그 교시가 통째로 미측정인데도. 그래서 뺀
    사실을 s65는 payload의 `unavailable`로, s70은 리포트 A5로 함께 낸다.
    (두 스테이지가 같은 목록을 봐야 리포트가 '리뷰가 왜 얇은지'를 설명할 수 있다 — 그래서
     규칙을 여기 한 곳에 둔다.)

    갈래(missing/skipped/failed)를 함께 돌려주는 건 '다시 실행하면 달라지는가'가 갈래마다
    다르기 때문이다. skipped(no_clova)는 전사가 없다는 결정론적 사실이라 몇 번을 돌려도
    같지만, failed·missing은 s30·s40을 다시 돌리면 관측이 생긴다."""
    usable, unavailable = {}, []
    for per in m.periods:
        path = m.artifacts_dir / "llm" / f"{per.id}_{kind}.json"
        where = f"{per.id} {kind}"
        if not path.is_file():
            unavailable.append((f"{where}: 파일 없음(스테이지 미실행)", "missing"))
            continue
        try:
            doc = read_json(path)
        except ValueError:                      # 쓰다 만 산출물(깨진 JSON)
            unavailable.append((f"{where}: 산출물 손상(JSON 파싱 실패)", "failed"))
            continue
        if not isinstance(doc, dict):
            unavailable.append((f"{where}: 산출물 형식 오류", "failed"))
        elif "llm_failed" in doc:
            unavailable.append((f"{where}: llm_failed({str(doc['llm_failed'])[:60]})", "failed"))
        elif "skipped" in doc:
            unavailable.append((f"{where}: skipped({doc['skipped']})", "skipped"))
        else:
            usable[per.id] = doc
    return usable, unavailable

def die(code: int, msg: str):
    print(msg, file=sys.stderr)
    sys.exit(code)

class HardStop(Exception):
    """하드 중단(exit 3) 신호 — run() 래퍼가 die(3)로 바꾼다.

    워커 스레드에서 die()를 부르면 SystemExit이 그 스레드 안에서만 나고 메인은 모른다.
    예외로 올려 보내면 map_jobs가 미시작 job을 취소한 뒤 메인 스레드에서 종료 코드를 낸다
    — 순차 코드에서 써도 같은 뜻이다(메시지는 stderr, 코드는 3)."""

DEFAULT_WORKERS = 3

def parallel_workers() -> int:
    """LA_PARALLEL — 교시별 `claude -p`를 동시에 몇 개 띄울지(1이면 순차). 기본 3.

    오타·0·음수는 기본값으로 조용히 떨어뜨리지 않고 입력 오류(exit 2)로 낸다 — 사용자는
    병렬로 도는 줄 알고 기다린다. 값의 뜻은 '동시 sub-agent 수'다: 각 호출이 독립
    프로세스라 지표·산출물은 워커 수와 무관하게 같다(교시 간 공유 상태가 없다)."""
    raw = os.environ.get("LA_PARALLEL", "").strip()
    if not raw:                     # 미지정과 빈 값(`LA_PARALLEL= …`)은 같은 뜻 — 기본값
        return DEFAULT_WORKERS
    try:
        n = int(raw)
    except ValueError:
        n = 0
    if n < 1:
        raise ValueError(f"LA_PARALLEL은 1 이상의 정수여야 함(받은 값: {raw!r})")
    return n

def map_jobs(work, jobs, workers: int):
    """jobs 각각에 work를 스레드 풀로 적용해 **제출 순서대로** 결과를 낸다(제너레이터).

    완료 순서가 아니라 제출 순서인 이유: 스테이지 출력 줄과 후속 처리가 교시 순서를 따라야
    순차 실행과 같은 로그가 남는다(어느 교시가 어떻게 됐는지 사람이 위에서 아래로 읽는다).
    work가 예외를 내면 첫 예외를 그대로 올리고 **아직 시작하지 않은 job은 취소**한다 —
    하드 중단·입력 오류 뒤의 LLM 호출은 낭비다(진행 중인 호출은 완료를 기다린다; 죽일 수
    없고, 그 산출물은 다음 실행에서 SKIP으로 살아남는다).

    work는 스레드에서 돈다: 파일 쓰기는 교시별 파일이라 안전하지만 print는 메인에서 하라
    (워커가 찍으면 줄이 섞인다) — 결과로 메시지를 돌려주고 여기서 받아 찍는 규약이다."""
    jobs = list(jobs)
    if not jobs:
        return
    stop = threading.Event()
    def guarded(job):
        # 워커가 다음 job을 집는 순간과 메인이 예외를 받는 순간 사이에 틈이 있다 — 취소 플래그를
        # 워커 자신이 예외 직후에 세워야 같은 워커의 다음 job이 확실히 건너뛴다.
        if stop.is_set():
            return None
        try:
            return work(job)
        except BaseException:
            stop.set()
            raise
    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as ex:
        futures = [ex.submit(guarded, j) for j in jobs]
        try:
            for f in futures:
                yield f.result()
        except BaseException:
            ex.shutdown(wait=True, cancel_futures=True)
            raise

def run(main_fn) -> None:
    """스테이지 진입점 공통 래퍼 — 종료 코드 규약(0 성공 / 2 입력 오류 / 3 하드 중단)을 지킨다.

    manifest 부재·TOML 문법 오류·참조 파일 부재·클로바 형식 변형은 전부 '입력 오류'다.
    래퍼가 없으면 트레이스백과 함께 exit 1로 새어 규약이 깨진다.
    (tomllib.TOMLDecodeError는 ValueError, FileNotFoundError는 OSError의 서브클래스다.
     die()가 던지는 SystemExit은 여기서 잡히지 않으므로 3번 하드 중단은 그대로 통과한다.)"""
    try:
        main_fn()
    except HardStop as e:
        die(3, str(e))
    except (ValueError, OSError) as e:
        die(2, f"입력 오류: {e}")
