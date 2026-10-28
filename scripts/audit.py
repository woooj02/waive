"""Audit every formalised policy against Colorado HDC and 501(r) with Z3.

    ./venv/bin/python scripts/audit.py                  # Nemotron models in formal/, else hand references
    ./venv/bin/python scripts/audit.py --source hand    # only the hand-written references in eval/formal/
    ./venv/bin/python scripts/audit.py --formalize uchealth childrens-colorado   # run Nemotron first (costs money)

Writes audit/<id>.json (findings with counterexamples and quotes) and audit/index.json (the overview the app
shows). Every finding records which model it came from: "nemotron" or "hand reference".
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
from app.law import audit, equivalent  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HAND, NEMO, OUT = ROOT / "eval" / "formal", ROOT / "formal", ROOT / "audit"
CORPUS = ROOT / "data" / "corpus"


def run_formalize(pid: str) -> None:
    from app.formalize import formalize
    text = (CORPUS / f"{pid}.txt").read_text()
    meta = json.loads((CORPUS / f"{pid}.json").read_text())
    meter = llm.Meter()
    t0 = time.monotonic()
    pm, report = formalize(text, meter, doc="summary" if pid.endswith("-summary") else "policy",
                           source_url=meta.get("url", ""))
    NEMO.mkdir(exist_ok=True)
    (NEMO / f"{pid}.json").write_text(json.dumps({
        "model": json.loads(pm.model_dump_json()), "report": report, "calls": meter.as_dicts(),
        "cost_usd": round(sum(c.orig_cost_usd for c in meter.calls), 5),
        "seconds": round(time.monotonic() - t0, 1), "made": time.strftime("%Y-%m-%d")}, indent=1))
    print(f"{pid}: formalised for ${meter.cost_usd:.4f}; equivalent after repair: {report.get('equivalent')}")


def load(pid: str, source: str) -> tuple[PolicyModel, str] | None:
    if source != "hand" and (NEMO / f"{pid}.json").exists():
        return PolicyModel(**json.loads((NEMO / f"{pid}.json").read_text())["model"]), "nemotron"
    if source != "nemotron" and (HAND / f"{pid}.json").exists():
        return PolicyModel(**json.loads((HAND / f"{pid}.json").read_text())), "hand reference"
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["auto", "hand", "nemotron"], default="auto")
    ap.add_argument("--formalize", nargs="*", default=[])
    args = ap.parse_args()
    for pid in args.formalize:
        run_formalize(pid)

    ids = sorted({p.stem for p in HAND.glob("*.json")} | {p.stem for p in NEMO.glob("*.json")})
    OUT.mkdir(exist_ok=True)
    index = []
    for pid in [i for i in ids if not i.endswith("-summary")]:
        got = load(pid, args.source)
        if not got:
            continue
        pm, source = got
        t0 = time.monotonic()
        findings = [f.as_dict() for f in audit(pm)]
        pair = load(f"{pid}-summary", args.source)
        if pair:
            findings += [dict(f.as_dict(), check="summary_matches_policy") for f in equivalent(pm, pair[0])]
        secs = round(time.monotonic() - t0, 3)
        doc = {"id": pid, "hospital": pm.hospital, "source_url": pm.source_url, "nonprofit": pm.nonprofit,
               "model_source": source, "summary_source": pair[1] if pair else None, "solver_seconds": secs,
               "model": json.loads(pm.model_dump_json()), "findings": findings}
        (OUT / f"{pid}.json").write_text(json.dumps(doc, indent=1))
        count = lambda st: sum(f["status"] == st for f in findings)  # noqa: E731
        rank = {"counterexample": 0, "unknown": 1, "not_stated": 2, "proved": 3, "n/a": 4}
        checks: dict = {}
        for f in findings:  # worst verdict per check
            if f["check"] not in checks or rank[f["status"]] < rank[checks[f["check"]]]:
                checks[f["check"]] = f["status"]
        index.append({"id": pid, "hospital": pm.hospital, "model_source": source, "nonprofit": pm.nonprofit,
                      "checks": checks,
                      "proved": count("proved"), "counterexamples": count("counterexample"),
                      "not_stated": count("not_stated"), "solver_seconds": secs,
                      "headline": next((f["summary"] for f in findings if f["status"] == "counterexample"), "")})
        print(f"{pm.hospital:32} [{source}] proved {index[-1]['proved']:2}  counterexamples "
              f"{index[-1]['counterexamples']:2}  not stated {index[-1]['not_stated']:2}  ({secs}s)")
    titles = {}
    for pid in [p["id"] for p in index]:
        for f in json.loads((OUT / f"{pid}.json").read_text())["findings"]:
            titles.setdefault(f["check"], f["title"] if f["check"] != "summary_matches_policy"
                              else "Plain-language summary matches the policy")
    (OUT / "index.json").write_text(json.dumps({"made": time.strftime("%Y-%m-%d"), "checks": titles,
                                                "policies": index}, indent=1))


if __name__ == "__main__":
    main()
