"""Year-1 milestone ladder (method-layer §1). Every definition of done is a count or a field
test over records; `1-min` items read `milestone_confirmations`. Suggestions (`proposed`
associations, `ai-suggested` checks and claims) count toward nothing (guardrail 5)."""
from __future__ import annotations

from ..i18n import KIND as L_KIND, TYPE_KIND

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import get_paths
from ..db.connection import now_iso
from ..files import datafiles
from . import concepts as concepts_mod
from . import mapgraph, matrix

LEVELS_AT_LEAST_DEVELOPED = ("developed", "paper-ready")


@dataclass
class Check:
    label: str
    passed: bool
    auto: bool = True
    detail: str = ""
    item: str | None = None          # confirmation key for 1-min items


@dataclass
class Milestone:
    id: str
    title: str
    gates_stage: str
    checks: list[Check]
    passed: bool
    transition: str = ""
    exports_as: str = ""
    behind: bool = False             # belongs to a stage the project has already left

    @property
    def failing(self) -> Check | None:
        return next((c for c in self.checks if not c.passed), None)


# id, title, stage it gates (the stage the project must finish), transition label, exports
LADDER = [
    ("M1", "Terrain surveyed", "exploring", "exploring", "landscape"),
    ("M2", "Puzzle stated", "exploring", "exploring → puzzle", "puzzle"),
    ("M3", "Literature map v1", "puzzle", "puzzle", "literature-map"),
    ("M4", "Concept fixed (v0)", "puzzle", "puzzle", "concept"),
    ("M5", "Question and rivals", "puzzle", "puzzle → evidence", "question, research-design"),
    ("M6", "Observation frame", "evidence", "evidence", "case-map"),
    ("M7", "Evidence coverage", "evidence", "evidence → argument", "evidence"),
    ("M8", "First empirical check", "argument", "argument", "analysis"),
    ("M9", "Mechanism and strongest rival", "argument", "argument", "mechanism, counterargument"),
    ("M10", "Design defended", "argument", "argument → draft", "research-design, advisor-question"),
    ("M11", "Paper-ready package", "draft", "draft", "paragraph, draft"),
]

# 1-min items: (milestone, item key) -> label
CONFIRM_ITEMS = {
    ("M2", "pattern"): "The puzzle artifact names the pattern",
    ("M2", "expectation"): "It names the expectation the pattern violates (e.g. structural realism expects great powers to bypass weak institutions)",
    ("M4", "indicators"): "Each indicator is observable in documents or data I can reach",
    ("M6", "frame"): "Universe and selection rule are fixed; each observation has a 'why this one' line I stand by",
    ("M9", "links"): "Each mechanism link is backed by the excerpt I attached, not by memory",
    ("M9", "rival"): "The counterargument targets the rival that scores best in the matrix",
    ("M10", "answer"): "The advisor's answer to the advisor-question is logged",
}


