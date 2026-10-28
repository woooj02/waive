"""A charity-care policy as logic, and Z3 as the auditor.

A policy (or its plain-language summary) is written as a small, typed model:
eligibility rules in conjunctive normal form over household facts, the
timing promises it makes (application deadline, earliest collection action)
and its payment-plan terms. `encode()` turns one model into Z3 terms over a
symbolic household and a symbolic billing timeline, so a question such as
"is there any Colorado family at or below 250% of the poverty line that this
policy turns away?" is a satisfiability query: UNSAT is a proof over every
household in the domain, SAT comes with a concrete counterexample.

Counterexamples are made readable with MaxSMT: soft constraints pull every
fact toward an ordinary household (uninsured, Colorado resident, no assets,
three people), so the solver changes only what the failure actually needs.
"""

from __future__ import annotations

import math
import time
from typing import Literal

import z3
from pydantic import BaseModel, Field, field_validator

# 2026 HHS poverty guideline, 48 contiguous states + DC (Colorado).
FPL_BASE, FPL_STEP = 15_960, 5_680

BOOL_VARS = {
    "insured": "has health insurance",
    "underinsured": "insured, but out-of-pocket costs exceed what they can pay (as the policy defines it)",
    "co_resident": "lives in Colorado",
    "in_service_area": "lives in the hospital's own service area",
    "emergency": "the care was emergency care",
    "medically_necessary": "the care was emergency or medically necessary",
    "inpatient": "the care was an inpatient stay",
    "applied_public_programs": "has applied for Medicaid or other public programs",
    "presumptive_program": "is enrolled in a means-tested program (SNAP, WIC, Medicaid…) or homeless",
}
NUM_VARS = {
    "fpl_pct": "household income as % of the federal poverty line",
    "income_usd": "annual household income in dollars",
    "size": "people in the household",
    "assets_usd": "savings and other assets in dollars",
    "bill_pct_income": "the bill as % of annual income",
    "bill_pct_income_assets": "the bill as % of annual income plus assets",
}
VARS = list(BOOL_VARS) + list(NUM_VARS)
Op = Literal["<=", "<", ">=", ">", "==", "true", "false"]
Anchor = Literal["service", "first_statement"]


class Atom(BaseModel):
    var: str
    op: Op
    value: float | None = None


class Rule(BaseModel):
    """Assistance the policy grants when every clause holds (a clause is an OR of atoms)."""
    id: str
    label: str
    when: list[list[Atom]] = Field(default_factory=list)
    discount_pct: float | None = None  # None: assistance is granted but the amount isn't stated here
    quote: str = ""

    @field_validator("when", mode="before")
    @classmethod
    def _none_is_empty(cls, v):
        return v or []


class Requirement(BaseModel):
    """A condition every grant needs. Discretionary: the policy says it *may* deny."""
    id: str
    label: str
    cond: list[list[Atom]] = Field(default_factory=list)
    discretionary: bool = False
    quote: str = ""

    @field_validator("cond", mode="before")
    @classmethod
    def _none_is_empty(cls, v):
        return v or []


class Deadline(BaseModel):
    anchor: Anchor
    days: int
    quote: str = ""


class Timing(BaseModel):
    application: list[Deadline] = Field(default_factory=list)  # window ends at the LATER of these
    eca_not_before: list[Deadline] = Field(default_factory=list)  # no collection action before the LATER of these
    first_statement_from_day: int | None = None  # earliest day the policy says bills may go out


class PaymentPlan(BaseModel):
    max_pct_monthly_income: float | None = None
    max_months: int | None = None
    min_monthly_usd: float | None = None
    quote: str = ""


class PolicyModel(BaseModel):
    hospital: str
    doc: Literal["policy", "summary"] = "policy"
    source_url: str = ""
    nonprofit: bool | None = True  # None: not verified (often a public hospital district); 501(r) checks skipped
    requires: list[Requirement] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)
    timing: Timing = Field(default_factory=Timing)
    payment_plan: PaymentPlan = Field(default_factory=PaymentPlan)
    notes: list[str] = Field(default_factory=list)


