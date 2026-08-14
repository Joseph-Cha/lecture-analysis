#!/usr/bin/env python3
"""s10: 오디오 → artifacts/whisper/pNN.json (완전 로컬 — 오디오는 기기 밖으로 나가지 않는다).
mlx_whisper는 .venv에만 있으므로 이 스크립트는 analyze.sh가 .venv/bin/python으로 실행한다.
to_segments()는 stdlib만 사용 — 시스템 python 유닛 테스트 가능."""
import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib.manifest import load_manifest
from lib.stage import cli, stage_guard, write_json, run

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"

def to_segments(raw: dict) -> list:
    """mlx_whisper 결과를 계약 형태로 정규화 — start·end·text만, text는 strip.

    빈 텍스트 세그먼트를 버리는 이유: whisper는 무음 구간에도 빈/공백 세그먼트를 낸다.
    그대로 두면 s50이 '발화 시간'으로 세어 말속도(SPM)를 실제보다 낮게 만든다."""
    out = []
    for s in raw.get("segments", []):
        text = (s.get("text") or "").strip()
        if text:
            out.append({"start": float(s["start"]), "end": float(s["end"]), "text": text})
    return out

def _why(err: str, keep: int = 2) -> str:
    """디코딩 실패 사유 요약 — ffmpeg는 배너 20여 줄을 먼저 뱉고 진짜 원인은 끝에 온다."""
    lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
    return " / ".join(lines[-keep:]) if lines else err.strip()

def main():
    args = cli("오디오 전사 (mlx-whisper, 로컬)")
    m = load_manifest(args.manifest)
    model = os.environ.get("LA_WHISPER_MODEL", DEFAULT_MODEL)
    try:
        import mlx_whisper
    except ImportError:
        print("mlx_whisper 없음 — setup.sh 실행 후 .venv/bin/python으로 실행하세요.", file=sys.stderr)
        sys.exit(2)
    for per in m.periods:
        out = m.artifacts_dir / "whisper" / f"{per.id}.json"
        if per.audio is None:
            print(f"{per.id}: 오디오 없음 → 스킵(정밀 지표 미측정)")
            continue
        if not stage_guard(out, args.force):
            continue
        print(f"{per.id}: 전사 시작 ({per.audio.name}) — 수 분 소요", flush=True)
        try:
            raw = mlx_whisper.transcribe(str(per.audio), path_or_hf_repo=model, language="ko")
        except FileNotFoundError as e:
            # mlx_whisper는 디코딩을 ffmpeg CLI에 맡긴다 — 없으면 어떤 오디오도 못 읽는다.
            raise ValueError(f"{per.id}: 오디오 디코딩에 ffmpeg가 필요합니다 "
                             f"(brew install ffmpeg) — {e}") from e
        except RuntimeError as e:
            # 깨진·미지원 오디오는 사용자 입력 문제(exit 2)다. 그냥 두면 RuntimeError가
            # run() 래퍼를 지나쳐 트레이스백 + exit 1로 새고 규약(0/2/3)이 깨진다.
            # 전사 중의 다른 RuntimeError(메모리·백엔드)까지 덮으면 진짜 원인이 사라지므로
            # mlx_whisper의 로드 실패만 좁게 잡는다.
            if "Failed to load audio" not in str(e):
                raise
            raise ValueError(f"{per.id}: 오디오를 읽지 못했습니다 ({per.audio.name}) "
                             f"— {_why(str(e))}") from e
        segs = to_segments(raw)
        write_json(out, {"period": per.id, "model": model, "segments": segs})
        print(f"{per.id}: 세그먼트 {len(segs)}건")

if __name__ == "__main__":
    run(main)
