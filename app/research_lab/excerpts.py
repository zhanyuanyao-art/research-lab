"""Excerpt service: capture, quote verification, tags, status, suggestions, add-to-artifact.

Guardrails (method-layer §7): the quote is only ever written from user input; AI suggestions are
stored as `proposed` and become real only through accept_* calls made on a user action; nothing here
writes into an artifact's Claim or "What did I figure out" sections.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from rapidfuzz import fuzz

from .config import get_paths
from .db.connection import next_sequence_id, now_iso
from .files import artifacts as art_files
from .files import datafiles
from .files.weeks import week_of

log = logging.getLogger("research_lab.excerpts")

KINDS = ("quote", "data", "paraphrase", "idea")
USES = ("definition", "evidence", "counter", "mechanism", "method", "framing", "quotable")
CAMPS = ("institutional-constructivist", "small-state-strategy", "great-power-hierarchy", "sceptic",
         "relational-network", "other")
CASES = ("ARF", "APT", "EAS", "ADMM+", "AOIP", "RCEP", "TAC", "SCS-COC", "QUAD")
STRENGTHS = ("direct", "inferential", "circumstantial")
SOURCE_TYPES = ("primary-official", "primary-statement", "interview", "secondary-scholarly", "media")
STANCES = ("supports", "contradicts", "neutral")
STATUSES = ("inbox", "linked", "used", "discarded")
REJECT_REASONS = ("wrong-link", "trivial", "already-known", "overclaims")
RELATIONS = ("supports", "contradicts", "same-case", "same-concept", "defines", "mechanism", "parallel")
VERIFY_THRESHOLD = 95
QUOTE_IN_ARTIFACT = 240
# Artifact type for a new Seed, from the dominant kind of the excerpt's accepted links.
TYPE_FOR_TARGET = {"concept": "concept", "case": "case-map", "camp": "literature-map", "claim": "evidence",
                   "anchor": "question", "excerpt": "evidence", "artifact": "evidence"}


class ExcerptError(ValueError):
    pass


# ---------------------------------------------------------------- formatting helpers

def _authors(authors_json: str | None) -> list[str]:
    try:
        raw = json.loads(authors_json or "[]")
    except (TypeError, ValueError):
        return []
    out = []
    for a in raw if isinstance(raw, list) else []:
        if isinstance(a, dict):
            name = a.get("family") or a.get("last") or a.get("name") or ""
            if name and not (a.get("family") or a.get("last")) and "," not in name:
                name = name.split()[-1]
            out.append(name.split(",")[0].strip())
        elif isinstance(a, str) and a.strip():
            s = a.strip()
            out.append(s.split(",")[0].strip() if "," in s else s.split()[-1])
    return [a for a in out if a]


def first_author(authors_json: str | None, fallback: str = "Anon.") -> str:
    names = _authors(authors_json)
    if not names:
        return fallback
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]} et al."


def short_title(title: str | None, limit: int = 48) -> str:
    """Main title before a subtitle ("Who Convenes Asia?"), cut at a word boundary."""
    t = " ".join((title or "").split())
    m = re.match(r"^(.+?(?:[?:](?=\s)|[：？]))", t + " ")
    t = (m.group(1) if m else t).rstrip(":：").strip()
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0].rstrip(",;") + "…"
    return t


def author_year(authors_json: str | None, year: Any, title: str | None = None) -> str:
    fallback = short_title(title) or "Anon."
    return f"{first_author(authors_json, fallback)} {year or 'n.d.'}"


def cite_zh(authors_json: str | None, year: Any, title: str | None = None) -> str:
    """Short citation for the interface: "Tan 2023", or the title when the author is unknown (no "n.d.")."""
    who = first_author(authors_json, "")
    if who:
        return f"{who} {year}" if year else who
    return short_title(title, 36)


_CJK = re.compile(r"[㐀-鿿豈-﫿]")
# Crossref sometimes lists an affiliation as an author ("Professor at the University of Auckland, New Zealand.").
_NOT_A_PERSON = re.compile(r"\b(?:professor|university|department|institute|school|college|cent(?:er|re))\b", re.I)


def _full_name(a: Any) -> str:
    if isinstance(a, dict):
        family = str(a.get("family") or a.get("last") or "").strip()
        given = str(a.get("given") or a.get("first") or "").strip()
        if not family:
            return str(a.get("name") or "").strip()
    elif isinstance(a, str) and a.count(",") == 1:
        family, given = (s.strip() for s in a.split(","))
    else:
        return str(a or "").strip()
    return family + given if _CJK.search(family + given) else f"{given} {family}".strip()


def _biblio(crossref_json: str | None, openalex_json: str | None) -> tuple[str, str, str]:
    """Volume, issue and page range from the stored Crossref record, else OpenAlex's biblio."""
    def load(raw: str | None) -> dict:
        try:
            d = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}
    cr, oa = load(crossref_json), load(openalex_json)
    b = oa.get("biblio") if isinstance(oa.get("biblio"), dict) else {}
    vol = str(cr.get("volume") or b.get("volume") or "").strip()
    issue = str(cr.get("issue") or b.get("issue") or "").strip()
    page = str(cr.get("page") or "").strip()
    if not page and b.get("first_page"):
        first, last = str(b["first_page"]).strip(), str(b.get("last_page") or "").strip()
        page = f"{first}-{last}" if last and last != first else first
    return vol, issue, re.sub(r"\s*[–—-]\s*", "-", page)


