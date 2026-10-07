"""Render the maintained acceptance guide (requires optional reportlab)."""

import re
import textwrap
from html import escape
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
    XPreformatted,
)


def main():
    root = Path(__file__).resolve().parents[1]
    output = root / "output/pdf/QuotaMesh_Final_Testing_Guide_v0.1.0.pdf"
    output.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    styles["Normal"].fontSize = 9
    styles["Normal"].leading = 13
    styles["Normal"].spaceAfter = 7
    styles["Title"].fontSize = 25
    styles["Title"].leading = 29
    styles["Title"].alignment = TA_LEFT
    styles["Title"].textColor = colors.HexColor("#15293b")
    styles["Heading2"].fontSize = 15
    styles["Heading2"].leading = 19
    styles["Heading2"].spaceBefore = 14
    styles["Heading2"].textColor = colors.HexColor("#15293b")
    styles.add(
        ParagraphStyle(
            "CodeBlock",
            fontName="Courier",
            fontSize=7,
            leading=10,
            backColor=colors.HexColor("#eef2f4"),
            borderPadding=8,
            spaceBefore=5,
            spaceAfter=10,
        )
    )
    styles.add(
        ParagraphStyle("Cell", parent=styles["Normal"], fontSize=8, leading=11, spaceAfter=0)
    )

    def markup(value):
        value = escape(value)
        value = re.sub(r"`([^`]+)`", r'<font name="Courier">\1</font>', value)
        value = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", value)
        return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', value)

    lines = (root / "docs/final-testing-guide.md").read_text(encoding="utf-8").splitlines()
    story = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith("```"):
            block = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                block.append(lines[index])
                index += 1
            story.append(
                XPreformatted(
                    escape(
                        "\n".join(
                            segment
                            for line in block
                            for segment in (
                                textwrap.wrap(line, width=100, subsequent_indent="  ") or [""]
                            )
                        )
                    ),
                    styles["CodeBlock"],
                )
            )
            index += 1
            continue
        if line.startswith("|"):
            rows = []
            while index < len(lines) and lines[index].startswith("|"):
                cells = [c.strip() for c in lines[index].strip("|").split("|")]
                if not all(re.fullmatch(r"[: -]+", c) for c in cells):
                    rows.append([Paragraph(markup(c), styles["Cell"]) for c in cells])
                index += 1
            width = A4[0] - 38 * mm
            table = Table(
                rows, colWidths=[width / len(rows[0])] * len(rows[0]), repeatRows=1, hAlign="LEFT"
            )
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e3ebf0")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        (
                            "ROWBACKGROUNDS",
                            (0, 1),
                            (-1, -1),
                            [colors.white, colors.HexColor("#f7f9fa")],
                        ),
                        ("LINEBELOW", (0, 0), (-1, 0), 0.7, colors.HexColor("#90a2af")),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                        ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ]
                )
            )
            story.extend([table, Spacer(1, 8)])
            continue
        if line.startswith("# "):
            story.append(
                KeepTogether(
                    [
                        Paragraph(markup(line[2:]), styles["Title"]),
                        Paragraph("FINAL MVP / ACCEPTANCE & OPERATIONS", styles["Normal"]),
                        Spacer(1, 8),
                    ]
                )
            )
        elif line.startswith("## "):
            story.append(Paragraph(markup(line[3:]), styles["Heading2"]))
        elif line.startswith("### "):
            story.append(Paragraph(markup(line[4:]), styles["Heading3"]))
        elif re.match(r"^(?:- |\d+\. )", line):
            content = [line]
            index += 1
            while (
                index < len(lines)
                and lines[index].strip()
                and not re.match(r"^(?:#|\||```|- |\d+\. )", lines[index])
            ):
                content.append(lines[index].strip())
                index += 1
            story.append(Paragraph(markup(" ".join(content)), styles["Normal"]))
            continue
        else:
            content = [line]
            index += 1
            while (
                index < len(lines)
                and lines[index].strip()
                and not re.match(r"^(?:#|\||```|- |\d+\. )", lines[index])
            ):
                content.append(lines[index])
                index += 1
            story.append(Paragraph(markup(" ".join(content)), styles["Normal"]))
            continue
        index += 1

    def footer(canvas, document):
        canvas.setStrokeColor(colors.HexColor("#b8c5ce"))
        canvas.line(19 * mm, 17 * mm, A4[0] - 19 * mm, 17 * mm)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#546572"))
        canvas.drawString(
            19 * mm, 12 * mm, "QuotaMesh 0.1.0 | Synthetic tests use no real provider quota"
        )
        canvas.drawRightString(A4[0] - 19 * mm, 12 * mm, str(document.page))

    SimpleDocTemplate(
        str(output),
        pagesize=A4,
        leftMargin=19 * mm,
        rightMargin=19 * mm,
        topMargin=19 * mm,
        bottomMargin=23 * mm,
        title="QuotaMesh 0.1.0 Final Testing Guide",
        author="QuotaMesh",
    ).build(story, onFirstPage=footer, onLaterPages=footer)
    print(output)


if __name__ == "__main__":
    main()
