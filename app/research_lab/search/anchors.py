"""Anchors: pseudo-candidates that exist before any excerpt does, so association has something
to place excerpt #1 against. Ids are stable strings such as `anchor:now:2` (1-based list index)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..files import datafiles
from .fts import tokens


@dataclass
class Anchor:
    id: str
    label: str          # where it comes from, e.g. "now.yml · need to find out #2"
    text: str
    project: str | None = None
    type: str = "anchor"

    @property
    def title(self) -> str:
        return self.label

    @property
    def snippet(self) -> str:
        return self.text


def all_anchors(conn: sqlite3.Connection, project: str | None = None, *, unfiled: bool = False) -> list[Anchor]:
    """Every anchor, or those of `project` (now.yml's when none is given). `unfiled` is for something in no 课题
    (card 0202): every project's question, but neither now.yml's list (the current project's) nor any camp or case."""
    now = datafiles.read("now") or {}
    if not unfiled:
        project = project or (str(now.get("project")) if now.get("project") else None)
    out: list[Anchor] = []
    if not unfiled:
        for i, item in enumerate(now.get("need_to_find_out") or [], start=1):
            out.append(Anchor(f"anchor:now:{i}", f"now.yml · need to find out #{i}", str(item), project))
        if now.get("question"):
            out.append(Anchor("anchor:now:question", "now.yml · question", str(now["question"]), project))
    for p in datafiles.read("projects") or []:
        if p.get("question"):
            slug = str(p.get("slug"))
            out.append(Anchor(f"anchor:project:{slug}", f"{p.get('name', slug)} · project question", str(p["question"]), slug))
    if unfiled:
        return out
    sql, args = "SELECT id, project_slug, label, core_claim FROM camps", []
    if project:
        sql += " WHERE project_slug = ?"; args.append(project)
    for r in conn.execute(sql + " ORDER BY sort, id", args):
        text = r["label"] + (f": {r['core_claim']}" if r["core_claim"] else "")
        out.append(Anchor(f"anchor:camp:{r['id']}", f"landscape camp · {r['label']}", text, r["project_slug"]))
    sql, args = "SELECT id, project_slug, label, selection_note FROM cases WHERE is_observation = 1", []
    if project:
        sql += " AND project_slug = ?"; args.append(project)
    for r in conn.execute(sql + " ORDER BY sort, id", args):
        text = r["label"] + (f" ({r['selection_note']})" if r["selection_note"] else "")
        out.append(Anchor(f"anchor:case:{r['id']}", f"matrix column · {r['id']}", text, r["project_slug"]))
    return out


def resolve(conn: sqlite3.Connection, anchor_id: str) -> Anchor | None:
    for a in all_anchors(conn):
        if a.id == anchor_id:
            return a
    # camps/cases of other projects are filtered out of all_anchors(project) but still resolve
    kind, _, key = anchor_id.removeprefix("anchor:").partition(":")
    if kind == "camp":
        r = conn.execute("SELECT id, project_slug, label, core_claim FROM camps WHERE id = ?", (key,)).fetchone()
        if r:
            return Anchor(anchor_id, f"landscape camp · {r['label']}", r["label"], r["project_slug"])
    if kind == "case":
        r = conn.execute("SELECT id, project_slug, label FROM cases WHERE id = ?", (key,)).fetchone()
        if r:
            return Anchor(anchor_id, f"matrix column · {r['id']}", r["label"], r["project_slug"])
    return None


def ranked(conn: sqlite3.Connection, text: str, project: str | None = None) -> list[Anchor]:
    """Anchors ordered by word overlap with `text`, the source project's anchors breaking ties."""
    words = set(tokens(text))
    anchors = all_anchors(conn, project, unfiled=not project)

    def key(pair: tuple[int, Anchor]) -> tuple:
        i, a = pair
        overlap = len(words & set(tokens(a.text)))
        own = 1 if (a.project == project or a.id.startswith("anchor:now:")) else 0
        return (-overlap, -own, i)

    return [a for _, a in sorted(enumerate(anchors), key=key)]
