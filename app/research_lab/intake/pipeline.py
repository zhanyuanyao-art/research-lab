"""Intake pipeline (architecture §6) and the paper-record operations the CLI and web share."""
from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..config import get_paths
from ..db.connection import get_setting, now_iso
from . import dedupe, directions, extract, metadata, sections
from .extract import RejectedFile  # noqa: F401 - re-exported for callers

log = logging.getLogger("research_lab.intake")

SUMMARY_KIND = "paper.summarize"
TRANSLATE_KIND = "paper.translate"
AUTO_TRANSLATE_SETTING = "ai.auto_translate_cjk"
LANG_NAMES = {"zh": "中文", "en": "英文", "ja": "日文", "ko": "韩文", "other": "其他"}
TRANSLATE_TARGETS = ("en", "zh")
KINDS = ("article", "book", "chapter", "report", "other")
READ_DEPTHS = ("abstract", "skimmed", "read", "reread")
RELATIONS = ("supports", "rivals", "scope-condition", "irrelevant")
USER_FIELDS = ("camp", "claim_text", "mechanism", "period", "method", "level_of_analysis", "concept_usage",
               "cannot_explain", "relation_to_puzzle", "read_depth", "direction")
# user-owned column -> (summary field, key inside it)
ADOPT_MAP = {
    "camp": ("camp_guess", "camp"),
    "claim_text": ("argument", "text"),
    "mechanism": ("mechanism", "text"),
    "cannot_explain": ("cannot_explain_guess", "text"),
    "concept_usage": ("concept_usage", "text"),
    "method": ("method", "text"),
    "period": ("period", "text"),
    "level_of_analysis": ("level_of_analysis", "text"),
    "direction": ("direction", "slug"),
}
BOOK_PAGES = 120


@dataclass
class IngestResult:
    paper_id: str | None
    created: bool
    duplicate_of: str | None
    job_id: str | None
    pending: str | None = None          # sha12 of a file held until the user answers "same paper?"
    reason: str | None = None           # same file | same DOI | similar title
    metadata_status: str | None = None
    notes: list[str] = field(default_factory=list)
    translate_jobs: list[str] = field(default_factory=list)
    language: str | None = None


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = doi.strip()
    d = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", d, flags=re.I)
    return d.lower() or None


def _pending_dir() -> Path:
    d = get_paths().cache / "incoming"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pending_info(sha12: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[0-9a-f]{12}", sha12 or ""):
        return None
    meta = _pending_dir() / f"pending-{sha12}.json"
    pdf = _pending_dir() / f"pending-{sha12}.pdf"
    if not (meta.exists() and pdf.exists()):
        return None
    info = json.loads(meta.read_text())
    info["path"] = str(pdf)
    return info


def _hold(path: Path, sha: str, info: dict[str, Any]) -> str:
    sha12 = sha[:12]
    shutil.copy2(path, _pending_dir() / f"pending-{sha12}.pdf")
    (_pending_dir() / f"pending-{sha12}.json").write_text(json.dumps(info))
    return sha12


def _drop_pending(sha12: str) -> None:
    for suffix in (".pdf", ".json"):
        (_pending_dir() / f"pending-{sha12}{suffix}").unlink(missing_ok=True)


def _clues(ex: extract.Extracted, user_doi: str | None) -> metadata.Clues:
    return metadata.Clues(title_guess=ex.title_guess, author_guess=ex.author_guess, dois=ex.dois,
                          years=ex.years, head_text=ex.head_text, user_doi=user_doi)


def _lookup_state(res: metadata.Resolution, title_guess: str | None) -> dict[str, Any]:
    return {"_lookup": {"candidates": [c.to_dict() for c in res.candidates], "query": res.query,
                        "title_guess": title_guess, "errors": res.errors, "at": now_iso()}}


def lookup_state(paper: sqlite3.Row | dict) -> dict[str, Any] | None:
    """Candidates and errors of the last unverified lookup, stored under crossref_json._lookup."""
    raw = paper["crossref_json"]
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data.get("_lookup") if isinstance(data, dict) else None


def _insert_file(conn: sqlite3.Connection, paper_id: str, src: Path, sha: str, ex: extract.Extracted,
                 original_name: str | None, is_primary: bool) -> int:
    paths = get_paths()
    rel = f"papers/{paper_id}/{sha[:12]}.pdf"
    dest = paths.data / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    cur = conn.execute(
        "INSERT INTO paper_files(paper_id, sha256, rel_path, original_name, bytes, page_count, has_text_layer, extractor, is_primary, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (paper_id, sha, rel, original_name, dest.stat().st_size, ex.page_count, int(ex.has_text_layer), ex.extractor,
         int(is_primary), now_iso()))
    file_id = int(cur.lastrowid)
    conn.executemany("INSERT INTO paper_pages(file_id, page_index, page_label, text) VALUES (?,?,?,?)",
                     [(file_id, p.index, p.label, p.text) for p in ex.pages])
    conn.executemany("INSERT INTO paper_sections(file_id, kind, heading, page_start, page_end) VALUES (?,?,?,?,?)",
                     [(file_id, s.kind, s.heading, s.page_start, s.page_end) for s in sections.detect(ex.pages)])
    return file_id


