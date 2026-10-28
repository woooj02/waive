"""The application packet: what the patient sends, as a PDF.

The letter is a fixed template (legal requests shouldn't be paraphrased by a
model). Nemotron 3.5 Lightning only writes the short Spanish summary, and the
packet is complete without it.
"""

from __future__ import annotations

import io
from datetime import date
from xml.sax.saxutils import escape

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import llm

DEFAULT_DOCS = ["Proof of income for the last 3 months (pay stubs, benefit letters, or a self-employment ledger)",
                "Your most recent federal tax return, if you filed one",
                "A photo ID", "A copy of this bill",
                "Any letter showing enrollment in Medicaid, SNAP, WIC or CICP"]

SPANISH = """Write 3 short sentences in plain Spanish (8th-grade level) for a patient, summarising this result.
Do not add facts. Result: {headline} Deadline: {deadline}. The hospital: {hospital}."""


def _p(text: str, style) -> Paragraph:
    return Paragraph(escape(text).replace("\n", "<br/>"), style)


def money(x) -> str:
    return "—" if x is None else f"${x:,.2f}"


def build(result: dict) -> bytes:
    v, hh, bill = result["verdict"], result["household"], result.get("bill") or {}
    prog, src = result.get("program") or {}, result.get("source") or {}
    hospital = bill.get("hospital") or prog.get("hospital") or "the hospital"
    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontName="Helvetica", fontSize=10.5, leading=15)
    small = ParagraphStyle("s", parent=body, fontSize=9, leading=12, textColor=HexColor("#555555"))
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontName="Times-Bold", fontSize=20, spaceAfter=8)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Times-Bold", fontSize=14, spaceBefore=10)
    quote = ParagraphStyle("q", parent=body, fontName="Times-Italic", backColor=HexColor("#fff7c2"), borderPadding=6,
                           leftIndent=6, rightIndent=6, spaceBefore=6, spaceAfter=10)
    story = []

    # Page 1: summary for the patient.
    story += [_p("Your Waive packet", h1), _p(v["headline"], ParagraphStyle("v", parent=h2, textColor=HexColor("#0f7b55"))),
              _p(f"Hospital: {hospital}\nBalance on the bill: {money(v['balance_usd'])}\n"
                 f"Your household: {hh['size']} people, {money(hh['income_usd'])} a year = {v['pct_fpl']}% of the "
                 f"2026 federal poverty level", body)]
    if v.get("deadline"):
        story.append(_p(f"Send your application before {v['deadline']} (240 days after the statement date).", body))
    main = next((r for r in v.get("reasons", []) if r.get("quote")), None)
    if main:
        story += [_p("What the hospital's own policy says:", h2), _p(f"“{main['quote']}”", quote),
                  _p(f"Source: {src.get('url', '')} (fetched {src.get('fetched', '')})", small)]
    story += [_p("What to do", h2), _p(
        "1. Fill in the blanks on the letter (your name, address, account number) and sign it.\n"
        "2. Gather the documents on the checklist.\n"
        "3. Send the letter, the hospital's application form (ask for it, or download it from its website) and the "
        "documents to the financial assistance office. Keep copies and note the date you sent them.\n"
        "4. If you get a collection call, say you have applied for financial assistance and ask them to pause "
        "collections until you get a written decision.", body)]
    if llm.configured():
        try:
            es = llm.chat("spanish summary (Nemotron 3.5 Lightning)", "lightning", [{"role": "user", "content":
                          SPANISH.format(headline=v["headline"], deadline=v.get("deadline") or "—", hospital=hospital)}],
                          max_tokens=300, private=True)
            story += [_p("Resumen en español", h2), _p(es.strip(), body),
                      _p("Traducción automática (Nemotron 3.5 Lightning). Pida al hospital ayuda en español: "
                         "están obligados a ofrecerla.", small)]
        except (llm.LLMError, llm.BudgetExceeded):
            pass
    story += [Spacer(1, 10), _p("Waive is not a law firm and this is not legal advice. Waive never contacts the "
                                "hospital for you.", small), PageBreak()]

    # Page 2: the letter.
    today = date.today().strftime("%B %d, %Y")
    co = hh.get("state") == "CO"
    requests = [
        "Please evaluate me for financial assistance under your Financial Assistance Policy and send me your "
        "written determination.",
        "Please send me an itemized bill listing each charge with its date and billing code.",
        "While my application is pending, please suspend any collection activity on this account, including "
        "reporting to credit bureaus or referral to a collection agency, as required for nonprofit hospitals by "
        "26 CFR 1.501(r)-6.",
        "If I am found eligible, please refund any amount I have paid above what I owe under the policy.",
    ]
    if co:
        requests.insert(1, "Please also screen me for Colorado Hospital Discounted Care (C.R.S. 25.5-3-501 et seq.) "
                           "and for public coverage such as Health First Colorado.")
    story += [_p(today, body), Spacer(1, 6),
              _p(f"Financial Assistance Office\n{hospital}", body), Spacer(1, 10),
              _p("Re: Request for financial assistance\nPatient name: ____________________________   "
                 "Account number: ____________________\nDate(s) of service: "
                 f"{bill.get('service_dates') or '______________'}   Balance: {money(v['balance_usd'])}", body),
              Spacer(1, 10), _p("To the Financial Assistance Office,", body),
              _p(f"I am writing to apply for financial assistance for the balance above. My household has "
                 f"{hh['size']} people and a yearly income of {money(hh['income_usd'])}, which is {v['pct_fpl']}% of "
                 f"the 2026 federal poverty guidelines.", body)]
    if main:
        story.append(_p(f"Your policy states: “{main['quote']}”", body))
    story += [_p(f"{i}. {r}", body) for i, r in enumerate(requests, 1)]
    story += [_p("I have enclosed the documents listed below. Please contact me if you need anything else.", body),
              Spacer(1, 14), _p("Sincerely,\n\n______________________________\nName, address, phone", body), PageBreak()]

    # Page 3: answers and checklist.
    docs = (prog.get("application") or {}).get("documents") or DEFAULT_DOCS
    rows = [["Question on most applications", "Your answer"],
            ["People in your household", str(hh["size"])],
            ["Yearly household income before taxes", money(hh["income_usd"])],
            ["Monthly household income", money(hh["income_usd"] / 12)],
            ["Health insurance", "Yes" if hh.get("insured") else "No"],
            ["Enrolled in Medicaid, SNAP, WIC or CICP", ", ".join(hh.get("programs") or []) or "No"],
            ["Savings and assets (not your home)", money(hh.get("assets_usd")) if hh.get("assets_usd") is not None else "______"]]
    t = Table(rows, colWidths=[3.3 * inch, 3.2 * inch])
    t.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "Helvetica", 10), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 10),
                           ("BACKGROUND", (0, 0), (-1, 0), HexColor("#eeeeee")),
                           ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#cccccc")), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [_p("Answers for the application form", h1), t, _p("Documents checklist", h2)]
    story += [_p(f"[  ]  {d}", body) for d in docs]
    story += [_p("What to say on the phone", h2), _p(
        "“I'd like to apply for financial assistance for my account. Please send me your application and hold my "
        "account from collections while I apply." + (" I'd also like to be screened for Hospital Discounted Care."
                                                       if co else "") + "”", quote)]

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=letter, title="Waive packet", leftMargin=0.9 * inch, rightMargin=0.9 * inch,
                      topMargin=0.8 * inch, bottomMargin=0.8 * inch).build(story)
    return buf.getvalue()
