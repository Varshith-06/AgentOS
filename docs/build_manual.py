"""Builds AgentOS.pdf — the project's reference manual.

    pip install reportlab
    python docs/build_manual.py

The manual is written for someone who knows roughly what a process and a
scheduler are, and nothing at all about this project. It explains every
component, why it exists, how it behaves at the edges, and what it does not
do. Regenerate it whenever the system changes; the content lives in
docs/manual.py so this file stays about layout.
"""

from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, KeepTogether, PageBreak, PageTemplate, Paragraph,
    Preformatted, Spacer, Table, TableStyle,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from manual import CONTENT, SUBTITLE, TITLE  # noqa: E402

INK = colors.HexColor("#1a1a1a")
MUTED = colors.HexColor("#5a5a5a")
RULE = colors.HexColor("#d4d4d4")
ACCENT = colors.HexColor("#8a4b08")
CODE_BG = colors.HexColor("#f6f6f4")
NOTE_BG = colors.HexColor("#f4f1ea")


def styles() -> dict:
    base = getSampleStyleSheet()
    body = ParagraphStyle(
        "body", parent=base["BodyText"], fontName="Times-Roman", fontSize=10.2,
        leading=15.2, alignment=TA_JUSTIFY, textColor=INK, spaceAfter=7,
    )
    return {
        "title": ParagraphStyle(
            "title", parent=body, fontName="Helvetica-Bold", fontSize=27,
            leading=32, alignment=0, spaceAfter=6, textColor=INK),
        "subtitle": ParagraphStyle(
            "subtitle", parent=body, fontName="Helvetica", fontSize=12.5,
            leading=17, textColor=MUTED, spaceAfter=20, alignment=0),
        "h1": ParagraphStyle(
            "h1", parent=body, fontName="Helvetica-Bold", fontSize=17,
            leading=21, spaceBefore=4, spaceAfter=9, textColor=INK,
            alignment=0),
        "h2": ParagraphStyle(
            "h2", parent=body, fontName="Helvetica-Bold", fontSize=12.6,
            leading=16, spaceBefore=15, spaceAfter=6, textColor=INK,
            alignment=0),
        "h3": ParagraphStyle(
            "h3", parent=body, fontName="Helvetica-BoldOblique", fontSize=10.6,
            leading=14, spaceBefore=11, spaceAfter=4, textColor=ACCENT,
            alignment=0),
        "body": body,
        "bullet": ParagraphStyle(
            "bullet", parent=body, leftIndent=13, bulletIndent=3, spaceAfter=4),
        "code": ParagraphStyle(
            "code", parent=base["Code"], fontName="Courier", fontSize=8.0,
            leading=10.6, textColor=INK, spaceAfter=0, spaceBefore=0),
        "note": ParagraphStyle(
            "note", parent=body, fontSize=9.6, leading=14, spaceAfter=0,
            spaceBefore=0),
        "cell": ParagraphStyle(
            "cell", parent=body, fontSize=8.9, leading=12, alignment=0,
            spaceAfter=0),
        "cellhead": ParagraphStyle(
            "cellhead", parent=body, fontName="Helvetica-Bold", fontSize=8.9,
            leading=12, alignment=0, spaceAfter=0),
        "toc": ParagraphStyle(
            "toc", parent=body, fontSize=10, leading=15, spaceAfter=1,
            alignment=0),
        "tocsub": ParagraphStyle(
            "tocsub", parent=body, fontSize=9.4, leading=13.4, leftIndent=15,
            textColor=MUTED, spaceAfter=0, alignment=0),
    }


class Manual(BaseDocTemplate):
    """Adds the running footer and remembers where each section landed."""

    def __init__(self, path: str, **kw):
        super().__init__(path, pagesize=A4, **kw)
        frame = Frame(20 * mm, 18 * mm, A4[0] - 40 * mm, A4[1] - 36 * mm,
                      id="body", showBoundary=0)
        self.addPageTemplates([
            PageTemplate(id="cover", frames=[frame]),
            PageTemplate(id="main", frames=[frame], onPage=self._decorate),
        ])
        self.section = ""
        self.toc_pages: dict[str, int] = {}

    def _decorate(self, canvas, doc) -> None:
        canvas.saveState()
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.4)
        canvas.line(20 * mm, 14 * mm, A4[0] - 20 * mm, 14 * mm)
        canvas.setFont("Helvetica", 7.6)
        canvas.setFillColor(MUTED)
        canvas.drawString(20 * mm, 9.5 * mm, self.section[:78])
        canvas.drawRightString(A4[0] - 20 * mm, 9.5 * mm, str(doc.page))
        canvas.restoreState()

    def afterFlowable(self, flowable) -> None:
        """Record the page a chapter starts on, for the contents page."""
        if getattr(flowable, "_chapter", None):
            self.section = flowable._chapter
            self.toc_pages.setdefault(flowable._chapter, self.page)


