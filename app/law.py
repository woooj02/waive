"""Colorado Hospital Discounted Care and federal 501(r), written as Z3 checks.

Each check quantifies over every household (or every billing timeline) in a stated,
bounded domain and asks Z3 for a counterexample. UNSAT is a proof that the encoded model
of the policy meets the requirement within that domain (how faithful the model is to the text is measured
separately, by the eval and the reviews); SAT returns the most ordinary household or
timeline that breaks it. Citations quote the statute, regulation or HCPF
manual verbatim (tests check the quotes against the downloaded sources).

What this is not: legal advice, or a finding that a hospital breaks the law
in practice. It audits the written policy: a hospital may still apply
Colorado law through a separate process the policy doesn't describe.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import z3

from .formal import (Encoded, Household, PolicyModel, describe, explain, household_text, outcome, solve)

SRC = {
    "hb1198": ("C.R.S. 25.5-3-501 et seq., 6-20-203 (HB21-1198)",
               "https://leg.colorado.gov/sites/default/files/2021a_1198_signed.pdf"),
    "manual": ("HCPF Hospital Discounted Care Operations Manual, Mar 31 2026",
               "https://hcpf.colorado.gov/sites/hcpf/files/Hospital%20Discounted%20Care%20Manual-03312026.pdf"),
    "501r1": ("26 CFR 1.501(r)-1(b)(3)", "https://www.ecfr.gov/current/title-26/section-1.501(r)-1"),
    "501r6": ("26 CFR 1.501(r)-6(c)(3)", "https://www.ecfr.gov/current/title-26/section-1.501(r)-6"),
}

LAW_QUOTES = {
    "co_qualified": ("manual", "Patients and their households are eligible for Hospital Discounted Care if, during the "
                     "required screening and application processes, they are determined to: Have a total household "
                     "income at or below 250% of the Federal Poverty Guidelines (FPG), and Be residents of the State "
                     "of Colorado."),
    "co_insured": ("hb1198", "IF REQUESTED BY THE PATIENT, A HEALTH-CARE FACILITY SHALL SCREEN AN INSURED PATIENT "
                   "FOR DISCOUNTED CARE"),
    "co_assets": ("hb1198", "THE METHODOLOGY MUST NOT CONSIDER A PATIENT'S ASSETS."),
    "co_public": ("hb1198", "DENY DISCOUNTED CARE ON THE BASIS THAT THE PATIENT HAS NOT APPLIED FOR ANY PUBLIC "
                  "BENEFITS PROGRAM"),
    "co_eca": ("hb1198", "NO MEDICAL CREDITOR COLLECTING ON A DEBT FOR HOSPITAL SERVICES SHALL ENGAGE IN ANY "
               "PERMISSIBLE EXTRAORDINARY COLLECTION ACTIONS UNTIL ONE HUNDRED EIGHTY-TWO DAYS AFTER THE DATE THE "
               "PATIENT RECEIVES HOSPITAL SERVICES."),
    "co_bills_46": ("manual", "begin sending bills beginning 46 days after the patient's date of service or date of "
                    "discharge"),
    "co_window": ("manual", "Patients may complete a screening at any time within at least 181 days after their date "
                  "of service or discharge, whichever is later"),
    "co_window_insured": ("manual", "At least 45 days from the date of the first bill after the patient's insurance "
                          "adjustment, even if that date falls outside the normal 181-day window."),
    "co_plan": ("hb1198", "THE PATIENT IS NOT PAYING MORE THAN FOUR PERCENT OF THE PATIENT'S MONTHLY HOUSEHOLD "
                "INCOME ON A BILL FROM A HEALTH-CARE FACILITY"),
    "co_36": ("hb1198", "AFTER A CUMULATIVE THIRTY-SIX MONTHS OF PAYMENTS, CONSIDER THE PATIENT'S BILL PAID IN FULL"),
    "fed_window": ("501r1", "ends on the later of the 240th day after the date that the first post-discharge billing "
                   "statement for the care is provided"),
    "fed_eca": ("501r6", "for at least 120 days from the date the hospital facility provides the first post-discharge "
                "billing statement for the care"),
}


@dataclass
class Finding:
    check: str
    title: str
    status: str  # proved | counterexample | not_stated | n/a | unknown
    law: list[dict]
    summary: str
    severity: str = ""  # gap | allows_denial | timing | ambiguity | info
    household: dict | None = None
    household_b: dict | None = None
    policy_outcome: dict | None = None
    clauses: list[dict] = field(default_factory=list)
    seconds: float = 0.0
    domain: str = ""
    basis: str = ""  # "absence": the failure is that no clause grants help (weak if clauses were dropped)

    def as_dict(self) -> dict:
        return asdict(self)


def cite(*keys: str) -> list[dict]:
    return [{"source": SRC[LAW_QUOTES[k][0]][0], "url": SRC[LAW_QUOTES[k][0]][1], "quote": LAW_QUOTES[k][1]}
            for k in keys]


def hdc_domain(h: Household):
    """Who Colorado's law protects: Colorado residents at or below 250% FPL, for emergency or necessary care."""
    return [h.b["co_resident"], h.income * 100 <= 250 * h.fpl(), h.b["medically_necessary"]]


