"""Evidence matrix (method-layer §3): explanations × observations, the status rules of §3.1,
coverage, and the process-tracing verdicts of §4.

Cell status changes only through these functions, which the web and CLI call on a user
action; no AI job imports them (guardrail 3)."""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from ..db.connection import now_iso

STATUSES = ("missing", "weak", "strong", "contradicting", "searched-none", "na")
RESOLVED = ("weak", "strong", "contradicting", "searched-none")
FOUND = ("weak", "strong")
NOT_FOUND = ("searched-none", "contradicting")
TEST_TYPES = ("straw", "hoop", "smoking-gun", "doubly-decisive")
DECISIVE = ("hoop", "smoking-gun", "doubly-decisive")
PRIMARY_SOURCES = ("primary-official", "primary-statement", "interview")
SOURCE_TYPES = ("primary-official", "primary-statement", "interview", "secondary-scholarly", "media")

# Glyph + text label so a cell never relies on colour alone.
GLYPHS = {
    "missing": ("○", "missing"), "weak": ("◐", "weak"), "strong": ("●", "strong"),
    "contradicting": ("✕", "contradicting"), "searched-none": ("⊘", "searched, none"), "na": ("—", "n/a"),
}
TEST_LABELS = {"straw": "straw", "hoop": "hoop", "smoking-gun": "smoking gun", "doubly-decisive": "doubly decisive"}
TEST_RANK = {"doubly-decisive": 3, "smoking-gun": 3, "hoop": 2, "straw": 1, None: 0}


class CellRuleError(ValueError):
    """A status change that the §3.1 rules do not allow."""


def explanations(conn: sqlite3.Connection, slug: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM claims WHERE project_slug = ? AND kind = 'explanation' AND status = 'accepted' "
                        "ORDER BY CASE WHEN code IS NULL THEN 1 ELSE 0 END, code, id", (slug,)).fetchall()


def observations(conn: sqlite3.Connection, slug: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM cases WHERE project_slug = ? AND is_observation = 1 "
                        "ORDER BY COALESCE(sort, 999), date_start, id", (slug,)).fetchall()


def row_key(claim: sqlite3.Row | dict) -> str:
    return claim["code"] or f"#{claim['id']}"


def ensure_cells(conn: sqlite3.Connection, slug: str) -> int:
    """Create a `missing` cell for every explanation × observation pair that lacks one."""
    n, now = 0, now_iso()
    for h in explanations(conn, slug):
        for o in observations(conn, slug):
            cur = conn.execute("INSERT OR IGNORE INTO evidence_cells(claim_id, case_id, status, updated_at) VALUES (?,?,?,?)",
                               (h["id"], o["id"], "missing", now))
            n += cur.rowcount
    return n


def project_cells(conn: sqlite3.Connection, slug: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT ec.*, c.code, c.is_own, c.statement, ca.label AS case_label, ca.date_start, ca.sort AS case_sort, "
        "(SELECT COUNT(*) FROM cell_excerpts ce WHERE ce.cell_id = ec.id) AS n_excerpts, "
        "(SELECT MAX(searched_on) FROM search_log s WHERE s.cell_id = ec.id) AS last_search, "
        "(SELECT COUNT(*) FROM search_log s WHERE s.cell_id = ec.id) AS n_searches "
        "FROM evidence_cells ec JOIN claims c ON c.id = ec.claim_id JOIN cases ca ON ca.id = ec.case_id "
        "WHERE c.project_slug = ? AND c.kind = 'explanation' AND c.status = 'accepted' AND ca.is_observation = 1", (slug,)).fetchall()


