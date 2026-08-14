"""산출물 이름 규칙 — 슬러그·강의 날짜 압축의 단일 구현(배포 스펙 §4).

파일명 파생이 두 곳이면(bash init과 파이썬 s70이 각자 구현하는 식) 강의명 개명·날짜
수정 시 서로 다른 파일을 가리킨다 — analyze.sh init도 python3 -c로 이 모듈을 부른다."""
import re
import unicodedata

_KEEP = re.compile(r"[0-9A-Za-z가-힣_-]")

def slugify(name: str) -> str:
    """공백류→'_', 허용 밖 문자→'-'. 허용: 한글 음절·영숫자·'-'·'_'.
    빈 결과를 그대로 돌려주는 이유: 기본값('강의')이냐 오류(exit 2)냐는 호출부마다 다르다.

    NFC 정규화가 먼저인 이유: 맥 파일시스템은 한글을 NFD(자모 분해)로 저장하고, 파인더
    드래그·터미널 탭 완성으로 들어온 폴더명도 NFD다. 허용 목록의 '가-힣'은 **완성형 음절**만
    맞으므로 정규화 없이 걸면 분해된 자모가 전부 '-'가 된다 — '업무자동화'가 '------------'가
    되고, 이름이 통째로 치환 문자만 남으면 init은 아예 exit 2로 죽는다(엉뚱한 안내와 함께).
    배포판에서 --name을 폴더명으로 채우는 것이 기본 경로라 이 입력이 예외가 아니라 표준이다."""
    s = re.sub(r"\s+", "_", unicodedata.normalize("NFC", name or "").strip())
    return "".join(ch if _KEEP.match(ch) else "-" for ch in s)

def compact_date(date: str) -> str:
    """'2026-08-11' → '20260811'. 숫자가 없으면 '날짜미상' — 파일명 슬롯을 비우지 않는다."""
    d = re.sub(r"\D", "", date or "")[:8]
    return d or "날짜미상"
