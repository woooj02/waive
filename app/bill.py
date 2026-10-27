"""Read a hospital bill with Nemotron 3.5 Super VL, then check its arithmetic in code.

The model is told not to return names or account numbers: Waive doesn't need
them and never stores the image.
"""

from __future__ import annotations

import base64
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, Field

from . import llm


class LineItem(BaseModel):
    date: str = ""
    description: str = ""
    code: str = ""
    amount_usd: float = 0


class BillFields(BaseModel):
    hospital: str = Field("", description="Hospital or facility name exactly as printed")
    city: str = ""
    state: str = Field("", description="Two-letter state")
    provider_type: str = Field("hospital", description="hospital | physician_group | unknown")
    statement_date: str = Field("", description="Statement date, YYYY-MM-DD")
    service_dates: str = ""
    total_charges_usd: float | None = None
    insurance_paid_usd: float | None = None
    adjustments_usd: float | None = None
    patient_paid_usd: float | None = None
    balance_usd: float | None = Field(None, description="Amount the patient is asked to pay now")
    insured: bool | None = Field(None, description="True if the bill shows an insurance payment or plan")
    line_items: list[LineItem] = Field(default_factory=list)


PROMPT = """This is a medical bill. Extract the fields in the JSON schema below. Copy names and numbers exactly as
printed. Dates as YYYY-MM-DD. Money as plain numbers without $ or commas. If a field isn't on the bill, use null.
Do NOT include the patient's name, address, date of birth or account number.
Schema: {schema}"""


def to_images(data: bytes, filename: str) -> list[bytes]:
    """PDF → PNG pages (first two); images pass through."""
    if not (filename.lower().endswith(".pdf") or data[:5] == b"%PDF-"):
        return [data]
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "bill.pdf"
        src.write_bytes(data)
        subprocess.run(["pdftoppm", "-png", "-r", "150", "-f", "1", "-l", "2", str(src), str(Path(tmp) / "p")],
                       check=True, capture_output=True, timeout=60)
        return [p.read_bytes() for p in sorted(Path(tmp).glob("p*.png"))]


def _mime(img: bytes) -> str:
    if img[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if img[:4] == b"RIFF":
        return "image/webp"
    return "image/png"


def read_bill(data: bytes, filename: str, meter: llm.Meter) -> BillFields:
    images = to_images(data, filename)
    content: list[dict] = [{"type": "text", "text": PROMPT.format(schema=BillFields.model_json_schema())}]
    for img in images:
        b64 = base64.b64encode(img).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:{_mime(img)};base64,{b64}"}})
    out = llm.chat_json("read bill (Nemotron 3.5 Super VL)", "vl", [{"role": "user", "content": content}], meter,
                        schema=BillFields.model_json_schema(), max_tokens=3000)
    return BillFields.model_validate(out)


def check(b: BillFields) -> list[str]:
    """Arithmetic and sanity checks a person would do with a calculator."""
    notes = []
    amounts = [li.amount_usd for li in b.line_items if li.amount_usd]
    if amounts and b.total_charges_usd is not None and abs(sum(amounts) - b.total_charges_usd) > 1:
        notes.append(f"Line items add up to ${sum(amounts):,.2f}, but the bill says total charges are "
                     f"${b.total_charges_usd:,.2f}. Ask for an itemised bill.")
    if b.total_charges_usd is not None and b.balance_usd is not None:
        paid = sum(x or 0 for x in (b.insurance_paid_usd, b.adjustments_usd, b.patient_paid_usd))
        expect = b.total_charges_usd - paid
        if abs(expect - b.balance_usd) > 1:
            notes.append(f"Charges minus payments and adjustments is ${expect:,.2f}, not the ${b.balance_usd:,.2f} "
                         f"balance shown.")
    dupes = [k for k, n in Counter((li.date, li.code or li.description, li.amount_usd) for li in b.line_items
                                   if li.amount_usd).items() if n > 1]
    for d in dupes:
        notes.append(f"Possible duplicate charge: {d[1]} on {d[0]} for ${d[2]:,.2f} appears more than once.")
    return notes
