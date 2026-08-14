#!/bin/bash
# lecture-analysis 파이프라인 진입점. 사용법:
#   analyze.sh init <records_dir> [--name <강의명>] | run <manifest> [--force [sNN]] | status <manifest>
#
# 이 스크립트는 판단하지 않는다 — 순서를 지키고, 실패를 숨기지 않고, 어디서 멈췄는지 남긴다.
# 분석 로직은 전부 scripts/sNN_*.py에 있다.
set -euo pipefail
SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
PY=python3
# s10만 .venv(mlx-whisper)로 돈다. LA_VENV_PY는 테스트 seam이다 —
# mlx 설치 없이 s10 게이트(오디오 유무·.venv 유무)를 확인하려면 실행 파일을 갈아끼워야 한다.
VENV_PY="${LA_VENV_PY:-$SKILL_DIR/.venv/bin/python}"
STAGES=(s10 s20 s30 s40 s50 s60 s65 s70 s80)
AUDIO_EXT=(m4a wav mp3 aac)

die() { echo "$1" >&2; exit "${2:-2}"; }

usage() {
  echo "사용법: analyze.sh init <records_dir> [--name <강의명>] | run <manifest> [--force [sNN]] | status <manifest>" >&2
  exit 2
}

# ── init ────────────────────────────────────────────────────────────────────
find_inputs() {  # $1=폴더, $2…=확장자 → 정렬된 절대 경로 1줄씩(하위 폴더 제외)
  local dir="$1" ext; shift
  local expr=()
  # -iname(대소문자 무시)인 이유: 실물은 .TXT·.M4A로도 떨어진다(내보내기 도구·녹음기마다
  # 다르다). -name이면 그 파일이 조용히 빠지고 — 그게 최악이다 — 그 교시는 manifest에
  # 실리지 않은 채 리포트가 '전 교시 분석'으로 발행된다(미가용 목록은 manifest에 있는
  # 교시만 센다). 빠진 사실이 어디에도 남지 않는다.
  for ext in "$@"; do
    [ ${#expr[@]} -eq 0 ] || expr+=(-o)
    expr+=(-iname "*.$ext")
  done
  find "$dir" -maxdepth 1 -type f \( "${expr[@]}" \) | LC_ALL=C sort
}

cmd_init() {  # $1=records_dir  $2…=--name <이름> (선택, 기본값: 폴더명)
  local records="$1" name="" i f
  shift
  while [ $# -gt 0 ]; do
    case "$1" in
      --name) [ $# -ge 2 ] || die "--name 값이 없습니다"; name="$2"; shift 2 ;;
      *) die "알 수 없는 인자: $1 (사용법: init <records_dir> [--name <강의명>])" ;;
    esac
  done
  [ -d "$records" ] || die "records_dir 없음: $records"
  # 절대 경로로 굳힌다. 세션 폴더는 녹취 폴더 밖(LECTURE_ANALYSIS)이라, 상대 경로가
  # 그대로 실리면 로더가 다른 자리를 뒤진다.
  records="$(cd "$records" && pwd)"
  [ -n "$name" ] || name="$(basename "$records")"
  # 슬러그는 파이썬 구현 한 곳만 쓴다 — bash가 따로 구현하면 파일명 파생이 갈라진다.
  local slug
  slug="$("$PY" -c "import sys; sys.path.insert(0, '$SKILL_DIR/scripts'); \
from lib.naming import slugify; print(slugify(sys.argv[1]))" "$name")"
  # '----'처럼 치환 문자만 남으면 사람이 알아볼 수 없는 폴더가 된다 — 이름을 다시 받는다.
  printf '%s' "$slug" | grep -q '[0-9A-Za-z가-힣]' \
    || die "강의명에 쓸 수 있는 글자가 없습니다: '$name' — --name으로 지정하세요"
  local root="$PWD/LECTURE_ANALYSIS"
  mkdir -p "$root" || die "산출물 루트를 만들 수 없습니다(쓰기 권한 확인): $root"
  local dir="$root/${slug}_$(date +%Y%m%d-%H%M)"
  [ -e "$dir/manifest.toml" ] && die "이미 존재: $dir/manifest.toml (1분 뒤 다시 시도하거나 폴더를 지우세요)"
  local txts=() auds=()
  # 파이프(`find | while`)로 읽으면 서브셸이라 배열이 남지 않는다 — 프로세스 치환으로 받는다.
  while IFS= read -r f; do txts+=("$f"); done < <(find_inputs "$records" txt)
  while IFS= read -r f; do auds+=("$f"); done < <(find_inputs "$records" "${AUDIO_EXT[@]}")
  local n=${#txts[@]} p
  [ ${#auds[@]} -gt "$n" ] && n=${#auds[@]}
  mkdir -p "$dir"
  {
    echo "[session]"
    printf 'name = "%s"\n' "$slug"
    printf 'date = %s  # 강의 날짜 — 산출물 파일명에 들어갑니다. 실제 강의일로 고치세요.\n' "$(date +%Y-%m-%d)"
    echo '# plan = "/절대/경로/강의_계획.md"  # 선택: 커버리지 체크'
    echo
    if [ ${#auds[@]} -gt 0 ]; then
      echo '# 오디오는 파일명 순서대로 pNN에 배정했습니다 — 교시 매칭 확인 필수'
      echo '# (어긋나면 말속도·침묵 같은 오디오 지표가 통째로 다른 교시에 붙습니다).'
      echo
    fi
    for ((i = 0; i < n; i++)); do
      printf '[[period]]\nid = "p%02d"\n' "$((i + 1))"
      # 경로의 \와 "를 이스케이프한다. 이제 값은 상위 폴더까지 담긴 절대 경로라 사용자가
      # 만들지 않은 조상 폴더 이름이 TOML 안으로 들어온다 — 하나만 새도 manifest 전체가
      # 파싱 불가가 되고, 오류는 어느 줄 때문인지 알려주지 않는 TOMLDecodeError로 나온다.
      # \가 반드시 먼저다: "를 \"로 바꾼 뒤 \를 처리하면 방금 넣은 이스케이프까지 다시
      # 이스케이프해 경로가 망가진다. \를 흘리면 두 갈래로 깨지는데, `\자`처럼 무효
      # 이스케이프는 로드가 죽고(그나마 드러난다), `\t`·`\n`은 조용히 탭·개행으로 변질돼
      # '참조 파일 없음'으로만 나타난다 — 둘 다 init이 exit 0을 낸 한참 뒤의 일이다.
      if [ "$i" -lt ${#txts[@]} ]; then
        p="${txts[$i]//\\/\\\\}"; printf 'clova = "%s"\n' "${p//\"/\\\"}"
      fi
      if [ "$i" -lt ${#auds[@]} ]; then
        p="${auds[$i]//\\/\\\\}"; printf 'audio = "%s"\n' "${p//\"/\\\"}"
      fi
      echo
    done
    echo "[speakers]"
    echo 'instructor_label = "auto"'
    echo 'mask_names = []  # LLM 전송 전 치환할 실명 — 비우면 원문 그대로 나갑니다'
    echo
    echo "[bounds]"
    echo '# p01 = { start = "05:13", end = "60:00", breaks = [] }'
  } > "$dir/manifest.toml"
  [ "$n" -gt 0 ] || echo "주의: $records 에 txt·오디오가 없습니다 — 폴더가 맞는지 확인하세요." >&2
  echo "생성: $dir/manifest.toml — 교시 매핑·mask_names 확인 후 run 하세요."
}

# ── run ─────────────────────────────────────────────────────────────────────
has_audio() {  # manifest에 주석이 아닌 audio 항목이 있는가
  grep -qE '^[[:space:]]*audio[[:space:]]*=' "$1"
}

stage_cmd() {  # $1=스테이지 $2=manifest $3=force여부(0/1)
  local s="$1" mani="$2" force="$3" args=()
  [ "$force" = "1" ] && args+=(--force)
  # bash 3.2(맥 기본)는 set -u에서 빈 배열의 "${a[@]}"를 unbound로 본다 — ${a[@]+…}로 감싼다.
  case "$s" in
    s10) # 오디오가 없으면 부를 이유가 없다 — .venv 파이썬은 mlx 로딩만으로 수 초를 쓴다.
         has_audio "$mani" || { echo "s10 스킵(manifest에 audio 없음 — 정밀 오디오 지표 미측정)"; return 0; }
         [ -x "$VENV_PY" ] || { echo "s10 스킵(.venv 없음 — setup.sh 실행; 정밀 오디오 지표 미측정)"; return 0; }
         "$VENV_PY" "$SKILL_DIR/scripts/s10_transcribe.py" "$mani" ${args[@]+"${args[@]}"} ;;
    s70|s80) # 둘 다 추세 CSV를 읽으므로 같은 _growth 오버라이드를 받는다 —
         # 위치가 갈라지면 md의 추세 미니표와 html의 추세 카드가 서로 다른 과거를 그린다.
         local extra=()
         [ -n "${LA_GROWTH_DIR:-}" ] && extra=(--growth-dir "$LA_GROWTH_DIR")
         "$PY" "$SKILL_DIR/scripts/${s}_"*.py "$mani" \
               ${args[@]+"${args[@]}"} ${extra[@]+"${extra[@]}"} ;;
    *)   "$PY" "$SKILL_DIR/scripts/${s}_"*.py "$mani" ${args[@]+"${args[@]}"} ;;
  esac
}

stage_known() {
  local s
  for s in "${STAGES[@]}"; do
    if [ "$s" = "$1" ]; then return 0; fi
  done
  return 1
}

cmd_run() {
  local mani="$1" forcing=0 force_from="" started=0 s f rc
  [ -f "$mani" ] || die "manifest 없음: $mani"
  if [ "${2:-}" = "--force" ]; then
    forcing=1
    force_from="${3:-s10}"
    # 오타(--force s35)를 조용히 무시하면 아무것도 재생성되지 않은 채 전부 SKIP으로 돌고,
    # 사용자는 강제 재생성했다고 믿는다.
    stage_known "$force_from" || die "--force 대상이 스테이지가 아닙니다: $force_from (${STAGES[*]})"
  elif [ -n "${2:-}" ]; then
    die "알 수 없는 인자: $2"
  fi
  for s in "${STAGES[@]}"; do
    f=0
    if [ "$forcing" = "1" ]; then
      if [ "$s" = "$force_from" ]; then started=1; fi
      f=$started
    fi
    echo "── $s ──"
    rc=0
    stage_cmd "$s" "$mani" "$f" || rc=$?
    # set -e에 맡기면 코드는 전파돼도 어디서 멈췄는지가 사라진다 — 스테이지 이름을 남긴다.
    [ "$rc" -eq 0 ] || { echo "중단: $s (exit $rc)" >&2; exit "$rc"; }
  done
  # 파일명은 manifest 파생이라 여기서 다시 조립하지 않는다 — 폴더만 가리키고 안내는 README에 맡긴다.
  # README.md는 outputs/가 아니라 세션 폴더 루트다(스펙 §4) — 경로를 붙여 말하면 없는 자리를 뒤진다.
  echo "완료: $(dirname "$mani") — outputs/의 분석리포트부터 보세요(안내: README.md)"
}

# ── status ──────────────────────────────────────────────────────────────────
cmd_status() {
  local mani="$1" dir f n bad note
  [ -f "$mani" ] || die "manifest 없음: $mani"
  dir="$(cd "$(dirname "$mani")" && pwd)"
  # printf의 %-18s는 바이트를 센다 — 한글 제목은 손으로 맞춘다(값 열은 전부 ASCII 경로).
  echo "산출물             상태"
  # 최종 산출물은 파일명이 manifest 파생이라 이름을 여기서 조립하지 않는다 —
  # outputs/ 폴더째로 세면 강의명·날짜가 바뀌어도 status가 따라 깨지지 않는다.
  for f in artifacts/whisper artifacts/parsed artifacts/llm artifacts/metrics.json outputs; do
    if [ ! -e "$dir/$f" ]; then
      printf "%-18s %s\n" "$f" "-"
      continue
    fi
    n="$(find "$dir/$f" -type f 2>/dev/null | wc -l | tr -d ' ')"
    # 중단된 스테이지가 남긴 빈 폴더다(가드가 산출물을 쓰기 전에 부모를 만든다).
    # '있음 (0개)'로 보이면 미측정이 완료처럼 읽혀 사용자가 다시 돌리지 않는다.
    if [ "$n" = "0" ]; then
      printf "%-18s %s\n" "$f" "비어 있음 (중단된 스테이지 — 다시 run 하세요)"
      continue
    fi
    note=""
    if [ "$f" = "artifacts/llm" ]; then
      # llm_failed 기록은 '있는 산출물'이 아니라 '비어 있는 교시'다 — 개수만 보면 성공처럼 보인다.
      bad="$(grep -l '"llm_failed"' "$dir"/artifacts/llm/*.json 2>/dev/null | wc -l | tr -d ' ')" || true
      [ "$bad" = "0" ] || note=", 실패 마커 $bad"
    fi
    printf "%-18s %s\n" "$f" "있음 (${n}개${note})"
  done
}

case "${1:-}" in
  init)   [ $# -ge 2 ] || usage; shift; cmd_init "$@" ;;
  run)    { [ $# -ge 2 ] && [ $# -le 4 ]; } || usage; shift; cmd_run "$@" ;;
  status) [ $# -eq 2 ] || usage; cmd_status "$2" ;;
  *)      usage ;;
esac
