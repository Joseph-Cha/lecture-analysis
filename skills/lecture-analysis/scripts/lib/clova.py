"""클로바노트 내보내기 txt 파서.
형식: 1행 제목 / 2행 'YYYY.MM.DD 요일 시각 ・ N분 N초' / 3행 작성자 /
이후 '화자라벨 MM:SS' 헤더 + 본문 블록 반복. 형식 변형은 ValueError로 즉시 드러낸다(조용한 누락 금지)."""
import re
from dataclasses import dataclass

_HDR2 = re.compile(r"^(\d{4}\.\d{2}\.\d{2})\s+\S+\s+.*?・\s*(?:(\d+)시간)?\s*(\d+)분\s*(\d+)초")
_BLOCK = re.compile(r"^(.+?)\s+(\d{1,2}:\d{2}(?::\d{2})?)$")
_FOOTER = "clovanote.naver.com"  # 내보내기 푸터 — 발화 본문 아님(실물 6개 파일 전부에 존재)

@dataclass
class Block:
    speaker: str
    ts: int
    text: str

@dataclass
class ClovaDoc:
    title: str
    date: str
    duration_sec: int
    owner: str
    blocks: list

def parse_ts(s: str) -> int:
    parts = [int(p) for p in s.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"타임스탬프 형식 오류: {s}")

def fmt_ts(sec: int) -> str:
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

def parse_clova(text: str) -> ClovaDoc:
    lines = text.splitlines()
    if len(lines) < 4:
        raise ValueError("클로바노트 형식 아님: 헤더 3줄 미만")
    m = _HDR2.match(lines[1].strip())
    if not m:
        raise ValueError(f"헤더 2행 형식 오류: {lines[1]!r}")
    date = m.group(1)
    dur = (int(m.group(2) or 0)) * 3600 + int(m.group(3)) * 60 + int(m.group(4))
    blocks, cur = [], None
    for line in lines[3:]:
        bm = _BLOCK.match(line.strip())
        if bm:
            if cur:
                blocks.append(Block(cur[0], cur[1], "\n".join(cur[2]).strip()))
            cur = (bm.group(1), parse_ts(bm.group(2)), [])
        elif cur is not None and line.strip() and line.strip() != _FOOTER:
            cur[2].append(line.strip())
    if cur:
        blocks.append(Block(cur[0], cur[1], "\n".join(cur[2]).strip()))
    if not blocks:
        raise ValueError("발화 블록 0건 — 형식 변형 의심")
    return ClovaDoc(lines[0].strip(), date, dur, lines[2].strip(), blocks)

def speaker_shares(doc: ClovaDoc) -> dict:
    totals = {}
    for b in doc.blocks:
        totals[b.speaker] = totals.get(b.speaker, 0) + len(b.text)
    whole = sum(totals.values()) or 1
    return {k: v / whole for k, v in totals.items()}

def pick_instructor(doc: ClovaDoc):
    shares = speaker_shares(doc)
    label = max(shares, key=shares.get)
    return label, shares[label]

def mask_text(text: str, names: list) -> str:
    # 긴 이름부터 치환 — 짧은 이름이 긴 이름의 접두사일 때 '[이름1]수' 식 잔여 노출을 막는다.
    # 번호는 names의 원래 인덱스를 유지한다(names[i] → [이름{i+1}]).
    for i, name in sorted(enumerate(names, 1), key=lambda p: -len(p[1])):
        text = text.replace(name, f"[이름{i}]")
    return text
