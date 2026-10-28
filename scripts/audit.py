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
import re
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


def second(pid: str) -> PolicyModel | None:
    p = NEMO / f"{pid}.json"
    if p.exists():
        sm = json.loads(p.read_text())["report"].get("secondary_model")
        return PolicyModel(**sm) if sm else None
    return None


def verdicts(findings) -> dict:
    out: dict = {}
    for f in findings:
        out.setdefault(f.check, set()).add(f.status)
    return out


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


def dropped_rules(pid: str) -> int:
    rep = json.loads((NEMO / f"{pid}.json").read_text())["report"]["primary"]
    return sum(1 for d in rep.get("dropped", []) if d["kind"] in ("rule", "unsupported")
               and (d["kind"] == "rule" or str(d.get("id", "")).startswith("rule")))


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["auto", "hand", "nemotron"], default="auto")
    ap.add_argument("--formalize", nargs="*", default=[])
    args = ap.parse_args()
    todo = args.formalize
    if todo == ["all"]:  # one run per distinct document: hospitals in a system often share one policy
        seen, todo = set(), []
        for meta in sorted(CORPUS.glob("*.json")):
            url = json.loads(meta.read_text()).get("url")
            if url not in seen and (CORPUS / f"{meta.stem}.txt").exists():
                seen.add(url)
                todo.append(meta.stem)
    from concurrent.futures import ProcessPoolExecutor  # Z3 isn't thread-safe: one process per document
    with ProcessPoolExecutor(4) as pool:
        for pid, exc in zip(todo, pool.map(_safe_formalize, todo)):
            if exc:
                print(f"{pid}: not formalised ({exc})")

    ids = sorted({p.stem for p in HAND.glob("*.json")} | {p.stem for p in NEMO.glob("*.json")})
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("*.json"):  # no stale audits from an earlier run
        old.unlink()
    index = []
    for pid in [i for i in ids if not i.endswith("-summary")]:
        got = load(pid, args.source)
        if not got:
            continue
        pm, source = got
        lost = dropped_rules(pid) if source == "nemotron" else 0
        if not pm.rules:  # nothing survived the checks: auditing it would only "find" that nothing grants help
            index.append({"id": pid, "hospital": display_name(pid, pm), "model_source": source,
                          "nonprofit": pm.nonprofit, "not_formalised": True, "checks": {}, "proved": 0,
                          "counterexamples": 0, "not_stated": 0, "solver_seconds": 0, "doc": pm.doc,
                          "hospitals": shared_hospitals(pid),
                          "reason": f"none of the model's rules survived the quote and meaning checks ({lost} dropped)"})
            print(f"{pm.hospital:32} [{source}] not formalised: no rule survived the checks")
            continue
        t0 = time.monotonic()
        primary = audit(pm)
        findings = [f.as_dict() for f in primary]
        other = second(pid) if source == "nemotron" else None
        if other:  # confidence: does Nemotron 3 Super's independent model reach the same verdict?
            theirs = verdicts(audit(other))
            mine = verdicts(primary)
            for f in findings:
                same = mine.get(f["check"]) == theirs.get(f["check"])
                f["confidence"] = "confirmed" if same else "disputed"
                f["second_opinion"] = sorted(theirs.get(f["check"], []))
        pair = load(f"{pid}-summary", args.source)
        if pair:
            findings += [dict(f.as_dict(), check="summary_matches_policy") for f in equivalent(pm, pair[0])]
        if lost:
            for f in findings:
                if f["status"] == "counterexample" and f.get("basis") == "absence":
                    f["status"] = "inconclusive"
                    f["summary"] = (f"Inconclusive: {lost} of the model's rules were dropped by the quote and meaning "
                                    f"checks, so a missing rule may be our error. Z3's case: " + f["summary"])
        secs = round(time.monotonic() - t0, 3)
        doc = {"id": pid, "hospital": display_name(pid, pm), "dropped_rules": lost, "source_url": pm.source_url, "nonprofit": pm.nonprofit,
               "model_source": source, "summary_source": pair[1] if pair else None, "solver_seconds": secs,
               "model": json.loads(pm.model_dump_json()), "findings": findings}
        (OUT / f"{pid}.json").write_text(json.dumps(doc, indent=1))
        count = lambda st: sum(f["status"] == st for f in findings)  # noqa: E731
        rank = {"counterexample": 0, "inconclusive": 1, "unknown": 1, "not_stated": 2, "proved": 3, "n/a": 4}
        checks: dict = {}
        for f in findings:  # worst verdict per check
            if f["check"] not in checks or rank[f["status"]] < rank[checks[f["check"]]]:
                checks[f["check"]] = f["status"]
        conf = {f["check"]: f.get("confidence") for f in findings if f.get("confidence")}
        index.append({"id": pid, "hospital": display_name(pid, pm), "model_source": source, "nonprofit": pm.nonprofit,
                      "checks": checks, "inconclusive": count("inconclusive"), "confidence": conf,
                      "proved": count("proved"), "counterexamples": count("counterexample"),
                      "confirmed_counterexamples": sum(f["status"] == "counterexample" and
                                                       f.get("confidence") == "confirmed" for f in findings),
                      "disputed": sum(f.get("confidence") == "disputed" for f in findings),
                      "doc": pm.doc, "hospitals": shared_hospitals(pid),
                      "not_stated": count("not_stated"), "solver_seconds": secs,
                      "headline": next((f["summary"] for f in findings if f["status"] == "counterexample"), "")})
        print(f"{pm.hospital:32} [{source}] proved {index[-1]['proved']:2}  counterexamples "
              f"{index[-1]['counterexamples']:2}  not stated {index[-1]['not_stated']:2}  ({secs}s)")
    titles = {}
    for pid in [p["id"] for p in index if not p.get("not_formalised")]:
        for f in json.loads((OUT / f"{pid}.json").read_text())["findings"]:
            titles.setdefault(f["check"], f["title"] if f["check"] != "summary_matches_policy"
                              else "Plain-language summary matches the policy")
    (OUT / "index.json").write_text(json.dumps({"made": time.strftime("%Y-%m-%d"), "checks": titles,
                                                "policies": index}, indent=1))


if __name__ == "__main__":
    main()
