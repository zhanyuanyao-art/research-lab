"""New journal articles: Crossref per journal ISSN, abstracts and open-access links from OpenAlex,
an optional Unpaywall lookup, front-matter / correction filtering, book reviews kept apart (card 0154), dedupe by DOI.

One journal failing (timeout, HTTP error, bad JSON) is recorded in the summary and never stops the run.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import sqlite3
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from ..db.connection import now_iso
from ..intake import directions
from ..intake.metadata import CROSSREF, OPENALEX, abstract_from_inverted
from . import net, publisher_oa, sources as sources_mod
from .sources import Journal, Sources

log = logging.getLogger("research_lab.feeds.papers")

UNPAYWALL = "https://api.unpaywall.org/v2"
SELECT = "DOI,title,subtitle,author,created,published-online,published-print,issued,abstract,type,license,page,resource"
ROWS = 80
OPENALEX_BATCH = 40

# Publication furniture, not papers (as the old daily brief filtered them).
FRONT_MATTER = ("cover and front matter", "cover and back matter", "front matter", "back matter", "volume information",
                "issue information", "editorial board", "list of reviewers", "notes on contributors", "books received",
                "thanks to reviewers", "table of contents", "acknowledgement to reviewers", "reviewer acknowledgement",
                "call for papers")
# Prefix match, so "market corrections in …" survives.
NOTICE_PREFIXES = ("correction", "corrigendum", "erratum", "errata", "retraction", "retracted", "expression of concern",
                   "in memoriam", "obituary", "editor's note", "editors' note", "editor’s note",
                   "editors’ note", "editor's introduction", "editor’s introduction")
EDITORIAL_RE = re.compile(r"^editorial\s*($|[:.\-–—])")
# the book-review section's own header or introduction, not a review; IA's 「Contributors」「Abstracts」 and
# 「Books reviewed: September 2026」 lists
SECTION_RE = re.compile(r"^(?:book reviews?(?:\s+(?:editorial|introduction|section|editor'?s? note))?|contributors|abstracts|"
                        r"books reviewed\b.*)\s*$")
ROMAN_PAGES_RE = re.compile(r"^\s*[ivxlcdm]+(?:\s*[-–]\s*[ivxlcdm]+)?\s*$", re.I)   # front matter is numbered v–x
PAGES_RE = re.compile(r"^\s*(\d+)\s*(?:[-–]\s*(\d+))?\s*$")
SHORT_PAGES = 3          # a book review runs one to three pages; IA's articles run 10 to 25
TRAILING_NOTICE_RE = re.compile(r"[–—-]\s*(corrigendum|erratum|correction)\s*$")

# Book reviews (card 0154): kept as their own kind, not dropped. The journals title them four ways:
#   Taylor & Francis  「Title: by Author, City, Press, 2025, 300 pp., $30」 (also ", by" and "edited by")
#   CSA               「BOOK REVIEW: Title. By Author」
#   SEX               「Book review: Title …」
#   CQ                「Title Author. Cambridge, MA: MIT Press, 2025 …」
REVIEW_PREFIX_RE = re.compile(r"^\s*(?:book\s+reviews?|review\s+of)\s*[:.\-–—]?\s*", re.I)
# page counts, bindings, prices, "University Press, 2024"
REVIEWISH_RE = re.compile(r"\b\d+\s?pp\b|\bpp\.\s?\d|hardcover|hardback|paperback|[$£€]\s?\d+(?:\.\d{2})?|university press[,.]? \d{4}",
                          re.I)
# 「: by」「, by」「. By」 first, so a title with "by" in it ("Girt by Sea: …: by Rebecca Strating") keeps it; a bare
# " by " only where nothing punctuated is found (SEX's 「Title by Author」)
BY_RE = re.compile(r"[:,.]\s+(?:edited\s+by|by)\s+", re.I)
BARE_BY_RE = re.compile(r"\s+(?:edited\s+by|by)\s+", re.I)
PAGES_PRICE_RE = re.compile(r"\b\d+\s?pp\b|\bpp\.\s?\d|hardcover|hardback|paperback|[$£€]\s?\d", re.I)
SENTENCE_RE = re.compile(r"(?<=[a-z]{2})\.\s+")      # "Smith. Cambridge", not the "J. " of an initial
# ". Cambridge, MA: MIT Press, 2025" — a city, a colon, a publisher, a year
IMPRINT_RE = re.compile(r"\.\s+([A-Z][^.:]{1,40}):\s*([^,]{2,80}),\s*((?:19|20)\d{2})\b")
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
# the reviewed book's author at the end of 「Title Author」: a given name, maybe a middle name or initials, a family
# name. The title before it is greedy, so the shortest such name wins; a given name is a word, never an initial.
GIVEN = r"[A-Z][a-z'’]+(?:-[A-Z]?[a-z'’]+)*"     # Andrew, You-Tien
NAME = rf"{GIVEN}(?:\s+(?:[A-Z]\.|{GIVEN})){{0,2}}\s+[A-Z][\w'’-]+"
NAME_TAIL_RE = re.compile(rf"^(?P<title>.*\S)\s+(?P<name>{NAME})$")
JOINER_RE = re.compile(r"\s*(?:,|\band\b|&)\s*$")


def _norm(title: str | None) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def page_span(page: str | None) -> int | None:
    """How many pages a Crossref page range covers ("1430-1432" is 3), or None when it is not a plain range."""
    m = PAGES_RE.match(page or "")
    if not m:
        return None
    first, last = int(m.group(1)), int(m.group(2) or m.group(1))
    return last - first + 1 if last >= first else None


def is_junk(title: str | None, page: str | None = None) -> bool:
    """Publication furniture, notices and editorials: never kept. Roman page numbers are front matter too."""
    t = _norm(title)
    if not t or ROMAN_PAGES_RE.match(page or ""):
        return True
    if any(p in t for p in FRONT_MATTER):
        return True
    return bool(t.startswith(NOTICE_PREFIXES) or EDITORIAL_RE.match(t) or TRAILING_NOTICE_RE.search(t) or SECTION_RE.match(t))


def is_book_review(title: str | None) -> bool:
    t = (title or "").strip()
    if not t or is_junk(t):
        return False
    if REVIEW_PREFIX_RE.match(t) or REVIEWISH_RE.search(t) or IMPRINT_RE.search(t):
        return True
    low = _norm(t)
    return ", by " in low or ": by " in low or "edited by" in low


def is_short_review(row: dict[str, Any]) -> bool:
    """A book review titled only by its book (International Affairs and others): no abstract, at most three pages
    and at most two authors, the reviewer. Real articles carry an abstract and run ten pages or more."""
    span = page_span(row.get("page"))
    return not row.get("abstract") and span is not None and span <= SHORT_PAGES and len(row.get("authors") or []) <= 2


def is_non_paper(title: str | None) -> bool:
    """Not a research article: junk, or a book review. history.py's import drops both."""
    return is_junk(title) or is_book_review(title)


