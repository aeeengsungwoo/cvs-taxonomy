"""FigJam 스타일 보드를 SVG로 생성 → docs/board.svg (README에 이미지로 들어간다).
실제 파이프라인 결과로 그리므로 데이터·정책을 바꾸면 다시 돌린다: python tools/board_svg.py"""
import sys
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cvs  # noqa: E402

FONT = "'Malgun Gothic','Apple SD Gothic Neo','Noto Sans KR',sans-serif"
HAND = "'Gaegu','Nanum Pen Script','Malgun Gothic',cursive"
INK, SOFT, LINE, MARK = "#23221f", "#6b6862", "#57534b", "#5b3fd1"
C = {"gray": "#e6e3dc", "yellow": "#ffe28a", "blue": "#aed8ff", "violet": "#d6c6ff",
     "orange": "#ffc99e", "green": "#b4ecbf", "pink": "#ffc4e6", "red": "#ffb3ad"}
TYPE_KO = {"snack": "과자", "ramen": "라면", "beverage": "음료", "household": "생필품"}


def text_w(s, size):
    return sum(size * (0.95 if ord(ch) > 0x2E80 else 0.56) for ch in s)


def wrap(s, max_w, size):
    out = []
    for para in s.split("\n"):
        line = ""
        for word in para.split(" "):
            cand = f"{line} {word}".strip()
            if text_w(cand, size) <= max_w or not line:
                line = cand
            else:
                out.append(line)
                line = word
        out.append(line)
    return out


class Board:
    def __init__(self):
        self.el = []

    def add(self, s):
        self.el.append(s)

    def text(self, x, y, s, size=12, weight=400, fill=INK, family=FONT, anchor="start"):
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family}" font-size="{size}" font-weight="{weight}" '
                 f'fill="{fill}" text-anchor="{anchor}">{escape(s)}</text>')

    def sticky(self, x, y, w, title, body, color):
        """스티키 하나. 반환: 높이."""
        lines_t = wrap(title, w - 20, 13) if title else []
        lines_b = wrap(body, w - 20, 11.5) if body else []
        h = 14 + len(lines_t) * 17 + len(lines_b) * 15.5 + 8
        self.add(f'<rect x="{x + 1}" y="{y + 3}" width="{w}" height="{h:.1f}" rx="3" fill="#000" opacity=".08"/>')
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h:.1f}" rx="3" fill="{C[color]}"/>')
        cy = y + 12
        for ln in lines_t:
            cy += 15
            self.text(x + 10, cy, ln, 13, 700)
        cy += 2
        for ln in lines_b:
            cy += 15.5
            self.text(x + 10, cy, ln, 11.5)
        return h

    def frame(self, x, y, w, h, label):
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="#fff" stroke="#e2dfd8"/>')
        tw = text_w(label, 12) + 20
        self.add(f'<rect x="{x + 16}" y="{y - 12}" width="{tw:.0f}" height="22" rx="6" fill="{INK}"/>')
        self.text(x + 26, y + 3, label, 12, 700, "#fff")

    def arrow(self, x1, y, x2):
        self.add(f'<path d="M{x1} {y} H{x2 - 7}" stroke="{LINE}" stroke-width="2" fill="none"/>'
                 f'<polygon points="{x2 - 8},{y - 5} {x2},{y} {x2 - 8},{y + 5}" fill="{LINE}"/>')

    def svg(self, w, h):
        dots = ('<defs><pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse">'
                '<circle cx="1.5" cy="1.5" r="1.2" fill="#d3d0c8"/></pattern></defs>')
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">{dots}'
                f'<rect width="{w}" height="{h}" fill="#f2f1ee"/><rect width="{w}" height="{h}" fill="url(#dots)"/>'
                + "".join(self.el) + "</svg>")


