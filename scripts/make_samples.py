"""Make the fictional sample bills used by the demo and tests.

Patients, account numbers and charges are invented. Each page is stamped
SAMPLE so no one mistakes it for a real bill. Hospital names are real because
Waive looks up the real hospital's public policy.

    ./venv/bin/python scripts/make_samples.py
"""

from __future__ import annotations

import io
import json
import random
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter
from reportlab.lib.colors import Color, HexColor
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent.parent / "samples"

SAMPLES = [
    {
        "id": "er-visit-uchealth",
        "title": "ER visit, uninsured",
        "hospital": "UCHealth University of Colorado Hospital",
        "address": "12605 E 16th Ave, Aurora, CO 80045",
        "patient": "Jordan Sample",
        "statement_date": "2026-10-15",
        "service_dates": "2026-09-28",
        "insurance": None,
        "items": [
            ("2026-09-28", "0450", "Emergency room visit, level 4", 3_950.00),
            ("2026-09-28", "0350", "CT scan, head without contrast", 2_760.00),
            ("2026-09-28", "0300", "Laboratory, comprehensive metabolic panel", 412.00),
            ("2026-09-28", "0300", "Laboratory, complete blood count", 245.00),
            ("2026-09-28", "0300", "Laboratory, complete blood count", 245.00),
            ("2026-09-28", "0250", "Pharmacy", 800.00),
        ],
        "household": {"size": 4, "income_usd": 70_000, "state": "CO", "insured": False},
        "story": "Family of four, $70,000 a year, no insurance. An ER visit after a fall.",
    },
    {
        "id": "surgery-saint-joseph",
        "title": "Outpatient surgery, insured",
        "hospital": "Intermountain Health Saint Joseph Hospital",
        "address": "1375 E 19th Ave, Denver, CO 80218",
        "patient": "Riley Example",
        "statement_date": "2026-09-30",
        "service_dates": "2026-08-19",
        "insurance": 9_310.00,
        "items": [
            ("2026-08-19", "0360", "Operating room services", 7_480.00),
            ("2026-08-19", "0370", "Anesthesia", 2_150.00),
            ("2026-08-19", "0710", "Recovery room", 1_890.00),
            ("2026-08-19", "0250", "Pharmacy", 1_730.00),
        ],
        "household": {"size": 2, "income_usd": 80_000, "state": "CO", "insured": True},
        "story": "Couple earning $80,000 with a high-deductible plan; $3,940 left after insurance.",
    },
    {
        "id": "imaging-not-eligible",
        "title": "Imaging, high income",
        "hospital": "Children's Hospital Colorado",
        "address": "13123 E 16th Ave, Aurora, CO 80045",
        "patient": "Casey Placeholder",
        "statement_date": "2026-10-05",
        "service_dates": "2026-09-12",
        "insurance": None,
        "items": [
            ("2026-09-12", "0610", "MRI, knee without contrast", 2_150.00),
        ],
        "household": {"size": 3, "income_usd": 190_000, "state": "CO", "insured": False},
        "story": "Family of three earning $190,000: the honest 'you probably don't qualify' case.",
    },
]

STAMP = Color(0.86, 0.15, 0.15, alpha=0.18)


