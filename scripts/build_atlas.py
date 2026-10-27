"""Build the Colorado Policy Atlas: find each policy, keep its text, compile it.

    ./venv/bin/python scripts/build_atlas.py --fetch-only          # just collect texts
    ./venv/bin/python scripts/build_atlas.py --ids uchealth banner # compile some
    ./venv/bin/python scripts/build_atlas.py                       # everything not yet compiled

Policy texts go to data/corpus/ (hospital documents: not redistributed in the repo).
Compiled programs go to atlas/ (rules + short quotes + source links).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import atlas, compiler, finder, llm  # noqa: E402
from app.fetch import FetchError, fetch  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / "atlas-manifest.json").read_text())


def collect(m: dict) -> dict | None:
    meta_path = atlas.CORPUS / f"{m['id']}.json"
    if meta_path.exists():
        return json.loads(meta_path.read_text())
    doc = None
    trace: list = []
    try:
        if m.get("seed"):
            d = fetch(m["seed"])
            if d["kind"] == "pdf" and finder.looks_like_policy(d["text"]):
                doc = d
            for link in finder.rank_links(d.get("links", []))[:4]:
                if doc:
                    break
                try:
                    pdf = fetch(link)
                except FetchError:
                    continue
                if finder.looks_like_policy(pdf["text"]):
                    doc = pdf | {"found_via": m["seed"]}
            if not doc and finder.looks_like_policy(d["text"]):
                doc = d
        if not doc and not finder.tavily_configured():
            print(f"  … {m['id']}: needs Tavily (TAVILY_API_KEY not set)")
            return None
        if not doc:
            doc = finder.find_policy(m["hospital"], m.get("city", ""), "Colorado", llm.Meter(), trace)
    except (FetchError, finder.NotFound, llm.LLMError) as exc:
        print(f"  ✗ {m['id']}: {exc}")
        return None
    atlas.CORPUS.mkdir(parents=True, exist_ok=True)
    (atlas.CORPUS / f"{m['id']}.txt").write_text(doc["text"])
    meta = {"id": m["id"], "url": doc["url"], "kind": doc["kind"], "found_via": doc.get("found_via", ""),
            "fetched": date.today().isoformat(), "chars": len(doc["text"]), "trace": trace}
    meta_path.write_text(json.dumps(meta, indent=1))
    print(f"  ✓ {m['id']}: {doc['url']} ({len(doc['text']):,} chars)")
    return meta


def build(m: dict, meta: dict, cross_check: bool) -> None:
    text = (atlas.CORPUS / f"{m['id']}.txt").read_text()
    meter = llm.Meter()
    program, report = compiler.compile_policy(text, meter, meta["url"], cross_check=cross_check)
    program.hospital = program.hospital or m["hospital"]
    entry = {k: m.get(k) for k in ("id", "hospital", "system", "hospitals", "city", "nonprofit")} | {
        "policy_name": program.hospital, "source_url": meta["url"], "fetched": meta["fetched"],
        "compiled": date.today().isoformat(), "program": program.model_dump(), "report": report,
        "calls": meter.as_dicts(), "cost_usd": meter.cost_usd}
    atlas.save(entry)
    tiers = [(t.max_fpl, t.discount_pct, t.verified) for t in program.tiers]
    print(f"  ⚙ {m['id']}: tiers {tiers}, dropped {len(program.dropped)}, "
          f"disagreements {list(report.get('disagreements', {}))}, ${meter.cost_usd:.4f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--fetch-only", action="store_true")
    ap.add_argument("--recompile", action="store_true")
    ap.add_argument("--no-cross-check", action="store_true")
    args = ap.parse_args()
    for m in MANIFEST:
        if args.ids and m["id"] not in args.ids:
            continue
        meta = collect(m)
        if not meta or args.fetch_only:
            continue
        if atlas.get(m["id"]) and not args.recompile:
            continue
        try:
            build(m, meta, cross_check=not args.no_cross_check)
        except llm.BudgetExceeded as exc:
            print(f"stopping: {exc}")
            return
        except (llm.LLMError, ValueError) as exc:
            print(f"  ✗ compile {m['id']}: {str(exc)[:200]}")
    print(f"spent today: ${llm.spent_today():.4f}")


if __name__ == "__main__":
    main()