def cell(conn: sqlite3.Connection, cell_id: int) -> dict[str, Any] | None:
    r = conn.execute(
        "SELECT ec.*, c.code, c.statement, c.is_own, c.project_slug, ca.label AS case_label, ca.date_start "
        "FROM evidence_cells ec JOIN claims c ON c.id = ec.claim_id JOIN cases ca ON ca.id = ec.case_id WHERE ec.id = ?",
        (cell_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["excerpts"] = cell_excerpts(conn, cell_id)
    d["searches"] = conn.execute("SELECT * FROM search_log WHERE cell_id = ? ORDER BY searched_on DESC, id DESC", (cell_id,)).fetchall()
    d["glyph"], d["label"] = GLYPHS[d["status"]]
    return d


def cell_excerpts(conn: sqlite3.Connection, cell_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT e.id, e.paper_id, e.quote, e.locator, e.source_type, e.status, e.private, e.my_note, "
        "p.title AS paper_title, p.year, p.authors_json, p.metadata_status "
        "FROM cell_excerpts ce JOIN excerpts e ON e.id = ce.excerpt_id JOIN papers p ON p.id = e.paper_id "
        "WHERE ce.cell_id = ? ORDER BY e.id", (cell_id,)).fetchall()


def _usable(excerpts: list[sqlite3.Row]) -> list[sqlite3.Row]:
    # Guardrail 1: an AI-proposed `candidate` reference never carries evidence.
    return [e for e in excerpts if e["metadata_status"] != "candidate" and e["status"] != "discarded"]


def check_status(conn: sqlite3.Connection, cell_id: int, status: str) -> None:
    if status not in STATUSES:
        raise CellRuleError(f"status must be one of {', '.join(STATUSES)}")
    if status in ("missing", "na"):
        return
    if status == "searched-none":
        if not conn.execute("SELECT 1 FROM search_log WHERE cell_id = ?", (cell_id,)).fetchone():
            raise CellRuleError("'searched, none found' needs a search log entry first (where you looked, which terms)")
        return
    usable = _usable(cell_excerpts(conn, cell_id))
    if not usable:
        raise CellRuleError(f"'{status}' needs at least one attached excerpt from a real source")
    if status == "strong":
        located = [e for e in usable if (e["locator"] or "").strip()]
        primary = [e for e in located if e["source_type"] in PRIMARY_SOURCES]
        papers = {e["paper_id"] for e in located}
        if not primary and len(papers) < 2:
            raise CellRuleError("'strong' needs a primary source excerpt, or excerpts from two independent works, each with a page")


def set_status(conn: sqlite3.Connection, cell_id: int, status: str) -> None:
    check_status(conn, cell_id, status)
    conn.execute("UPDATE evidence_cells SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), cell_id))


_UNSET: Any = object()


def update_cell(conn: sqlite3.Connection, cell_id: int, *, implication: Any = _UNSET, test_type: Any = _UNSET,
                discriminating: Any = _UNSET, feasibility: Any = _UNSET, hint: Any = _UNSET) -> None:
    sets, args = [], []
    if implication is not _UNSET:
        sets += ["implication = ?", "implication_origin = 'user'"]
        args.append((implication or "").strip() or None)
    if test_type is not _UNSET:
        if test_type in ("", None):
            test_type = None
        elif test_type not in TEST_TYPES:
            raise CellRuleError(f"test type must be one of {', '.join(TEST_TYPES)}")
        sets.append("test_type = ?"); args.append(test_type)
    for name, value in (("discriminating", discriminating), ("feasibility", feasibility)):
        if value is _UNSET:
            continue
        if value in ("", None):
            value = None
        else:
            value = int(value)
            if value not in (0, 1, 2):
                raise CellRuleError(f"{name} is a 0–2 score")
        sets.append(f"{name} = ?"); args.append(value)
    if hint is not _UNSET:
        sets += ["hint = ?", "hint_origin = 'user'"]
        args.append((hint or "").strip() or None)
    if not sets:
        return
    sets.append("updated_at = ?"); args.append(now_iso())
    conn.execute(f"UPDATE evidence_cells SET {', '.join(sets)} WHERE id = ?", (*args, cell_id))


def attach_excerpt(conn: sqlite3.Connection, cell_id: int, excerpt_id: str) -> None:
    excerpt_id = excerpt_id.strip()
    if not conn.execute("SELECT 1 FROM evidence_cells WHERE id = ?", (cell_id,)).fetchone():
        raise CellRuleError(f"cell {cell_id} not found")
    if not conn.execute("SELECT 1 FROM excerpts WHERE id = ?", (excerpt_id,)).fetchone():
        raise CellRuleError(f"excerpt {excerpt_id} not found")
    conn.execute("INSERT OR IGNORE INTO cell_excerpts(cell_id, excerpt_id) VALUES (?, ?)", (cell_id, excerpt_id))
    conn.execute("UPDATE excerpts SET status = 'linked', updated_at = ? WHERE id = ? AND status = 'inbox'", (now_iso(), excerpt_id))


def detach_excerpt(conn: sqlite3.Connection, cell_id: int, excerpt_id: str) -> None:
    row = conn.execute("SELECT status FROM evidence_cells WHERE id = ?", (cell_id,)).fetchone()
    if not row:
        raise CellRuleError(f"cell {cell_id} not found")
    conn.execute("SAVEPOINT detach")
    conn.execute("DELETE FROM cell_excerpts WHERE cell_id = ? AND excerpt_id = ?", (cell_id, excerpt_id))
    try:
        check_status(conn, cell_id, row["status"])
    except CellRuleError as e:
        conn.execute("ROLLBACK TO detach")
        conn.execute("RELEASE detach")
        raise CellRuleError(f"cannot detach: the cell is '{row['status']}' and would break its rule ({e}). Change the status first.")
    conn.execute("RELEASE detach")


def log_search(conn: sqlite3.Connection, cell_id: int, where: str, terms: str | None = None,
               result_note: str | None = None, searched_on: str | None = None) -> int:
    if not (where or "").strip():
        raise CellRuleError("say where you searched")
    if not conn.execute("SELECT 1 FROM evidence_cells WHERE id = ?", (cell_id,)).fetchone():
        raise CellRuleError(f"cell {cell_id} not found")
    cur = conn.execute("INSERT INTO search_log(cell_id, searched_on, where_searched, terms, result_note) VALUES (?,?,?,?,?)",
                       (cell_id, searched_on or date.today().isoformat(), where.strip(), (terms or "").strip() or None,
                        (result_note or "").strip() or None))
    return int(cur.lastrowid)


def is_priority(c: sqlite3.Row | dict) -> bool:
    return (c["discriminating"] or 0) >= 1 or c["test_type"] in DECISIVE


def coverage(conn: sqlite3.Connection, slug: str, priority_only: bool = False) -> dict[str, Any]:
    """(weak + strong + contradicting + searched-none) / all non-n/a cells."""
    cells = [c for c in project_cells(conn, slug) if c["status"] != "na"]
    if priority_only:
        cells = [c for c in cells if is_priority(c)]
    resolved = sum(1 for c in cells if c["status"] in RESOLVED)
    total = len(cells)
    return {"resolved": resolved, "total": total, "ratio": resolved / total if total else 0.0,
            "pct": round(resolved * 100 / total) if total else 0}


def grid(conn: sqlite3.Connection, slug: str) -> dict[str, Any]:
    rows = explanations(conn, slug)
    cols = observations(conn, slug)
    cells: dict[tuple[int, str], dict[str, Any]] = {}
    for c in project_cells(conn, slug):
        d = dict(c)
        d["glyph"], d["label"] = GLYPHS[d["status"]]
        cells[(c["claim_id"], c["case_id"])] = d
    return {"rows": rows, "cols": cols, "cells": cells}


def verdicts(conn: sqlite3.Connection, slug: str) -> dict[str, str]:
    """Process-tracing verdict per explanation (method-layer §4 test-type table).

    hoop not found after a logged search (or contradicted) → eliminated; hoop found → survives;
    smoking gun found → strengthened, and rivals whose cell in that column failed → weakened;
    doubly decisive found → strengthened and every other row eliminated; straws never decide."""
    rows = explanations(conn, slug)
    by_row: dict[int, list[sqlite3.Row]] = {}
    for c in project_cells(conn, slug):
        by_row.setdefault(c["claim_id"], []).append(c)
    out: dict[str, str] = {}
    sg_columns: dict[int, set[str]] = {}
    dd_rows: list[int] = []
    for h in rows:
        cells = by_row.get(h["id"], [])
        v = "untested"
        if any(c["test_type"] in ("hoop", "doubly-decisive") and c["status"] in NOT_FOUND for c in cells):
            v = "eliminated"
        elif any(c["test_type"] in ("smoking-gun", "doubly-decisive") and c["status"] in FOUND for c in cells):
            v = "strengthened"
        elif any(c["test_type"] == "hoop" and c["status"] in FOUND for c in cells):
            v = "survives"
        out[row_key(h)] = v
        sg_columns[h["id"]] = {c["case_id"] for c in cells if c["test_type"] == "smoking-gun" and c["status"] in FOUND}
        if any(c["test_type"] == "doubly-decisive" and c["status"] in FOUND for c in cells):
            dd_rows.append(h["id"])
    for h in rows:
        cols = sg_columns[h["id"]]
        if not cols:
            continue
        for r in rows:
            if r["id"] == h["id"] or out[row_key(r)] in ("eliminated", "strengthened"):
                continue
            if any(c["case_id"] in cols and c["status"] in NOT_FOUND for c in by_row.get(r["id"], [])):
                out[row_key(r)] = "weakened"
    for hid in dd_rows:
        for r in rows:
            if r["id"] != hid and r["id"] not in dd_rows:
                out[row_key(r)] = "eliminated"
    return out


def row_counts(conn: sqlite3.Connection, slug: str) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {}
    for c in project_cells(conn, slug):
        d = out.setdefault(c["claim_id"], {s: 0 for s in STATUSES})
        d[c["status"]] += 1
    return out


def best_rival(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    """The rival (not the user's own explanation) that scores best in the matrix."""
    counts = row_counts(conn, slug)
    rivals = [h for h in explanations(conn, slug) if not h["is_own"]]
    if not rivals:
        return None

    def score(h: sqlite3.Row) -> int:
        c = counts.get(h["id"], {})
        return 2 * c.get("strong", 0) + c.get("weak", 0) - 2 * c.get("contradicting", 0)

    best = max(rivals, key=score)
    return best if score(best) > 0 else None
