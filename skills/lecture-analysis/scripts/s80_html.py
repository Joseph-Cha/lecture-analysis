#!/usr/bin/env python3
"""s80: 공유용 단일 파일 report.html 발행 + 내부 링크 검증 (LLM 없음).

브리프 3장(산출물 문서 포맷)의 구현: 읽기용 HTML(차트·지표 카드·접이식 부록·인쇄 CSS)과
데이터용 md/CSV/JSON의 이중 구조에서 '읽기용' 쪽이다. 기준 구조·품질은 2026-08-11 수제
report.html(루트)을 따른다.

- **오프라인 완결**: Chart.js v4.4.1·CSS를 templates/html/에서 읽어 전부 인라인한다.
  CDN 참조 금지 — 고객사 보안망에서 외부 다운로드가 차단된 실사례(s03 트러블)가 근거다.
- **SSOT**: 차트가 그리는 수치는 전부 lib/aggregate + 상류 산출물에서 온다. 같은 값이
  분석데이터 md 표와 report_data.json에도 실린다 — 차트에만 있는 숫자 금지(브리프 3-B).
- **내부 경로 비노출(P3-3)**: 실행 메타·절대 경로를 싣지 않는다. 사이드카는 분석데이터 md C3
  (파일명은 manifest 파생 — 본문·꼬리말 모두 m.report_md_path에서만 가져온다).
- **앵커 검증(P3-4)**: 발행 직전 모든 내부 링크의 목적지 존재를 검사 — 위반 시 발행 실패.
"""
import argparse
import html as html_mod
import json, pathlib, re, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import s70_report as s70
from lib.aggregate import (LOW_SPM, pct_str, question_stats, speech_obs_stats,
                           speed_summary, time_allocation, wait_stats)
from lib.prev import _FIELD_SEP
from lib.clova import fmt_ts
from lib.manifest import load_manifest
from lib.stage import PIPELINE_VERSION, load_analysis, require_json, run, stage_guard

TPL = pathlib.Path(__file__).resolve().parent.parent / "templates" / "html"
# 스택 바 공용 팔레트(기준 HTML의 시간 배분 색). 라벨 집합이 늘면 뒤 색을 순환한다.
ACTIVITY_COLORS = {"이론": "#B4B2A9", "시연": "#2A78D6", "실습": "#0F6E56",
                   "운영": "#E3E2DB", "중단": "#B4451F", "기타": "#8A6A12"}
KIND_PALETTE = ["#B4451F", "#EF9F27", "#B4B2A9", "#2A78D6", "#0F6E56", "#8A6A12"]

def esc(v) -> str:
    return html_mod.escape(s70._q(v), quote=True)

def esc_cut(v, limit: int) -> str:
    return html_mod.escape(s70._q(v, limit), quote=True)

def ts_of(v) -> str:
    return html_mod.escape(s70._ts(v), quote=True)

def tc(pid, ts) -> str:
    """타임코드 알약. 시각 미상은 경고 스타일로 구분한다(빈 괄호 출력 금지 — P1-2)."""
    label = s70._ts(ts)
    cls = "tc miss" if label == "시각 미상" else "tc"
    return f'<span class="{cls}">{html_mod.escape(str(pid))} {html_mod.escape(label)}</span>'

def src_link(anchors: set, aid: str, label: str = "원문↩") -> str:
    return f'<a class="src" href="#{aid}">{label}</a>' if aid in anchors else ""

def validate(html_str: str, md_str: str) -> None:
    """내부 링크 무결성 — 깨진 앵커가 하나라도 있으면 발행하지 않는다(브리프 P3-4).

    md도 함께 검사한다: 두 산출물은 같은 앵커 체계(#b1·#sem-…)를 쓰므로 한쪽만 성하면
    인용 링크 호환(브리프 3-A)이 깨진 채 배포된다."""
    broken = set(re.findall(r'href="#([^"]+)"', html_str)) \
        - set(re.findall(r'id="([^"]+)"', html_str))
    if broken:
        raise ValueError(f"report.html 깨진 내부 링크 {len(broken)}건: "
                         f"{sorted(broken)[:5]} — 발행 중단")
    md_broken = set(re.findall(r"\]\(#([^)]+)\)", md_str)) \
        - set(re.findall(r'<a id="([^"]+)"></a>', md_str))
    if md_broken:
        raise ValueError(f"report.md 깨진 내부 링크 {len(md_broken)}건: "
                         f"{sorted(md_broken)[:5]} — 발행 중단")

def no_internal_paths(html_str: str, m) -> None:
    """공유용 HTML에 로컬 절대 경로가 새면 안 된다(브리프 P3-3) — 자기 검증.

    세션 폴더(out_dir)뿐 아니라 입력·계획 파일의 폴더도 검사한다: 배포판 manifest는 녹취를
    세션 폴더 밖 절대 경로로 가리키므로(스펙 §4), out_dir만 보던 규칙으로는 사용자 데이터
    폴더 구조가 새는 것을 못 잡는다. $HOME은 그 둘 밖의 경로까지 받는 상위 그물이다."""
    dirs = {pathlib.Path.home(), m.out_dir} | {p.parent for per in m.periods
                                               for p in (per.clova, per.audio) if p is not None}
    if m.plan is not None:
        dirs.add(m.plan.parent)
    # 원형과 resolve() 결과를 둘 다 본다 — 심볼릭 링크가 걸린 홈·임시 폴더에서는 두 표기가
    # 다르고, 렌더에 새는 쪽이 어느 쪽인지 미리 알 수 없다. sorted는 오류 문구 결정론.
    needles = {s for d in dirs for s in (str(d), str(pathlib.Path(d).resolve()))}
    for needle in sorted(needles):
        if needle and needle in html_str:
            raise ValueError(f"report.html에 내부 경로 노출: {needle} — 발행 중단")

