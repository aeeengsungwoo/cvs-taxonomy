"""우리동네GS 추천용 온톨로지: 텍소노미 → TPO 추론 → 정책(상품간·규격·속성·아이덴티티)으로 튀는 추천 차단
→ 대체재·보완재·콤보·트렌드 → 인간 검수 루프 → 그래프DB(Neo4j) 적재.

  python cvs.py validate          정의 누락·처음 보는 속성값 점검 (review/new_values.csv)
  python cvs.py sweep             뎁스·가중치 튜닝 → config/weights.yaml
  python cvs.py review export     검수할 대체재 후보 → review/queue.csv
  python cvs.py review import     검수 끝난 queue → review/gold.csv
  python cvs.py anchor            대체 기준(라인·메뉴·카테고리) 판정 → review/anchor.csv (갈리면 사람이 decision 칸에)
  python cvs.py impact            속성 하나씩 빼 보고 유지·보류·제거 판정 → out/attribute_impact.csv
  python cvs.py triage            벡터 근처 후보 자동 판정 + 관리자 질문 → review/auto_labels.csv, review/admin_queue.csv
  python cvs.py admin             관리자 답 반영 (상품쌍 → gold.csv, 정책 문항 → 방향 정책)
  python cvs.py oos 코카콜라      품절 시 대신 권할 상품과 이유 (--also-out, --context weather=더움, --all)
  python cvs.py survey            페르소나 설문 집계 → 기존 라벨과 대조, 품절 추천 precision/recall (out/survey_*.csv)
  python cvs.py graph [--push]    out/graph.cypher, out/edges.csv, out/audit.md 생성 (--push 면 Neo4j에 바로 적재)
"""
import argparse
import csv
import itertools
import json
import os
import sys
from pathlib import Path

import numpy as np
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


