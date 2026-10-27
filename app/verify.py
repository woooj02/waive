"""Prove every compiled rule against the policy's own words.

A rule survives only if its quote appears in the policy text. Matching ignores
case, whitespace, hyphenation at line breaks, curly quotes and bullet glyphs,
because PDF text extraction mangles all of those. Anything else is a miss.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from .schema import RuleProgram

_SUBS = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
         " ": " ", "•": " ", "": " ", "­": ""}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    for a, b in _SUBS.items():
        text = text.replace(a, b)
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)  # re-join words hyphenated across lines
    text = re.sub(r"\s+", " ", text)
    return text.lower().strip()


def found(quote: str, source_norm: str, min_ratio: float = 0.92) -> bool:
    """Exact match after normalising, or a near-exact one for PDF glitches in long quotes."""
    q = normalize(quote).strip(" .\"'")
    if len(q) < 12:  # too short to prove anything ("100%", "yes")
        return False
    if q in source_norm:
        return True
    if len(q) < 40:
        return False
    # Slide a window anchored on the quote's first words; tolerate a few mangled characters.
    head = q[:20]
    for m in re.finditer(re.escape(head[:12]), source_norm):
        window = source_norm[m.start(): m.start() + len(q) + 20]
        if SequenceMatcher(None, q, window[: len(q)]).ratio() >= min_ratio:
            return True
    return False


def verify(program: RuleProgram, source_text: str) -> RuleProgram:
    """Mark each quoted rule verified or not; unproven rules lose their effect and are listed."""
    src = normalize(source_text)
    p = program.model_copy(deep=True)
    dropped: list[dict] = []

    def check(name: str, rule) -> bool:
        ok = bool(rule.quote) and found(rule.quote, src)
        rule.verified = ok
        if rule.quote and not ok:
            dropped.append({"rule": name, "quote": rule.quote})
        return ok

    for t in p.tiers:
        check(f"tier {t.min_fpl:g}-{t.max_fpl:g}% FPL", t)
    for pr in p.presumptive:
        check(f"presumptive {pr.program}", pr)
    for name in ("asset_test", "residency", "hardship", "insured", "providers", "application"):
        check(name.replace("_", " "), getattr(p, name))
    p.dropped = dropped
    return p
