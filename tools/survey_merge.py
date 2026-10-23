"""선별 설문 응답을 survey/answers/Pxx.json 에 덧붙인다. 응답마다 그때 본 보기(shown)와 라운드 이름을 남긴다.
보기 밖 ID 는 버리고, 대기열에 없는 (페르소나, 문항) 은 받지 않는다.

python tools/survey_merge.py <라운드 이름> <응답 JSON>...   (응답 JSON = {"P01": [{q, skip, accept, best, why}], ...})"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(name, *paths):
    queue = {q["q"]: q for q in json.loads((ROOT / "survey/queue.json").read_text(encoding="utf-8"))}
    added = dropped = 0
    for path in paths:
        for who, answers in json.loads(Path(path).read_text(encoding="utf-8")).items():
            f = ROOT / f"survey/answers/{who}.json"
            d = json.loads(f.read_text(encoding="utf-8"))
            for a in answers:
                q = queue.get(a["q"])
                if not q or who not in q["personas"]:
                    dropped += 1
                    continue
                shown = [o["id"] for o in q["options"]]
                accept = [b for b in a.get("accept") or [] if b in shown]
                d["answers"].append({"q": a["q"], "skip": bool(a.get("skip")), "accept": accept,
                                     "best": a.get("best") if a.get("best") in accept else (accept[0] if accept else None),
                                     "why": a.get("why", ""), "shown": shown, "round": name})
                added += 1
            f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    print(f"라운드 {name}: 응답 {added}건 추가, 대기열 밖 {dropped}건 버림")


if __name__ == "__main__":
    main(*sys.argv[1:])
