"""상품 3D 그래프 → docs/product_graph_3d.html (브라우저로 열면 돌려 볼 수 있는 옵시디언식 그래프)
점 = 상품(크기 = 대체로 이어진 수), 선 = 대체 간선(진할수록 강함). 점을 누르면 품절 시 1~3순위가 나온다.
python tools/product_graph3d.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cvs  # noqa: E402
from board_svg import TYPE_KO  # noqa: E402


def build_data():
    """3D 그래프 데이터: 상품 점(이름·종류·카테고리·품절 시 1~3순위)과 대체 간선(양방향은 강한 쪽 하나)."""
    attrs, cats, P, _, policy, _ = cvs.load_context()
    subs, *_ = cvs.build_edges(P, attrs, policy, cvs.load_weights(), cvs.load_gold(), 5, 0.55)
    pair = {}  # 방향 있는 간선을 한 선으로 — 양쪽 중 강한 쪽
    for (a, c), e in subs.items():
        if e["review"] != "unreviewed":
            k = tuple(sorted((a, c)))
            pair[k] = max(pair.get(k, 0), e["strength"])
    links = [{"s": a, "t": c, "w": round(w, 3)} for (a, c), w in pair.items()]
    nodes = []
    for i, p in P.items():
        g = ".".join(p["attrs"]["category"].split(".")[:2])
        recs = cvs.oos_recommend(i, P, attrs, cats, policy, subs)
        nodes.append({"id": i, "name": p["name"], "type": p["type"], "typeKo": TYPE_KO[p["type"]],
                      "cat": cats.get(g, {}).get("name", g), "recs": [P[b]["name"] for b, *_ in recs]})
    return {"nodes": nodes, "links": links}


def main():
    d = build_data()
    nodes, links = d["nodes"], d["links"]
    data = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
    tpl = (Path(__file__).parent / "graph3d_template.html").read_text(encoding="utf-8")
    out = Path(cvs.ROOT) / "docs/product_graph_3d.html"
    out.write_text(tpl.replace("/*DATA*/null", data), encoding="utf-8")
    print(out, f"· 상품 {len(nodes)} · 간선 {len(links)}")


if __name__ == "__main__":
    main()