HOUSEHOLDS = ("every household in the encoded domain: 1–12 people, income $0–$1M, assets $0–$10M, bill $0–$5M, "
              "first bill by day 400, every combination of the yes/no facts")


def check_hdc_coverage(pm: PolicyModel) -> list[Finding]:
    """One query per reason a protected household could be turned away: each requirement, then the rules."""
    h = Household()
    enc = Encoded(pm, h)
    title = "Covers everyone Colorado's Hospital Discounted Care protects"
    keys = ("co_qualified", "co_insured")
    queries = [(r, [z3.Not(enc.req_terms[r.id])]) for r in pm.requires]
    queries.append((None, [enc.eligible, z3.Not(enc.covered)]))
    out, secs_total = [], 0.0
    for req, extra in queries:
        status, m, secs = solve(hdc_domain(h) + extra, [h])
        secs_total += secs
        if m is None:
            if status == "unknown":
                out.append(Finding("co_hdc_coverage", title, "unknown", cite(*keys), "Solver timed out.",
                                   seconds=secs, domain=HOUSEHOLDS))
            continue
        d = describe(m, h)
        f = Finding("co_hdc_coverage", title, "counterexample", cite(*keys), "", household=d,
                    policy_outcome=outcome(m, enc), clauses=explain(m, enc), seconds=secs, domain=HOUSEHOLDS)
        if req is not None:
            f.severity = "allows_denial" if req.discretionary else "gap"
            verb = "may be denied" if req.discretionary else "is turned away"
            f.summary = (f"{household_text(d)} {verb} by this requirement: \"{req.label}\". Colorado law makes "
                         f"them eligible for Hospital Discounted Care.")
        else:
            f.severity, f.basis = "gap", "absence"
            f.summary = (f"{household_text(d)} meets every requirement, but no rule of this policy grants them help. "
                         f"Colorado law makes them eligible for Hospital Discounted Care.")
        out.append(f)
    if not out:
        out.append(Finding("co_hdc_coverage", title, "proved", cite(*keys),
                           "Proved for the encoded rules: every Colorado resident at or below 250% of the poverty line qualifies for help "
                           "under this policy's written rules.", seconds=round(secs_total, 3), domain=HOUSEHOLDS))
    return out


def _noninterference(pm: PolicyModel, var: str, check: str, title: str, keys: tuple, what: str) -> Finding:
    """Two households identical except `var`, both protected by Colorado law: must get the same outcome."""
    a, b = Household("_a"), Household("_b")
    ea, eb = Encoded(pm, a), Encoded(pm, b)
    attr = {"assets_usd": "assets"}.get(var)
    diff = {attr} if attr else {var}
    hard = hdc_domain(a) + hdc_domain(b) + a.same_as(b, except_=diff)
    # Fix which household is which, so the pair comes out the same way every time.
    hard.append(a.assets > b.assets if var == "assets_usd" else z3.And(z3.Not(a.b[var]), b.b[var]))
    hard.append(z3.Or(ea.covered != eb.covered, ea.best != eb.best))
    soft = [(z3.Or(a.assets >= 5000, b.assets >= 5000), 4)] if var == "assets_usd" else []
    status, m, secs = solve(hard, [a, b], soft=soft)
    f = Finding(check, title, status, cite(*keys), "", seconds=secs, domain=HOUSEHOLDS + " (pairs of households)")
    if status == "unsat":
        f.status = "proved"
        f.summary = f"Proved for the encoded rules: for households Colorado's law protects, {what} never changes the outcome."
        return f
    if m is None:
        return f
    da, db = describe(m, a), describe(m, b)
    oa, ob = outcome(m, ea), outcome(m, eb)
    f.status, f.severity = "counterexample", "gap"
    f.household, f.household_b, f.policy_outcome = da, db, {"a": oa, "b": ob}
    f.clauses = explain(m, ea if not oa["covered"] else eb)
    disc = [r for r in pm.requires if r.discretionary and any(at.var == var for cl in r.cond for at in cl)]
    if disc:
        f.severity = "allows_denial"
    worse, better = (da, db) if (oa["covered"], oa["best_stated_discount_pct"]) < (ob["covered"], ob["best_stated_discount_pct"]) else (db, da)
    f.summary = (f"Two households differ only in {what}: {household_text(worse)} is treated worse than the same "
                 f"household with {_fmt(var, better)}. Colorado law says {what} must not decide discounted care.")
    return f


