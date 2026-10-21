"""Empirical check cards (product-ux §5, method-layer §4). The app does not run statistics:
it holds the prediction written before looking, the status, and the recorded result."""
from __future__ import annotations

import sqlite3
from typing import Any

from ..db.connection import now_iso

STATUSES = ("planned", "data-ready", "run", "recorded")
STATUS_LABELS = {"planned": "planned", "data-ready": "data ready", "run": "run", "recorded": "result recorded"}
FIELDS = ("title", "prediction", "data_needed", "method", "would_weaken_if", "result_note", "reading", "script_ref")


class CheckRuleError(ValueError):
    pass


def list_checks(conn: sqlite3.Connection, slug: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT k.*, c.code AS claim_code, c.statement AS claim_statement FROM checks k LEFT JOIN claims c ON c.id = k.claim_id "
        "WHERE k.project_slug = ? ORDER BY CASE k.origin WHEN 'ai-suggested' THEN 1 ELSE 0 END, k.created_at, k.id", (slug,)).fetchall()
    return [dict(r) for r in rows]


def get(conn: sqlite3.Connection, check_id: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT k.*, c.code AS claim_code, c.statement AS claim_statement FROM checks k "
                     "LEFT JOIN claims c ON c.id = k.claim_id WHERE k.id = ?", (check_id,)).fetchone()
    return dict(r) if r else None


def create(conn: sqlite3.Connection, slug: str, *, title: str, prediction: str, claim_id: int | None = None,
           data_needed: str = "", method: str = "", would_weaken_if: str = "") -> int:
    if not title.strip() or not prediction.strip():
        raise CheckRuleError("a check needs a title and a prediction written before looking")
    now = now_iso()
    cur = conn.execute(
        "INSERT INTO checks(project_slug, claim_id, title, prediction, data_needed, method, would_weaken_if, status, origin, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (slug, claim_id, title.strip(), prediction.strip(), data_needed.strip() or None, method.strip() or None,
         would_weaken_if.strip() or None, "planned", "user", now, now))
    return int(cur.lastrowid)


def update(conn: sqlite3.Connection, check_id: int, **fields: Any) -> None:
    k = get(conn, check_id)
    if not k:
        raise CheckRuleError(f"check {check_id} not found")
    sets, args = [], []
    for name, value in fields.items():
        if name not in FIELDS or value is None:
            continue
        value = str(value).strip()
        if name == "prediction" and value != (k["prediction"] or "") and k["status"] != "planned":
            raise CheckRuleError("the prediction is fixed once data is ready; record a new check instead")
        if name in ("result_note", "reading") and value and k["status"] in ("planned", "data-ready"):
            raise CheckRuleError("record the result after the check has run (set status to 'run' first)")
        if name in ("title", "prediction") and not value:
            raise CheckRuleError(f"{name} cannot be empty")
        sets.append(f"{name} = ?"); args.append(value or None)
    if sets:
        sets.append("updated_at = ?"); args.append(now_iso())
        conn.execute(f"UPDATE checks SET {', '.join(sets)} WHERE id = ?", (*args, check_id))


def set_status(conn: sqlite3.Connection, check_id: int, status: str) -> None:
    k = get(conn, check_id)
    if not k:
        raise CheckRuleError(f"check {check_id} not found")
    if status not in STATUSES:
        raise CheckRuleError(f"status must be one of {', '.join(STATUSES)}")
    if k["origin"] == "ai-suggested" and status != "planned":
        raise CheckRuleError("accept the suggested check before moving it forward")
    if status == "recorded" and not ((k["result_note"] or "").strip() and (k["reading"] or "").strip()):
        raise CheckRuleError("a recorded check needs a result note and a one-paragraph reading")
    conn.execute("UPDATE checks SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), check_id))


def accept(conn: sqlite3.Connection, check_id: int) -> None:
    conn.execute("UPDATE checks SET origin = 'user', updated_at = ? WHERE id = ? AND origin = 'ai-suggested'", (now_iso(), check_id))


def discard(conn: sqlite3.Connection, check_id: int) -> None:
    """Only AI suggestions can be discarded; a user's check stays on record."""
    cur = conn.execute("DELETE FROM checks WHERE id = ? AND origin = 'ai-suggested'", (check_id,))
    if cur.rowcount == 0:
        raise CheckRuleError("only an AI-suggested check can be discarded")


def derive(conn: sqlite3.Connection, target: str) -> str | None:
    """Queue checks.derive for a claim id (or an ART- artifact ref). Returns the job id or None if one is open."""
    from ..ai import queue
    job_id = queue.enqueue(conn, "checks.derive", str(target))
    if job_id:
        from ..ai import dispatch
        dispatch.dispatch(job_id)
    return job_id
