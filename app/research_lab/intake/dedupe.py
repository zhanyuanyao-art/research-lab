"""Exact (sha256) and fuzzy (title) duplicate detection, and citekey ids."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata

from rapidfuzz import fuzz

FUZZY_THRESHOLD = 92
STOPWORDS = frozenset("""
a an the and or but nor of on in into onto to for from by with without at as over under between among
about after before during through toward towards against beyond within upon via vs versus is are was were be
been being do does did doing has have had can could should would will shall may might must not no than then
this that these those there here what which who whom whose why how when where whether its it their his her
our your my one two new beyond revisiting rethinking re toward
""".split())


def normalize_title(title: str) -> str:
    decomposed = unicodedata.normalize("NFKD", title or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
    return " ".join(re.findall(r"[^\W_]+", stripped))


def title_hash(title: str) -> str:
    return hashlib.sha1(normalize_title(title).encode("utf-8")).hexdigest()


def ascii_slug(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    return re.sub(r"[^a-z0-9]", "", decomposed.encode("ascii", "ignore").decode().lower())


def family_name(author: str | None) -> str:
    """'Tan, Lin' -> 'Tan'; 'Lin Tan' -> 'Tan'."""
    if not author:
        return ""
    a = author.strip()
    if "," in a:
        return a.split(",", 1)[0].strip()
    parts = a.split()
    return parts[-1] if parts else ""


def first_author(authors_json: str | list | None) -> str:
    authors = json.loads(authors_json) if isinstance(authors_json, str) else (authors_json or [])
    return family_name(authors[0]) if authors else ""


def by_sha(conn: sqlite3.Connection, sha256: str) -> str | None:
    row = conn.execute("SELECT paper_id FROM paper_files WHERE sha256 = ?", (sha256,)).fetchone()
    return row[0] if row else None


def by_doi(conn: sqlite3.Connection, doi: str | None) -> str | None:
    if not doi:
        return None
    row = conn.execute("SELECT id FROM papers WHERE lower(doi) = lower(?)", (doi,)).fetchone()
    return row[0] if row else None


def fuzzy_match(conn: sqlite3.Connection, title: str, authors: list[str], year: int | None,
                exclude: str | None = None) -> str | None:
    """A title-hash hit, or token_set_ratio >= 92 with the same first author and year."""
    h = title_hash(title)
    row = conn.execute("SELECT id FROM papers WHERE title_hash = ? AND id IS NOT ?", (h, exclude)).fetchone()
    if row:
        return row[0]
    if year is None or not authors:
        return None
    fam = ascii_slug(family_name(authors[0]))
    norm = normalize_title(title)
    for r in conn.execute("SELECT id, title, authors_json FROM papers WHERE year = ? AND id IS NOT ?", (year, exclude)):
        if ascii_slug(first_author(r["authors_json"])) == fam and fuzz.token_set_ratio(norm, normalize_title(r["title"])) >= FUZZY_THRESHOLD:
            return r["id"]
    return None


def first_noun(title: str) -> str:
    for word in normalize_title(title).split():
        w = ascii_slug(word)
        if len(w) >= 3 and w not in STOPWORDS and not w.isdigit():
            return w
    # A Chinese/Japanese/Korean title has no ASCII word; without a romanisation dependency a short
    # stable hash keeps citekeys distinct instead of stacking anon-nd-untitled-2, -3, …
    if re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", title or ""):
        return "zh" + title_hash(title)[:4]
    return "untitled"


def make_citekey(conn: sqlite3.Connection, authors: list[str], year: int | None, title: str) -> str:
    fam = ascii_slug(family_name(authors[0])) if authors else ""
    base = f"{fam or 'anon'}{year or 'nd'}-{first_noun(title)}"
    key, n = base, 2
    while conn.execute("SELECT 1 FROM papers WHERE id = ?", (key,)).fetchone():
        key = f"{base}-{n}"
        n += 1
    return key