def _fmt(var: str, d: dict) -> str:
    if var == "assets_usd":
        return f"${d['assets_usd']:,.0f} in savings"
    if var == "applied_public_programs":
        return "a Medicaid application on file" if d[var] else "no Medicaid application"
    return f"{var}={d[var]}"


def check_assets(pm: PolicyModel) -> Finding:
    return _noninterference(pm, "assets_usd", "co_hdc_assets", "Ignores assets for Colorado-protected households",
                            ("co_assets",), "savings and assets")


def check_public_programs(pm: PolicyModel) -> Finding:
    return _noninterference(pm, "applied_public_programs", "co_hdc_public",
                            "Doesn't require a Medicaid application first", ("co_public",),
                            "whether they applied for Medicaid or other public programs")


def _timeline(pm: PolicyModel, check: str, title: str, keys: tuple, term_of, required_of, lo: int,
              ok_text: str, bad_text) -> Finding:
    """For every first-bill day s >= lo: the policy's promised day must be >= the law's required day."""
    h = Household()
    enc = Encoded(pm, h)
    promised = term_of(enc)
    f = Finding(check, title, "not_stated", cite(*keys), "", domain=f"every first-bill day from day {lo} to day 400")
    if promised is None:
        f.severity = "info"
        f.summary = "The policy doesn't state this in a way that can be checked. The law applies anyway."
        return f
    lo = max(lo, pm.timing.first_statement_from_day or 0)
    bad = promised < required_of(h)
    status, m, secs = solve([h.statement >= lo, bad], [h], minimize=h.statement)
    f.status, f.seconds = status, secs
    if status == "unsat":
        f.status, f.summary = "proved", ok_text
        return f
    if m is None:
        return f
    s = describe(m, h)["first_statement_day"]
    got = m.eval(promised, model_completion=True).as_long()
    need = m.eval(required_of(h), model_completion=True).as_long()
    later_ok, _, _ = solve([h.statement >= s, h.statement <= 400, z3.Not(bad)], [h])
    field_ = "eca_not_before" if "eca" in check else "application"
    f.clauses = [{"kind": "timing", "id": field_, "label": f"{d.days} days after {d.anchor.replace('_', ' ')}",
                  "quote": d.quote, "holds": True} for d in getattr(pm.timing, field_)]
    if pm.timing.first_statement_from_day:
        f.clauses.append({"kind": "timing", "id": "first_statement_from_day", "holds": True, "quote": "",
                          "label": f"bills may go out from day {pm.timing.first_statement_from_day}"})
    f.status, f.severity = "counterexample", "timing"
    f.household = {"first_statement_day": s, "policy_day": got, "law_day": need,
                   "fails_every_later_day": later_ok == "unsat"}
    f.summary = bad_text(s, got, need)
    if later_ok == "unsat" and s > lo:
        f.summary += f" The same happens for every first-bill day after day {s}."
    return f


def check_co_eca(pm: PolicyModel) -> Finding:
    return _timeline(
        pm, "co_eca_182", "No collection lawsuits or garnishment before day 182", ("co_eca", "co_bills_46"),
        lambda e: e.eca_from, lambda h: z3.IntVal(182), 46,
        "Proved for the encoded rules: whatever day the first bill goes out, the policy's own timeline never allows an extraordinary collection "
        "action (a lawsuit, garnishment) before day 182 after care.",
        lambda s, got, need: f"If the first bill goes out on day {s} after care (Colorado allows bills from day 46), "
                             f"the policy's written timeline allows extraordinary collection actions (lawsuits, garnishment) "
                             f"from day {got}. Colorado law "
                             f"bars them until day {need}.")


def check_co_window(pm: PolicyModel) -> Finding:
    return _timeline(
        pm, "co_window_181", "Accepts applications for at least 181 days after care", ("co_window", "co_bills_46"),
        lambda e: e.application_ends, lambda h: z3.IntVal(181), 46,
        "Proved for the encoded rules: whatever day the first bill goes out (Colorado allows bills from day 46), the application window "
        "never closes before day 181 after care.",
        lambda s, got, need: f"With a first bill on day {s}, the policy closes applications on day {got} after care; "
                             f"Colorado requires at least day {need}.")


