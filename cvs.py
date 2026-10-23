"""편의점 상품 텍소노미 → 대체/보완재 점수 → 인간 검수 루프 → 그래프DB(Neo4j) 적재.

  python cvs.py validate          정의 누락·처음 보는 속성값 점검 (review/new_values.csv)
  python cvs.py sweep             뎁스·가중치 튜닝 → config/weights.yaml
  python cvs.py review export     검수할 대체재 후보 → review/queue.csv
  python cvs.py review import     검수 끝난 queue → review/gold.csv
  python cvs.py graph [--push]    out/graph.cypher, out/edges.csv 생성 (--push 면 Neo4j에 바로 적재)
"""
import argparse
import csv
import itertools
import json
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
POS, NEG = {"O", "1", "Y"}, {"X", "0", "N"}


def read_yaml(rel):
    return yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))


def read_csv(rel):
    with open(ROOT / rel, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(rel, header, rows):
    with open(ROOT / rel, "w", encoding="utf-8-sig", newline="") as f:  # BOM: 엑셀에서 한글 안 깨지게
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


# ── 텍소노미 · 데이터 ──────────────────────────────────────────────

def load_taxonomy():
    attrs = {a["id"]: a for a in read_yaml("taxonomy/schema.yaml")["attributes"]}
    for a in attrs.values():
        a.setdefault("values", {})
        a["syn"] = {s: v for v, d in a["values"].items() for s in (d or {}).get("synonyms", [])}
    cats = {}

    def walk(nodes, parent):
        for n in nodes:
            cats[n["id"]] = {**n, "parent": parent}
            walk(n.get("children", []), n["id"])

    walk(read_yaml("taxonomy/categories.yaml"), None)
    return attrs, cats


def normalize(a, raw, cats):
    """원본 표기 → 표준 값. 모르는 값이면 None (docs/unknown_attribute_policy.md)."""
    if a["type"] == "open":
        return raw
    if a["type"] == "tree":
        return raw if raw in cats and not cats[raw].get("children") else None
    return raw if raw in a["values"] else a["syn"].get(raw)


def load_products(attrs, cats):
    products, issues = {}, []
    for row in read_csv("data/products.csv"):
        p = {"id": row["id"], "type": row["type"], "name": row["name"], "attrs": {}}
        for aid, a in attrs.items():
            if p["type"] not in a["applies_to"]:
                continue
            raws = [r.strip() for r in (row.get(aid) or "").split("|") if r.strip()]
            if not raws:
                issues.append((p["id"], p["name"], aid, "", "missing"))
                continue
            known = []
            for r in raws:
                v = normalize(a, r, cats)
                if v is None:
                    issues.append((p["id"], p["name"], aid, r, "unknown_value"))
                else:
                    known.append(v)
            if known:
                p["attrs"][aid] = frozenset(known) if a["type"] == "multi" else known[0]
        products[p["id"]] = p
    return products, issues


def load_weights():
    path = ROOT / "config/weights.yaml"
    if path.exists():
        return read_yaml("config/weights.yaml")
    return {"max_depth": 4, "depth_mult": {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0}}


def load_gold():
    return {(r["anchor"], r["candidate"]): int(r["label"]) for r in read_csv("review/gold.csv")}


def label_of(gold, a, b):
    return gold.get((a, b), gold.get((b, a)))


# ── 점수 ──────────────────────────────────────────────────────────

def attr_sim(a, x, y):
    t = a["type"]
    if t == "multi":
        return len(x & y) / len(x | y)
    if t == "ordinal":
        order = list(a["values"])
        return 1 - abs(order.index(x) - order.index(y)) / (len(order) - 1)
    if t == "tree":  # 공통 조상 깊이 / 더 깊은 쪽 깊이
        px, py = x.split("."), y.split(".")
        common = sum(1 for _ in itertools.takewhile(lambda t: t[0] == t[1], zip(px, py)))
        return common / max(len(px), len(py))
    return float(x == y)


def score(p, q, attrs, w):
    """대체재 점수 0~1. 같은 type끼리만.

    둘 다 값이 있는 속성만 비교하고 가중치를 재정규화해 결측이 곧 감점이 되지 않게 한다.
    대신 비교한 가중치가 비교 가능했던 가중치의 70% 미만이면 그 비율만큼 깎는다(근거 부족 할인).
    """
    if p["type"] != q["type"]:
        return 0.0
    num = den = possible = 0.0
    for aid, a in attrs.items():
        if a["depth"] > w["max_depth"] or p["type"] not in a["applies_to"]:
            continue
        wt = a["weight"] * w["depth_mult"][a["depth"]]
        possible += wt
        if aid in p["attrs"] and aid in q["attrs"]:
            num += wt * attr_sim(a, p["attrs"][aid], q["attrs"][aid])
            den += wt
    if not den:
        return 0.0
    return num / den * min(1.0, den / (0.7 * possible))


def ranked(products, attrs, w, pid):
    p = products[pid]
    cands = [(score(p, q, attrs, w), q["id"]) for q in products.values()
             if q["id"] != pid and q["type"] == p["type"]]
    return sorted(cands, reverse=True)


def evaluate(products, attrs, w, gold):
    """AUC: 라벨된 쌍에서 정답(대체 O)이 오답(대체 X)보다 점수가 높은 비율.
    MRR: 정답 후보가 같은 type 전체 후보 중 몇 등인지의 역수 평균(다른 정답은 순위 계산에서 제외)."""
    s = {pair: score(products[pair[0]], products[pair[1]], attrs, w) for pair in gold}
    pos = [v for k, v in s.items() if gold[k] == 1]
    neg = [v for k, v in s.items() if gold[k] == 0]
    auc = sum((x > y) + 0.5 * (x == y) for x in pos for y in neg) / (len(pos) * len(neg))
    rr = []
    for (a, b), lab in gold.items():
        if lab != 1:
            continue
        others = [sc for sc, q in ranked(products, attrs, w, a)
                  if q != b and label_of(gold, a, q) != 1]
        rr.append(1 / (1 + sum(o > s[(a, b)] for o in others)))
    return auc, sum(rr) / len(rr)


# ── 명령 ──────────────────────────────────────────────────────────

def cmd_validate(args):
    attrs, cats = load_taxonomy()
    gaps = [f"카테고리 {c}" for c, n in cats.items() if not n.get("definition")]
    for aid, a in attrs.items():
        if not a.get("definition"):
            gaps.append(f"속성 {aid}")
        gaps += [f"값 {aid}={v}" for v, d in a["values"].items() if not (d or {}).get("definition")]
    products, issues = load_products(attrs, cats)
    write_csv("review/new_values.csv", ["product", "name", "attr", "raw_value", "kind", "decision"],
              [(*i, "") for i in issues])
    print(f"상품 {len(products)}개 / 카테고리 {len(cats)}개 / 속성 {len(attrs)}개")
    print(f"정의 누락 {len(gaps)}건" + ("".join(f"\n  - {g}" for g in gaps)))
    unknown = [i for i in issues if i[4] == "unknown_value"]
    print(f"처음 보는 값 {len(unknown)}건, 결측 {len(issues) - len(unknown)}건 → review/new_values.csv")
    for i in unknown:
        print(f"  - {i[1]} · {i[2]} = '{i[3]}'")
    return 1 if gaps else 0


def cmd_sweep(args):
    attrs, cats = load_taxonomy()
    products, _ = load_products(attrs, cats)
    gold = load_gold()
    rows = []
    for d in range(1, 5):
        best = None
        for mult in itertools.product([0.25, 0.5, 1.0, 2.0], repeat=d - 1):  # 뎁스1 배수는 1로 고정(스케일 기준)
            w = {"max_depth": d, "depth_mult": {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0}}
            w["depth_mult"].update({i + 2: m for i, m in enumerate(mult)})
            auc, mrr = evaluate(products, attrs, w, gold)
            obj = (auc + mrr) / 2
            if best is None or obj > best[0] + 1e-9:
                best = (obj, auc, mrr, w)
        rows.append(best)
        print(f"뎁스≤{d}  AUC {best[1]:.3f}  MRR {best[2]:.3f}  종합 {best[0]:.3f}  배수 {best[3]['depth_mult']}")
    top = max(r[0] for r in rows)
    obj, auc, mrr, w = next(r for r in rows if r[0] >= top - args.tol)  # 거의 같으면 얕은 뎁스
    w["metrics"] = {"auc": round(auc, 4), "mrr": round(mrr, 4), "objective": round(obj, 4),
                    "gold_pairs": len(gold)}
    header = "# python cvs.py sweep 이 생성. 직접 고치지 말고 검수셋을 늘린 뒤 다시 sweep 할 것.\n"
    (ROOT / "config/weights.yaml").write_text(
        header + yaml.safe_dump(w, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"→ 선택: 뎁스≤{w['max_depth']} (최고 대비 {args.tol} 이내면 얕은 쪽) · config/weights.yaml 저장")


def cmd_review(args):
    attrs, cats = load_taxonomy()
    products, _ = load_products(attrs, cats)
    header = ["anchor", "anchor_name", "candidate", "candidate_name", "score", "verdict", "note"]
    if args.action == "export":
        gold, w, seen, rows = load_gold(), load_weights(), set(), []
        for pid in products:
            for sc, q in ranked(products, attrs, w, pid)[:args.k]:
                pair = frozenset((pid, q))
                if label_of(gold, pid, q) is None and pair not in seen:
                    seen.add(pair)
                    rows.append((pid, products[pid]["name"], q, products[q]["name"], f"{sc:.3f}", "", ""))
        write_csv("review/queue.csv", header, rows)
        print(f"검수 대기 {len(rows)}쌍 → review/queue.csv (verdict 칸에 O=대체 가능 / X=아님)")
        return
    queue = read_csv("review/queue.csv")
    done = [r for r in queue if r["verdict"].strip().upper() in POS | NEG]
    with open(ROOT / "review/gold.csv", "a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(
            (r["anchor"], r["candidate"], int(r["verdict"].strip().upper() in POS), "review", r["note"])
            for r in done)
    write_csv("review/queue.csv", header, [[r[h] for h in header] for r in queue if r not in done])
    print(f"{len(done)}쌍 gold.csv 반영, 남은 대기 {len(queue) - len(done)}쌍. 다음: python cvs.py sweep")


def matches(p, cond):
    for key, want in cond.items():
        want = want if isinstance(want, list) else [want]
        if key == "type":
            if p["type"] not in want:
                return False
            continue
        v = p["attrs"].get(key)
        have = v if isinstance(v, frozenset) else {v}
        if not have & set(want):
            return False
    return True


def edge_strength(model, label):
    """간선 강도. 검수 O = 0.5 바닥 보장, 검수 X = 간선 삭제, 미검수 = 모델 점수 20% 할인."""
    if label == 1:
        return round(0.5 + 0.5 * model, 3), "approved"
    if label == 0:
        return None, "rejected"
    return round(0.8 * model, 3), "unreviewed"


def build_edges(products, attrs, w, gold, k, min_score):
    subs = {}
    pairs = {tuple(sorted(pair)) for pair, lab in gold.items() if lab == 1}
    for pid in products:
        pairs |= {tuple(sorted((pid, q))) for sc, q in ranked(products, attrs, w, pid)[:k] if sc >= min_score}
    for a, b in pairs:
        model = score(products[a], products[b], attrs, w)
        strength, review = edge_strength(model, label_of(gold, a, b))
        if strength is not None:
            subs[(a, b)] = {"strength": strength, "model_score": round(model, 3), "review": review}
    comps = {}
    rules = read_yaml("config/complement_rules.yaml")
    for r in rules.get("pairs", []):
        comps[(r["a"], r["b"])] = {"strength": r["strength"], "reason": r["reason"], "source": "pair"}
    for r in rules.get("rules", []):
        for a in (p for p in products.values() if matches(p, r["a"])):
            for b in (p for p in products.values() if matches(p, r["b"]) and p["id"] != a["id"]):
                cur = comps.get((a["id"], b["id"]))
                if cur is None or cur["strength"] < r["strength"]:
                    comps[(a["id"], b["id"])] = {"strength": r["strength"], "reason": r["name"], "source": "rule"}
    return subs, comps


def to_cypher(products, attrs, cats, subs, comps, w):
    q = lambda v: json.dumps(v, ensure_ascii=False)  # Cypher 문자열 리터럴로도 유효
    out = [
        "CREATE CONSTRAINT product_id IF NOT EXISTS FOR (n:Product) REQUIRE n.id IS UNIQUE;",
        "CREATE CONSTRAINT category_id IF NOT EXISTS FOR (n:Category) REQUIRE n.id IS UNIQUE;",
        "CREATE CONSTRAINT attribute_id IF NOT EXISTS FOR (n:Attribute) REQUIRE n.id IS UNIQUE;",
        "CREATE CONSTRAINT attrvalue_key IF NOT EXISTS FOR (n:AttrValue) REQUIRE n.key IS UNIQUE;",
        "// 재적재 시 이전 추천 간선만 지운다(노드·다른 관계는 그대로).",
        "MATCH ()-[r:SUBSTITUTE|COMPLEMENT]->() DELETE r;",
    ]
    for c, n in cats.items():
        out.append(f"MERGE (c:Category {{id: {q(c)}}}) SET c.name = {q(n['name'])}, c.definition = {q(n['definition'])};")
        if n["parent"]:
            out.append(f"MATCH (c:Category {{id: {q(c)}}}), (p:Category {{id: {q(n['parent'])}}}) MERGE (c)-[:SUBCATEGORY_OF]->(p);")
    for aid, a in attrs.items():
        out.append(f"MERGE (a:Attribute {{id: {q(aid)}}}) SET a.name = {q(a['name'])}, a.definition = {q(a['definition'])}, "
                   f"a.depth = {a['depth']}, a.type = {q(a['type'])}, a.weight = {a['weight']}, a.applies_to = {q(a['applies_to'])};")
    values = {(aid, v): (d or {}).get("definition", "") for aid, a in attrs.items() for v, d in a["values"].items()}
    values |= {(aid, v): "" for p in products.values() for aid, v in p["attrs"].items() if attrs[aid]["type"] == "open"}
    for (aid, v), d in values.items():
        key = f"{aid}={v}"
        out.append(f"MERGE (v:AttrValue {{key: {q(key)}}}) SET v.attr = {q(aid)}, v.value = {q(v)}, v.definition = {q(d)};")
        out.append(f"MATCH (a:Attribute {{id: {q(aid)}}}), (v:AttrValue {{key: {q(key)}}}) MERGE (a)-[:HAS_VALUE]->(v);")
    for p in products.values():
        out.append(f"MERGE (p:Product {{id: {q(p['id'])}}}) SET p.name = {q(p['name'])}, p.type = {q(p['type'])};")
        for aid, v in p["attrs"].items():
            if attrs[aid]["type"] == "tree":
                out.append(f"MATCH (p:Product {{id: {q(p['id'])}}}), (c:Category {{id: {q(v)}}}) MERGE (p)-[:IN_CATEGORY]->(c);")
                continue
            for x in (sorted(v) if isinstance(v, frozenset) else [v]):
                out.append(f"MATCH (p:Product {{id: {q(p['id'])}}}), (v:AttrValue {{key: {q(f'{aid}={x}')}}}) MERGE (p)-[:HAS_ATTR]->(v);")
    for (a, b), e in subs.items():
        out.append(f"MATCH (a:Product {{id: {q(a)}}}), (b:Product {{id: {q(b)}}}) CREATE (a)-[:SUBSTITUTE {{strength: {e['strength']}, "
                   f"model_score: {e['model_score']}, review: {q(e['review'])}, depth: {w['max_depth']}}}]->(b);")
    for (a, b), e in comps.items():
        out.append(f"MATCH (a:Product {{id: {q(a)}}}), (b:Product {{id: {q(b)}}}) CREATE (a)-[:COMPLEMENT {{strength: {e['strength']}, "
                   f"reason: {q(e['reason'])}, source: {q(e['source'])}}}]->(b);")
    return out


def cmd_graph(args):
    attrs, cats = load_taxonomy()
    products, _ = load_products(attrs, cats)
    w = load_weights()
    subs, comps = build_edges(products, attrs, w, load_gold(), args.k, args.min_score)
    stmts = to_cypher(products, attrs, cats, subs, comps, w)
    (ROOT / "out").mkdir(exist_ok=True)
    (ROOT / "out/graph.cypher").write_text("\n".join(stmts) + "\n", encoding="utf-8")
    name = lambda i: products[i]["name"]
    write_csv("out/edges.csv", ["rel", "a", "a_name", "b", "b_name", "strength", "model_score", "review_or_reason"],
              [("SUBSTITUTE", a, name(a), b, name(b), e["strength"], e["model_score"], e["review"])
               for (a, b), e in sorted(subs.items(), key=lambda x: -x[1]["strength"])]
              + [("COMPLEMENT", a, name(a), b, name(b), e["strength"], "", e["reason"])
                 for (a, b), e in sorted(comps.items(), key=lambda x: -x[1]["strength"])])
    by = lambda key: sum(e["review"] == key for e in subs.values())
    print(f"대체 간선 {len(subs)} (검수O {by('approved')} / 미검수 {by('unreviewed')}) · 보완 간선 {len(comps)}")
    print(f"→ out/graph.cypher ({len(stmts)}문), out/edges.csv")
    if args.push:
        from neo4j import GraphDatabase  # pip install neo4j
        uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        auth = (os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"])
        with GraphDatabase.driver(uri, auth=auth) as drv, drv.session() as s:
            for st in stmts:
                if not st.startswith("//"):
                    s.run(st)
        print(f"Neo4j 적재 완료: {uri}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate").set_defaults(fn=cmd_validate)
    sp = sub.add_parser("sweep")
    sp.add_argument("--tol", type=float, default=0.01, help="최고 성능 대비 이 차이 이내면 더 얕은 뎁스 선택")
    sp.set_defaults(fn=cmd_sweep)
    rp = sub.add_parser("review")
    rp.add_argument("action", choices=["export", "import"])
    rp.add_argument("--k", type=int, default=3, help="상품당 검수 후보 수")
    rp.set_defaults(fn=cmd_review)
    gp = sub.add_parser("graph")
    gp.add_argument("--k", type=int, default=5, help="상품당 대체 간선 최대 수")
    gp.add_argument("--min-score", type=float, default=0.55, help="미검수 대체 간선의 최소 모델 점수")
    gp.add_argument("--push", action="store_true", help="NEO4J_URI/USER/PASSWORD 로 바로 적재")
    gp.set_defaults(fn=cmd_graph)
    args = ap.parse_args()
    sys.exit(args.fn(args) or 0)


if __name__ == "__main__":
    main()