def citation(paper: Any) -> str:
    """The full reference the way Chinese IR journals print it (Leo, card 0200): `Detlef Nolte, "How to Compare
    Regional Powers", Review of International Studies, Vol. 36, No. 4, 2010, pp. 881-901.` for a foreign work,
    `秦亚青：《关系本位与过程建构》，载《中国社会科学》2009年第3期，第69—86页。` for a Chinese one. Parts the record
    lacks are left out; more than three authors become "et al."."""
    try:
        raw = json.loads(paper["authors_json"] or "[]")
    except (TypeError, ValueError):
        raw = []
    names = [n for n in (_full_name(a) for a in (raw if isinstance(raw, list) else [])) if n and not _NOT_A_PERSON.search(n)]
    title = " ".join((paper["title"] or "").split())
    venue = " ".join((paper["venue"] or "").split())
    year = paper["year"]
    vol, issue, pages = _biblio(paper["crossref_json"], paper["openalex_json"])
    book = paper["kind"] == "book"
    if _CJK.search(title):
        if book:
            tail = venue + (f"{year}年版" if year else "")
        else:
            tail = (f"载《{venue}》" if venue else "") + (f"{year}年" if year else "") + (f"第{issue}期" if issue else "")
            if pages:
                tail += ("，" if tail else "") + f"第{pages.replace('-', '—')}页"
        return ("、".join(names) + "：" if names else "") + f"《{title}》" + (f"，{tail}" if tail else "") + "。"
    if len(names) > 3:
        who = f"{names[0]} et al."
    elif len(names) > 1:
        who = ", ".join(names[:-1]) + " and " + names[-1]
    else:
        who = names[0] if names else ""
    parts = [who, title if book else f'"{title}"', venue, f"Vol. {vol}" if vol else "", f"No. {issue}" if issue else "",
             str(year) if year else "", f"pp. {pages}" if pages and not book else ""]
    text = ", ".join(p for p in parts if p)
    return text if text.endswith(".") else text + "."


def locator_label(locator: str | None) -> str:
    loc = (locator or "").strip()
    if re.fullmatch(r"[0-9ivxlcdm]+", loc, re.I):
        return f"p. {loc}"
    m = re.fullmatch(r"([0-9ivxlcdm]+)\s*[-–]\s*([0-9ivxlcdm]+)", loc, re.I)
    if m:
        return f"pp. {m.group(1)}–{m.group(2)}"
    return loc


_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                         " ": " ", "­": None})


def normalize(text: str) -> str:
    s = unicodedata.normalize("NFKC", text or "").translate(_QUOTES)
    s = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def one_line(text: str, limit: int) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------- verification

def _matches(q: str, text: str) -> bool:
    return bool(q) and len(text) >= len(q) and fuzz.partial_ratio(q, text) >= VERIFY_THRESHOLD