def label_of(gold, a, b, directional=False):
    """a 찾는 손님에게 b. 방향 속성이 다른 쌍이면 반대 방향 라벨을 빌려 쓰지 않는다(코카콜라→제로 ≠ 제로→코카콜라)."""
    return gold.get((a, b), None if directional else gold.get((b, a)))


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
    final = lambda a, b: 0.0 if (a, b) in blocked else score(products[a], products[b], attrs, w)
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
        rr.append(1 / (1 + sum((o > s[(a, b)]) + 0.5 * (o == s[(a, b)]) for o in others)))  # 동점은 절반만 앞선 것으로
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
    blocked = policy_blocked(products, attrs, policy)
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
                pair = (pid, q) if direction_steps(products[pid], products[q], attrs, policy) else frozenset((pid, q))
                if label_of(gold, pid, q, isinstance(pair, tuple)) is None and pair not in seen:
                    seen.add(pair)
                    rows.append((pid, products[pid]["name"], q, products[q]["name"], f"{sc:.3f}", "", ""))
        write_csv("review/queue.csv", header, rows)
        print(f"검수 대기 {len(rows)}쌍 → review/queue.csv (verdict 칸에 O=대체 가능 / X=아님)")
        return
    queue = read_csv("review/queue.csv")
    done = [r for r in queue if r["verdict"].strip().upper() in POS | NEG]
    with open(ROOT / "review/gold.csv", "a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(
            (r["anchor"], r["candidate"], int(r["verdict"].strip().upper() in POS), args.source, r["note"])
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
    policy = read_yaml("ontology/policy.yaml")
    for r in admin_rows():
        if r["kind"] == "policy" and r["answer"].strip().upper() in NEG:  # 관리자가 X → 그 방향 허용 단계 0
            _, aid, way = r["key"].split(".")
            policy["substitute"]["direction"][aid][way] = 0
    return policy, read_yaml("ontology/tpo.yaml")


def admin_rows():
    return read_csv("review/admin_queue.csv") if (ROOT / "review/admin_queue.csv").exists() else []


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
    sub, rule, out = policy["substitute"], policy["substitute"][p["type"]], []
    line = bool(p["attrs"].get("line")) and p["attrs"].get("line") == q["attrs"].get("line")
    within = lambda cs: any(matches(p, {"category": c}) for c in cs) and any(matches(q, {"category": c}) for c in cs)
    covers = any(within([i["set"], *i["covers"]]) if i.get("two_way") else
                 matches(q, {"category": i["set"]}) and any(matches(p, {"category": c}) for c in i["covers"])
                 for i in sub.get("includes", []))  # 세트 ↔ 구성품은 아이덴티티가 달라도 대체
    grouped = any(within(g["cats"]) for g in sub.get("groups", []))  # 손님이 같은 욕구로 보는 카테고리 묶음 (물 ↔ 차)
    if "category_level" in rule and not (covers or grouped) and cat_prefix(p, rule["category_level"]) != cat_prefix(q, rule["category_level"]):
        out.append(f"아이덴티티: 카테고리 {rule['category_level']}단계 다름")
    for aid in rule.get("same", []):
        a, b = p["attrs"].get(aid), q["attrs"].get(aid)
        if (aid in rule.get("same_unless_line", []) and line) or within(rule.get("loose", {}).get(aid, [])):
            continue  # 같은 라인이면 포장형태가 달라도 됨 (불닭 봉지 → 불닭 큰컵) · 생리대는 사이즈 한 단계 차이 허용
        if a is not None and b is not None and a != b:
            out.append(f"아이덴티티: {attrs[aid]['name']} 다름({a}/{b})")
    for aid in rule.get("share", []):
        a, b = p["attrs"].get(aid), q["attrs"].get(aid)
        if a is not None and b is not None and not as_set(a) & as_set(b):
            out.append(f"상품간: {attrs[aid]['name']} 겹침 없음")
    for aid, step in direction_steps(p, q, attrs, policy).items():
        r, way = sub["direction"][aid], "up" if step > 0 else "down"
        if r.get("line_free") and line:
            continue  # 같은 라인 안에서는 방향 제한 없음 (코카콜라 제로 → 코카콜라). 감점과 점원 순서(뒤로)는 그대로
        if abs(step) > r[way] or (step > 0 and p["attrs"][aid] in r.get("hard_from", [])):  # 제로를 고른 손님에게 당 올림 금지
            out.append(f"방향: {attrs[aid]['name']} {p['attrs'][aid]}→{q['attrs'][aid]}")
    for aid, gap in sub["max_gap"].items():
        a, b = p["attrs"].get(aid), q["attrs"].get(aid)
        if a is not None and b is not None:
            order = list(attrs[aid]["values"])
            if abs(order.index(a) - order.index(b)) > gap:
                out.append(f"규격: {attrs[aid]['name']} {a}→{b}")
    return out


def direction_steps(p, q, attrs, policy, raw=False):
    """p 찾는 손님에게 q 를 권할 때 방향 속성별 이동 단계. {aid: +n 커짐 / -n 작아짐}
    free_up_to 이하로 올라가는 것(안 매움 → 약간 매움)과 free_from 이상에서 올라가는 것(매운 걸 고른 손님에게 더 매운 것)은
    방향 정책에서 제외. raw=True 면 그것도 포함(이유 문구용)."""
    out = {}
    for aid, r in policy["substitute"].get("direction", {}).items():
        a, b = p["attrs"].get(aid), q["attrs"].get(aid)
        if a is not None and b is not None and a != b:
            order = list(attrs[aid]["values"])
            st = order.index(b) - order.index(a)
            free = st > 0 and ("free_up_to" in r and order.index(b) <= order.index(r["free_up_to"])
                               or "free_from" in r and order.index(a) >= order.index(r["free_from"]))
            if raw or not free:
                out[aid] = st
    return out


def direction_penalty(p, q, attrs, policy):
    rules = policy["substitute"].get("direction", {})
    return sum(rules[aid].get("penalty", {}).get("up" if st > 0 else "down", 0)
               for aid, st in direction_steps(p, q, attrs, policy).items())


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


def policy_blocked(products, attrs, policy):
    """같은 type 방향 쌍 (찾는 상품, 권할 상품) 중 대체재 하드 정책에 걸리는 쌍."""
    return {(p["id"], q["id"]) for p, q in itertools.permutations(products.values(), 2)
            if p["type"] == q["type"] and substitute_violations(p, q, attrs, policy)}


# ── 앵커(대체 기준) ───────────────────────────────────────────────

ANCHOR_HEADER = ["id", "name", "line", "menu", "proposed", "status", "decision", "line_candidates", "menu_candidates", "note"]


def anchor_proposals(products, attrs, policy):
    """ontology/policy.yaml anchor 의 자동 판정 규칙. 반환 {id: (제안, auto|pending, line 후보, menu 후보)}."""
    out = {}
    for p in products.values():
        peers = [q for q in products.values() if q["id"] != p["id"] and q["type"] == p["type"]
                 and not substitute_violations(p, q, attrs, policy)]
        line, menu = p["attrs"].get("line"), p["attrs"].get("menu")
        lp = [q for q in peers if line and q["attrs"].get("line") == line]
        mp = [q for q in peers if menu and q["attrs"].get("menu") == menu]
        if line and menu:  # 브랜드와 메뉴가 둘 다 이름에 있으면 손님이 무엇을 놓지 않을지 데이터로 못 정함
            out[p["id"]] = ("pending", "pending", lp, mp)
        elif lp:
            out[p["id"]] = ("line", "auto", lp, mp)
        elif mp:
            out[p["id"]] = ("menu", "auto", lp, mp)
        else:
            out[p["id"]] = ("category", "auto", lp, mp)
    return out


def effective_anchors(products, attrs, policy):
    """사람 결정(review/anchor.csv decision) > 자동 판정. pending 은 결정 전까지 category."""
    decided = {r["id"]: r["decision"].strip() for r in read_csv("review/anchor.csv")} \
        if (ROOT / "review/anchor.csv").exists() else {}
    res = {}
    for pid, (prop, status, _, _) in anchor_proposals(products, attrs, policy).items():
        d = decided.get(pid, "")
        res[pid] = d if d in ("line", "menu", "category") else (prop if status == "auto" else "category")
    return res


def anchor_hit(p, q, anchors):
    """p 또는 q 의 앵커 값이 서로 같으면 그 앵커 이름(line/menu)."""
    for x, y in ((p, q), (q, p)):
        an = anchors.get(x["id"])
        if an in ("line", "menu") and x["attrs"].get(an) and x["attrs"].get(an) == y["attrs"].get(an):
            return an
    return ""


def cmd_anchor(args):
    attrs, cats, products, _, policy, _ = load_context()
    old = {r["id"]: r for r in read_csv("review/anchor.csv")} if (ROOT / "review/anchor.csv").exists() else {}
    names = lambda qs: ", ".join(q["name"] for q in qs)
    rows, pending = [], []
    for pid, (prop, status, lp, mp) in anchor_proposals(products, attrs, policy).items():
        p = products[pid]
        if not (p["attrs"].get("line") or p["attrs"].get("menu")):
            continue  # 라인·메뉴가 없으면 판단할 게 없음 → category
        o = old.get(pid, {})
        rows.append((pid, p["name"], p["attrs"].get("line", ""), p["attrs"].get("menu", ""), prop, status,
                     o.get("decision", ""), names(lp), names(mp), o.get("note", "")))
        if status == "pending" and not o.get("decision", "").strip():
            pending.append(f"  - {p['name']}: line({p['attrs'].get('line')}) → [{names(lp) or '후보 없음'}]"
                           f" / menu({p['attrs'].get('menu')}) → [{names(mp) or '후보 없음'}]")
    write_csv("review/anchor.csv", ANCHOR_HEADER, rows)
    print(f"앵커 판정 {len(rows)}개 → review/anchor.csv · 사람 결정 대기 {len(pending)}개")
    print("\n".join(pending))


# ── 속성 영향도 ───────────────────────────────────────────────────

def structural_uses(policy, tpo):
    """정책·TPO에서 속성 id가 쓰이는 곳. 점수와 무관하게 구조적으로 필요한 속성을 가려낸다."""
    used = {}

    def walk(node, where):
        if isinstance(node, dict):
            for k, v in node.items():
                used.setdefault(k, set()).add(where)
                walk(v, where)
        elif isinstance(node, list):
            for v in node:
                if isinstance(v, str):
                    used.setdefault(v, set()).add(where)
                walk(v, where)

    for sec in ("identity", "substitute", "complement", "restricted"):
        walk(policy.get(sec), f"정책:{sec}")
    walk(tpo["rules"], "TPO 규칙")
    walk([s["slots"] for s in tpo["scenes"]], "콤보 장면")
    for a in ("line", "menu"):
        used.setdefault(a, set()).add("정책:anchor")
    used.setdefault("category", set()).add("정책:identity")
    used.setdefault("price_band", set()).add("콤보 예산")
    return used


def cmd_impact(args):
    attrs, cats, products, _, policy, tpo = load_context()
    W, gold, cfg = load_weights(), load_gold(), policy["attribute_impact"]
    blocked = policy_blocked(products, attrs, policy)
    used = structural_uses(policy, tpo)
    rows = []
    for typ in dict.fromkeys(p["type"] for p in products.values()):
        g = {k: v for k, v in gold.items() if products[k[0]]["type"] == typ}
        if not any(g.values()) or all(g.values()):
            continue
        w = {typ: W.get(typ, DEFAULT_W)}
        ids = [pid for pid, p in products.items() if p["type"] == typ]
        def top5(A):  # 정책 통과 후 상위 5 대체 후보
            return {pid: {q for _, q in [x for x in ranked(products, A, w, pid)
                                         if (pid, x[1]) not in blocked][:5]} for pid in ids}

        base = sum(evaluate(products, attrs, w, g, blocked)) / 2
        base_top = top5(attrs)
        for aid, a in attrs.items():
            if typ not in a["applies_to"]:
                continue
            A = {k: (dict(v, weight=0) if k == aid else v) for k, v in attrs.items()}
            delta = base - sum(evaluate(products, A, w, g, blocked)) / 2
            new_top = top5(A)
            churn = sum(1 - len(base_top[i] & new_top[i]) / len(base_top[i] | new_top[i])
                        for i in ids if base_top[i] | new_top[i]) / len(ids)
            missing = 0.0 if a.get("optional") else sum(aid not in products[i]["attrs"] for i in ids) / len(ids)
            in_depth = a["depth"] <= w[typ]["max_depth"] and a["weight"] > 0
            uses = " · ".join(sorted(used.get(aid, ())))
            if a["weight"] == 0:
                role = "보관"  # 영향도 판정으로 가중치 0 — 정의·데이터는 남기고 점수에서만 뺌
            elif not in_depth:
                role = "미사용"
            elif delta >= cfg["keep_delta"]:
                role = "기여"
            elif delta <= cfg["noise_delta"]:
                role = "노이즈"
            elif churn >= cfg["churn_hold"]:
                role = "보류"
            else:
                role = "미미"
            if uses:  # 정책·TPO가 쓰면 점수 기여와 무관하게 남긴다. 점수에서만 뺄지는 role 로 판단
                verdict = "유지"
                why = {"기여": "점수·정책 둘 다 씀", "노이즈": "정책엔 필요, 점수에선 노이즈 → 점수 가중치만 0 검토",
                       "미미": "정책용(점수 영향 미미)", "보류": "정책용, 점수 영향은 검수로 판단",
                       "미사용": "정책용(점수엔 안 씀)", "보관": "정책용(점수 가중치 0)"}[role]
            else:
                verdict, why = {"기여": ("유지", "빼면 성능 하락"), "노이즈": ("제거 권장", "빼면 성능 상승"),
                                "보류": ("보류", "성능은 같은데 추천 목록이 바뀜 → 검수로 판단"),
                                "미미": ("제거 후보", "성능·추천 목록 모두 영향 미미"),
                                "미사용": ("제거 후보", "현재 뎁스 밖이라 점수에 안 쓰임"),
                                "보관": ("보관", "가중치 0으로 점수에서 뺌, 정의·데이터는 남김")}[role]
            if missing >= cfg["missing_warn"]:
                why += " · 결측 높음 → 데이터 보강"
            rows.append((typ, aid, a["name"], a["depth"], a["weight"], "O" if in_depth else "", uses,
                         round(delta, 4), round(churn, 3), round(missing, 2), role, verdict, why))
    (ROOT / "out").mkdir(exist_ok=True)
    write_csv("out/attribute_impact.csv", ["type", "attr", "name", "depth", "weight", "score_used", "structural_use",
                                           "delta_objective", "top5_churn", "missing_rate", "score_role", "verdict", "reason"], rows)
    for typ in dict.fromkeys(r[0] for r in rows):
        print(f"[{typ}]")
        for r in rows:
            if r[0] == typ:
                print(f"  {r[2]:<8} Δ{r[7]:+.3f} 변동 {r[8]:.2f} 점수:{r[10]:<3} → {r[11]} ({r[12]})")
    print("→ out/attribute_impact.csv")


# ── 상품 벡터 (속성 공간의 위치) ─────────────────────────────────────

def embed(products, attrs, w):
    """상품을 속성 공간의 단위 벡터로. score 와 같은 속성·가중치를 쓰고, 순서 속성은 온도계 인코딩(가까운 단계일수록 많이 겹침).
    벡터가 가까운 상품 = 대체 후보, 벡터 지도 = 상품 위치."""
    feats = {}
    for pid, p in products.items():
        tw, v = w.get(p["type"], DEFAULT_W), {}
        for aid, a in attrs.items():
            if (a["weight"] == 0 or a["depth"] > tw["max_depth"] or p["type"] not in a["applies_to"]
                    or aid not in p["attrs"]):
                continue
            x = p["attrs"][aid]
            if a["type"] == "tree":
                keys = [".".join(x.split(".")[:i + 1]) for i in range(x.count(".") + 1)]
            elif a["type"] == "multi":
                keys = sorted(x)
            elif a["type"] == "ordinal":
                order = list(a["values"])
                keys = order[:order.index(x) + 1]
            else:
                keys = [x]
            for key in keys:
                v[f"{aid}={key}"] = (a["weight"] * tw["depth_mult"][a["depth"]] / len(keys)) ** 0.5
        feats[pid] = v
    index = {f: i for i, f in enumerate(sorted({f for v in feats.values() for f in v}))}
    out = {}
    for pid, v in feats.items():
        x = np.zeros(len(index))
        for f, val in v.items():
            x[index[f]] = val
        n = np.linalg.norm(x)
        out[pid] = x / n if n else x
    return out


def labeled_examples(products, attrs, policy, gold):
    """검수된 방향 쌍 목록. 방향 속성이 같은 쌍은 반대 방향도 같은 라벨로 쓴다."""
    ex = []
    for (c, d), lab in gold.items():
        ex.append((c, d, lab))
        if not direction_steps(products[c], products[d], attrs, policy) and (d, c) not in gold:  # 반대 방향 라벨이 따로 있으면 빌리지 않음
            ex.append((d, c, lab))
    return ex


def propagate(a, b, M, products, attrs, policy, examples, cutoff):
    """(a→b) 와 비슷한 검수 쌍 (c→d) 의 라벨 가중 평균. 비슷함 = cos(a,c) × cos(b,d), 방향 속성의 이동도 같아야 함."""
    sig = {k: v > 0 for k, v in direction_steps(products[a], products[b], attrs, policy).items()}
    near = []
    for c, d, lab in examples:
        if products[c]["type"] != products[a]["type"] or {c, d} == {a, b}:
            continue
        sim = float(M[a] @ M[c]) * float(M[b] @ M[d])
        if sim >= cutoff and {k: v > 0 for k, v in direction_steps(products[c], products[d], attrs, policy).items()} == sig:
            near.append((sim, c, d, lab))
    near.sort(reverse=True)
    near = near[:5]
    support = sum(s for s, *_ in near)
    p_hat = sum(s * lab for s, _, _, lab in near) / support if support else None
    return p_hat, support, near


ADMIN_HEADER = ["kind", "key", "question", "suggestion", "evidence", "affects", "answer", "note"]


def cmd_triage(args):
    """구축 단계 자동 판정. 손님에게는 묻지 않고, 근거가 부족한 것만 관리자 질문으로 모은다."""
    attrs, cats, products, _, policy, _ = load_context()
    w, gold, cfg = load_weights(), load_gold(), policy["hitl"]
    M = embed(products, attrs, w)
    examples = labeled_examples(products, attrs, policy, gold)
    survey = survey_labels(policy)  # 설문이 판정한 쌍은 관리자에게 묻지 않는다
    name = lambda i: products[i]["name"]
    old = {r["key"]: r for r in admin_rows()}
    asked = {(aid, way) for aid, r in policy["substitute"]["direction"].items() for way in r.get("ask", {})}
    auto, asks, seen, by_policy = [], [], set(), 0
    for pid in products:
        near = sorted(((float(M[pid] @ M[q]), q) for q in products if q != pid and products[q]["type"] == products[pid]["type"]),
                      reverse=True)[:cfg["candidates_k"]]
        for _, q in near:
            for a, b in ((pid, q), (q, pid)):
                if (a, b) in seen:
                    continue
                seen.add((a, b))
                pa, pb = products[a], products[b]
                directional = bool(direction_steps(pa, pb, attrs, policy))
                if (label_of(gold, a, b, directional) is not None or (a, b) in survey
                        or substitute_violations(pa, pb, attrs, policy)):
                    continue
                sc = score(pa, pb, attrs, w) - direction_penalty(pa, pb, attrs, policy)
                if sc < cfg["min_score"]:
                    continue
                steps = direction_steps(pa, pb, attrs, policy)
                if any((aid, "up" if st > 0 else "down") in asked for aid, st in steps.items()):
                    by_policy += 1  # 방향 정책 문항 하나로 일괄 결정되는 쌍 — 따로 묻지 않음
                    continue
                p_hat, support, ev = propagate(a, b, M, products, attrs, policy, examples, cfg["similar_cutoff"])
                why = " / ".join(f"{name(c)}→{name(d)} {'O' if lab else 'X'}({s:.2f})" for s, c, d, lab in ev)
                if p_hat is not None and support >= cfg["min_support"] and p_hat >= cfg["approve"]:
                    auto.append((a, b, 1, round(p_hat, 3), round(support, 2), why))
                elif p_hat is not None and support >= cfg["min_support"] and p_hat <= cfg["reject"]:
                    auto.append((a, b, 0, round(p_hat, 3), round(support, 2), why))
                else:
                    unsure = 1 - abs((p_hat if p_hat is not None else 0.5) - 0.5) * 2  # 0.5 에 가까울수록 불확실
                    asks.append((unsure * sc, a, b, sc, p_hat, why))
    write_csv("review/auto_labels.csv", ["anchor", "candidate", "label", "p_hat", "support", "evidence"],
              [(a, b, lab, ph, sp, ev) for a, b, lab, ph, sp, ev in auto])
    rows = []
    # 정책 문항: 방향 하나에 질문 하나. 답 하나가 그 방향의 모든 상품쌍에 적용된다.
    base = read_yaml("ontology/policy.yaml")["substitute"]["direction"]
    for aid, r in base.items():
        for way, question in r.get("ask", {}).items():
            hits = [(a["id"], b["id"]) for a, b in itertools.permutations(products.values(), 2)
                    if a["type"] == b["type"] and (st := direction_steps(a, b, attrs, policy).get(aid))
                    and (st > 0) == (way == "up") and abs(st) <= r[way]
                    and not [v for v in substitute_violations(a, b, attrs, policy) if not v.startswith("방향")]]
            hits.sort(key=lambda x: -score(products[x[0]], products[x[1]], attrs, w))
            key = f"direction.{aid}.{way}"
            o = old.get(key, {})
            rows.append(("policy", key, question, f"기본값: 허용 (강도 −{r.get('penalty', {}).get(way, 0)})",
                         " / ".join(f"{name(a)}→{name(b)}" for a, b in hits[:3]), len(hits), o.get("answer", ""), o.get("note", "")))
    asks.sort(reverse=True)
    for _, a, b, sc, p_hat, why in asks[:cfg["max_questions"]]:
        key = f"{a}>{b}"
        o = old.get(key, {})
        sug = "?" if p_hat is None else ("O" if p_hat >= 0.5 else "X") + f" (비슷한 검수 {p_hat:.0%} O)"
        rows.append(("pair", key, f"{name(a)} → {name(b)} 대체 추천", sug,
                     why or "비슷한 검수 쌍 없음", f"유사도 {sc:.2f}", o.get("answer", ""), o.get("note", "")))
    write_csv("review/admin_queue.csv", ADMIN_HEADER, rows)
    pol = sum(1 for r in rows if r[0] == "policy")
    print(f"자동 판정 {len(auto)}쌍 (승인 {sum(1 for x in auto if x[2])} · 거절 {sum(1 for x in auto if not x[2])}) → review/auto_labels.csv")
    print(f"관리자 질문 {len(rows)}개 (정책 {pol} · 상품쌍 {len(rows) - pol}, 불확실 후보 {len(asks)}개 중 상위) → review/admin_queue.csv")
    print(f"방향 정책 문항으로 일괄 결정되는 후보 {by_policy}쌍")


def cmd_admin(args):
    """관리자 답 반영. 상품쌍 답 → gold.csv(source=admin). 정책 문항은 파일에 남아 load_ontology 가 읽는다."""
    rows = admin_rows()
    done = [r for r in rows if r["kind"] == "pair" and r["answer"].strip().upper() in POS | NEG]
    with open(ROOT / "review/gold.csv", "a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows((*r["key"].split(">"), int(r["answer"].strip().upper() in POS), "admin", r["note"]) for r in done)
    write_csv("review/admin_queue.csv", ADMIN_HEADER, [[r[h] for h in ADMIN_HEADER] for r in rows if r not in done])
    pol = [r for r in rows if r["kind"] == "policy" and r["answer"].strip()]
    print(f"상품쌍 답 {len(done)}개 → gold.csv · 정책 답 {len(pol)}개 적용. 다음: python cvs.py sweep && python cvs.py triage")


# ── 간선 ──────────────────────────────────────────────────────────

def edge_strength(model, label, auto=None, policy=None):
    """간선 강도. 검수 O = 0.5 바닥 보장, 검수 X = 간선 삭제, 자동 승인 = 사람 O 보다 낮게, 미검수 = 모델 점수 20% 할인."""
    if label == 1:
        return round(0.5 + 0.5 * model, 3), "approved"
    if label == 0 or auto == 0:
        return None, "rejected"
    if auto == 1:
        a = policy["hitl"]["auto_strength"]
        return round(a["base"] + a["slope"] * model, 3), "auto"
    return round(0.8 * model, 3), "unreviewed"


def load_auto():
    path = ROOT / "review/auto_labels.csv"
    return {(r["anchor"], r["candidate"]): int(r["label"]) for r in read_csv("review/auto_labels.csv")} if path.exists() else {}


def build_edges(products, attrs, policy, w, gold, k, min_score, survey=None):
    """반환: 대체 간선, 보완 간선, 정책 차단 목록(튀는 추천), 정책 충돌(검수 O인데 정책 위반).
    survey: 설문 판정 {(a, b): (라벨, 수용률)}. 없으면 전 응답자로 만든다. 사람 검수(review) 다음으로 우선한다."""
    subs, blocked, conflicts = {}, [], []
    survey = survey_labels(policy) if survey is None else survey
    human = {(r["anchor"], r["candidate"]) for r in read_csv("review/gold.csv") if r["source"] == "review"}
    anchors, auto = effective_anchors(products, attrs, policy), load_auto()
    ok_ways = {tuple(r["key"].split(".")[1:]) for r in admin_rows()
               if r["kind"] == "policy" and r["answer"].strip().upper() in POS}  # 관리자가 O 한 방향 (sugar, down) …
    # 방향 있는 쌍 (a 찾는 손님에게 b). 후보는 양방향으로 넣고 방향마다 따로 정책을 건다.
    pairs = {pair for pair, lab in gold.items() if lab == 1} | {pair for pair, lab in auto.items() if lab == 1}         | {pair for pair, (lab, _) in survey.items() if lab == 1}
    for pid, p in products.items():
        pairs |= {(pid, q) for sc, q in ranked(products, attrs, w, pid)[:k] if sc >= min_score}
        an = anchors[pid]
        if an in ("line", "menu") and p["attrs"].get(an):  # 앵커가 같은 상품은 상위 k 밖이어도 후보
            pairs |= {(pid, q["id"]) for q in products.values()
                      if q["id"] != pid and q["type"] == p["type"] and q["attrs"].get(an) == p["attrs"][an]}
    pairs |= {(b, a) for a, b in pairs}
    for a, b in sorted(pairs):
        pa, pb = products[a], products[b]
        hit = anchor_hit(pa, pb, anchors)
        model = score(pa, pb, attrs, w)
        if hit:
            model = min(1.0, model + policy["anchor"]["boost"])
        model = max(0.0, model - direction_penalty(pa, pb, attrs, policy))
        label = label_of(gold, a, b, bool(direction_steps(pa, pb, attrs, policy)))
        if (a, b) in survey and (a, b) not in human:
            label = survey[(a, b)][0]  # 설문이 옳다고 가정 — AI 검수·관리자(AI) 라벨보다 우선
        why = substitute_violations(pa, pb, attrs, policy)
        if why:
            (conflicts if label == 1 else blocked).append(("SUBSTITUTE", a, b, round(model, 3), " · ".join(why)))
            continue
        steps = direction_steps(pa, pb, attrs, policy)
        if label == 1 and (a, b) in survey and (a, b) not in human:
            strength, review = round(0.5 + 0.5 * survey[(a, b)][1], 3), "survey"  # 강도 = 설문 수용률 → 점원 순서가 설문을 따름
        elif label is None and steps and all((aid, "up" if st > 0 else "down") in ok_ways for aid, st in steps.items()):
            # 이 방향은 관리자 정책 문항으로 승인됨 (코카콜라 → 코카콜라 제로)
            strength, review = round(0.5 + 0.5 * model, 3), "policy"
        else:
            strength, review = edge_strength(model, label, auto.get((a, b)), policy)
        if strength is not None:
            subs[(a, b)] = {"strength": strength, "model_score": round(model, 3), "review": review, "anchor": hit}
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
                   f"model_score: {e['model_score']}, review: {q(e['review'])}, anchor: {q(e['anchor'])}, depth: {w.get(products[a]['type'], DEFAULT_W)['max_depth']}}}]->(b);")
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


# ── 품절 대체 추천 ────────────────────────────────────────────────

def explain(p, q, attrs, cats, policy):
    """점원이 말하듯 '왜 이걸 권하는지'를 속성 차이로 만든다. 같은 점 하나 + 다른 점 최대 두 개."""
    pa, qa, same = p["attrs"], q["attrs"], None
    for aid, word in (("line", "라인"), ("menu", "메뉴")):
        if pa.get(aid) and pa.get(aid) == qa.get(aid):
            same = f"같은 {pa[aid]} {word}"
            break
    if not same:
        for level in (3, 2):
            c = cat_prefix(p, level)
            if c == cat_prefix(q, level) and c in cats:
                same = f"같은 {cats[c]['name']}"
                break
    if not same:
        same = next((f"같은 {g['name']}" for g in policy["substitute"].get("groups", [])
                     if any(matches(p, {"category": c}) for c in g["cats"]) and any(matches(q, {"category": c}) for c in g["cats"])), None)
    diffs = []
    for aid, st in direction_steps(p, q, attrs, policy, raw=True).items():
        word = {"sugar": ("당", "더 많음", "더 적음"), "spicy": ("맵기", "한 단계 더 매움", "한 단계 덜 매움"),
                "caffeine": ("카페인", "더 많음", "더 적음")}[aid]
        diffs.append(f"{word[0]} 없음" if aid == "sugar" and qa[aid] == "없음" else f"{word[0]} {word[1] if st > 0 else word[2]}")
    for aid, up, down in (("price_band", "조금 더 비쌈", "조금 더 저렴"), ("size", "양이 더 많음", "양이 더 적음")):
        if pa.get(aid) and qa.get(aid) and pa[aid] != qa[aid]:
            order = list(attrs[aid]["values"])
            diffs.append(up if order.index(qa[aid]) > order.index(pa[aid]) else down)
    added = sorted(as_set(qa.get("flavor") or frozenset()) - as_set(pa.get("flavor") or frozenset()) - set(policy["oos"]["quiet_flavors"]))
    if added:
        diffs.append("+" + "·".join(added))
    return " · ".join([s for s in [same] if s] + diffs[:2]) or "비슷한 상품"


def oos_recommend(pid, products, attrs, cats, policy, subs, out_of_stock=(), context=None):
    """pid 품절일 때 대신 권할 상품. 방향 있는 대체 간선(정책·검수·관리자 판단 반영)을 강도순으로,
    다른 품절 상품과 지금 상황(TPO)에 안 맞는 상품은 빼고, 기준 미만이면 억지로 권하지 않는다."""
    cfg, p = policy["oos"], products[pid]

    def tier(b, e):
        # 점원 순서: 같은 라인(더 세지는 쪽 아님) → 같은 세부 카테고리 → 나머지. 같은 단계 안에서는 강도순
        up = any(st > 0 for st in direction_steps(p, products[b], attrs, policy).values())
        if e["anchor"] == "line" and not up:
            return 0
        return 1 if cat_prefix(p, 3) == cat_prefix(products[b], 3) and not up else 2

    cands = sorted(((tier(b, e), -e["strength"], b, e) for (a, b), e in subs.items()
                    if a == pid and b not in out_of_stock and e["strength"] >= cfg["min_strength"]))
    out = []
    for _, neg, b, e in cands:
        s = -neg
        # 지금 상황(TPO)에 안 맞는 대안은 뺀다. 단 손님이 고른 상품도 그 상황을 피하는 상품이면 손님 선택이 우선
        if context and any(v in products[b]["tpo_excl"].get(dim, ()) and v not in p["tpo_excl"].get(dim, ())
                           for dim, v in context.items()):
            continue
        out.append((b, s, e["review"], explain(products[pid], products[b], attrs, cats, policy)))
        if len(out) == cfg["top"]:
            break
    return out


def find_product(products, key):
    if key in products:
        return key
    hits = [i for i, p in products.items() if key in p["name"]]
    exact = [i for i in hits if products[i]["name"] == key]
    if len(exact) == 1:
        return exact[0]
    if len(hits) != 1:
        sys.exit(f"상품 '{key}' — 후보 {len(hits)}개: {', '.join(products[i]['name'] for i in hits[:10])}")
    return hits[0]


def cmd_oos(args):
    attrs, cats, products, _, policy, _ = load_context()
    subs, *_ = build_edges(products, attrs, policy, load_weights(), load_gold(), 5, 0.55)
    context = dict(kv.split("=") for kv in args.context) if args.context else None
    if args.all:
        rows = []
        for pid, p in products.items():
            recs = oos_recommend(pid, products, attrs, cats, policy, subs)
            rows += [(pid, p["name"], k + 1, b, products[b]["name"], s, rv, why) for k, (b, s, rv, why) in enumerate(recs)] \
                or [(pid, p["name"], "", "", "", "", "", "권할 대체 없음")]
        write_csv("out/oos_recommendations.csv", ["id", "name", "rank", "alt_id", "alt_name", "strength", "review", "reason"], rows)
        none = sum(1 for r in rows if r[2] == "")
        print(f"상품 {len(products)}개 품절 시 대체 추천 → out/oos_recommendations.csv (권할 대체 없음 {none}개)")
        return
    pid = find_product(products, args.product)
    gone = {find_product(products, k) for k in args.also_out} if args.also_out else set()
    recs = oos_recommend(pid, products, attrs, cats, policy, subs, gone, context)
    print(f"{products[pid]['name']} 품절" + (f" (함께 품절: {', '.join(products[g]['name'] for g in gone)})" if gone else "")
          + (f" · 상황 {context}" if context else ""))
    for k, (b, s, rv, why) in enumerate(recs, 1):
        print(f"  {k}. {products[b]['name']}  — {why}  [{s:.2f}{'' if rv == 'approved' else ', ' + rv}]")
    if not recs:
        print("  권할 대체 없음 (기준 미만은 억지로 권하지 않음)")


# ── 페르소나 설문 ─────────────────────────────────────────────────

def load_survey(only=None):
    """survey/answers/*.json → 상품쌍별 {accept 대신 산다, best 가장 먼저 집음} 인원, 문항별 {n 응답자, none 안 산다}.
    skip(애초에 그 상품을 살 일이 없음)한 응답자는 그 문항의 분모에서 뺀다."""
    pairs, qs = {}, {}
    for f in sorted((ROOT / "survey/answers").glob("P*.json")):
        if only and f.stem not in only:
            continue
        for a in json.loads(f.read_text(encoding="utf-8"))["answers"]:
            if a.get("skip"):
                continue
            q = qs.setdefault(a["q"], {"n": 0, "none": 0})
            q["n"] += 1
            q["none"] += not a["accept"]
            for b in a["accept"]:
                pairs.setdefault((a["q"], b), {"accept": 0, "best": 0})["accept"] += 1
            if a.get("best"):
                pairs.setdefault((a["q"], a["best"]), {"accept": 0, "best": 0})["best"] += 1
    return pairs, qs


def survey_labels(policy, only=None):
    """설문 판정 {(a, b): (1 또는 0, 수용률)}. min_n 명 이상 답한 보기 중 accept 이상 O, reject 이하 X, 갈린 것은 뺀다."""
    if not (ROOT / "survey/answers").exists():
        return {}
    cfg, (pairs, qs), out = policy["survey"], load_survey(only), {}
    for (a, b), s in pairs.items():
        n = qs[a]["n"]
        if n >= cfg["min_n"]:
            r = s["accept"] / n
            if r >= cfg["accept"] or r <= cfg["reject"]:
                out[(a, b)] = (int(r >= cfg["accept"]), r)
    return out


def survey_eval(products, attrs, cats, policy, subs, pairs, qs):
    """품절 추천이 설문 다수와 얼마나 맞나. 추천은 여러 개라 precision/recall 로 잰다.
    precision = 권한 것 중 응답자 과반이 '대신 산다'인 비율, recall = 과반이 받아들인 보기 중 권한 비율,
    top1 = 1순위가 '가장 먼저 집음' 최다 보기와 같은가, 억지 = 과반이 받는 보기가 없는데 권함, 놓침 = 있는데 안 권함."""
    cfg, rows = policy["survey"], []
    for pid, q in qs.items():
        if q["n"] < cfg["min_n"]:
            continue
        ours = [b for b, *_ in oos_recommend(pid, products, attrs, cats, policy, subs)]
        liked = {b for (a, b), s in pairs.items() if a == pid and s["accept"] / q["n"] >= cfg["accept"]}
        bests = sorted(((s["best"], b) for (a, b), s in pairs.items() if a == pid and s["best"]), reverse=True)
        hit = len(set(ours) & liked)
        rows.append({"id": pid, "name": products[pid]["name"], "n": q["n"], "none": round(q["none"] / q["n"], 2),
                     "ours": "|".join(ours), "liked": "|".join(sorted(liked)),
                     "precision": round(hit / len(ours), 3) if ours else "", "recall": round(hit / len(liked), 3) if liked else "",
                     "top1": int(bool(ours) and ours[0] == bests[0][1]) if bests else "",
                     "forced": int(bool(ours) and not liked), "missed": int(not ours and bool(liked))})
    mean = lambda k: round(float(np.mean([r[k] for r in rows if r[k] != ""])), 3)
    return rows, {k: mean(k) for k in ("precision", "recall", "top1")} | {
        "forced": sum(r["forced"] for r in rows), "missed": sum(r["missed"] for r in rows), "questions": len(rows)}


def survey_features(p, q, attrs, policy):
    """설문 보기(p 품절 → q)의 차이 유형. 유형별 평균 수용률로 정책 값을 맞춘다.
    주의: 보기에 없는 상품쌍(점수가 아주 낮은 쌍)은 설문에 없다 — 수용률은 '근처 상품 중에서'의 값."""
    out = ["전체"]
    if p["attrs"].get("line") and p["attrs"].get("line") == q["attrs"].get("line"):
        out.append("같은 라인")
    out.append("같은 세부 카테고리" if cat_prefix(p, 3) == cat_prefix(q, 3) else
               "같은 중분류·다른 세부" if cat_prefix(p, 2) == cat_prefix(q, 2) else "다른 중분류")
    out.append("같은 제조사" if p["attrs"].get("maker") == q["attrs"].get("maker") else "다른 제조사")
    for aid, st in direction_steps(p, q, attrs, policy, raw=True).items():
        out.append(f"{attrs[aid]['name']} {'올림' if st > 0 else '내림'}{abs(st)}")
    for aid in ("price_band", "size"):
        a, b = p["attrs"].get(aid), q["attrs"].get(aid)
        if a and b and a != b:
            order = list(attrs[aid]["values"])
            out.append(f"{attrs[aid]['name']} {'올림' if order.index(b) > order.index(a) else '내림'}")
    out += sorted({"차단 " + why.split(":")[0] for why in substitute_violations(p, q, attrs, policy)})
    return out


def cmd_survey(args):
    attrs, cats, products, _, policy, _ = load_context()
    pairs, qs = load_survey()
    if not qs:
        sys.exit("survey/answers/ 에 응답이 없다 — 설문부터 (docs/survey.md)")
    gold = load_gold()
    src = {(r["anchor"], r["candidate"]): r["source"] for r in read_csv("review/gold.csv")}
    cfg, out = policy["survey"], []
    for (a, b), s in sorted(pairs.items()):
        n = qs[a]["n"]
        rate = s["accept"] / n
        verdict = "O" if n >= cfg["min_n"] and rate >= cfg["accept"] else "X" if n >= cfg["min_n"] and rate <= cfg["reject"] else ""
        out.append([a, products[a]["name"], b, products[b]["name"], n, round(rate, 3), round(s["best"] / n, 3), verdict,
                    gold.get((a, b), ""), src.get((a, b), ""), " · ".join(substitute_violations(products[a], products[b], attrs, policy))])
    write_csv("out/survey_pairs.csv", ["a", "a_name", "b", "b_name", "n", "accept", "best", "survey", "gold", "gold_source", "blocked"], out)
    # 설문 자체의 검증: 다수결 정답을 기존 라벨과 대조 (당근 LLM-judge 방식의 goldset 대조)
    agree = {}
    for r in out:
        if r[7] and r[8] != "":
            k = agree.setdefault(r[9], [0, 0])
            k[0] += (r[7] == "O") == (r[8] == 1)
            k[1] += 1
    print(f"응답 문항 {len(qs)}개, 보기 판정 {len(out)}개 → out/survey_pairs.csv")
    print("기존 라벨과 일치: " + ", ".join(f"{s} {a}/{n} ({a / n:.0%})" for s, (a, n) in sorted(agree.items())))
    feats = {}
    for (a, b), s in pairs.items():
        if qs[a]["n"] >= cfg["min_n"]:
            for f in survey_features(products[a], products[b], attrs, policy):
                feats.setdefault(f, []).append(s["accept"] / qs[a]["n"])
    write_csv("out/survey_features.csv", ["feature", "pairs", "accept_mean"],
              sorted(([f, len(v), round(float(np.mean(v)), 3)] for f, v in feats.items()), key=lambda r: -r[2]))
    w = load_weights()
    # 과적합 확인: 홀수 페르소나 판정으로 간선을 만들고 짝수 페르소나 답으로 채점 (정책만 vs 정책 + 설문 판정)
    odd = {f"P{i:02d}" for i in range(1, 31, 2)}
    even_pairs, even_qs = load_survey({f"P{i:02d}" for i in range(2, 31, 2)})
    for name, sv in (("정책만", {}), ("정책 + 설문 판정(홀수)", survey_labels(policy, odd))):
        subs, *_ = build_edges(products, attrs, policy, w, gold, 5, 0.55, sv)
        m = survey_eval(products, attrs, cats, policy, subs, even_pairs, even_qs)[1]
        print(f"  짝수 페르소나로 채점 · {name}: precision {m['precision']} · recall {m['recall']} · top1 {m['top1']}"
              f" · 억지 추천 {m['forced']} · 놓침 {m['missed']}")
    subs, *_ = build_edges(products, attrs, policy, w, gold, 5, 0.55)
    rows, m = survey_eval(products, attrs, cats, policy, subs, pairs, qs)
    write_csv("out/survey_eval.csv", list(rows[0]), [list(r.values()) for r in rows])
    print(f"품절 추천 vs 설문 전체 ({m['questions']}문항): precision {m['precision']} · recall {m['recall']} · top1 {m['top1']}"
          f" · 억지 추천 {m['forced']} · 놓침 {m['missed']} → out/survey_eval.csv")


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
    write_csv("out/edges.csv", ["rel", "a", "a_name", "b", "b_name", "strength", "model_score", "review_or_reason", "anchor"],
              [("SUBSTITUTE", a, name(a), b, name(b), e["strength"], e["model_score"], e["review"], e["anchor"])
               for (a, b), e in sorted(subs.items(), key=lambda x: -x[1]["strength"])]
              + [("COMPLEMENT", a, name(a), b, name(b), e["strength"], "", e["reason"], "")
                 for (a, b), e in sorted(comps.items(), key=lambda x: -x[1]["strength"])])
    # 속성 증강 결과: 정규화된 속성 + 추론된 TPO + 아이덴티티 (상품 1행)
    join = lambda v: "|".join(sorted(v)) if isinstance(v, (set, frozenset, tuple)) else ("" if v is None else str(v))
    dims = list(tpo["dimensions"])
    write_csv("out/products_enriched.csv",
              ["id", "type", "name", "identity", *attrs, *[f"tpo_{d}" for d in dims], *[f"avoid_{d}" for d in dims]],
              [(p["id"], p["type"], p["name"], "/".join(str(x) for x in identity(p, policy)),
                *[join(p["attrs"].get(a)) for a in attrs],
                *[join(p["tpo"].get(d, ())) for d in dims], *[join(p["tpo_excl"].get(d, ())) for d in dims])
               for p in products.values()])
    write_csv("out/combos.csv", ["scene_id", "scene", "rank", "score", "price", "role", "product_id", "product"],
              [(sc["id"], sc["name"], i, cs, total, slot["role"], p["id"], p["name"])
               for sc in tpo["scenes"] for i, (cs, total, items) in enumerate(combos[sc["id"]], 1)
               for slot, p in zip(sc["slots"], items)])
    write_csv("out/blocked.csv", ["kind", "rel", "a", "a_name", "b", "b_name", "score", "reason"],
              [("정책 충돌", r, a, name(a), b, name(b), s, why) for r, a, b, s, why in conflicts]
              + [("정책 차단", r, a, name(a), b, name(b), s, why) for r, a, b, s, why in sorted(blocked, key=lambda x: -x[3])])
    by = lambda key: sum(e["review"] == key for e in subs.values())
    print(f"대체 간선 {len(subs)} (설문 {by('survey')} / 검수O {by('approved')} / 미검수 {by('unreviewed')}) · 보완 간선 {len(comps)}")
    print(f"정책 차단 {len(blocked)} · 정책 충돌 {len(conflicts)} · 콤보 {sum(map(len, combos.values()))} · 트렌드 {len(trends)}")
    print(f"→ out/graph.cypher ({len(stmts)}문), out/edges.csv, out/products_enriched.csv, out/combos.csv, out/blocked.csv, out/audit.md")
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
    rp.add_argument("--source", default="review", help="gold.csv source 칸 (사람=review, AI 1차 검수=claude)")
    rp.set_defaults(fn=cmd_review)
    sub.add_parser("anchor").set_defaults(fn=cmd_anchor)
    sub.add_parser("impact").set_defaults(fn=cmd_impact)
    sub.add_parser("triage").set_defaults(fn=cmd_triage)
    sub.add_parser("admin").set_defaults(fn=cmd_admin)
    op = sub.add_parser("oos")
    op.add_argument("product", nargs="?", help="품절 상품 id 또는 이름 일부")
    op.add_argument("--also-out", nargs="*", help="함께 품절인 상품 (대안에서 뺌)")
    op.add_argument("--context", nargs="*", help="지금 상황 (예: weather=더움 time=심야)")
    op.add_argument("--all", action="store_true", help="전 상품 품절 시 추천을 out/oos_recommendations.csv 로")
    op.set_defaults(fn=cmd_oos)
    sub.add_parser("survey").set_defaults(fn=cmd_survey)
    gp = sub.add_parser("graph")
    gp.add_argument("--k", type=int, default=5, help="상품당 대체 간선 최대 수")
    gp.add_argument("--min-score", type=float, default=0.55, help="미검수 대체 간선의 최소 모델 점수")
    gp.add_argument("--push", action="store_true", help="NEO4J_URI/USER/PASSWORD 로 바로 적재")
    gp.set_defaults(fn=cmd_graph)
    args = ap.parse_args()
    sys.exit(args.fn(args) or 0)


if __name__ == "__main__":
    main()