def main():
    attrs, cats, P, issues, policy, tpo = cvs.load_context()
    W = cvs.load_weights()
    gold = cvs.load_gold()
    subs, comps, blocked, conflicts = cvs.build_edges(P, attrs, policy, W, gold, 5, 0.55)
    combos = cvs.build_combos(P, policy, tpo, comps)
    count = lambda t: sum(p["type"] == t for p in P.values())
    depth = lambda t: W.get(t, cvs.DEFAULT_W)["max_depth"]

    b = Board()
    WIDTH, X0 = 1720, 24
    y = 24
    b.text(X0, y + 22, "우리동네GS 추천 온톨로지", 26, 700)
    b.text(X0, y + 46, f"과자 {count('snack')} · 라면 {count('ramen')} · 음료 {count('beverage')} · 생필품 {count('household')} "
           f"시드 기준 실제 결과 · python tools/board_svg.py 로 재생성", 13, 400, SOFT)
    y += 80

    # ── 1. 플로우 ──
    cols = [
        ("원본 상품", [("상품 데이터", "이름 · 카테고리 · 속성 표기\ndata/products.csv", "gray"),
                    ("새 상품", "먹태깡 청양마요맛\n맛: 짠맛|해산물|청양|마요", "gray")]),
        ("① 정규화", [("동의어 흡수", "청양 → 매운맛\n제로 → 당 없음", "yellow"),
                    ("처음 보는 값", "마요 → 비우고 후보 큐\nreview/new_values.csv", "yellow"),
                    ("승격 기준", "상품 3개 이상에서 등장 → 정의 붙여 새 값", "yellow")]),
        ("② 속성 부여 (뎁스)", [("D1 정체성", "카테고리 · 주원료 · 제조방식 · 조리형태 · 알코올 · 탄산 · 용도 · 규격", "yellow"),
                           ("D2 감각", "맛계열 · 맵기 · 식감 · 국물베이스 · 카페인 · 당", "yellow"),
                           ("D3 상황·가치", "가격대 · 용량 · 구매상황 · 음용온도 · 용기", "yellow"),
                           ("D4 세부", "주타깃 · 제조사", "yellow")]),
        ("③ TPO 추론", [("술안주 · 음주", "→ 저녁·심야 / 집·야외", "blue"),
                      ("봉지라면", "→ 사무실·이동중 제외", "blue"),
                      ("국물 / 온장 음료", "→ 추움·비, 더움 제외", "blue"),
                      ("주류", "→ 아침·사무실·이동중 제외", "blue"),
                      ("외박·여행 · 우천", "→ 심야·이동중 / 비", "blue")]),
        ("④ 아이덴티티", [("과자 · 음료", "카테고리 2단계\nSNK.CHIP · BEV.CARB", "violet"),
                       ("라면", "조리형태: 국물 · 짜장 · 볶음 · 비빔", "violet"),
                       ("생필품", "카테고리 3단계\n칫솔 ≠ 치약 → 보완재", "violet")]),
        ("⑤ 점수 (type별 뎁스)", [("뎁스별 가중 유사도", "트리 거리 · 일치 · 자카드 · 순서 거리", "violet"),
                             ("sweep 결과", f"과자 ≤{depth('snack')} · 라면 ≤{depth('ramen')}\n"
                                          f"음료 ≤{depth('beverage')} · 생필품 ≤{depth('household')}", "violet")]),
        ("⑥ 정책 필터", [("상품간", "구매상황 겹침 · 주류×어린이 금지", "orange"),
                      ("규격", "가격대·용량 1 · 알코올 0단계\n규격·라면 포장 일치", "orange"),
                      ("방향", "제로 → 일반 금지\n일반 → 제로 감점 허용", "orange"),
                      ("아이덴티티", "같은 정체성끼리만 대체, 다른 정체성끼리만 보완", "orange"),
                      ("TPO", "보완재는 시간대 겹침", "orange"),
                      ("앵커", "라인·메뉴 같으면 +0.25\n둘 다 있으면 사람 결정", "orange")]),
        ("⑦ 추천 간선", [(f"대체재 {len(subs)}", "검수 O: 0.5 + 0.5×점수\n미검수: 0.8×점수", "green"),
                      (f"보완재 {len(comps)}", "지정 조합 0.6~0.9\n속성 규칙 0.25~0.5", "green"),
                      (f"콤보 {sum(map(len, combos.values()))}", f"장면 {len(tpo['scenes'])}개 × 상위 5", "green"),
                      ("트렌드", "판매·검색 데이터 대기", "green")]),
        ("⑧ 적재 · 검수", [("Neo4j", "out/graph.cypher", "gray"),
                        ("속성 영향도", "빼 보고 영향 없으면\n제거 후보", "pink"),
                        ("인간 검수", "queue.csv에 O / X\nanchor.csv에 기준", "pink")]),
    ]
    cw, gap = 158, 22
    fy, top = y, y + 34
    heights, review_xy, score_x, attr_x = [], None, None, None
    x = X0 + 16
    for i, (title, items) in enumerate(cols):
        b.text(x, top - 8, title, 12, 700, SOFT)
        cy = top
        for t, body, col in items:
            h = b.sticky(x, cy, cw, t, body, col)
            if t == "인간 검수":
                review_xy = (x + cw / 2, cy + h)
            cy += h + 8
        heights.append(cy)
        if title.startswith("⑤"):
            score_x = (x + cw / 2, cy)
        if title.startswith("②"):
            attr_x = (x + cw / 2, cy)
        if i < len(cols) - 1:
            b.arrow(x + cw + 3, top + 18, x + cw + gap - 3)
        x += cw + gap
    loop1, loop2 = max(heights) + 30, max(heights) + 64
    rx, ry = review_xy
    for (tx, ty), ly, label in [(score_x, loop1, "O/X → 정답셋 → sweep 다시"),
                                (attr_x, loop2, "X가 많으면 → 속성·정의·정책 수정 후 루프 다시")]:
        b.add(f'<path d="M{rx} {ry + 2} V{ly} H{tx} V{ty + 10}" stroke="{MARK}" stroke-width="2" '
              f'stroke-dasharray="6 5" fill="none"/>'
              f'<polygon points="{tx - 6},{ty + 10} {tx + 6},{ty + 10} {tx},{ty}" fill="{MARK}"/>')
        b.text(tx + 12, ly - 7, label, 17, 700, MARK, HAND)
    fh = loop2 + 20 - fy
    frame1 = (X0, fy, WIDTH - 2 * X0, fh, "1 · 속성 증강 플로우")
    y = fy + fh + 40

    # ── 2. 예시 ──
    ex_ids = ["S43", "R01", "B02", "H13"]
    heads = ["원본 → 정규화", "D1 정체성", "D2 감각", "D3 상황·가치", "③ TPO", "④ 아이덴티티 · ⑥ 정책", "⑦ 추천 결과"]
    ex_y0 = y
    rowh_w, ecw, eg = 118, 196, 10
    hx = X0 + 16 + rowh_w
    for i, hname in enumerate(heads):
        b.text(hx + i * (ecw + eg), y + 34, hname, 12, 700, SOFT)
    y += 46
    fmt = lambda v: " | ".join(sorted(v)) if isinstance(v, frozenset) else str(v)
    for pid in ex_ids:
        p = P[pid]
        a = p["attrs"]
        by_depth = {d: [f"{attrs[k]['name']} {fmt(v)}" for k, v in a.items()
                        if attrs[k]["depth"] == d and k not in ("category", "maker")] for d in (1, 2, 3)}
        raw_issue = [i for i in issues if i[0] == pid and i[4] == "unknown_value"]
        norm = (f"{a['category']}\n" + ("미등록 값: " + ", ".join(i[3] for i in raw_issue) + " → 후보 큐"
                                        if raw_issue else "모든 값 등록됨"))
        tpo_txt = "\n".join(f"{dim}: {', '.join(sorted(v))}" for dim, v in p["tpo"].items()) or "TPO 없음 → 중립"
        if p["tpo_excl"]:
            tpo_txt += "\n제외: " + ", ".join(f"{', '.join(sorted(v))}" for v in p["tpo_excl"].values())
        ident = "/".join(str(x) for x in cvs.identity(p, policy))
        blk = [(P[y2 if x2 == pid else x2]["name"], why) for r, x2, y2, sc, why in blocked if r == "SUBSTITUTE" and pid in (x2, y2)]
        ident_txt = ident + ("\n차단: " + blk[0][0] + " — " + blk[0][1] if blk else "")
        sub = sorted(((e["strength"], P[y2 if x2 == pid else x2]["name"], e["review"]) for (x2, y2), e in subs.items() if pid in (x2, y2)), reverse=True)[:2]
        com = sorted(((e["strength"], P[y2 if x2 == pid else x2]["name"]) for (x2, y2), e in comps.items() if pid in (x2, y2)), reverse=True)[:2]
        rec = ("대체: " + ", ".join(f"{n} {s}{' ✔' if r == 'approved' else ''}" for s, n, r in sub) if sub else "대체: 없음")
        rec += "\n보완: " + (", ".join(f"{n} {s}" for s, n in com) if com else "없음")
        cells = [(norm, "gray"), ("\n".join(by_depth[1]), "yellow"), ("\n".join(by_depth[2]) or "해당 없음", "yellow"),
                 ("\n".join(by_depth[3]), "yellow"), (tpo_txt, "blue"), (ident_txt, "violet"), (rec, "green")]
        name_lines = wrap(p["name"], rowh_w - 8, 14)
        for li, ln in enumerate(name_lines):
            b.text(X0 + 16, y + 20 + li * 18, ln, 14, 700)
        b.text(X0 + 16, y + 20 + len(name_lines) * 18, f"{TYPE_KO[p['type']]} · {a.get('maker', '')}", 11.5, 400, SOFT)
        hmax = 0
        for i, (body, col) in enumerate(cells):
            hmax = max(hmax, b.sticky(hx + i * (ecw + eg), y, ecw, "", body, col))
        y += hmax + 14
    frame2 = (X0, ex_y0, WIDTH - 2 * X0, y - ex_y0 + 10, "2 · 속성 증강 예시 (카테고리별 1개)")
    y += 50

    # ── 3. 정책 차단 + 4. 카테고리 요약 ──
    bl_y0 = y
    picks, seen = [], set()
    for r, x2, y2, sc, why in sorted(blocked, key=lambda t: -t[3]):
        t = P[x2]["type"]
        key = (t, why.split(":")[0])
        if r == "SUBSTITUTE" and key not in seen:
            seen.add(key)
            picks.append((P[x2]["name"], P[y2]["name"], sc, why))
    picks = picks[:8]
    bw, bg = 196, 12
    for i, (n1, n2, sc, why) in enumerate(picks):
        bx = X0 + 16 + (i % 4) * (bw + bg)
        by = y + 24 + (i // 4) * 120
        b.sticky(bx, by, bw, f"{n1} ✕→ {n2}", f"유사도 {sc}\n{why}", "red")
    y_bl_end = y + 24 + ((len(picks) + 3) // 4) * 120
    b.text(X0 + 16, y_bl_end + 6, f"정책 차단 {len(blocked)}건 · 정책 충돌(검수 O인데 위반) {len(conflicts)}건", 17, 700, MARK, HAND)
    half = (WIDTH - 2 * X0 - 24) // 2
    frame3 = (X0, bl_y0, half, y_bl_end + 26 - bl_y0, "3 · 정책이 막은 추천")

    sx = X0 + half + 24
    rows = [("type", "상품", "정답셋", "뎁스", "대체", "차단")]
    for t in ("snack", "ramen", "beverage", "household"):
        ng = sum(1 for k in gold if P[k[0]]["type"] == t)
        rows.append((TYPE_KO[t], str(count(t)), str(ng), f"≤{depth(t)}",
                     str(sum(1 for k in subs if P[k[0]]["type"] == t)),
                     str(sum(1 for r in blocked if P[r[1]]["type"] == t))))
    colx = [0, 110, 190, 280, 360, 440]
    for ri, row in enumerate(rows):
        ry2 = y + 50 + ri * 30
        if ri:
            b.add(f'<rect x="{sx + 16}" y="{ry2 - 20}" width="{half - 32}" height="26" rx="3" fill="{C["yellow"] if ri % 2 else C["gray"]}" opacity=".7"/>')
        for ci, cell in enumerate(row):
            b.text(sx + 26 + colx[ci], ry2, cell, 13, 700 if ri == 0 or ci == 0 else 400, SOFT if ri == 0 else INK)
    ty = y + 50 + len(rows) * 30 + 10
    xc = {}
    for (x2, y2), e in comps.items():
        k = tuple(sorted((TYPE_KO[P[x2]["type"]], TYPE_KO[P[y2]["type"]])))
        xc[k] = xc.get(k, 0) + 1
    b.text(sx + 26, ty, "보완 간선 (type 조합)", 13, 700, SOFT)
    for i, (k, v) in enumerate(sorted(xc.items(), key=lambda t: -t[1])):
        b.text(sx + 26 + (i % 3) * 150, ty + 26 + (i // 3) * 22, f"{k[0]} ↔ {k[1]}  {v}", 13)
    frame4 = (sx, bl_y0, half, y_bl_end + 26 - bl_y0, "4 · 카테고리별 결과")
    y = y_bl_end + 60

    frames = []
    for fx, fy2, fw, fh2, label in (frame1, frame2, frame3, frame4):
        sub_b = Board()
        sub_b.frame(fx, fy2, fw, fh2, label)
        frames += sub_b.el
    b.el = frames + b.el
    out = Path(__file__).resolve().parent.parent / "docs/board.svg"
    out.write_text(b.svg(WIDTH, int(y)), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
