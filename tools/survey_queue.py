"""선별 설문 대기열. 전 상품을 30명에게 다시 묻지 않고, 확인 안 된 것만 관련 있는 페르소나에게 묻는다(docs/limits_and_next.md 대안 A).
묻는 것: ① 설문을 한 번도 안 받은 상품(새 상품)은 보기 전부, ② 기존 상품은 지금 1~3순위로 보여 주는데 설문 판정이 없는 보기만.
증류 모델이 켜져 있으면(policy distill) 그중 모델이 확신하는 보기(수용 확률 ≥ accept 또는 ≤ reject)는 묻지 않는다.
누구에게: 같은 중분류 문항에서 skip(살 일 없음)하지 않은 비율이 높은 페르소나 순으로 최대 10명, 최소 min_n 명.

python tools/survey_queue.py [--no-model]   →  survey/queue.json (문항·보기·응답자), 전수 재설문 대비 응답 수 출력"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cvs  # noqa: E402
from survey_build import question  # noqa: E402

MAX_PEOPLE = 10


def relevance(pid, products, answers):
    """페르소나별로 비슷한 상품(같은 중분류, 없으면 대분류) 문항에서 살 일이 있다고 답한 비율."""
    for depth in (2, 1):
        key = cvs.cat_prefix(products[pid], depth)
        seen = {}
        for a in answers:
            if a["q"] in products and cvs.cat_prefix(products[a["q"]], depth) == key:
                s = seen.setdefault(a["who"], [0, 0])
                s[0] += not a["skip"]
                s[1] += 1
        if seen:
            return {who: buy / n for who, (buy, n) in seen.items()}
    return {}


def plan(use_model=True, write=True):
    """반환 (대기열, 전수 재설문 문항 수, 페르소나 수)."""
    ctx = cvs.load_context()
    attrs, cats, products, _, policy, _ = ctx
    w = cvs.load_weights()
    dcfg = policy.get("distill", {})
    pred = cvs.distill_predictions(products, attrs, policy, w) if use_model and dcfg.get("enabled") else {}
    subs, *_ = cvs.build_edges(products, attrs, policy, w, cvs.load_gold(), 5, 0.55, pred=pred)
    sure = lambda a, b: (a, b) in pred and (pred[(a, b)] >= dcfg["accept"] or pred[(a, b)] <= dcfg["reject"])
    raw = {r["id"]: r for r in cvs.read_csv("data/products.csv")}
    answers = list(cvs.survey_answers())
    pairs, qs = cvs.load_survey()
    judged = {k for k, s in pairs.items() if s["n"] >= policy["survey"]["min_n"]}
    personas = sorted({a["who"] for a in answers})
    shown = {}
    for a in answers:
        shown.setdefault(a["q"], set()).update(a["shown"])
    queue, full = [], 0
    for pid in products:
        q = question(pid, ctx, subs, raw)
        full += not {o["id"] for o in q["options"]} <= shown.get(pid, set())  # 전수 방식: 보기가 바뀐 문항은 30명에게 다시 묻는다
        if pid in qs:
            top3 = {b for b, *_ in cvs.oos_recommend(pid, products, attrs, cats, policy, subs)}
            q["options"] = [o for o in q["options"] if o["id"] in top3 and (pid, o["id"]) not in judged]
        q["options"] = [o for o in q["options"] if not sure(pid, o["id"])]
        if not q["options"]:
            continue
        rel = relevance(pid, products, answers)
        ranked = sorted(personas, key=lambda w: (-rel.get(w, 0), w))
        pick = [w for w in ranked if rel.get(w, 0) >= 0.5][:MAX_PEOPLE]
        q["personas"] = pick if len(pick) >= policy["survey"]["min_n"] else ranked[:policy["survey"]["min_n"]]
        queue.append(q)
    if write:
        (cvs.ROOT / "survey/queue.json").write_text(json.dumps(queue, ensure_ascii=False, indent=1), encoding="utf-8")
    return queue, full, len(personas)


def main():
    queue, full, people = plan("--no-model" not in sys.argv)
    asked = sum(len(q["personas"]) for q in queue)
    print(f"선별 설문 {len(queue)}문항 · 보기 {sum(len(q['options']) for q in queue)}개 · 응답 {asked}건 → survey/queue.json")
    print(f"전수 재설문이었다면 {full}문항 × {people}명 = 응답 {full * people}건")
    for q in queue:
        print(f"  {q['q']} {q['name']}: 보기 {len(q['options'])}개 · {len(q['personas'])}명")


if __name__ == "__main__":
    main()