class _Ctx:
    """Queries shared by the milestone checks, computed once per evaluate()."""

    def __init__(self, conn: sqlite3.Connection, slug: str):
        self.conn, self.slug = conn, slug
        self.camps = mapgraph.camps(conn, slug)
        self.camp_labels = {c["id"]: c["label"] for c in self.camps}
        self.paper_camps = mapgraph.paper_camps(conn, slug)
        ids = sorted(self.paper_camps)
        self.papers = conn.execute(f"SELECT * FROM papers WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall() if ids else []
        self.confirmed = {(r["milestone"], r["item"]) for r in conn.execute(
            "SELECT milestone, item FROM milestone_confirmations WHERE project_slug = ?", (slug,))}
        self.artifacts = conn.execute("SELECT ref, type, level, title, counts FROM artifacts WHERE project = ? ORDER BY ref",
                                      (slug,)).fetchall()
        self.explanations = matrix.explanations(conn, slug)
        self.cells = matrix.project_cells(conn, slug)
        self.observations = matrix.observations(conn, slug)

    def arts(self, type_: str) -> list[sqlite3.Row]:
        return [a for a in self.artifacts if a["type"] == type_]

    def confirm(self, milestone: str, item: str) -> Check:
        ok = (milestone, item) in self.confirmed
        return Check(CONFIRM_ITEMS[(milestone, item)], ok, auto=False,
                     detail="confirmed" if ok else "tick after a one-minute read", item=item)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _artifact_check(ctx: _Ctx, type_: str, label: str, developed: bool = False) -> Check:
    rows = ctx.arts(type_)
    if not rows:
        return Check(label, False, detail=f"这个课题还没有{L_KIND.get(TYPE_KIND.get(type_, ''), type_)}类产出")
    if developed:
        good = [a for a in rows if a["level"] in LEVELS_AT_LEAST_DEVELOPED]
        if not good:
            return Check(label, False, detail=f"{rows[-1]['ref']} is at {rows[-1]['level'] or 'no level'}; needs developed")
        return Check(label, True, detail=f"{good[-1]['ref']} at {good[-1]['level']}")
    return Check(label, True, detail=rows[-1]["ref"])


def _m1(ctx: _Ctx) -> list[Check]:
    n = len(ctx.paper_camps)
    k = len(set(ctx.paper_camps.values()))
    return [
        Check("文献地图至少 12 篇", n >= 12, detail=f"已归入阵营 {n} / 12 篇"),
        Check("分布在至少 3 个阵营", k >= 3, detail=f"有文献的阵营 {k} / 3 个"),
        _artifact_check(ctx, "landscape", "研究版图达到成形", developed=True),
    ]


def _observation_excerpts(ctx: _Ctx) -> set[str]:
    linked = {r[0] for r in ctx.conn.execute(
        "SELECT dst_id FROM associations WHERE src_type = 'excerpt' AND dst_type = 'case' AND status = 'accepted'")}
    linked |= {c["case_id"] for c in ctx.cells if c["n_excerpts"]}
    return linked


def _m2(ctx: _Ctx) -> list[Check]:
    linked = _observation_excerpts(ctx)
    good = [o for o in ctx.observations if o["date_start"] and o["id"] in linked]
    return [
        _artifact_check(ctx, "puzzle", "写出一份谜题"),
        ctx.confirm("M2", "pattern"),
        Check("至少 3 个有日期且挂有片段的观察", len(good) >= 3,
              detail=f"符合条件的观察 {len(good)} / 3 个"
                     + (f"（共列出 {len(ctx.observations)} 个观察）" if ctx.observations else "")),
        ctx.confirm("M2", "expectation"),
    ]


def _points_at_puzzle(ctx: _Ctx, paper: sqlite3.Row, fails_on: set[str]) -> bool:
    if not (paper["cannot_explain"] or "").strip():
        return False
    return (paper["relation_to_puzzle"] or "") not in ("", "irrelevant") or paper["id"] in fails_on


def _m3(ctx: _Ctx) -> list[Check]:
    n = len(ctx.paper_camps)
    per_camp = {c["id"]: 0 for c in ctx.camps}
    for cid in ctx.paper_camps.values():
        per_camp[cid] = per_camp.get(cid, 0) + 1
    thin = [ctx.camp_labels.get(c, c) for c, k in per_camp.items() if k < 3]
    filled = [p for p in ctx.papers if all((p[f] or "").strip() for f in ("claim_text", "mechanism", "cannot_explain"))]
    pct = round(len(filled) * 100 / n) if n else 0
    contests = len(mapgraph.contests_edges(ctx.conn, ctx.slug))
    fails_on = {r[0] for r in ctx.conn.execute(
        "SELECT src_id FROM associations WHERE rel = 'fails_on' AND src_type = 'paper' AND status = 'accepted'")}
    no_gap = []
    for c in ctx.camps:
        members = [p for p in ctx.papers if ctx.paper_camps.get(p["id"]) == c["id"]]
        if not any(_points_at_puzzle(ctx, p, fails_on) for p in members):
            no_gap.append(c["label"])
    return [
        Check("至少 20 篇文献", n >= 20, detail=f"已归入 {n} / 20 篇"),
        Check("每个阵营至少 3 篇", bool(ctx.camps) and not thin,
              detail="还没有定义阵营" if not ctx.camps else
              (f"{len(thin)} 个阵营不足 3 篇（{', '.join(thin)}）" if thin else f"{len(ctx.camps)} 个阵营都已达到")),
        Check("八成文献填好了论点、机制和解释不了什么", n > 0 and pct >= 80,
              detail=f"{len(filled)} / {n} 篇（{pct}%）三项齐全"),
        Check("≥4 contests edges", contests >= 4, detail=f"{contests} of 4 accepted contests edges"),
        Check("每个阵营都指出了它解释不了的谜题部分", bool(ctx.camps) and not no_gap,
              detail="还没有定义阵营" if not ctx.camps else
              (f"缺少：{', '.join(no_gap)}" if no_gap else "每个阵营都有")),
    ]


def _definition_detail(s: dict[str, Any]) -> str:
    if not s["definition"] and not s["dimensions"]:
        return "还没有工作定义和维度"
    if not s["definition"]:
        return "还没有工作定义"
    if not s["dimensions"]:
        return "no dimensions named"
    return f"definition set, {_plural(s['dimensions'], 'dimension')}"


def _m4(ctx: _Ctx) -> list[Check]:
    linked = concepts_mod.for_project(ctx.conn, ctx.slug)
    if not linked:
        return [Check("a concept ledger for the project", False, detail="no concept ledger linked to this project"),
                ctx.confirm("M4", "indicators")]
    s = concepts_mod.m4_state(ctx.conn, linked[0]["id"])
    return [
        Check(f"≥4 sourced usages of '{s['label']}'", s["usages"] >= 4, detail=f"{s['usages']} of 4 definition excerpts linked"),
        Check("working definition with named dimensions", s["definition"] and s["dimensions"] > 0,
              detail=_definition_detail(s)),
        Check("≥1 observable indicator per dimension", s["dimensions"] > 0 and not s["missing_indicators"],
              detail=(f"no indicator for {', '.join(s['missing_indicators'])}" if s["missing_indicators"] else
                      "no dimensions" if not s["dimensions"] else "every dimension has an indicator")),
        ctx.confirm("M4", "indicators"),
    ]


def _m5(ctx: _Ctx) -> list[Check]:
    rivals = [h for h in ctx.explanations if not h["is_own"]]
    own = [h for h in ctx.explanations if h["is_own"]]
    impl: dict[int, int] = {}
    hoop: dict[int, int] = {}
    for c in ctx.cells:
        if (c["implication"] or "").strip() and c["implication_origin"] == "user" and c["status"] != "na":
            impl[c["claim_id"]] = impl.get(c["claim_id"], 0) + 1
            if c["test_type"] in ("hoop", "doubly-decisive"):
                hoop[c["claim_id"]] = hoop.get(c["claim_id"], 0) + 1
    short_impl = [matrix.row_key(h) for h in ctx.explanations if impl.get(h["id"], 0) < 2]
    no_hoop = [matrix.row_key(h) for h in ctx.explanations if not hoop.get(h["id"])]
    return [
        _artifact_check(ctx, "question", "写出研究问题"),
        Check("至少 3 个竞争解释加你自己的解释", len(rivals) >= 3 and len(own) >= 1,
              detail=f"竞争解释 {len(rivals)} 个，自己的 {len(own)} 个"),
        Check("each explanation has ≥2 observable implications you wrote", bool(ctx.explanations) and not short_impl,
              detail=(f"fewer than 2 written implications: {', '.join(short_impl)} (placeholders do not count)"
                      if short_impl else "all rows have ≥2") if ctx.explanations else "no explanations yet"),
        Check("each explanation has ≥1 implication marked necessary (hoop)", bool(ctx.explanations) and not no_hoop,
              detail=(f"no hoop implication: {', '.join(no_hoop)}" if no_hoop else "every row has a hoop")
              if ctx.explanations else "no explanations yet"),
    ]


def _frame_detail(obs: list, undated: list[str], no_why: list[str]) -> str:
    if not obs:
        return "还没有观察"
    parts = []
    if undated:
        parts.append(f"undated: {', '.join(undated[:3])}")
    if no_why:
        parts.append(f"no why-line: {', '.join(no_why[:3])}")
    return "; ".join(parts) or "all dated and justified"


def _m6(ctx: _Ctx) -> list[Check]:
    obs = ctx.observations
    undated = [o["label"] for o in obs if not o["date_start"]]
    no_why = [o["label"] for o in obs if not (o["selection_note"] or "").strip()]
    return [
        Check("8 到 15 个观察", 8 <= len(obs) <= 15, detail=f"目前 {len(obs)} 个（需要 8–15 个）"),
        Check("each has a date and a 'why this one' line", bool(obs) and not undated and not no_why,
              detail=_frame_detail(obs, undated, no_why)),
        ctx.confirm("M6", "frame"),
    ]


def _m7(ctx: _Ctx) -> list[Check]:
    cov = matrix.coverage(ctx.conn, ctx.slug, priority_only=True)
    attempted: dict[int, bool] = {}
    for c in ctx.cells:
        if c["test_type"] in ("hoop", "doubly-decisive") and (c["status"] not in ("missing", "na") or c["n_searches"]):
            attempted[c["claim_id"]] = True
    no_hoop = [matrix.row_key(h) for h in ctx.explanations if not attempted.get(h["id"])]
    contra = [c for c in ctx.cells if c["status"] == "contradicting" or (c["status"] == "searched-none" and c["n_searches"])]
    return [
        Check("七成优先格子已判定", cov["total"] > 0 and cov["ratio"] >= 0.7,
              detail=f"已判定 {cov['resolved']} / {cov['total']} 个优先格子（{cov['pct']}%）"),
        Check("every explanation has ≥1 attempted hoop test", bool(ctx.explanations) and not no_hoop,
              detail=(f"no attempted hoop for {', '.join(no_hoop)}" if no_hoop else "all attempted") if ctx.explanations
              else "no explanations yet"),
        Check("≥1 contradicting cell, or a logged 'searched, none found'", bool(contra),
              detail=f"{_plural(len(contra), 'cell')} contradicting or searched-none"),
    ]


def _script_exists(ref: str | None) -> bool:
    if not ref:
        return False
    p = Path(ref.split("@")[0].split("#")[0].strip())
    if not p.is_absolute():
        p = get_paths().root / p
    return p.is_file()


def _m8(ctx: _Ctx) -> list[Check]:
    rows = ctx.conn.execute("SELECT * FROM checks WHERE project_slug = ? AND origin = 'user' AND status = 'recorded'",
                            (ctx.slug,)).fetchall()
    scripted = [k for k in rows if _script_exists(k["script_ref"])]
    exported = [k for k in scripted if k["artifact_ref"]]
    return [
        Check("≥1 recorded check whose script is a committed file", bool(scripted),
              detail=f"{_plural(len(rows), 'recorded check')}, {len(scripted)} with a script file in the repo"),
        Check("its result exported as a dated analysis artifact", bool(exported),
              detail=exported[0]["artifact_ref"] if exported else "export the recorded check as an analysis artifact"),
    ]


def _m9(ctx: _Ctx) -> list[Check]:
    mechs = ctx.conn.execute("SELECT * FROM claims WHERE project_slug = ? AND kind = 'mechanism' AND status = 'accepted'",
                             (ctx.slug,)).fetchall()
    backed_assoc = {r[0] for r in ctx.conn.execute(
        "SELECT dst_id FROM associations WHERE src_type = 'excerpt' AND dst_type = 'claim' AND status = 'accepted'")}
    backed = [m for m in mechs if json.loads(m["source_excerpts_json"] or "[]") or str(m["id"]) in backed_assoc]
    best = matrix.best_rival(ctx.conn, ctx.slug)
    rival = ctx.confirm("M9", "rival")
    rival.detail = (f"best-scoring rival now: {best['code'] or best['statement'][:40]}" if best else "no rival scores above zero yet") \
        + ("; confirmed" if rival.passed else "")
    return [
        _artifact_check(ctx, "mechanism", "mechanism artifact"),
        Check("≥3 mechanism links, each backed by an excerpt", len(backed) >= 3,
              detail=f"{len(backed)} of {len(mechs)} mechanism links have an excerpt (need 3)"),
        ctx.confirm("M9", "links"),
        _artifact_check(ctx, "counterargument", "counterargument artifact"),
        rival,
    ]


def _m10(ctx: _Ctx) -> list[Check]:
    return [
        _artifact_check(ctx, "research-design", "research design at developed", developed=True),
        _artifact_check(ctx, "advisor-question", "advisor-question artifact"),
        ctx.confirm("M10", "answer"),
    ]


def _m11(ctx: _Ctx) -> list[Check]:
    ready = [a for a in ctx.artifacts if a["level"] == "paper-ready"]
    return [Check("≥3 paper-ready artifacts (lit review, case section, table or figure)", len(ready) >= 3,
                  detail=f"{len(ready)} of 3 paper-ready artifacts")]


CHECKERS = {"M1": _m1, "M2": _m2, "M3": _m3, "M4": _m4, "M5": _m5, "M6": _m6, "M7": _m7, "M8": _m8,
            "M9": _m9, "M10": _m10, "M11": _m11}


def stage_keys() -> list[str]:
    return [str(s["key"]) for s in (datafiles.read("stages") or [])]


def project_stage(slug: str) -> str | None:
    p = datafiles.project(slug)
    return str(p["stage"]) if p and p.get("stage") else None


def evaluate(conn: sqlite3.Connection, slug: str, write: bool = True) -> list[Milestone]:
    ctx = _Ctx(conn, slug)
    keys = stage_keys()
    stage = project_stage(slug)
    cur_idx = keys.index(stage) if stage in keys else -1
    out = []
    for mid, title, gates, transition, exports in LADDER:
        checks = CHECKERS[mid](ctx)
        behind = cur_idx >= 0 and gates in keys and keys.index(gates) < cur_idx
        out.append(Milestone(mid, title, gates, checks, all(c.passed for c in checks), transition, exports, behind))
    if write:
        write_lab_summary(conn, slug, out)
    return out


def _first_unmet(ms: list[Milestone]) -> Milestone | None:
    return next((m for m in ms if not m.passed and not m.behind), None)


def next_unmet(conn: sqlite3.Connection, slug: str) -> dict[str, str] | None:
    """{"milestone": "M3", "title": "Literature map v1", "failing": "<first failing check detail>"},
    or None when every milestone at or after the project's stage passes."""
    m = _first_unmet(evaluate(conn, slug))
    if m is None:
        return None
    f = m.failing
    return {"milestone": m.id, "title": m.title, "failing": (f.detail or f.label) if f else ""}


def stage_suggestion(conn: sqlite3.Connection, slug: str, ms: list[Milestone] | None = None) -> str | None:
    """The next stage key when every milestone gating the current stage passes."""
    keys = stage_keys()
    stage = project_stage(slug)
    if stage not in keys or keys.index(stage) + 1 >= len(keys):
        return None
    ms = ms if ms is not None else evaluate(conn, slug, write=False)
    gating = [m for m in ms if m.gates_stage == stage]
    if not gating or not all(m.passed for m in gating):
        return None
    return keys[keys.index(stage) + 1]


def confirm(conn: sqlite3.Connection, slug: str, milestone: str, item: str, checked: bool, note: str | None = None) -> None:
    if (milestone, item) not in CONFIRM_ITEMS:
        raise ValueError(f"{milestone}/{item} is not a one-minute item")
    if checked:
        conn.execute("INSERT INTO milestone_confirmations(project_slug, milestone, item, confirmed_at, note) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(project_slug, milestone, item) DO UPDATE SET confirmed_at = excluded.confirmed_at, note = excluded.note",
                     (slug, milestone, item, now_iso(), note))
    else:
        conn.execute("DELETE FROM milestone_confirmations WHERE project_slug = ? AND milestone = ? AND item = ?",
                     (slug, milestone, item))


def project_counts(conn: sqlite3.Connection, slug: str) -> dict[str, int]:
    pids = mapgraph.project_paper_ids(conn, slug)
    n_ex = conn.execute("SELECT COUNT(*) FROM excerpts WHERE project_slug = ?", (slug,)).fetchone()[0]
    if pids:
        n_ex += conn.execute(f"SELECT COUNT(*) FROM excerpts WHERE (project_slug IS NULL OR project_slug != ?) "
                             f"AND paper_id IN ({','.join('?' * len(pids))})", (slug, *sorted(pids))).fetchone()[0]
    cov = matrix.coverage(conn, slug)
    return {"papers": len(pids), "excerpts": n_ex, "cells_resolved": cov["resolved"], "cells_total": cov["total"]}


def _plain(x: Any) -> Any:
    return json.loads(json.dumps(x, default=str))


def write_lab_summary(conn: sqlite3.Connection, slug: str, ms: list[Milestone]) -> bool:
    """Update this project's entry in _data/lab.yml; rewrite the file only when a number changed."""
    m = _first_unmet(ms)
    f = m.failing if m else None
    entry = {"next_milestone": m.id if m else None, "failing": (f.detail or f.label) if f else None,
             **project_counts(conn, slug)}
    try:
        existing = _plain(datafiles.read("lab") or {})
    except Exception:  # noqa: BLE001 - a hand-broken lab.yml is regenerated
        existing = {}
    projects = existing.get("projects") if isinstance(existing, dict) else None
    projects = dict(projects or {})
    if projects.get(slug) == entry:
        return False
    projects[slug] = entry
    datafiles.write_lab({"projects": projects, "generated_at": now_iso()})
    return True