def build(blocks, out: str, toc_pages: dict | None = None) -> dict:
    st = styles()
    story = []
    width = A4[0] - 40 * mm

    def code_block(text: str) -> Table:
        lines = text.strip("\n").rstrip()
        t = Table([[Preformatted(lines, st["code"])]], colWidths=[width])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), CODE_BG),
            ("BOX", (0, 0), (-1, -1), 0.4, RULE),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        return t

    for kind, payload in blocks:
        if kind == "cover":
            story.append(Spacer(1, 55 * mm))
            story.append(Paragraph(TITLE, st["title"]))
            story.append(Paragraph(SUBTITLE, st["subtitle"]))
            for line in payload:
                story.append(Paragraph(line, st["body"]))
            story.append(PageBreak())
        elif kind == "toc":
            story.append(Paragraph("Contents", st["h1"]))
            for entry in payload:
                if isinstance(entry, tuple):
                    name, subs = entry
                    page = (toc_pages or {}).get(name)
                    dots = f"&nbsp;&nbsp;<font color='#9a9a9a'>{'.' * 3}</font>&nbsp;"
                    num = f"{dots}{page}" if page else ""
                    story.append(Paragraph(
                        f"<b>{name}</b>{num}", st["toc"]))
                    for sub in subs:
                        story.append(Paragraph(sub, st["tocsub"]))
                else:
                    story.append(Paragraph(entry, st["toc"]))
            story.append(PageBreak())
        elif kind == "h1":
            para = Paragraph(payload, st["h1"])
            para._chapter = payload
            story.append(PageBreak())
            story.append(para)
        elif kind in ("h2", "h3", "body"):
            story.append(Paragraph(payload, st[kind]))
        elif kind == "bullets":
            for item in payload:
                story.append(Paragraph(item, st["bullet"], bulletText="•"))
            story.append(Spacer(1, 4))
        elif kind == "code":
            story.append(Spacer(1, 2))
            story.append(code_block(payload))
            story.append(Spacer(1, 8))
        elif kind == "note":
            inner = Paragraph(payload, st["note"])
            t = Table([[inner]], colWidths=[width])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), NOTE_BG),
                ("LINEBEFORE", (0, 0), (0, -1), 2.2, ACCENT),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]))
            story.append(Spacer(1, 3))
            story.append(t)
            story.append(Spacer(1, 9))
        elif kind == "table":
            header, rows, widths = payload
            data = [[Paragraph(c, st["cellhead"]) for c in header]]
            data += [[Paragraph(c, st["cell"]) for c in row] for row in rows]
            cols = [width * w for w in widths]
            t = Table(data, colWidths=cols, repeatRows=1)
            t.setStyle(TableStyle([
                ("LINEBELOW", (0, 0), (-1, 0), 0.7, INK),
                ("LINEBELOW", (0, 1), (-1, -2), 0.25, RULE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.white, colors.HexColor("#faf9f7")]),
            ]))
            story.append(Spacer(1, 3))
            story.append(KeepTogether(t) if len(rows) <= 6 else t)
            story.append(Spacer(1, 10))
        elif kind == "space":
            story.append(Spacer(1, payload))

    doc = Manual(out)
    doc.build(story)
    return doc.toc_pages


if __name__ == "__main__":
    out = str(Path(__file__).resolve().parents[1] / "AgentOS.pdf")
    # Two passes: the first discovers what page each chapter starts on, the
    # second writes those numbers into the contents page.
    pages = build(CONTENT, out)
    build(CONTENT, out, toc_pages=pages)
    size = Path(out).stat().st_size
    print(f"wrote {out} ({size / 1024:.0f} KB), {len(pages)} chapters")
