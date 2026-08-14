"""리포트 파생 집계 — report.md(s70)와 report.html(s80)이 같은 숫자를 쓰게 하는 층.

브리프 3-B의 SSOT 규칙("차트에만 있는 숫자 금지")의 구현 지점이다: HTML 차트가 그리는
값과 md 표에 적히는 값을 각자 계산하면 언젠가 갈라지고, 그때 어느 쪽이 맞는지 알 방법이
없다. 그래서 두 렌더러는 여기 함수의 반환값만 소비한다.

여기는 '집계'만 있다 — 지표의 정의(무엇을 셀 것인가)는 references/metrics.md와 s50이고,
여기는 이미 측정된 값을 표·차트용으로 접는 산술뿐이다.
"""
from decimal import Decimal, ROUND_HALF_UP

# 5분 창 말속도에서 '실제 발화'로 보지 않는 하한(음절/분). 이 미만은 침묵·개별 실습 구간의
# 파편 발화라 실제 말속도 축에 섞으면 평균·최저가 왜곡된다(브리프 P2-2의 '인위적 하한값 18.0').
LOW_SPM = 50

# 활동 타임라인 라벨의 고정 순서 — s40 프롬프트의 라벨 집합과 같다. 순서가 고정이어야
# md 표의 열과 HTML 스택 바의 색이 회차가 바뀌어도 같은 뜻을 가리킨다.
ACTIVITY_ORDER = ["이론", "시연", "실습", "운영", "중단"]
SPARK_BARS = "▁▂▃▄▅▆▇█"


