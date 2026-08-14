#!/usr/bin/env python3
"""s20: 클로바 txt → artifacts/parsed/pNN.json (+마스킹본, 강사 식별)."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from dataclasses import asdict
from lib import clova
from lib.manifest import load_manifest
from lib.stage import cli, stage_guard, write_json, die, run

MIN_SHARE = 0.60  # DESIGN §6: 미만이면 하드 중단

def main():
    args = cli("클로바 파싱·강사 식별·마스킹")
    m = load_manifest(args.manifest)
    # 마스킹 대상이 하나도 없으면 실행 시점에 알린다. init이 만드는 기본값이 []라,
    # 채우는 것을 잊으면 아무 신호 없이 전사 원문이 국외(Anthropic)로 나간다 —
    # 문서에만 적힌 규칙은 실행 중에 보이지 않는다(DESIGN §10.1).
    # 차단하지 않는 이유: 실명이 정말 없는 녹취도 있고, 여기서 막으면 그 세션은
    # manifest에 가짜 이름을 적기 전엔 진행할 방법이 없다. 결정은 사람에게 남긴다.
    if not m.mask_names:
        print("경고: mask_names가 비어 있음 — 전사 원문이 마스킹 없이 LLM(국외)으로 "
              "전송됩니다. manifest [speakers].mask_names 확인", file=sys.stderr)
    for per in m.periods:
        out = m.artifacts_dir / "parsed" / f"{per.id}.json"
        if not stage_guard(out, args.force):
            continue
        if per.clova is None:
            # 하류(s30~s65)가 키 유무로 분기하지 않도록 정상 산출물과 키 집합을 맞춘다.
            # 판별 규약: masked_blocks가 비면 스킵(+no_clova 플래그 참고).
            write_json(out, {
                "period": per.id, "no_clova": True, "title": "", "date": "",
                "duration_sec": 0, "instructor_label": "", "instructor_share": 0.0,
                "low_share_override": False,
                "blocks": [], "masked_blocks": [],
                "mask_names_count": len(m.mask_names),
            })
            print(f"{per.id}: clova 없음 → whisper 단독 모드 표시")
            continue
        # utf-8-sig: 실물 클로바 내보내기에 BOM이 섞여 있으면 제목 첫 글자에 U+FEFF가 붙는다.
        doc = clova.parse_clova(per.clova.read_text(encoding="utf-8-sig"))
        # 강사 라벨은 파일마다 다르다(실물에서 참석자 1/참석자 2 혼재) — 교시별로 매번 식별한다.
        shares = clova.speaker_shares(doc)
        manual = m.instructor_label != "auto"
        if manual:
            label = m.instructor_label
            share = shares.get(label, 0.0)
        else:
            label, share = clova.pick_instructor(doc)
        low_share_override = False
        if share < MIN_SHARE:
            if manual and label in shares:
                # 사용자가 라벨을 직접 지정했고 그 화자가 파일에 실제로 있다 = 명시적 오버라이드.
                # 토론·실습처럼 강사 발화가 적은 교시가 여기 해당한다. 막지 않되 흔적을 남긴다.
                low_share_override = True
                print(f"경고: [{per.id}] 지정 강사 {label!r} 점유율 {share:.0%} < {MIN_SHARE:.0%} "
                      f"— 수동 지정이므로 진행합니다(low_share_override).", file=sys.stderr)
            else:
                samples = {s: next(b.text[:40] for b in doc.blocks if b.speaker == s) for s in shares}
                # 라벨을 함께 찍는다 — 수동 지정 오타면 '점유율 0%'만으로는 원인이 보이지 않는다.
                # ('최고 점유율'이라 부르지 않는 이유: 수동 경로에서는 최고가 아니라 그 라벨의 점유율이다.)
                die(3, f"[{per.id}] instructor 식별 실패: {label!r} 점유율 {share:.0%} < {MIN_SHARE:.0%}\n"
                       f"화자 샘플: {samples}\n"
                       f"manifest [speakers].instructor_label을 위 화자 중 하나로 지정하세요.")
        masked = [clova.Block(b.speaker, b.ts, clova.mask_text(b.text, m.mask_names))
                  for b in doc.blocks]
        write_json(out, {
            "period": per.id, "no_clova": False, "title": doc.title, "date": doc.date,
            "duration_sec": doc.duration_sec,
            "instructor_label": label, "instructor_share": round(share, 4),
            "low_share_override": low_share_override,
            "blocks": [asdict(b) for b in doc.blocks],
            "masked_blocks": [asdict(b) for b in masked],
            "mask_names_count": len(m.mask_names),
        })
        print(f"{per.id}: blocks={len(doc.blocks)} instructor={label}({share:.0%})")

if __name__ == "__main__":
    run(main)
