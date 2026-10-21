"""Literature map (method-layer §2): cytoscape elements, paper placement, and the debate and gap
tests of §2.4. Suggested (`proposed`) associations are drawn dashed and count toward no test."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..db.connection import now_iso

MAP_RELS = ("advances", "belongs", "contests", "via", "studies", "uses", "fails_on", "bears_on")
NODE_TYPES = ("paper", "claim", "camp", "case")


def camps(conn: sqlite3.Connection, slug: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM camps WHERE project_slug = ? ORDER BY COALESCE(sort, 999), label", (slug,)).fetchall()


def _camp_lookup(rows: list[sqlite3.Row]) -> dict[str, str]:
    out: dict[str, str] = {}
    for c in rows:
        out[str(c["label"]).lower()] = c["id"]
        out[str(c["id"]).lower()] = c["id"]
    return out


def paper_camps(conn: sqlite3.Connection, slug: str) -> dict[str, str]:
    """paper id -> camp id for papers the user placed in one of this project's camps.

    `papers.camp` (id or label) wins; otherwise an accepted `belongs` edge to a project camp."""
    lookup = _camp_lookup(camps(conn, slug))
    if not lookup:
        return {}
    out: dict[str, str] = {}
    for r in conn.execute("SELECT id, camp FROM papers WHERE camp IS NOT NULL AND camp != ''"):
        cid = lookup.get(str(r["camp"]).lower())
        if cid:
            out[r["id"]] = cid
    camp_ids = set(lookup.values())
    for a in conn.execute("SELECT src_id, dst_id FROM associations WHERE rel = 'belongs' AND src_type = 'paper' "
                          "AND dst_type = 'camp' AND status = 'accepted' ORDER BY id"):
        if a["dst_id"] in camp_ids and a["src_id"] not in out:
            out[a["src_id"]] = a["dst_id"]
    return out


def suggested_camps(conn: sqlite3.Connection, slug: str) -> dict[str, list[dict[str, Any]]]:
    camp_rows = {c["id"]: c for c in camps(conn, slug)}
    out: dict[str, list[dict[str, Any]]] = {}
    for a in conn.execute("SELECT id, src_id, dst_id, reason FROM associations WHERE rel = 'belongs' AND src_type = 'paper' "
                          "AND dst_type = 'camp' AND status = 'proposed'"):
        if a["dst_id"] in camp_rows:
            out.setdefault(a["src_id"], []).append(
                {"assoc_id": a["id"], "camp_id": a["dst_id"], "camp": camp_rows[a["dst_id"]]["label"], "reason": a["reason"]})
    return out


def project_paper_ids(conn: sqlite3.Connection, slug: str) -> set[str]:
    ids = set(paper_camps(conn, slug))
    ids |= {r[0] for r in conn.execute("SELECT paper_id FROM paper_projects WHERE project_slug = ?", (slug,))}
    ids |= {r[0] for r in conn.execute("SELECT DISTINCT paper_id FROM excerpts WHERE project_slug = ?", (slug,))}
    ids |= set(suggested_camps(conn, slug))
    return ids


def _family(a: Any) -> str:
    if isinstance(a, dict):
        return str(a.get("family") or a.get("last") or a.get("name") or "")
    s = str(a).strip()
    if not s:
        return ""
    return s.split(",")[0].strip() if "," in s else s.split()[-1]


def short_cite(p: sqlite3.Row | dict) -> str:
    try:
        authors = json.loads(p["authors_json"] or "[]")
    except (TypeError, ValueError):
        authors = []
    names = [n for n in (_family(a) for a in authors) if n]
    if not names:
        first = ""
    elif len(names) == 1:
        first = names[0]
    elif len(names) == 2:
        first = f"{names[0]} & {names[1]}"
    else:
        first = f"{names[0]} et al."
    return f"{first} {p['year'] or 'n.d.'}" if first else str(p["id"])


def claim_camps(conn: sqlite3.Connection, slug: str, pc: dict[str, str] | None = None) -> dict[int, set[str]]:
    """claim id -> camps of the papers that advance it (accepted edges only)."""
    pc = pc if pc is not None else paper_camps(conn, slug)
    out: dict[int, set[str]] = {}
    for a in conn.execute("SELECT src_id, dst_id FROM associations WHERE rel = 'advances' AND src_type = 'paper' "
                          "AND dst_type = 'claim' AND status = 'accepted'"):
        if a["src_id"] in pc:
            try:
                out.setdefault(int(a["dst_id"]), set()).add(pc[a["src_id"]])
            except ValueError:
                continue
    return out


def _endpoint_camps(t: str, i: str, pc: dict[str, str], cc: dict[int, set[str]], camp_ids: set[str]) -> set[str]:
    if t == "paper":
        return {pc[i]} if i in pc else set()
    if t == "claim":
        try:
            return set(cc.get(int(i), set()))
        except ValueError:
            return set()
    if t == "camp":
        return {i} if i in camp_ids else set()
    return set()


def contests_edges(conn: sqlite3.Connection, slug: str) -> list[sqlite3.Row]:
    """Accepted contests edges with at least one endpoint in this project."""
    pids = project_paper_ids(conn, slug)
    cids = {str(r[0]) for r in conn.execute("SELECT id FROM claims WHERE project_slug = ?", (slug,))}
    campids = {c["id"] for c in camps(conn, slug)}
    out = []
    for a in conn.execute("SELECT * FROM associations WHERE rel = 'contests' AND status = 'accepted'"):
        ends = [(a["src_type"], a["src_id"]), (a["dst_type"], a["dst_id"])]
        if any((t == "paper" and i in pids) or (t == "claim" and i in cids) or (t == "camp" and i in campids) for t, i in ends):
            out.append(a)
    return out


def map_checks(conn: sqlite3.Connection, slug: str) -> dict[str, dict[str, Any]]:
    """The debate test and the gap test of method-layer §2.4."""
    camp_rows = camps(conn, slug)
    labels = {c["id"]: c["label"] for c in camp_rows}
    camp_ids = set(labels)
    pc = paper_camps(conn, slug)
    cc = claim_camps(conn, slug, pc)
    populated = sorted({c for c in pc.values()}, key=lambda c: labels.get(c, c))

    parent = {c: c for c in populated}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in contests_edges(conn, slug):
        left = _endpoint_camps(a["src_type"], a["src_id"], pc, cc, camp_ids)
        right = _endpoint_camps(a["dst_type"], a["dst_id"], pc, cc, camp_ids)
        for l in left:
            for r in right:
                if l in parent and r in parent:
                    parent[find(l)] = find(r)
    groups: dict[str, list[str]] = {}
    for c in populated:
        groups.setdefault(find(c), []).append(labels.get(c, c))
    components = sorted(groups.values(), key=len, reverse=True)
    if len(populated) < 2:
        debate = {"passed": False, "components": components,
                  "detail": f"{len(populated)} camp{'s' if len(populated) != 1 else ''} with papers; the debate test needs at least 2"}
    elif len(components) == 1:
        debate = {"passed": True, "components": components,
                  "detail": f"contests edges connect all {len(populated)} camps with papers"}
    else:
        debate = {"passed": False, "components": components,
                  "detail": "not connected: " + " | ".join("{" + ", ".join(g) + "}" for g in components)}

    advanced = {(r["dst_type"], r["dst_id"]) for r in conn.execute(
        "SELECT dst_type, dst_id FROM associations WHERE rel IN ('advances','via') AND status = 'accepted'")}
    reached: dict[tuple[str, str], set[str]] = {}
    for a in conn.execute("SELECT * FROM associations WHERE rel = 'fails_on' AND status = 'accepted'"):
        cs = _endpoint_camps(a["src_type"], a["src_id"], pc, cc, camp_ids)
        if cs:
            reached.setdefault((a["dst_type"], a["dst_id"]), set()).update(cs)
    gaps = []
    for (t, i), cs in reached.items():
        if len(cs) >= 2 and (t, i) not in advanced:
            gaps.append({"node": f"{t}:{i}", "label": _node_label(conn, t, i), "camps": sorted(labels.get(c, c) for c in cs)})
    gap = {"passed": bool(gaps), "gaps": gaps,
           "detail": ("gap: " + "; ".join(f"{g['label']} (fails for {', '.join(g['camps'])})" for g in gaps)) if gaps
           else "no case or mechanism is reached by fails_on edges from 2 camps and advanced by no claim"}
    return {"debate": debate, "gap": gap}


def _node_label(conn: sqlite3.Connection, t: str, i: str) -> str:
    if t == "case":
        r = conn.execute("SELECT label FROM cases WHERE id = ?", (i,)).fetchone()
        return r["label"] if r else i
    if t == "claim":
        try:
            r = conn.execute("SELECT code, statement FROM claims WHERE id = ?", (int(i),)).fetchone()
        except ValueError:
            r = None
        return (f"{r['code']} " if r and r["code"] else "") + (r["statement"][:60] if r else i)
    if t == "paper":
        r = conn.execute("SELECT * FROM papers WHERE id = ?", (i,)).fetchone()
        return short_cite(r) if r else i
    if t == "camp":
        r = conn.execute("SELECT label FROM camps WHERE id = ?", (i,)).fetchone()
        return r["label"] if r else i
    return i


def elements(conn: sqlite3.Connection, slug: str) -> dict[str, Any]:
    """Cytoscape elements. Papers sit inside their camp (compound parent); proposed edges carry
    the `suggested` class; unplaced papers are returned separately for the tray."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    unplaced: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    for c in camps(conn, slug):
        nid = f"camp:{c['id']}"
        nodes.append({"data": {"id": nid, "label": c["label"], "kind": "camp", "core_claim": c["core_claim"] or ""},
                      "classes": "camp"})
        node_ids.add(nid)
    pc = paper_camps(conn, slug)
    sugg = suggested_camps(conn, slug)
    ids = sorted(project_paper_ids(conn, slug))
    if ids:
        marks = ",".join("?" * len(ids))
        papers = conn.execute(f"SELECT * FROM papers WHERE id IN ({marks}) ORDER BY year, id", ids).fetchall()
    else:
        papers = []
    for p in papers:
        label = short_cite(p)
        verified = p["metadata_status"] == "verified"
        if p["id"] in pc:
            nid = f"paper:{p['id']}"
            nodes.append({"data": {"id": nid, "label": label, "kind": "paper", "parent": f"camp:{pc[p['id']]}",
                                   "title": p["title"], "verified": verified},
                          "classes": "paper" + ("" if verified else " unverified")})
            node_ids.add(nid)
        else:
            unplaced.append({"id": p["id"], "label": label, "title": p["title"], "suggestions": sugg.get(p["id"], [])})
    for cl in conn.execute("SELECT * FROM claims WHERE project_slug = ? AND status != 'rejected' ORDER BY code, id", (slug,)):
        nid = f"claim:{cl['id']}"
        label = (f"{cl['code']}: " if cl["code"] else "") + (cl["statement"][:70] + ("…" if len(cl["statement"]) > 70 else ""))
        classes = f"claim kind-{cl['kind']}" + (" own" if cl["is_own"] else "") + (" suggested" if cl["status"] == "proposed" else "")
        nodes.append({"data": {"id": nid, "label": label, "kind": "claim", "claim_kind": cl["kind"]}, "classes": classes})
        node_ids.add(nid)
    for a in conn.execute("SELECT * FROM associations WHERE status IN ('accepted','proposed') AND rel IN (%s) ORDER BY id"
                          % ",".join("?" * len(MAP_RELS)), MAP_RELS):
        s, t = f"{a['src_type']}:{a['src_id']}", f"{a['dst_type']}:{a['dst_id']}"
        if a["rel"] == "belongs" and a["status"] == "accepted" and a["src_id"] in pc:
            continue
        if s not in node_ids and t not in node_ids:
            continue
        for end_type, end_id, nid in ((a["src_type"], a["src_id"], s), (a["dst_type"], a["dst_id"], t)):
            if nid in node_ids:
                continue
            if end_type == "case":
                r = conn.execute("SELECT * FROM cases WHERE id = ?", (end_id,)).fetchone()
                if r:
                    nodes.append({"data": {"id": nid, "label": r["label"], "kind": "case"}, "classes": "case"})
                    node_ids.add(nid)
            elif end_type == "puzzle":
                nodes.append({"data": {"id": nid, "label": "My puzzle", "kind": "puzzle"}, "classes": "puzzle"})
                node_ids.add(nid)
        if s not in node_ids or t not in node_ids:
            continue
        classes = f"rel-{a['rel']}" + (" suggested" if a["status"] == "proposed" else "")
        edges.append({"data": {"id": f"a{a['id']}", "source": s, "target": t, "rel": a["rel"], "label": a["rel"].replace("_", " "),
                               "status": a["status"], "origin": a["origin"], "assoc_id": a["id"], "stance": a["stance"]},
                      "classes": classes})
    return {"nodes": nodes, "edges": edges, "unplaced": unplaced, "placed_count": len(pc)}


