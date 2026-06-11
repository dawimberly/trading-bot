"""Build consolidated court-ready evidence PDF for small claims case."""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

BASE = Path(r"c:\Users\Owner\OneDrive\Desktop\JT POS")
CASE = BASE / "Dan Wimberly v. Jamael Thompson-20250416T054844Z-001" / "Dan Wimberly v. Jamael Thompson"
OUTPUT = Path(r"c:\Users\Owner\PythonTrading\small claims\Evidence_Package.pdf")
COPY_OUTPUT = BASE / "Evidence_Package_Court_Ready.pdf"

PAGE_W, PAGE_H = letter
MARGIN = 0.75 * inch
CONTENT_WIDTH = PAGE_W - 2 * MARGIN


@dataclass(frozen=True)
class Exhibit:
    letter: str
    title: str
    description: str
    source: Path

    @property
    def divider_title(self) -> str:
        return f"EXHIBIT {self.letter} — {self.title}"


EXHIBITS: list[Exhibit] = [
    Exhibit(
        "A",
        "Body Repair Paid-in-Full Receipt",
        "Smokey's Auto & Body invoice INV005 dated May 14, 2024 — $6,386.75 paid in full, balance $0.00.",
        CASE / "Finances" / "Car repair paid in Full reciept.png",
    ),
    Exhibit(
        "B",
        "Anne's $5,000 Bank Withdrawal Receipt",
        "Chase Bank savings withdrawal receipt dated May 8, 2024 for $5,000.00 cash.",
        CASE / "Finances" / "Anne Bank Withdrawal Reciept.jpg",
    ),
    Exhibit(
        "C",
        "Initial Collision Admission",
        'Text message from JT: "Damn I hit your car bro" and "I\'m take care of the damage" — March 9, 2024.',
        CASE / "JT texts" / "1.jpg",
    ),
    Exhibit(
        "D",
        "Vehicle Damage at Scene",
        "Photograph of rear-quarter panel damage to 2010 Chevy Camaro (TX plate RYF-2374).",
        CASE / "Photos" / "1.jpeg",
    ),
    Exhibit(
        "E",
        "No Insurance Admission",
        'Text message from JT: "No coverage since March 3rd." — March 9, 2024.',
        CASE / "JT texts" / "5.jpg",
    ),
    Exhibit(
        "F",
        "Admission and Promise to Pay",
        'Text message from JT: "Yes I hit your car and I\'m sorry and I\'m gonna handle it all."',
        CASE / "JT texts" / "93.jpg",
    ),
    Exhibit(
        "G",
        "Agreement to Reimburse Rental and Wages",
        'Text message from JT: "Yes receipts and reimbursement cool" — May 5, 2024.',
        BASE / "Anne JT Text" / "1.jpg",
    ),
    Exhibit(
        "H",
        "$5,000 Settlement Offer from Body Shop",
        'Orlando (body shop) proposes $5,000 settlement; balance $5,755 and counting — cash only.',
        CASE / "JT texts" / "290.jpg",
    ),
    Exhibit(
        "I",
        "Agreement to Sign $6,000 Payment Plan",
        'Text message from JT: "I\'ll sign the $6000 even if he charges taxes" — May 7, 2024.',
        CASE / "JT texts" / "281.jpg",
    ),
    Exhibit(
        "J",
        "Refusal to Complete $6,000 Deal",
        'Text message from JT: "I\'ll never do a $6000 deal with no receipt" and "Need receipt."',
        CASE / "JT texts" / "235.jpg",
    ),
    Exhibit(
        "K",
        "Acknowledgment of Lost Wages",
        'JT acknowledges plaintiff lost wages and states he is "trying to right my wrong" after two months without work.',
        CASE / "JT texts" / "163.jpg",
    ),
    Exhibit(
        "L",
        "Body Shop Coordination — Payment Delay",
        'Text to Orlando body shop: "Jt doesn\'t have 500 until saturday" — March 14, 2024.',
        BASE / "Body Shop" / "Body Shop" / "1.jpg",
    ),
    Exhibit(
        "M",
        "Daily Wage Rate and Work Impact",
        'Plaintiff states $150/day ($750/week) income and inability to work because JT hit his car.',
        CASE / "JT texts" / "166.jpg",
    ),
    Exhibit(
        "N",
        "2023 W-2 Wage Statement (Plaintiff)",
        "Form W-2 from River City Roofing LLC — wages $10,333.40 (2023); supports lost-income claim.",
        CASE / "Finances" / "2023 W2 River City Roofing.pdf",
    ),
]


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=22,
            leading=28,
            alignment=TA_CENTER,
            spaceAfter=18,
        ),
        "subtitle": ParagraphStyle(
            "subtitle",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=12,
            leading=16,
            alignment=TA_CENTER,
            spaceAfter=8,
        ),
        "heading": ParagraphStyle(
            "heading",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=14,
            leading=18,
            spaceBefore=12,
            spaceAfter=8,
        ),
        "body": ParagraphStyle(
            "body",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=11,
            leading=14,
            alignment=TA_LEFT,
        ),
        "index_header": ParagraphStyle(
            "index_header",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=13,
            alignment=TA_LEFT,
            textColor=colors.white,
        ),
        "index_cell": ParagraphStyle(
            "index_cell",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=9,
            leading=12,
            alignment=TA_LEFT,
        ),
        "divider_label": ParagraphStyle(
            "divider_label",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=24,
            leading=30,
            alignment=TA_CENTER,
            spaceAfter=6,
        ),
        "divider_title": ParagraphStyle(
            "divider_title",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=16,
            leading=20,
            alignment=TA_CENTER,
            spaceAfter=16,
        ),
        "divider_desc": ParagraphStyle(
            "divider_desc",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=11,
            leading=15,
            alignment=TA_CENTER,
            spaceAfter=10,
            textColor=colors.HexColor("#333333"),
        ),
        "divider_source": ParagraphStyle(
            "divider_source",
            parent=base["Normal"],
            fontName="Helvetica-Oblique",
            fontSize=9,
            leading=12,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#666666"),
        ),
    }


