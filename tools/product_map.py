"""상품 벡터 지도 → docs/product_map.svg, out/product_vectors.csv
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


def spread(pt, box, gap=26, steps=300):
    """겹친 점을 조금씩 밀어낸다(같은 벡터를 가진 상품이 한 점에 쌓이지 않게). 상대 위치는 거의 유지."""
    ids, rng = list(pt), np.random.default_rng(0)
    xy = np.array([pt[i] for i in ids], dtype=float) + rng.normal(0, 0.5, (len(ids), 2))
    for _ in range(steps):
        d = xy[:, None, :] - xy[None, :, :]
        dist = np.linalg.norm(d, axis=2) + np.eye(len(ids))
        push = np.clip(gap - dist, 0, None) / dist
        np.fill_diagonal(push, 0)
        xy += (d * push[:, :, None]).sum(axis=1) * 0.25
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
    b = Board()
    WIDTH, PW, PH, G = 1720, 832, 600, 24
    b.text(24, 44, "상품 벡터 지도", 26, 800)
    b.text(24, 68, "속성 공간에서 상품의 위치 (전 속성 벡터를 type별 2차원 투영). 가까울수록 대체 후보 · 선 = 대체 간선 · 빨간 점선 = 방향 정책 차단(제로 → 일반 등)", 13, 400, SOFT)
    for n, typ in enumerate(TYPE_KO):
        ids = [i for i, p in P.items() if p["type"] == typ]
        pos = project(ids, F)
        ox, oy = 24 + (n % 2) * (PW + G), 96 + (n // 2) * (PH + G)
        b.frame(ox, oy, PW, PH, f"{TYPE_KO[typ]} {len(ids)}개")
        xs, ys = np.array([pos[i][0] for i in ids]), np.array([pos[i][1] for i in ids])
        sx = lambda v: ox + 60 + (v - xs.min()) / (np.ptp(xs) or 1) * (PW - 200)
        sy = lambda v: oy + 40 + (v - ys.min()) / (np.ptp(ys) or 1) * (PH - 80)
        groups = sorted({".".join(P[i]["attrs"]["category"].split(".")[:2]) for i in ids})
        color = {g: PALETTE[k % len(PALETTE)] for k, g in enumerate(groups)}
        pt = spread({i: (sx(pos[i][0]), sy(pos[i][1])) for i in ids}, (ox + 20, oy + 30, ox + PW - 190, oy + PH - 20))
        for (a, c), e in subs.items():
            if P[a]["type"] == typ and e["review"] in ("approved", "auto"):
                b.add(f'<line x1="{pt[a][0]:.1f}" y1="{pt[a][1]:.1f}" x2="{pt[c][0]:.1f}" y2="{pt[c][1]:.1f}" '
                      f'stroke="{INK}" stroke-opacity="{0.08 + 0.25 * e["strength"]:.2f}" stroke-width="1.2"/>')
        for kind, a, c, _, why in blocked:
            if kind == "SUBSTITUTE" and P[a]["type"] == typ and why.startswith("방향") and float(M[a] @ M[c]) > 0.6:
                b.add(f'<line x1="{pt[a][0]:.1f}" y1="{pt[a][1]:.1f}" x2="{pt[c][0]:.1f}" y2="{pt[c][1]:.1f}" '
                      f'stroke="#e03131" stroke-width="1.4" stroke-dasharray="4 3" stroke-opacity=".7"/>')
        placed = []
        for i in sorted(ids, key=lambda i: (pt[i][1], pt[i][0])):
            x, y = pt[i]
            g = ".".join(P[i]["attrs"]["category"].split(".")[:2])
            b.add(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{color[g]}" stroke="#fff" stroke-width="1.5"/>')
            ly = y + 4
            while any(abs(ly - py) < 12 and x - 150 < px < x + 150 for px, py in placed):  # 이름 겹침 회피
                ly += 12
            placed.append((x, ly))
            if ly != y + 4:
                b.add(f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x + 7:.1f}" y2="{ly - 4:.1f}" stroke="#adb5bd" stroke-width=".8"/>')
            b.text(x + 8, ly, P[i]["name"], 10.5, 400, INK, FONT)
            near = sorted(((float(M[i] @ M[q]), q) for q in ids if q != i), reverse=True)[:3]
            rows.append((i, typ, P[i]["name"], g, round(float(pos[i][0]), 4), round(float(pos[i][1]), 4),
                         " | ".join(f"{P[q]['name']} {s:.2f}" for s, q in near)))
        for k, g in enumerate(groups):
            gy = oy + 30 + k * 18
            b.add(f'<circle cx="{ox + PW - 110}" cy="{gy - 4}" r="5" fill="{color[g]}"/>')
            b.text(ox + PW - 100, gy, cats.get(g, {}).get("name", g), 11, 400, SOFT)
    cvs.write_csv("out/product_vectors.csv", ["id", "type", "name", "category2", "x", "y", "nearest3"], rows)
    out = Path(cvs.ROOT) / "docs/product_map.svg"
    out.write_text(b.svg(WIDTH, 96 + 2 * (PH + G)), encoding="utf-8")
    print(out, "· out/product_vectors.csv")


if __name__ == "__main__":
    main()
