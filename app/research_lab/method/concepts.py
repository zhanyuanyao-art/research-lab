"""Definition ledger (method-layer §5): usages, dimensions with indicators, competing usages,
a versioned working definition, the Sartori ladder, and the two ledger flags."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db.connection import now_iso

LADDER = ("asean-specific", "regional-organisations", "general")
LADDER_LABELS = {"asean-specific": "ASEAN-specific", "regional-organisations": "travels to other regional organisations (AU, SCO)",
                 "general": "general concept"}
STALE_DAYS = 56
STALE_NEW_USAGES = 5


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "item"


def get(conn: sqlite3.Connection, concept_id: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM concepts WHERE id = ?", (concept_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["dimensions"] = json.loads(d["dimensions_json"] or "[]")
    d["competing"] = json.loads(d.get("competing_json") or "[]")
    d["versions"] = conn.execute("SELECT * FROM concept_versions WHERE concept_id = ? ORDER BY created_at DESC, version DESC",
                                 (concept_id,)).fetchall()
    return d


def for_project(conn: sqlite3.Connection, slug: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT c.* FROM concept_projects cp JOIN concepts c ON c.id = cp.concept_id "
                        "WHERE cp.project_slug = ? ORDER BY cp.sort, c.id", (slug,)).fetchall()


def usages(conn: sqlite3.Connection, concept_id: str) -> list[dict[str, Any]]:
    """Excerpts tagged use=definition and linked `about` this concept (accepted links only)."""
    rows = conn.execute(
        "SELECT e.*, p.title AS paper_title, p.year, p.authors_json, p.camp AS paper_camp, p.id AS pid, a.created_at AS linked_at "
        "FROM associations a JOIN excerpts e ON e.id = a.src_id JOIN papers p ON p.id = e.paper_id "
        "WHERE a.src_type = 'excerpt' AND a.rel = 'about' AND a.dst_type = 'concept' AND a.dst_id = ? AND a.status = 'accepted' "
        "AND e.status != 'discarded' ORDER BY e.created_at", (concept_id,)).fetchall()
    dims_by_excerpt: dict[str, list[str]] = {}
    for a in conn.execute("SELECT src_id, dst_id FROM associations WHERE src_type = 'excerpt' AND rel = 'emphasises' "
                          "AND dst_type = 'dimension' AND dst_id LIKE ? AND status = 'accepted'", (f"{concept_id}:%",)):
        dims_by_excerpt.setdefault(a["src_id"], []).append(a["dst_id"].split(":", 1)[1])
    out = []
    for r in rows:
        try:
            uses = json.loads(r["uses_json"] or "[]")
        except ValueError:
            uses = []
        if "definition" not in uses:
            continue
        d = dict(r)
        d["dimensions"] = sorted(dims_by_excerpt.get(r["id"], []))
        d["camp"] = r["camp"] or r["paper_camp"]
        out.append(d)
    return out


def _save_dims(conn: sqlite3.Connection, concept_id: str, dims: list[dict[str, Any]]) -> None:
    conn.execute("UPDATE concepts SET dimensions_json = ?, updated_at = ? WHERE id = ?",
                 (json.dumps(dims, ensure_ascii=False), now_iso(), concept_id))


def add_dimension(conn: sqlite3.Connection, concept_id: str, label: str, description: str = "") -> str:
    c = get(conn, concept_id)
    if not c:
        raise ValueError(f"concept {concept_id} not found")
    label = label.strip()
    if not label:
        raise ValueError("a dimension needs a label")
    key = slugify(label)
    if any(d["key"] == key for d in c["dimensions"]):
        raise ValueError(f"dimension {label!r} exists")
    c["dimensions"].append({"key": key, "label": label, "description": description.strip(), "indicators": []})
    _save_dims(conn, concept_id, c["dimensions"])
    return key


def update_dimension(conn: sqlite3.Connection, concept_id: str, key: str, *, label: str | None = None,
                     description: str | None = None, indicators: list[str] | None = None) -> None:
    c = get(conn, concept_id)
    if not c:
        raise ValueError(f"concept {concept_id} not found")
    for d in c["dimensions"]:
        if d["key"] == key:
            if label is not None and label.strip():
                d["label"] = label.strip()
            if description is not None:
                d["description"] = description.strip()
            if indicators is not None:
                d["indicators"] = [i.strip() for i in indicators if i.strip()]
            _save_dims(conn, concept_id, c["dimensions"])
            return
    raise ValueError(f"dimension {key!r} not found")


def remove_dimension(conn: sqlite3.Connection, concept_id: str, key: str) -> None:
    c = get(conn, concept_id)
    if not c:
        raise ValueError(f"concept {concept_id} not found")
    _save_dims(conn, concept_id, [d for d in c["dimensions"] if d["key"] != key])


def tag_usage_dimension(conn: sqlite3.Connection, concept_id: str, excerpt_id: str, key: str, on: bool = True) -> None:
    dst = f"{concept_id}:{key}"
    if on:
        conn.execute("INSERT INTO associations(src_type, src_id, rel, dst_type, dst_id, origin, status, created_at) "
                     "VALUES ('excerpt', ?, 'emphasises', 'dimension', ?, 'user', 'accepted', ?) "
                     "ON CONFLICT(src_type, src_id, rel, dst_type, dst_id) DO UPDATE SET status = 'accepted'",
                     (excerpt_id, dst, now_iso()))
    else:
        conn.execute("UPDATE associations SET status = 'rejected' WHERE src_type = 'excerpt' AND src_id = ? "
                     "AND rel = 'emphasises' AND dst_type = 'dimension' AND dst_id = ?", (excerpt_id, dst))


def add_competing(conn: sqlite3.Connection, concept_id: str, a: str, b: str, note: str = "") -> None:
    c = get(conn, concept_id)
    if not c:
        raise ValueError(f"concept {concept_id} not found")
    a, b = a.strip(), b.strip()
    if not a or not b or a == b:
        raise ValueError("a competing pair needs two different usages")
    c["competing"].append({"a": a, "b": b, "note": note.strip()})
    conn.execute("UPDATE concepts SET competing_json = ?, updated_at = ? WHERE id = ?",
                 (json.dumps(c["competing"], ensure_ascii=False), now_iso(), concept_id))


def remove_competing(conn: sqlite3.Connection, concept_id: str, index: int) -> None:
    c = get(conn, concept_id)
    if not c:
        raise ValueError(f"concept {concept_id} not found")
    if 0 <= index < len(c["competing"]):
        c["competing"].pop(index)
        conn.execute("UPDATE concepts SET competing_json = ?, updated_at = ? WHERE id = ?",
                     (json.dumps(c["competing"], ensure_ascii=False), now_iso(), concept_id))


def _next_version(conn: sqlite3.Connection, concept_id: str) -> str:
    n = conn.execute("SELECT COUNT(*) FROM concept_versions WHERE concept_id = ?", (concept_id,)).fetchone()[0]
    return f"v0.{n + 1}"


def set_definition(conn: sqlite3.Connection, concept_id: str, text: str, reason: str = "",
                   artifact_ref: str | None = None) -> str:
    text = text.strip()
    if not text:
        raise ValueError("the working definition cannot be empty")
    c = get(conn, concept_id)
    if not c:
        raise ValueError(f"concept {concept_id} not found")
    if c["working_definition"] == text:
        return c["def_version"] or ""
    if c["working_definition"] and not reason.strip():
        raise ValueError("say why the definition changed")
    version = _next_version(conn, concept_id)
    now = now_iso()
    conn.execute("INSERT INTO concept_versions(concept_id, version, text, reason, artifact_ref, created_at) VALUES (?,?,?,?,?,?)",
                 (concept_id, version, text, reason.strip() or "first working definition", (artifact_ref or "").strip() or None, now))
    conn.execute("UPDATE concepts SET working_definition = ?, def_version = ?, updated_at = ? WHERE id = ?",
                 (text, version, now, concept_id))
    return version


def set_ladder(conn: sqlite3.Connection, concept_id: str, ladder: str) -> None:
    if ladder and ladder not in LADDER:
        raise ValueError(f"ladder must be one of {', '.join(LADDER)}")
    conn.execute("UPDATE concepts SET ladder = ?, updated_at = ? WHERE id = ?", (ladder or None, now_iso(), concept_id))


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower())}


def flags(conn: sqlite3.Connection, concept_id: str, now: datetime | None = None) -> list[dict[str, str]]:
    """(1) a definition usage that matches no dimension → candidate new dimension;
    (2) working definition unchanged for 8 weeks while ≥5 new definition usages arrived."""
    c = get(conn, concept_id)
    if not c:
        return []
    out: list[dict[str, str]] = []
    us = usages(conn, concept_id)
    dim_words = {d["key"]: _words(d["label"] + " " + d.get("description", "")) for d in c["dimensions"]}
    for u in us:
        if u["dimensions"]:
            continue
        text = _words(u["quote"] + " " + u["my_note"])
        if not any(text & words for words in dim_words.values()):
            out.append({"kind": "new-dimension", "excerpt": u["id"],
                        "text": f"{u['id']} defines {c['label'].lower()} in terms that match no dimension. Candidate new dimension?"})
    now = now or datetime.now(timezone.utc)
    last = c["versions"][0]["created_at"] if c["versions"] else None
    since = last or ""
    new_usages = [u for u in us if (u["created_at"] or "") > since]
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            last_dt = now
        if now - last_dt >= timedelta(days=STALE_DAYS) and len(new_usages) >= STALE_NEW_USAGES:
            out.append({"kind": "stale-definition",
                        "text": f"The working definition ({c['def_version']}) has not changed in {(now - last_dt).days // 7} weeks "
                                f"while {len(new_usages)} new definition excerpts arrived. Does it still hold?"})
    elif len(us) >= STALE_NEW_USAGES:
        out.append({"kind": "stale-definition",
                    "text": f"{len(us)} definition excerpts and no working definition yet. Write v0.1."})
    return out


def m4_state(conn: sqlite3.Connection, concept_id: str) -> dict[str, Any]:
    c = get(conn, concept_id) or {"dimensions": [], "working_definition": None, "label": concept_id}
    us = usages(conn, concept_id)
    missing_ind = [d["label"] for d in c["dimensions"] if not d.get("indicators")]
    return {"label": c["label"], "usages": len(us), "definition": bool(c["working_definition"]),
            "dimensions": len(c["dimensions"]), "missing_indicators": missing_ind}
