#!/bin/bash
# 최초 1회: s10(transcribe) 전용 venv. 나머지 스테이지는 시스템 python3 stdlib만 사용.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install --quiet --upgrade pip mlx-whisper
# 버전 확인은 변수로 받는다 — echo 인자 안에서는 import 실패가 set -e에 걸리지 않아
# 설치가 깨져도 "OK"만 찍힌다.
ver=$(./.venv/bin/python -c 'import mlx_whisper; print(getattr(mlx_whisper, "__version__", "installed"))')
echo "OK: mlx-whisper $ver"
echo "모델(large-v3-turbo, ~1.6GB)은 첫 전사 때 자동 다운로드됩니다."
