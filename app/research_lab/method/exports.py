"""Exports: views become Markdown (method-layer §2.5, product-ux §5).

The app writes only between `<!-- rl:begin NAME -->` and `<!-- rl:end -->` markers (appending
them when absent) and never touches other text. In artifacts it fills tables, a Mermaid graph
and a source list; "What did I figure out" and "Claim" stay the user's (guardrail 6). Private
papers and excerpts never leave the database (guardrail 10)."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from ..config import get_paths
from ..files import artifacts as A
from ..files.weeks import week_of
from ..files.yamlio import atomic_write
from . import mapgraph, matrix
from .milestones import evaluate

END = "<!-- rl:end -->"


def begin(name: str) -> str:
    return f"<!-- rl:begin {name} -->"


def replace_block(text: str, name: str, content: str) -> str:
    """Replace the content of block NAME, or append the block at the end of `text`."""
    start = text.find(begin(name))
    block = f"{begin(name)}\n{content.strip()}\n{END}"
    if start == -1:
        return text.rstrip("\n") + "\n\n" + block + "\n"
    end = text.find(END, start)
    if end == -1:
        raise ValueError(f"block {name!r} has no {END}")
    return text[:start] + block + text[end + len(END):]


def _cell(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).replace("|", "\\|").strip()


def _papers_by_camp(conn: sqlite3.Connection, slug: str, verified_only: bool) -> dict[str, list[sqlite3.Row]]:
    pc = mapgraph.paper_camps(conn, slug)
    out: dict[str, list[sqlite3.Row]] = {}
    for pid, cid in pc.items():
        p = conn.execute("SELECT * FROM papers WHERE id = ?", (pid,)).fetchone()
        if not p or p["private"] or (verified_only and p["metadata_status"] != "verified"):
            continue
        out.setdefault(cid, []).append(p)
    for v in out.values():
        v.sort(key=lambda p: (p["year"] or 0, p["id"]))
    return out


def literature_table(conn: sqlite3.Connection, slug: str, verified_only: bool = False) -> str:
    rows = ["| Camp | Core claim | Key works | Mechanism | Cannot explain |", "|---|---|---|---|---|"]
    by_camp = _papers_by_camp(conn, slug, verified_only)
    for c in mapgraph.camps(conn, slug):
        ps = by_camp.get(c["id"], [])
        works = "; ".join(mapgraph.short_cite(p) + ("" if p["metadata_status"] == "verified" else " (unverified)") for p in ps)
        mechs = "; ".join(dict.fromkeys(p["mechanism"].strip() for p in ps if (p["mechanism"] or "").strip()))
        cannot = "; ".join(dict.fromkeys(p["cannot_explain"].strip() for p in ps if (p["cannot_explain"] or "").strip()))
        rows.append(f"| {_cell(c['label'])} | {_cell(c['core_claim'])} | {_cell(works)} | {_cell(mechs)} | {_cell(cannot)} |")
    return "\n".join(rows)


def _excerpt_refs(conn: sqlite3.Connection, cell_id: int) -> str:
    refs = []
    for e in matrix.cell_excerpts(conn, cell_id):
        if e["private"] or e["metadata_status"] == "candidate":
            continue
        refs.append(f"{e['id']} ({mapgraph.short_cite({'authors_json': e['authors_json'], 'year': e['year'], 'id': e['paper_id']})}"
                    f"{', ' + e['locator'] if e['locator'] else ''})")
    return "; ".join(refs)


def evidence_table(conn: sqlite3.Connection, slug: str) -> str:
    rows = ["| Date | Event / document | What it shows | Source | Artifact |", "|---|---|---|---|---|"]
    cells = [c for c in matrix.project_cells(conn, slug) if c["status"] in matrix.RESOLVED]
    cells.sort(key=lambda c: (c["date_start"] or "", c["case_sort"] or 0, c["code"] or ""))
    for c in cells:
        label = matrix.GLYPHS[c["status"]][1]
        imp = c["implication"] if c["implication_origin"] == "user" else ""
        shows = f"{c['code'] or '#' + str(c['claim_id'])} {label}" + (f": {imp}" if imp else "")
        rows.append(f"| {_cell(c['date_start'])} | {_cell(c['case_label'])} | {_cell(shows)} | {_cell(_excerpt_refs(conn, c['id']))} | |")
    return "\n".join(rows)


def _mid(text: str) -> str:
    return "n_" + re.sub(r"[^A-Za-z0-9]", "_", text)


def mermaid(conn: sqlite3.Connection, slug: str) -> str:
    """Camps plus the contests edges between them (a paper or claim stands for its camp)."""
    camps = mapgraph.camps(conn, slug)
    labels = {c["id"]: c["label"] for c in camps}
    pc = mapgraph.paper_camps(conn, slug)
    cc = mapgraph.claim_camps(conn, slug, pc)
    lines = ["```mermaid", "graph LR"]
    for c in camps:
        lines.append(f'  {_mid(c["id"])}["{c["label"].replace(chr(34), "")}"]')
    seen = set()
    for a in mapgraph.contests_edges(conn, slug):
        left = mapgraph._endpoint_camps(a["src_type"], a["src_id"], pc, cc, set(labels))
        right = mapgraph._endpoint_camps(a["dst_type"], a["dst_id"], pc, cc, set(labels))
        for l in left:
            for r in right:
                key = tuple(sorted((l, r)))
                if l != r and key not in seen:
                    seen.add(key)
                    lines.append(f"  {_mid(l)} <-- contests --> {_mid(r)}")
    lines.append("```")
    return "\n".join(lines)


def sources(conn: sqlite3.Connection, slug: str) -> str:
    """Only verified, non-private papers (guardrail 1)."""
    out = []
    for ps in _papers_by_camp(conn, slug, verified_only=True).values():
        for p in ps:
            venue = f" {p['venue']}." if p["venue"] else ""
            doi = f" doi:{p['doi']}" if p["doi"] else ""
            out.append((mapgraph.short_cite(p), f"- {mapgraph.short_cite(p)}. *{_cell(p['title'])}*.{venue}{doi}"))
    return "\n".join(line for _, line in sorted(out)) or "- (no verified sources yet)"


def _project_file(slug: str, name: str) -> Path:
    return get_paths().projects / slug / name


def write_project_table(slug: str, name: str, table: str) -> Path:
    path = _project_file(slug, name)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    atomic_write(path, replace_block(text, "table", table))
    return path


def write_literature_md(conn: sqlite3.Connection, slug: str) -> Path:
    return write_project_table(slug, "literature.md", literature_table(conn, slug))


def write_evidence_md(conn: sqlite3.Connection, slug: str) -> Path:
    return write_project_table(slug, "evidence.md", evidence_table(conn, slug))


def set_section_block(a: A.Artifact, heading: str, name: str, content: str, scaffold: str = "") -> None:
    """Put block NAME at the end of section `heading`, creating the section before Sources if missing."""
    secs = A.sections(a.body)
    target = next(((h, s, e) for h, s, e in secs if h.lower().startswith(heading.lower())), None)
    if target is None:
        new = f"## {heading}\n\n" + (scaffold.strip() + "\n\n" if scaffold else "") + f"{begin(name)}\n{content.strip()}\n{END}\n\n"
        src = next(((h, s, e) for h, s, e in secs if h.lower().startswith("sources")), None)
        if src:
            a.body = a.body[:src[1]] + new + a.body[src[1]:]
        else:
            a.body = a.body.rstrip("\n") + "\n\n" + new
        return
    _, s, e = target
    sec = a.body[s:e]
    if begin(name) in sec:
        sec = replace_block(sec, name, content)
    else:
        sec = sec.rstrip("\n") + f"\n\n{begin(name)}\n{content.strip()}\n{END}\n\n"
    a.body = a.body[:s] + sec + a.body[e:]


def _open_or_create(type_: str, art_slug: str, project: str, title: str) -> A.Artifact:
    paths = get_paths()
    path = paths.artifacts / f"{week_of()}-{art_slug}.md"
    if not path.exists():
        path = A.create(type_, art_slug, project, title=title)
    return A.load(path)


def _maybe_develop(a: A.Artifact, passed: bool) -> None:
    # The export click is the user's confirmation (guardrail 7); never downgrade.
    if passed and str(a.front.get("level", "seed")) == "seed":
        a.front["level"] = "developed"


def _milestone_passed(conn: sqlite3.Connection, slug: str, mid: str) -> bool:
    return next(m.passed for m in evaluate(conn, slug, write=False) if m.id == mid)


def _project_name(slug: str) -> str:
    from ..files import datafiles
    p = datafiles.project(slug)
    return str(p["name"]) if p else slug


def export_literature_map(conn: sqlite3.Connection, slug: str) -> str:
    write_literature_md(conn, slug)
    a = _open_or_create("literature-map", f"{slug}-literature-map", slug, f"Literature map: {_project_name(slug)}")
    set_section_block(a, "The terrain", "terrain", literature_table(conn, slug, verified_only=True) + "\n\n" + mermaid(conn, slug))
    if A.section_body(a.body, "The gap") is None:
        set_section_block(a, "The gap", "gap-nodes", _gap_hint(conn, slug),
                          scaffold="<!-- One paragraph, yours: the opening the paper walks through. -->\n\n...")
    else:
        set_section_block(a, "The gap", "gap-nodes", _gap_hint(conn, slug))
    set_section_block(a, "Sources", "sources", sources(conn, slug))
    _maybe_develop(a, _milestone_passed(conn, slug, "M3"))
    A.save(a)
    A.index(conn)
    return a.ref


def _gap_hint(conn: sqlite3.Connection, slug: str) -> str:
    gap = mapgraph.map_checks(conn, slug)["gap"]
    if not gap["gaps"]:
        return "*Gap test: no case or mechanism yet fails for two camps while no claim advances it.*"
    return "*Gap test candidates:* " + "; ".join(f"{g['label']} (fails for {', '.join(g['camps'])})" for g in gap["gaps"])


def export_evidence(conn: sqlite3.Connection, slug: str) -> str:
    write_evidence_md(conn, slug)
    a = _open_or_create("evidence", f"{slug}-evidence", slug, f"Evidence: {_project_name(slug)}")
    cov = matrix.coverage(conn, slug)
    v = matrix.verdicts(conn, slug)
    summary = (f"Coverage: {cov['resolved']} of {cov['total']} cells resolved ({cov['pct']}%). "
               "Process-tracing verdicts: " + ", ".join(f"{k} {val}" for k, val in v.items()) + ".")
    set_section_block(a, "Evidence table", "evidence-table", summary + "\n\n" + evidence_table(conn, slug))
    _maybe_develop(a, _milestone_passed(conn, slug, "M7"))
    A.save(a)
    A.index(conn)
    return a.ref


def export_check(conn: sqlite3.Connection, check_id: int) -> str:
    from . import checks as checks_mod
    from .concepts import slugify
    k = checks_mod.get(conn, check_id)
    if not k:
        raise ValueError(f"check {check_id} not found")
    if k["status"] != "recorded":
        raise ValueError("export a check once its result is recorded")
    if k["origin"] != "user":
        raise ValueError("accept the suggested check first")
    slug = k["project_slug"]
    art_slug = f"{slug}-check-{check_id}-{slugify(k['title'])[:40]}".rstrip("-")
    a = _open_or_create("analysis", art_slug, slug, f"Check: {k['title']}")
    claim = f"{k['claim_code'] or ''} {k['claim_statement'] or ''}".strip() or "(no claim linked)"
    rows = [
        ("Claim tested", claim), ("Prediction (written before the result)", k["prediction"]),
        ("Prediction recorded", (k["created_at"] or "")[:10]), ("Data", k["data_needed"]), ("Method", k["method"]),
        ("Would weaken the claim if", k["would_weaken_if"]), ("Script", k["script_ref"]),
        ("Result", k["result_note"]), ("Recorded", (k["updated_at"] or "")[:10]),
    ]
    table = "| Field | Value |\n|---|---|\n" + "\n".join(f"| {f} | {_cell(v)} |" for f, v in rows)
    set_section_block(a, "The check", "check", table)
    set_section_block(a, "Reading", "reading", k["reading"] or "")
    if k["script_ref"]:
        a.front["script_ref"] = k["script_ref"]
    A.save(a)
    conn.execute("UPDATE checks SET artifact_ref = ? WHERE id = ?", (a.ref, check_id))
    A.index(conn)
    return a.ref
