"""발표 자료 → docs/presentation.pdf (16:9 슬라이드)
수치는 README·docs 와 같은 출처: out/distill_eval.csv(검증 설문), survey/cost_log.yaml(토큰), docs/rank_eval.md(1순위 적중), out/oos_recommendations.csv(예시).
python tools/make_deck.py"""
import csv
import sys
from pathlib import Path

import yaml
from PIL import Image
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
pdfmetrics.registerFont(TTFont("R", "C:/Windows/Fonts/malgun.ttf"))
pdfmetrics.registerFont(TTFont("B", "C:/Windows/Fonts/malgunbd.ttf"))

W, H, M = 1280, 720, 72
INK, SOFT, MUTE, LINE, BG = (HexColor(x) for x in ("#16181d", "#4b4f5a", "#8a8f9c", "#e3e5ea", "#ffffff"))
BLUE, ORANGE, GRAY, RED, PALE = (HexColor(x) for x in ("#2a78d6", "#eb6834", "#b9bcc4", "#d64545", "#f3f5f8"))
TOTAL = 15


class Deck:
    def __init__(self, path):
        self.c = canvas.Canvas(str(path), pagesize=(W, H))
        self.c.setTitle("GS 품절 대체 추천")
        self.n = 0

    def page(self, kicker=None, title=None):
        if self.n:
            self.c.showPage()
        self.n += 1
        c = self.c
        c.setFillColor(BG); c.rect(0, 0, W, H, fill=1, stroke=0)
        if kicker:
            c.setFillColor(BLUE); c.setFont("B", 16); c.drawString(M, H - 70, kicker)
        if title:
            c.setFillColor(INK); c.setFont("B", 38); c.drawString(M, H - 120, title)
        c.setFillColor(MUTE); c.setFont("R", 13)
        c.drawRightString(W - M, 36, f"{self.n} / {TOTAL}")
        c.drawString(M, 36, "GS 품절 대체 추천")
        return c

    def text(self, x, y, s, size=20, font="R", color=None, width=None, lead=1.5):
        c = self.c
        c.setFont(font, size); c.setFillColor(color or SOFT)
        lines = simpleSplit(s, font, size, width) if width else [s]
        for k, line in enumerate(lines):
            c.drawString(x, y - k * size * lead, line)
        return y - len(lines) * size * lead

    def bullets(self, x, y, items, size=21, width=520, gap=14):
        for it in items:
            self.c.setFillColor(BLUE); self.c.circle(x + 5, y + size * 0.33, 4, fill=1, stroke=0)
            y = self.text(x + 22, y, it, size, width=width - 22) - gap
        return y

    def box(self, x, y, w, h, label, fill=PALE, stroke=LINE, color=INK, size=18, sub=None):
        c = self.c
        c.setFillColor(fill); c.setStrokeColor(stroke); c.setLineWidth(1.5)
        c.roundRect(x, y, w, h, 10, fill=1, stroke=1)
        c.setFillColor(color); c.setFont("B", size)
        cy = y + h / 2 + (8 if sub else -size * 0.35)
        c.drawCentredString(x + w / 2, cy, label)
        if sub:
            c.setFont("R", size - 4); c.setFillColor(SOFT); c.drawCentredString(x + w / 2, cy - size - 2, sub)

    def arrow(self, x1, y1, x2, y2, color=MUTE, dash=False, label=None, width=2):
        c = self.c
        c.setStrokeColor(color); c.setFillColor(color); c.setLineWidth(width)
        c.setDash(6, 5) if dash else c.setDash()
        c.line(x1, y1, x2, y2); c.setDash()
        import math
        a = math.atan2(y2 - y1, x2 - x1)
        p = c.beginPath(); p.moveTo(x2, y2)
        p.lineTo(x2 - 12 * math.cos(a - 0.4), y2 - 12 * math.sin(a - 0.4))
        p.lineTo(x2 - 12 * math.cos(a + 0.4), y2 - 12 * math.sin(a + 0.4)); p.close()
        c.drawPath(p, fill=1, stroke=0)
        if label:
            c.setFont("B", 15); c.drawCentredString((x1 + x2) / 2, (y1 + y2) / 2 + 10, label)

    def big(self, x, y, value, label, color=BLUE, width=None):
        c = self.c
        c.setFillColor(color); c.setFont("B", 64); c.drawString(x, y, value)
        self.text(x, y - 32, label, 18, width=width, lead=1.35)

    def hbars(self, x, y, w, rows, series_colors, fmt, xmax=1.0, barh=34, inner=6, gap=26, labelw=190, legend=None):
        """rows = [(이름, [값...])]. 위에서 아래로."""
        c = self.c
        pw = w - labelw - 90
        if legend:
            lx = x + labelw
            for name, col in legend:
                c.setFillColor(col); c.roundRect(lx, y + 14, 14, 14, 3, fill=1, stroke=0)
                c.setFillColor(SOFT); c.setFont("R", 16); c.drawString(lx + 22, y + 16, name)
                lx += 50 + pdfmetrics.stringWidth(name, "R", 16)
            y -= 30
        n = len(series_colors)
        gh = n * barh + (n - 1) * inner
        bottom = y - len(rows) * (gh + gap) + gap
        for t in (0, 0.25, 0.5, 0.75, 1.0):
            v = xmax * t; gx = x + labelw + pw * t
            c.setStrokeColor(LINE); c.setLineWidth(1); c.line(gx, bottom - 6, gx, y + 4)
            c.setFillColor(MUTE); c.setFont("R", 13); c.drawCentredString(gx, bottom - 24, fmt(v, True))
        for i, (name, vals) in enumerate(rows):
            y0 = y - i * (gh + gap)
            c.setFillColor(INK); c.setFont("B", 18)
            c.drawRightString(x + labelw - 16, y0 - gh / 2 - 6, name)
            for k, v in enumerate(vals):
                col = series_colors[k](i) if callable(series_colors[k]) else series_colors[k]
                by = y0 - (k + 1) * barh - k * inner
                c.setFillColor(col); c.roundRect(x + labelw, by, max(2, pw * v / xmax), barh, 4, fill=1, stroke=0)
                c.setFillColor(INK if col == BLUE else SOFT); c.setFont("B" if col == BLUE else "R", 18)
                c.drawString(x + labelw + pw * v / xmax + 10, by + barh / 2 - 6, fmt(v, False))


