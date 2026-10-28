"""Second pass for hospitals whose document is a web page, a summary or too short: look for the full policy PDF.

    ./venv/bin/python scripts/upgrade_docs.py            # every weak document
    ./venv/bin/python scripts/upgrade_docs.py --ids banner healthone

A replacement must be a PDF on the hospital's own site, read like a policy, and be longer than what we have. Every swap is printed and recorded in the corpus metadata.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import finder  # noqa: E402
from app.fetch import FetchError, fetch  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "data" / "corpus"
MANIFEST = {e["id"]: e for e in json.loads((ROOT / "atlas-manifest.json").read_text())}
SUMMARY = re.compile(r"summary|plain[-_ ]?language|brochure|guidelines|getpage|location|enrollment|cobijo", re.I)


def weak(meta: dict, text: str) -> bool:
    return meta.get("kind") == "html" or bool(SUMMARY.search(meta.get("url", ""))) or len(text) < 12_000


def site(url: str) -> str:
    return ".".join(urlparse(url).netloc.lower().split(".")[-2:])


def upgrade(pid: str) -> str:
    meta = json.loads((CORPUS / f"{pid}.json").read_text())
    text = (CORPUS / f"{pid}.txt").read_text()
    entry = MANIFEST.get(pid, {})
    name = entry.get("hospital", pid)
    own = site(meta["url"])
    results = finder.search(f"{name} Colorado financial assistance policy pdf")
    best = None
    for r in results:
        u = r["url"]
        if finder.NOT_SOURCES.search(urlparse(u).netloc + u) or finder.AVOID.search(urlparse(u).path):
            continue
        if site(u) != own:  # only the hospital's own site: a shared word in a domain once matched another hospital
            continue
        try:
            doc = fetch(u)
        except FetchError:
            continue
        if doc["kind"] == "pdf" and finder.looks_like_policy(doc["text"]) and len(doc["text"]) > 1.3 * len(text) \
                and not SUMMARY.search(u) and (best is None or len(doc["text"]) > len(best["text"])):
            best = doc
    if not best:
        return f"{pid}: kept {meta['url']} ({len(text):,} chars)"
    (CORPUS / f"{pid}.txt").write_text(best["text"])
    meta.update({"url": best["url"], "kind": best["kind"], "chars": len(best["text"]),
                 "replaced": meta["url"], "found_via": "tavily second pass (full policy pdf)"})
    (CORPUS / f"{pid}.json").write_text(json.dumps(meta, indent=1))
    return f"{pid}: UPGRADED {len(text):,} → {len(best['text']):,} chars: {best['url']}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", nargs="*")
    args = ap.parse_args()
    ids = args.ids or [p.stem for p in sorted(CORPUS.glob("*.json"))
                       if (CORPUS / f"{p.stem}.txt").exists() and not p.stem.endswith("-summary")
                       and weak(json.loads(p.read_text()), (CORPUS / f"{p.stem}.txt").read_text())]
    for pid in ids:
        print(upgrade(pid), flush=True)


if __name__ == "__main__":
    main()
