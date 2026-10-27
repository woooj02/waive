"""Eligibility in plain code. No model call here: every number is reproducible.

Inputs are a compiled RuleProgram (only rules whose quotes were verified count),
a Household and a Bill. Output is a Verdict with the reasons, each pointing at
the policy sentence or the law it came from.
"""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel, Field

from .schema import Bill, Household, RuleProgram

# 2026 HHS poverty guidelines, effective Jan 13, 2026 (ASPE detailed-guidelines-2026.pdf).
FPL_YEAR = 2026
FPL_BASE = {"contiguous": 15_960, "AK": 19_950, "HI": 18_360}
FPL_STEP = {"contiguous": 5_680, "AK": 7_100, "HI": 6_530}
FPL_SOURCE = "HHS 2026 Poverty Guidelines (ASPE), effective Jan 13, 2026"

# Colorado Hospital Discounted Care (HB21-1198, SB24-116), per hcpf.colorado.gov.
CO_HDC_MAX_FPL = 250
CO_HDC_HOSPITAL_PCT = 4  # % of gross monthly household income, hospital bills
CO_HDC_PROFESSIONAL_PCT = 2  # per separately billing professional
CO_HDC_MAX_PAYMENTS = 36
CO_HDC_SOURCE = "Colorado Hospital Discounted Care, hcpf.colorado.gov/colorado-hospital-discounted-care"

# 26 CFR 1.501(r)-6: applications accepted for 240 days after the first post-discharge statement.
APPLICATION_WINDOW_DAYS = 240
R501_SOURCE = "26 CFR 1.501(r)-6 (nonprofit hospitals)"


def fpl(size: int, state: str = "CO") -> float:
    region = state if state in ("AK", "HI") else "contiguous"
    return FPL_BASE[region] + FPL_STEP[region] * (size - 1)


def pct_fpl(income: float, size: int, state: str = "CO") -> float:
    return round(100 * income / fpl(size, state), 1)


class Reason(BaseModel):
    text: str
    source: str = ""
    quote: str = ""


class Verdict(BaseModel):
    status: str = Field(description="free | discount | colorado_cap | needs_info | not_eligible | no_policy")
    headline: str
    balance_usd: float
    expected_usd: float | None = None
    discount_pct: float = 0
    pct_fpl: float
    fpl_usd: float
    deadline: str = ""
    reasons: list[Reason] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    uncertain: list[str] = Field(default_factory=list)
    colorado: dict | None = None


def _money(x: float) -> str:
    return f"${x:,.0f}"


def colorado_cap(household: Household, bill: Bill) -> dict | None:
    """Most a qualifying household can be made to pay on this bill under Colorado HDC."""
    p = pct_fpl(household.income_usd, household.size, household.state)
    if household.state != "CO" or p > CO_HDC_MAX_FPL:
        return None
    pct = CO_HDC_PROFESSIONAL_PCT if bill.provider_type == "physician_group" else CO_HDC_HOSPITAL_PCT
    monthly = household.income_usd / 12 * pct / 100
    total = min(bill.balance_usd, monthly * CO_HDC_MAX_PAYMENTS)
    return {
        "monthly_max_usd": round(monthly, 2),
        "total_max_usd": round(total, 2),
        "pct_of_income": pct,
        "payments": CO_HDC_MAX_PAYMENTS,
        "source": CO_HDC_SOURCE,
        "note": "Charges are also limited to state-set rates, which can only lower this.",
    }


def deadline(bill: Bill, program: RuleProgram | None) -> str:
    if not bill.statement_date:
        return ""
    try:
        start = date.fromisoformat(bill.statement_date)
    except ValueError:
        return ""
    days = APPLICATION_WINDOW_DAYS
    if program and program.application.verified and program.application.window_days:
        days = max(days, program.application.window_days)
    return (start + timedelta(days=days)).isoformat()