def check_co_window_insured(pm: PolicyModel) -> Finding:
    return _timeline(
        pm, "co_window_insured", "Insured patients get 45 days after their first post-insurance bill",
        ("co_window_insured",), lambda e: e.application_ends, lambda h: h.statement + 45, 0,
        "Proved for the encoded rules: the window always stays open at least 45 days past the first bill.",
        lambda s, got, need: f"If insurance takes until day {s} to process and the first bill arrives then, the "
                             f"policy's window has already closed on day {got}; Colorado gives until day {need}.")


def _not_501r(pm: PolicyModel) -> str:
    if pm.nonprofit is None:
        return ("Skipped: 501(r) applies to tax-exempt hospitals, and we haven't verified this hospital's status "
                "(many rural Colorado hospitals are public districts).")
    return "501(r) applies to nonprofit hospitals; this one is for-profit."


def check_fed_window(pm: PolicyModel) -> Finding:
    if not pm.nonprofit:
        return Finding("fed_window_240", "Application window of 240 days after the first bill (501(r))", "n/a",
                       cite("fed_window"), _not_501r(pm))
    return _timeline(
        pm, "fed_window_240", "Application window of 240 days after the first bill (501(r))", ("fed_window",),
        lambda e: e.application_ends, lambda h: h.statement + 240, 0,
        "Proved for the encoded rules: for any first-bill day, applications stay open at least 240 days after it.",
        lambda s, got, need: f"If the first bill goes out on day {s} after care (common when insurance is billed "
                             f"first), the policy stops taking applications on day {got}; 501(r) keeps them open "
                             f"until day {need}.")


def check_fed_eca(pm: PolicyModel) -> Finding:
    if not pm.nonprofit:
        return Finding("fed_eca_120", "No extraordinary collection actions in the first 120 days after the first bill (501(r))",
                       "n/a", cite("fed_eca"), _not_501r(pm))
    return _timeline(
        pm, "fed_eca_120", "No extraordinary collection actions in the first 120 days after the first bill (501(r))",
        ("fed_eca",), lambda e: e.eca_from, lambda h: h.statement + 120, 0,
        "Proved for the encoded rules: extraordinary collection actions never start sooner than 120 days after the first bill.",
        lambda s, got, need: f"With a first bill on day {s}, the policy allows extraordinary collection actions on day {got}; "
                             f"501(r) requires waiting until day {need}.")


def check_payment_plan(pm: PolicyModel) -> Finding:
    pp = pm.payment_plan
    f = Finding("co_plan_4pct", "Payment plans capped at 4% of monthly income, done after 36 payments",
                "not_stated", cite("co_plan", "co_36"), "")
    if pp.max_pct_monthly_income is None and pp.max_months is None and pp.min_monthly_usd is None:
        f.severity = "info"
        f.summary = "The policy states no payment-plan terms to check. Colorado's 4% / 36-month limits apply anyway."
        return f
    problems = []
    if pp.max_pct_monthly_income is not None and pp.max_pct_monthly_income > 4:
        problems.append(f"the plan allows up to {pp.max_pct_monthly_income:g}% of monthly income")
    if pp.max_months is not None and pp.max_months > 36:
        problems.append(f"payments can run {pp.max_months} months")
    if pp.min_monthly_usd:
        h = Household()
        status, m, secs = solve(hdc_domain(h) + [z3.RealVal(str(pp.min_monthly_usd)) * 12 * 100 > 4 * h.income],
                                [h])
        f.seconds = secs
        if m is not None:
            d = describe(m, h)
            f.household = d
            problems.append(f"the ${pp.min_monthly_usd:g} minimum monthly payment is more than 4% of the monthly "
                            f"income of {household_text(d)[0].lower() + household_text(d)[1:]}")
    if problems:
        f.status, f.severity = "counterexample", "gap"
        f.summary = "Conflicts with Colorado's payment cap: " + "; ".join(problems) + "."
    else:
        f.status, f.summary = "proved", "Proved for the encoded rules: the stated plan terms stay within 4% of monthly income and 36 months."
    return f