def _clip(text: str) -> str:
    return text.strip().strip(".,;:–—- ").strip()


def parse_review(title: str) -> dict[str, str | None]:
    """The reviewed book from a review's title: book_title, book_authors and book_imprint (publisher and year),
    each None when the title does not say. The raw title stays the fallback in the UI."""
    raw = re.sub(r"\s+", " ", (title or "").strip())
    text = REVIEW_PREFIX_RE.sub("", raw, count=1)
    out: dict[str, str | None] = {"book_title": None, "book_authors": None, "book_imprint": None}
    imprint = IMPRINT_RE.search(text)
    if imprint:                                   # CQ and CSA: ". City: Press, 2025"
        out["book_imprint"] = f"{_clip(imprint.group(2))}, {imprint.group(3)}"
        text = text[:imprint.start()]
    m = BY_RE.search(text) or BARE_BY_RE.search(text)
    if m and m.start() > 0:                       # 「Title: by Author, City, Press, 2025, …」 or 「Title. By Author」
        head, tail = text[:m.start()], text[m.end():]
        out["book_title"] = _clip(head)
        cut = PAGES_PRICE_RE.search(tail)
        bits = SENTENCE_RE.split(tail[:cut.start()] if cut else tail, maxsplit=1)
        names, after = bits[0], (bits[1] if len(bits) > 1 else "")
        parts = [x.strip() for x in names.split(",")]
        out["book_authors"] = _clip(parts[0]) or None
        rest = ", ".join([*parts[1:], after])
        year = YEAR_RE.search(rest)
        if not out["book_imprint"] and year:      # the publisher is what stands just before the year
            publisher = _clip(rest[:year.start()].rstrip(", ").split(",")[-1])
            publisher = _clip(publisher.split(": ", 1)[-1])   # "Bielefeld: transcript publishing" keeps the publisher
            out["book_imprint"] = f"{publisher}, {year.group(0)}" if publisher else None
        return out
    if imprint:                                   # 「Title Author. City: Press, 2025」: the names end the head
        head, names = _clip(text), []
        while (tail := NAME_TAIL_RE.match(head)) and len(tail.group("title").split()) >= 2:
            names.insert(0, tail.group("name"))
            head = tail.group("title")
            joined = JOINER_RE.search(head)
            if not joined:
                break
            head = head[:joined.start()]
        if names and not JOINER_RE.search(head):
            out["book_title"] = _clip(head)
            out["book_authors"] = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        else:
            out["book_title"] = _clip(text) or None
        return out
    # SEX: 「Book review: title in lower case AuthorsRunTogether. Title. City. Publisher. Year」
    cut = PAGES_PRICE_RE.search(text)
    parts = [x.strip() for x in re.split(r"\.\s+", (text[:cut.start()] if cut else text).rstrip(" ;,.")) if x.strip()]
    if len(parts) >= 4 and YEAR_RE.fullmatch(parts[-1]) and parts[0].lower().startswith(parts[-4].lower()[:30]):
        out["book_title"], out["book_imprint"] = _clip(parts[-4]), f"{_clip(parts[-2])}, {parts[-1]}"
        return out
    cut = REVIEWISH_RE.search(text)
    out["book_title"] = _clip(text[:cut.start()] if cut else text) or None
    return out