def verify_quote(conn: sqlite3.Connection, paper_id: str, quote: str, file_id: int | None = None,
                 page_index: int | None = None) -> tuple[bool, int | None, int | None]:
    """Search the given page first, then the rest of that file, then the paper's other files.
    Returns (verified, file_id, page_index) of the page where it matched."""
    q = normalize(quote)
    if not q:
        return False, file_id, page_index
    rows = conn.execute(
        "SELECT pp.file_id, pp.page_index, pp.text FROM paper_pages pp JOIN paper_files f ON f.id = pp.file_id "
        "WHERE f.paper_id = ? ORDER BY (pp.file_id = ?) DESC, f.is_primary DESC, pp.file_id, pp.page_index",
        (paper_id, file_id if file_id is not None else -1)).fetchall()
    if not rows:
        return False, file_id, page_index
    pages = [(r["file_id"], r["page_index"], normalize(r["text"])) for r in rows]
    first = [p for p in pages if p[1] == page_index and (file_id is None or p[0] == file_id)] if page_index is not None else []
    for fid, pidx, text in first:
        if _matches(q, text):
            return True, fid, pidx
    for fid, pidx, text in pages:
        if _matches(q, text):
            return True, fid, pidx
    # quotes that run across a page break
    for (fid, pidx, text), (fid2, _, text2) in zip(pages, pages[1:]):
        if fid == fid2 and _matches(q, text + " " + text2):
            return True, fid, pidx
    return False, file_id, page_index


# ---------------------------------------------------------------- tags