def draw_bill(s: dict) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    w, h = letter
    c.setFillColor(HexColor("#1f2937"))
    c.setFont("Helvetica-Bold", 15)
    c.drawString(50, h - 60, s["hospital"])
    c.setFont("Helvetica", 9.5)
    c.drawString(50, h - 76, s["address"])
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(w - 50, h - 60, "PATIENT STATEMENT")
    c.setFont("Helvetica", 9.5)
    rows = [("Statement date", s["statement_date"]), ("Dates of service", s["service_dates"]),
            ("Patient", s["patient"]), ("Account number", "SAMPLE-000-0000")]
    for i, (k, v) in enumerate(rows):
        c.drawRightString(w - 160, h - 80 - 14 * i, k + ":")
        c.drawString(w - 150, h - 80 - 14 * i, v)

    y = h - 170
    c.setFillColor(HexColor("#e5e7eb"))
    c.rect(50, y - 4, w - 100, 18, stroke=0, fill=1)
    c.setFillColor(HexColor("#111827"))
    c.setFont("Helvetica-Bold", 9.5)
    for x, label in ((56, "Date"), (130, "Rev code"), (190, "Description")):
        c.drawString(x, y, label)
    c.drawRightString(w - 56, y, "Charge")
    c.setFont("Helvetica", 9.5)
    total = 0.0
    for d, code, desc, amt in s["items"]:
        y -= 18
        c.drawString(56, y, d)
        c.drawString(130, y, code)
        c.drawString(190, y, desc)
        c.drawRightString(w - 56, y, f"{amt:,.2f}")
        total += amt
    ins = s["insurance"] or 0.0
    balance = total - ins
    y -= 30
    c.line(w - 260, y + 14, w - 50, y + 14)
    for label, val, bold in (("Total charges", total, False), ("Insurance payments", -ins if ins else 0.0, False),
                             ("Adjustments", 0.0, False), ("Patient payments", 0.0, False),
                             ("AMOUNT DUE", balance, True)):
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 11 if bold else 9.5)
        c.drawRightString(w - 160, y, label)
        c.drawRightString(w - 56, y, f"${val:,.2f}")
        y -= 16
    c.setFont("Helvetica", 8.5)
    c.setFillColor(HexColor("#4b5563"))
    c.drawString(50, 120, "Questions about your bill? Call Patient Financial Services. Financial assistance may be "
                          "available; ask us how to apply.")
    c.drawString(50, 106, "Please pay by the due date shown to avoid late notices.")
    # The stamp: unmissable, on every page.
    c.saveState()
    c.setFillColor(STAMP)
    c.setFont("Helvetica-Bold", 84)
    c.translate(w / 2, h / 2 - 40)
    c.rotate(28)
    c.drawCentredString(0, 0, "SAMPLE")
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(0, -34, "Fictional patient. Not a real bill.")
    c.restoreState()
    c.showPage()
    c.save()
    s["total"], s["balance"] = round(total, 2), round(balance, 2)
    return buf.getvalue()


def photo(png: bytes, seed: int) -> bytes:
    """Make a clean render look like a phone photo: tilt, uneven light, a little blur."""
    rnd = random.Random(seed)
    page = Image.open(io.BytesIO(png)).convert("RGB")
    bg = Image.new("RGB", (int(page.width * 1.18), int(page.height * 1.12)), (92, 84, 74))
    page = page.rotate(rnd.uniform(-4, 4), expand=True, fillcolor=(92, 84, 74), resample=Image.BICUBIC)
    bg.paste(page, ((bg.width - page.width) // 2, (bg.height - page.height) // 2))
    shade = Image.new("L", bg.size, 0)
    ImageDraw.Draw(shade).ellipse((-bg.width // 3, -bg.height // 4, bg.width, bg.height), fill=60)
    bg = Image.composite(Image.new("RGB", bg.size, (0, 0, 0)), bg, shade.filter(ImageFilter.GaussianBlur(120)))
    bg = bg.filter(ImageFilter.GaussianBlur(0.8)).resize((bg.width * 2 // 3, bg.height * 2 // 3))
    out = io.BytesIO()
    bg.save(out, "JPEG", quality=80)
    return out.getvalue()


def main() -> None:
    OUT.mkdir(exist_ok=True)
    index = []
    for i, s in enumerate(SAMPLES):
        pdf = draw_bill(s)
        (OUT / f"{s['id']}.pdf").write_bytes(pdf)
        png_path = OUT / f"{s['id']}.png"
        subprocess.run(["pdftoppm", "-png", "-r", "120", "-singlefile", str(OUT / f"{s['id']}.pdf"),
                        str(png_path.with_suffix(""))], check=True)
        (OUT / f"{s['id']}-photo.jpg").write_bytes(photo(png_path.read_bytes(), i))
        index.append({k: s[k] for k in ("id", "title", "hospital", "statement_date", "household", "story",
                                        "total", "balance")} | {"files": [f"{s['id']}-photo.jpg", f"{s['id']}.pdf"]})
    (OUT / "index.json").write_text(json.dumps(index, indent=1))
    print(json.dumps([(x["id"], x["balance"]) for x in index]))


if __name__ == "__main__":
    main()