def chart_js(data: dict) -> str:
    """기준 HTML의 차트 설정 스크립트를 데이터 주입형으로 재현한다.

    로직(색·축·툴팁)은 고정, 수치는 전부 DATA에서 — 회차가 바뀌어도 코드는 같고
    데이터만 바뀐다. 존재하지 않는 canvas는 각 블록이 스스로 건너뛴다(저하 모드)."""
    return f"""
const DATA={json.dumps(data, ensure_ascii=False)};
const INK="#1D1E22", MUTE="#6E6F76", FAINT="#9A9B9F", LINE="rgba(110,111,118,.14)";
Chart.defaults.font.family=getComputedStyle(document.body).fontFamily;
Chart.defaults.font.size=11.5;
Chart.defaults.color=MUTE;
function el(id){{return document.getElementById(id);}}

/* ── 말속도 라인 (무발화 창 null = 선 끊김·저속 회색 점) ── */
if(el("speedChart")&&DATA.speed.segs.length){{
 const sl=[],sd=[],pc=[],pr=[];
 DATA.speed.segs.forEach(([p,vs])=>{{vs.forEach((v,i)=>{{sl.push(i===0?p:"");sd.push(v);
  const low=v!==null&&v<{LOW_SPM};pc.push(low?FAINT:"#2A78D6");pr.push(low?3.5:2);}});}});
 new Chart(el("speedChart"),{{type:"line",
  data:{{labels:sl,datasets:[{{data:sd,borderColor:"#2A78D6",borderWidth:2,spanGaps:false,
   pointBackgroundColor:pc,pointBorderColor:pc,pointRadius:pr,tension:.25}}]}},
  options:{{responsive:true,maintainAspectRatio:false,
   plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:c=>c.parsed.y===null?"무발화":Math.round(c.parsed.y)+" 음절/분"}}}}}},
   scales:{{x:{{ticks:{{autoSkip:false,maxRotation:0,callback:(v,i)=>sl[i]||null}},grid:{{display:false}}}},
           y:{{min:0,grid:{{color:LINE}},ticks:{{callback:v=>Math.round(v)}}}}}}}},
  plugins:[{{id:"avg",afterDraw(ch){{const a=ch.chartArea,s=ch.scales,x=ch.ctx;
   if(DATA.speed.avg===null)return;const y=s.y.getPixelForValue(DATA.speed.avg);
   x.save();x.strokeStyle="#B4451F";x.setLineDash([6,4]);x.lineWidth=1.6;
   x.beginPath();x.moveTo(a.left,y);x.lineTo(a.right,y);x.stroke();x.restore();}}}}]}});
}}

/* ── 침묵 상위 ── */
if(el("silenceChart")&&DATA.silence.labels.length){{
 new Chart(el("silenceChart"),{{type:"bar",
  data:{{labels:DATA.silence.labels,
   datasets:[{{data:DATA.silence.durs,backgroundColor:"#9A9B9F",
    borderRadius:{{topRight:4,bottomRight:4}},barThickness:20}}]}},
  options:{{indexAxis:"y",responsive:true,maintainAspectRatio:false,
   plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:c=>c.parsed.x+"초 ("+(c.parsed.x/60).toFixed(1)+"분)"}}}}}},
   scales:{{x:{{grid:{{color:LINE}},ticks:{{callback:v=>v+"s"}}}},
           y:{{grid:{{display:false}},ticks:{{font:{{family:"SF Mono,Menlo,monospace",size:11}}}}}}}}}}}});
}}

/* ── 질문 응답/무응답 (17건 규모 — 비율화 대신 건수) ── */
if(el("qChart")&&DATA.q.labels.length){{
 new Chart(el("qChart"),{{type:"bar",
  data:{{labels:DATA.q.labels,
   datasets:[{{label:"응답 관측",data:DATA.q.answered,backgroundColor:"#0F6E56",barThickness:22}},
             {{label:"무응답",data:DATA.q.unanswered,backgroundColor:"#D5D4CB",
              borderRadius:{{topRight:4,bottomRight:4}},barThickness:22}}]}},
  options:{{indexAxis:"y",responsive:true,maintainAspectRatio:false,
   plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:c=>c.dataset.label+" "+c.parsed.x+"건"}}}}}},
   scales:{{x:{{stacked:true,grid:{{color:LINE}},ticks:{{stepSize:2}}}},
           y:{{stacked:true,grid:{{display:false}}}}}}}}}});
}}

/* ── 시간 배분 스택 ── */
if(el("timeChart")&&DATA.time.periods.length){{
 new Chart(el("timeChart"),{{type:"bar",
  data:{{labels:DATA.time.periods,
   datasets:DATA.time.order.map(t=>({{label:t,data:DATA.time.periods.map(p=>(DATA.time.per[p]||{{}})[t]||0),
    backgroundColor:DATA.time.colors[t],barThickness:34}}))}},
  options:{{responsive:true,maintainAspectRatio:false,
   plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:c=>c.dataset.label+" "+c.parsed.y.toFixed(1)+"분"}}}}}},
   scales:{{x:{{stacked:true,grid:{{display:false}},ticks:{{font:{{family:"SF Mono,Menlo,monospace"}}}}}},
           y:{{stacked:true,grid:{{color:LINE}},ticks:{{callback:v=>v+"분"}}}}}}}}}});
}}

/* ── 화법 관찰 스택 ── */
if(el("speechChart")&&DATA.obs.periods.length){{
 new Chart(el("speechChart"),{{type:"bar",
  data:{{labels:DATA.obs.periods,
   datasets:DATA.obs.kinds.map((k,i)=>({{label:k,data:DATA.obs.periods.map(p=>(DATA.obs.per[p]||{{}})[k]||0),
    backgroundColor:DATA.obs.colors[i],barThickness:30}}))}},
  options:{{responsive:true,maintainAspectRatio:false,
   plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:c=>c.dataset.label+" "+c.parsed.y+"건"}}}}}},
   scales:{{x:{{stacked:true,grid:{{display:false}},ticks:{{font:{{family:"SF Mono,Menlo,monospace"}}}}}},
           y:{{stacked:true,grid:{{color:LINE}},ticks:{{stepSize:2}}}}}}}}}});
}}

/* ── 전달 지표 추세 (2회차부터 — 첫 회차는 기준값 카드) ── */
if(el("trendChart")&&DATA.trend.sessions.length>1){{
 new Chart(el("trendChart"),{{type:"line",
  data:{{labels:DATA.trend.sessions,datasets:[
   {{label:"헤징/시간당",data:DATA.trend.hedging_per_h,borderColor:"#2A78D6",borderWidth:2,tension:.2}},
   {{label:"사과(협의)",data:DATA.trend.apology,borderColor:"#B4451F",borderWidth:2,tension:.2}}]}},
  options:{{responsive:true,maintainAspectRatio:false,
   plugins:{{legend:{{display:true,position:"bottom"}}}},
   scales:{{y:{{min:0,grid:{{color:LINE}}}},x:{{grid:{{display:false}}}}}}}}}});
}}

/* ── 인쇄 시 접힘 섹션 펼치기 ── */
let wasOpen=[];
window.addEventListener("beforeprint",()=>{{wasOpen=[...document.querySelectorAll("details")].map(d=>d.open);
  document.querySelectorAll("details").forEach(d=>d.open=true);}});
window.addEventListener("afterprint",()=>{{[...document.querySelectorAll("details")].forEach((d,i)=>d.open=wasOpen[i]);}});
"""

