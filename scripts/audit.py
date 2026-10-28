"""Audit every formalised policy against Colorado HDC and 501(r) with Z3.

    ./venv/bin/python scripts/audit.py                  # Nemotron models in formal/, else hand references
    ./venv/bin/python scripts/audit.py --source hand    # only the hand-written references in eval/formal/
    ./venv/bin/python scripts/audit.py --formalize uchealth childrens-colorado   # run Nemotron first (costs money)
    ./venv/bin/python scripts/audit.py --super-runs 5   # up to 5 independent Super models per policy (live if missing)

Writes audit/<id>.json (findings with counterexamples and quotes) and audit/index.json (the overview the app
shows). Every finding records which model it came from: "nemotron" or "hand reference".
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor  # Z3 isn't thread-safe: processes, not threads
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import llm  # noqa: E402
from app.formal import PolicyModel  # noqa: E402
from app.auditing import RUNS, audit_document, dropped_rules  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HAND, NEMO, OUT = ROOT / "eval" / "formal", ROOT / "formal", ROOT / "audit"
CORPUS = ROOT / "data" / "corpus"
MANIFEST = {e["id"]: e for e in json.loads((ROOT / "atlas-manifest.json").read_text())}




def run_formalize(pid: str) -> None:
    from app.formalize import formalize
    text = (CORPUS / f"{pid}.txt").read_text()
    meta = json.loads((CORPUS / f"{pid}.json").read_text())
    meter = llm.Meter()
    t0 = time.monotonic()
    summary = pid.endswith("-summary") or re.search(r"summary|plain[-_ ]?language", meta.get("url", ""), re.I)
    pm, report = formalize(text, meter, doc="summary" if summary else "policy", source_url=meta.get("url", ""))
    entry = MANIFEST.get(pid.removesuffix("-summary"), {})
    pm.nonprofit = entry.get("nonprofit") if entry else pm.nonprofit  # a fact from the hospital list, not a reading
    if entry.get("hospital"):
        pm.hospital = entry["hospital"]
    if report.get("secondary_model"):
        report["secondary_model"]["nonprofit"] = pm.nonprofit
    NEMO.mkdir(exist_ok=True)
    (NEMO / f"{pid}.json").write_text(json.dumps({
        "model": json.loads(pm.model_dump_json()), "report": report, "calls": meter.as_dicts(),
        "cost_usd": round(sum(c.orig_cost_usd for c in meter.calls), 5),
        "seconds": round(time.monotonic() - t0, 1), "made": time.strftime("%Y-%m-%d")}, indent=1))
    print(f"{pid}: formalised for ${meter.cost_usd:.4f}; equivalent after repair: {report.get('equivalent')}")






def super_run(task: tuple[str, int]) -> str:
    """One more independent Nemotron 3 Super model of a policy, for majority confirmation (cached per run number)."""
    import os
    from app.formalize import formalize_once
    pid, j = task
    out = RUNS / pid / f"super-{j}.json"
    if out.exists():
        return ""
    os.environ["LLM_RUN"] = str(j)
    first = PolicyModel(**json.loads((NEMO / f"{pid}.json").read_text())["model"])
    meter = llm.Meter()
    try:
        pm, report = formalize_once((CORPUS / f"{pid}.txt").read_text(), "super", meter, doc=first.doc,
                                    source_url=first.source_url)
    except Exception as exc:  # reported; the audit uses the runs that exist
        return f"{pid} Super run {j}: {type(exc).__name__}: {str(exc)[:200]}"
    pm.nonprofit, pm.hospital = first.nonprofit, first.hospital
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"model": json.loads(pm.model_dump_json()), "report": report,
                               "cost_usd": round(sum(c.orig_cost_usd for c in meter.calls), 5),
                               "made": time.strftime("%Y-%m-%d")}, indent=1))
    return ""


def _safe_formalize(pid: str) -> str:
    try:
        if not (NEMO / f"{pid}.json").exists():
            run_formalize(pid)
        return ""
    except Exception as exc:  # one bad document must not stop the batch; it's reported
        return f"{type(exc).__name__}: {str(exc)[:200]}"


def shared_hospitals(pid: str) -> list[str]:
    """All hospitals whose policy is this same document."""
    url = json.loads((CORPUS / f"{pid}.json").read_text()).get("url") if (CORPUS / f"{pid}.json").exists() else None
    names = list(MANIFEST.get(pid, {}).get("hospitals", []))
    for meta in CORPUS.glob("*.json"):
        if url and meta.stem != pid and json.loads(meta.read_text()).get("url") == url:
            names += MANIFEST.get(meta.stem, {}).get("hospitals", []) or [MANIFEST.get(meta.stem, {}).get("hospital", "")]
    return [n for n in dict.fromkeys(names) if n]


def settle(pid: str, findings: list[dict], pm: PolicyModel) -> None:
    """Disputed, inconclusive or single-model counterexamples: Ultra reads the policy for that one case."""
    from app.adjudicate import adjudicate
    text_id = pid if (CORPUS / f"{pid}.txt").exists() else pid.removesuffix("-summary")
    text = (CORPUS / f"{text_id}.txt").read_text()
    meter = llm.Meter()
    for f in findings:
        weak = f["status"] == "inconclusive" or f.get("confidence") in ("disputed", "unconfirmed")
        if f["status"] not in ("counterexample", "inconclusive") or not weak or f["check"] == "summary_matches_policy":
            continue  # summary comparisons need both documents: left for a person
        a = adjudicate(text, f, meter)
        f["adjudication"] = a
        if a["verdict"] == "holds":
            f["status"] = "counterexample"
            f["confidence"] = "adjudicated"
            f["summary"] = f["summary"].split("Z3's case: ")[-1]
        elif a["verdict"] == "refuted":
            f["status"] = "refuted"




FEATURE_ORDER = ["co_eca_182", "co_window_181", "fed_window_240", "co_hdc_coverage"]


def featured(index: list[dict]) -> dict | None:
    """The finding the page opens on: confirmed by both models and upheld on review, most concrete check first."""
    best = None
    for p in index:
        if p.get("not_formalised"):
            continue
        for i, f in enumerate(json.loads((OUT / f"{p['id']}.json").read_text())["findings"]):
            if f["status"] == "counterexample" and f.get("confidence") == "confirmed" \
                    and f.get("review", {}).get("verdict") == "upheld" and f["check"] in FEATURE_ORDER:
                rank = (FEATURE_ORDER.index(f["check"]), p["id"])
                if best is None or rank < best[0]:
                    best = (rank, {"id": p["id"], "hospital": p["hospital"], "finding": i})
    return best[1] if best else None


def display_name(pid: str, pm: PolicyModel) -> str:
    """A document shared by several hospitals is named after the system."""
    entry = MANIFEST.get(pid, {})
    return entry.get("system", pm.hospital) if len(shared_hospitals(pid)) > len(entry.get("hospitals", [])) + 1 \
        else pm.hospital


def load(pid: str, source: str) -> tuple[PolicyModel, str] | None:
    if source != "hand" and (NEMO / f"{pid}.json").exists():
        return PolicyModel(**json.loads((NEMO / f"{pid}.json").read_text())["model"]), "nemotron"
    if source != "nemotron" and (HAND / f"{pid}.json").exists():
        return PolicyModel(**json.loads((HAND / f"{pid}.json").read_text())), "hand reference"
    return None


def audit_one(task: tuple[str, str, bool]) -> dict | None:
    """One document's audit, in a fresh process: Z3's choice among equally valid counterexamples can depend on
    solver state left by earlier documents, so each document starts clean and its findings depend on it alone."""
    pid, source_arg, adjudicate = task
    got = load(pid, source_arg)
    if not got:
        return None
    pm, source = got
    lost = dropped_rules(pid) if source == "nemotron" else 0
    if not pm.rules:  # nothing survived the checks: auditing it would only "find" that nothing grants help
        print(f"{pm.hospital:32} [{source}] not formalised: no rule survived the checks")
        return {"id": pid, "hospital": display_name(pid, pm), "model_source": source,
                "nonprofit": pm.nonprofit, "not_formalised": True, "checks": {}, "proved": 0,
                "counterexamples": 0, "not_stated": 0, "solver_seconds": 0, "doc": pm.doc,
                "hospitals": shared_hospitals(pid),
                "reason": f"none of the model's rules survived the quote and meaning checks ({lost} dropped)"}
    t0 = time.monotonic()
    pair = load(f"{pid}-summary", source_arg)
    findings = audit_document(pid, pm, source, pair[0] if pair else None)
    secs = round(time.monotonic() - t0, 3)
    if adjudicate and source == "nemotron":
        settle(pid, findings, pm)
    doc = {"id": pid, "hospital": display_name(pid, pm), "dropped_rules": lost,
           "tax_status": MANIFEST.get(pid.removesuffix("-summary"), {}).get("tax_status", ""), "source_url": pm.source_url, "nonprofit": pm.nonprofit,
           "model_source": source, "summary_source": pair[1] if pair else None, "solver_seconds": secs,
           "model": json.loads(pm.model_dump_json()), "findings": findings}
    (OUT / f"{pid}.json").write_text(json.dumps(doc, indent=1))
    count = lambda st: sum(f["status"] == st for f in findings)  # noqa: E731
    rank = {"counterexample": 0, "inconclusive": 1, "unknown": 1, "not_stated": 2, "proved": 3, "refuted": 3,
            "n/a": 4}
    checks: dict = {}
    for f in findings:  # worst verdict per check
        if f["check"] not in checks or rank[f["status"]] < rank[checks[f["check"]]]:
            checks[f["check"]] = f["status"]
    conf = {f["check"]: f.get("confidence") for f in findings if f.get("confidence")}
    row = {"id": pid, "hospital": display_name(pid, pm), "model_source": source, "nonprofit": pm.nonprofit,
           "checks": checks, "inconclusive": count("inconclusive"), "confidence": conf,
           "proved": count("proved"), "counterexamples": count("counterexample"),
           "confirmed_counterexamples": sum(f["status"] == "counterexample" and
                                             f.get("confidence") in ("confirmed", "adjudicated")
                                             for f in findings),
           "refuted": count("refuted"),
           "reviewed": sum(1 for f in findings if f.get("review")),
           "disputed": sum(f.get("confidence") == "disputed" for f in findings),
           "doc": pm.doc, "hospitals": shared_hospitals(pid),
           "not_stated": count("not_stated"), "solver_seconds": secs,
           "headline": next((f["summary"] for f in findings if f["status"] == "counterexample"), "")}
    print(f"{pm.hospital:32} [{source}] proved {row['proved']:2}  counterexamples "
          f"{row['counterexamples']:2}  not stated {row['not_stated']:2}  ({secs}s)")
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["auto", "hand", "nemotron"], default="auto")
    ap.add_argument("--formalize", nargs="*", default=[])
    ap.add_argument("--super-runs", type=int, default=0,
                    help="make sure each policy has this many independent Super models (run 1 is the pipeline's)")
    ap.add_argument("--adjudicate", action="store_true",
                    help="Nemotron 3 Ultra settles disputed / inconclusive counterexamples (costs a little)")
    args = ap.parse_args()
    todo = args.formalize
    if todo == ["all"]:  # one run per distinct document: hospitals in a system often share one policy
        seen, todo = set(), []
        for meta in sorted(CORPUS.glob("*.json")):
            url = json.loads(meta.read_text()).get("url")
            if url not in seen and (CORPUS / f"{meta.stem}.txt").exists():
                seen.add(url)
                todo.append(meta.stem)
    with ProcessPoolExecutor(4) as pool:
        for pid, exc in zip(todo, pool.map(_safe_formalize, todo)):
            if exc:
                print(f"{pid}: not formalised ({exc})")
        runs = [(p.stem, j) for p in sorted(NEMO.glob("*.json")) if not p.stem.endswith("-summary")
                for j in range(2, args.super_runs + 1)]
        for err in pool.map(super_run, runs):
            if err:
                print(err)

    ids = sorted({p.stem for p in HAND.glob("*.json")} | {p.stem for p in NEMO.glob("*.json")})
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("*.json"):  # no stale audits from an earlier run
        old.unlink()
    with ProcessPoolExecutor(4, max_tasks_per_child=1) as pool:  # a fresh interpreter per document
        tasks = [(i, args.source, args.adjudicate) for i in ids if not i.endswith("-summary")]
        index = [row for row in pool.map(audit_one, tasks) if row]
    titles = {}
    for pid in [p["id"] for p in index if not p.get("not_formalised")]:
        for f in json.loads((OUT / f"{pid}.json").read_text())["findings"]:
            titles.setdefault(f["check"], f["title"] if f["check"] != "summary_matches_policy"
                              else "Plain-language summary matches the policy")
    (OUT / "index.json").write_text(json.dumps({"made": time.strftime("%Y-%m-%d"), "checks": titles,
                                                "featured": featured(index), "policies": index}, indent=1))


if __name__ == "__main__":
    main()