def evaluate(program: RuleProgram | None, household: Household, bill: Bill) -> Verdict:
    p = pct_fpl(household.income_usd, household.size, household.state)
    base = fpl(household.size, household.state)
    v = Verdict(status="needs_info", headline="", balance_usd=bill.balance_usd, pct_fpl=p, fpl_usd=base,
                deadline=deadline(bill, program), colorado=colorado_cap(household, bill))
    v.reasons.append(Reason(
        text=f"Household of {household.size} earning {_money(household.income_usd)} a year is at {p:g}% of the "
             f"{FPL_YEAR} poverty level ({_money(base)}).", source=FPL_SOURCE))

    if program is None:
        return _fallback(v, "no_policy", "We couldn't find this hospital's policy.")

    # Hard stops first: rules that exclude the patient no matter the income.
    if household.insured and program.insured.verified and program.insured.eligible is False:
        v.reasons.append(Reason(text="This policy only helps uninsured patients.", quote=program.insured.quote,
                                source=program.source_url))
        return _fallback(v, "not_eligible", "This hospital's policy doesn't cover insured patients.")

    if bill.provider_type == "physician_group" and program.providers.verified and program.providers.scope == "facility_only":
        v.reasons.append(Reason(text="This bill is from a doctors' group the hospital policy doesn't cover.",
                                quote=program.providers.quote, source=program.source_url))
        return _fallback(v, "not_eligible", "This bill isn't covered by the hospital's policy.")

    res = program.residency
    if res.verified and res.kind == "state" and res.states and household.state not in res.states:
        v.reasons.append(Reason(text=f"The policy requires living in {', '.join(res.states)}.", quote=res.quote,
                                source=program.source_url))
        return _fallback(v, "not_eligible", "You don't meet this hospital's residency rule.")
    if res.verified and res.kind in ("service_area", "county_list"):
        v.uncertain.append("Check you live in the hospital's service area: " + (", ".join(res.areas) or res.quote))

    # Presumptive eligibility: enrolment in a listed program qualifies outright.
    enrolled = {x.lower() for x in household.programs}
    for pr in program.presumptive:
        if pr.verified and pr.program.lower() in enrolled:
            v.reasons.append(Reason(text=f"Enrolled in {pr.program}: the policy qualifies you automatically.",
                                    quote=pr.quote, source=program.source_url))
            return _grant(v, pr.discount_pct, bill)

    tiers = sorted((t for t in program.tiers if t.verified), key=lambda t: t.max_fpl)
    # Policies write bounds as whole numbers ("0–250%", "251–400%"), so a household at 250.4% sits in the
    # gap. Tolerate one point below a lower bound; if tiers overlap, the patient gets the larger discount.
    fits = [t for t in tiers if t.min_fpl - 1 < p <= t.max_fpl]
    tier = max(fits, key=lambda t: t.discount_pct) if fits else None

    if tier is None:
        hs = program.hardship
        if hs.verified and hs.applies and hs.bill_pct_of_income and household.income_usd > 0:
            share = 100 * bill.balance_usd / household.income_usd
            if share > hs.bill_pct_of_income:
                v.uncertain.append(f"Your bill is {share:.0f}% of your yearly income; the policy's hardship rule "
                                   f"starts at {hs.bill_pct_of_income:g}%. Ask for a hardship review.")
                v.reasons.append(Reason(text="Possible hardship (catastrophic) discount.", quote=hs.quote,
                                        source=program.source_url))
        if not tiers:
            v.missing.append("The policy's income limits")
            return _fallback(v, "needs_info", "We couldn't prove this policy's income limits.")
        top = tiers[-1]
        v.reasons.append(Reason(text=f"This hospital helps households up to {top.max_fpl:g}% of the poverty level.",
                                quote=top.quote, source=program.source_url))
        return _fallback(v, "not_eligible", "Your income is above this hospital's limit.")

    v.reasons.append(Reason(text=f"This hospital gives {tier.discount_pct:g}% off between {tier.min_fpl:g}% and "
                                 f"{tier.max_fpl:g}% of the poverty level.", quote=tier.quote, source=program.source_url))

    at = program.asset_test
    if at.verified and at.applies:
        if household.assets_usd is None:
            v.missing.append("Your household's assets (the policy has an asset test)")
        elif at.limit_usd is not None and household.assets_usd > at.limit_usd:
            v.reasons.append(Reason(text=f"Assets above the policy's {_money(at.limit_usd)} limit.", quote=at.quote,
                                    source=program.source_url))
            return _fallback(v, "not_eligible", "Your assets are above this hospital's limit.")
    return _grant(v, tier.discount_pct, bill)


def _grant(v: Verdict, discount: float, bill: Bill) -> Verdict:
    v.discount_pct = discount
    v.expected_usd = round(bill.balance_usd * (1 - discount / 100), 2)
    likely = "likely " if v.missing or v.uncertain else ""
    if discount >= 100:
        v.status = "free"
        v.headline = f"You {likely}owe $0 of {_money(bill.balance_usd)}."
    else:
        v.status = "discount"
        v.headline = f"You {likely}owe {_money(v.expected_usd)} instead of {_money(bill.balance_usd)}."
    if v.colorado and v.expected_usd > v.colorado["total_max_usd"]:
        v.expected_usd = v.colorado["total_max_usd"]
        v.headline = (f"You {likely}owe at most {_money(v.expected_usd)} of {_money(bill.balance_usd)} "
                      f"under Colorado law.")
    return v


def _fallback(v: Verdict, status: str, headline: str) -> Verdict:
    """Not eligible under the hospital's own policy: Colorado's law may still cap the bill."""
    v.status, v.headline = status, headline
    if v.colorado and status in ("not_eligible", "no_policy", "needs_info"):
        cap = v.colorado["total_max_usd"]
        if cap < v.balance_usd:
            v.status = "colorado_cap"
            v.expected_usd = cap
            v.headline = (f"Colorado law caps this bill at {_money(cap)} "
                          f"({_money(v.colorado['monthly_max_usd'])}/month for at most 36 months).")
        v.reasons.append(Reason(text=f"At or below {CO_HDC_MAX_FPL}% of the poverty level, Colorado's Hospital "
                                     f"Discounted Care applies at every Colorado hospital.", source=CO_HDC_SOURCE))
    return v
