"""Download a policy document and turn it into plain text.

Hospitals publish policies as PDFs, HTML pages, or HTML pages linking to PDFs.
Text keeps page markers ("[page 3]") so a quote can be traced back to its page.
"""

from __future__ import annotations

import io
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
MAX_BYTES = 15_000_000
POLICY_HINT = re.compile(r"financial[-_ ]?assistance|charity|fap\b|plain[-_ ]?language|discounted[-_ ]?care", re.I)


class FetchError(Exception):
    pass


def get(url: str, timeout: float = 30) -> httpx.Response:
    try:
        r = httpx.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=timeout)
    except httpx.HTTPError as exc:
        raise FetchError(f"{type(exc).__name__} fetching {url}") from exc
    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code} for {url}")
    if len(r.content) > MAX_BYTES:
        raise FetchError(f"{url} is larger than {MAX_BYTES // 1_000_000} MB")
    return r


def pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [p.extract_text() or "" for p in reader.pages]
    except Exception as exc:  # pypdf raises many types on broken files
        raise FetchError(f"unreadable PDF: {type(exc).__name__}") from exc
    text = "\n".join(f"[page {i}]\n{t}" for i, t in enumerate(pages, 1))
    if len(text.strip()) < 200:
        raise FetchError("PDF has no text layer (scanned image)")
    return text


def html_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "svg"]):
        tag.decompose()
    main = soup.find("main") or soup.body or soup
    lines = (ln.strip() for ln in main.get_text("\n").splitlines())
    return "\n".join(ln for ln in lines if ln)


def policy_links(html: str, base: str) -> list[str]:
    """PDF links on a hospital page that look like financial-assistance documents."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        label = f"{a.get_text(' ', strip=True)} {href}"
        if urlparse(href).path.lower().endswith((".pdf", ".ashx")) and POLICY_HINT.search(label):
            if href not in out:
                out.append(href)
    return out


def is_pdf(r: httpx.Response) -> bool:
    return "pdf" in r.headers.get("content-type", "") or r.content[:5] == b"%PDF-"


def fetch(url: str) -> dict:
    """Return {url, kind, text, links}. links: policy PDFs found on an HTML page."""
    r = get(url)
    if is_pdf(r):
        return {"url": str(r.url), "kind": "pdf", "text": pdf_text(r.content), "links": []}
    html = r.text
    return {"url": str(r.url), "kind": "html", "text": html_text(html), "links": policy_links(html, str(r.url))}
