"""상품을 늘릴 때 판정 비용(토큰) 비교 차트 → docs/token_cost.svg (README에 이미지로 들어간다).
실측은 survey/cost_log.yaml 의 서브에이전트 토큰 합계, 실행하지 않은 방식은 같은 방식의 실측 단가(응답 1건당 토큰) × 응답 수로 환산한다.

python tools/token_chart.py"""
import sys
from html import escape
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FONT = "'Pretendard','Malgun Gothic','Apple SD Gothic Neo','Noto Sans KR',sans-serif"


def text_w(s, size):
    return sum(size * (0.95 if ord(ch) > 0x2E80 else 0.58) for ch in s)


def fmt(t):
    return f"{t / 1e6:.1f}M" if t >= 1e6 else f"{t / 1e3:.0f}k"


def main():
    log = yaml.safe_load((ROOT / "survey/cost_log.yaml").read_text(encoding="utf-8"))
    run = {r["key"]: r for r in log["runs"]}
    s = log["scale_up"]
    unit_full = run["first"]["tokens"] / run["first"]["responses"]
    unit_sel = run["round2"]["tokens"] / run["round2"]["responses"]
    tag = run["catalog"]["tokens"]
    rows = [("전수 재설문", s["full_responses"] * unit_full, "환산", f"{s['full_responses']:,}응답"),
            ("선별 설문", s["selective_responses"] * unit_sel, "환산", f"{s['selective_responses']:,}응답"),
            ("증류 모델 + 선별 설문", run["round2"]["tokens"], "실측", f"{run['round2']['responses']:,}응답")]

    W, padL, padR = 960, 230, 110
    plotW = W - padL - padR
    top = 150
    barH, gap = 40, 44
    vmax = max(tag + r[1] for r in rows)
    step = 2e6 if vmax > 6e6 else 1e6
    xmax = (int(vmax // step) + 1) * step
    x = lambda v: padL + plotW * v / xmax
    H = top + len(rows) * (barH + gap) + 50
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="{FONT}" role="img" '
         f'aria-label="상품 {s["before"]}개에서 {s["after"]}개로 늘릴 때 판정 토큰: '
         + ", ".join(f"{n} {fmt(tag + v)}" for n, v, *_ in rows) + '">',
         """<style>
  .bg{fill:#fcfcfb}.t1{fill:#0b0b0b}.t2{fill:#52514e}.t3{fill:#8a8984}.grid{stroke:#e8e6e1}.axis{stroke:#c9c7c0}
  .s1{fill:#2a78d6}.s2{fill:#eb6834}.tile{fill:#f3f2ef}
  @media (prefers-color-scheme: dark){
    .bg{fill:#1a1a19}.t1{fill:#f0efec}.t2{fill:#c3c2b7}.t3{fill:#8f8e88}.grid{stroke:#2e2d2b}.axis{stroke:#4a4945}
    .s1{fill:#3987e5}.s2{fill:#d95926}.tile{fill:#242422}}
</style>""", f'<rect class="bg" width="{W}" height="{H}" rx="12"/>',
         f'<text class="t1" x="28" y="44" font-size="24" font-weight="700">상품 {s["before"]} → {s["after"]}개, 판정 토큰 비교</text>',
         f'<text class="t2" x="28" y="74" font-size="15">상품 {s["added"]}개를 추가하고 대체 판정을 할 때 든 토큰 (페르소나 {s["personas"]}명)</text>']
    ly = 112
    o += [f'<rect class="s2" x="{padL}" y="{ly - 12}" width="14" height="14" rx="3"/>',
          f'<text class="t2" x="{padL + 22}" y="{ly}" font-size="15">상품 속성 태깅 (세 방식 공통, 실측 {fmt(tag)})</text>',
          f'<rect class="s1" x="{padL + 360}" y="{ly - 12}" width="14" height="14" rx="3"/>',
          f'<text class="t2" x="{padL + 382}" y="{ly}" font-size="15">대체 판정 설문</text>']
    base = top + len(rows) * (barH + gap) - gap + 10
    v = 0
    while v <= xmax:
        o += [f'<line class="grid" x1="{x(v):.1f}" y1="{top - 8}" x2="{x(v):.1f}" y2="{base}" stroke-width="1"/>',
              f'<text class="t3" x="{x(v):.1f}" y="{base + 18}" font-size="14" text-anchor="middle">{"0" if v == 0 else fmt(v)}</text>']
        v += step
    for i, (name, val, kind, resp) in enumerate(rows):
        y = top + i * (barH + gap)
        o += [f'<text class="t1" x="{padL - 16}" y="{y + 17}" font-size="16" font-weight="600" text-anchor="end">{escape(name)}</text>',
              f'<text class="t3" x="{padL - 16}" y="{y + 37}" font-size="14" text-anchor="end">{kind} · {resp}</text>',
              f'<rect class="s2" x="{padL}" y="{y}" width="{x(tag) - padL:.1f}" height="{barH}"/>',
              f'<path class="s1" d="M{x(tag) + 2:.1f},{y} h{x(tag + val) - x(tag) - 6:.1f} a4,4 0 0 1 4,4 v{barH - 8} a4,4 0 0 1 -4,4 h-{x(tag + val) - x(tag) - 6:.1f} z"/>',
              f'<text class="t1" x="{x(tag + val) + 10:.1f}" y="{y + 27}" font-size="18" font-weight="700">{fmt(tag + val)}</text>']
    o.append(f'<line class="axis" x1="{padL}" y1="{top - 8}" x2="{padL}" y2="{base}" stroke-width="1"/>')
    o.append("</svg>")
    (ROOT / "docs/token_cost.svg").write_text("\n".join(o), encoding="utf-8")
    for name, val, kind, resp in rows:
        print(f"{name}: {fmt(tag + val)} ({kind}, {resp}, 설문 {fmt(val)} + 태깅 {fmt(tag)})")


if __name__ == "__main__":
    main()
