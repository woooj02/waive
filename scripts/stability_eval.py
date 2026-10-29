"""Repeated independent runs: how much does a formalisation vary, and how often is a "confirmed" finding real?

    ./venv/bin/python scripts/stability_eval.py --runs 3     # live where a run isn't saved yet (about $0.03 a run)

Each reference document is formalised N times by Nemotron 3 Ultra and N times by Nemotron 3 Super (run 1 reuses
the cached call formal_eval.py made; runs 2..N are fresh calls, cached under their own run number). Saved to
eval/runs/<doc>/<model>-<run>.json so the analysis re-runs for free.

Measured:
- verdict agreement with our reference per run: mean, lowest and highest run (not an accuracy rate);
- stability: share of checks whose verdict is the same in every run of a model;
- per finding: each counterexample from each Ultra run is replayed against the hand reference (does the
  reference fail the same requirement for the same case?). That tells how often a finding is real when Super
  confirms it, when it is disputed, and under other confirmation rules.
Writes stability-eval.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import llm  # noqa: E402
from app.auditing import lost_rules, replay, vote  # noqa: E402
from app.formal import PolicyModel  # noqa: E402
from app.law import audit  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HAND, CORPUS, RUNS = ROOT / "eval" / "formal", ROOT / "data" / "corpus", ROOT / "eval" / "runs"
OUT = ROOT / "stability-eval.json"
NONPROFIT = {e["id"]: e["nonprofit"] for e in json.loads((ROOT / "atlas-manifest.json").read_text())}
MODELS = ("ultra", "super")


def run_one(task: tuple[str, str, int]) -> str:
    pid, key, i = task
    path = RUNS / pid / f"{key}-{i}.json"
    if path.exists():
        return ""
    from app.formalize import formalize_once
    if i > 1:
        os.environ["LLM_RUN"] = str(i)
    ref = PolicyModel(**json.loads((HAND / f"{pid}.json").read_text()))
    meter = llm.Meter()
    try:
        pm, rep = formalize_once((CORPUS / f"{pid}.txt").read_text(), key, meter, doc=ref.doc,
                                 source_url=ref.source_url)
    except (llm.LLMError, ValueError) as exc:
        return f"{pid} {key} run {i}: {exc}"[:300]
    pm.nonprofit = NONPROFIT.get(pid.removesuffix("-summary"), pm.nonprofit)  # a fact, not a reading
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"model": json.loads(pm.model_dump_json()), "report": rep,
                                "cost_usd": round(sum(c.cost_usd for c in meter.calls), 5),
                                "fresh_calls": sum(not c.cached_result for c in meter.calls)}, indent=1))
    return ""


def ultra_findings(task: tuple[str, int]) -> list[dict]:
    """One Ultra run's audit, in a fresh process: Z3's choice among equally valid counterexamples depends on solver
    state, so each run's findings must not depend on what was solved before it."""
    pid, i = task
    return [f.as_dict() for f in audit(PolicyModel(**json.loads((RUNS / pid / f"ultra-{i}.json").read_text())["model"]))]


def verdicts(pm: PolicyModel) -> dict:
    out: dict = {}
    for f in audit(pm):
        out.setdefault(f.check, set()).add(f.status)
    return out