class Household:
    """One symbolic household and billing timeline. Several can coexist (self-composition)."""

    def __init__(self, tag: str = ""):
        self.tag = tag
        self.b = {v: z3.Bool(f"{v}{tag}") for v in BOOL_VARS}
        self.size = z3.Int(f"size{tag}")
        self.income = z3.Real(f"income{tag}")
        self.assets = z3.Real(f"assets{tag}")
        self.bill = z3.Real(f"bill{tag}")
        self.statement = z3.Int(f"first_statement_day{tag}")  # days after service

    def fpl(self):
        return FPL_BASE + FPL_STEP * (z3.ToReal(self.size) - 1)

    def axioms(self) -> list:
        b = self.b
        return [self.size >= 1, self.size <= 12, self.income >= 0, self.income <= 1_000_000,
                self.assets >= 0, self.assets <= 10_000_000, self.bill >= 0, self.bill <= 5_000_000,
                self.statement >= 0, self.statement <= 400,
                z3.Implies(b["underinsured"], b["insured"]),
                z3.Implies(b["in_service_area"], b["co_resident"]),
                z3.Implies(b["emergency"], b["medically_necessary"])]

    def same_as(self, other: "Household", except_: set[str] = frozenset()) -> list:
        eq = [self.b[v] == other.b[v] for v in BOOL_VARS if v not in except_]
        for name in ("size", "income", "assets", "bill", "statement"):
            if name not in except_:
                eq.append(getattr(self, name) == getattr(other, name))
        return eq

    def atom(self, a: Atom):
        if a.var in BOOL_VARS:
            v = self.b[a.var]
            return z3.Not(v) if a.op == "false" else v
        if a.value is None:
            raise ValueError(f"{a.var} {a.op} needs a value")
        k = z3.RealVal(str(a.value))
        lhs, rhs = {
            "fpl_pct": (self.income * 100, k * self.fpl()),
            "income_usd": (self.income, k),
            "size": (z3.ToReal(self.size), k),
            "assets_usd": (self.assets, k),
            "bill_pct_income": (self.bill * 100, k * self.income),
            "bill_pct_income_assets": (self.bill * 100, k * (self.income + self.assets)),
        }[a.var]
        return {"<=": lhs <= rhs, "<": lhs < rhs, ">=": lhs >= rhs, ">": lhs > rhs, "==": lhs == rhs}[a.op]

    def cnf(self, clauses: list[list[Atom]]):
        return z3.And([z3.Or([self.atom(a) for a in c]) for c in clauses]) if clauses else z3.BoolVal(True)


class Encoded:
    """A policy model applied to one household: who is covered, the best stated discount, the deadlines."""

    def __init__(self, pm: PolicyModel, h: Household, honour_discretion: bool = True):
        self.pm, self.h = pm, h
        reqs = [r for r in pm.requires if honour_discretion or not r.discretionary]
        self.req_terms = {r.id: h.cnf(r.cond) for r in reqs}
        self.rule_terms = {r.id: h.cnf(r.when) for r in pm.rules}
        self.eligible = z3.And(list(self.req_terms.values())) if reqs else z3.BoolVal(True)
        self.covered = z3.And(self.eligible, z3.Or(list(self.rule_terms.values()) or [z3.BoolVal(False)]))
        best = z3.RealVal(0)
        for r in pm.rules:
            if r.discount_pct is not None:
                pct = z3.RealVal(str(r.discount_pct))
                best = z3.If(z3.And(self.eligible, self.rule_terms[r.id], best < pct), pct, best)
        self.best = best

    def _later(self, ds: list[Deadline]):
        if not ds:
            return None
        terms = [z3.IntVal(d.days) + (self.h.statement if d.anchor == "first_statement" else 0) for d in ds]
        out = terms[0]
        for t in terms[1:]:
            out = z3.If(t > out, t, out)
        return out

    @property
    def application_ends(self):
        return self._later(self.pm.timing.application)

    @property
    def eca_from(self):
        return self._later(self.pm.timing.eca_not_before)


# Soft preferences: an ordinary household. Weights say which facts we'd rather keep.
DEFAULTS = {"co_resident": True, "in_service_area": True, "insured": False, "underinsured": False,
            "emergency": True, "medically_necessary": True, "inpatient": False,
            "applied_public_programs": True, "presumptive_program": False}


def ordinary(h: Household) -> list[tuple]:
    soft = [(h.b[v] if want else z3.Not(h.b[v]), 3) for v, want in DEFAULTS.items()]
    soft += [(h.assets == 0, 3), (h.size == 3, 1), (h.bill <= 10_000, 1), (h.statement == 46, 1),
             (h.income * 100 >= 100 * h.fpl(), 1)]  # a working household, not $0 income
    return soft


def solve(hard: list, households: list[Household], timeout_ms: int = 20_000, soft: list = (), minimize=None,
          round_money: bool = True):
    """Return (status, model, seconds). Minimises deviation from an ordinary household (then `minimize`)."""
    opt = z3.Optimize()
    opt.set("timeout", timeout_ms)
    for h in households:
        opt.add(*h.axioms())
        for term, w in list(ordinary(h)) + list(soft):
            opt.add_soft(term, w)
    opt.add(*hard)
    if minimize is not None:
        opt.minimize(minimize)
    t0 = time.monotonic()
    res = opt.check()
    secs = round(time.monotonic() - t0, 3)
    if res == z3.sat:
        return "sat", (_round_money(opt.model(), hard, households) if round_money else opt.model()), secs
    return ("unsat" if res == z3.unsat else "unknown"), None, secs