def build_cover_and_index_pdf(exhibits: Iterable[Exhibit]) -> bytes:
    buf = io.BytesIO()
    styles = _styles()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
        title="Evidence Package — Wimberly v. Thompson",
    )
    story = []

    story.append(Spacer(1, 0.6 * inch))
    story.append(Paragraph("EVIDENCE PACKAGE", styles["title"]))
    story.append(Paragraph("Small Claims Court — Travis County, Texas", styles["subtitle"]))
    story.append(Paragraph("Dan Wimberly v. Jamael Thompson", styles["subtitle"]))
    story.append(Paragraph(f"Prepared: {date.today():%B %d, %Y}", styles["subtitle"]))
    story.append(Spacer(1, 0.35 * inch))

    story.append(Paragraph("Statement of Purpose", styles["heading"]))
    story.append(
        Paragraph(
            "This self-contained evidence package consolidates key exhibits supporting the plaintiff's "
            "claim for vehicle repair damages, out-of-pocket expenses, and lost wages arising from "
            "a March 9, 2024 collision. All exhibits are embedded below — no external links or "
            "references are required to view this evidence.",
            styles["body"],
        )
    )
    story.append(Spacer(1, 0.25 * inch))

    story.append(PageBreak())
    story.append(Paragraph("Table of Exhibits", styles["heading"]))

    col_exhibit = 0.65 * inch
    col_title = 2.1 * inch
    col_desc = CONTENT_WIDTH - col_exhibit - col_title
    header_style = styles["index_header"]
    cell_style = styles["index_cell"]
    rows = [
        [
            Paragraph("Exhibit", header_style),
            Paragraph("Title", header_style),
            Paragraph("Description", header_style),
        ]
    ]
    for ex in exhibits:
        rows.append(
            [
                Paragraph(_escape(ex.letter), cell_style),
                Paragraph(_escape(ex.title), cell_style),
                Paragraph(_escape(ex.description), cell_style),
            ]
        )

    table = Table(
        rows,
        colWidths=[col_exhibit, col_title, col_desc],
        repeatRows=1,
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a365d")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7fafc")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 0.35 * inch))
    story.append(
        Paragraph(
            "<b>Instructions:</b> Each exhibit begins with a divider page labeled EXHIBIT A, B, C, etc., "
            "followed by the embedded document or screenshot on the next page(s).",
            styles["body"],
        )
    )

    doc.build(story)
    return buf.getvalue()


def build_divider_pdf(exhibit: Exhibit) -> bytes:
    buf = io.BytesIO()
    styles = _styles()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
    )
    story = [
        Spacer(1, 1.75 * inch),
        Paragraph(f"EXHIBIT {_escape(exhibit.letter)}", styles["divider_label"]),
        Paragraph(_escape(exhibit.title), styles["divider_title"]),
        Paragraph(_escape(exhibit.description), styles["divider_desc"]),
        Spacer(1, 0.25 * inch),
        Paragraph(f"Source: {_escape(exhibit.source.name)}", styles["divider_source"]),
    ]
    doc.build(story)
    return buf.getvalue()


def image_to_pdf_bytes(path: Path) -> bytes:
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    iw, ih = img.size
    max_w = PAGE_W - 2 * MARGIN
    max_h = PAGE_H - 2 * MARGIN
    scale = min(max_w / iw, max_h / ih, 1.0)
    nw, nh = int(iw * scale), int(ih * scale)

    if scale < 1.0:
        img = img.resize((nw, nh), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=150.0)
    return buf.getvalue()


def file_to_pdf_bytes(path: Path) -> bytes:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return path.read_bytes()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}:
        return image_to_pdf_bytes(path)
    raise ValueError(f"Unsupported exhibit format: {path}")


def merge_pdfs(parts: list[bytes]) -> bytes:
    writer = PdfWriter()
    for part in parts:
        reader = PdfReader(io.BytesIO(part))
        for page in reader.pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def main() -> None:
    missing = [ex for ex in EXHIBITS if not ex.source.exists()]
    if missing:
        lines = "\n".join(f"  {ex.letter}: {ex.source}" for ex in missing)
        raise FileNotFoundError(f"Missing exhibit source files:\n{lines}")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    parts: list[bytes] = [build_cover_and_index_pdf(EXHIBITS)]

    for ex in EXHIBITS:
        parts.append(build_divider_pdf(ex))
        parts.append(file_to_pdf_bytes(ex.source))

    final_pdf = merge_pdfs(parts)
    OUTPUT.write_bytes(final_pdf)
    COPY_OUTPUT.write_bytes(final_pdf)

    page_count = len(PdfReader(io.BytesIO(final_pdf)).pages)
    size_mb = OUTPUT.stat().st_size / (1024 * 1024)
    print(f"Created: {OUTPUT}")
    print(f"Copied: {COPY_OUTPUT}")
    print(f"Pages: {page_count}")
    print(f"Size: {size_mb:.2f} MB")
    print(f"Exhibits: {len(EXHIBITS)}")


if __name__ == "__main__":
    main()