def node_detail(conn: sqlite3.Connection, slug: str, node_id: str) -> dict[str, Any] | None:
    if ":" not in node_id:
        return None
    t, i = node_id.split(":", 1)
    out: dict[str, Any] = {"type": t, "id": i, "node": node_id, "label": _node_label(conn, t, i)}
    edges = conn.execute("SELECT * FROM associations WHERE status IN ('accepted','proposed') AND "
                         "((src_type = ? AND src_id = ?) OR (dst_type = ? AND dst_id = ?)) ORDER BY id", (t, i, t, i)).fetchall()
    out["edges"] = [{"id": e["id"], "rel": e["rel"], "status": e["status"], "origin": e["origin"],
                     "other": _node_label(conn, *((e["dst_type"], e["dst_id"]) if (e["src_type"], e["src_id"]) == (t, i)
                                                  else (e["src_type"], e["src_id"]))),
                     "outgoing": (e["src_type"], e["src_id"]) == (t, i)} for e in edges if e["rel"] != "about"]
    if t == "paper":
        p = conn.execute("SELECT * FROM papers WHERE id = ?", (i,)).fetchone()
        if not p:
            return None
        out["paper"] = p
        out["camp"] = paper_camps(conn, slug).get(i)
        out["excerpts"] = conn.execute("SELECT id, quote, locator, my_note, uses_json FROM excerpts WHERE paper_id = ? "
                                       "AND status != 'discarded' ORDER BY created_at DESC LIMIT 8", (i,)).fetchall()
    elif t == "camp":
        c = conn.execute("SELECT * FROM camps WHERE id = ?", (i,)).fetchone()
        if not c:
            return None
        out["camp_row"] = c
        pc = paper_camps(conn, slug)
        members = [pid for pid, cid in pc.items() if cid == i]
        out["members"] = [conn.execute("SELECT * FROM papers WHERE id = ?", (m,)).fetchone() for m in sorted(members)]
    elif t == "claim":
        try:
            out["claim"] = conn.execute("SELECT * FROM claims WHERE id = ?", (int(i),)).fetchone()
        except ValueError:
            return None
        if out["claim"] is None:
            return None
    elif t == "case":
        out["case"] = conn.execute("SELECT * FROM cases WHERE id = ?", (i,)).fetchone()
    return out