@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def split_list(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    items = re.split(r"[,;\n]", value) if isinstance(value, str) else [x for v in value for x in re.split(r"[,;\n]", str(v))]
    out: list[str] = []
    for x in items:
        x = x.strip()
        if x and x not in out:
            out.append(x)
    return out


def norm_case(conn: sqlite3.Connection, value: str) -> str:
    v = value.strip()
    for c in CASES:
        if v.upper() == c:
            return c
    r = conn.execute("SELECT id FROM cases WHERE lower(id) = lower(?) OR lower(label) = lower(?)", (v, v)).fetchone()
    return r[0] if r else v


def norm_concept(conn: sqlite3.Connection, value: str) -> str:
    v = value.strip()
    r = conn.execute("SELECT id FROM concepts WHERE lower(id) = lower(?) OR lower(label) = lower(?)", (v, v)).fetchone()
    return r[0] if r else re.sub(r"[^a-z0-9+]+", "-", v.lower()).strip("-")


def norm_camp(conn: sqlite3.Connection, value: str | None) -> str | None:
    v = (value or "").strip()
    if not v:
        return None
    r = conn.execute("SELECT id FROM camps WHERE lower(id) = lower(?) OR lower(label) = lower(?)", (v, v)).fetchone()
    if r:
        return r[0]
    return v.lower() if v.lower() in CAMPS else v


def _upsert_tag(conn: sqlite3.Connection, excerpt_id: str, dst_type: str, dst_id: str, stance: str | None = None) -> None:
    conn.execute(
        "INSERT INTO associations(src_type, src_id, rel, dst_type, dst_id, stance, origin, status, created_at) "
        "VALUES ('excerpt', ?, 'about', ?, ?, ?, 'user', 'accepted', ?) "
        "ON CONFLICT(src_type, src_id, rel, dst_type, dst_id) DO UPDATE SET status = 'accepted', "
        "stance = COALESCE(excluded.stance, associations.stance)",
        (excerpt_id, dst_type, dst_id, stance, now_iso()))


def _replace_tags(conn: sqlite3.Connection, excerpt_id: str, dst_type: str, wanted: dict[str, str | None]) -> None:
    """Make the accepted `about` tags of one kind equal `wanted` ({dst_id: stance})."""
    rows = conn.execute("SELECT id, dst_id, origin FROM associations WHERE src_type='excerpt' AND src_id=? AND rel='about' "
                        "AND dst_type=? AND status='accepted'", (excerpt_id, dst_type)).fetchall()
    for r in rows:
        if r["dst_id"] not in wanted:
            if r["origin"] == "user":
                conn.execute("DELETE FROM associations WHERE id = ?", (r["id"],))
            else:   # keep the trace of an accepted AI tag, and keep it from being proposed again
                conn.execute("UPDATE associations SET status='rejected', reject_reason='removed' WHERE id = ?", (r["id"],))
    for dst_id, stance in wanted.items():
        _upsert_tag(conn, excerpt_id, dst_type, dst_id, stance)


def _explanations(conn: sqlite3.Connection, explanations: Iterable[dict] | None) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for e in explanations or []:
        cid, stance = e.get("claim_id"), (e.get("stance") or None)
        if cid in (None, ""):
            continue
        if stance is not None and stance not in STANCES:
            raise ExcerptError(f"stance must be one of {', '.join(STANCES)}")
        if not conn.execute("SELECT 1 FROM claims WHERE id = ?", (int(cid),)).fetchone():
            raise ExcerptError(f"unknown claim {cid}")
        out[str(int(cid))] = stance
    return out


def tags(conn: sqlite3.Connection, excerpt_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT uses_json, camp FROM excerpts WHERE id = ?", (excerpt_id,)).fetchone()
    out: dict[str, Any] = {"uses": json.loads(row["uses_json"]) if row else [], "camp": row["camp"] if row else None,
                           "cases": [], "concepts": [], "explanations": []}
    for r in conn.execute(
            "SELECT a.dst_type, a.dst_id, a.stance, a.origin, c.code, c.statement FROM associations a "
            "LEFT JOIN claims c ON a.dst_type = 'claim' AND c.id = CAST(a.dst_id AS INTEGER) "
            "WHERE a.src_type='excerpt' AND a.src_id=? AND a.rel='about' AND a.status='accepted' ORDER BY a.id", (excerpt_id,)):
        if r["dst_type"] == "case":
            out["cases"].append(r["dst_id"])
        elif r["dst_type"] == "concept":
            out["concepts"].append(r["dst_id"])
        elif r["dst_type"] == "claim":
            out["explanations"].append({"claim_id": int(r["dst_id"]), "stance": r["stance"], "code": r["code"],
                                        "statement": r["statement"], "origin": r["origin"]})
    return out


# ---------------------------------------------------------------- create / update / status

def default_project(conn: sqlite3.Connection, paper_id: str) -> str | None:
    """The 课题 an excerpt of this paper is filed under: the current one when the paper is in it, else the paper's
    first. A paper in no 课题 gives none (card 0202): filing it under the current one sent its excerpts through that
    project's vocabulary."""
    slugs = [r[0] for r in conn.execute("SELECT project_slug FROM paper_projects WHERE paper_id = ? ORDER BY project_slug", (paper_id,))]
    if not slugs:
        return None
    now = datafiles.read("now") or {}
    now_project = str(now.get("project")) if now.get("project") else None
    return now_project if now_project in slugs else slugs[0]


def _validate_note(note: str) -> str:
    note = (note or "").strip()
    words = len(re.findall(r"[^\W\d_]{2,}", re.sub(r"[\u3400-\u9fff]", " ", note)))
    if words < 3 and len(re.findall(r"[\u3400-\u9fff]", note)) < 6:
        raise ExcerptError("my_note is required: at least one sentence in your own words on why this matters")
    return note


def _validate_choice(value: str | None, allowed: tuple[str, ...], name: str, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise ExcerptError(f"{name} is required")
        return None
    if value not in allowed:
        raise ExcerptError(f"{name} must be one of {', '.join(allowed)}")
    return value


def _validate_uses(uses: Iterable[str] | None) -> list[str]:
    out = []
    for u in split_list(list(uses or [])):
        if u not in USES:
            raise ExcerptError(f"use must be among {', '.join(USES)} (got {u!r})")
        if u not in out:
            out.append(u)
    return out


def create(conn: sqlite3.Connection, *, paper_id: str, quote: str, my_note: str, locator: str, kind: str = "quote",
           uses: Iterable[str] | None = None, camp: str | None = None, cases: Iterable[str] | str | None = None,
           concepts: Iterable[str] | str | None = None, explanations: Iterable[dict] | None = None,
           strength: str | None = None, source_type: str = "secondary-scholarly", project: str | None = None,
           file_id: int | None = None, page_index: int | None = None, private: bool = False) -> str:
    """Create an excerpt from user input and return its id (EXC-YYYY-NNNN)."""
    paper = conn.execute("SELECT id FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if paper is None:
        raise ExcerptError(f"unknown paper {paper_id!r}")
    quote = (quote or "").strip()
    if not quote:
        raise ExcerptError("quote is required (paste it from the source)")
    locator = (locator or "").strip()
    if not locator:
        raise ExcerptError("locator is required (page, paragraph or timestamp)")
    note = _validate_note(my_note)
    _validate_choice(kind, KINDS, "kind", required=True)
    _validate_choice(strength, STRENGTHS, "strength")
    _validate_choice(source_type, SOURCE_TYPES, "source_type", required=True)
    use_list = _validate_uses(uses)
    expl = _explanations(conn, explanations)
    verified, vfile, vpage = verify_quote(conn, paper_id, quote, file_id, page_index)
    if verified:
        file_id, page_index = vfile, vpage
    project = project or default_project(conn, paper_id)
    camp_n = norm_camp(conn, camp)
    ts = now_iso()
    with _tx(conn):
        excerpt_id = next_sequence_id(conn, "excerpts", "EXC")
        conn.execute(
            "INSERT INTO excerpts(id, paper_id, file_id, page_index, locator, kind, quote, quote_verified, uses_json, camp, "
            "my_note, strength, source_type, project_slug, status, private, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'inbox',?,?,?)",
            (excerpt_id, paper_id, file_id, page_index, locator, kind, quote, int(verified), json.dumps(use_list), camp_n,
             note, strength or None, source_type, project, int(bool(private)), ts, ts))
        if camp_n:
            _upsert_tag(conn, excerpt_id, "camp", camp_n)
        for c in split_list(cases):
            _upsert_tag(conn, excerpt_id, "case", norm_case(conn, c))
        for c in split_list(concepts):
            _upsert_tag(conn, excerpt_id, "concept", norm_concept(conn, c))
        for cid, stance in expl.items():
            _upsert_tag(conn, excerpt_id, "claim", cid, stance)
    return excerpt_id


UPDATABLE = ("quote", "my_note", "locator", "kind", "uses", "camp", "cases", "concepts", "explanations", "strength",
             "source_type", "project", "private", "file_id", "page_index")


def update(conn: sqlite3.Connection, excerpt_id: str, **fields: Any) -> None:
    """Edit fields a user changed. Tag lists replace the accepted tags of that kind."""
    row = get(conn, excerpt_id)
    if row is None:
        raise ExcerptError(f"unknown excerpt {excerpt_id}")
    unknown = set(fields) - set(UPDATABLE)
    if unknown:
        raise ExcerptError(f"cannot update {', '.join(sorted(unknown))}")
    cols: dict[str, Any] = {}
    if "quote" in fields:
        quote = (fields["quote"] or "").strip()
        if not quote:
            raise ExcerptError("quote is required")
        verified, vfile, vpage = verify_quote(conn, row["paper_id"], quote, fields.get("file_id", row["file_id"]),
                                              fields.get("page_index", row["page_index"]))
        cols.update(quote=quote, quote_verified=int(verified))
        if verified:
            cols.update(file_id=vfile, page_index=vpage)
    if "my_note" in fields:
        cols["my_note"] = _validate_note(fields["my_note"])
    if "locator" in fields:
        if not (fields["locator"] or "").strip():
            raise ExcerptError("locator is required")
        cols["locator"] = fields["locator"].strip()
    if "kind" in fields:
        cols["kind"] = _validate_choice(fields["kind"], KINDS, "kind", required=True)
    if "strength" in fields:
        cols["strength"] = _validate_choice(fields["strength"], STRENGTHS, "strength")
    if "source_type" in fields:
        cols["source_type"] = _validate_choice(fields["source_type"], SOURCE_TYPES, "source_type", required=True)
    if "uses" in fields:
        cols["uses_json"] = json.dumps(_validate_uses(fields["uses"]))
    if "project" in fields:
        cols["project_slug"] = fields["project"] or None
    if "private" in fields:
        cols["private"] = int(bool(fields["private"]))
    for k in ("file_id", "page_index"):
        if k in fields and k not in cols:
            cols[k] = fields[k]
    camp_n = norm_camp(conn, fields["camp"]) if "camp" in fields else None
    if "camp" in fields:
        cols["camp"] = camp_n
    expl = _explanations(conn, fields["explanations"]) if "explanations" in fields else None
    with _tx(conn):
        if cols:
            cols["updated_at"] = now_iso()
            conn.execute(f"UPDATE excerpts SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?", (*cols.values(), excerpt_id))
        if "camp" in fields:
            _replace_tags(conn, excerpt_id, "camp", {camp_n: None} if camp_n else {})
        if "cases" in fields:
            _replace_tags(conn, excerpt_id, "case", {norm_case(conn, c): None for c in split_list(fields["cases"])})
        if "concepts" in fields:
            _replace_tags(conn, excerpt_id, "concept", {norm_concept(conn, c): None for c in split_list(fields["concepts"])})
        if expl is not None:
            _replace_tags(conn, excerpt_id, "claim", expl)


def set_status(conn: sqlite3.Connection, excerpt_id: str, status: str) -> None:
    _validate_choice(status, STATUSES, "status", required=True)
    cur = conn.execute("UPDATE excerpts SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), excerpt_id))
    if cur.rowcount == 0:
        raise ExcerptError(f"unknown excerpt {excerpt_id}")


def discard(conn: sqlite3.Connection, excerpt_id: str) -> None:
    set_status(conn, excerpt_id, "discarded")


# ---------------------------------------------------------------- reads

SELECT_EXCERPT = ("SELECT e.*, p.title AS paper_title, p.year AS paper_year, p.authors_json AS paper_authors "
                  "FROM excerpts e JOIN papers p ON p.id = e.paper_id")


def get(conn: sqlite3.Connection, excerpt_id: str) -> sqlite3.Row | None:
    return conn.execute(SELECT_EXCERPT + " WHERE e.id = ?", (excerpt_id,)).fetchone()


def list_excerpts(conn: sqlite3.Connection, *, status: str | None = None, paper: str | None = None,
                  project: str | None = None, use: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
    sql, args = SELECT_EXCERPT + " WHERE 1=1", []
    if status:
        sql += " AND e.status = ?"; args.append(status)
    else:
        sql += " AND e.status != 'discarded'"
    if paper:
        sql += " AND e.paper_id = ?"; args.append(paper)
    if project:
        sql += " AND e.project_slug = ?"; args.append(project)
    if use:
        sql += " AND EXISTS (SELECT 1 FROM json_each(e.uses_json) WHERE value = ?)"; args.append(use)
    sql += " ORDER BY e.created_at DESC, e.id DESC LIMIT ?"; args.append(limit)
    return conn.execute(sql, args).fetchall()


def check(conn: sqlite3.Connection, excerpt_id: str) -> list[str]:
    """Soft warnings from the excerpt schema; they never block saving."""
    row = get(conn, excerpt_id)
    if row is None:
        return []
    t = tags(conn, excerpt_id)
    uses = set(t["uses"])
    out = []
    # A missing use tag is not flagged: a highlight is a passage plus a note, tags are optional.
    if uses & {"evidence", "counter"} and not t["explanations"]:
        out.append("标为证据或反证时，最好注明它支持或反驳哪个假设。")
    if "evidence" in uses and not row["strength"]:
        out.append("这条证据还没有标注强度。")
    if not re.search(r"\d", row["locator"] or ""):
        out.append("位置里没有页码，这条划线不能作为强证据。")
    if not row["quote_verified"] and row["kind"] in ("quote", "data"):
        out.append("在页面文本里没找到这段原文，可能是照纸本录入或文字识别有出入，请核对。")
    return out


# ---------------------------------------------------------------- suggestions (user actions)

def accept_suggestion(conn: sqlite3.Connection, assoc_id: int, *, reason: str | None = None,
                      relation: str | None = None) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM associations WHERE id = ?", (assoc_id,)).fetchone()
    if row is None:
        raise ExcerptError(f"unknown suggestion {assoc_id}")
    with _tx(conn):
        sets, args = ["status = 'accepted'", "reject_reason = NULL"], []
        if reason and reason.strip():
            sets.append("reason = ?"); args.append(" ".join(reason.split())[:400])
        if relation and relation != row["rel"] and row["rel"] != "about":
            if relation not in RELATIONS:
                raise ExcerptError(f"relation must be one of {', '.join(RELATIONS)}")
            clash = conn.execute("SELECT 1 FROM associations WHERE src_type=? AND src_id=? AND rel=? AND dst_type=? AND dst_id=?",
                                 (row["src_type"], row["src_id"], relation, row["dst_type"], row["dst_id"])).fetchone()
            if not clash:
                sets.append("rel = ?"); args.append(relation)
        conn.execute(f"UPDATE associations SET {', '.join(sets)} WHERE id = ?", (*args, assoc_id))
        if row["src_type"] == "excerpt":
            ex = conn.execute("SELECT uses_json, camp, status FROM excerpts WHERE id = ?", (row["src_id"],)).fetchone()
            if ex:
                if row["rel"] == "about" and row["dst_type"] == "use":
                    uses = json.loads(ex["uses_json"])
                    if row["dst_id"] not in uses:
                        uses.append(row["dst_id"])
                        conn.execute("UPDATE excerpts SET uses_json = ? WHERE id = ?", (json.dumps(uses), row["src_id"]))
                if row["rel"] == "about" and row["dst_type"] == "camp" and not ex["camp"]:
                    conn.execute("UPDATE excerpts SET camp = ? WHERE id = ?", (row["dst_id"], row["src_id"]))
                if ex["status"] == "inbox":
                    conn.execute("UPDATE excerpts SET status = 'linked', updated_at = ? WHERE id = ?", (now_iso(), row["src_id"]))
    return conn.execute("SELECT * FROM associations WHERE id = ?", (assoc_id,)).fetchone()


def reject_suggestion(conn: sqlite3.Connection, assoc_id: int, reason: str | None = None) -> sqlite3.Row:
    if reason in ("", "none"):
        reason = None
    if reason is not None and reason not in REJECT_REASONS:
        raise ExcerptError(f"reason must be one of {', '.join(REJECT_REASONS)}")
    cur = conn.execute("UPDATE associations SET status = 'rejected', reject_reason = ? WHERE id = ?", (reason, assoc_id))
    if cur.rowcount == 0:
        raise ExcerptError(f"unknown suggestion {assoc_id}")
    return conn.execute("SELECT * FROM associations WHERE id = ?", (assoc_id,)).fetchone()


def accept_claim(conn: sqlite3.Connection, claim_id: int, text: str | None = None) -> sqlite3.Row:
    """"Use as claim": the user adopts (optionally edits) a proposed claim. It does not touch any file."""
    row = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    if row is None:
        raise ExcerptError(f"unknown claim {claim_id}")
    statement = " ".join((text or row["statement"]).split())
    if not statement:
        raise ExcerptError("claim text is empty")
    conn.execute("UPDATE claims SET statement = ?, status = 'accepted' WHERE id = ?", (statement, claim_id))
    return conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()


def reject_claim(conn: sqlite3.Connection, claim_id: int) -> None:
    conn.execute("UPDATE claims SET status = 'rejected' WHERE id = ? AND status = 'proposed'", (claim_id,))


def send_next_question(text: str) -> bool:
    text = " ".join((text or "").split())
    if not text:
        raise ExcerptError("the question is empty")
    return datafiles.append_need_to_find_out(text)


# ---------------------------------------------------------------- association job

def enqueue_associate(conn: sqlite3.Connection, excerpt_id: str) -> str | None:
    """Queue `excerpt.associate` (reusing an open job) and hand it to the dispatcher if one exists."""
    from .ai import queue
    open_job = conn.execute("SELECT id FROM ai_jobs WHERE kind = 'excerpt.associate' AND target_id = ? "
                            "AND status IN ('queued','claimed') ORDER BY created_at DESC LIMIT 1", (excerpt_id,)).fetchone()
    if open_job:
        return open_job[0]
    job_id = queue.enqueue(conn, "excerpt.associate", excerpt_id)
    if job_id:
        try:
            from .ai import dispatch
            dispatch.dispatch(job_id)
        except Exception:  # noqa: BLE001 - the excerpt is saved; a routing failure must not lose it
            log.exception("dispatch of %s failed", job_id)
    return job_id


# ---------------------------------------------------------------- add to this week's artifact

def current_week_artifact(conn: sqlite3.Connection, week: str | None = None) -> sqlite3.Row | None:
    art_files.index(conn)
    return conn.execute("SELECT * FROM artifacts WHERE week = ? ORDER BY ref DESC LIMIT 1", (week or week_of(),)).fetchone()


def suggested_artifact_type(conn: sqlite3.Connection, excerpt_id: str) -> str:
    rows = conn.execute("SELECT dst_type, COUNT(*) n FROM associations WHERE src_type='excerpt' AND src_id=? AND status='accepted' "
                        "GROUP BY dst_type ORDER BY n DESC, dst_type", (excerpt_id,)).fetchall()
    known = {str(t["key"]) for t in datafiles.read("types") or []}
    for r in rows:
        t = TYPE_FOR_TARGET.get(r["dst_type"])
        if t and (not known or t in known):
            return t
    return "evidence"


def artifact_line(row: sqlite3.Row) -> str:
    author = first_author(row["paper_authors"], (row["paper_title"] or "Anon.").split(":")[0][:40])
    year = row["paper_year"] or "n.d."
    text = one_line(row["quote"], QUOTE_IN_ARTIFACT)
    if row["kind"] == "quote":
        text = f"“{text}”"
    return f"- {author} ({year}, {locator_label(row['locator'])}): {text} <!-- rl:{row['id']} -->"


def add_to_artifact(conn: sqlite3.Connection, excerpt_id: str, section: str = "Evidence",
                    artifact_ref: str | None = None, type_: str | None = None) -> tuple[Path, str]:
    """Append the excerpt's marker line to a section of this week's artifact (creating a Seed if the
    week has none). Returns (path, ref). Refuses the earned sections."""
    section = (section or "Evidence").strip()
    for earned in art_files.EARNED_SECTIONS:
        if section.lower().startswith(earned.lower()) or earned.lower().startswith(section.lower()):
            raise ExcerptError(f"the app never writes into “{earned}”; that section is yours")
    row = get(conn, excerpt_id)
    if row is None:
        raise ExcerptError(f"unknown excerpt {excerpt_id}")
    if row["private"]:
        raise ExcerptError("private excerpts stay out of artifacts")
    paths = get_paths()
    if artifact_ref:
        art_files.index(conn)
        target = conn.execute("SELECT * FROM artifacts WHERE ref = ?", (artifact_ref,)).fetchone()
        if target is None:
            raise ExcerptError(f"unknown artifact {artifact_ref}")
        path = paths.root / target["path"]
    else:
        target = current_week_artifact(conn)
        if target is not None:
            path = paths.root / target["path"]
        else:
            type_ = type_ or suggested_artifact_type(conn, excerpt_id)
            project = row["project_slug"] or str((datafiles.read("now") or {}).get("project") or "")
            label = next((str(t.get("label")) for t in datafiles.read("types") or [] if t.get("key") == type_), type_)
            slug, n = type_, 1
            while True:
                try:
                    path = art_files.create(type_, slug, project, title=f"{label} seed")
                    break
                except FileExistsError:
                    n += 1
                    slug = f"{type_}-{n}"
    a = art_files.load(path)
    if f"rl:{excerpt_id}" not in a.body:
        art_files.append_to_section(a, section, artifact_line(row))
        art_files.save(a)
    art_files.index(conn)
    set_status(conn, excerpt_id, "used")
    return path, a.ref