def pct_str(v) -> str:
    """비율(0~1) → 캐논 % 표기. 전 섹션 단일 규칙: 소수 1자리·반올림(half-up).

    브리프 P1-3: 97%(0자리)와 96.7%(1자리)가 한 리포트에 공존하면 독자는 어느 쪽이
    반올림인지 알 수 없다. half-up인 이유: 파이썬 기본 float 포맷은 96.65가 이진 오차로
    96.6이 되는데, 그러면 LLM 산문이 이미 쓴 96.7과 영영 어긋난다."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return "미상"
    return f"{Decimal(str(v * 100)).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"


def normalize_share_pcts(text: str, share: dict) -> tuple[str, int]:
    """산문 속 발화 비율 % 표기를 캐논 표기로 교체 — (교체된 텍스트, 교체 수).

    s65(LLM)는 metrics.json의 원값(0.9665)을 자유 표기(96.7%·3.35%·97%)로 산문에 싣는다.
    LLM을 다시 부르지 않고 표기만 통일하는 결정론 후처리다. **인용(quote) 필드에는 절대
    적용하지 않는다** — 인용 원문 보존이 검증 가능성의 전제다(브리프 §5).

    교체 대상은 발화 비율 값 근방(±0.5%p)의 % 토큰뿐이다: '80%'·'50%' 같은 다른 수치를
    건드리면 정규화가 아니라 왜곡이 된다."""
    import re
    canon = {}
    for key in ("instructor", "students"):
        v = share.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            canon[round(v * 100, 2)] = pct_str(v)
    if not canon:
        return text, 0
    n = 0

    def sub(m):
        nonlocal n
        val = float(m.group(1))
        for raw, formatted in canon.items():
            if abs(val - raw) <= 0.5 and m.group(0) != formatted:
                n += 1
                return formatted
        return m.group(0)

    return re.sub(r"(\d{1,3}(?:\.\d{1,2})?)%", sub, text), n


def sparkline(vals: list) -> str:
    """수치 목록 → 유니코드 스파크라인. None(무발화 창)은 '·'로 구분해 그린다.

    데이터 레이어(md)에서 텍스트 diff만으로 추세 모양이 보이게 하는 장치다(브리프 2-5)."""
    nums = [v for v in vals if isinstance(v, (int, float))]
    top = max(nums) if nums else 0
    out = []
    for v in vals:
        if not isinstance(v, (int, float)):
            out.append("·")
        elif top <= 0:
            out.append(SPARK_BARS[0])
        else:
            idx = min(len(SPARK_BARS) - 1, int(v / top * (len(SPARK_BARS) - 1) + 0.5))
            out.append(SPARK_BARS[idx])
    return "".join(out)


def _minutes(seg: dict) -> float:
    """타임라인 세그먼트 길이(분). 시각 파싱 실패·역전은 0 — 집계가 발행을 죽이지 않는다."""
    from .clova import parse_ts
    try:
        sec = parse_ts(str(seg.get("end_ts"))) - parse_ts(str(seg.get("start_ts")))
    except (ValueError, AttributeError):
        return 0.0
    return max(0.0, sec / 60)


def time_allocation(struct: dict) -> dict:
    """B4 배분 요약·시간 배분 차트의 단일 원천.

    반환: {"order": [활동…], "per": {pid: {활동: 분}}, "total": {활동: 분}, "grand": 분}
    타임라인에 없는 라벨은 '기타'로 접는다 — 라벨 집합이 늘어도 표가 깨지지 않게."""
    per, extra = {}, False
    for pid, d in struct.items():
        acc = {}
        for seg in d.get("timeline") or []:
            label = seg.get("label") if seg.get("label") in ACTIVITY_ORDER else "기타"
            extra = extra or label == "기타"
            acc[label] = acc.get(label, 0.0) + _minutes(seg)
        if acc:
            per[pid] = {k: round(v, 1) for k, v in acc.items()}
    order = ACTIVITY_ORDER + (["기타"] if extra else [])
    total = {lab: round(sum(p.get(lab, 0.0) for p in per.values()), 1) for lab in order}
    return {"order": order, "per": per, "total": total,
            "grand": round(sum(total.values()), 1)}


def question_stats(struct: dict) -> dict:
    """B3 집계·질문 차트의 단일 원천.

    반환: {"kinds": [{"kind", "answered", "unanswered"}…], "answered", "total", "uptake_n"}
    유형 순서는 건수 내림차순(동수는 이름순) — LLM이 주는 유형 집합이 회차마다 달라도
    결정론 순서를 유지한다."""
    acc = {}
    uptake_n = 0
    for d in struct.values():
        for q in d.get("questions") or []:
            kind = str(q.get("kind") or "미분류")
            a = acc.setdefault(kind, {"answered": 0, "unanswered": 0})
            a["answered" if q.get("answered") else "unanswered"] += 1
        uptake_n += len(d.get("uptake") or [])
    kinds = [{"kind": k, **v} for k, v in acc.items()]
    kinds.sort(key=lambda r: (-(r["answered"] + r["unanswered"]), r["kind"]))
    return {"kinds": kinds,
            "answered": sum(r["answered"] for r in kinds),
            "total": sum(r["answered"] + r["unanswered"] for r in kinds),
            "uptake_n": uptake_n}


def speech_obs_stats(struct: dict) -> dict:
    """B8 집계·화법 관찰 차트의 단일 원천.

    반환: {"kinds": [종류…], "per": {pid: {종류: n}}, "total": {종류: n}, "grand": n}"""
    per, totals = {}, {}
    for pid, d in struct.items():
        for o in d.get("speech_observations") or []:
            kind = str(o.get("kind") or "미분류")
            per.setdefault(pid, {})[kind] = per.get(pid, {}).get(kind, 0) + 1
            totals[kind] = totals.get(kind, 0) + 1
    kinds = sorted(totals, key=lambda k: (-totals[k], k))
    return {"kinds": kinds, "per": per, "total": totals,
            "grand": sum(totals.values())}


def speed_summary(audio: dict) -> list:
    """B2 교시별 말속도 요약·말속도 차트의 단일 원천.

    행: {"period", "windows", "avg", "max", "min_valid", "low_n", "spark", "spms"}
    평균·최고·최저는 유효 창(LOW_SPM 이상)만으로 계산한다 — 18.0 같은 파편 값이 실제
    말속도 통계에 섞이는 것이 브리프 P2-2가 지적한 왜곡이다. low_n(저속·무발화 창 수)이
    그 빠진 창들의 행방을 밝힌다."""
    rows, by_pid = [], {}
    for w in (audio or {}).get("speech_rate_windows") or []:
        pid, _, label = str(w.get("label", "")).partition(" ")
        by_pid.setdefault(pid, []).append({"label": label, "spm": w.get("spm")})
    for pid, wins in by_pid.items():
        spms = [w["spm"] for w in wins]
        valid = [v for v in spms if isinstance(v, (int, float)) and v >= LOW_SPM]
        rows.append({
            "period": pid, "windows": len(wins),
            "avg": round(sum(valid) / len(valid), 1) if valid else None,
            "max": max(valid) if valid else None,
            "min_valid": min(valid) if valid else None,
            "low_n": sum(1 for v in spms
                         if v is None or (isinstance(v, (int, float)) and v < LOW_SPM)),
            "spark": sparkline(spms), "spms": spms})
    return rows


def wait_stats(audio: dict) -> dict:
    """B2 대기 시간 목록·중앙값. 구형 metrics.json(1.0.x — wait_median_sec 없음)도 계산해
    돌려준다 — 다음 액션 판정 기준('gap 중앙값')의 이번 회차 기준값이라 빠지면 안 된다."""
    waits = (audio or {}).get("wait_times") or []
    median = (audio or {}).get("wait_median_sec")
    if median is None and waits:
        import statistics
        median = round(statistics.median(w.get("gap_sec", 0) for w in waits), 1)
    return {"items": waits, "median": median}
