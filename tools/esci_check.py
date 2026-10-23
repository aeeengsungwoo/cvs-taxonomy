"""Amazon ESCI(Shopping Queries Dataset, Apache-2.0)의 사람 라벨로 대체 정책을 검증한다.
ESCI 라벨: E 정확히 맞음 · S 일부 조건은 못 맞추지만 기능상 대체 가능 · C 같이 쓰는 상품 · I 핵심 조건을 어김
정책마다 '질의 조건 → 상품 조건' 쌍을 정의하고, 그 쌍에 붙은 사람 라벨 분포를 잰다.
대체 인정률 = (E+S) / 전체. 핵심 조건 위반률 = I / 전체.

python tools/esci_check.py <esci 폴더>   →  out/esci_policy_check.csv, out/esci_samples.csv
데이터: https://github.com/amazon-science/esci-data (원본은 커밋하지 않는다, 1.1GB)"""
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent

ZERO = r"\b(?:diet|zero|sugar[- ]?free|no sugar|unsweetened)\b|ゼロ|無糖|糖類ゼロ|カロリーゼロ"
DECAF = r"\bdecaf|decaffeinated|caffeine[- ]free\b|デカフェ|カフェインレス|ノンカフェイン"
SPICY = r"\b(?:spicy|hot|fire|chili|buldak)\b|辛"
MILD = r"\b(?:mild|original|plain)\b"
COLA = r"\b(?:cola|coke|pepsi|soda|sprite|7up|ginger ale|dr pepper)\b|コーラ|サイダー|炭酸"
COFFEE = r"\bcoffee|espresso|latte|cold brew\b|コーヒー|珈琲"
NOODLE = r"\b(?:ramen|ramyun|instant noodles?|cup noodles?|noodle soup)\b|ラーメン|カップ麺|カップヌードル|焼きそば"
CHIPS = r"\b(?:chips|crisps|pringles|doritos|cheetos)\b|ポテトチップス|スナック菓子"
SNACKS = CHIPS + r"|\b(?:cookies|crackers)\b|クッキー|クラッカー"
BRANDS = {
    "coca-cola": r"coca[- ]?cola|\bcoke\b|コカ・?コーラ", "pepsi": r"pepsi|ペプシ", "duracell": r"duracell|デュラセル",
    "energizer": r"energizer|エナジャイザー", "nissin": r"nissin|日清|cup noodles", "samyang": r"samyang|buldak|三養",
    "nongshim": r"nongshim|shin ramyun|農心|辛ラーメン", "pringles": r"pringles|プリングルズ", "lays": r"\blay'?s\b",
    "oral-b": r"oral[- ]?b|オーラルb", "colgate": r"colgate", "gillette": r"gillette|ジレット",
}

has = lambda s, pat: s.str.contains(pat, case=False, regex=True, na=False)


def checks(df):
    q, t = df["query"], df["product_title"]
    aa_q = has(q, r"\baa\b(?!a)|単3")
    aaa_q = has(q, r"\baaa\b|単4")
    aa_t = has(t, r"\baa\b(?!a)|単3") & ~has(t, r"\baaa\b|単4")
    aaa_t = has(t, r"\baaa\b|単4") & ~has(t, r"\baa\b(?!a)|単3")
    bat = has(q, r"batter|電池")
    return [
        # (정책, 우리 정책의 판단, 질의 조건, 상품 조건)
        ("당: 제로 → 일반 (탄산)", "금지", has(q, COLA) & has(q, ZERO), has(t, COLA) & ~has(t, ZERO)),
        ("당: 일반 → 제로 (탄산)", "감점 허용", has(q, COLA) & ~has(q, ZERO), has(t, COLA) & has(t, ZERO)),
        ("당: 제로 → 제로 (탄산, 기준선)", "허용", has(q, COLA) & has(q, ZERO), has(t, COLA) & has(t, ZERO)),
        ("카페인: 디카페인 → 일반 커피", "금지", has(q, COFFEE) & has(q, DECAF), has(t, COFFEE) & ~has(t, DECAF)),
        ("카페인: 일반 → 디카페인 커피", "감점 허용", has(q, COFFEE) & ~has(q, DECAF), has(t, COFFEE) & has(t, DECAF)),
        ("맵기: 매운 라면 → 안 매운 라면", "1단계까지", has(q, NOODLE) & has(q, SPICY), has(t, NOODLE) & ~has(t, SPICY)),
        ("맵기: 순한·기본 라면 → 매운 라면", "중 이상 금지", has(q, NOODLE) & has(q, MILD) & ~has(q, SPICY), has(t, NOODLE) & has(t, SPICY)),
        ("맵기: 매운 과자 → 안 매운 과자", "1단계까지", has(q, CHIPS) & has(q, SPICY), has(t, CHIPS) & ~has(t, SPICY)),
        ("규격: AA → AAA 건전지", "금지", bat & aa_q & ~aaa_q, aaa_t),
        ("규격: AAA → AA 건전지", "금지", bat & aaa_q, aa_t),
        ("규격: AA → AA (기준선)", "허용", bat & aa_q & ~aaa_q, aa_t),
    ]


def brand_check(df):
    q, t, b = df["query"], df["product_title"], df["product_brand"].fillna("")
    rows = []
    for name, pat in BRANDS.items():
        qm = has(q, pat)
        same = has(t, pat) | has(b, pat)
        rows.append((name, qm & same, qm & ~same))
    return rows


def dist(sub):
    n = len(sub)
    c = sub["esci_label"].value_counts()
    return n, *(round(c.get(k, 0) / n, 3) if n else None for k in "ESCI")


def main(folder):
    folder = Path(folder)
    ex = pd.read_parquet(folder / "shopping_queries_dataset_examples.parquet",
                         columns=["query", "product_id", "product_locale", "esci_label"])
    ex = ex[ex["product_locale"].isin(["us", "jp"])]
    domain = has(ex["query"], "|".join([COLA, COFFEE, NOODLE, SNACKS, r"batter|電池", *BRANDS.values()]))
    ex = ex[domain]
    pr = pd.read_parquet(folder / "shopping_queries_dataset_products.parquet",
                         columns=["product_id", "product_locale", "product_title", "product_brand"])
    df = ex.merge(pr, on=["product_id", "product_locale"], how="left")
    print(f"편의점 관련 판정 {len(df):,}개 (US·JP)")
    out, samples = [], []
    for name, ours, qm, tm in checks(df):
        sub = df[qm & tm]
        n, e, s, c, i = dist(sub)
        ok = None if not n else round(e + s, 3)
        out.append((name, ours, n, e, s, c, i, ok))
        samples += [(name, r.query, r.product_title[:120], r.esci_label) for r in sub.sample(min(5, n), random_state=0).itertuples()]
    for name, same, diff in brand_check(df):
        for kind, m in (("같은 브랜드", same), ("다른 브랜드", diff)):
            n, e, s, c, i = dist(df[m])
            out.append((f"브랜드: {name} 질의 → {kind}", "라인 앵커", n, e, s, c, i, None if not n else round(e + s, 3)))
    res = pd.DataFrame(out, columns=["policy", "ours", "n", "E", "S", "C", "I", "accept_ES"])
    (ROOT / "out").mkdir(exist_ok=True)
    res.to_csv(ROOT / "out/esci_policy_check.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(samples, columns=["policy", "query", "product_title", "label"]).to_csv(
        ROOT / "out/esci_samples.csv", index=False, encoding="utf-8-sig")
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(res.to_string(index=False))


if __name__ == "__main__":
    main(sys.argv[1])