def find_existing(conn: sqlite3.Connection, path: Path) -> dict | None:
    """Read-only identity / candidate lookup; a cited DOI is not proof of identity."""
    extract.check_pdf(path)
    same = dedupe.by_sha(conn, extract.sha256_file(path))
    if same:
        return {'paper_id': same, 'confirmed': True}
    ex = extract.extract(path)
    same = dedupe.fuzzy_match(conn, ex.title_guess, [], None) if ex.title_guess else None
    if same:
        return {'paper_id': same, 'confirmed': True}
    if len(ex.dois) == 1:
        candidate = dedupe.by_doi(conn, ex.dois[0])
        if candidate:
            return {'paper_id': candidate, 'confirmed': False}
    return None


def ingest(conn: sqlite3.Connection, path: str | Path, *, project: str | None = None, doi: str | None = None,
           http: httpx.Client | None = None, ai: bool = True, allow_duplicate: bool = False,
           original_name: str | None = None, direction: str | None = None, hold_duplicate: bool = True) -> IngestResult:
    """Raises RejectedFile for anything that is not a readable, unencrypted PDF under 100 MB.

    Every new paper gets a 方向 (docs/design/PAPERS.md §B): the one given, else the given 课题's, else the
    current project's (now.yml), so nothing lands in 未归档 unless no direction exists. It joins a 课题 only when
    one is given: the current project no longer claims every import (card 0130, LIBRARY-UI §八 3)."""
    path = Path(path)
    original_name = original_name or path.name
    extract.check_pdf(path)
    sha = extract.sha256_file(path)
    same = dedupe.by_sha(conn, sha)
    if same:
        return IngestResult(same, False, same, None, reason="same file")

    ex = extract.extract(path)
    user_doi = normalize_doi(doi)
    res = metadata.resolve(conn, _clues(ex, user_doi), http=http)
    # A 404 only means "no record for this DOI/title"; connection failures are worth a note.
    down = sorted({e.split(":", 1)[0] for e in res.errors if "HTTP 404" not in e})
    notes = [f"暂时连不上 {'、'.join(down)}，引用信息稍后可在文献页重新核验"] if down else []
    if not ex.has_text_layer:
        notes.append("前几页没有文本层，可能是扫描件")
    if extract.is_cjk(ex.language) and res.status != "verified":
        notes.append("Crossref 对中文期刊覆盖有限，引用信息保持待核验，可在文献页手动填写")

    if res.status == "verified" and res.chosen:
        c = res.chosen
        title, authors, year, venue, kind, paper_doi = c.title, c.authors, c.year, c.venue, c.kind, c.doi or user_doi
    else:
        title = ex.title_guess or Path(original_name).stem.replace("_", " ")
        authors = [a.strip() for a in (ex.author_guess or "").split(";") if a.strip()]
        year, venue, kind, paper_doi = None, None, None, user_doi
    kind = kind or ("book" if ex.page_count > BOOK_PAGES or ex.isbns else "article")
    direction = directions.clean(direction) if direction else None
    direction = direction or project_direction(project)      # no 课题 given, no direction guessed (card 0202)

    if not allow_duplicate:
        dup, why = dedupe.by_doi(conn, paper_doi), "same DOI"
        if not dup:
            dup, why = dedupe.fuzzy_match(conn, title, authors, year), "similar title"
        if dup:
            if not hold_duplicate:
                return IngestResult(None, False, dup, None, reason=why, notes=notes)
            sha12 = _hold(path, sha, {"duplicate_of": dup, "project": project, "doi": user_doi, "direction": direction,
                                      "original_name": original_name, "title": title, "reason": why})
            return IngestResult(None, False, dup, None, pending=sha12, reason=why, notes=notes)
    elif dedupe.by_doi(conn, paper_doi):
        notes.append("这个 DOI 已属于文献库里的另一篇，这份副本不再记录 DOI")
        paper_doi = None

    status = res.status
    paper_id = dedupe.make_citekey(conn, authors, year, title)
    crossref_json = json.dumps(res.crossref) if res.crossref else (
        None if res.status == "verified" else json.dumps(_lookup_state(res, ex.title_guess)))
    ts = now_iso()
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO papers(id, doi, isbn, openalex_id, title, title_hash, authors_json, year, venue, kind, abstract, "
            "metadata_status, crossref_json, openalex_json, language, direction, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (paper_id, paper_doi, ex.isbns[0] if ex.isbns else None, res.openalex_id, title, dedupe.title_hash(title),
             json.dumps(authors, ensure_ascii=False), year, venue, kind, res.abstract, status, crossref_json,
             json.dumps(res.openalex) if res.openalex else None, ex.language, direction, ts, ts))
        _insert_file(conn, paper_id, path, sha, ex, original_name, True)
        if project:
            conn.execute("INSERT OR IGNORE INTO paper_projects(paper_id, project_slug) VALUES (?,?)", (paper_id, project))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        shutil.rmtree(get_paths().papers / paper_id, ignore_errors=True)
        raise
    job_id = enqueue_summary(conn, paper_id) if ai else None
    translate_jobs: list[str] = []
    if ai and extract.is_cjk(ex.language) and ex.has_text_layer and get_setting(conn, AUTO_TRANSLATE_SETTING, True):
        try:
            translate_jobs = enqueue_translation(conn, paper_id, "en")
        except Exception:  # noqa: BLE001 - a failed translation queue must not undo the intake
            log.exception("auto-translation of %s could not be queued", paper_id)
    return IngestResult(paper_id, True, None, job_id, metadata_status=status, notes=notes,
                        translate_jobs=translate_jobs, language=ex.language)


