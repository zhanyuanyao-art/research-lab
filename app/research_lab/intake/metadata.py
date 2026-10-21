"""Crossref and OpenAlex lookups: polite, throttled, and cached in http_cache.

Auto-accept (metadata_status='verified') needs token_set_ratio(title) >= 92 and the year within
±1 of a year printed in the PDF; a DOI the user typed counts as verified once it resolves.
Everything else is stored as candidates for the user to pick (method-layer rule 1).
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import quote

import httpx
from rapidfuzz import fuzz

from ..config import APP_VERSION, env
from ..db.connection import now_iso
from .dedupe import family_name, normalize_title

CROSSREF = "https://api.crossref.org"
OPENALEX = "https://api.openalex.org"
SEARCH_SELECT = "DOI,title,subtitle,author,issued,container-title,score,type,publisher"
MATCH_THRESHOLD = 92
MIN_INTERVAL = {"api.crossref.org": 1 / 3, "api.openalex.org": 0.1}
CACHEABLE = (200, 404)

CROSSREF_KIND = {"journal-article": "article", "proceedings-article": "article", "posted-content": "article",
                 "book": "book", "monograph": "book", "edited-book": "book", "reference-book": "book",
                 "book-chapter": "chapter", "book-section": "chapter", "book-part": "chapter",
                 "report": "report", "report-component": "report", "dissertation": "other"}
OPENALEX_KIND = {"article": "article", "review": "article", "preprint": "article", "book": "book",
                 "book-chapter": "chapter", "report": "report"}

_throttle_lock = threading.Lock()
_last_call: dict[str, float] = {}


@dataclass
class Candidate:
    source: str
    doi: str | None
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    kind: str | None = None
    match: float = 0.0            # token_set_ratio against the PDF title guess
    source_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Candidate":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Clues:
    title_guess: str | None = None
    author_guess: str | None = None
    dois: list[str] = field(default_factory=list)
    years: list[int] = field(default_factory=list)
    head_text: str = ""
    user_doi: str | None = None


@dataclass
class Resolution:
    status: str                              # verified | unverified
    chosen: Candidate | None = None
    candidates: list[Candidate] = field(default_factory=list)
    crossref: dict | None = None
    openalex: dict | None = None
    abstract: str | None = None
    openalex_id: str | None = None
    errors: list[str] = field(default_factory=list)
    query: str | None = None


def strip_tags(text: str | None) -> str | None:
    if not text:
        return None
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip() or None


def abstract_from_inverted(index: dict[str, list[int]] | None) -> str | None:
    if not index:
        return None
    slots: dict[int, str] = {}
    for word, positions in index.items():
        for pos in positions:
            slots[pos] = word
    return " ".join(slots[i] for i in sorted(slots)) or None


def _first(xs: Any) -> Any:
    return xs[0] if isinstance(xs, list) and xs else None


def _crossref_year(msg: dict) -> int | None:
    for key in ("issued", "published-print", "published-online", "published"):
        parts = (msg.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            try:
                return int(parts[0][0])
            except (TypeError, ValueError):
                continue
    return None


def from_crossref(msg: dict) -> Candidate:
    title = _first(msg.get("title")) or ""
    sub = _first(msg.get("subtitle"))
    if sub and normalize_title(sub) not in normalize_title(title):
        title = f"{title.rstrip(':')}: {sub}"
    authors = []
    for a in msg.get("author") or []:
        if a.get("family"):
            authors.append(f"{a['family']}, {a['given']}" if a.get("given") else a["family"])
        elif a.get("name"):
            authors.append(a["name"])
    kind = CROSSREF_KIND.get(msg.get("type") or "", "other" if msg.get("type") else None)
    venue = _first(msg.get("container-title")) or (msg.get("publisher") if kind == "book" else None)
    return Candidate("crossref", (msg.get("DOI") or "").lower() or None, re.sub(r"\s+", " ", title).strip(),
                     authors, _crossref_year(msg), venue, kind, 0.0, msg.get("score"))


def _openalex_author(name: str) -> str:
    parts = name.split()
    return f"{parts[-1]}, {' '.join(parts[:-1])}" if len(parts) > 1 else name


def from_openalex(work: dict) -> Candidate:
    doi = (work.get("doi") or "").lower().replace("https://doi.org/", "") or None
    authors = [_openalex_author(a["author"]["display_name"]) for a in work.get("authorships") or []
               if (a.get("author") or {}).get("display_name")]
    venue = (((work.get("primary_location") or {}).get("source")) or {}).get("display_name")
    kind = OPENALEX_KIND.get(work.get("type") or "", "other" if work.get("type") else None)
    return Candidate("openalex", doi, work.get("display_name") or work.get("title") or "", authors,
                     work.get("publication_year"), venue, kind)


def title_match(candidate_title: str, guess: str | None) -> float:
    if not guess or not candidate_title:
        return 0.0
    return float(fuzz.token_set_ratio(normalize_title(candidate_title), normalize_title(guess)))


def head_match(candidate_title: str, head_text: str) -> float:
    """How completely the candidate title appears on the first pages (used for DOIs found in the PDF)."""
    norm = normalize_title(candidate_title)
    if len(norm.split()) < 3 or not head_text:
        return 0.0
    return float(fuzz.token_set_ratio(norm, normalize_title(head_text[:4000])))


def year_ok(year: int | None, years: list[int], *, lenient: bool) -> bool:
    if year is None:
        return lenient and not years
    if not years:
        return lenient
    return any(abs(year - y) <= 1 for y in years)


class MetadataClient:
    """One lookup session. Pass `http` to inject a client (tests use httpx.MockTransport)."""

    def __init__(self, conn: sqlite3.Connection, http: httpx.Client | None = None, *, refresh: bool = False):
        from ..feeds import net   # inside the function: feeds.net imports this module at load time

        self.conn = conn
        self.refresh = refresh
        self.contact = env("CONTACT_EMAIL")
        self._own = http is None
        ua = f"research-lab/{APP_VERSION}" + (f" (mailto:{self.contact})" if self.contact else "")
        self.http = net.guard_client(http) if http is not None else net.new_client(timeout=15.0)
        self.headers = {"User-Agent": ua, "Accept": "application/json"}
        self.errors: list[str] = []

    def close(self) -> None:
        if self._own:
            self.http.close()

    def __enter__(self) -> "MetadataClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _throttle(self, host: str) -> None:
        gap = MIN_INTERVAL.get(host, 0.0)
        with _throttle_lock:
            wait = _last_call.get(host, 0.0) + gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_call[host] = time.monotonic()

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> tuple[int | None, Any]:
        from ..feeds import net   # inside the function: feeds.net imports this module at load time

        key = str(httpx.URL(url, params=params or {}))
        if not self.refresh:
            row = self.conn.execute("SELECT status, body FROM http_cache WHERE url = ?", (key,)).fetchone()
            if row and row["status"] in CACHEABLE:
                return row["status"], self._parse(row["body"]) if row["status"] == 200 else None
        send = dict(params or {})
        if self.contact:
            send["mailto"] = self.contact
        req_url = httpx.URL(url)
        self._throttle(req_url.host)
        try:
            resp = self.http.get(url, params=send, headers=self.headers)
        except httpx.HTTPError as e:
            self.errors.append(f"{req_url.host}: {net.error_reason(e)}")
            return None, None
        body = resp.text
        self.conn.execute(
            "INSERT INTO http_cache(url, fetched_at, status, body) VALUES (?,?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET fetched_at=excluded.fetched_at, status=excluded.status, body=excluded.body",
            (key, now_iso(), resp.status_code, body if resp.status_code == 200 else body[:2000]))
        if resp.status_code != 200:
            if resp.status_code != 404:
                self.errors.append(f"{req_url.host}: HTTP {resp.status_code}")
            return resp.status_code, None
        data = self._parse(body)
        if data is None:
            self.errors.append(f"{req_url.host}: invalid JSON")
        return resp.status_code, data

    @staticmethod
    def _parse(body: str | None) -> Any:
        try:
            return json.loads(body or "")
        except ValueError:
            return None

    def crossref_work(self, doi: str) -> dict | None:
        _, data = self.get_json(f"{CROSSREF}/works/{quote(doi, safe='/:;()._-')}")
        msg = (data or {}).get("message") if isinstance(data, dict) else None
        return msg if isinstance(msg, dict) and msg.get("title") is not None else None

    def crossref_search(self, query: str, rows: int = 5) -> list[dict]:
        _, data = self.get_json(f"{CROSSREF}/works", {"query.bibliographic": query, "rows": rows, "select": SEARCH_SELECT})
        items = ((data or {}).get("message") or {}).get("items") if isinstance(data, dict) else None
        return [i for i in items or [] if isinstance(i, dict)]

    def openalex_work(self, doi: str) -> dict | None:
        _, data = self.get_json(f"{OPENALEX}/works/doi:{quote(doi, safe='/:;()._-')}")
        return data if isinstance(data, dict) and data.get("id") else None


def _enrich(client: MetadataClient, res: Resolution, crossref_msg: dict | None) -> Resolution:
    """Fill abstract and OpenAlex id for the chosen DOI."""
    chosen = res.chosen
    if chosen is None:
        return res
    if crossref_msg is None and chosen.doi and chosen.source != "openalex":
        crossref_msg = client.crossref_work(chosen.doi)
    res.crossref = crossref_msg
    oa = client.openalex_work(chosen.doi) if chosen.doi else None
    res.openalex = oa
    res.openalex_id = (oa.get("id") or "").rsplit("/", 1)[-1] if oa else None
    res.abstract = strip_tags((crossref_msg or {}).get("abstract")) or abstract_from_inverted((oa or {}).get("abstract_inverted_index"))
    return res


def resolve(conn: sqlite3.Connection, clues: Clues, *, http: httpx.Client | None = None, refresh: bool = False) -> Resolution:
    with MetadataClient(conn, http, refresh=refresh) as client:
        res = _resolve(client, clues)
        res.errors = client.errors
        return res


def _resolve(client: MetadataClient, clues: Clues) -> Resolution:
    candidates: list[Candidate] = []

    if clues.user_doi:
        msg = client.crossref_work(clues.user_doi)
        cand = from_crossref(msg) if msg else None
        if cand is None:
            oa = client.openalex_work(clues.user_doi)
            cand = from_openalex(oa) if oa else None
        if cand is not None:
            cand.match = max(title_match(cand.title, clues.title_guess), head_match(cand.title, clues.head_text))
            return _enrich(client, Resolution("verified", cand, [cand]), msg)

    for doi in [d for d in clues.dois if d != clues.user_doi][:3]:
        msg = client.crossref_work(doi)
        cand = from_crossref(msg) if msg else None
        if cand is None:
            oa = client.openalex_work(doi)
            cand = from_openalex(oa) if oa else None
            msg = None
        if cand is None:
            continue
        cand.doi = cand.doi or doi
        cand.match = max(title_match(cand.title, clues.title_guess), head_match(cand.title, clues.head_text))
        if cand.match >= MATCH_THRESHOLD and year_ok(cand.year, clues.years, lenient=True):
            return _enrich(client, Resolution("verified", cand, [cand]), msg)
        candidates.append(cand)

    query = None
    if clues.title_guess:
        fam = family_name(clues.author_guess.split(";")[0]) if clues.author_guess else ""
        query = f"{clues.title_guess} {fam}".strip()
        for item in client.crossref_search(query):
            cand = from_crossref(item)
            if not cand.title or any(c.doi and c.doi == cand.doi for c in candidates):
                continue
            cand.match = title_match(cand.title, clues.title_guess)
            candidates.append(cand)
        best = max((c for c in candidates if c.source == "crossref"), key=lambda c: c.match, default=None)
        if best and best.match >= MATCH_THRESHOLD and year_ok(best.year, clues.years, lenient=False):
            return _enrich(client, Resolution("verified", best, candidates, query=query), None)

    candidates.sort(key=lambda c: c.match, reverse=True)
    return Resolution("unverified", None, candidates[:6], query=query)


def fetch_for_doi(conn: sqlite3.Connection, doi: str, *, http: httpx.Client | None = None, refresh: bool = False) -> Resolution:
    """Resolve one DOI the user chose or typed; verified when Crossref or OpenAlex knows it."""
    return resolve(conn, Clues(user_doi=doi.lower().strip()), http=http, refresh=refresh)