def check_monotone(pm: PolicyModel) -> Finding:
    """No household should get less help than an otherwise identical household that earns more."""
    a, b = Household("_poorer"), Household("_richer")
    ea, eb = Encoded(pm, a), Encoded(pm, b)
    hard = a.same_as(b, except_={"income"}) + [a.income < b.income,
                                               z3.Or(z3.And(z3.Not(ea.covered), eb.covered), ea.best < eb.best)]
    status, m, secs = solve(hard, [a, b], soft=[(b.income - a.income <= 100, 4)], steps=(1,))
    f = Finding("monotone", "Earning less never means getting less help", status, [],
                "", seconds=secs, domain=HOUSEHOLDS + " (pairs of households)")
    if status == "unsat":
        f.status, f.summary = "proved", "Proved for the encoded rules: no household gets less help than an identical one that earns more."
        return f
    if m is None:
        return f
    da, db = describe(m, a), describe(m, b)
    f.status, f.severity, f.basis = "counterexample", "ambiguity", "absence"
    f.household, f.household_b = da, db
    f.policy_outcome = {"a": outcome(m, ea), "b": outcome(m, eb)}
    f.clauses = explain(m, ea)
    oa, ob = f.policy_outcome["a"], f.policy_outcome["b"]
    f.summary = (f"{household_text(da)} gets {_say(oa)}; the same household earning more, at {db['fpl_pct']:.2f}% "
                 f"of the poverty line, gets {_say(ob)}. The written income bands leave a gap between them.")
    return f


CHECKS = [check_hdc_coverage, check_assets, check_public_programs, check_co_eca, check_co_window,
          check_co_window_insured, check_payment_plan, check_fed_window, check_fed_eca, check_monotone]


def audit(pm: PolicyModel) -> list[Finding]:
    out: list[Finding] = []
    for c in CHECKS:
        r = c(pm)
        out.extend(r if isinstance(r, list) else [r])
    return out


def _say(o: dict) -> str:
    if not o["covered"]:
        return "no help"
    return f"{o['best_stated_discount_pct']:g}% off" if o["best_stated_discount_pct"] else "help (amount not stated)"


def equivalent(a: PolicyModel, b: PolicyModel, limit: int = 4, names: tuple[str, str] | None = None) -> list[Finding]:
    """Do two models (policy vs summary, Ultra vs Super, model vs hand) treat every household the same way?

    Returns one finding per distinct way they differ: after each counterexample, the pattern of which clauses
    held in both models is blocked, so the next one must differ for another reason.
    """
    h = Household()
    ea, eb = Encoded(a, h), Encoded(b, h)
    hard = [z3.Or(ea.covered != eb.covered, ea.best != eb.best)]
    na, nb = names or (a.doc, b.doc)
    title = f"The {na} and the {nb} treat every household the same"
    out: list[Finding] = []
    for _ in range(limit):
        status, m, secs = solve(hard, [h])
        if m is None:
            if not out:
                out.append(Finding("equivalent", title, "proved" if status == "unsat" else status, [],
                                   "Proved equivalent: no household is treated differently." if status == "unsat"
                                   else "Solver timed out.", seconds=secs, domain=HOUSEHOLDS))
            break
        d = describe(m, h)
        oa, ob = outcome(m, ea), outcome(m, eb)
        out.append(Finding("equivalent", title, "counterexample", [],
                           f"{household_text(d)}: the {na} gives {_say(oa)}, the {nb} gives {_say(ob)}.",
                           severity="gap", household=d, policy_outcome={"a": oa, "b": ob},
                           clauses=[dict(c, model=na) for c in explain(m, ea)] +
                                   [dict(c, model=nb) for c in explain(m, eb)],
                           seconds=secs, domain=HOUSEHOLDS))
        # The cause: rules that fired on the more generous side, requirements that failed on the other.
        win, lose = (ea, eb) if (oa["covered"], oa["best_stated_discount_pct"]) > \
            (ob["covered"], ob["best_stated_discount_pct"]) else (eb, ea)
        val = lambda t: z3.is_true(m.eval(t, model_completion=True))  # noqa: E731
        same_cover = oa["covered"] == ob["covered"]
        top = max(oa["best_stated_discount_pct"], ob["best_stated_discount_pct"])
        fired = [r for r in win.pm.rules if val(win.rule_terms[r.id])
                 and (not same_cover or r.discount_pct == top)]
        cause = [win.rule_terms[r.id] for r in fired] + [z3.Not(t) for t in lose.req_terms.values() if not val(t)]
        if len(cause) > 1 and not same_cover:
            cause = cause[:1]  # the first generous rule is enough to name the difference
        hard.append(z3.Not(z3.And(cause)) if cause else z3.BoolVal(False))
    return out