def _num(model, term) -> float:
    v = model.eval(term, model_completion=True)
    if z3.is_int_value(v):
        return float(v.as_long())
    if z3.is_rational_value(v):
        return v.numerator_as_long() / v.denominator_as_long()
    return float(v.as_decimal(6).rstrip("?"))


def _round_money(model, hard: list, households: list[Household]):
    """Re-solve with incomes, assets and bills pinned to round numbers when that still fails the check."""
    pins = []
    for h in households:
        for term in (h.income, h.assets, h.bill):
            x = _num(model, term)
            for step in (1000, 100, 10, 1):
                cand = round(x / step) * step
                s = z3.Solver()
                s.add(*[a for hh in households for a in hh.axioms()], *hard, *pins, term == cand)
                if s.check() == z3.sat:
                    pins.append(term == cand)
                    break
    s = z3.Solver()
    s.add(*[a for hh in households for a in hh.axioms()], *hard, *pins)
    # Keep the minimised booleans and size where possible.
    for h in households:
        for v in BOOL_VARS:
            s.push()
            s.add(h.b[v] == z3.is_true(model.eval(h.b[v], model_completion=True)))
            if s.check() != z3.sat:
                s.pop()
        for term in (h.size, h.statement):
            s.push()
            s.add(term == model.eval(term, model_completion=True))
            if s.check() != z3.sat:
                s.pop()
    s.check()
    return s.model()


def describe(model, h: Household) -> dict:
    """The concrete household and timeline a SAT result describes."""
    size = int(_num(model, h.size))
    income = _num(model, h.income)
    fpl = FPL_BASE + FPL_STEP * (size - 1)
    out = {"size": size, "income_usd": round(income, 2), "fpl_pct": math.floor(10_000 * income / fpl) / 100,
           "assets_usd": round(_num(model, h.assets), 2), "bill_usd": round(_num(model, h.bill), 2),
           "first_statement_day": int(_num(model, h.statement))}
    for v in BOOL_VARS:
        out[v] = z3.is_true(model.eval(h.b[v], model_completion=True))
    return out


def explain(model, enc: Encoded) -> list[dict]:
    """Which of the policy's own clauses held for the counterexample, with their quotes."""
    rows = []
    for r in enc.pm.requires:
        if r.id in enc.req_terms:
            rows.append({"kind": "requires", "id": r.id, "label": r.label, "quote": r.quote,
                         "discretionary": r.discretionary,
                         "holds": z3.is_true(model.eval(enc.req_terms[r.id], model_completion=True))})
    for r in enc.pm.rules:
        rows.append({"kind": "rule", "id": r.id, "label": r.label, "quote": r.quote, "discount_pct": r.discount_pct,
                     "holds": z3.is_true(model.eval(enc.rule_terms[r.id], model_completion=True))})
    return rows


def outcome(model, enc: Encoded) -> dict:
    return {"covered": z3.is_true(model.eval(enc.covered, model_completion=True)),
            "best_stated_discount_pct": _num(model, enc.best)}


def household_text(d: dict) -> str:
    """'A family of 3 earning $40,000 (161% of the poverty line), Colorado residents, insured' in plain words."""
    who = "One person" if d["size"] == 1 else f"A household of {d['size']}"
    bits = [f"{who} earning ${d['income_usd']:,.0f} a year ({d['fpl_pct']:.1f}% of the poverty line)"]
    bits.append("living in Colorado" if d["co_resident"] else "living outside Colorado")
    if d["co_resident"] and not d["in_service_area"]:
        bits.append("outside the hospital's service area")
    if d["insured"]:
        bits.append("insured, and not under-insured as the policy defines it" if not d["underinsured"]
                    else "under-insured")
    else:
        bits.append("uninsured")
    if d["assets_usd"]:
        bits.append(f"with ${d['assets_usd']:,.0f} in savings")
    if d.get("bill_usd"):
        bits.append(f"owing ${d['bill_usd']:,.0f}")
    if d["presumptive_program"]:
        bits.append("enrolled in SNAP, WIC or a similar program (or homeless)")
    if not d["applied_public_programs"]:
        bits.append("who hasn't applied for Medicaid")
    if not d["emergency"]:
        bits.append("after non-emergency care" if d["medically_necessary"] else "after elective care")
    return ", ".join(bits)
