"""상품 벡터 지도 → docs/product_map_<type>.svg (종류별 한 장씩, 크게), out/product_vectors.csv
상품을 속성 공간의 벡터로 놓고(cvs.embed) type별로 2차원에 투영(PCA)한다. 가까울수록 대체 후보.
선 = 대체 간선(진할수록 강함), 빨간 점선 = 방향 정책이 막은 쌍(예: 제로 → 일반).
python tools/product_map.py"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cvs  # noqa: E402
from board_svg import FONT, INK, SOFT, MARK, TYPE_KO, Board  # noqa: E402

PALETTE = ["#e8590c", "#1971c2", "#2f9e44", "#9c36b5", "#c2255c", "#0c8599", "#e67700", "#5c7cfa", "#868e96"]


def project(ids, M):
    X = np.array([M[i] for i in ids])
    X = X - X.mean(axis=0)
    u, s, _ = np.linalg.svd(X, full_matrices=False)
    xy = u[:, :2] * s[:2]
    return {i: xy[k] for k, i in enumerate(ids)}


def spread(pt, box, gap=26, steps=300, wide=1.0):
    """겹친 점을 조금씩 밀어낸다(같은 벡터를 가진 상품이 한 점에 쌓이지 않게). 상대 위치는 거의 유지.
    wide > 1 이면 가로 거리를 그만큼 짧게 보고 밀어낸다 — 옆에 붙는 상품명(가로로 긴 글자)이 겹치지 않게."""
    ids, rng = list(pt), np.random.default_rng(0)
    xy = np.array([pt[i] for i in ids], dtype=float) + rng.normal(0, 0.5, (len(ids), 2))
    for _ in range(steps):
        d = (xy[:, None, :] - xy[None, :, :]) / np.array([wide, 1.0])
        dist = np.linalg.norm(d, axis=2) + np.eye(len(ids))
        push = np.clip(gap - dist, 0, None) / dist
        np.fill_diagonal(push, 0)
        xy += (d * push[:, :, None]).sum(axis=1) * np.array([wide, 1.0]) * 0.25
        xy[:, 0] = xy[:, 0].clip(box[0], box[2])
        xy[:, 1] = xy[:, 1].clip(box[1], box[3])
    return {i: xy[k] for k, i in enumerate(ids)}


def main():
    attrs, cats, P, _, policy, _ = cvs.load_context()
    W, gold = cvs.load_weights(), cvs.load_gold()
    M = cvs.embed(P, attrs, W)                          # 추천에 쓰는 벡터 (선택 뎁스)
    F = cvs.embed(P, attrs, {})                         # 지도용 벡터 (전 뎁스) — 같은 뎁스1 값을 가진 상품도 펼쳐 보이게
    subs, _, blocked, _ = cvs.build_edges(P, attrs, policy, W, gold, 5, 0.55)
    rows = []
    for typ in TYPE_KO:
        ids = [i for i, p in P.items() if p["type"] == typ]
        pos = project(ids, F)
        b = Board()
        PW, PH = 1400, max(640, 160 + 12 * len(ids))  # 상품이 많을수록 세로로 길게 — 이름이 겹치지 않게
        WIDTH = PW + 48
        b.text(24, 44, f"상품 벡터 지도 — {TYPE_KO[typ]} {len(ids)}개", 28, 800)
        b.text(24, 70, "속성 공간에서 상품의 위치 (전 속성 벡터를 2차원 투영). 가까울수록 대체 후보 · 선 = 대체 간선(진할수록 강함) · 빨간 점선 = 방향 정책 차단(제로 → 일반 등)", 15, 400, SOFT)
        ox, oy = 24, 96
        b.frame(ox, oy, PW, PH, f"{TYPE_KO[typ]} {len(ids)}개")
        # 투영 좌표를 그대로 늘이면 멀리 떨어진 묶음(사탕·껌 등) 때문에 빈 공간이 생긴다 → 거리 절반 + 순위 절반으로 고르게 편다
        def even(k):
            v = np.array([pos[i][k] for i in ids])
            lin = (v - v.min()) / (np.ptp(v) or 1)
            rank = np.argsort(np.argsort(v)) / max(len(v) - 1, 1)
            return {i: 0.4 * lin[n] + 0.6 * rank[n] for n, i in enumerate(ids)}
        ex, ey = even(0), even(1)
        sx = lambda i: ox + 60 + ex[i] * (PW - 470)
        sy = lambda i: oy + 50 + ey[i] * (PH - 100)
        groups = sorted({".".join(P[i]["attrs"]["category"].split(".")[:2]) for i in ids})
        color = {g: PALETTE[k % len(PALETTE)] for k, g in enumerate(groups)}
        pt = spread({i: (sx(i), sy(i)) for i in ids}, (ox + 20, oy + 30, ox + PW - 400, oy + PH - 30), gap=22, steps=600, wide=7.5)
        top2 = {}
        for (a, c), e in subs.items():
            if P[a]["type"] == typ and e["review"] != "unreviewed":
                top2.setdefault(a, []).append((e["strength"], c, e))
        for a, lst in top2.items():  # 상품마다 가장 강한 대체 2개만 — 선이 너무 많으면 묶음이 안 보인다
            for _, c, e in sorted(lst, key=lambda t: t[:2], reverse=True)[:2]:
                b.add(f'<line x1="{pt[a][0]:.1f}" y1="{pt[a][1]:.1f}" x2="{pt[c][0]:.1f}" y2="{pt[c][1]:.1f}" '
                      f'stroke="{INK}" stroke-opacity="{0.08 + 0.3 * e["strength"]:.2f}" stroke-width="1.4"/>')
        for kind, a, c, _, why in blocked:
            if kind == "SUBSTITUTE" and P[a]["type"] == typ and why.startswith("방향") and float(M[a] @ M[c]) > 0.6:
                b.add(f'<line x1="{pt[a][0]:.1f}" y1="{pt[a][1]:.1f}" x2="{pt[c][0]:.1f}" y2="{pt[c][1]:.1f}" '
                      f'stroke="#e03131" stroke-width="1.4" stroke-dasharray="4 3" stroke-opacity=".7"/>')
        placed = []
        for i in sorted(ids, key=lambda i: (pt[i][1], pt[i][0])):
            x, y = pt[i]
            g = ".".join(P[i]["attrs"]["category"].split(".")[:2])
            b.add(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="{color[g]}" stroke="#fff" stroke-width="2"/>')
            ly = y + 5
            while any(abs(ly - py) < 17 and x - 190 < px < x + 190 for px, py in placed) and ly < oy + PH - 12:  # 이름 겹침 회피
                ly += 17
            placed.append((x, ly))
            if ly != y + 5:
                b.add(f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x + 9:.1f}" y2="{ly - 5:.1f}" stroke="#adb5bd" stroke-width="1"/>')
            b.text(x + 11, ly, P[i]["name"], 14, 400, INK, FONT)
            near = sorted(((float(M[i] @ M[q]), q) for q in ids if q != i), reverse=True)[:3]
            rows.append((i, typ, P[i]["name"], g, round(float(pos[i][0]), 4), round(float(pos[i][1]), 4),
                         " | ".join(f"{P[q]['name']} {s:.2f}" for s, q in near)))
        for k, g in enumerate(groups):
            gy = oy + 40 + k * 24
            b.add(f'<circle cx="{ox + PW - 150}" cy="{gy - 5}" r="7" fill="{color[g]}"/>')
            b.text(ox + PW - 136, gy, cats.get(g, {}).get("name", g), 15, 400, SOFT)
        out = Path(cvs.ROOT) / f"docs/product_map_{typ}.svg"
        out.write_text(b.svg(WIDTH, oy + PH + 24), encoding="utf-8")
        print(out)
    cvs.write_csv("out/product_vectors.csv", ["id", "type", "name", "category2", "x", "y", "nearest3"], rows)
    print("out/product_vectors.csv")


if __name__ == "__main__":
    main()
