"""How well does each Nemotron formalise a policy? Scored by Z3 against hand-written reference models.

    ./venv/bin/python scripts/formal_eval.py                      # nano, super, ultra, pipeline on every reference
    ./venv/bin/python scripts/formal_eval.py --models ultra --ids uchealth

For each model and document:
- equivalent: Z3 proves the model treats every household exactly like the reference (the strict score);
- differences: how many distinct ways it differs (each one a concrete household);
- findings_match: share of audit checks whose verdict (proved / counterexample / not stated / n/a) matches the
  verdict on the reference model, i.e. would the audit a user sees be the same;
- quotes_proven, cost and time.
Writes formal-eval.json. References are ours (hand-written from the policy text, every quote checked); we say so.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import llm  # noqa: E402
from app.formal import PolicyModel  # noqa: E402
from app.formalize import formalize, formalize_once  # noqa: E402
from app.law import audit, equivalent  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HAND, CORPUS = ROOT / "eval" / "formal", ROOT / "data" / "corpus"
OUT = ROOT / "formal-eval.json"
NONPROFIT = {e["id"]: e["nonprofit"] for e in json.loads((ROOT / "atlas-manifest.json").read_text())}


def verdicts(pm: PolicyModel) -> dict:
    out: dict = {}
    for f in audit(pm):
        out.setdefault(f.check, set()).add(f.status)
    return {k: sorted(v) for k, v in out.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=["nano", "super", "ultra", "repair"])
    ap.add_argument("--ids", nargs="*")
    args = ap.parse_args()
    ids = args.ids or sorted(p.stem for p in HAND.glob("*.json") if (CORPUS / f"{p.stem}.txt").exists())
    prev = json.loads(OUT.read_text()) if OUT.exists() else {}
    rows = {(r["model"], r["doc"]): r for r in prev.get("rows", [])}
    for key in args.models:
        for pid in ids:
            ref = PolicyModel(**json.loads((HAND / f"{pid}.json").read_text()))
            text = (CORPUS / f"{pid}.txt").read_text()
            meter = llm.Meter()
            t0 = time.monotonic()
            try:
                if key in ("pipeline", "repair"):
                    pm, rep = formalize(text, meter, doc=ref.doc, source_url=ref.source_url,
                                        rounds=1 if key == "repair" else 0)
                    clauses, proven = rep["primary"]["clauses"], len(pm.requires) + len(pm.rules)
                else:
                    pm, rep = formalize_once(text, key, meter, doc=ref.doc, source_url=ref.source_url)
                    clauses, proven = rep["clauses"], rep["clauses_proven"]
            except (llm.LLMError, ValueError) as exc:
                rows[(key, pid)] = {"model": key, "doc": pid, "error": str(exc)[:300]}
                print(f"{key:9} {pid:22} ERROR {exc}")
                continue
            pm.nonprofit = NONPROFIT.get(pid.removesuffix("-summary"), pm.nonprofit)  # a fact, not a reading
            diffs = [f for f in equivalent(pm, ref, names=(f"{key} model", "reference"))
                     if f.status == "counterexample"]
            want, got = verdicts(ref), verdicts(pm)
            match = sum(want[k] == got.get(k) for k in want)
            rows[(key, pid)] = {
                "model": key, "doc": pid, "equivalent": not diffs, "differences": [f.summary for f in diffs],
                "findings_match": f"{match}/{len(want)}", "findings_match_pct": round(100 * match / len(want)),
                "verdicts": got, "quotes_proven_pct": round(100 * proven / max(1, clauses)),
                "cost_usd": round(sum(c.orig_cost_usd for c in meter.calls), 5),
                "seconds": round(sum(c.orig_seconds for c in meter.calls) or time.monotonic() - t0, 1),
                "report": rep}
            r = rows[(key, pid)]
            print(f"{key:9} {pid:22} equivalent={r['equivalent']!s:5} diffs={len(diffs)} findings {r['findings_match']} "
                  f"quotes {r['quotes_proven_pct']}%  ${r['cost_usd']:.4f}  {r['seconds']}s")
    summary = {}
    for key in ["nano", "super", "ultra", "repair"]:
        rs = [r for (m, _), r in rows.items() if m == key and "error" not in r]
        if rs:
            summary[key] = {"label": "Ultra + Super + Z3 counterexample repair" if key == "repair"
                            else llm.MODELS[key].label, "docs": len(rs),
                            "equivalent": sum(r["equivalent"] for r in rs),
                            "findings_match_pct": round(sum(r["findings_match_pct"] for r in rs) / len(rs)),
                            "quotes_proven_pct": round(sum(r["quotes_proven_pct"] for r in rs) / len(rs)),
                            "cost_per_doc": round(sum(r["cost_usd"] for r in rs) / len(rs), 5),
                            "seconds_per_doc": round(sum(r["seconds"] for r in rs) / len(rs), 1)}
    # Two models as a confidence signal: verdicts Ultra and Super agree on vs those they don't.
    agree = right_a = dis = right_d = cost = 0
    for pid in ids:
        u, sp = rows.get(("ultra", pid)), rows.get(("super", pid))
        if not u or not sp or "error" in u or "error" in sp:
            continue
        want = verdicts(PolicyModel(**json.loads((HAND / f"{pid}.json").read_text())))
        cost += u["cost_usd"] + sp["cost_usd"]
        for c, w in want.items():
            if u["verdicts"].get(c) == sp["verdicts"].get(c):
                agree += 1
                right_a += u["verdicts"].get(c) == w
            else:
                dis += 1
                right_d += u["verdicts"].get(c) == w
    if agree:
        summary["two_models"] = {"label": "Ultra + Super, verdicts confirmed when both agree",
                                 "confirmed_share_pct": round(100 * agree / (agree + dis)),
                                 "confirmed_match_pct": round(100 * right_a / agree),
                                 "disputed_match_pct": round(100 * right_d / max(1, dis)),
                                 "cost_per_doc": round(cost / len(ids), 5)}
    OUT.write_text(json.dumps({"run": time.strftime("%Y-%m-%d"), "references": ids, "summary": summary,
                               "rows": list(rows.values())}, indent=1, default=list))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