def paper_key(doi: str | None, title: str = "", journal: str = "") -> str:
    basis = f"doi:{doi.lower()}" if doi else f"title:{journal}:{re.sub(r'\W+', ' ', title.lower()).strip()}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


# ISEAS deposits curly quotes as U+FFFD (card 0155), as in Prabowo?s and ?Bidenomics?. Contractions and letters on both
# sides take an apostrophe, a pair around a word or phrase takes “ ”, and anything left takes an apostrophe.
FFFD = "\ufffd"
CONTRACTION_RE = re.compile(rf"(?<=[A-Za-z]){FFFD}(?=(?:s|t|d|ll|re|ve|m)\b)")
QUOTE_PAIR_RE = re.compile(rf"(^|[\s(\[—–/-]){FFFD}([^{FFFD}\s](?:[^{FFFD}]*?[^{FFFD}\s])?){FFFD}(?=$|[\s.,;:!?)\]—–/-])")
INNER_RE = re.compile(rf"(?<=[A-Za-z]){FFFD}(?=[A-Za-z])")


def fix_mojibake(text: str | None) -> str | None:
    if not text or FFFD not in text:
        return text
    t = CONTRACTION_RE.sub("’", text)
    t = QUOTE_PAIR_RE.sub(lambda m: f"{m.group(1)}“{m.group(2)}”", t)
    t = INNER_RE.sub("’", t)
    return t.replace(FFFD, "’")


def clean_text(text: str | None) -> str:
    """Strip JATS / HTML tags and entities; drop a leading 'Abstract' label; mend U+FFFD quotes."""
    s = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return fix_mojibake(re.sub(r"^(abstract|summary)[\s:.]+", "", s, flags=re.I)) or ""