def assign_camp(conn: sqlite3.Connection, slug: str, paper_id: str, camp_id: str) -> None:
    camp = conn.execute("SELECT id FROM camps WHERE id = ? AND project_slug = ?", (camp_id, slug)).fetchone()
    if not camp:
        raise ValueError(f"camp {camp_id!r} is not a camp of {slug}")
    if not conn.execute("SELECT 1 FROM papers WHERE id = ?", (paper_id,)).fetchone():
        raise ValueError(f"paper {paper_id!r} not found")
    now = now_iso()
    conn.execute("UPDATE papers SET camp = ?, updated_at = ? WHERE id = ?", (camp_id, now, paper_id))
    conn.execute("INSERT OR IGNORE INTO paper_projects(paper_id, project_slug) VALUES (?, ?)", (paper_id, slug))
    conn.execute(
        "INSERT INTO associations(src_type, src_id, rel, dst_type, dst_id, origin, status, created_at) "
        "VALUES ('paper', ?, 'belongs', 'camp', ?, 'user', 'accepted', ?) "
        "ON CONFLICT(src_type, src_id, rel, dst_type, dst_id) DO UPDATE SET status = 'accepted'",
        (paper_id, camp_id, now))


def parse_node(ref: str) -> tuple[str, str]:
    if ":" not in ref:
        raise ValueError(f"node reference {ref!r} must look like paper:<id> or claim:<id>")
    t, i = ref.split(":", 1)
    if t not in NODE_TYPES or not i:
        raise ValueError(f"node type must be one of {', '.join(NODE_TYPES)}")
    return t, i


