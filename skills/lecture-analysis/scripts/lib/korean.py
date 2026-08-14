"""한국어 텍스트 유틸 — 지표 정의 SSOT(references/metrics.md)를 구현으로 옮긴 층.
정의 안정성이 정확성보다 중요: 로직 변경은 추세 리셋을 뜻한다."""
import re

_SENT_END = re.compile(r"(?<=[.?!…])\s+|\n+")
_HANGUL = re.compile(r"[가-힣]")
_FILLER_TOKENS = {"어", "음"}  # 정의 고정 — 변경은 정의 변경(metrics.md 참조)
_STRIP = ".,!?…~\"'()[]{}<>:;"

def split_sentences(text: str) -> list[str]:
    parts = _SENT_END.split(text)
    return [p.strip() for p in parts if p and p.strip()]

def count_syllables(text: str) -> int:
    return len(_HANGUL.findall(text))

def is_hedging(sentence: str) -> bool:
    return "것 같" in sentence

def count_fillers(text: str) -> int:
    n = 0
    for tok in text.split():
        if tok.strip(_STRIP) in _FILLER_TOKENS:
            n += 1
    return n