def _date(msg: dict, *keys: str) -> str | None:
    for key in keys:
        v = msg.get(key) or {}
        if v.get("date-time"):
            return str(v["date-time"])[:10]
        parts = (v.get("date-parts") or [[None]])[0]
        if parts and parts[0]:
            nums = [int(p) for p in parts if p] + [1, 1]
            try:
                return date(nums[0], nums[1], nums[2]).isoformat()
            except ValueError:
                continue
    return None


def parse_item(msg: dict, journal: Journal) -> dict[str, Any] | None:
    """One Crossref work → a feed row, or None when it has no title."""
    title = clean_text((msg.get("title") or [""])[0] if isinstance(msg.get("title"), list) else msg.get("title"))
    if not title:
        return None
    sub = clean_text((msg.get("subtitle") or [""])[0] if isinstance(msg.get("subtitle"), list) else "")
    if sub and sub.lower() not in title.lower():
        title = f"{title.rstrip(':')}: {sub}"
    authors = []
    for a in msg.get("author") or []:
        name = " ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name") or ""
        if name.strip():
            authors.append(name.strip())
    doi = (msg.get("DOI") or "").strip().lower() or None
    return {"id": paper_key(doi, title, journal.abbr), "doi": doi, "journal": journal.abbr, "journal_name": journal.name,
            "journal_column": journal.column, "pool": journal.pool, "title": title, "authors": authors,
            "published": _date(msg, "created", "published-online", "published-print", "issued"),
            "abstract": clean_text(msg.get("abstract")) or None, "is_oa": None, "oa_pdf_url": None, "oa_license": None,
            "page": str(msg.get("page") or "").strip() or None,
            "resource_url": ((msg.get("resource") or {}).get("primary") or {}).get("URL")}


def crossref_items(http: httpx.Client, journal: Journal, since: str, rows: int = ROWS) -> list[dict]:
    """Newest registrations of one journal, trying alternate ISSNs when the primary returns nothing.
    Raises net.FetchError when no ISSN could be read at all."""
    params = {"filter": f"from-created-date:{since},type:journal-article", "sort": "created", "order": "desc", "rows": rows}
    mail = net.contact()
    if mail:
        params["mailto"] = mail
    error: net.FetchError | None = None
    ok = False
    for issn in journal.issns:
        url = f"{CROSSREF}/journals/{quote(issn)}/works"
        try:
            data = net.get_json(http, url, {**params, "select": SELECT})
        except net.FetchError as e:
            if "HTTP 400" not in str(e):
                error = e
                continue
            try:   # an unsupported select field answers 400; ask for every field instead
                data = net.get_json(http, url, params)
            except net.FetchError as e2:
                error = e2
                continue
        ok = True
        items = ((data or {}).get("message") or {}).get("items") if isinstance(data, dict) else None
        items = [i for i in items or [] if isinstance(i, dict)]
        if items:
            return items
    if not ok and error is not None:
        raise error
    return []


def openalex_fill(http: httpx.Client, rows: list[dict]) -> list[str]:
    """Abstracts (when Crossref had none) and open-access state for rows with a DOI. Returns errors."""
    errors: list[str] = []
    with_doi = [r for r in rows if r.get("doi")]
    for i in range(0, len(with_doi), OPENALEX_BATCH):
        batch = with_doi[i:i + OPENALEX_BATCH]
        params: dict[str, Any] = {"filter": "doi:" + "|".join(r["doi"] for r in batch), "per-page": OPENALEX_BATCH + 10,
                                  "select": "doi,abstract_inverted_index,open_access,best_oa_location"}
        if net.contact():
            params["mailto"] = net.contact()
        try:
            data = net.get_json(http, f"{OPENALEX}/works", params)
        except net.FetchError as e:
            errors.append(f"OpenAlex: {e}")
            continue
        found = {}
        for w in (data or {}).get("results") or []:
            key = (w.get("doi") or "").lower().replace("https://doi.org/", "")
            if key:
                found[key] = w
        for r in batch:
            w = found.get(r["doi"])
            if not w:
                continue
            if not r.get("abstract"):
                r["abstract"] = clean_text(abstract_from_inverted(w.get("abstract_inverted_index"))) or None
            oa = w.get("open_access") or {}
            loc = w.get("best_oa_location") or {}
            if oa.get("is_oa") is not None:
                r["is_oa"] = 1 if oa.get("is_oa") else 0
            if loc.get("pdf_url") and net.looks_public(loc["pdf_url"]):
                r["oa_pdf_url"] = loc["pdf_url"]
            r["oa_license"] = loc.get("license") or r.get("oa_license")
    return errors


