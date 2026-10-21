"""Shared bibliography storage, independent of legacy feed repositories."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from ..db.connection import now_iso


@dataclass
class Item:
    index: int
    title: str
    authors: str
    date: str
    url: str
    doi: str | None
    journal: str
    journal_name: str
    tag: str
    abstract: str | None = None
    hit: bool = False
    direction: str | None = None
    direction_name: str | None = None


def saved_paper_for(conn: sqlite3.Connection, item: Item) -> sqlite3.Row | None:
    if item.doi:
        row = conn.execute("SELECT id, title FROM papers WHERE doi = ?", (item.doi.lower(),)).fetchone()
        if row:
            return row
    from . import dedupe
    return conn.execute("SELECT id, title FROM papers WHERE title_hash = ?", (dedupe.title_hash(item.title),)).fetchone()


def save_item(conn: sqlite3.Connection, item: Item, project: str | None = None, *, http=None,
              direction: str | None = None) -> str:
    """Create a file-less paper record from a digest item (metadata via Crossref/OpenAlex when the DOI resolves).
    The radar's direction chip becomes papers.direction; an already saved paper gets it only if it has none."""
    from . import directions as dirs_mod
    direction = dirs_mod.clean(direction if direction is not None else item.direction)
    existing = saved_paper_for(conn, item)
    if existing:
        if direction:
            conn.execute("UPDATE papers SET direction = ? WHERE id = ? AND direction IS NULL", (direction, existing["id"]))
        return existing["id"]
    from . import dedupe, metadata
    title, authors, year, venue, kind, abstract = item.title, [a.strip() for a in item.authors.split(",") if a.strip()], None, item.journal_name, "article", item.abstract
    status_, crossref_json, openalex_json, openalex_id = "unverified", None, None, None
    if item.doi:
        try:
            res = metadata.fetch_for_doi(conn, item.doi, http=http)
        except Exception:  # noqa: BLE001 — offline is fine; the row stays unverified
            res = None
        if res and res.chosen:
            c = res.chosen
            title, authors = c.title or title, c.authors or authors
            year, venue, kind = c.year, c.venue or venue, c.kind or kind
            abstract = res.abstract or abstract
            status_ = "verified" if res.status == "verified" else "candidate"
            crossref_json = json.dumps(res.crossref) if res.crossref else None
            openalex_json = json.dumps(res.openalex) if res.openalex else None
            openalex_id = res.openalex_id
    if year is None and item.date:
        year = int(item.date[:4])
    paper_id = dedupe.make_citekey(conn, authors, year, title)
    ts = now_iso()
    conn.execute(
        "INSERT INTO papers(id, doi, openalex_id, title, title_hash, authors_json, year, venue, kind, abstract, metadata_status, "
        "crossref_json, openalex_json, read_depth, direction, language, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (paper_id, item.doi.lower() if item.doi else None, openalex_id, title, dedupe.title_hash(title),
         json.dumps(authors, ensure_ascii=False), year, venue, kind, abstract, status_, crossref_json, openalex_json,
         "abstract", direction, "en", ts, ts))
    if project:
        conn.execute("INSERT OR IGNORE INTO paper_projects(paper_id, project_slug) VALUES (?,?)", (paper_id, project))
    return paper_id


