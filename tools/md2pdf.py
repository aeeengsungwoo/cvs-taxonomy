"""docs/*.md → PDF (맑은 고딕). 사용: python tools/md2pdf.py docs/design.md docs/design.pdf"""
import sys

import markdown
from fpdf import FPDF, FontFace

FONT = "C:/Windows/Fonts/malgun.ttf"
FONT_B = "C:/Windows/Fonts/malgunbd.ttf"

src, dst = sys.argv[1], sys.argv[2]
html = markdown.markdown(open(src, encoding="utf-8").read(), extensions=["tables", "fenced_code"])
html = html.replace("<code>", "").replace("</code>", "")  # 고정폭 폰트에 한글이 없어서 본문 폰트로
html = html.replace("<td>", '<td align="left">').replace("<th>", '<th align="left">')  # 기본 양쪽정렬은 셀 글자 간격이 벌어짐

pdf = FPDF(format="A4")
pdf.set_margins(16, 16, 16)
pdf.set_auto_page_break(True, 16)
pdf.add_font("malgun", "", FONT)
pdf.add_font("malgun", "B", FONT_B)
pdf.add_font("malgun", "I", FONT)
pdf.add_font("malgun", "BI", FONT_B)
pdf.add_page()
pdf.set_font("malgun", size=10)
pdf.write_html(html, font_family="malgun", tag_styles={
    "h1": FontFace(color=(20, 20, 20), size_pt=20), "h2": FontFace(color=(20, 20, 20), size_pt=14),
    "h3": FontFace(color=(40, 40, 40), size_pt=12), "pre": FontFace(family="malgun", color=(60, 60, 60), size_pt=9)},
               table_line_separators=True)
pdf.output(dst)
print(dst)