def unpaywall(http: httpx.Client, doi: str) -> dict[str, Any] | None:
    """{'is_oa', 'pdf_url', 'license'} or None. Unpaywall needs a contact address; without one this is a no-op."""
    mail = net.contact()
    if not mail or not doi:
        return None
    try:
        data = net.get_json(http, f"{UNPAYWALL}/{quote(doi, safe='/:;()._-')}", {"email": mail})
    except net.FetchError:
        return None
    loc = (data or {}).get("best_oa_location") or {}
    pdf = loc.get("url_for_pdf") or None
    return {"is_oa": bool(data.get("is_oa")), "pdf_url": pdf if net.looks_public(pdf) else None, "license": loc.get("license")}


def known_ids(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
    out: set[str] = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        out.update(r[0] for r in conn.execute(f"SELECT id FROM feed_papers WHERE id IN ({','.join('?' * len(chunk))})", chunk))
    return out


def insert(conn: sqlite3.Connection, row: dict[str, Any], *, origin: str = "crossref", first_seen: str | None = None) -> bool:
    """INSERT OR IGNORE one feed paper. True when it is new."""
    ts = now_iso()
    cur = conn.execute(
        "INSERT OR IGNORE INTO feed_papers(id, doi, journal, journal_name, journal_column, pool, title, authors_json, published, "
        "abstract, zh_title, zh_abstract, direction, direction_source, radar, radar_json, is_oa, oa_pdf_url, oa_license, origin, "
        "first_seen, status, updated_at, kind, book_title, book_authors, book_imprint) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row["id"], row.get("doi"), row.get("journal"), row.get("journal_name"), row.get("journal_column"), row.get("pool"),
         row["title"], json.dumps(row.get("authors") or [], ensure_ascii=False), row.get("published"), row.get("abstract"),
         row.get("zh_title") or None, row.get("zh_abstract") or None, row.get("direction"),
         "keyword" if row.get("direction") else None, 1 if row.get("radar") else 0,
         json.dumps(row.get("radar_terms") or [], ensure_ascii=False), row.get("is_oa"), row.get("oa_pdf_url"),
         row.get("oa_license"), origin, first_seen or ts, row.get("status") or "new", ts, row.get("kind") or "article",
         row.get("book_title"), row.get("book_authors"), row.get("book_imprint")))
    return cur.rowcount == 1


def default_days(last_at: str | None, today: date | None = None) -> int:
    """First run looks back three weeks; later runs cover the gap since the last run plus a margin."""
    today = today or date.today()
    if not last_at:
        return 21
    try:
        gap = (today - date.fromisoformat(last_at[:10])).days
    except ValueError:
        return 21
    return max(4, min(30, gap + 3))