def add_edge(conn: sqlite3.Connection, src: str, dst: str, rel: str = "contests", stance: str | None = None,
             reason: str | None = None) -> int:
    if rel not in MAP_RELS:
        raise ValueError(f"rel must be one of {', '.join(MAP_RELS)}")
    st, si = parse_node(src)
    dt, di = parse_node(dst)
    if (st, si) == (dt, di):
        raise ValueError("an edge needs two different nodes")
    conn.execute(
        "INSERT INTO associations(src_type, src_id, rel, dst_type, dst_id, stance, origin, status, reason, created_at) "
        "VALUES (?,?,?,?,?,?,'user','accepted',?,?) "
        "ON CONFLICT(src_type, src_id, rel, dst_type, dst_id) DO UPDATE SET status = 'accepted'",
        (st, si, rel, dt, di, stance, reason, now_iso()))
    return conn.execute("SELECT id FROM associations WHERE src_type = ? AND src_id = ? AND rel = ? AND dst_type = ? AND dst_id = ?",
                        (st, si, rel, dt, di)).fetchone()[0]


def set_edge_status(conn: sqlite3.Connection, assoc_id: int, status: str) -> None:
    if status not in ("accepted", "rejected"):
        raise ValueError("status must be accepted or rejected")
    row = conn.execute("SELECT * FROM associations WHERE id = ?", (assoc_id,)).fetchone()
    if not row:
        raise ValueError(f"association {assoc_id} not found")
    conn.execute("UPDATE associations SET status = ? WHERE id = ?", (status, assoc_id))
    if status == "accepted" and row["rel"] == "belongs" and row["src_type"] == "paper" and row["dst_type"] == "camp":
        p = conn.execute("SELECT camp FROM papers WHERE id = ?", (row["src_id"],)).fetchone()
        if p and not p["camp"]:
            conn.execute("UPDATE papers SET camp = ?, updated_at = ? WHERE id = ?", (row["dst_id"], now_iso(), row["src_id"]))


def node_options(conn: sqlite3.Connection, slug: str) -> list[dict[str, str]]:
    """Choices for the 'Add contests edge' form: placed papers and project claims."""
    pc = paper_camps(conn, slug)
    out = []
    for pid in sorted(pc):
        p = conn.execute("SELECT * FROM papers WHERE id = ?", (pid,)).fetchone()
        if p:
            out.append({"value": f"paper:{pid}", "label": short_cite(p)})
    for cl in conn.execute("SELECT id, code, statement FROM claims WHERE project_slug = ? AND status = 'accepted' ORDER BY code, id", (slug,)):
        out.append({"value": f"claim:{cl['id']}", "label": (f"{cl['code']}: " if cl["code"] else "") + cl["statement"][:60]})
    return out