def rate(xs: list[bool]) -> dict:
    return {"n": len(xs), "real": sum(xs), "real_pct": round(100 * sum(xs) / len(xs)) if xs else None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--ids", nargs="*")
    args = ap.parse_args()
    ids = args.ids or sorted(p.stem for p in HAND.glob("*.json") if (CORPUS / f"{p.stem}.txt").exists())
    tasks = [(pid, key, i) for pid in ids for key in MODELS for i in range(1, args.runs + 1)]
    with ProcessPoolExecutor(6) as pool:  # Z3 isn't thread-safe: processes
        for err in pool.map(run_one, tasks):
            if err:
                print("error:", err)

    runs: dict = {}
    cost = 0.0
    for pid, key, i in tasks:
        p = RUNS / pid / f"{key}-{i}.json"
        if p.exists():
            d = json.loads(p.read_text())
            runs[(pid, key, i)] = (PolicyModel(**d["model"]), d["report"])
            cost += d["cost_usd"]
    refs = {pid: PolicyModel(**json.loads((HAND / f"{pid}.json").read_text())) for pid in ids}
    want = {pid: verdicts(ref) for pid, ref in refs.items()}
    got = {k: verdicts(pm) for k, (pm, _) in runs.items()}

    summary: dict = {"docs": len(ids), "runs": args.runs, "cost_usd_new_calls": round(cost, 4)}
    for key in MODELS:
        per_run = []
        for i in range(1, args.runs + 1):
            docs = [pid for pid in ids if (pid, key, i) in got]
            if docs:
                per_run.append(sum(100 * sum(want[pid][c] == got[(pid, key, i)].get(c) for c in want[pid])
                                   / len(want[pid]) for pid in docs) / len(docs))
        same = total = 0
        for pid in ids:
            rs = [got[(pid, key, i)] for i in range(1, args.runs + 1) if (pid, key, i) in got]
            for c in want[pid]:
                total += 1
                same += len({frozenset(r.get(c, ())) for r in rs}) == 1
        summary[key] = {"label": llm.MODELS[key].label, "verdict_agreement_mean_pct": round(sum(per_run) / len(per_run)),
                        "lowest_run_pct": round(min(per_run)), "highest_run_pct": round(max(per_run)),
                        "same_verdict_every_run_pct": round(100 * same / total)}

    # Per finding: every distinct counterexample any Ultra run produced, replayed against the reference and
    # against each Super run. A single Super run is what the audit used; several Super runs are the alternative.
    seen: dict = {}
    ultra = [(pid, i) for (pid, key, i) in runs if key == "ultra"]
    with ProcessPoolExecutor(6, max_tasks_per_child=1) as pool:
        found = dict(zip(ultra, pool.map(ultra_findings, ultra)))
    for (pid, i), fs in found.items():
        rep = runs[(pid, "ultra", i)][1]
        for fd in fs:
            k = (pid, fd["check"], fd["summary"])
            if fd["status"] != "counterexample" or k in seen or (truth := replay(fd, refs[pid])) is None:
                continue
            # each Super run votes as in the audit: one that can't tell (lost rules behind its "fails too") abstains
            votes = [vote(fd, *runs[(pid, "super", j)]) for j in range(1, args.runs + 1) if (pid, "super", j) in runs]
            seen[k] = {"doc": pid, "check": fd["check"], "summary": fd["summary"], "real": truth,
                       "inconclusive": fd["basis"] == "absence" and lost_rules(rep) > 0, "super_votes": votes}
    rows = list(seen.values())
    firm = [r for r in rows if not r["inconclusive"]]
    one_conf = [r["real"] for r in firm for v in r["super_votes"] if v]  # every (finding, single Super run) pair
    one_disp = [r["real"] for r in firm for v in r["super_votes"] if not v]  # an abstaining run doesn't confirm

    def majority(r: dict) -> bool:
        return r["super_votes"].count(True) * 2 > len(r["super_votes"])

    summary["findings"] = {
        "distinct_counterexamples": len(rows), "all": rate([r["real"] for r in rows]),
        "inconclusive": rate([r["real"] for r in rows if r["inconclusive"]]),
        "one_super_run": {"note": "every pairing of a finding with one Super run (what the audit used)",
                          "confirmed": rate(one_conf), "disputed": rate(one_disp)},
        "super_majority": {"note": f"confirmed when most of {args.runs} Super runs fail the same case",
                           "confirmed": rate([r["real"] for r in firm if majority(r)]),
                           "disputed": rate([r["real"] for r in firm if not majority(r)])}}
    OUT.write_text(json.dumps({"references": ids, "summary": summary, "findings": rows}, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