def attach_pending(conn: sqlite3.Connection, sha12: str, paper_id: str, *, ai: bool = True) -> int:
    """'Same paper' answer: store the held file with an existing paper. A bibliography-only record
    takes it as its primary file (pages appear, a summary is queued); otherwise it is a second file."""
    info = pending_info(sha12)
    if info is None:
        raise LookupError(f"no held file {sha12}")
    if not conn.execute("SELECT 1 FROM papers WHERE id = ?", (paper_id,)).fetchone():
        raise LookupError(paper_id)
    file_id, _job = _attach(conn, Path(info["path"]), paper_id, info.get("original_name"), ai=ai)
    _drop_pending(sha12)
    return file_id


def attach_file(conn: sqlite3.Connection, path: str | Path, paper_id: str, *, original_name: str | None = None,
                ai: bool = True) -> tuple[int, str | None]:
    """补 PDF: give an existing record this PDF directly (no duplicate question: Leo chose the record).
    Returns (file id, summary job id or None). Raises RejectedFile, LookupError, ValueError."""
    path = Path(path)
    extract.check_pdf(path)
    if get_paper(conn, paper_id) is None:
        raise LookupError(paper_id)
    owner = dedupe.by_sha(conn, extract.sha256_file(path))
    if owner == paper_id:
        raise ValueError("这份 PDF 已经是这篇文献的文件。")
    if owner:
        raise ValueError(f"这份 PDF 已经属于文献库里的另一篇（{owner}）。")
    return _attach(conn, path, paper_id, original_name or path.name, ai=ai)


