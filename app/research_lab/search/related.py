"""Related excerpts and artifacts without embeddings (architecture §4).

    T = b / max b           b = -bm25, 0 when outside the FTS pool
    G = 1 - Π(1 - w_k)      noisy-OR over tags shared by source and candidate
    L = 1 if two hops apart through accepted associations
    R = exp(-age_days / 120)
    base = 0.50·T + 0.30·G + 0.12·L + 0.08·R

Weights are settings (`related.weights`, `related.tag_weights`, `related.recency_days`).
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from ..db.connection import get_setting
from ..excerpts import author_year, locator_label
from ..files import artifacts as art_files
from . import fts

DEFAULT_WEIGHTS = {"T": 0.50, "G": 0.30, "L": 0.12, "R": 0.08}
DEFAULT_TAG_WEIGHTS = {"claim": 0.35, "concept": 0.25, "case": 0.20, "camp": 0.10, "project": 0.10, "paper": 0.05}
TAG_KINDS = ("claim", "concept", "case", "camp")
POOL = 50
PER_GROUP = 2
MARKER_RE = re.compile(r"rl:(EXC-\d{4}-\d{4,})")

Node = tuple[str, str]


@dataclass
class Candidate:
    type: str                     # excerpt | artifact
    id: str
    title: str
    snippet: str
    score: float = 0.0
    paper_id: str | None = None
    project: str | None = None
    created: str | None = None
    T: float = 0.0
    G: float = 0.0
    L: float = 0.0
    R: float = 0.0
    bm25: float = 0.0
    shared: list[str] = field(default_factory=list)
    explain: dict[str, Any] | None = None

    @property
    def group(self) -> str:
        return f"paper:{self.paper_id}" if self.type == "excerpt" and self.paper_id else f"{self.type}:{self.id}"


@dataclass
class Source:
    type: str                     # excerpt | artifact | text
    id: str | None
    text: str
    tags: set[Node]
    concept_labels: list[str]
    linked: set[Node]


def weights(conn: sqlite3.Connection) -> tuple[dict[str, float], dict[str, float], float]:
    w = {**DEFAULT_WEIGHTS, **(get_setting(conn, "related.weights", {}) or {})}
    tw = {**DEFAULT_TAG_WEIGHTS, **(get_setting(conn, "related.tag_weights", {}) or {})}
    return w, tw, float(get_setting(conn, "related.recency_days", 120) or 120)


def _in(n: int) -> str:
    return ",".join("?" * n)


def excerpt_tags(conn: sqlite3.Connection, ids: list[str]) -> dict[str, set[Node]]:
    out: dict[str, set[Node]] = {i: set() for i in ids}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        for r in conn.execute(f"SELECT id, camp, project_slug, paper_id FROM excerpts WHERE id IN ({_in(len(chunk))})", chunk):
            s = out[r["id"]]
            if r["camp"]:
                s.add(("camp", r["camp"]))
            if r["project_slug"]:
                s.add(("project", r["project_slug"]))
            s.add(("paper", r["paper_id"]))
        for r in conn.execute(
                f"SELECT src_id, dst_type, dst_id FROM associations WHERE src_type='excerpt' AND rel='about' AND status='accepted' "
                f"AND dst_type IN ({_in(len(TAG_KINDS))}) AND src_id IN ({_in(len(chunk))})", (*TAG_KINDS, *chunk)):
            out[r["src_id"]].add((r["dst_type"], r["dst_id"]))
    return out


def _artifact_markers(conn: sqlite3.Connection) -> dict[str, list[str]]:
    return {r["ref"]: MARKER_RE.findall(r["body"] or "") for r in conn.execute("SELECT ref, body FROM artifacts")}


def artifact_tags(conn: sqlite3.Connection, markers: dict[str, list[str]]) -> dict[str, set[Node]]:
    projects = {r["ref"]: r["project"] for r in conn.execute("SELECT ref, project FROM artifacts")}
    all_ids = sorted({x for ids in markers.values() for x in ids})
    et = excerpt_tags(conn, all_ids) if all_ids else {}
    out = {}
    for ref, ids in markers.items():
        s: set[Node] = set()
        if projects.get(ref):
            s.add(("project", projects[ref]))
        for x in ids:
            s |= et.get(x, set())
        out[ref] = s
    return out


def _direct_links(conn: sqlite3.Connection, node: Node) -> set[Node]:
    t, i = node
    out = {(r[0], r[1]) for r in conn.execute("SELECT dst_type, dst_id FROM associations WHERE src_type=? AND src_id=?", (t, i))}
    out |= {(r[0], r[1]) for r in conn.execute("SELECT src_type, src_id FROM associations WHERE dst_type=? AND dst_id=?", (t, i))}
    return out


def _accepted_neighbors(conn: sqlite3.Connection, node: Node) -> set[Node]:
    t, i = node
    out = {(r[0], r[1]) for r in conn.execute(
        "SELECT dst_type, dst_id FROM associations WHERE src_type=? AND src_id=? AND status='accepted'", (t, i))}
    out |= {(r[0], r[1]) for r in conn.execute(
        "SELECT src_type, src_id FROM associations WHERE dst_type=? AND dst_id=? AND status='accepted'", (t, i))}
    return out


def two_hop(conn: sqlite3.Connection, node: Node | None, tags: set[Node]) -> set[Node]:
    """Nodes that share an accepted-association neighbour with the source (s→X←c)."""
    hubs = set(tags) - {t for t in tags if t[0] in ("project", "paper")}
    if node:
        hubs |= _accepted_neighbors(conn, node)
    out: set[Node] = set()
    for hub in hubs:
        out |= _accepted_neighbors(conn, hub)
    if node:
        out.discard(node)
    return {n for n in out if n[0] in ("excerpt", "artifact")}


def resolve_source(conn: sqlite3.Connection, excerpt_id: str | None, artifact_ref: str | None, text: str | None,
                   markers: dict[str, list[str]]) -> Source:
    if excerpt_id:
        row = conn.execute("SELECT * FROM excerpts WHERE id = ?", (excerpt_id,)).fetchone()
        if row is None:
            raise LookupError(f"no excerpt {excerpt_id}")
        tags = excerpt_tags(conn, [excerpt_id])[excerpt_id]
        concept_ids = [i for k, i in tags if k == "concept"]
        labels = [r[0] for r in conn.execute(f"SELECT label FROM concepts WHERE id IN ({_in(len(concept_ids))})", concept_ids)] if concept_ids else []
        body = f"{row['quote']}\n{row['my_note']}"
        labels += fts.concept_labels_in(conn, body)
        linked = _direct_links(conn, ("excerpt", excerpt_id))
        linked |= {("artifact", ref) for ref, ids in markers.items() if excerpt_id in ids}
        return Source("excerpt", excerpt_id, body, tags, labels, linked)
    if artifact_ref:
        row = conn.execute("SELECT * FROM artifacts WHERE ref = ?", (artifact_ref,)).fetchone()
        if row is None:
            raise LookupError(f"no artifact {artifact_ref}")
        tags = artifact_tags(conn, {artifact_ref: markers.get(artifact_ref, [])})[artifact_ref]
        body = f"{row['title'] or ''}\n{_strip(row['body'] or '')}"
        linked = _direct_links(conn, ("artifact", artifact_ref))
        linked |= {("excerpt", x) for x in markers.get(artifact_ref, [])}
        linked |= {("artifact", r) for r in json.loads(row["related_json"] or "[]")}
        return Source("artifact", artifact_ref, body, tags, fts.concept_labels_in(conn, body), linked)
    body = text or ""
    return Source("text", None, body, set(), fts.concept_labels_in(conn, body), set())


def _strip(md: str) -> str:
    md = re.sub(r"<!--.*?-->", " ", md, flags=re.S)
    return re.sub(r"[#>*_`|\[\]]", " ", md)


def _age_days(created: str | None) -> float:
    if not created:
        return 365.0
    try:
        if len(created) == 10:
            d = datetime.combine(date.fromisoformat(created), datetime.min.time(), tzinfo=timezone.utc)
        else:
            d = datetime.fromisoformat(created)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
    except ValueError:
        return 365.0
    return max(0.0, (datetime.now(timezone.utc) - d).total_seconds() / 86400)


def noisy_or(shared: set[Node], tag_weights: dict[str, float]) -> float:
    p = 1.0
    for kind, _ in shared:
        p *= 1.0 - tag_weights.get(kind, 0.0)
    return 1.0 - p


def _excerpt_rows(conn: sqlite3.Connection, ids: list[str]) -> dict[str, sqlite3.Row]:
    out = {}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        for r in conn.execute(
                f"SELECT e.*, p.title AS paper_title, p.year AS paper_year, p.authors_json FROM excerpts e "
                f"JOIN papers p ON p.id = e.paper_id WHERE e.id IN ({_in(len(chunk))})", chunk):
            out[r["id"]] = r
    return out


def excerpt_title(r: sqlite3.Row) -> str:
    return f"{author_year(r['authors_json'], r['paper_year'], r['paper_title'])}, {locator_label(r['locator'])} ({r['kind']})"


def excerpt_snippet(r: sqlite3.Row, n: int = 600) -> str:
    s = f"“{r['quote']}” Note: {r['my_note']}"
    return s if len(s) <= n else s[: n - 1] + "…"


def artifact_snippet(r: sqlite3.Row, n: int = 600) -> str:
    body = r["body"] or ""
    parts = []
    for head in ("Claim", "What did I figure out", "Evidence"):
        sec = art_files.section_body(body, head)
        if sec:
            parts.append(" ".join(_strip(sec).split()))
    s = " · ".join(p for p in parts if p and p != "...") or " ".join(_strip(body).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def related_with_query(conn: sqlite3.Connection, *, excerpt_id: str | None = None, artifact_ref: str | None = None,
                       text: str | None = None, limit: int = 20, explain: bool = False) -> tuple[list[Candidate], fts.Query]:
    w, tw, tau = weights(conn)
    markers = _artifact_markers(conn)
    src = resolve_source(conn, excerpt_id, artifact_ref, text, markers)
    self_node = (src.type, src.id) if src.id else None
    query = fts.build_query(conn, src.text, src.concept_labels)

    bm: dict[Node, float] = {}
    for i, s in fts.search_excerpts(conn, query, POOL + 1):
        bm[("excerpt", i)] = s
    for r, s in fts.search_artifacts(conn, query, POOL + 1):
        bm[("artifact", r)] = s

    # tag pool: excerpts sharing ≥1 claim/concept/case/camp tag, artifacts embedding such excerpts
    tag_pool: set[Node] = set()
    for kind, key in src.tags:
        if kind in TAG_KINDS:
            for r in conn.execute("SELECT src_id FROM associations WHERE src_type='excerpt' AND rel='about' AND status='accepted' "
                                  "AND dst_type=? AND dst_id=?", (kind, key)):
                tag_pool.add(("excerpt", r[0]))
        if kind == "camp":
            for r in conn.execute("SELECT id FROM excerpts WHERE camp = ?", (key,)):
                tag_pool.add(("excerpt", r[0]))
    a_tags = artifact_tags(conn, markers)
    strong_src = {t for t in src.tags if t[0] in TAG_KINDS}
    for ref, tags in a_tags.items():
        if tags & strong_src:
            tag_pool.add(("artifact", ref))
    hops = two_hop(conn, self_node, src.tags)

    pool = set(bm) | tag_pool | hops
    if self_node:
        pool.discard(self_node)
    pool -= src.linked

    ex_ids = [i for t, i in pool if t == "excerpt"]
    ex_rows = _excerpt_rows(conn, ex_ids)
    e_tags = excerpt_tags(conn, ex_ids) if ex_ids else {}
    art_rows = {r["ref"]: r for r in conn.execute("SELECT * FROM artifacts")}

    max_b = max((bm[n] for n in pool if n in bm), default=0.0)
    cands: list[Candidate] = []
    for node in pool:
        kind, key = node
        if kind == "excerpt":
            r = ex_rows.get(key)
            if r is None or r["status"] == "discarded":
                continue
            tags = e_tags.get(key, set())
            c = Candidate("excerpt", key, excerpt_title(r), excerpt_snippet(r), paper_id=r["paper_id"],
                          project=r["project_slug"], created=r["created_at"])
        else:
            r = art_rows.get(key)
            if r is None:
                continue
            tags = a_tags.get(key, set())
            c = Candidate("artifact", key, f"{key} · {r['title'] or ''}".strip(" ·"), artifact_snippet(r),
                          project=r["project"], created=r["created"] or None)
        shared = tags & src.tags
        c.bm25 = bm.get(node, 0.0)
        c.T = c.bm25 / max_b if max_b > 0 else 0.0
        c.G = noisy_or(shared, tw)
        c.L = 1.0 if node in hops else 0.0
        c.R = math.exp(-_age_days(c.created) / tau)
        c.score = w["T"] * c.T + w["G"] * c.G + w["L"] * c.L + w["R"] * c.R
        c.shared = sorted(f"{k}:{v}" for k, v in shared)
        if explain:
            c.explain = {"T": round(c.T, 3), "G": round(c.G, 3), "L": c.L, "R": round(c.R, 3), "bm25": round(c.bm25, 3),
                         "shared": c.shared, "weights": w}
        cands.append(c)

    cands.sort(key=lambda c: (-c.score, c.id))
    per_group: dict[str, int] = {}
    out: list[Candidate] = []
    for c in cands:
        if per_group.get(c.group, 0) >= PER_GROUP:
            continue
        per_group[c.group] = per_group.get(c.group, 0) + 1
        out.append(c)
        if len(out) >= limit:
            break
    return out, query


def related(conn: sqlite3.Connection, *, excerpt_id: str | None = None, artifact_ref: str | None = None,
            text: str | None = None, limit: int = 20, explain: bool = False) -> list[Candidate]:
    return related_with_query(conn, excerpt_id=excerpt_id, artifact_ref=artifact_ref, text=text,
                              limit=limit, explain=explain)[0]


def is_meaningful(c: Candidate, floor: float) -> bool:
    """A candidate worth showing to the model: lexical match above the floor, a real tag in common,
    or a two-hop link. Same project/paper plus recency alone is not a relation."""
    return c.T >= floor or c.L > 0 or any(s.split(":", 1)[0] in TAG_KINDS for s in c.shared)