def build(ctx: dict) -> tuple[str, dict]:
    """(html, report_data) — report_data는 차트 원천 수치의 JSON 내보내기(SSOT 검증용)."""
    m, metrics = ctx["manifest"], ctx["metrics"]
    sem, struct = ctx["sem"], ctx["struct"]
    goal, review = ctx["goal"], ctx["review"]
    s, t = ctx["summary"], (metrics.get("text") or {})
    au = metrics.get("audio") or {}
    prev_rows = ctx["prev_rows"]
    anchors = {aid for aid, *_ in ctx["evidence"]}
    confirmed = ctx.get("confirmed_actions") or set()
    # 데이터 레이어(사이드카) 파일명은 manifest 파생이라 본문에서도 하드코딩하지 않는다.
    # 옛 이름 `report.md`를 굳혀 두면 공유받은 사람이 outputs/에 없는 파일을 찾게 되고,
    # 강의명·날짜가 바뀔 때마다 캡션만 조용히 어긋난다(파생은 한 곳에서만 — 스펙 §4).
    md_name = esc(m.report_md_path.name)
    hours = metrics.get("bounds_hours")
    hours_s = f"{hours:.2f}h" if isinstance(hours, (int, float)) else "미상"
    qs, ta, ob = question_stats(struct), time_allocation(struct), speech_obs_stats(struct)
    speed_rows, wsx = speed_summary(au), wait_stats(au)
    share = t.get("speaker_share") or {}
    review_failed = "llm_failed" in review
    good, bad = review.get("good") or [], review.get("bad") or []
    cands = review.get("action_candidates") or []
    kind_colors = [KIND_PALETTE[i % len(KIND_PALETTE)] for i in range(len(ob["kinds"]))]

    data = {
        "speed": {"segs": [[r["period"], r["spms"]] for r in speed_rows],
                  "avg": au.get("speech_rate_spm") if au else None},
        "silence": {"labels": [f"{si.get('period', '')} {fmt_ts(s70._int(si.get('start')))}"
                               for si in au.get("silence_top") or []],
                    "durs": [si.get("dur") for si in au.get("silence_top") or []]},
        "q": {"labels": [f"{r['kind']} ({r['answered'] + r['unanswered']})" for r in qs["kinds"]],
              "answered": [r["answered"] for r in qs["kinds"]],
              "unanswered": [r["unanswered"] for r in qs["kinds"]]},
        "time": {"periods": list(ta["per"]), "order": ta["order"], "per": ta["per"],
                 "colors": {lab: ACTIVITY_COLORS.get(lab, "#8A6A12") for lab in ta["order"]}},
        "obs": {"periods": sorted(ob["per"]), "kinds": ob["kinds"], "per": ob["per"],
                "colors": kind_colors},
        "trend": {"sessions": [r.get("session", "") for r in prev_rows] + [m.session_id],
                  "hedging_per_h": [_f(r.get("hedging_per_h")) for r in prev_rows]
                  + [t.get("hedging_per_h")],
                  "apology": [_f(r.get("apology_n")) for r in prev_rows] + [s["apology_n"]]},
        "wait": {"items": wsx["items"], "median_sec": wsx["median"]},
    }

    H = []
    W = H.append
    style = (TPL / "style.css").read_text(encoding="utf-8")
    chartjs = (TPL / "chart.umd.min.js").read_text(encoding="utf-8")
    # 표기용 강의명은 label 하나뿐이다 — 배포판 뼈대 manifest에는 project·id가 없어서
    # 옛 표기(session_id — project)는 제목·꼬리말이 통째로 빈 칸이 됐다(md 제목과 같은 규칙).
    title = esc(f"{m.label} 강의 분석 리포트")
    W('<!DOCTYPE html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">')
    W('<meta name="viewport" content="width=device-width, initial-scale=1">')
    W(f"<title>{title}</title>")
    W(f"<style>\n{style}\n</style>\n</head>\n<body>")

    # ── masthead + TOC ──
    first = not prev_rows
    badge = ('<span class="badge base">첫 측정 — 이번 회차가 기준선</span>' if first
             else f'<span class="badge">직전 {esc(prev_rows[-1].get("session", ""))} 대비 추세는 A·B1</span>')
    W('<header class="mast">')
    W(f'  <div class="kicker">Lecture analysis · pipeline v{PIPELINE_VERSION}</div>')
    W(f"  <h1>{esc(m.label)} 강의 분석 리포트</h1>")
    # 부제에서 project를 뺀 이유: 강의명은 바로 위 h1이 이미 말했고, 뼈대 manifest에서는
    # 빈 <b></b>만 남아 유령 강조가 됐다(레거시 세션도 h1에 같은 강의명이 선다).
    W(f'  <div class="sub">강의일 {esc(m.date)} '
      f"&nbsp;·&nbsp; 측정 구간 {hours_s}({len(m.periods)}교시) &nbsp;·&nbsp; {badge}</div>")
    W("</header>")
    W('<nav class="toc"><div class="in">')
    W('  <a href="#a">A 요약</a><a href="#b1">B1 전달 지표</a><a href="#b2">B2 음성</a>'
      '<a href="#b3">B3 질문·참여</a><a href="#b4">B4 시간 배분</a><a href="#b5">B5 트러블</a>'
      '<a href="#b6">B6 평가</a><a href="#b7">B7 다음 액션</a><a href="#b8">B8 화법 관찰</a>'
      '<a href="#c">C 증거·정의</a>')
    W("</div></nav>\n<main>")

    # ── A ──
    W('<section id="a">')
    note = ("모든 수치는 첫 측정 기준선 — 비교는 2회차부터"
            if first else "추세는 자기 과거 회차와만 비교 — 좋고 나쁨의 판정이 아니에요")
    W(f'  <div class="shead"><span class="idx">A</span><h2>한눈 요약</h2>'
      f'<span class="note">{note}</span></div>')
    W('  <p class="desc">이 리포트는 평가가 아니라 다음 회차를 위한 복기 노트예요 — '
      "수치는 추세의 재료로, 인용은 그날의 기록으로 읽어 주세요.</p>")
    W('  <div class="cards">')
    apo_ex_n = sum(len((d.get("apologies") or {}).get("excluded") or []) for d in sem.values())
    W(f'    <div class="mcard"><div class="lab">사과(협의 집계)</div>'
      f'<div class="val">{s["apology_n"]}<small>회</small></div>'
      f'<div class="foot">습관성·사전 양해 {apo_ex_n}건 제외</div></div>')
    W(f'    <div class="mcard"><div class="lab">헤징</div>'
      f'<div class="val">{esc(s70._num(t.get("hedging_n")))}<small>문장</small></div>'
      f'<div class="foot">시간당 {esc(s70._num(t.get("hedging_per_h")))} · 추세로 보는 지표</div></div>')
    W(f'    <div class="mcard"><div class="lab">강사 발화 비율</div>'
      f'<div class="val">{pct_str(share.get("instructor")).rstrip("%")}<small>%</small></div>'
      f'<div class="foot">수강생 {pct_str(share.get("students"))}는 단일 마이크 하한</div></div>')
    if qs["total"]:
        W(f'    <div class="mcard"><div class="lab">질문 응답</div>'
          f'<div class="val">{qs["answered"]}<small>/{qs["total"]}건</small></div>'
          f'<div class="foot">uptake {qs["uptake_n"]}건 · 관측 가능 구간 한정</div></div>')
    W(f'    <div class="mcard"><div class="lab">트러블</div>'
      f'<div class="val">{len(s["troubles"])}<small>건 · 약{s["trouble_min"]}분</small></div>'
      f'<div class="foot">진행이 실제 멈춘 장애만 · 리허설 재료</div></div>')
    W(f'    <div class="mcard"><div class="lab">자문자답</div>'
      f'<div class="val">≤{s["selfqa_max"]}<small>건</small></div>'
      f'<div class="foot">단일 마이크 상한값</div></div>')
    W("  </div>")
    verdicts = goal.get("verdicts") or []
    if verdicts:
        W("  <h3>직전 액션 판정</h3><ul class=\"qlist\">")
        for v in verdicts:
            W(f'    <li><span class="tag gray">{esc(v.get("verdict"))}</span>'
              f"<span>{esc_cut(v.get('action'), 80)} — {esc_cut(v.get('reason'), 60)}</span></li>")
        W("  </ul>")
    W("  <h3>하이라이트</h3>")
    if review_failed:
        W(f'  <p class="desc">리뷰 미생성 — <a href="#b6">B6 참조</a></p>')
    for g in good[:2]:
        W(f'  <div class="find"><div class="head"><span class="tag keep">유지</span></div>'
          f'<div class="body">{esc(g.get("point"))}</div>'
          f'<div class="refs"><a class="src" href="#b6">→ B6 상세</a></div></div>')
    for b in bad[:2]:
        W(f'  <div class="find"><div class="head"><span class="tag fix">개선</span></div>'
          f'<div class="body">{esc(b.get("point"))}</div>'
          f'<div class="refs"><a class="src" href="#b6">→ B6 상세</a></div></div>')
    if not first:
        W('  <h3>전달 지표 추세</h3>')
        W('  <div class="chart-card"><div class="cw" style="height:220px">'
          '<canvas id="trendChart" role="img" aria-label="회차별 헤징·사과 추세 라인 차트"></canvas></div>'
          # 경로를 빼고 파일명만 남긴다 — `_growth/`는 LA_GROWTH_DIR opt-in 폴더라
          # 세션 폴더 안에 있지도 않고, 지정한 사람마다 위치가 다르다(스펙 §5).
          '<div class="cap">자기 과거 회차와만 비교 · 원자료는 추세 CSV(trend_metrics.csv)'
          ' · 방향 표시일 뿐 좋고 나쁨의 판정이 아니에요</div></div>')
    W("</section>")

    # ── B1 ──
    W('<section id="b1">')
    W('  <div class="shead"><span class="idx">B1</span><h2>전달 지표 상세</h2></div>')
    text_h = t.get("text_hours")
    denom = f" (분모 {text_h:.2f}h)" if isinstance(text_h, (int, float)) else ""
    W('  <ul class="qlist">')
    W(f'    <li><span class="tag gray">헤징</span><span>{esc(s70._num(t.get("hedging_n")))}문장 · '
      f'시간당 {esc(s70._num(t.get("hedging_per_h")))}{denom} — {esc(s70.GUIDE["hedging"])}</span></li>')
    W(f'    <li><span class="tag gray">필러</span><span>{esc(s70._num(t.get("filler_min_n")))} (하한) — '
      "ASR 정규화로 실제보다 적게 잡힙니다</span></li>")
    W(f'    <li><span class="tag gray">자문자답</span><span>≤{s["selfqa_max"]} — 단일 마이크 상한값, '
      "동일 환경이면 추세 비교는 유효합니다</span></li>")
    W(f'    <li><span class="tag gray">발화 비율</span><span>강사 {pct_str(share.get("instructor"))} · '
      f'수강생 {pct_str(share.get("students"))} — 수강생 쪽은 하한</span></li>')
    W("  </ul>")
    hx = t.get("hedging_examples") or []
    if hx:
        W(f'  <h3>헤징 예시 {len(hx)}건 <span style="font-weight:400;color:var(--mute);font-size:13px">'
          "— 전 구간 고른 표본 · 기계적 카운트(것 같-)의 실제 문장</span></h3>")
        W('  <ul class="qlist">')
        for ex in hx:
            W(f"    <li>{tc(ex.get('period', '?'), ex.get('ts'))}"
              f"<blockquote>{esc_cut(ex.get('sentence'), 90)}</blockquote></li>")
        W("  </ul>")
    counted = [(pid, it) for pid, d in sem.items()
               for it in ((d.get("apologies") or {}).get("counted") or [])]
    excluded = [(pid, it) for pid, d in sem.items()
                for it in ((d.get("apologies") or {}).get("excluded") or [])]
    W(f'  <h3>사과 집계 {s["apology_n"]}건 <span style="font-weight:400;color:var(--mute);'
      'font-size:13px">— 협의 사과만 집계, 제외분은 접이식 목록</span></h3>')
    W('  <ul class="qlist">')
    for pid, it in counted:
        W(f"    <li>{tc(pid, it.get('ts'))}<blockquote>{esc_cut(it.get('quote'), 70)}</blockquote>"
          f"{src_link(anchors, 'sem-' + str(it.get('id')))}</li>")
    W("  </ul>")
    if excluded:
        W(f"  <details><summary>제외 {len(excluded)}건 — 습관성 추임새·사전 양해·자기 정정·"
          '측정 구간 밖</summary><div class="inner"><ul>')
        for pid, it in excluded:
            W(f'    <li><span class="tag warnb">제외</span> {esc(it.get("reason"))} — '
              f"“{esc_cut(it.get('quote'), 70)}” {src_link(anchors, 'sem-' + str(it.get('id')))}</li>")
        W("  </ul></div></details>")
    W("</section>")

    # ── B2 ──
    W('<section id="b2">')
    spm_note = (f"평균 {au.get('speech_rate_spm')} 음절/분" if au else "미측정(오디오 없음)")
    W(f'  <div class="shead"><span class="idx">B2</span><h2>정밀 음성 지표</h2>'
      f'<span class="note">{esc(spm_note)}</span></div>')
    if not au:
        W('  <p class="desc">미측정(오디오 없음) — 텍스트 지표는 B1 참조</p>')
    else:
        if speed_rows:
            W('  <div class="chart-card">')
            W('    <div class="legend">'
              '<span><span class="sw" style="background:#2A78D6"></span>발화 구간 말속도</span>'
              f'<span><span class="sw" style="background:#9A9B9F"></span>저속({LOW_SPM} 미만 — 침묵·실습 추정)</span>'
              '<span><span style="width:18px;border-top:2px dashed #B4451F;display:inline-block"></span>'
              f"세션 평균 {au.get('speech_rate_spm')}</span></div>")
            W('    <div class="cw" style="height:280px"><canvas id="speedChart" role="img" '
              f'aria-label="교시별 5분 구간 말속도 라인 차트, 평균 {au.get("speech_rate_spm")} 음절/분">'
              "</canvas></div>")
            gap_n = sum(r["low_n"] for r in speed_rows)
            W(f'    <div class="cap">단위: 음절/분 · 5분 창 · 측정 {hours_s} · 단일 레코더(강사 지배 가정)'
              f" · {LOW_SPM} 미만 회색 점·선 끊김은 저속·무발화 창(계 {gap_n}개 — 세그먼트 없음 포함)"
              " · 원자료 표는 아래 접이식</div>")
            W("  </div>")
            n_rows = sum(r["windows"] for r in speed_rows)
            W(f"  <details><summary>원자료 — 5분 구간 말속도 전체 표 ({n_rows}행)</summary>"
              '<div class="inner"><table><tr><th>구간</th><th class="n">음절/분</th></tr>')
            for w2 in au.get("speech_rate_windows") or []:
                spm = w2.get("spm")
                val = ("무발화" if spm is None
                       else f"{spm} †" if isinstance(spm, (int, float)) and spm < LOW_SPM else spm)
                W(f"    <tr><td>{esc(w2.get('label'))}</td><td class=\"n\">{val}</td></tr>")
            W(f'  </table><p class="desc" style="margin-top:8px">† = {LOW_SPM} 음절/분 미만'
              "(침묵·개별 실습 추정) · 무발화 = whisper 세그먼트 없음</p></div></details>")
        if au.get("silence_top"):
            W("  <h3>침묵 상위 구간</h3>")
            W('  <div class="chart-card">')
            W('    <div class="cw" style="height:230px"><canvas id="silenceChart" role="img" '
              'aria-label="침묵 상위 구간 가로 막대 차트"></canvas></div>')
            wait_s = (f"대기 시간 근사 {len(wsx['items'])}건 · gap 중앙값 {wsx['median']}초"
                      if wsx["items"] else "대기 시간 근사 0건")
            W(f'    <div class="cap">3초 이상 무발화 총 {au.get("silence_min")}분/'
              f'{au.get("silence_n")}건 중 상위 {len(au.get("silence_top"))}건 · 실습 순회와 침묵 구분은 '
              f"B4 타임라인 대조 · {wait_s}(실측값은 아래)</div>")
            W("  </div>")
        if wsx["items"]:
            W(f'  <h3>질문 후 대기 시간 {len(wsx["items"])}건 <span style="font-weight:400;'
              f'color:var(--mute);font-size:13px">— 중앙값 {wsx["median"]}초 · 구두점 의존 근사 · '
              "다음 회차 판정 기준의 이번 값</span></h3>")
            W('  <ul class="qlist">')
            for wt in wsx["items"]:
                W(f"    <li>{tc(wt.get('period', '?'), fmt_ts(s70._int(wt.get('after_ts'))))}"
                  f"<span>질문 직후 {wt.get('gap_sec', '?')}초</span></li>")
            W("  </ul>")
    W("</section>")

    # ── B3 ──
    W('<section id="b3">')
    W(f'  <div class="shead"><span class="idx">B3</span><h2>질문·참여</h2>'
      f'<span class="note">응답 {qs["answered"]} / {qs["total"]}건</span></div>')
    if qs["total"]:
        W('  <div class="chart-card">')
        W('    <div class="legend"><span><span class="sw" style="background:#0F6E56"></span>응답 관측</span>'
          '<span><span class="sw" style="background:#D5D4CB"></span>무응답</span></div>')
        W('    <div class="cw" style="height:200px"><canvas id="qChart" role="img" '
          'aria-label="질문 유형별 응답, 무응답 건수 가로 누적 막대 차트"></canvas></div>')
        W('    <div class="cap">단일 마이크로 보인 구간만의 기록 — 비율로 읽지 않는 게 안전해요 · '
          "인용 원문에 ASR 오인식이 섞일 수 있어요(보정은 병기로만) · 전체 목록은 아래 접이식</div>")
        W("  </div>")
        W(f"  <details><summary>질문 {qs['total']}건 전체 목록</summary>"
          '<div class="inner"><ul>')
        for pid, d in struct.items():
            for q in d.get("questions") or []:
                mark, cls = (("응답", "keep") if q.get("answered") else ("무응답", "gray"))
                guess = (f' <span class="flag">추정: {esc_cut(q.get("asr_guess"), 30)}</span>'
                         if q.get("asr_guess") else "")
                W(f"    <li>{tc(pid, q.get('ts'))} <span class=\"tag {cls}\">"
                  f"{esc(q.get('kind'))}·{mark}</span> “{esc_cut(q.get('quote'), 70)}”{guess} "
                  f"{src_link(anchors, 'str-' + str(q.get('id')))}</li>")
        W("  </ul>")
        ups = [(pid, u) for pid, d in struct.items() for u in d.get("uptake") or []]
        if ups:
            refs = " · ".join(
                f'<a class="src" href="#str-{u.get("id")}">{esc(pid)} {ts_of(u.get("ts"))}↩</a>'
                if f"str-{u.get('id')}" in anchors else f"{esc(pid)} {ts_of(u.get('ts'))}"
                for pid, u in ups)
            W(f'  <p class="desc" style="margin-top:10px">uptake(수강생 발화를 받아 잇기) 관측 '
              f"{len(ups)}건: {refs}</p>")
        W("  </div></details>")
    else:
        W('  <p class="desc">질문 관측 없음</p>')
    W("</section>")

    # ── B4 ──
    W('<section id="b4">')
    W(f'  <div class="shead"><span class="idx">B4</span><h2>시간 배분·커버리지</h2>'
      f'<span class="note">총 {ta["grand"]:.0f}분 집계</span></div>')
    if ta["per"]:
        W('  <div class="chart-card">')
        legend = "".join(f'<span><span class="sw" style="background:{ACTIVITY_COLORS.get(l, "#8A6A12")}">'
                         f"</span>{esc(l)}</span>" for l in ta["order"])
        W(f'    <div class="legend">{legend}</div>')
        W('    <div class="cw" style="height:280px"><canvas id="timeChart" role="img" '
          'aria-label="교시별 활동 유형 시간 누적 막대 차트"></canvas></div>')
        g = ta["grand"] or 1
        mix = " · ".join(f"{l} {pct_str(ta['total'][l] / g)}" for l in ta["order"])
        W(f'    <div class="cap">전체 비중 — {mix} · B4 타임라인에서 자동 집계 '
          f"· 같은 수치가 {md_name} B4 표에 있음(SSOT)</div>")
        W("  </div>")
        for pid, d in struct.items():
            for c in d.get("coverage") or []:
                W(f'  <p class="desc">계획 대비: {esc_cut(c.get("planned"), 70)} → '
                  f"<b>{esc(c.get('verdict'))}</b> "
                  f"{src_link(anchors, 'str-' + str(c.get('id')), '근거↩')}</p>")
        n_seg = sum(len(d.get("timeline") or []) for d in struct.values())
        W(f"  <details><summary>원자료 — 세그먼트 타임라인 전체 ({n_seg}개)</summary>"
          '<div class="inner"><ul>')
        tag_of = {"실습": "keep", "중단": "fix"}
        for pid, d in struct.items():
            for seg in d.get("timeline") or []:
                cls = tag_of.get(seg.get("label"), "gray")
                W(f'    <li><span class="tc">{esc(pid)} {ts_of(seg.get("start_ts"))}~'
                  f'{ts_of(seg.get("end_ts"))}</span> <span class="tag {cls}">{esc(seg.get("label"))}'
                  f"</span> {esc(seg.get('topic'))}</li>")
        W("  </ul></div></details>")
    else:
        W('  <p class="desc">관측 없음(구조 분석 미가용)</p>')
    W("</section>")

    # ── B5 ──
    W('<section id="b5">')
    W(f'  <div class="shead"><span class="idx">B5</span><h2>트러블 타임라인</h2>'
      f'<span class="note">{len(s["troubles"])}건 · 약 {s["trouble_min"]}분</span></div>')
    W('  <ul class="qlist">')
    for pid, tr in s["troubles"]:
        dur = max(0, s70.parse_any(tr.get("end_ts")) - s70.parse_any(tr.get("start_ts")))
        dur_s = f"(약 {max(1, round(dur / 60))}분) " if dur else ""
        W(f'    <li><span class="tc">{esc(pid)} {ts_of(tr.get("start_ts"))}~{ts_of(tr.get("end_ts"))}'
          f"</span><span><b>{dur_s}</b>{esc(tr.get('summary'))}</span>"
          f"{src_link(anchors, 'sem-' + str(tr.get('id')))}</li>")
    if not s["troubles"]:
        W("    <li><span>트러블 관측 없음</span></li>")
    W("  </ul>")
    W("</section>")

    # ── B6 ──
    W('<section id="b6">')
    W('  <div class="shead"><span class="idx">B6</span><h2>잘된 것 / 아쉬운 것</h2></div>')

    def find_card(item, tag, label):
        refs = []
        for r0 in item.get("evidence") or []:
            aid = s70.anchor(r0)
            if aid in anchors:
                refs.append(f'<a class="src" href="#{aid}">근거↩</a>')
            else:
                refs.append(f'<code style="font-family:var(--mono);font-size:11px">{esc(r0)}</code>')
        cause = (f'<div class="why"><b>원인</b> — {esc(item.get("cause"))}</div>'
                 if item.get("cause") else "")
        principle = (f'<div class="why"><b>원리</b> — {esc(item.get("principle"))}</div>'
                     if item.get("principle") else "")
        W(f'  <div class="find"><div class="head"><span class="tag {tag}">{label}</span></div>'
          f'<div class="body">{esc(item.get("point"))}</div>{cause}{principle}'
          f'<div class="refs">{"".join(refs)}</div></div>')

    if review_failed or (not good and not bad):
        W(f'  <p class="desc">{esc(review.get("llm_failed", review.get("note", "미생성")))}'
          f" — 상세는 {md_name} B6</p>")
    if good:
        W("  <h3>다음에도 그대로 가져갈 것</h3>")
        for g0 in good:
            find_card(g0, "keep", "유지")
    if bad:
        W("  <h3>다음을 위해 다듬을 것</h3>")
        for b0 in bad:
            find_card(b0, "fix", "개선")
    W("</section>")

    # ── B7 ──
    W('<section id="b7">')
    W('  <div class="shead"><span class="idx">B7</span><h2>다음 액션</h2><span class="note">'
      '상태: <span class="tag gray">추천</span> = 파이프라인 제안 · '
      '<span class="tag keep">강사 확정</span> = 강사가 채택 · 화법 목표는 회차당 1개면 충분해요</span></div>')
    if not cands and not confirmed:
        W('  <p class="desc">후보 없음' + (" — 리뷰 미생성" if review_failed else "") + "</p>")
    matched = set()
    for a in cands:
        key = " ".join(s70._q(a.get("action")).split())
        is_conf = key in confirmed
        if is_conf:
            matched.add(key)
        cls, tag, lab = (("act confirmed", "keep", "강사 확정") if is_conf
                         else ("act", "gray", "추천"))
        W(f'  <div class="{cls}"><div class="row1"><span class="tag {tag}">{lab}</span>'
          f'<span class="t">{esc(a.get("action"))}</span></div>')
        W(f"  <dl><dt>적용 시점</dt><dd>{esc(a.get('when'))}</dd>"
          f"<dt>판정 기준</dt><dd>{esc(a.get('criteria'))}</dd>"
          f"<dt>준비</dt><dd>{esc(a.get('prep'))}</dd></dl></div>")
    # 재합성으로 후보 문구가 바뀌어도 강사 확정은 소실되지 않는다(md B7과 같은 보존 규칙).
    carried = ([(k, ln) for k, ln in confirmed.items() if k not in matched]
               if isinstance(confirmed, dict) else [])
    if carried:
        W('  <p class="desc">아래는 직전 발행에서 강사가 확정한 액션 — 이번 후보 목록에는 '
          "없지만 보존됨</p>")
        label_map = {"적용 시점": "적용 시점", "판정 기준": "판정 기준", "전략·도구·준비": "준비"}
        for key, line in carried:
            body = re.sub(r"^- \[[xX]\] \*\*액션\*\*:\s*", "", line)
            parts = _FIELD_SEP.split(body)
            fields = {}
            for seg in parts[1:]:
                fm = re.match(r"^\*\*([^*]+)\*\*\s*:\s*(.*)$", seg.strip(), re.S)
                if fm:
                    fields[fm.group(1).strip()] = fm.group(2).strip()
            W(f'  <div class="act confirmed"><div class="row1">'
              f'<span class="tag keep">강사 확정</span>'
              f'<span class="t">{esc(parts[0].strip())}</span></div>')
            dl = "".join(f"<dt>{esc(label_map.get(k2, k2))}</dt><dd>{esc(v2)}</dd>"
                         for k2, v2 in fields.items())
            if dl:
                W(f"  <dl>{dl}</dl>")
            W("  </div>")
    W("</section>")

    # ── B8 ──
    W('<section id="b8">')
    W(f'  <div class="shead"><span class="idx">B8</span><h2>화법 관찰</h2>'
      f'<span class="note">진단 없음 — 인용 수집 · 분기 추세 재료</span></div>')
    if ob["grand"]:
        W('  <div class="chart-card">')
        legend = "".join(f'<span><span class="sw" style="background:{kind_colors[i]}"></span>'
                         f"{esc(k)}</span>" for i, k in enumerate(ob["kinds"]))
        W(f'    <div class="legend">{legend}</div>')
        W(f'    <div class="cw" style="height:240px"><canvas id="speechChart" role="img" '
          f'aria-label="교시별 화법 관찰 건수 누적 막대 차트, 총 {ob["grand"]}건"></canvas></div>')
        mix = " · ".join(f"{esc(k)} {ob['total'][k]}" for k in ob["kinds"])
        W(f'    <div class="cap">관찰 후보 {ob["grand"]}건의 교시별 분포({mix}) — 진단이 아닌 '
          f"인용 수집 · 같은 수치가 {md_name} B8 표에 있음(SSOT)</div>")
        W("  </div>")
        obs_items = [(pid, o) for pid, d in struct.items()
                     for o in d.get("speech_observations") or []]
        W(f"  <details><summary>관찰 인용 {len(obs_items)}건 전체 목록</summary>"
          '<div class="inner"><ul>')
        kind_tag = {k: ("fix" if i == 0 else "warnb" if i == 1 else "gray")
                    for i, k in enumerate(ob["kinds"])}
        for pid, o in obs_items:
            guess = (f' <span class="flag">추정: {esc_cut(o.get("asr_guess"), 30)}</span>'
                     if o.get("asr_guess") else "")
            W(f"    <li>{tc(pid, o.get('ts'))} <span class=\"tag {kind_tag.get(o.get('kind'), 'gray')}\">"
              f"{esc(o.get('kind'))}</span> “{esc_cut(o.get('quote'), 70)}”{guess} "
              f"{src_link(anchors, 'str-' + str(o.get('id')))}</li>")
        W("  </ul></div></details>")
    reus = [(pid, rm) for pid, d in struct.items() for rm in d.get("reusable") or []]
    if reus:
        W("  <h3>재사용 후보 멘트</h3><ul class=\"qlist\">")
        for pid, rm in reus:
            W(f"    <li>{tc(pid, rm.get('ts'))}<blockquote>{esc_cut(rm.get('quote'), 70)}"
              f"</blockquote><span>{esc_cut(rm.get('why'), 50)}</span>"
              f"{src_link(anchors, 'str-' + str(rm.get('id')))}</li>")
        W("  </ul>")
    W("</section>")

    # ── C ──
    W('<section id="c">')
    W('  <div class="shead"><span class="idx">C</span><h2>증거·측정 정의</h2>'
      '<span class="note">검증용 원문 — 기본 접힘</span></div>')
    W(f"  <details><summary>C1. 인용 증거 전문 ({len(ctx['evidence'])}건)</summary>"
      '<div class="inner">')
    by_pid = {}
    for aid, quote, ts, pid in ctx["evidence"]:
        by_pid.setdefault(pid or "?", []).append((aid, quote, ts))
    for pid in sorted(by_pid):
        W(f'  <h3 style="margin-top:18px">{esc(pid)}</h3><ul>')
        for aid, quote, ts in by_pid[pid]:
            W(f'    <li id="{aid}">{tc(pid, ts)} <code style="font-family:var(--mono);'
              f'font-size:10.5px;color:var(--faint)">{aid}</code>'
              f'<blockquote style="margin-top:4px">{esc(quote)}</blockquote></li>')
        W("  </ul>")
    W("  </div></details>")
    W('  <details><summary>C2. 측정 정의·비고</summary><div class="inner"><ul>')
    W('    <li>정의 SSOT: <code style="font-family:var(--mono);font-size:12px">'
      "references/metrics.md</code> — 표기·구성만 다루며 정의 미변경</li>")
    for n in s70.FIXED_NOTES:
        W(f"    <li>{esc(n)}</li>")
    spans = " · ".join(f"{esc(pid)} {esc(s70._span(b))}"
                       for pid, b in (metrics.get("bounds_used") or {}).items())
    if spans:
        W(f"    <li>측정 구간 — {spans}</li>")
    for n in metrics.get("notes") or []:
        W(f"    <li>{esc(n)}</li>")
    for u in ctx["unavailable"]:
        W(f"    <li>미가용: {esc(u)}</li>")
    W(f"    <li>인용 검증 위반 폐기 {s['dropped']}건</li>")
    W("  </ul></div></details>")
    W('  <p class="desc" style="margin-top:14px">인용 원문에는 ASR 오인식이 섞일 수 있어요 — '
      "원문은 보존하고 보정은 병기로만 합니다</p>")
    W("</section>")
    W("</main>")
    # 사이드카 파일명은 manifest 파생이라 여기서 하드코딩하지 않는다 — 공유받은 사람이
    # 없는 파일을 찾게 된다(실행 메타를 어디서 보라는 안내가 이 문장의 유일한 목적이다).
    W(f"<footer>{esc(m.label)} · 생성 {esc(ctx['today'])} · 파이프라인 v{PIPELINE_VERSION}"
      f" · whisper {esc(au.get('model') or '미사용')} · 실행 메타·경로는 "
      f"사이드카({md_name} C3)에만"
      " 기록 — 본 HTML은 공유용(브리프 3-B)</footer>")
    W(f"<script>{chartjs}</script>")
    W(f"<script>{chart_js(data)}</script>")
    W("</body>\n</html>")
    return "\n".join(H), data

