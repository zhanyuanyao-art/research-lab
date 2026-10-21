"""Free PDFs that only the publisher's own site offers (card 0155). Unpaywall and Crossref say nothing about them, but
Crossref's resource URL leads to a page that does: ISEAS (Contemporary Southeast Asia) points every item of an issue at
the issue's page on bookshop.iseas.edu.sg, which marks the free items with a Download button and the rest with Check
Price. A resolver is keyed by the resource URL's host; its parser is a pure function over the page's HTML.

A paper found this way gets is_oa 1, the absolute download URL as oa_pdf_url and oa_license 'publisher', which the
access mark reads as 出版社免费提供.
"""
from __future__ import annotations

import html
import logging
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

log = logging.getLogger("research_lab.feeds.publisher_oa")

LICENSE = "publisher"            # oa_license for a PDF the publisher gives away (partials/ui.html access_mark)
PAGE_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
ISEAS = "bookshop.iseas.edu.sg"

ITEM_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.S | re.I)
ISEAS_TITLE_RE = re.compile(r'<span class="me-2">(.*?)(?:,\s*by(?:&nbsp;|\s)+<a\b|</span>\s*</div>|<br\s*/?>)', re.S | re.I)
ISEAS_PERSON_RE = re.compile(r",\s*by(?:&nbsp;|\s)+<a\b[^>]*>(.*?)</a>", re.S | re.I)
ISEAS_DOWNLOAD_RE = re.compile(r'<a\b[^>]*class="[^"]*x-btn-download[^"]*"[^>]*href="([^"]+)"', re.S | re.I)


@dataclass(frozen=True)
class Entry:
    """One item listed on a publisher's page: its normalised title, its author or reviewer, and the PDF when free."""
    title: str
    person: str = ""
    pdf: str | None = None


def norm_title(text: str | None) -> str:
    """A title for matching: no tags, entities, numbering, 'Book review:' or punctuation; quotes of any kind (and the
    U+FFFD some publishers deposit for them) ignored; lower case."""
    t = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    t = unicodedata.normalize("NFKC", t).replace("�", "'")
    t = re.sub(r"^\s*\d{1,2}\.\s+", "", t)
    t = re.sub(r"^\s*book\s+reviews?\s*[:.]\s*", "", t, flags=re.I)
    t = re.sub(r"['’‘`´\"“”]", "", t)
    t = re.sub(r"[^\w]+", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def _name_words(name: str | None) -> frozenset[str]:
    return frozenset(w for w in norm_title(name).split() if len(w) > 1)


def safe_pdf_url(href: str | None, host: str) -> str | None:
    """The download link as an absolute https URL on the publisher's own host, or None. A relative path is resolved
    against that origin; javascript:, data:, http:, another host or a protocol-relative //elsewhere never pass, so
    nothing but the publisher's file can end up behind a Download link."""
    from . import net
    raw = html.unescape(href or "").strip()
    if not raw or raw.startswith("//"):
        return None
    url = urljoin(f"https://{host}/", raw)
    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() != host or parts.username or parts.password:
        return None
    return url if net.looks_public(url) else None


def parse_iseas(page: str, base_url: str = "") -> list[Entry]:
    """Every item an ISEAS issue page lists, free or priced, with its author (the reviewer for a review) and, when
    it has a Download button with a safe link, the PDF."""
    out = []
    for block in ITEM_RE.findall(page or ""):
        title = ISEAS_TITLE_RE.search(block)
        if not title:
            continue
        name = norm_title(title.group(1))
        if not name:
            continue
        person = ISEAS_PERSON_RE.search(block)
        link = ISEAS_DOWNLOAD_RE.search(block)
        out.append(Entry(name, html.unescape(re.sub(r"<[^>]+>", "", person.group(1))).strip() if person else "",
                         safe_pdf_url(link.group(1), ISEAS) if link else None))
    return out


RESOLVERS: dict[str, Callable[[str, str], list[Entry]]] = {
    ISEAS: parse_iseas,
}


def host_of(url: str | None) -> str:
    return (urlsplit(url or "").hostname or "").lower()


def handles(url: str | None) -> bool:
    return host_of(url) in RESOLVERS


def match(title: str | None, entries: list[Entry], authors: Iterable[str] = ()) -> str | None:
    """The PDF for a feed item, only when its full normalised title names exactly one entry on the page, the author
    deciding between entries that share a title (two reviews of one book), and that entry is free. A prefix is never
    enough: 「… Rivalry」 must not take the PDF of 「… Rivalry: A Rejoinder」."""
    t = norm_title(title)
    if not t:
        return None
    same = [e for e in entries if e.title == t]
    if len(same) > 1:
        names = [_name_words(a) for a in authors]
        same = [e for e in same if (w := _name_words(e.person)) and any(w <= n or n <= w for n in names if n)]
    return same[0].pdf if len(same) == 1 else None


def fetch_page(url: str, http: httpx.Client | None = None) -> str | None:
    """The page's HTML, or None for a non-public address, an error or anything but HTML."""
    from . import net
    if not net.public_url(url):
        return None
    client = net.guard_client(http) if http is not None else net.new_client(timeout=PAGE_TIMEOUT)
    try:
        resp = client.get(url, headers={"User-Agent": net.BROWSER_UA, "Accept": "text/html"})
        if resp.status_code != 200 or "html" not in resp.headers.get("content-type", "html").lower():
            return None
        return resp.text
    except httpx.HTTPError as e:
        log.info("publisher page %s not readable: %s", url, e)
        return None
    finally:
        if http is None:
            client.close()


def resolve(rows: Iterable[dict[str, Any]], http: httpx.Client | None = None) -> int:
    """Fill is_oa / oa_pdf_url / oa_license on the rows whose resource URL a resolver knows and whose title the
    publisher's page offers for free. Each page is fetched once. Rows carry 'resource_url', 'title' and, to tell
    same-titled entries apart, 'authors'. Returns how many rows got a PDF."""
    pages: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r.get("oa_pdf_url") or not handles(r.get("resource_url")):
            continue
        pages.setdefault(r["resource_url"], []).append(r)
    found = 0
    for url, group in pages.items():
        page = fetch_page(url, http)
        if not page:
            continue
        entries = RESOLVERS[host_of(url)](page, url)
        for r in group:
            pdf = match(r.get("title"), entries, r.get("authors") or ())
            if pdf:
                r.update(is_oa=1, oa_pdf_url=pdf, oa_license=LICENSE)
                found += 1
    return found