def pct(v, axis):
    return f"{v:.0%}" if axis else f"{v:.1%}"


def main():
    dist = {r["method"]: r for r in csv.DictReader(open(ROOT / "out/distill_eval.csv", encoding="utf-8-sig"))}
    log = yaml.safe_load((ROOT / "survey/cost_log.yaml").read_text(encoding="utf-8"))
    run = {r["key"]: r for r in log["runs"]}; su = log["scale_up"]
    tag = run["catalog"]["tokens"]
    tok = [("전수 재설문", su["full_responses"] * run["first"]["tokens"] / run["first"]["responses"], f"환산 · {su['full_responses']:,}응답"),
           ("선별 설문만", su["selective_responses"] * run["round2"]["tokens"] / run["round2"]["responses"], f"환산 · {su['selective_responses']:,}응답"),
           ("증류 모델 + 선별", run["round2"]["tokens"], f"실측 · {run['round2']['responses']:,}응답")]
    oos = {}
    for r in csv.DictReader(open(ROOT / "out/oos_recommendations.csv", encoding="utf-8-sig")):
        if r["alt_name"]:
            oos.setdefault(r["name"], []).append(r["alt_name"])

    d = Deck(ROOT / "docs/presentation.pdf")

    # 1 표지
    c = d.page()
    c.setFillColor(BLUE); c.rect(0, 0, 18, H, fill=1, stroke=0)
    c.setFillColor(BLUE); c.setFont("B", 22); c.drawString(M + 20, H - 230, "cvs-taxonomy")
    c.setFillColor(INK); c.setFont("B", 66); c.drawString(M + 20, H - 320, "GS 품절 대체 추천")
    d.text(M + 22, H - 380, "편의점 상품 온톨로지와 페르소나 설문으로 만든, 점원처럼 권하는 대체 추천", 24)
    d.text(M + 22, 120, f"상품 {su['after']}개 · 페르소나 30명 · 2026-10-23", 18, color=MUTE)

    # 2 목표
    c = d.page("목표", "신라면이 품절이면, 점원은 뭐라고 할까요")
    d.text(M, H - 190, "찾는 상품이 품절일 때 대신 권할 상품 1~3개를, 사람이 납득할 순서로 고르는 것이 목표예요.", 22, width=1100)
    # 예시 카드
    ex = [("신라면", oos.get("신라면", [])), ("코카콜라 500ml", oos.get("코카콜라 500ml", [])), ("튀김우동 큰사발", oos.get("튀김우동 큰사발", []))]
    cw = 350
    for k, (name, recs) in enumerate(ex):
        x = M + k * (cw + 25); y = 150
        c.setFillColor(PALE); c.roundRect(x, y, cw, 330, 14, fill=1, stroke=0)
        c.setFillColor(RED); c.setFont("B", 15); c.drawString(x + 26, y + 290, "품절")
        c.setFillColor(INK); c.setFont("B", 26); c.drawString(x + 26, y + 252, name)
        c.setStrokeColor(LINE); c.line(x + 26, y + 232, x + cw - 26, y + 232)
        for j, r in enumerate(recs[:3]):
            yy = y + 190 - j * 58
            c.setFillColor(BLUE if j < 2 else GRAY); c.circle(x + 42, yy + 7, 15, fill=1, stroke=0)
            c.setFillColor(BG); c.setFont("B", 16); c.drawCentredString(x + 42, yy + 1, str(j + 1))
            c.setFillColor(INK if j < 2 else SOFT); c.setFont("B" if j < 2 else "R", 20); c.drawString(x + 70, yy, r)
    d.text(M, 110, "1·2순위는 \"대신 이건 어떠세요\", 3순위는 \"이것도 있어요\"로 약하게 보여 줘요.", 17, color=MUTE)

    # 3 개요
    c = d.page("개발 과정", "여섯 단계")
    steps = [("1", "추천 기반 설계", "속성 벡터 + 정책"), ("2", "목표 재정의", "품절 대체, 방향 정책"), ("3", "사람다움 측정", "페르소나 30명 설문"),
             ("4", "설문 비용 절감", "선별 설문 + 증류 모델"), ("5", "빈칸 해소", "카탈로그 118 → 238개"), ("6", "대체 관계 시각화", "3D 대체 그래프")]
    bw, gap = 170, 22
    for k, (n, t, s) in enumerate(steps):
        x = M + k * (bw + gap)
        c.setFillColor(BLUE); c.circle(x + 26, 440, 22, fill=1, stroke=0)
        c.setFillColor(BG); c.setFont("B", 22); c.drawCentredString(x + 26, 432, n)
        if k < len(steps) - 1:
            c.setStrokeColor(LINE); c.setLineWidth(3); c.line(x + 54, 440, x + bw + gap - 2, 440)
        c.setFillColor(INK); c.setFont("B", 21); c.drawString(x, 380, t)
        d.text(x, 350, s, 16, width=bw)

    # 4 추천 기반
    c = d.page("1. 추천 기반 설계", "포카칩이 없으면 수미칩을 권해도 될까요")
    d.text(M, H - 180, "상품명만으로는 판단할 수 없어서, 속성을 붙여 벡터로 바꾸고 가까운 상품을 후보로 잡았어요.", 21, width=1120)
    xs = [M, M + 195, M + 390, M + 570, M + 765]
    labels = [("상품명", "포카칩 오리지널"), ("속성 태깅", "원료·식감·맛·맵기"), ("속성 벡터", None), ("가까운 상품", "수미칩·포테토칩"), ("정책 필터", "규격·아이덴티티")]
    for k, (x, (t, s)) in enumerate(zip(xs, labels)):
        wdt = 170 if k != 2 else 150
        d.box(x, 380, wdt, 90, t, sub=s, fill=PALE if k < 4 else HexColor("#e8f0fb"))
        if k:
            d.arrow(xs[k - 1] + (170 if k - 1 != 2 else 150) + 4, 425, x - 6, 425)
    d.box(M + 990, 465, 146, 60, "대체 후보", fill=HexColor("#e8f0fb"), stroke=BLUE)
    d.arrow(M + 940, 445, M + 984, 492, color=BLUE)
    c.setFillColor(BLUE); c.setFont("B", 14); c.drawString(M + 942, 482, "통과")
    d.box(M + 990, 325, 146, 60, "AA → AAA", fill=HexColor("#fdecec"), stroke=RED, color=RED)
    d.arrow(M + 940, 405, M + 984, 358, color=RED, dash=True)
    c.setFillColor(RED); c.setFont("B", 14); c.drawString(M + 942, 360, "차단")
    d.bullets(M, 260, ["속성이 비슷해도 대신 못 쓰는 쌍은 정책으로 막음 (AA 건전지 → AAA)",
                       "국물베이스(소고기·된장·해물)는 손님이 구분하지 않음. 얼큰하면 신라면 ↔ 안성탕면 ↔ 너구리",
                       "노이즈 속성 5개 제외 → 종합 점수 과자 0.842 → 0.866, 라면 0.907 → 0.944"], size=19, width=1130, gap=8)

    # 5 방향 정책
    c = d.page("2. 목표 재정의", "대체에는 방향이 있어요")
    d.box(M, 430, 230, 80, "코카콜라", size=22)
    d.box(M + 470, 430, 230, 80, "코카콜라 제로", size=22)
    d.arrow(M + 236, 490, M + 462, 490, color=BLUE, label="허용: 당만 줄어듦", width=3)
    d.arrow(M + 462, 450, M + 236, 450, color=RED, dash=True, label="", width=3)
    c.setFillColor(RED); c.setFont("B", 15); c.drawCentredString(M + 350, 422, "금지: 제로를 고른 이유가 깨짐")
    d.box(M, 280, 230, 80, "진라면 순한맛", size=22)
    d.box(M + 470, 280, 230, 80, "진라면 매운맛", size=22)
    d.arrow(M + 236, 320, M + 462, 320, color=RED, dash=True, width=3)
    c.setFillColor(RED); c.setFont("B", 15); c.drawCentredString(M + 350, 330, "금지: 더 매움")
    c.setFillColor(PALE); c.roundRect(M + 790, 150, 346, 400, 14, fill=1, stroke=0)
    d.text(M + 816, 510, "외부 라벨로 검증", 20, "B", INK)
    d.text(M + 816, 480, "Amazon ESCI 편의점 관련 25,079쌍", 16, color=MUTE)
    d.big(M + 816, 390, "87%", "펩시 질의에 다른 브랜드는 '상관없음'", RED, width=300)
    d.big(M + 816, 250, "86~93%", "건전지·라면은 브랜드가 달라도 '대체 가능'", width=300)
    d.text(M, 200, "당·맵기·카페인을 방향별 정책으로 정하고,", 19)
    d.text(M, 170, "정책으로 판단이 안 되는 16문항만 관리자에게 물었어요.", 19)

    # 6 페르소나 설문
    c = d.page("3. 사람다움 측정", "판매 데이터 대신, 손님 30명을 만들었어요")
    flow = [("품절 상품", "서울우유 200ml"), ("보기 7개", "매대의 비슷한 상품"), ("페르소나 30명", "당뇨·다이어트·맵찔이…"), ("상품쌍 수용률", None)]
    for k, (t, s) in enumerate(flow):
        x = M + k * 228
        d.box(x, 420, 196, 90, t, sub=s)
        if k:
            d.arrow(x - 28, 465, x - 6, 465)
    outs = [("50% 이상 → O", BLUE, "#e8f0fb"), ("20% 이하 → X", RED, "#fdecec"), ("그 사이 → 보류", MUTE, "#f3f5f8")]
    for k, (t, col, fill) in enumerate(outs):
        y = 520 - k * 70
        d.box(M + 950, y - 25, 186, 52, t, fill=HexColor(fill), stroke=col, color=col, size=17)
        d.arrow(M + 884, 465, M + 944, y, color=col)
    c.setFillColor(PALE); c.roundRect(M, 130, 1136, 210, 14, fill=1, stroke=0)
    d.text(M + 30, 300, "서울우유가 없을 때 바나나맛우유를 권해도 될까요?", 22, "B", INK)
    d.big(M + 30, 200, "74%", "괜찮다고 답함")
    d.big(M + 330, 200, "0명", "당뇨·다이어트 응답자 중 받아들인 사람", RED)
    d.text(M + 760, 230, "다수결로는 권하지만,", 20, "B", INK)
    d.text(M + 760, 198, "권하면 안 되는 사람이 분명하므로 막음", 20, "B", INK)

    # 7 결과: 1순위 적중
    c = d.page("3. 사람다움 측정 · 결과", "인기순의 두 배 가까운 1순위 적중")
    d.hbars(M, 520, 760, [("우리 추천", [0.893]), ("벡터 유사도순", [0.858]), ("인기순", [0.483]), ("무작위", [0.411])],
            [lambda i: BLUE if i == 0 else GRAY], pct)
    d.text(M, 560, "손님이 1순위를 고른 비율 · 상품 118개, 짝수 페르소나 채점", 16, color=MUTE)
    d.big(M + 860, 470, "94.6%", "화면 3개 중 하나를 고른 비율")
    d.text(M + 860, 330, "인기순은 비슷한 상품만 모은 보기", 17, width=320)
    d.text(M + 860, 302, "안에서도 무작위와 큰 차이가 없음.", 17, width=320)
    d.text(M + 860, 260, "품절 대체는 많이 팔리는 상품보다", 17, "B", INK)
    d.text(M + 860, 232, "찾던 사람이 받아들일 상품", 17, "B", INK)

    # 8 비용 문제와 증류
    c = d.page("4. 설문 비용 절감", "상품 5개 추가에 166만 토큰")
    d.text(M, H - 180, "짜장라면 5개를 추가하자 보기가 바뀐 13문항을 30명에게 다시 물어야 했어요.", 21, width=1120)
    cy = 300
    d.box(M, cy - 45, 190, 90, "새 상품쌍")
    d.box(M + 270, cy - 45, 220, 90, "증류 모델", sub="예측 수용률")
    d.arrow(M + 196, cy, M + 264, cy)
    for k, (t, s, col, fill) in enumerate([("O로 판정", "0.55 이상 · 묻지 않음", BLUE, "#e8f0fb"), ("X로 판정", "0.3 이하 · 묻지 않음", RED, "#fdecec"),
                                          ("선별 설문", "그 사이 · 관련 페르소나 5~10명", ORANGE, "#fdf0e9")]):
        y = cy + 75 - k * 115
        d.box(M + 600, y, 300, 80, t, sub=s, fill=HexColor(fill), stroke=col, color=col)
        d.arrow(M + 496, cy, M + 594, y + 40, color=col)
    d.arrow(M + 700, cy - 155, M + 400, cy - 51, color=ORANGE, dash=True)
    c.setFillColor(ORANGE); c.setFont("B", 15); c.drawString(M + 330, cy - 140, "설문 판정으로 재학습")
    d.big(M + 960, 380, "0.942", "O/X 가르는 힘(AUC)")
    d.text(M + 960, 290, "벡터 점수 0.869", 17, color=MUTE)
    d.text(M + 960, 262, "정책 규칙 0.832", 17, color=MUTE)
    d.text(M, 80, "아이디어 출처: 당근 Taxonomy 팀, 전부 묻지 말고 후보를 좁힌 뒤 애매한 것만 묻기", 16, color=MUTE)

    # 9 토큰 차트
    c = d.page("4. 설문 비용 절감 · 결과", f"상품쌍은 4배, 토큰은 15%")
    d.text(M, H - 170, f"상품 {su['before']} → {su['after']}개, 상품쌍 {su['pairs_before']:,} → {su['pairs_after']:,}개를 판정할 때 든 토큰", 19, color=MUTE)
    xmax = 8e6; px, pw = M + 260, 760
    c.setFillColor(ORANGE); c.roundRect(px, 500, 14, 14, 3, fill=1, stroke=0)
    d.text(px + 22, 502, f"속성 태깅 (공통, {tag / 1e3:.0f}k)", 16)
    c.setFillColor(BLUE); c.roundRect(px + 260, 500, 14, 14, 3, fill=1, stroke=0)
    d.text(px + 282, 502, "대체 판정 설문", 16)
    for t in range(0, 9, 2):
        gx = px + pw * t * 1e6 / xmax
        c.setStrokeColor(LINE); c.line(gx, 150, gx, 485)
        c.setFillColor(MUTE); c.setFont("R", 13); c.drawCentredString(gx, 130, f"{t}M" if t else "0")
    for k, (name, val, sub) in enumerate(tok):
        y = 420 - k * 110
        c.setFillColor(INK); c.setFont("B", 20); c.drawRightString(px - 18, y + 24, name)
        c.setFillColor(MUTE); c.setFont("R", 14); c.drawRightString(px - 18, y + 2, sub)
        c.setFillColor(ORANGE); c.rect(px, y, pw * tag / xmax, 54, fill=1, stroke=0)
        c.setFillColor(BLUE); c.roundRect(px + pw * tag / xmax + 2, y, pw * val / xmax, 54, 4, fill=1, stroke=0)
        tot = (tag + val) / 1e6
        c.setFillColor(INK); c.setFont("B", 24); c.drawString(px + pw * (tag + val) / xmax + 12, y + 18, f"{tot:.1f}M" if k < 2 else f"{tot:.2f}M")

    # 10 빈칸 해소
    c = d.page("5. 빈칸 해소", "빈칸에 가장 가까운 상품을 채우면?")
    d.text(M, H - 180, "손님은 대신 살 게 있다는데 아무것도 못 보여 준 경우가 4%. 가장 가까운 상품으로 채워 봤더니:", 21, width=1130)
    fails = [("마스크", "생리대"), ("비닐우산", "생리대"), ("자일리톨 껌", "하리보")]
    for k, (a, b) in enumerate(fails):
        y = 420 - k * 85
        d.box(M, y, 220, 64, a + " 품절", size=19)
        d.arrow(M + 226, y + 32, M + 330, y + 32, color=RED, dash=True)
        d.box(M + 336, y, 200, 64, b, fill=HexColor("#fdecec"), stroke=RED, color=RED, size=19)
    c.setFillColor(PALE); c.roundRect(M + 640, 140, 496, 370, 14, fill=1, stroke=0)
    d.text(M + 670, 470, "원인은 카탈로그", 24, "B", INK)
    d.text(M + 670, 436, "우동·우산이 하나뿐이면 권할 상품이 없음", 17, width=440)
    d.big(M + 670, 345, f"{su['before']} → {su['after']}", "상품 수 (과자 40 · 라면 30 · 음료 30 · 생필품 20 추가)", width=440)
    d.big(M + 670, 220, "9 → 2", "권할 대체 없음")
    d.text(M, 95, "이제 튀김우동 품절이면 가쓰오우동, 비닐우산 품절이면 3단 접이식 우산을 권해요.", 18, color=SOFT)

    # 11 검증 설문
    c = d.page("5. 빈칸 해소 · 검증", "판정에 쓰지 않은 설문으로 다시 채점")
    b0, b1 = dist["검증 · 정책+벡터만"], dist["검증 · 증류 모델 + 선별 설문"]
    d.text(M, 560, "새 상품 24개 · 검증 응답 240건", 16, color=MUTE)
    d.hbars(M, 520, 760, [("셋 중 하나를 고름", [float(b0["pick"]), float(b1["pick"])]), ("1순위를 고름", [float(b0["first"]), float(b1["first"])])],
            [GRAY, BLUE], pct, legend=[("정책 + 벡터만", GRAY), ("+ 증류 모델 + 선별 설문", BLUE)])
    c.setFillColor(HexColor("#fdecec")); c.roundRect(M + 820, 170, 316, 350, 14, fill=1, stroke=0)
    d.text(M + 845, 480, "남은 문제", 20, "B", RED)
    d.big(M + 845, 380, "15%", "모델이 'O'라고 확신한 쌍 중 실제로 대부분이 거절", RED, width=270)
    d.text(M + 845, 260, "'X' 판정은 거의 틀리지 않음.", 17, width=270)
    d.text(M + 845, 228, "다음: O 판정 기준 상향", 17, "B", INK)

    # 12 채점 착시
    c = d.page("5. 빈칸 해소 · 채점", "확장 직후 헛추천이 급증한 이유")
    d.text(M, H - 180, "추천은 그대로였고, 채점 방식에 문제가 있었어요.", 22, width=1130)
    for k, (t, s) in enumerate([("보기 일부만 물은 응답", "묻지 않은 보기를 '안 골랐다'로 셈 → 일부만 물은 응답은 채점에서 제외"),
                                ("본 적 없는 새 상품 추천", "옛 응답자가 못 본 상품을 오답으로 셈 → '확인 불가'로 따로 집계")]):
        x = M + k * 580
        c.setFillColor(PALE); c.roundRect(x, 220, 550, 260, 14, fill=1, stroke=0)
        d.text(x + 30, 430, t, 24, "B", INK)
        d.text(x + 30, 380, s, 19, width=490)

    # 13 시각화
    c = d.page("6. 대체 관계 시각화", "상품 238개의 대체 관계")
    gif = Image.open(ROOT / "docs/product_graph_3d.gif"); gif.seek(40)
    frame = gif.convert("RGB")
    c.drawImage(ImageReader(frame), M, 110, width=690, height=690 * frame.height / frame.width)
    d.bullets(M + 740, 500, ["점 = 상품, 선 = 대체 관계. 서로 대신하는 상품끼리 모임",
                             "음료 안에서도 물·차, 커피, 탄산, 주류가 따로 모임",
                             "생필품은 상품당 대체 관계 2.3개 (과자 5.9, 라면 6.7). 규격이 맞아야 대신할 수 있어서"], size=19, width=400)

    # 14 예시 표
    c = d.page("결과", "품절이면 이 순서로 권해요")
    names = ["코카콜라 제로 500ml", "불닭볶음면 큰컵", "진라면 순한맛", "빼빼로 오리지널", "하늘보리 500ml", "서울우유 200ml", "2080 여행용 칫솔치약 세트"]
    cols = [M, M + 330, M + 610, M + 870]
    y = 540
    c.setFillColor(MUTE); c.setFont("B", 16)
    for x, h in zip(cols, ["품절 상품", "1순위", "2순위", "3순위"]):
        c.drawString(x, y, h)
    c.setStrokeColor(LINE); c.setLineWidth(1.5); c.line(M, y - 14, W - M, y - 14)
    for k, n in enumerate(names):
        yy = y - 52 - k * 50
        c.setFillColor(INK); c.setFont("B", 18); c.drawString(cols[0], yy, n)
        for j, r in enumerate(oos.get(n, [])[:3]):
            c.setFillColor(INK if j < 2 else MUTE); c.setFont("R", 18); c.drawString(cols[j + 1], yy, r)
        c.setStrokeColor(LINE); c.setLineWidth(1); c.line(M, yy - 18, W - M, yy - 18)

    # 15 한계와 다음
    c = d.page("정리", "한계와 다음 과제")
    d.text(M, 520, "한계", 24, "B", RED)
    d.bullets(M, 470, ["응답자가 전부 AI 페르소나", "30명이 실제 GS앱 이용자 분포를 대변하지 않음 (분포 비공개)",
                       "모델의 O 판정이 후함 (15% 오판)"], size=20, width=520)
    d.text(M + 610, 520, "다음 과제", 24, "B", BLUE)
    d.bullets(M + 610, 470, ["증류 모델 O 판정 기준 상향", "설문과 정책이 부딪히는 36쌍 결정", "실제 사람 설문으로 페르소나 교정",
                             "실제 GS 카탈로그 적용"], size=20, width=520)
    d.text(M, 130, "github.com/aeeengsungwoo/cvs-taxonomy", 18, color=MUTE)

    assert d.n == TOTAL, d.n
    d.c.save()
    print(ROOT / "docs/presentation.pdf", d.n, "쪽")


if __name__ == "__main__":
    main()
