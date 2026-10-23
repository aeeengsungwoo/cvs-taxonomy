"""품절 대체 설문 문항 생성. 상품마다 '이게 품절이면 대신 사겠는가'를 묻는 보기 목록을 만든다.
보기 = 지금 추천(oos 상위 3) + 점수 근처 상품(정책이 막은 것 포함) + 같은 라인, 최대 7개, 섞어서.
추천이 고른 것인지 정책이 막은 것인지는 응답자에게 보이지 않는다.

python tools/survey_build.py   →  survey/questions.json, survey/questions.txt (에이전트에게 주는 문항)"""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cvs  # noqa: E402

MAX_OPTIONS = 7
SHOW = [("price_band", "가격"), ("size", "용량"), ("flavor", "맛"), ("spicy", "맵기"), ("sugar", "당"),
        ("caffeine", "카페인"), ("spec", "규격")]


def describe(raw):
    parts = [f"{label} {raw[aid].replace('|', '·')}" for aid, label in SHOW if raw.get(aid)]
    return ", ".join(parts)


def shuffled(qid, ids):
    return sorted(ids, key=lambda i: hashlib.md5(f"{qid}:{i}".encode()).hexdigest())


def main():
    attrs, cats, products, _, policy, _ = cvs.load_context()
    w, gold = cvs.load_weights(), cvs.load_gold()
    subs, *_ = cvs.build_edges(products, attrs, policy, w, gold, 5, 0.55)
    raw = {r["id"]: r for r in cvs.read_csv("data/products.csv")}
    questions = []
    for pid, p in products.items():
        picks = [b for b, *_ in cvs.oos_recommend(pid, products, attrs, cats, policy, subs)]
        near = [q for _, q in cvs.ranked(products, attrs, w, pid)]
        line = [q for q in near if p["attrs"].get("line") and products[q]["attrs"].get("line") == p["attrs"]["line"]]
        opts = list(dict.fromkeys(picks + line + near))[:MAX_OPTIONS]
        questions.append({"q": pid, "name": p["name"], "desc": describe(raw[pid]),
                          "options": [{"id": o, "name": products[o]["name"], "desc": describe(raw[o])}
                                      for o in shuffled(pid, opts)]})
    out = cvs.ROOT / "survey"
    out.mkdir(exist_ok=True)
    (out / "questions.json").write_text(json.dumps(questions, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = []
    for q in questions:
        lines.append(f"[{q['q']}] {q['name']} ({q['desc']}) 품절")
        lines += [f"  {o['id']} {o['name']} ({o['desc']})" for o in q["options"]]
    (out / "questions.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"문항 {len(questions)}개, 보기 {sum(len(q['options']) for q in questions)}개 → survey/questions.json")


if __name__ == "__main__":
    main()
