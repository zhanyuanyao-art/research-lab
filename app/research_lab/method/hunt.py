"""Weekly hunt list (method-layer §3.2 and guardrail 8).

priority = discriminating × feasibility × staleness, where discriminating gets +1 for a hoop,
smoking-gun or doubly-decisive cell and staleness is weeks since the last search + 1 (weeks
since the cell was last edited when it was never searched). The list names what to check and
where to look; it never says what will be found."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any

from .matrix import DECISIVE, RESOLVED, TEST_LABELS, TEST_RANK, explanations, project_cells

HUNTABLE = ("missing", "weak")
MAX_STALENESS = 12


def _weeks_since(stamp: str | None, today: date) -> int:
    if not stamp:
        return 0
    try:
        d = datetime.fromisoformat(stamp).date() if "T" in stamp else date.fromisoformat(stamp[:10])
    except ValueError:
        return 0
    return max(0, (today - d).days // 7)


def cell_priority(c: sqlite3.Row | dict, today: date | None = None) -> float:
    today = today or date.today()
    if c["status"] not in HUNTABLE:
        return 0.0
    disc = (c["discriminating"] or 0) + (1 if c["test_type"] in DECISIVE else 0)
    feas = c["feasibility"] if c["feasibility"] is not None else 1
    stale = min(MAX_STALENESS, _weeks_since(c["last_search"] or c["updated_at"], today) + 1)
    return float(disc * feas * stale)


def _item(c: sqlite3.Row, priority: float) -> dict[str, Any]:
    placeholder = c["implication_origin"] == "placeholder"
    return {
        "cell_id": c["id"], "code": c["code"] or f"#{c['claim_id']}", "claim_id": c["claim_id"], "statement": c["statement"],
        "is_own": bool(c["is_own"]), "case_id": c["case_id"], "case_label": c["case_label"], "date": c["date_start"],
        "status": c["status"], "test_type": c["test_type"], "test_label": TEST_LABELS.get(c["test_type"] or "", ""),
        "implication": c["implication"] or "", "implication_placeholder": placeholder,
        "where_to_look": c["hint"] or "", "priority": priority, "last_search": c["last_search"],
        "forced": False, "reason": "",
    }


def _sort_key(it: dict[str, Any], sort: dict[int, int]) -> tuple:
    return (-it["priority"], -TEST_RANK.get(it["test_type"], 0), -(it.get("_feas") or 0), -int(it["is_own"]),
            sort.get(it["cell_id"], 999), it["cell_id"])


def recompute(conn: sqlite3.Connection, slug: str, today: date | None = None) -> list[dict[str, Any]]:
    """Store the priority of every project cell and return the huntable ones, best first."""
    today = today or date.today()
    cells = project_cells(conn, slug)
    items, sort = [], {}
    for c in cells:
        p = cell_priority(c, today)
        if int(round(p)) != (c["priority"] or 0):
            conn.execute("UPDATE evidence_cells SET priority = ? WHERE id = ?", (int(round(p)), c["id"]))
        if c["status"] in HUNTABLE:
            it = _item(c, p)
            it["_feas"] = c["feasibility"] or 0
            items.append(it)
            sort[c["id"]] = c["case_sort"] if c["case_sort"] is not None else 999
    items.sort(key=lambda it: _sort_key(it, sort))
    return items


def own_least_evidenced(conn: sqlite3.Connection, slug: str) -> list[sqlite3.Row]:
    """Own explanations with fewer resolved cells than every rival (guardrail 8)."""
    rows = explanations(conn, slug)
    resolved: dict[int, int] = {h["id"]: 0 for h in rows}
    for c in project_cells(conn, slug):
        if c["status"] in RESOLVED:
            resolved[c["claim_id"]] = resolved.get(c["claim_id"], 0) + 1
    rivals = [h for h in rows if not h["is_own"]]
    return [h for h in rows if h["is_own"] and rivals and all(resolved[h["id"]] < resolved[r["id"]] for r in rivals)]


def hunt_list(conn: sqlite3.Connection, slug: str, n: int = 5, today: date | None = None) -> list[dict[str, Any]]:
    items = recompute(conn, slug, today)
    top = items[:n]
    for own in own_least_evidenced(conn, slug):
        hoops = [it for it in items if it["claim_id"] == own["id"] and it["test_type"] in ("hoop", "doubly-decisive")]
        if not hoops:
            continue
        forced = hoops[0]
        forced["reason"] = (f"Rule 8: {forced['code']} is your own explanation and has fewer evidenced cells than every "
                            "rival, so its top hoop test stays on the list.")
        if forced not in top:
            forced["forced"] = True
            top = top[: max(0, n - 1)] + [forced] if len(top) >= n else top + [forced]
    for it in top:
        it.pop("_feas", None)
    return top