def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def main():
    ap = argparse.ArgumentParser(description="공유용 report.html 발행 + 링크 검증")
    ap.add_argument("manifest")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--growth-dir", default=None,
                    help="추세 CSV 폴더 — 지정할 때만 추세 카드를 그린다"
                         "(analyze.sh가 LA_GROWTH_DIR을 넘긴다)")
    args = ap.parse_args()
    m = load_manifest(args.manifest)
    out = m.report_html_path
    if not stage_guard(out, args.force):
        return
    md_path = m.report_md_path
    md_str = (md_path.read_text(encoding="utf-8") if md_path.is_file() else "")
    if not md_str:
        raise ValueError(f"{md_path.name} 없음 — s70_report.py를 먼저 실행하세요")
    metrics = require_json(m.artifacts_dir / "metrics.json", "s50_metrics.py")
    goal = require_json(m.artifacts_dir / "llm" / "goal_check.json", "s60_goalcheck.py")
    review = require_json(m.artifacts_dir / "llm" / "review.json", "s65_review.py")
    s70.normalize_prose(review, goal, (metrics.get("text") or {}).get("speaker_share") or {})
    sem, un_sem = load_analysis(m, "semantic")
    struct, un_struct = load_analysis(m, "structure")
    # s70과 같은 규칙으로 직전 회차를 읽는다 — 추세 카드·배지가 md와 어긋나면 안 된다.
    # 추세 CSV가 opt-in이므로 미지정이면 s70과 똑같이 비교 대상 없음('첫 측정')이 된다.
    gdir = s70.growth_dir(m, args.growth_dir)
    prev_rows = (s70.trend_tail(gdir / "trend_metrics.csv", m.project, m.session_id)
                 if gdir else [])
    html_str, data = build({
        "manifest": m, "metrics": metrics, "sem": sem, "struct": struct,
        "goal": goal, "review": review, "prev_rows": prev_rows,
        "summary": s70.summarize(metrics, sem, struct, goal, review),
        "evidence": s70.collect_evidence(sem, struct, goal),
        "unavailable": [t0 for t0, _ in un_sem + un_struct],
        "confirmed_actions": s70.confirmed_actions_of(md_path),
        "today": md_str.split("생성: ", 1)[-1][:10] if "생성: " in md_str else m.date,
    })
    validate(html_str, md_str)
    no_internal_paths(html_str, m)
    out.write_text(html_str, encoding="utf-8")
    # 차트 원천 수치의 JSON 내보내기 — "차트에만 있는 숫자 금지"의 기계 검증 지점(브리프 3-B).
    s70_data = m.artifacts_dir / "report_data.json"
    s70_data.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # 세션 폴더 루트의 내비게이션(스펙 §4). analyze.sh 완료 안내가 이 파일을 가리키므로
    # 발행 스테이지가 같이 쓴다 — 파일명은 manifest 파생이라 여기서도 조립하지 않는다.
    # 스킬 설치 경로는 적지 않는다: README는 산출물과 함께 건네질 수 있는 파일이다.
    (m.out_dir / "README.md").write_text(f"""# {m.label} 강의 분석 ({m.date})

- **먼저 볼 것**: `outputs/{m.report_html_path.name}` — 브라우저로 열면 요약·차트가 보입니다.
- **강사·제3자에게 공유할 파일은 위 html 하나**입니다.
  `outputs/{m.report_md_path.name}`는 데이터 레이어(실행 메타 포함)라 내부용입니다.
- `artifacts/`는 파이프라인 중간 산출물(재실행·검증용)입니다 — 직접 볼 일은 없습니다.
- 재실행: 이 폴더에서 `bash <스킬 폴더>/analyze.sh run manifest.toml` (완료 스테이지는 SKIP).
""", encoding="utf-8")
    kb = len(html_str.encode("utf-8")) // 1024
    print(f"{out.name} 발행({kb}KB · 단일 파일 오프라인) + artifacts/report_data.json "
          "+ README.md — 내부 링크 검증 통과")

if __name__ == "__main__":
    run(main)
