"""One audit of one policy, start to finish: Z3 checks, then everything that decides how much to trust each finding.

Shared by `scripts/audit.py` (batch) and the "Re-prove with Z3 now" button, so a live re-run returns exactly what
the published audit says: per-finding confirmation by the second model, inconclusive marking, summary comparisons
and the reviewers' verdicts.

Confirmation is per finding, not per check: the exact household (or first-bill day) Z3 found in Nemotron 3 Ultra's
model is replayed against Nemotron 3 Super's independent models (one per run, up to five: formal/runs/), and the
finding is "confirmed" only if most of them fail the same requirement for that same case. One Super run is noisy
(the same check got the same verdict in only 76% of cases across five runs, stability-eval.json); a majority isn't.
"""

from __future__ import annotations

import json
from pathlib import Path

from .formal import PolicyModel, evaluate, promised_day
from .law import audit, equivalent

ROOT = Path(__file__).resolve().parent.parent
NEMO, HAND = ROOT / "formal", ROOT / "eval" / "formal"
RUNS = NEMO / "runs"
_REVIEWS = ROOT / "eval" / "reviews.json"
REVIEWS = json.loads(_REVIEWS.read_text())["reviews"] if _REVIEWS.exists() else []

TIMING = {"co_eca_182": "eca", "fed_eca_120": "eca", "co_window_181": "application",
          "co_window_insured": "application", "fed_window_240": "application"}


def second_models(pid: str) -> list[PolicyModel]:
    """Nemotron 3 Super's independent models of this policy: the pipeline's own run, then any extra runs."""
    p = NEMO / f"{pid}.json"
    sm = json.loads(p.read_text())["report"].get("secondary_model") if p.exists() else None
    extra = sorted((RUNS / pid).glob("super-*.json"), key=lambda q: int(q.stem.split("-")[1]))
    return ([PolicyModel(**sm)] if sm else []) + [PolicyModel(**json.loads(q.read_text())["model"]) for q in extra]


def dropped_rules(pid: str) -> int:
    p = NEMO / f"{pid}.json"
    return lost_rules(json.loads(p.read_text())["report"]["primary"]) if p.exists() else 0


def lost_rules(rep: dict) -> int:
    """Rules one formalisation lost to the quote and meaning checks."""
    return sum(1 for d in rep.get("dropped", []) if d["kind"] in ("rule", "unsupported")
               and (d["kind"] == "rule" or str(d.get("id", "")).startswith("rule")))


def _key(o: dict | None) -> tuple:
    return (o["covered"], o["best_stated_discount_pct"]) if o else (None, None)


def replay(f: dict, other: PolicyModel) -> bool | None:
    """Does the second model fail this requirement for this same case? None when it can't be replayed."""
    check, h = f["check"], f.get("household")
    if not h:
        return None
    if check == "co_hdc_coverage":
        o = evaluate(other, h)
        return None if o is None else not o["covered"]
    if check in ("co_hdc_assets", "co_hdc_public", "monotone") and f.get("household_b"):
        a, b = evaluate(other, h), evaluate(other, f["household_b"])
        if a is None or b is None:
            return None
        if check == "monotone":  # the poorer household gets less than the richer one
            return (a["covered"], a["best_stated_discount_pct"]) < (b["covered"], b["best_stated_discount_pct"])
        return _key(a) != _key(b)
    if check in TIMING and "law_day" in h:
        day = promised_day(other, TIMING[check], h["first_statement_day"])
        return day is not None and day < h["law_day"]
    if check == "co_plan_4pct":
        m = other.payment_plan
        too_much = (m.max_pct_monthly_income or 0) > 4 or (m.max_months or 0) > 36
        if m.min_monthly_usd and h.get("income_usd") is not None:
            too_much = too_much or m.min_monthly_usd * 12 * 100 > 4 * h["income_usd"]
        return too_much
    return None


def apply_reviews(pid: str, findings: list[dict]) -> None:
    """A reviewer read the policy for this finding: show their verdict; a rejected finding is dismissed, not hidden."""
    for r in REVIEWS:
        if r["id"] != pid:
            continue
        for f in findings:
            if f["check"] == r["check"] and r["match"] in f["summary"] and f["status"] == "counterexample" \
                    and not f.get("review"):
                f["review"] = {"verdict": r["verdict"], "note": r["note"]}
                if r["verdict"] == "rejected":
                    f["status"] = "refuted"
                break


def audit_document(pid: str, pm: PolicyModel, source: str, summary: PolicyModel | None = None) -> list[dict]:
    """Every finding for one policy, with confidence, inconclusive marking and reviews applied."""
    primary = audit(pm)
    findings = [f.as_dict() for f in primary]
    others = second_models(pid) if source == "nemotron" else []
    if others:
        theirs: list[dict] = []
        for o in others:
            v: dict = {}
            for g in audit(o):
                v.setdefault(g.check, set()).add(g.status)
            theirs.append(v)
        for f in findings:
            f["second_opinion"] = sorted(set().union(*(v.get(f["check"], set()) for v in theirs)))
            if f["status"] == "counterexample":  # the same case, replayed in each Super model
                agree = sum(bool(replay(f, o)) for o in others)
            else:  # a proof or "not stated" is about the whole check: compare verdicts on it
                agree = sum(v.get(f["check"]) == {f["status"]} for v in theirs)
            f["votes"] = {"agree": agree, "of": len(others)}
            f["confidence"] = "confirmed" if agree * 2 > len(others) else "disputed"
    if summary is not None:
        single = source == "nemotron"  # one Ultra model per document: not cross-checked like the audit verdicts
        findings += [dict(f.as_dict(), check="summary_matches_policy",
                          **({"confidence": "unconfirmed"} if single and f.status == "counterexample" else {}))
                     for f in equivalent(pm, summary)]
    lost = dropped_rules(pid) if source == "nemotron" else 0
    if lost:
        for f in findings:
            if f["status"] == "counterexample" and f.get("basis") == "absence":
                f["status"] = "inconclusive"
                f["summary"] = (f"Inconclusive: {lost} of the model's rules were dropped by the quote and meaning "
                                f"checks, so a missing rule may be our error. Z3's case: " + f["summary"])
    apply_reviews(pid, findings)
    return findings


def reverify(pid: str, doc: dict, summary: PolicyModel | None = None) -> list[dict]:
    """Re-check the published audit with Z3 now, without replacing it.

    Proofs are re-run from scratch (a proof is deterministic); each counterexample's exact household or day is
    pinned and checked again against the same model. Returns the published findings, each with "live": "holds"
    or "changed", so the page never silently swaps a reviewed finding for a different one.
    """
    pm = PolicyModel(**doc["model"])
    fresh: dict = {}
    for g in audit(pm):
        fresh.setdefault(g.check, set()).add(g.status)
    out = []
    for f in doc["findings"]:
        f = dict(f)
        if f["check"] == "summary_matches_policy":
            a = evaluate(pm, f["household"]) if f.get("household") and summary else None
            b = evaluate(summary, f["household"]) if a is not None else None
            ok = (a is not None and _key(a) != _key(b)) if f["status"] == "counterexample" else \
                (summary is not None and not any(e.status == "counterexample" for e in equivalent(pm, summary)))
        elif f.get("household") and f["status"] in ("counterexample", "inconclusive", "refuted"):
            ok = bool(replay(f, pm))  # the witness still breaks the requirement in this model
        else:  # proved / not stated / n/a: the check's verdict, recomputed
            ok = f["status"] in fresh.get(f["check"], set())
        f["live"] = "holds" if ok else "changed"
        out.append(f)
    return out