def _attach(conn: sqlite3.Connection, src: Path, paper_id: str, original_name: str | None, *, ai: bool) -> tuple[int, str | None]:
    sha = extract.sha256_file(src)
    ex = extract.extract(src)
    first = primary_file(conn, paper_id) is None
    conn.execute("BEGIN")
    try:
        file_id = _insert_file(conn, paper_id, src, sha, ex, original_name, first)
        if first:
            conn.execute("UPDATE papers SET language = COALESCE(language, ?), updated_at = ? WHERE id = ?",
                         (ex.language, now_iso(), paper_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    paper = get_paper(conn, paper_id)
    job_id = enqueue_summary(conn, paper_id) if ai and first and not paper["ai_summary_json"] else None
    return file_id, job_id


def _refuse_with_excerpts(conn: sqlite3.Connection, paper_id: str) -> None:
    n = conn.execute("SELECT COUNT(*) FROM excerpts WHERE paper_id = ?", (paper_id,)).fetchone()[0]
    if n:
        raise ValueError(f"这篇已有 {n} 条划线，它们指向现在这份文件的页码；先处理划线再换文件。")


def _delete_file_rows(conn: sqlite3.Connection, file_id: int) -> None:
    # Explicit rather than relying on ON DELETE CASCADE, so the pages_fts trigger fires on every connection.
    conn.execute("DELETE FROM paper_translations WHERE file_id = ?", (file_id,))
    conn.execute("DELETE FROM paper_sections WHERE file_id = ?", (file_id,))
    conn.execute("DELETE FROM paper_pages WHERE file_id = ?", (file_id,))
    conn.execute("DELETE FROM paper_files WHERE id = ?", (file_id,))


def _unlink_file(row: sqlite3.Row) -> None:
    p = get_paths().data / str(row["rel_path"])
    try:
        p.unlink(missing_ok=True)
    except OSError:
        log.warning("could not remove %s", p)


def _cancel_queued_summaries(conn: sqlite3.Connection, paper_id: str) -> None:
    from ..ai import queue
    for row in conn.execute("SELECT id FROM ai_jobs WHERE kind = ? AND target_id = ? AND status = 'queued'",
                            (SUMMARY_KIND, paper_id)).fetchall():
        queue.cancel(conn, row["id"])


def replace_primary_file(conn: sqlite3.Connection, path: str | Path, paper_id: str, *, original_name: str | None = None,
                         ai: bool = True) -> tuple[int, str | None]:
    """换原文 (card 0175): the chosen PDF becomes the 原文 and the file it replaces goes, with its pages, sections
    and translations; the summary read from the old file is cleared and a new one queued. Refused while the paper
    has highlights, whose page references point into the old file. Returns (file id, summary job id or None)."""
    path = Path(path)
    extract.check_pdf(path)
    if get_paper(conn, paper_id) is None:
        raise LookupError(paper_id)
    _refuse_with_excerpts(conn, paper_id)
    sha = extract.sha256_file(path)
    owner = dedupe.by_sha(conn, sha)
    if owner == paper_id:
        raise ValueError("这份 PDF 已经是这篇文献的文件。")
    if owner:
        raise ValueError(f"这份 PDF 已经属于文献库里的另一篇（{owner}）。")
    old = primary_file(conn, paper_id)
    ex = extract.extract(path)
    _cancel_queued_summaries(conn, paper_id)
    conn.execute("BEGIN")
    try:
        file_id = _insert_file(conn, paper_id, path, sha, ex, original_name or path.name, True)
        if old is not None:
            _delete_file_rows(conn, int(old["id"]))
        conn.execute("UPDATE papers SET language = COALESCE(?, language), ai_summary_json = NULL, ai_summary_run_id = NULL, "
                     "updated_at = ? WHERE id = ?", (ex.language, now_iso(), paper_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if old is not None:
        _unlink_file(old)
    job_id = enqueue_summary(conn, paper_id, force=True) if ai else None
    return file_id, job_id


def remove_file(conn: sqlite3.Connection, paper_id: str, file_id: int) -> None:
    """移除 one file of a paper (card 0175), with its pages, sections and translations. When it was the 原文 the
    oldest remaining file takes over and the summary is cleared, since it was read from the removed file. Refused
    while the paper has highlights."""
    row = conn.execute("SELECT * FROM paper_files WHERE id = ? AND paper_id = ?", (file_id, paper_id)).fetchone()
    if row is None:
        raise LookupError(f"{paper_id}/{file_id}")
    _refuse_with_excerpts(conn, paper_id)
    if row["is_primary"]:
        _cancel_queued_summaries(conn, paper_id)
    conn.execute("BEGIN")
    try:
        _delete_file_rows(conn, int(row["id"]))
        if row["is_primary"]:
            nxt = conn.execute("SELECT id FROM paper_files WHERE paper_id = ? ORDER BY id LIMIT 1", (paper_id,)).fetchone()
            if nxt is not None:
                conn.execute("UPDATE paper_files SET is_primary = 1 WHERE id = ?", (nxt["id"],))
            conn.execute("UPDATE papers SET ai_summary_json = NULL, ai_summary_run_id = NULL, updated_at = ? WHERE id = ?",
                         (now_iso(), paper_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    _unlink_file(row)


def project_direction(project: str | None) -> str | None:
    """The 方向 a 课题 sits under in projects.yml, if it names a known one."""
    if not project:
        return None
    from ..files import datafiles
    try:
        entry = datafiles.project(project) or {}
    except Exception:  # noqa: BLE001 - a broken projects.yml must not block intake
        return None
    return directions.clean(str(entry.get("direction") or "")) or None


def summarize_missing(conn: sqlite3.Connection, *, project: str | None = None) -> dict[str, int]:
    """Queue a summary for every paper (in one 课题, or all) that has a PDF and no summary or open job.
    Returns counts: queued, waiting (already queued or running), no_file (bibliography only)."""
    where, args = ["p.ai_summary_json IS NULL"], []
    if project:
        where.append("p.id IN (SELECT paper_id FROM paper_projects WHERE project_slug = ?)")
        args.append(project)
    rows = conn.execute("SELECT p.id, (SELECT COUNT(*) FROM paper_files f WHERE f.paper_id = p.id) AS n_files, "
                        "(SELECT COUNT(*) FROM ai_jobs j WHERE j.kind = ? AND j.target_id = p.id AND j.status IN ('queued','claimed')) AS n_open "
                        f"FROM papers p WHERE {' AND '.join(where)} ORDER BY p.created_at", [SUMMARY_KIND, *args]).fetchall()
    out = {"queued": 0, "waiting": 0, "no_file": 0}
    for r in rows:
        if not r["n_files"]:
            out["no_file"] += 1
        elif r["n_open"]:
            out["waiting"] += 1
        elif enqueue_summary(conn, r["id"]):
            out["queued"] += 1
    return out


def import_pending(conn: sqlite3.Connection, sha12: str, *, http: httpx.Client | None = None, ai: bool = True) -> IngestResult:
    """'Different paper' answer: ingest the held file as its own paper."""
    info = pending_info(sha12)
    if info is None:
        raise LookupError(f"no held file {sha12}")
    result = ingest(conn, info["path"], project=info.get("project"), doi=info.get("doi"), http=http, ai=ai,
                    allow_duplicate=True, original_name=info.get("original_name"), direction=info.get("direction"))
    _drop_pending(sha12)
    return result


# ---------------------------------------------------------------- metadata after intake

def get_paper(conn: sqlite3.Connection, paper_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()


def primary_file(conn: sqlite3.Connection, paper_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM paper_files WHERE paper_id = ? ORDER BY is_primary DESC, id LIMIT 1",
                        (paper_id,)).fetchone()


def load_pages(conn: sqlite3.Connection, file_id: int) -> list[extract.PageText]:
    return [extract.PageText(r["page_index"], r["page_label"] or str(r["page_index"] + 1), r["text"]) for r in
            conn.execute("SELECT page_index, page_label, text FROM paper_pages WHERE file_id = ? ORDER BY page_index", (file_id,))]


def load_sections(conn: sqlite3.Connection, file_id: int) -> list[sections.Section]:
    """Stored rows lose the heading line number, so it is recovered from the page text."""
    pages = {p.index: p for p in load_pages(conn, file_id)}
    out = []
    for r in conn.execute("SELECT kind, heading, page_start, page_end FROM paper_sections WHERE file_id = ? ORDER BY page_start, rowid", (file_id,)):
        line_no = -1
        page = pages.get(r["page_start"])
        if r["heading"] and page:
            for n, line in enumerate(page.text.splitlines()):
                if line.strip() == r["heading"]:
                    line_no = n
                    break
        out.append(sections.Section(r["kind"], r["heading"], r["page_start"], r["page_end"], line_no))
    return out


def _apply_verified(conn: sqlite3.Connection, paper_id: str, res: metadata.Resolution) -> list[str]:
    c = res.chosen
    assert c is not None
    other = dedupe.by_doi(conn, c.doi)
    if other and other != paper_id:
        return [f"DOI {c.doi} already belongs to {other}"]
    paper = get_paper(conn, paper_id)
    conn.execute(
        "UPDATE papers SET doi=?, title=?, title_hash=?, authors_json=?, year=?, venue=?, kind=COALESCE(?, kind), "
        "abstract=COALESCE(?, abstract), openalex_id=COALESCE(?, openalex_id), metadata_status='verified', "
        "crossref_json=?, openalex_json=COALESCE(?, openalex_json), updated_at=? WHERE id=?",
        (c.doi or paper["doi"], c.title, dedupe.title_hash(c.title), json.dumps(c.authors, ensure_ascii=False), c.year,
         c.venue, c.kind, res.abstract, res.openalex_id,
         json.dumps(res.crossref) if res.crossref else json.dumps({"_accepted_candidate": c.to_dict()}),
         json.dumps(res.openalex) if res.openalex else None, now_iso(), paper_id))
    return []


def lookup(conn: sqlite3.Connection, paper_id: str, *, http: httpx.Client | None = None) -> metadata.Resolution:
    """Re-run metadata resolution from the stored first pages; `rl intake lookup` and the Look-up button."""
    paper = get_paper(conn, paper_id)
    if paper is None:
        raise LookupError(paper_id)
    f = primary_file(conn, paper_id)
    pages = load_pages(conn, f["id"]) if f else []
    head = "\n".join(p.text for p in pages[:2])
    state = lookup_state(paper) or {}
    authors = json.loads(paper["authors_json"] or "[]")
    clues = metadata.Clues(
        title_guess=paper["title"] if paper["metadata_status"] == "verified" else (state.get("title_guess") or paper["title"]),
        author_guess="; ".join(authors) or None, dois=extract.find_dois(head),
        years=sorted({int(y) for y in extract.YEAR_RE.findall(head)} | ({paper["year"]} if paper["year"] else set())),
        head_text=head, user_doi=paper["doi"])
    res = metadata.resolve(conn, clues, http=http, refresh=True)
    if res.status == "verified" and res.chosen:
        res.errors += _apply_verified(conn, paper_id, res)
        if res.errors and dedupe.by_doi(conn, res.chosen.doi) not in (None, paper_id):
            res.status = "unverified"
    if res.status != "verified" and paper["metadata_status"] != "verified":
        conn.execute("UPDATE papers SET crossref_json=?, updated_at=? WHERE id=?",
                     (json.dumps(_lookup_state(res, clues.title_guess)), now_iso(), paper_id))
    return res


def accept_candidate(conn: sqlite3.Connection, paper_id: str, index: int, *, http: httpx.Client | None = None) -> list[str]:
    paper = get_paper(conn, paper_id)
    if paper is None:
        raise LookupError(paper_id)
    cands = (lookup_state(paper) or {}).get("candidates") or []
    if not 0 <= index < len(cands):
        raise IndexError(index)
    cand = metadata.Candidate.from_dict(cands[index])
    res = metadata.fetch_for_doi(conn, cand.doi, http=http) if cand.doi else metadata.Resolution("unverified")
    if res.status != "verified" or res.chosen is None:
        if not cand.doi:
            return ["candidate has no DOI; it cannot be verified"]
        res = metadata.Resolution("verified", cand, [cand])   # the candidate itself is a Crossref record
    return _apply_verified(conn, paper_id, res)


def update_metadata(conn: sqlite3.Connection, paper_id: str, *, title: str, authors: list[str], year: int | None,
                    venue: str | None, kind: str | None, doi: str | None) -> None:
    paper = get_paper(conn, paper_id)
    if paper is None:
        raise LookupError(paper_id)
    title = title.strip()
    if not title:
        raise ValueError("title is required")
    if kind and kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    doi = normalize_doi(doi)
    other = dedupe.by_doi(conn, doi)
    if other and other != paper_id:
        raise ValueError(f"DOI {doi} already belongs to {other}")
    status = paper["metadata_status"] if (doi or None) == (paper["doi"] or None) else "unverified"
    conn.execute("UPDATE papers SET title=?, title_hash=?, authors_json=?, year=?, venue=?, kind=?, doi=?, "
                 "metadata_status=?, updated_at=? WHERE id=?",
                 (title, dedupe.title_hash(title), json.dumps(authors, ensure_ascii=False), year, venue or None,
                  kind or None, doi, status, now_iso(), paper_id))


def update_user_fields(conn: sqlite3.Connection, paper_id: str, values: dict[str, str | None],
                       projects: list[str] | None = None) -> None:
    sets, args = [], []
    for k, v in values.items():
        if k not in USER_FIELDS:
            continue
        v = (v or "").strip() or None
        if k == "read_depth" and v not in (None, *READ_DEPTHS):
            raise ValueError("read_depth must be one of " + ", ".join(READ_DEPTHS))
        if k == "relation_to_puzzle" and v not in (None, *RELATIONS):
            raise ValueError("relation_to_puzzle must be one of " + ", ".join(RELATIONS))
        if k == "direction" and v is not None and directions.clean(v) is None:
            raise ValueError(f"未知方向 {v}")
        sets.append(f"{k} = ?")
        args.append(v)
    conn.execute("BEGIN")
    try:
        if sets:
            conn.execute(f"UPDATE papers SET {', '.join(sets)}, updated_at = ? WHERE id = ?", (*args, now_iso(), paper_id))
        if projects is not None:
            conn.execute("DELETE FROM paper_projects WHERE paper_id = ?", (paper_id,))
            conn.executemany("INSERT OR IGNORE INTO paper_projects(paper_id, project_slug) VALUES (?,?)",
                             [(paper_id, p) for p in projects if p])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def summary_of(paper: sqlite3.Row | dict) -> dict[str, Any] | None:
    raw = paper["ai_summary_json"]
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def adopt(conn: sqlite3.Connection, paper_id: str, column: str) -> str:
    """Copy one AI summary field into the user-owned column; only ever on a user click."""
    if column not in ADOPT_MAP:
        raise KeyError(column)
    paper = get_paper(conn, paper_id)
    summary = summary_of(paper) if paper else None
    if not summary:
        raise LookupError("还没有可采纳的 AI 摘要")
    src, key = ADOPT_MAP[column]
    value = (summary.get(src) or {}).get(key) if isinstance(summary.get(src), dict) else summary.get(src)
    if not value:
        raise LookupError(f"摘要里没有 {src}")
    if column == "direction" and directions.clean(value) is None:
        raise LookupError(f"建议的方向 {value} 不在 directions.yml 里")
    conn.execute(f"UPDATE papers SET {column} = ?, updated_at = ? WHERE id = ?", (str(value).strip(), now_iso(), paper_id))
    return str(value)


# ---------------------------------------------------------------- AI summary job

def latest_summary_job(conn: sqlite3.Connection, paper_id: str) -> sqlite3.Row | None:
    """Job ids increase within a day, so they break created_at ties (same-second re-summarize)."""
    return conn.execute("SELECT * FROM ai_jobs WHERE kind = ? AND target_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                        (SUMMARY_KIND, paper_id)).fetchone()


def enqueue_summary(conn: sqlite3.Connection, paper_id: str, *, force: bool = False, mode: str | None = None) -> str | None:
    """Queue paper.summarize and hand it to the dispatcher. `force` replaces a still-queued job. mode light | full
    overrides the setting papers.summary_mode for this one job (the paper page's Full summary is full). A forced
    rerun without a mode keeps a full summary full (card 0126); a light one follows the setting."""
    from ..ai import queue
    from ..ai.jobs import paper_summarize
    paper_summarize.refresh_queued(conn)        # waiting jobs built before light reading follow the setting now
    paper = get_paper(conn, paper_id) if force and mode is None else None
    if paper is not None and paper_summarize.depth_of(summary_of(paper)) == "full":
        mode = "full"
    if force:
        for row in conn.execute("SELECT id FROM ai_jobs WHERE kind = ? AND target_id = ? AND status = 'queued'",
                                (SUMMARY_KIND, paper_id)).fetchall():
            queue.cancel(conn, row["id"])
    with paper_summarize.forced_mode(mode):
        job_id = queue.enqueue(conn, SUMMARY_KIND, paper_id)
        if job_id is None and force:
            job_id = queue.enqueue(conn, SUMMARY_KIND, paper_id, force=True)
    if job_id is None:
        row = conn.execute("SELECT id FROM ai_jobs WHERE kind = ? AND target_id = ? AND status IN ('queued','claimed') "
                           "ORDER BY created_at DESC LIMIT 1", (SUMMARY_KIND, paper_id)).fetchone()
        return row["id"] if row else None
    from ..ai import dispatch
    try:
        dispatch.dispatch(job_id)
    except Exception:  # noqa: BLE001 - the job stays queued and can be run from `rl agent` / `rl ai`
        log.exception("dispatch of %s failed", job_id)
    return job_id


# ---------------------------------------------------------------- language, direction, translation

def ensure_language(conn: sqlite3.Connection, paper_id: str) -> str | None:
    """Papers ingested before language detection existed get it from their stored pages on first view."""
    paper = get_paper(conn, paper_id)
    if paper is None:
        return None
    if paper["language"]:
        return paper["language"]
    f = primary_file(conn, paper_id)
    if f is None:
        return None
    pages = load_pages(conn, f["id"])[:extract.LANG_SAMPLE_PAGES]
    lang = extract.detect_language("\n".join(p.text for p in pages))
    if lang:
        conn.execute("UPDATE papers SET language = ? WHERE id = ?", (lang, paper_id))
    return lang


def set_direction(conn: sqlite3.Connection, paper_id: str, slug: str | None) -> str | None:
    """User-set direction; empty clears it. Raises ValueError for an unknown slug."""
    if get_paper(conn, paper_id) is None:
        raise LookupError(paper_id)
    value = (slug or "").strip() or None
    if value is not None and directions.clean(value) is None:
        raise ValueError(f"未知方向 {value}")
    conn.execute("UPDATE papers SET direction = ?, updated_at = ? WHERE id = ?", (value, now_iso(), paper_id))
    return value


def default_translate_target(language: str | None) -> str:
    """CJK papers translate into English by default; everything else into Chinese."""
    return "en" if extract.is_cjk(language) else "zh"


def enqueue_translation(conn: sqlite3.Connection, paper_id: str, lang_to: str, *, force: bool = False) -> list[str]:
    """One paper.translate job per chunk of pages, each handed to the dispatcher."""
    from ..ai.jobs import paper_translate
    return paper_translate.enqueue_chunks(conn, paper_id, lang_to, force=force)


def translation_status(conn: sqlite3.Connection, paper_id: str, lang_to: str) -> dict[str, Any]:
    from ..ai.jobs import paper_translate
    return paper_translate.status(conn, paper_id, lang_to)


def translations_for(conn: sqlite3.Connection, file_id: int, lang_to: str) -> dict[int, str]:
    return {r["page_index"]: r["text"] for r in conn.execute(
        "SELECT page_index, text FROM paper_translations WHERE file_id = ? AND lang_to = ?", (file_id, lang_to))}


def translated_langs(conn: sqlite3.Connection, paper_id: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT lang_to FROM paper_translations WHERE paper_id = ? ORDER BY lang_to",
                                       (paper_id,))]