def refresh_papers(conn: sqlite3.Connection, http: httpx.Client | None = None, days: int = 10, *,
                   journals: list[Journal] | None = None, srcs: Sources | None = None, lookup_oa: bool = True) -> dict[str, Any]:
    """Fetch every journal's new articles since `days` ago and store the new ones. Book reviews are stored too, as
    kind 'review' with the reviewed book parsed from the title (card 0154); they skip the OpenAlex and Unpaywall
    lookups. Returns counts: {'sources', 'fetched', 'new', 'reviews', 'skipped', 'known', 'per_source': {abbr: n},
    'failed': [{'source', 'error'}]}."""
    srcs = srcs or sources_mod.load()
    journals = journals if journals is not None else srcs.journals
    own = http is None
    http = http or net.new_client()
    since = (date.today() - timedelta(days=days)).isoformat()
    dirs = directions.load()
    summary: dict[str, Any] = {"sources": len(journals), "fetched": 0, "new": 0, "reviews": 0, "skipped": 0, "known": 0, "filtered": 0,
                               "per_source": {}, "failed": [], "errors": []}
    try:
        fresh: list[dict] = []
        for j in journals:
            try:
                items = crossref_items(http, j, since)
            except net.FetchError as e:
                summary["failed"].append({"source": j.abbr, "error": str(e)})
                continue
            except Exception as e:  # noqa: BLE001 - one journal never breaks the run
                log.exception("journal %s failed", j.abbr)
                summary["failed"].append({"source": j.abbr, "error": type(e).__name__})
                continue
            rows = [r for r in (parse_item(i, j) for i in items) if r]
            summary["fetched"] += len(rows)
            kept = []
            for r in rows:
                if is_junk(r["title"], r.get("page")):
                    summary["skipped"] += 1
                    continue
                if is_book_review(r["title"]):
                    r.update(kind="review", **parse_review(r["title"]))
                kept.append(r)
            seen = known_ids(conn, [r["id"] for r in kept])
            summary["known"] += sum(1 for r in kept if r["id"] in seen)
            batch_ids: set[str] = set()
            for r in kept:
                if r["id"] not in seen and r["id"] not in batch_ids:
                    batch_ids.add(r["id"])
                    r["_journal"] = j
                    fresh.append(r)
        summary["errors"] += openalex_fill(http, [r for r in fresh if r.get("kind") != "review"])
        ready: list[dict] = []
        for r in fresh:
            j: Journal = r.pop("_journal")
            if r.get("kind") != "review" and is_short_review(r):   # judged after OpenAlex had its chance at an abstract
                r.update(kind="review", **parse_review(r["title"]))
            text = f"{r['title']} {r.get('abstract') or ''}"
            if j.gp_filter and not sources_mod.gp_relevant(text, srcs.gp_terms):
                summary["filtered"] += 1
                continue
            review = r.get("kind") == "review"
            if lookup_oa and not review and r.get("doi") and not r.get("oa_pdf_url") and r.get("is_oa") != 0:
                found = unpaywall(http, r["doi"])
                if found:
                    r["is_oa"] = 1 if found["is_oa"] else 0
                    r["oa_pdf_url"] = found["pdf_url"] or r.get("oa_pdf_url")
                    r["oa_license"] = found["license"] or r.get("oa_license")
            hits = sources_mod.radar_hits(text, srcs.radar_terms)
            r.update(radar=bool(hits), radar_terms=hits, _journal=j,
                     direction=directions.guess(r.get("book_title") or r["title"], r.get("abstract"), dirs=dirs))
            ready.append(r)
        # after Unpaywall: what only the publisher's own page gives away (publisher_oa, card 0155), reviews included
        if lookup_oa:
            publisher_oa.resolve(ready, http)
        for r in ready:
            j = r.pop("_journal")
            review = r.get("kind") == "review"
            if not insert(conn, r):
                continue
            if review:
                summary["reviews"] += 1
            else:
                summary["new"] += 1
                summary["per_source"][j.abbr] = summary["per_source"].get(j.abbr, 0) + 1
    finally:
        if own:
            http.close()
    return summary


def _drop_from_picks(conn: sqlite3.Connection, ids: list[str]) -> None:
    """Take papers that turned out to be reviews out of every stored 今日精选 (feeds/digest.py keeps the picks per day)."""
    from ..db.connection import set_setting
    from . import digest
    gone = set(ids)
    for key, value in conn.execute("SELECT key, value FROM settings WHERE key LIKE ?", (digest.KEY + "%",)).fetchall():
        issue = json.loads(value)
        if isinstance(issue, dict) and gone & set(issue.get("papers") or []):
            issue["papers"] = [i for i in issue["papers"] if i not in gone]
            set_setting(conn, key, issue)


