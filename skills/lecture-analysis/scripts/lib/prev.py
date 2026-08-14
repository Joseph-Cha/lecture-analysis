"""직전 세션 산출물 해석 — 액션 연속성의 원천 (DESIGN §5 prev 3단 폴백).

s60이 판정할 '직전 액션'과 s70의 목표 신선도 배너가 전부 여기서 나온다. 추출이 결정론이라야
하는 이유: 액션 목록을 LLM에 맡기면 회차마다 문장이 미묘하게 달라져 같은 액션이 추적되지 않는다.
"""
import re
from pathlib import Path

# 체크박스 액션 줄. `- [x]`(확정) / `- [ ]`(후보) 둘 다 받는다.
_CHECK = re.compile(r"^- \[([ xX])\] \*\*액션\*\*:\s*(.+)$")
# 필드 구분자는 중점(·)이 아니라 '· **라벨**:' 이다.
# 중점만 보고 자르면 실물 회고의 본문·기준이 중간에서 잘린다
# ("지급 계정 + Windows PC로 … (체크리스트: Codex OAuth·기본 브라우저 …)",
#  "s04 녹취에 실시간 정정·사과 발언 0회, 자료 배포 실패 0건").
# 잘린 기준으로 판정하면 s60이 원래 기준과 다른 것을 판정한다.
# 라벨 자체에도 중점이 들어간다(신형 리포트의 '**전략·도구·준비**') — 그래서 라벨은 [^*]+.
_FIELD_SEP = re.compile(r"\s*·\s*(?=\*\*[^*]+\*\*\s*:)")
_FIELD = re.compile(r"^\*\*([^*]+)\*\*\s*:\s*(.*)$", re.S)
# 회고 '## 6. 다음 액션' 절 끝에 붙는 비액션 목록 — 여기부터는 액션이 아니다.
_STOP = "**검토할 것**"

def prev_session_id(session_id: str):
    """"s04" → "s03". 번호가 아니거나 s00이면 None(= 더 앞 세션이 없다).

    s01 → "s00"은 의도된 경계다(PLAN 타입 일관성 수정 ①). s00 산출물이 없는 보통의 경우엔
    파일 탐색이 실패해 그대로 '첫 분석'이 되므로 손해가 없고, 준비 회차를 s00으로 둔
    프로젝트에서는 그 회차가 직전으로 잡힌다."""
    m = re.fullmatch(r"s(\d+)", session_id or "")
    if not m or int(m.group(1)) < 1:
        return None
    return f"s{int(m.group(1)) - 1:02d}"

def _split_fields(body: str):
    """액션 줄 본문 → (액션 텍스트, {라벨: 값}). 레거시(전략 필드 없음)·신형 모두 같은 규칙."""
    parts = _FIELD_SEP.split(body)
    fields = {}
    for seg in parts[1:]:
        fm = _FIELD.match(seg.strip())
        if fm:
            fields[fm.group(1).strip()] = fm.group(2).strip()
    return parts[0].strip(), fields

def _parse_actions(md: str, heading_re: str):
    """지정한 절의 체크박스 액션만 추출. 절 밖의 같은 형식 줄은 읽지 않는다."""
    out, in_sec = [], False
    for ln in md.splitlines():
        if re.match(heading_re, ln):
            in_sec = True
            continue
        if not in_sec:
            continue
        if ln.startswith("#") or ln.startswith(_STOP):
            break
        m = _CHECK.match(ln.strip())
        if m:
            text, fields = _split_fields(m.group(2))
            out.append({"text": text, "criteria": fields.get("판정 기준", ""),
                        "confirmed": m.group(1).lower() == "x"})
    return out

def resolve_prev(m) -> dict:
    """직전 세션의 액션 목록 — ① 분석 리포트 B7 → ② 레거시 회고 '## 6' → ③ 없음.

    ①에서 확정(`- [x]`)이 하나라도 있으면 그것만, 하나도 없으면 후보 전체를 판정 대상으로
    삼는다(DESIGN §6 s60). 강사가 확정 대화를 건너뛴 회차에서 직전 액션이 통째로
    사라지는 것을 막기 위해서다.
    (manifest의 `prev`는 계약상 "auto" 한 값뿐이라 여기서 분기하지 않는다 — DESIGN §5.)"""
    records_dir = m.out_dir.parent          # <프로젝트>/04_records
    project_dir = records_dir.parent        # <프로젝트>
    pid = prev_session_id(m.session_id)
    if pid:
        rep = records_dir / f"{pid}_analysis" / "report.md"
        if rep.is_file():
            acts = _parse_actions(rep.read_text(encoding="utf-8"), r"^##\s*B7")
            confirmed = [a for a in acts if a["confirmed"]]
            return {"source": "report", "path": str(rep),
                    "actions": confirmed or acts, "prev_date": None}
        legacy = project_dir / "05_retro" / f"{pid}_회고.md"
        if legacy.is_file():
            acts = _parse_actions(legacy.read_text(encoding="utf-8"), r"^##\s*6")
            return {"source": "legacy", "path": str(legacy), "actions": acts, "prev_date": None}
    # prev_date는 계약상 슬롯이다(s70 신선도 배너는 _growth/trend_metrics.csv의 날짜를 쓴다).
    return {"source": "none", "path": None, "actions": [], "prev_date": None}
