"""우리동네GS 추천용 온톨로지: 텍소노미 → TPO 추론 → 정책(상품간·규격·속성·아이덴티티)으로 튀는 추천 차단
→ 대체재·보완재·콤보·트렌드 → 인간 검수 루프 → 그래프DB(Neo4j) 적재.

  python cvs.py validate          정의 누락·처음 보는 속성값 점검 (review/new_values.csv)
  python cvs.py sweep             뎁스·가중치 튜닝 → config/weights.yaml
  python cvs.py review export     검수할 대체재 후보 → review/queue.csv
  python cvs.py review import     검수 끝난 queue → review/gold.csv
  python cvs.py graph [--push]    out/graph.cypher, out/edges.csv, out/audit.md 생성 (--push 면 Neo4j에 바로 적재)
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
                if not a.get("optional"):  # optional 속성의 빈 칸은 '해당 없음'
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


DEFAULT_W = {"max_depth": 4, "depth_mult": {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0}}


def load_weights():
    """type별 가중치 {snack: {max_depth, depth_mult}, ...}. 카테고리마다 쓰는 속성이 달라 뎁스도 따로 정한다."""
    path = ROOT / "config/weights.yaml"
    return read_yaml("config/weights.yaml") if path.exists() else {}


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
    w = w.get(p["type"], DEFAULT_W)
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


def evaluate(products, attrs, w, gold, blocked):
    """정책까지 적용한 최종 추천 기준으로 잰다(정책 차단 쌍 = 점수 0).
    AUC: 라벨된 쌍에서 정답(대체 O)이 오답(대체 X)보다 점수가 높은 비율.
    MRR: 정답 후보가 같은 type 전체 후보 중 몇 등인지의 역수 평균(다른 정답은 순위 계산에서 제외)."""
    final = lambda a, b: 0.0 if frozenset((a, b)) in blocked else score(products[a], products[b], attrs, w)
    s = {pair: final(*pair) for pair in gold}
    pos = [v for k, v in s.items() if gold[k] == 1]
    neg = [v for k, v in s.items() if gold[k] == 0]
    auc = sum((x > y) + 0.5 * (x == y) for x in pos for y in neg) / (len(pos) * len(neg))
    rr = []
    for (a, b), lab in gold.items():
        if lab != 1:
            continue
        others = [final(a, q) for _, q in ranked(products, attrs, w, a)
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
    attrs, cats, products, _, policy, _ = load_context()
    gold = load_gold()
    blocked = {frozenset((p["id"], q["id"])) for p, q in itertools.combinations(products.values(), 2)
               if p["type"] == q["type"] and substitute_violations(p, q, attrs, policy)}
    result = {}
    for typ in dict.fromkeys(p["type"] for p in products.values()):
        g = {k: v for k, v in gold.items() if products[k[0]]["type"] == typ}
        if not any(g.values()) or all(g.values()):
            print(f"[{typ}] 정답셋에 O/X가 둘 다 있어야 잴 수 있음 — 기본값 사용")
            continue
        rows = []
        for d in range(1, 5):
            best = None
            for mult in itertools.product([0.25, 0.5, 1.0, 2.0], repeat=d - 1):  # 뎁스1 배수는 1로 고정(스케일 기준)
                w = {"max_depth": d, "depth_mult": {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0}}
                w["depth_mult"].update({i + 2: m for i, m in enumerate(mult)})
                auc, mrr = evaluate(products, attrs, {typ: w}, g, blocked)
                obj = (auc + mrr) / 2
                if best is None or obj > best[0] + 1e-9:
                    best = (obj, auc, mrr, w)
            rows.append(best)
            print(f"[{typ}] 뎁스≤{d}  AUC {best[1]:.3f}  MRR {best[2]:.3f}  종합 {best[0]:.3f}  배수 {best[3]['depth_mult']}")
        top = max(r[0] for r in rows)
        obj, auc, mrr, w = next(r for r in rows if r[0] >= top - args.tol)  # 거의 같으면 얕은 뎁스
        w["metrics"] = {"auc": round(auc, 4), "mrr": round(mrr, 4), "objective": round(obj, 4), "gold_pairs": len(g)}
        result[typ] = w
        print(f"[{typ}] → 뎁스≤{w['max_depth']} 선택\n")
    header = "# python cvs.py sweep 이 생성(type별 뎁스·배수). 직접 고치지 말고 검수셋을 늘린 뒤 다시 sweep 할 것.\n"
    (ROOT / "config/weights.yaml").write_text(
        header + yaml.safe_dump(result, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"config/weights.yaml 저장 (최고 대비 {args.tol} 이내면 얕은 뎁스)")


def cmd_review(args):
    attrs, cats, products, _, policy, _ = load_context()
    header = ["anchor", "anchor_name", "candidate", "candidate_name", "score", "verdict", "note"]
    if args.action == "export":
        gold, w, seen, rows = load_gold(), load_weights(), set(), []
        for pid in products:
            allowed = [(sc, q) for sc, q in ranked(products, attrs, w, pid)
                       if not substitute_violations(products[pid], products[q], attrs, policy)]  # 정책 차단 쌍은 검수할 필요 없음
            for sc, q in allowed[:args.k]:
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
    """조건 {속성: 값 또는 [값...]} AND 결합. 카테고리는 접두어 일치(CHO → CHO.BAR)."""
    for key, want in cond.items():
        want = want if isinstance(want, list) else [want]
        if key == "type":
            if p["type"] not in want:
                return False
            continue
        v = p["attrs"].get(key)
        have = v if isinstance(v, frozenset) else {v}
        if not any(h == x or (isinstance(h, str) and h.startswith(f"{x}.")) for h in have for x in want):
            return False
    return True


# ── 온톨로지: TPO 추론 · 정책 ─────────────────────────────────────

def load_ontology():
    return read_yaml("ontology/policy.yaml"), read_yaml("ontology/tpo.yaml")


def infer_tpo(products, tpo):
    """속성 → TPO 추론. exclude 는 어떤 규칙에서 붙었든 지운다."""
    for p in products.values():
        fits, excl = {}, {}
        for r in tpo["rules"]:
            if matches(p, r["when"]):
                for dim, vals in r.get("then", {}).items():
                    fits.setdefault(dim, set()).update(vals)
                for dim, vals in r.get("exclude", {}).items():
                    excl.setdefault(dim, set()).update(vals)
        p["tpo"] = {d: v - excl.get(d, set()) for d, v in fits.items()}
        p["tpo_excl"] = excl


def fits_scene(p, scene_tpo):
    """장면 TPO에 맞는가. 상품에 그 차원이 없으면 중립(통과), 제외된 값이면 탈락."""
    for dim, val in scene_tpo.items():
        if val in p["tpo_excl"].get(dim, ()) or (dim in p["tpo"] and val not in p["tpo"][dim]):
            return False
    return True


def cat_prefix(p, level):
    return ".".join(p["attrs"].get("category", "").split(".")[:level])


def as_set(v):
    return v if isinstance(v, frozenset) else {v}


def identity(p, policy):
    rule = policy["identity"][p["type"]]
    key = [p["type"]]
    if "category_level" in rule:
        key.append(cat_prefix(p, rule["category_level"]))
    key += [p["attrs"].get(a) for a in rule.get("attrs", [])]
    return tuple(key)


def substitute_violations(p, q, attrs, policy):
    """대체재 하드 정책 위반 사유 목록. 비어 있으면 통과. 결측 값은 판단 보류(위반 아님)."""
    if p["type"] != q["type"]:
        return ["상품간: type 다름"]
    rule, out = policy["substitute"][p["type"]], []
    if "category_level" in rule and cat_prefix(p, rule["category_level"]) != cat_prefix(q, rule["category_level"]):
        out.append(f"아이덴티티: 카테고리 {rule['category_level']}단계 다름")
    for aid in rule.get("same", []):
        a, b = p["attrs"].get(aid), q["attrs"].get(aid)
        if a is not None and b is not None and a != b:
            out.append(f"아이덴티티: {attrs[aid]['name']} 다름({a}/{b})")
    for aid in rule.get("share", []):
        a, b = p["attrs"].get(aid), q["attrs"].get(aid)
        if a is not None and b is not None and not as_set(a) & as_set(b):
            out.append(f"상품간: {attrs[aid]['name']} 겹침 없음")
    for aid, gap in policy["substitute"]["max_gap"].items():
        a, b = p["attrs"].get(aid), q["attrs"].get(aid)
        if a is not None and b is not None:
            order = list(attrs[aid]["values"])
            if abs(order.index(a) - order.index(b)) > gap:
                out.append(f"규격: {attrs[aid]['name']} {a}→{b}")
    return out


def restricted_violations(p, q, policy):
    """제한 상품(주류 등)을 금지 대상(어린이 상품 등)과 묶으면 위반."""
    out = []
    for r in policy.get("restricted", []):
        for x, y in ((p, q), (q, p)):
            if matches(x, r["items"]) and matches(y, r["never_with"]):
                out.append(f"상품간: {r['name']}")
                break
    return out


def complement_violations(p, q, policy):
    rule, out = policy["complement"], restricted_violations(p, q, policy)
    if rule.get("forbid_same_identity") and identity(p, policy) == identity(q, policy):
        out.append("아이덴티티: 같은 정체성(보완 아닌 대체)")
    for dim in rule.get("require_overlap", []):
        if dim in p["tpo"] and dim in q["tpo"] and not p["tpo"][dim] & q["tpo"][dim]:
            out.append(f"TPO: {dim} 겹침 없음")
    return out


def load_context():
    attrs, cats = load_taxonomy()
    products, issues = load_products(attrs, cats)
    policy, tpo = load_ontology()
    infer_tpo(products, tpo)
    return attrs, cats, products, issues, policy, tpo


# ── 간선 ──────────────────────────────────────────────────────────

def edge_strength(model, label):
    """간선 강도. 검수 O = 0.5 바닥 보장, 검수 X = 간선 삭제, 미검수 = 모델 점수 20% 할인."""
    if label == 1:
        return round(0.5 + 0.5 * model, 3), "approved"
    if label == 0:
        return None, "rejected"
    return round(0.8 * model, 3), "unreviewed"


def build_edges(products, attrs, policy, w, gold, k, min_score):
    """반환: 대체 간선, 보완 간선, 정책 차단 목록(튀는 추천), 정책 충돌(검수 O인데 정책 위반)."""
    subs, blocked, conflicts = {}, [], []
    pairs = {tuple(sorted(pair)) for pair, lab in gold.items() if lab == 1}
    for pid in products:
        pairs |= {tuple(sorted((pid, q))) for sc, q in ranked(products, attrs, w, pid)[:k] if sc >= min_score}
    for a, b in sorted(pairs):
        model = score(products[a], products[b], attrs, w)
        label = label_of(gold, a, b)
        why = substitute_violations(products[a], products[b], attrs, policy)
        if why:
            (conflicts if label == 1 else blocked).append(("SUBSTITUTE", a, b, round(model, 3), " · ".join(why)))
            continue
        strength, review = edge_strength(model, label)
        if strength is not None:
            subs[(a, b)] = {"strength": strength, "model_score": round(model, 3), "review": review}
    comps, cand = {}, {}
    rule = policy["complement"]
    for r in rule.get("pairs", []):
        cand[(r["a"], r["b"])] = {"strength": r["strength"], "reason": r["reason"], "source": "pair"}
    for r in rule.get("rules", []):
        for a in (p for p in products.values() if matches(p, r["a"])):
            for b in (p for p in products.values() if matches(p, r["b"]) and p["id"] != a["id"]):
                cur = cand.get((a["id"], b["id"]))
                if cur is None or cur["strength"] < r["strength"]:
                    cand[(a["id"], b["id"])] = {"strength": r["strength"], "reason": r["name"], "source": "rule"}
    for (a, b), e in cand.items():
        why = complement_violations(products[a], products[b], policy)
        if why:
            (conflicts if e["source"] == "pair" else blocked).append(("COMPLEMENT", a, b, e["strength"], " · ".join(why)))
        else:
            comps[(a, b)] = e
    return subs, comps, blocked, conflicts


def build_combos(products, policy, tpo, comps):
    """장면별 콤보. 슬롯 후보 = 슬롯 조건 + 장면 TPO 적합. 정체성 중복·예산 초과 조합은 버린다.
    점수 = 상품쌍 궁합 평균. 궁합 = 보완 간선 강도, 없으면 시간대 자카드 × 0.3(같이 먹을 수는 있음)."""
    cfg, price = policy["combo"], policy["combo"]["price"]

    def compat(p, q):
        e = comps.get((p["id"], q["id"])) or comps.get((q["id"], p["id"]))
        if e:
            return e["strength"]
        tp, tq = p["tpo"].get("time", set()), q["tpo"].get("time", set())
        return 0.3 * len(tp & tq) / len(tp | tq) if tp | tq else 0.0

    out = {}
    for sc in tpo["scenes"]:
        slots = [[p for p in products.values() if matches(p, s["cond"]) and fits_scene(p, sc["tpo"])] for s in sc["slots"]]
        combos = []
        for items in itertools.product(*slots):
            ids = {p["id"] for p in items}
            idents = {identity(p, policy) for p in items}
            if len(ids) < len(items) or (cfg["distinct_identity"] and len(idents) < len(items)):
                continue
            if any(complement_violations(p, q, policy) for p, q in itertools.combinations(items, 2)):
                continue
            total = sum(price[p["attrs"]["price_band"]] for p in items)
            if total > sc["budget"]:
                continue
            pairs = list(itertools.combinations(items, 2))
            combos.append((round(sum(compat(p, q) for p, q in pairs) / len(pairs), 3), total, items))
        combos.sort(key=lambda c: (-c[0], c[1]))
        sole = {cands[0]["id"] for cands in slots if len(cands) == 1}  # 후보가 하나뿐인 슬롯(우산 등)은 재사용 제한 없음
        picked, used = [], {}
        for c in combos:
            if all(p["id"] in sole or used.get(p["id"], 0) < cfg["max_reuse"] for p in c[2]):
                picked.append(c)
                for p in c[2]:
                    used[p["id"]] = used.get(p["id"], 0) + 1
            if len(picked) == cfg["top"]:
                break
        out[sc["id"]] = picked
    return out


def load_trends(products, policy):
    path = ROOT / "data/trends.csv"
    if not path.exists():
        return []
    return [r for r in read_csv("data/trends.csv")
            if r["product_id"] in products and float(r["score"]) >= policy["trend"]["min_score"]]


# ── 출력 ──────────────────────────────────────────────────────────

def to_cypher(products, attrs, cats, policy, tpo, subs, comps, combos, trends, w):
    q = lambda v: json.dumps(v, ensure_ascii=False)  # Cypher 문자열 리터럴로도 유효
    pmatch = lambda var, pid: f"(%s:Product {{id: {q(pid)}}})" % var
    out = [f"CREATE CONSTRAINT {n.lower()}_{k} IF NOT EXISTS FOR (n:{n}) REQUIRE n.{k} IS UNIQUE;"
           for n, k in [("Product", "id"), ("Category", "id"), ("Attribute", "id"), ("AttrValue", "key"),
                        ("TPO", "key"), ("Scene", "id"), ("Combo", "id"), ("Policy", "id"), ("Trend", "period")]]
    out += ["// 재적재 시 추천 결과(간선·콤보)만 지우고 다시 만든다. 상품·텍소노미 노드는 MERGE.",
            "MATCH ()-[r:SUBSTITUTE|COMPLEMENT|IN_TREND]->() DELETE r;",
            "MATCH (c:Combo) DETACH DELETE c;"]
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
    for dim, d in tpo["dimensions"].items():
        for v, vd in d["values"].items():
            out.append(f"MERGE (t:TPO {{key: {q(f'{dim}={v}')}}}) SET t.dim = {q(dim)}, t.dim_name = {q(d['name'])}, "
                       f"t.value = {q(v)}, t.definition = {q(vd['definition'])};")
    for pid, kind in [("identity", "아이덴티티"), ("substitute", "대체재"), ("complement", "보완재"), ("combo", "콤보"), ("trend", "트렌드")]:
        out.append(f"MERGE (x:Policy {{id: {q(pid)}}}) SET x.name = {q(kind)}, x.definition = {q(policy[pid]['definition'])};")
    for r in policy.get("restricted", []):
        out.append(f"MERGE (x:Policy {{id: {q('restricted:' + r['name'])}}}) SET x.name = {q(r['name'])}, x.definition = {q(r['definition'])};")
    for p in products.values():
        out.append(f"MERGE {pmatch('p', p['id'])} SET p.name = {q(p['name'])}, p.type = {q(p['type'])}, "
                   f"p.identity = {q('/'.join(str(x) for x in identity(p, policy)))};")
        for aid, v in p["attrs"].items():
            if attrs[aid]["type"] == "tree":
                out.append(f"MATCH {pmatch('p', p['id'])}, (c:Category {{id: {q(v)}}}) MERGE (p)-[:IN_CATEGORY]->(c);")
                continue
            for x in sorted(as_set(v)):
                out.append(f"MATCH {pmatch('p', p['id'])}, (v:AttrValue {{key: {q(f'{aid}={x}')}}}) MERGE (p)-[:HAS_ATTR]->(v);")
        for dim, vals in p["tpo"].items():
            for v in sorted(vals):
                out.append(f"MATCH {pmatch('p', p['id'])}, (t:TPO {{key: {q(f'{dim}={v}')}}}) MERGE (p)-[:FITS]->(t);")
        for dim, vals in p["tpo_excl"].items():
            for v in sorted(vals):
                out.append(f"MATCH {pmatch('p', p['id'])}, (t:TPO {{key: {q(f'{dim}={v}')}}}) MERGE (p)-[:AVOIDS]->(t);")
    for (a, b), e in subs.items():
        out.append(f"MATCH {pmatch('a', a)}, {pmatch('b', b)} CREATE (a)-[:SUBSTITUTE {{strength: {e['strength']}, "
                   f"model_score: {e['model_score']}, review: {q(e['review'])}, depth: {w.get(products[a]['type'], DEFAULT_W)['max_depth']}}}]->(b);")
    for (a, b), e in comps.items():
        out.append(f"MATCH {pmatch('a', a)}, {pmatch('b', b)} CREATE (a)-[:COMPLEMENT {{strength: {e['strength']}, "
                   f"reason: {q(e['reason'])}, source: {q(e['source'])}}}]->(b);")
    for sc in tpo["scenes"]:
        out.append(f"MERGE (s:Scene {{id: {q(sc['id'])}}}) SET s.name = {q(sc['name'])}, s.budget = {sc['budget']};")
        for dim, v in sc["tpo"].items():
            out.append(f"MATCH (s:Scene {{id: {q(sc['id'])}}}), (t:TPO {{key: {q(f'{dim}={v}')}}}) MERGE (s)-[:AT]->(t);")
        for i, (cs, total, items) in enumerate(combos[sc["id"]], 1):
            cid = f"{sc['id']}#{i}"
            out.append(f"MATCH (s:Scene {{id: {q(sc['id'])}}}) CREATE (c:Combo {{id: {q(cid)}, score: {cs}, price: {total}, rank: {i}}})-[:FOR]->(s);")
            for slot, p in zip(sc["slots"], items):
                out.append(f"MATCH (c:Combo {{id: {q(cid)}}}), {pmatch('p', p['id'])} CREATE (c)-[:INCLUDES {{role: {q(slot['role'])}}}]->(p);")
    for t in trends:
        out.append(f"MERGE (t:Trend {{period: {q(t['period'])}}});")
        out.append(f"MATCH {pmatch('p', t['product_id'])}, (t:Trend {{period: {q(t['period'])}}}) "
                   f"CREATE (p)-[:IN_TREND {{score: {float(t['score'])}, source: {q(t['source'])}}}]->(t);")
    return out


def to_audit(products, tpo, subs, comps, blocked, conflicts, combos, issues):
    """사람 검수용 요약. 튀는 추천이 정책에 막혔는지, 콤보가 말이 되는지 한눈에 본다."""
    n = lambda i: products[i]["name"]
    L = ["# 추천 검수 리포트", "", "`python cvs.py graph` 가 생성. 이상하면 정책(ontology/)이나 검수셋(review/)을 고친 뒤 다시 돌린다.", ""]
    L += [f"- 대체 간선 {len(subs)} · 보완 간선 {len(comps)} · 정책 차단 {len(blocked)} · 정책 충돌 {len(conflicts)}", ""]
    L += ["## 정책 충돌 (검수 O 또는 지정 조합인데 정책 위반 — 둘 중 하나가 틀림)", ""]
    L += [f"- {r} {n(a)} ↔ {n(b)} ({s}) — {why}" for r, a, b, s, why in conflicts] or ["- 없음"]
    L += ["", "## 장면별 콤보", ""]
    for sc in tpo["scenes"]:
        tpo_txt = ", ".join(f"{k}={v}" for k, v in sc["tpo"].items())
        L += [f"### {sc['name']} ({tpo_txt}, 예산 {sc['budget']:,}원)", ""]
        L += [f"{i}. " + " + ".join(f"{s['role']}:{p['name']}" for s, p in zip(sc["slots"], items)) + f" — 궁합 {cs} · {total:,}원"
              for i, (cs, total, items) in enumerate(combos[sc["id"]], 1)] or ["- 조건을 만족하는 조합 없음"]
        L.append("")
    L += ["## 상품별 대체재 상위 3", "", "| 상품 | 대체재 (강도·검수) |", "|---|---|"]
    for pid in products:
        es = sorted(((e["strength"], b if a == pid else a, e["review"]) for (a, b), e in subs.items() if pid in (a, b)), reverse=True)[:3]
        L.append(f"| {n(pid)} | " + ", ".join(f"{n(o)} {s} {'✔' if r == 'approved' else ''}" for s, o, r in es) + " |")
    L += ["", "## 정책이 막은 추천 (점수는 높았지만 사람이라면 안 할 추천)", "", "| 관계 | A | B | 점수 | 사유 |", "|---|---|---|---|---|"]
    L += [f"| {r} | {n(a)} | {n(b)} | {s} | {why} |" for r, a, b, s, why in sorted(blocked, key=lambda x: -x[3])[:40]]
    unknown = [i for i in issues if i[4] == "unknown_value"]
    L += ["", "## 처음 보는 속성값 (대응방침 대기)", ""] + ([f"- {i[1]} · {i[2]} = '{i[3]}'" for i in unknown] or ["- 없음"])
    return "\n".join(L) + "\n"


def cmd_graph(args):
    attrs, cats, products, issues, policy, tpo = load_context()
    w = load_weights()
    subs, comps, blocked, conflicts = build_edges(products, attrs, policy, w, load_gold(), args.k, args.min_score)
    combos = build_combos(products, policy, tpo, comps)
    trends = load_trends(products, policy)
    stmts = to_cypher(products, attrs, cats, policy, tpo, subs, comps, combos, trends, w)
    (ROOT / "out").mkdir(exist_ok=True)
    (ROOT / "out/graph.cypher").write_text("\n".join(stmts) + "\n", encoding="utf-8")
    (ROOT / "out/audit.md").write_text(to_audit(products, tpo, subs, comps, blocked, conflicts, combos, issues), encoding="utf-8")
    name = lambda i: products[i]["name"]
    write_csv("out/edges.csv", ["rel", "a", "a_name", "b", "b_name", "strength", "model_score", "review_or_reason"],
              [("SUBSTITUTE", a, name(a), b, name(b), e["strength"], e["model_score"], e["review"])
               for (a, b), e in sorted(subs.items(), key=lambda x: -x[1]["strength"])]
              + [("COMPLEMENT", a, name(a), b, name(b), e["strength"], "", e["reason"])
                 for (a, b), e in sorted(comps.items(), key=lambda x: -x[1]["strength"])])
    by = lambda key: sum(e["review"] == key for e in subs.values())
    print(f"대체 간선 {len(subs)} (검수O {by('approved')} / 미검수 {by('unreviewed')}) · 보완 간선 {len(comps)}")
    print(f"정책 차단 {len(blocked)} · 정책 충돌 {len(conflicts)} · 콤보 {sum(map(len, combos.values()))} · 트렌드 {len(trends)}")
    print(f"→ out/graph.cypher ({len(stmts)}문), out/edges.csv, out/audit.md")
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
