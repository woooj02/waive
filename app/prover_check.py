"""Check one household against one hospital's formal policy model, with Z3.

The facts the patient gave (size, income, insurance, state, programs, savings, the bill) are fixed; the facts we
don't know (service area, emergency or not, under-insured as the policy defines it...) stay symbolic. Z3 then
answers three questions: does every possible value of the unknowns lead to help (yes, for sure), does some value
(it depends: we name the fact), or none (no help under this policy's written rules). Colorado's own law is applied
on top in plain code, as before.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import z3

from . import engine
from .atlas import _tokens
from .formal import BOOL_VARS, Encoded, Household as Sym, PolicyModel
from .schema import Bill, Household

ROOT = Path(__file__).resolve().parent.parent
AUDIT = ROOT / "audit"

ASK = {"underinsured": "whether your out-of-pocket costs are more than you can pay (the policy's 'under-insured')",
       "in_service_area": "whether you live in the hospital's service area",
       "emergency": "whether the care was an emergency",
       "medically_necessary": "whether the care was medically necessary (not elective)",
       "inpatient": "whether you stayed overnight as an inpatient",
       "applied_public_programs": "whether you've applied for Medicaid"}


def find(hospital: str) -> tuple[str, PolicyModel, dict] | None:
    """The audited policy whose hospitals include this name (token match), with its audit summary."""
    idx = AUDIT / "index.json"
    if not idx.exists() or not hospital:
        return None
    words = lambda n: _tokens(n) or set(re.findall(r"[a-z0-9']+", n.lower()))  # noqa: E731  all-generic names
    want = words(hospital)
    best, best_score = None, 0.0
    for p in json.loads(idx.read_text())["policies"]:
        if p.get("not_formalised"):
            continue
        for name in (p.get("hospitals") or []) + [p["hospital"]]:
            have = words(name)
            if want and have:
                score = len(want & have) / len(want | have)
                if score > best_score:
                    best, best_score = p, score
    if not best or best_score < 0.5:
        return None
    doc = json.loads((AUDIT / f"{best['id']}.json").read_text())
    return best["id"], PolicyModel(**doc["model"]), doc


def _span(cons: list, enc: Encoded) -> tuple[float, float]:
    """Lowest and highest stated discount over every value of the unknown facts that still gets help."""
    out = []
    for goal in ("minimize", "maximize"):
        o = z3.Optimize()
        o.add(*cons, enc.covered)
        getattr(o, goal)(enc.best)
        o.check()
        out.append(float(o.model().eval(enc.best, model_completion=True).as_fraction()))
    return out[0], out[1]


def decide(pm: PolicyModel, household: Household, bill: Bill, doc_dropped: int = 0) -> tuple[engine.Verdict, dict]:
    h = Sym()
    enc = Encoded(pm, h)
    known = [h.size == household.size, h.income == household.income_usd,
             h.assets == (household.assets_usd or 0), h.bill == bill.balance_usd,
             h.b["insured"] == household.insured, h.b["co_resident"] == (household.state == "CO"),
             h.b["presumptive_program"] == bool(set(household.programs) & {"Medicaid", "SNAP", "WIC", "CICP"})]
    if not household.insured:
        known.append(z3.Not(h.b["underinsured"]))
    if household.state != "CO":
        known.append(z3.Not(h.b["in_service_area"]))
    s = z3.Solver()
    s.add(*h.axioms(), *known)
    if s.check() != z3.sat:  # outside the encoded domain: neither "always" nor "never" would mean anything
        v = engine.evaluate(None, household, bill)
        v.status, v.reasons, v.missing, v.uncertain = "needs_info", v.reasons[:1], [], []
        v.headline = ("This household is outside the range Waive's proofs cover (1–12 people, income up to $1M, "
                      "savings up to $10M, a bill up to $5M), so we can't check it. Ask the hospital's financial "
                      "assistance office directly.")
        return v, {"always": None, "sometimes": None, "depends_on": [], "in_domain": False,
                   "z3": z3.get_version_string()}
    always = s.check(z3.Not(enc.covered)) == z3.unsat
    sometimes = s.check(enc.covered) == z3.sat

    # One ordinary reading of the unknowns, to show which rules fire.
    opt = z3.Optimize()
    opt.add(*h.axioms(), *known, enc.covered)
    for v in ("in_service_area", "medically_necessary", "applied_public_programs"):
        opt.add_soft(h.b[v], 2)
    opt.add_soft(z3.Not(h.b["inpatient"]), 1)
    model = opt.model() if sometimes and opt.check() == z3.sat else None
    # The amount gets its own guarantee: the lowest and highest over every reading of the unknowns.
    lo, hi = _span([*h.axioms(), *known], enc) if sometimes else (0.0, 0.0)

    # Which unknown facts change the answer, or the amount?
    depends = []
    if sometimes and (not always or lo != hi):
        for v in ASK:
            if v == "underinsured" and not household.insured:
                continue
            a, b = s.check(enc.covered, h.b[v]) == z3.sat, s.check(enc.covered, z3.Not(h.b[v])) == z3.sat
            fixed_yes = s.check(z3.Not(enc.covered), h.b[v]) == z3.unsat
            fixed_no = s.check(z3.Not(enc.covered), z3.Not(h.b[v])) == z3.unsat
            amount = a and b and _span([*h.axioms(), *known, h.b[v]], enc) != _span([*h.axioms(), *known,
                                                                                    z3.Not(h.b[v])], enc)
            if (a != b) or (not always and (fixed_yes or fixed_no)) or amount:
                depends.append(ASK[v])

    v = engine.evaluate(None, household, bill)  # % FPL, Colorado cap, deadline, the household reason
    v.reasons = v.reasons[:1]
    fired = [r for r in pm.rules if model is not None and z3.is_true(model.eval(enc.rule_terms[r.id],
                                                                                 model_completion=True))]
    for r in fired:
        v.reasons.append(engine.Reason(text=f"This policy: {r.label}.", source=pm.source_url, quote=r.quote))
    for r in pm.requires:
        v.reasons.append(engine.Reason(text=f"The policy requires: {r.label}.", source=pm.source_url, quote=r.quote))
    v.uncertain = []
    v.missing = []
    if always or sometimes:
        sure = always and lo == hi
        v.discount_pct = lo if always else hi
        if sure and lo >= 100:
            v.status, v.expected_usd = "free", 0.0
            v.headline = "This hospital's own policy gives you free care for this bill."
        elif sure and lo > 0:
            v.status, v.expected_usd = "discount", round(bill.balance_usd * (1 - lo / 100), 2)
            v.headline = f"This hospital's own policy takes {lo:g}% off this bill."
        elif always and lo > 0:  # help for sure; part of the amount depends on a fact we don't know
            v.status, v.expected_usd = "discount", round(bill.balance_usd * (1 - lo / 100), 2)
            v.headline = (f"This hospital's own policy takes at least {lo:g}% off this bill, and up to {hi:g}% "
                          f"depending on a detail below.")
        elif always:
            v.status = "discount"
            what = fired[0].label if fired else "assistance"
            v.headline = (f"This hospital's own policy gives you help with this bill: {what} (amount set by its table"
                          + (f"; up to {hi:g}% off depending on a detail below)." if hi > 0 else ")."))
        else:  # help only for some values of the unknowns
            v.status = "discount"
            v.headline = (f"You may qualify: this hospital's own policy takes {'' if lo == hi else 'up to '}{hi:g}% off "
                          f"this bill if the detail below applies to you." if hi > 0 else
                          "You may qualify: this hospital's own policy gives help with this bill if the detail below "
                          "applies to you (amount set by its table).")
        if not sure:
            v.missing = depends or ["a detail the policy uses that we didn't ask about"]
    elif doc_dropped:  # the model lost rules in checking: a "no" from it isn't safe to tell a patient
        v.status = "colorado_cap" if v.colorado else "needs_info"
        v.headline = ("We can't be sure what this hospital's policy gives you: some of its rules couldn't be "
                      "verified." + (f" Colorado law still caps this bill at ${v.colorado['total_max_usd']:,.0f} "
                                     f"(${v.colorado['monthly_max_usd']:,.0f}/month for at most "
                                     f"{v.colorado['payments']} months)." if v.colorado else ""))
        v.missing = ["a person to read the policy (link below)"]
    elif v.colorado:
        v.status = "colorado_cap"
        v.headline = (f"The policy's written rules don't cover you, but Colorado law caps this bill at "
                      f"${v.colorado['total_max_usd']:,.0f} (${v.colorado['monthly_max_usd']:,.0f}/month for at "
                      f"most {v.colorado['payments']} months).")
    else:
        v.status, v.headline = "not_eligible", "This hospital's written rules don't give your household help with this bill."
    proof = {"always": always, "sometimes": sometimes, "depends_on": depends, "discount_range": [lo, hi],
             "in_domain": True, "z3": z3.get_version_string()}
    return v, proof
