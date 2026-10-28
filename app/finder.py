"""Find a hospital's own financial-assistance policy on the web.

Tavily searches; Nemotron Nano picks the official document among the results;
code follows the hospital page to the policy PDF and extracts its text.
Aggregators and news sites are never used as a policy source.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from . import llm
from .fetch import FetchError, fetch

NOT_SOURCES = re.compile(r"careroute|mycarecost|needhelppayingbills|kff|cpr\.org|coloradosun|news|reddit|yelp|"
                         r"wikipedia|facebook|linkedin|propublica|lowninstitute|dollarfor|scribd|billfairly|cobijohealth|"
                         r"rocketreach|hcpf\.colorado\.gov|\.gov/", re.I)
WANT = re.compile(r"financial[-_ ]?assistance[-_ ]?polic|charity[-_ ]?care[-_ ]?polic|\bfap\b", re.I)
AVOID = re.compile(r"spanish|espa[nñ]ol|-spa[-.]|rights|opt[-_ ]?out|application|form|billing[-_ ]?and[-_ ]?collect|"
                   r"vietnam|chinese|korean|russian|arabic|french|amharic|somali", re.I)

PICK = """A patient needs the official financial assistance (charity care) policy of this hospital:
{hospital}, {city}, {state}.
Search results:
{results}
Which result is most likely the hospital's own official policy document, or the hospital's own page that links to
it? Prefer the full policy over a summary, English over other languages, and the hospital's own domain over any
third party. Reply JSON {{"index": <number or null>, "reason": "<short>"}}."""


class NotFound(Exception):
    pass


def tavily_configured() -> bool:
    return bool(os.environ.get("TAVILY_API_KEY"))


def search(query: str, max_results: int = 8) -> list[dict]:
    from tavily import TavilyClient

    res = TavilyClient(api_key=os.environ["TAVILY_API_KEY"]).search(query, max_results=max_results,
                                                                    search_depth="basic")
    return [{"url": r["url"], "title": r.get("title", ""), "snippet": (r.get("content") or "")[:300]}
            for r in res.get("results", [])]


def rank_links(links: list[str]) -> list[str]:
    """Policy PDFs first, English full policy before summaries, forms and other languages."""
    def score(u: str) -> int:
        name = urlparse(u).path.lower()
        return (2 if WANT.search(name) else 0) - (3 if AVOID.search(name) else 0) + (1 if "polic" in name else 0)
    return sorted(links, key=score, reverse=True)


def find_policy(hospital: str, city: str = "", state: str = "Colorado", meter: llm.Meter | None = None,
                trace: list | None = None) -> dict:
    """Return {url, kind, text, found_via}. Raises NotFound with the reason."""
    meter = meter if meter is not None else llm.Meter()
    trace = trace if trace is not None else []
    query = f"{hospital} {city} {state} financial assistance policy".replace("  ", " ")
    results = [r for r in search(query) if not NOT_SOURCES.search(urlparse(r["url"]).netloc + r["url"])]
    trace.append({"step": "tavily search", "query": query, "results": [r["url"] for r in results]})
    if not results:
        raise NotFound("Tavily returned no hospital-owned pages")

    listing = "\n".join(f"{i}. {r['title']} | {r['url']} | {r['snippet'][:160]}" for i, r in enumerate(results))
    pick = llm.chat_json("pick policy (Nemotron 3 Nano)", "nano",
                         [{"role": "user", "content": PICK.format(hospital=hospital, city=city, state=state,
                                                                  results=listing)}],
                         meter, max_tokens=200)
    order = list(range(len(results)))
    if isinstance(pick.get("index"), int) and 0 <= pick["index"] < len(results):
        order.remove(pick["index"])
        order.insert(0, pick["index"])
        # Fall back only to pages on the picked hospital's own site: another system's policy is worse than none.
        site = _site(results[pick["index"]]["url"])
        order = [i for i in order if _site(results[i]["url"]) == site]
    trace.append({"step": "nano pick", "choice": pick})

    errors = []
    for i in order[:4]:
        url = results[i]["url"]
        try:
            doc = fetch(url)
        except FetchError as exc:
            errors.append(str(exc))
            continue
        if doc["kind"] == "html" and doc["links"]:
            for link in rank_links(doc["links"])[:3]:
                try:
                    pdf = fetch(link)
                except FetchError as exc:
                    errors.append(str(exc))
                    continue
                if looks_like_policy(pdf["text"]):
                    pdf["found_via"] = url
                    trace.append({"step": "followed link", "from": url, "to": pdf["url"]})
                    return pdf
        if looks_like_policy(doc["text"]):
            doc["found_via"] = url
            return doc
    raise NotFound("no readable policy among the results" + (f" ({errors[0]})" if errors else ""))


def _site(url: str) -> str:
    return ".".join(urlparse(url).netloc.lower().split(".")[-2:])


def looks_like_policy(text: str) -> bool:
    t = text.lower()
    return len(t) > 1500 and ("poverty" in t or "fpl" in t or "fpg" in t) and ("eligib" in t)
