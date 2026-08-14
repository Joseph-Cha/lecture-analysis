"""manifest.toml 로더 — 입력 계약(DESIGN §5)의 단일 구현.
경로는 manifest 파일 위치 기준 상대 해석. 잘못된 참조는 로드 시점에 즉시 실패."""
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from .clova import parse_ts, fmt_ts
from .naming import slugify, compact_date

@dataclass
class Period:
    id: str
    clova: Path | None
    audio: Path | None

@dataclass
class Manifest:
    path: Path
    out_dir: Path
    project: str
    session_id: str
    date: str
    prev: str
    plan: Path | None
    periods: list
    instructor_label: str
    mask_names: list
    bounds: dict = field(default_factory=dict)
    name: str = ""                                 # [session] name — 배포판 강의명

    # ── 산출물 경로 관문(스펙 §4) — 파생 로직이 두 곳이면 s80이 옛 파일을 읽는 사고가 난다
    @property
    def artifacts_dir(self) -> Path:
        return self.out_dir / "artifacts"

    @property
    def outputs_dir(self) -> Path:
        return self.out_dir / "outputs"

    @property
    def label(self) -> str:
        """파일명용 강의 라벨. 레거시 manifest(name 없음)는 project→session_id로 강등."""
        return slugify(self.name) or slugify(self.project) or slugify(self.session_id) or "강의"

    @property
    def report_md_path(self) -> Path:
        return self.outputs_dir / f"{self.label}_분석데이터_{compact_date(self.date)}.md"

    @property
    def report_html_path(self) -> Path:
        return self.outputs_dir / f"{self.label}_분석리포트_{compact_date(self.date)}.html"

def _resolve(base: Path, rel: str | None) -> Path | None:
    if not rel:
        return None
    p = (base / rel).resolve()
    if not p.is_file():
        raise ValueError(f"참조 파일 없음: {rel} → {p}")
    return p

def _ts(pid: str, field: str, v) -> int:
    """bounds 시각 1건 파싱. 문자열이 아니면 ValueError.

    TOML에서 따옴표를 빠뜨리면 "05:13"이 정수 513으로 읽히고 parse_ts가 AttributeError로
    터진다 — 스테이지 래퍼는 ValueError/OSError만 잡으므로 종료 코드 규약(입력 오류 = 2)이
    깨지고 트레이스백 + exit 1로 샌다."""
    if not isinstance(v, str):
        raise ValueError(f'[bounds] {pid}.{field}: "MM:SS"/"H:MM:SS" 형식의 문자열이어야 함 '
                         f"(따옴표 확인) — 받은 값: {v!r}")
    return parse_ts(v)

def check_span(where: str, start: int, end: int, breaks: list) -> None:
    """측정 구간 산술 불변식. bounds 출처(manifest 수동값·LLM 제안)와 무관하게 같은 규칙이다.

    s50이 (end-start) - Σ(break 길이)로 측정 시간을 구한다. 뒤집힌 break는 길이가 음수라
    측정 시간을 부풀리고(시간당 지표를 낮추고), 겹친 break는 같은 시간을 두 번 깎고,
    구간 밖 break는 있지도 않은 휴식을 깎는다. 셋 다 지표만 보고는 알 수 없는 왜곡이라
    소비 전에 막는다."""
    if start >= end:
        raise ValueError(f"{where}: start < end 여야 함 — {fmt_ts(start)}~{fmt_ts(end)}")
    for i, (s, e) in enumerate(breaks, 1):
        if not (start <= s < e <= end):
            raise ValueError(f"{where}: 휴식 {i}({fmt_ts(s)}~{fmt_ts(e)})은 측정 구간 "
                             f"{fmt_ts(start)}~{fmt_ts(end)} 안에서 앞→뒤여야 함")
    ordered = sorted(breaks)
    for (s1, e1), (s2, e2) in zip(ordered, ordered[1:]):
        if s2 < e1:
            raise ValueError(f"{where}: 휴식 구간이 겹침 — {fmt_ts(s1)}~{fmt_ts(e1)}, "
                             f"{fmt_ts(s2)}~{fmt_ts(e2)} (겹친 시간을 두 번 뺀다)")

def _parse_bounds(pid: str, b: dict) -> dict:
    """측정 구간 1건 파싱(문자열 시각 → 초) + 불변식 검증."""
    start, end = _ts(pid, "start", b["start"]), _ts(pid, "end", b["end"])
    breaks = []
    for i, pair in enumerate(b.get("breaks", []), 1):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError(f'[bounds] {pid}.breaks[{i}]: ["시작", "끝"] 2원소여야 함 — 받은 값: {pair!r}')
        breaks.append((_ts(pid, f"breaks[{i}][0]", pair[0]), _ts(pid, f"breaks[{i}][1]", pair[1])))
    check_span(f"[bounds] {pid}", start, end, breaks)
    return {"start": start, "end": end, "breaks": breaks}

def load_manifest(path) -> Manifest:
    path = Path(path).resolve()
    base = path.parent
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    sess = raw.get("session", {})
    periods = []
    for p in raw.get("period", []):
        # dict가 아니면 '[[period]]'를 '[period]'로 쓴 것 — 키 문자열을 순회하게 된다.
        if not isinstance(p, dict) or "id" not in p:
            raise ValueError(f"[[period]] 항목 형식 오류(id 필수, 배열 표기 [[period]] 확인): {p!r}")
        per = Period(p["id"], _resolve(base, p.get("clova")), _resolve(base, p.get("audio")))
        if per.clova is None and per.audio is None:
            raise ValueError(f"{per.id}: clova·audio 중 하나는 필수")
        periods.append(per)
    if not periods:
        raise ValueError("교시([[period]]) 0개")
    spk = raw.get("speakers", {})
    # 문자열을 그대로 list()에 넣으면 글자 단위로 쪼개져("차동훈" → ['차','동','훈'])
    # 하류 마스킹이 전사 전체의 낱글자를 치환한다 — 반드시 리스트 표기여야 한다.
    names = spk.get("mask_names", [])
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError(f"[speakers] mask_names: 문자열 리스트여야 함(예: [\"차동훈\"]) — 받은 값: {names!r}")
    bounds = {}
    known = {p.id for p in periods}
    # 산출물 파일명이 교시 id로 정해지므로(artifacts/parsed/pNN.json) 중복 id는 덮어쓰기 = 조용한 누락.
    if len(known) != len(periods):
        raise ValueError(f"교시 id 중복: {[p.id for p in periods]}")
    for pid, b in raw.get("bounds", {}).items():
        # 교시 id 오타를 조용히 무시하면 수동 구간이 사라지고 30단계 LLM 제안으로
        # 대체돼, 사용자가 지정한 것과 다른 구간으로 지표가 계산된다.
        if pid not in known:
            raise ValueError(f"[bounds] {pid}: 정의되지 않은 교시 id (정의된 교시: {sorted(known)})")
        if not isinstance(b, dict) or "start" not in b or "end" not in b:
            raise ValueError(f"[bounds] {pid}: start·end 필수")
        bounds[pid] = _parse_bounds(pid, b)
    return Manifest(
        path=path, out_dir=base,
        project=str(sess.get("project", "")), session_id=str(sess.get("id", "")),
        date=str(sess.get("date", "")), prev=str(sess.get("prev", "auto")),
        plan=_resolve(base, sess.get("plan")),
        periods=periods,
        instructor_label=str(spk.get("instructor_label", "auto")),
        mask_names=list(names),
        bounds=bounds,
        name=str(sess.get("name", "")),
    )
