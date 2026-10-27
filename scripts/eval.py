"""Score each Nemotron size at compiling policies, against hand-checked labels.

    ./venv/bin/python scripts/eval.py                 # all models, all gold policies
    ./venv/bin/python scripts/eval.py --models nano   # one model

For every model: field accuracy, income-limit accuracy, share of quotes the
verifier could prove, cost and time per policy. "Waive pipeline" is Ultra +
Super cross-check + adjudication + verifier, as the app runs it.
Writes atlas-eval.json (shown on the How it works page and in the README).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import compiler, engine, llm  # noqa: E402
from app.schema import Bill, Household, RuleProgram  # noqa: E402
from app.verify import found, normalize, verify  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GOLD = json.loads((ROOT / "eval" / "gold.json").read_text())
CORPUS = ROOT / "data" / "corpus"


def predicted(p: RuleProgram) -> dict:
    """Read the scored fields off a compiled (and verified) program. Unproven rules count as not stated."""
    tiers = [t for t in p.tiers if t.verified]
    general = [t for t in tiers if not t.facility]
    free = [t.max_fpl for t in tiers if (t.discount_pct or 0) >= 100 and not t.facility]
    out = {
        "free_to_fpl": max(free) if free else None,
        "discount_to_fpl": max((t.max_fpl for t in tiers), default=None),
        "residency_kind": p.residency.kind if p.residency.verified else None,
        "insured_eligible": p.insured.eligible if p.insured.verified else None,
        "hardship_pct": p.hardship.bill_pct_of_income if p.hardship.verified else None,
        "window_days": p.application.window_days if p.application.verified else None,
        "providers_scope": p.providers.scope if p.providers.verified else None,
        "asset_test": p.asset_test.applies if p.asset_test.verified else None,
    }
    band = [t for t in general + tiers if 250 < t.max_fpl <= 500 and (t.discount_pct or 0) < 100]
    out["discount_pct_250_500"] = band[0].discount_pct if band else None
    return out


def facility_ok(p: RuleProgram, want: dict) -> bool:
    hh = Household(size=1, income_usd=engine.fpl(1) * want["fpl"] / 100, state="TX")
    v = engine.evaluate(p, hh, Bill(hospital=want["facility"], balance_usd=1000))
    return v.discount_pct == want["discount_pct"]


def score(p: RuleProgram, gold: dict) -> dict:
    pred = predicted(p)
    fields, right, tier_fields, tier_right, misses = 0, 0, 0, 0, []
    for name, (want, _quote) in gold.items():
        if name == "facility_discount":
            ok = facility_ok(p, want)
        else:
            got = pred.get(name)
            ok = (got == want) if not isinstance(want, (int, float)) or isinstance(want, bool) else (
                got is not None and abs(float(got) - float(want)) < 1)
        fields += 1
        right += ok
        if name in ("free_to_fpl", "discount_to_fpl", "facility_discount", "discount_pct_250_500"):
            tier_fields += 1
            tier_right += ok
        if not ok:
            misses.append({"field": name, "want": want, "got": pred.get(name)})
    quotes = [t for t in p.tiers] + [p.asset_test, p.residency, p.hardship, p.insured, p.providers, p.application]
    quoted = [q for q in quotes if q.quote]
    return {"fields": fields, "right": right, "tier_fields": tier_fields, "tier_right": tier_right,
            "quotes": len(quoted), "quotes_proven": sum(q.verified for q in quoted), "misses": misses}


def run_model(key: str, pid: str, text: str) -> tuple[RuleProgram, llm.Meter, float]:
    meter = llm.Meter()
    t0 = time.monotonic()
    if key == "pipeline":
        prog, _ = compiler.compile_policy(text, meter)
    else:
        prog = verify(compiler.compile_once(text, key, meter), text)
    return prog, meter, time.monotonic() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=["nano", "lightning", "super", "ultra", "pipeline"])
    args = ap.parse_args()
    policies = {k: v for k, v in GOLD.items() if not k.startswith("_") and (CORPUS / f"{k}.txt").exists()}
    for pid, labels in policies.items():  # never score against a label we can't prove
        src = normalize((CORPUS / f"{pid}.txt").read_text())
        bad = [f for f, (_, q) in labels.items() if not found(q, src)]
        if bad:
            sys.exit(f"gold quote not found in {pid}: {bad}")

    out_path = ROOT / "atlas-eval.json"
    prev = json.loads(out_path.read_text()) if out_path.exists() else {}
    rows = {m["key"]: m for m in prev.get("models", [])}
    for key in args.models:
        label = "Waive pipeline (Ultra + Super cross-check)" if key == "pipeline" else llm.MODELS[key].label
        agg = {"fields": 0, "right": 0, "tier_fields": 0, "tier_right": 0, "quotes": 0, "quotes_proven": 0}
        cost = secs = 0.0
        per = []
        for pid, labels in policies.items():
            try:
                prog, meter, _ = run_model(key, pid, (CORPUS / f"{pid}.txt").read_text())
            except (llm.LLMError, ValueError) as exc:
                per.append({"policy": pid, "error": str(exc)[:200]})
                agg["fields"] += len(labels)
                continue
            s = score(prog, labels)
            for k in agg:
                agg[k] += s[k]
            cost += sum(c.orig_cost_usd for c in meter.calls)
            secs += sum(c.orig_seconds for c in meter.calls)
            per.append({"policy": pid, **{k: s[k] for k in ("right", "fields", "misses")}})
            print(f"{label:45} {pid:22} {s['right']}/{s['fields']}  misses={s['misses']}")
        n = max(1, len(policies))
        rows[key] = {"key": key, "label": label,
                     "field_acc": round(100 * agg["right"] / max(1, agg["fields"])),
                     "tier_acc": round(100 * agg["tier_right"] / max(1, agg["tier_fields"])),
                     "quotes_proven": round(100 * agg["quotes_proven"] / max(1, agg["quotes"])),
                     "cost": round(cost / n, 5), "seconds": round(secs / n, 1), "per_policy": per}
    order = ["nano", "lightning", "super", "ultra", "pipeline"]
    result = {"policies": list(policies), "labelled_fields": sum(len(v) for v in policies.values()),
              "run": time.strftime("%Y-%m-%d"),
              "summary": f"{len(policies)} Colorado policies, {sum(len(v) for v in policies.values())} hand-checked "
                         f"fields. Each model compiles alone; the pipeline adds the cross-check and adjudication.",
              "models": [rows[k] for k in order if k in rows]}
    out_path.write_text(json.dumps(result, indent=1))
    print(json.dumps([{k: m[k] for k in ("label", "field_acc", "tier_acc", "quotes_proven", "cost", "seconds")}
                      for m in result["models"]], indent=1))


if __name__ == "__main__":
    main()
