"""The Colorado Policy Atlas: compiled policies on disk, and hospital-name matching.

atlas/<slug>.json holds one policy: the verified rule program, the cross-check
report, the source URL and fetch date, the hospitals it covers, and the cost of
compiling it. Policy texts live in data/corpus/ (not in the repo).
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path

from .schema import RuleProgram

ROOT = Path(__file__).resolve().parent.parent
ATLAS = ROOT / "atlas"
CORPUS = ROOT / "data" / "corpus"

_STOP = {"the", "of", "hospital", "medical", "center", "health", "regional", "memorial", "and", "colorado", "co",
         "inc", "llc", "system", "campus"}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80]


def _tokens(name: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9']+", name.lower()) if t not in _STOP}


def load_all() -> list[dict]:
    if not ATLAS.exists():
        return []
    out = []
    for p in sorted(ATLAS.glob("*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except ValueError:
            continue
    return out


def get(policy_id: str) -> dict | None:
    p = ATLAS / f"{slug(policy_id)}.json"
    return json.loads(p.read_text()) if p.exists() else None


def save(entry: dict) -> None:
    ATLAS.mkdir(exist_ok=True)
    (ATLAS / f"{slug(entry['id'])}.json").write_text(json.dumps(entry, indent=1, ensure_ascii=False))


def program(entry: dict) -> RuleProgram:
    return RuleProgram.model_validate(entry["program"])


def match(hospital: str, min_score: float = 0.6) -> tuple[dict | None, float, str]:
    """Best Atlas entry for a hospital name on a bill. Returns (entry, score, matched name)."""
    want = _tokens(hospital)
    best: tuple[dict | None, float, str] = (None, 0.0, "")
    for e in load_all():
        for name in [e["hospital"], *e.get("hospitals", []), *e.get("aliases", [])]:
            have = _tokens(name)
            overlap = len(want & have) / max(1, len(want | have))
            ratio = SequenceMatcher(None, hospital.lower(), name.lower()).ratio()
            score = max(overlap, ratio)
            if score > best[1]:
                best = (e, score, name)
    return best if best[1] >= min_score else (None, best[1], best[2])
