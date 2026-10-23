"""3D 그래프 회전 GIF → docs/product_graph_3d.gif (README용 미리보기. 조작은 docs/product_graph_3d.html)
배치·투영은 graph3d_template.html 과 같은 규칙(종류마다 정사면체 꼭짓점으로 당김 + 반발 + 간선 스프링).
python tools/graph3d_gif.py"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cvs  # noqa: E402
from product_graph3d import build_data  # noqa: E402

COLOR = {"snack": (240, 166, 60), "ramen": (239, 95, 86), "beverage": (78, 168, 242), "household": (121, 201, 122)}
TYPE_KO = {"snack": "과자", "ramen": "라면", "beverage": "음료", "household": "생필품"}
ANCHOR = {"snack": (1, 1, 1), "ramen": (-1, -1, 1), "beverage": (-1, 1, -1), "household": (1, -1, -1)}
BG, W, H, SS, FRAMES = (22, 23, 29), 960, 640, 2, 60
FONT = "C:/Windows/Fonts/malgun.ttf"


def layout(nodes, links):
    n, rng = len(nodes), np.random.default_rng(7)
    anchor = np.array([ANCHOR[x["type"]] for x in nodes], float)
    same = np.array([[a["type"] == b["type"] for b in nodes] for a in nodes])
    pos = anchor * 120 + rng.uniform(-40, 40, (n, 3))
    vel = np.zeros((n, 3))
    li = np.array([(x["a"], x["b"]) for x in links]); lw = np.array([x["w"] for x in links])
    for it in range(320):
        alpha = 1 - it / 400
        d = pos[:, None, :] - pos[None, :, :]
        d2 = (d ** 2).sum(-1) + 0.01
        f = np.where(same, 900, 2200) * alpha / d2 * (d2 <= 160000)
        np.fill_diagonal(f, 0)
        vel += (d * f[:, :, None]).sum(1)
        dl = pos[li[:, 1]] - pos[li[:, 0]]; dist = np.linalg.norm(dl, axis=1) + 1e-9
        k = ((dist - (34 - 14 * lw)) / dist * 0.06 * alpha * (0.4 + lw))[:, None] * dl
        np.add.at(vel, li[:, 0], k); np.add.at(vel, li[:, 1], -k)
        vel += (anchor * 210 - pos) * 0.012 * alpha
        vel *= 0.6; pos += vel
    pos -= pos.mean(0)
    return pos


def main():
    d = build_data()
    nodes = d["nodes"]; idx = {x["id"]: k for k, x in enumerate(nodes)}
    links = [{"a": idx[x["s"]], "b": idx[x["t"]], "w": x["w"]} for x in d["links"]]
    deg = np.zeros(len(nodes)); [deg.__setitem__(i, deg[i] + 1) for x in links for i in (x["a"], x["b"])]
    pos = layout(nodes, links)
    R = np.sort(np.linalg.norm(pos, axis=1))[int(len(nodes) * 0.9)]
    font = ImageFont.truetype(FONT, 12 * SS); title = ImageFont.truetype(FONT, 19 * SS); small = ImageFont.truetype(FONT, 13 * SS)
    frames = []
    for f in range(FRAMES):
        yaw, pitch = 0.6 + 2 * np.pi * f / FRAMES, -0.35
        x1 = pos[:, 0] * np.cos(yaw) - pos[:, 2] * np.sin(yaw); z1 = pos[:, 0] * np.sin(yaw) + pos[:, 2] * np.cos(yaw)
        y2 = pos[:, 1] * np.cos(pitch) - z1 * np.sin(pitch); z2 = pos[:, 1] * np.sin(pitch) + z1 * np.cos(pitch)
        cam = R * 3; near = cam / (cam + z2); s = near * min(W, H) * 0.47 / R
        sx, sy = (W / 2 + x1 * s) * SS, (H / 2 + 18 + y2 * s) * SS
        img = Image.new("RGB", (W * SS, H * SS), BG); dr = ImageDraw.Draw(img, "RGBA")
        for x in links:
            a, b = x["a"], x["b"]
            op = int(255 * (0.08 + 0.22 * x["w"]) * min(1, max(0.3, (near[a] + near[b]) / 2)))
            dr.line([(sx[a], sy[a]), (sx[b], sy[b])], fill=(170, 176, 196, op), width=SS)
        for k in np.argsort(-z2):  # 먼 것부터
            r = min(14, (2.5 + np.sqrt(deg[k]) * 1.2) * near[k]) * SS
            c = COLOR[nodes[k]["type"]]; op = int(255 * min(1, 0.35 + near[k] * 0.6))
            dr.ellipse([sx[k] - r, sy[k] - r, sx[k] + r, sy[k] + r], fill=(*c, op))
        boxes = []
        for k in np.argsort(z2):  # 이름은 가까운 것부터, 앞서 쓴 이름과 겹치면 생략
            if near[k] < 1.0 or deg[k] < 3:
                continue
            r = min(14, (2.5 + np.sqrt(deg[k]) * 1.2) * near[k]) * SS
            x0, y0 = sx[k] + r + 4 * SS, sy[k] - 8 * SS
            box = (x0, y0, x0 + dr.textlength(nodes[k]["name"], font=font), y0 + 16 * SS)
            if any(box[0] < q[2] and q[0] < box[2] and box[1] < q[3] and q[1] < box[3] for q in boxes):
                continue
            boxes.append(box)
            dr.text((x0, y0), nodes[k]["name"], font=font, fill=(230, 231, 236, int(255 * min(1, (near[k] - 0.97) * 6))))
        dr.text((20 * SS, 16 * SS), f"품절 대체 그래프 · 상품 {len(nodes)} · 간선 {len(links)}", font=title, fill=(230, 231, 236))
        lx = 20 * SS
        for t, ko in TYPE_KO.items():
            dr.ellipse([lx, 52 * SS, lx + 10 * SS, 62 * SS], fill=COLOR[t])
            dr.text((lx + 15 * SS, 48 * SS), ko, font=small, fill=(154, 159, 174)); lx += 70 * SS
        dr.text((20 * SS, (H - 28) * SS), "클릭하면 직접 돌려 볼 수 있는 3D 페이지가 열립니다", font=small, fill=(154, 159, 174))
        frames.append(img.resize((W, H), Image.LANCZOS))
    # 모든 프레임에 같은 팔레트: 배경·글자 회색과 종류 색을 배경 위 여러 농도로 섞은 색 — 프레임마다 색이 바뀌지 않게
    cols = [BG, (230, 231, 236), (154, 159, 174)] + [tuple(int(BG[i] + (c[i] - BG[i]) * t) for i in range(3))
            for c in [*COLOR.values(), (170, 176, 196)] for t in np.linspace(0.08, 1, 40)]
    pal = Image.new("P", (1, 1)); pal.putpalette([v for c in cols[:256] for v in c] + [0] * (768 - 3 * len(cols[:256])))
    frames = [fr.quantize(palette=pal, dither=Image.NONE) for fr in frames]
    out = Path(cvs.ROOT) / "docs/product_graph_3d.gif"
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=85, loop=0, optimize=True)
    print(out, f"{out.stat().st_size / 1e6:.1f}MB")


if __name__ == "__main__":
    main()