def backfill_reviews(conn: sqlite3.Connection, http: httpx.Client | None = None, days: int = 60, *, dry_run: bool = False,
                     journals: list[Journal] | None = None, srcs: Sources | None = None) -> dict[str, dict[str, Any]]:
    """`rl feeds reviews-backfill` (card 0154): ask every journal again for the last `days` and apply today's rules to
    what the old ones got wrong. Per journal: 'new' reviews the old filter dropped, inserted as kind 'review';
    'reclassified' stored articles that are reviews (they leave the stored picks too); 'junk' front matter that slipped
    in, now dismissed; 'error' when the journal could not be read. A dry run counts and writes nothing."""
    srcs = srcs or sources_mod.load()
    journals = journals if journals is not None else srcs.journals
    own = http is None
    http = http or net.new_client()
    since = (date.today() - timedelta(days=days)).isoformat()
    dirs = directions.load()
    out: dict[str, dict[str, Any]] = {}
    turned: list[str] = []
    try:
        for j in journals:
            c: dict[str, Any] = {"new": 0, "reclassified": 0, "junk": 0}
            out[j.abbr] = c
            try:
                items = crossref_items(http, j, since)
            except net.FetchError as e:
                c["error"] = str(e)
                continue
            rows = [r for r in (parse_item(i, j) for i in items) if r]
            ids = [r["id"] for r in rows]
            stored = {row["id"]: row for row in conn.execute(
                f"SELECT id, kind, status, abstract FROM feed_papers WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
            fresh_short = []
            for r in rows:
                s = stored.get(r["id"])
                if is_junk(r["title"], r.get("page")):
                    if s and s["status"] != "dismissed":
                        c["junk"] += 1
                        if not dry_run:
                            conn.execute("UPDATE feed_papers SET status = 'dismissed', status_at = ?, updated_at = ? WHERE id = ?",
                                         (now_iso(), now_iso(), r["id"]))
                    continue
                if s is not None:
                    if s["kind"] != "review" and (is_book_review(r["title"]) or
                                                  is_short_review({**r, "abstract": r.get("abstract") or s["abstract"]})):
                        c["reclassified"] += 1
                        turned.append(r["id"])
                        if not dry_run:
                            book = parse_review(r["title"])
                            conn.execute("UPDATE feed_papers SET kind = 'review', book_title = ?, book_authors = ?, book_imprint = ?, "
                                         "updated_at = ? WHERE id = ?",
                                         (book["book_title"], book["book_authors"], book["book_imprint"], now_iso(), r["id"]))
                    continue
                if is_book_review(r["title"]):
                    _backfill_insert(conn, r, j, srcs, dirs, c, dry_run)
                elif is_short_review(r):
                    fresh_short.append(r)
            # a short item without a Crossref abstract may still have one in OpenAlex: then it is an article
            openalex_fill(http, fresh_short)
            for r in fresh_short:
                if is_short_review(r):
                    _backfill_insert(conn, r, j, srcs, dirs, c, dry_run)
        if turned and not dry_run:
            _drop_from_picks(conn, turned)
    finally:
        if own:
            http.close()
    return out


def _backfill_insert(conn: sqlite3.Connection, r: dict[str, Any], j: Journal, srcs: Sources, dirs: list, c: dict[str, Any],
                     dry_run: bool) -> None:
    text = f"{r['title']} {r.get('abstract') or ''}"
    if j.gp_filter and not sources_mod.gp_relevant(text, srcs.gp_terms):
        return   # the journal's own filter, as on a refresh
    c["new"] += 1
    if dry_run:
        return
    hits = sources_mod.radar_hits(text, srcs.radar_terms)
    r.update(kind="review", radar=bool(hits), radar_terms=hits, **parse_review(r["title"]))
    r["direction"] = directions.guess(r.get("book_title") or r["title"], r.get("abstract"), dirs=dirs)
    insert(conn, r)


def repair(conn: sqlite3.Connection, http: httpx.Client | None = None, days: int = 90, *, dry_run: bool = False,
           srcs: Sources | None = None) -> dict[str, int]:
    """`rl feeds repair` (card 0155) over the last `days` of stored rows; it writes only these fields and deletes nothing:
    'titles' / 'abstracts' with U+FFFD quotes mended (and a review's parsed book fields with them); 'publisher_pdfs'
    free PDFs found on the publisher's own page (publisher_oa), which asks Crossref once per journal that has unread
    papers without one; 'directions' and 'news_directions', a keyword direction (the guess made at insert) where the
    stored one is NULL, never over one that is set. A dry run counts and writes nothing."""
    srcs = srcs or sources_mod.load()
    since = (date.today() - timedelta(days=days)).isoformat()
    out = {"titles": 0, "abstracts": 0, "publisher_pdfs": 0, "directions": 0, "news_directions": 0}
    for r in conn.execute("SELECT id, title, abstract, book_title, book_authors FROM feed_papers WHERE substr(first_seen, 1, 10) >= ? "
                          "AND (instr(title, ?) > 0 OR instr(COALESCE(abstract, ''), ?) > 0 OR instr(COALESCE(book_title, ''), ?) > 0)",
                          (since, FFFD, FFFD, FFFD)).fetchall():
        title, abstract = fix_mojibake(r["title"]), fix_mojibake(r["abstract"])
        out["titles"] += title != r["title"]
        out["abstracts"] += abstract != r["abstract"]
        if not dry_run:
            conn.execute("UPDATE feed_papers SET title = ?, abstract = ?, book_title = ?, book_authors = ?, updated_at = ? WHERE id = ?",
                         (title, abstract, fix_mojibake(r["book_title"]), fix_mojibake(r["book_authors"]), now_iso(), r["id"]))
    own = http is None
    http = http or net.new_client()
    try:
        waiting: dict[str, dict[str, dict]] = {}
        for r in conn.execute("SELECT id, doi, journal, title, authors_json FROM feed_papers WHERE substr(first_seen, 1, 10) >= ? "
                              "AND doi IS NOT NULL AND COALESCE(oa_pdf_url, '') = '' AND status != 'dismissed'", (since,)).fetchall():
            waiting.setdefault(r["journal"], {})[r["doi"].lower()] = {"id": r["id"], "title": fix_mojibake(r["title"]),
                                                                       "authors": json.loads(r["authors_json"] or "[]")}
        for j in srcs.journals:
            rows = waiting.get(j.abbr)
            if not rows:
                continue
            try:
                items = crossref_items(http, j, since)
            except net.FetchError as e:
                log.info("repair: %s not readable: %s", j.abbr, e)
                continue
            for item in items:
                doi = str(item.get("DOI") or "").lower()
                url = ((item.get("resource") or {}).get("primary") or {}).get("URL")
                if doi in rows and publisher_oa.handles(url):
                    rows[doi]["resource_url"] = url
            found = [r for r in rows.values() if r.get("resource_url")]
            publisher_oa.resolve(found, http)
            for r in found:
                if r.get("oa_pdf_url"):
                    out["publisher_pdfs"] += 1
                    if not dry_run:
                        conn.execute("UPDATE feed_papers SET is_oa = 1, oa_pdf_url = ?, oa_license = ?, updated_at = ? WHERE id = ?",
                                     (r["oa_pdf_url"], r["oa_license"], now_iso(), r["id"]))
    finally:
        if own:
            http.close()
    dirs = directions.load()
    for table, key, cols in (("feed_papers", "directions", "COALESCE(book_title, title) AS t, abstract AS a"),
                             ("feed_news", "news_directions", "title AS t, summary AS a")):
        for r in conn.execute(f"SELECT id, {cols} FROM {table} WHERE direction IS NULL AND substr(first_seen, 1, 10) >= ?",
                              (since,)).fetchall():
            slug = directions.guess(fix_mojibake(r["t"]), fix_mojibake(r["a"]), dirs=dirs)
            if slug:
                out[key] += 1
                if not dry_run:
                    conn.execute(f"UPDATE {table} SET direction = ?, direction_source = 'keyword' WHERE id = ? AND direction IS NULL",
                                 (slug, r["id"]))
    return out
