"""README 개발 과정의 수치 그림 → docs/fig_rank.svg, docs/fig_validation.svg
fig_rank: 1순위 적중률, 우리 추천 vs 기준선 (상품 118개 시기, docs/rank_eval.md 2026-10-23 결과)
fig_validation: 새 상품 검증 설문 채점 (out/distill_eval.csv 의 '검증 ·' 행, python cvs.py distill 이 만든다)

python tools/story_charts.py"""
import csv
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONT = "'Pretendard','Malgun Gothic','Apple SD Gothic Neo','Noto Sans KR',sans-serif"
STYLE = """<style>
  .bg{fill:#fcfcfb}.t1{fill:#0b0b0b}.t2{fill:#52514e}.t3{fill:#8a8984}.grid{stroke:#e8e6e1}.axis{stroke:#c9c7c0}
  .s1{fill:#2a78d6}.s0{fill:#b9b7b0}
  @media (prefers-color-scheme: dark){
    .bg{fill:#1a1a19}.t1{fill:#f0efec}.t2{fill:#c3c2b7}.t3{fill:#8f8e88}.grid{stroke:#2e2d2b}.axis{stroke:#4a4945}
    .s1{fill:#3987e5}.s0{fill:#5c5b56}}
</style>"""

# docs/rank_eval.md "결과 (2026-10-23)" 표의 1순위 열
RANK = [("우리 추천", 0.893), ("벡터 유사도순", 0.858), ("인기순", 0.483), ("무작위", 0.411)]


def bar(x, y, w, h, cls):
    """오른쪽 끝만 둥근 막대 (기준선에 붙는 쪽은 각지게)."""
    r = min(4, w / 2)
    return (f'<path class="{cls}" d="M{x:.1f},{y:.1f} h{w - r:.1f} a{r},{r} 0 0 1 {r},{r} v{h - 2 * r:.1f} '
            f'a{r},{r} 0 0 1 -{r},{r} h-{w - r:.1f} z"/>')


def hbars(path, title, sub, groups, series, label):
    """groups = [(이름, [값 per series])], series = [(이름, css class)]. 값은 0~1 비율."""
    W, padL, padR, top = 960, 200, 90, 130 if len(series) > 1 else 104
    barH, inner, gap = 30, 6, 30
    gh = len(series) * barH + (len(series) - 1) * inner
    H = top + len(groups) * (gh + gap) - gap + 52
    x = lambda v: padL + (W - padL - padR) * v
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="{FONT}" role="img" '
         f'aria-label="{escape(title)}">', STYLE, f'<rect class="bg" width="{W}" height="{H}" rx="12"/>',
         f'<text class="t1" x="28" y="44" font-size="24" font-weight="700">{escape(title)}</text>',
         f'<text class="t2" x="28" y="72" font-size="15">{escape(sub)}</text>']
    if len(series) > 1:
        lx = padL
        for name, cls in series:
            o += [f'<rect class="{cls}" x="{lx}" y="{96}" width="14" height="14" rx="3"/>',
                  f'<text class="t2" x="{lx + 22}" y="{108}" font-size="15">{escape(name)}</text>']
            lx += 44 + len(name) * 15
    base = top + len(groups) * (gh + gap) - gap + 8
    for v in (0, 0.25, 0.5, 0.75, 1):
        o += [f'<line class="grid" x1="{x(v):.1f}" y1="{top - 8}" x2="{x(v):.1f}" y2="{base}" stroke-width="1"/>',
              f'<text class="t3" x="{x(v):.1f}" y="{base + 20}" font-size="14" text-anchor="middle">{v:.0%}</text>']
    for i, (name, vals) in enumerate(groups):
        y0 = top + i * (gh + gap)
        o.append(f'<text class="t1" x="{padL - 16}" y="{y0 + gh / 2 + 6:.1f}" font-size="16" font-weight="600" text-anchor="end">{escape(name)}</text>')
        for k, ((_, cls), v) in enumerate(zip(series, vals)):
            y = y0 + k * (barH + inner)
            c = cls(i) if callable(cls) else cls
            o += [bar(padL, y, x(v) - padL, barH, c),
                  f'<text class="{"t1" if c == "s1" else "t2"}" x="{x(v) + 10:.1f}" y="{y + 21}" font-size="16" '
                  f'font-weight="{700 if c == "s1" else 400}">{label(v)}</text>']
    o += [f'<line class="axis" x1="{padL}" y1="{top - 8}" x2="{padL}" y2="{base}" stroke-width="1"/>', "</svg>"]
    (ROOT / path).write_text("\n".join(o), encoding="utf-8")
    print(path)


def main():
    pct = lambda v: f"{v:.1%}"
    hbars("docs/fig_rank.svg", "1순위 적중률", "손님이 우리가 1순위로 보여 준 상품을 고른 비율 · 상품 118개, 짝수 페르소나 채점",
          [(n, [v]) for n, v in RANK], [("", lambda i: "s1" if i == 0 else "s0")], pct)
    rows = {r["method"]: r for r in csv.DictReader(open(ROOT / "out/distill_eval.csv", encoding="utf-8-sig"))}
    before, after = rows["검증 · 정책+벡터만"], rows["검증 · 증류 모델 + 선별 설문"]
    metrics = [("셋 중 하나를 고름", "pick"), ("1순위를 고름", "first")]
    hbars("docs/fig_validation.svg", "새 상품 24개 검증 설문", "판정에 쓰지 않은 별도 응답 240건으로 채점",
          [(n, [float(before[k]), float(after[k])]) for n, k in metrics],
          [("정책 + 벡터만", "s0"), ("+ 증류 모델 + 선별 설문", "s1")], pct)


if __name__ == "__main__":
    main()
