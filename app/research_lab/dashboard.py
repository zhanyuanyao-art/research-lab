"""Everything the Today page shows, computed from _data/*.yml, the artifact index and SQLite."""
from __future__ import annotations

import sqlite3
import subprocess
from datetime import date
from functools import partial
from typing import Any

from .config import Paths, get_paths
from .tunables import tunable
from .files import artifacts, datafiles, dates
from .files import tasks as tasks_file
from .files.weeks import week_add, week_of, week_start, weeks_between, weeks_since

LEVEL_RANK = {"seed": 1, "developed": 2, "paper-ready": 3}


def _counting_by_week(rows: list[sqlite3.Row]) -> dict[str, sqlite3.Row]:
    best: dict[str, sqlite3.Row] = {}
    for a in rows:
        if not a["counts"]:
            continue
        cur = best.get(a["week"])
        if cur is None or LEVEL_RANK.get(a["level"], 0) > LEVEL_RANK.get(cur["level"], 0):
            best[a["week"]] = a
    return best


def next_milestone(conn: sqlite3.Connection, slug: str) -> dict[str, Any] | None:
    try:
        from .method import milestones  # provided by the method module
    except ImportError:
        return None
    try:
        return milestones.next_unmet(conn, slug)
    except Exception:  # noqa: BLE001
        return None


def git_dirty(paths: Paths) -> list[str]:
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--", "_artifacts", "_data", "_harvests", "projects"],
                             cwd=paths.root, capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        return []
    return [l[3:] for l in out.splitlines() if l.strip()]


def project_progress(projects, current, rows=None, today=None):
    """Actual leaf-issue status counts, not an estimate of research/paper completion.

    Parents are omitted only while they have active children in this project. Archived
    and parked issues are outside the denominator. No task or research state is written.
    """
    from .files import issues
    from .pm.checkup import leaves as project_leaves   # shared with the assistant's progress check
    rows = issues.read() if rows is None else rows
    result = {}
    for p in projects:
        leaves = project_leaves(rows, p['slug'])
        counts = {state: sum(r['status'] == state for r in leaves) for state in ('todo', 'doing', 'done')}
        # `week`: 本周到期, open and due by Sunday (overdue included); there is no plan week since 0093.
        result[p['slug']] = dict(p, **counts, total=len(leaves),
                                 week=sum(tasks_file.due_this_week(r, today) for r in leaves))
    return result


def build(conn: sqlite3.Connection, paths: Paths | None = None, today: date | None = None) -> dict[str, Any]:
    paths = paths or get_paths()
    today = today or date.today()
    data = datafiles.read_all(paths)
    now = data.get("now") or {}
    artifacts.index(conn, paths)
    rows = conn.execute("SELECT * FROM artifacts ORDER BY ref").fetchall()
    current = week_of(today)
    counting = _counting_by_week(rows)
    # The cadence strip starts where the system started: the first artifact's
    # week, unless the user pins year.start_week in settings.
    start_week = cadence_start(conn, rows, current)
    this_week = [a for a in rows if a["week"] == current]
    week_art = max(this_week, key=lambda a: a["ref"]) if this_week else None
    latest = rows[-1] if rows else None
    last_counting_week = max(counting) if counting else None
    weeks_since_last = weeks_since(last_counting_week, today) if last_counting_week else None

    slug = str(now.get("project", ""))
    cur_project = data_project(data, slug)
    wk_start = week_start(current).isoformat()
    excerpts_week = conn.execute("SELECT COUNT(*) FROM excerpts WHERE created_at >= ?", (wk_start,)).fetchone()[0]
    works_placed = conn.execute(
        "SELECT COUNT(*) FROM papers p JOIN paper_projects pp ON pp.paper_id = p.id WHERE pp.project_slug = ? AND p.camp IS NOT NULL",
        (slug,)).fetchone()[0]
    works_target = tunable(conn, "map.target_works")
    cells = conn.execute(
        "SELECT ec.status, COUNT(*) n FROM evidence_cells ec JOIN claims c ON c.id = ec.claim_id WHERE c.project_slug = ? GROUP BY ec.status",
        (slug,)).fetchall()
    cells_total = sum(r["n"] for r in cells)
    cells_resolved = sum(r["n"] for r in cells if r["status"] in ("weak", "strong", "contradicting", "searched-none"))
    decisive = conn.execute(
        "SELECT ca.label FROM evidence_cells ec JOIN claims c ON c.id = ec.claim_id JOIN cases ca ON ca.id = ec.case_id "
        "WHERE c.project_slug = ? AND ec.test_type IN ('hoop','smoking-gun','doubly-decisive') AND ec.status = 'missing' "
        "ORDER BY ec.priority DESC LIMIT 1", (slug,)).fetchone()

    strip = cadence_strip(counting, start_week, current)
    plan = plan_view(current, today, paths)

    # This morning: at most three rule-based actions
    actions: list[dict[str, str]] = []
    if week_art is None:
        actions.append({"text": "本周还没有产出，先新建一份初稿。", "href": "/artifacts/new", "kind": "artifact"})
    elif not week_art["counts"]:
        actions.append({"text": "本周产出已建好，还差「想清楚了什么」和「主张」。", "href": f"/artifacts/{week_art['ref']}#describe", "kind": "artifact"})
    # The 本周 card itself asks what to do with overdue tasks (and on Monday and Tuesday for the week's
    # one line), so the strip says it once (docs/design/PM-AGENT.md §2.2: one nudge, time-windowed).
    if plan["overdue"]:
        actions.append({"text": f"有 {len(plan['overdue'])} 项待办已过截止，先定下怎么处理。", "href": "/#week-plan", "kind": "plan"})
    elif plan["state"] == "planning":
        actions.append({"text": "本周还没有到期的待办，先定下这周要做的事。", "href": "/#week-plan", "kind": "plan"})
    n_unsum = conn.execute(
        "SELECT COUNT(*) FROM papers p WHERE p.ai_summary_json IS NULL AND EXISTS (SELECT 1 FROM paper_files f WHERE f.paper_id = p.id)").fetchone()[0]
    if n_unsum:
        actions.append({"text": f"{n_unsum} 篇文献还没有中文概要", "href": "/papers?filter=unsummarized", "kind": "paper"})
    n_prop = conn.execute("SELECT COUNT(*) FROM associations WHERE status = 'proposed'").fetchone()[0]
    if n_prop:
        actions.append({"text": f"{n_prop} 条关联建议等待审阅。", "href": "/excerpts?filter=suggestions", "kind": "excerpt"})
    hunt = conn.execute(
        "SELECT ec.id, c.code, ca.label FROM evidence_cells ec JOIN claims c ON c.id = ec.claim_id JOIN cases ca ON ca.id = ec.case_id "
        "WHERE c.project_slug = ? AND ec.status = 'missing' ORDER BY ec.priority DESC, ec.id LIMIT 1", (slug,)).fetchone()
    if hunt and len(actions) < 3:
        actions.append({"text": f"有一条证据待查：{hunt['label']}", "href": f"/projects/{slug}/matrix", "kind": "evidence"})
    n_inbox = conn.execute("SELECT COUNT(*) FROM excerpts WHERE status = 'inbox'").fetchone()[0]
    if n_inbox and len(actions) < 3:
        actions.append({"text": f"{n_inbox} 条划线还没有整理进产出", "href": "/excerpts?filter=inbox", "kind": "excerpt"})
    actions = actions[:3]

    # Pipeline rows
    stage_keys = [s["key"] for s in (data.get("stages") or [])]
    pipeline = []
    for p in data.get("projects") or []:
        pslug = str(p["slug"])
        n_art = conn.execute("SELECT COUNT(*) FROM artifacts WHERE project = ?", (pslug,)).fetchone()[0]
        idx = stage_keys.index(p["stage"]) if p["stage"] in stage_keys else -1
        n_pap = conn.execute("SELECT COUNT(*) FROM paper_projects WHERE project_slug = ?", (pslug,)).fetchone()[0]
        pipeline.append({**dict(p), "artifact_count": n_art, "paper_count": n_pap, "stage_index": idx,
                         "stage_total": len(stage_keys), "next": next_milestone(conn, pslug)})

    recent_excerpts = conn.execute(
        "SELECT e.id, e.quote, e.locator, e.kind, e.status, e.created_at, p.id AS paper_id, p.title AS paper_title, p.year "
        "FROM excerpts e JOIN papers p ON p.id = e.paper_id WHERE e.private = 0 ORDER BY e.created_at DESC LIMIT 5").fetchall()
    latest_artifacts = list(reversed(rows[-5:]))

    goals = []
    for g in (data.get("year") or {}).get("goals", []):
        g = dict(g)
        auto = g.get("auto")
        if auto == "artifacts":
            done = sum(1 for a in rows if a["counts"])
        elif auto == "harvests":
            done = len(list(paths.harvests.glob("*.md")))
        elif auto == "papers":
            done = conn.execute("SELECT COUNT(*) FROM papers WHERE read_depth IN ('read','reread')").fetchone()[0]
        else:
            done = int(g.get("done", 0))
        target = int(g.get("target", 1)) or 1
        goals.append({**g, "done": done, "pct": min(100, round(done * 100 / target)), "reached": done >= target})

    from .ai import queue
    from . import home_notes
    from .feeds import sources as feed_sources, store as feed_store
    feed = feed_store.home(conn)
    front = feed_store.headlines(conn)
    # The single news slide keeps both lanes of 今日精选: two 世界要闻 in the brief's order, then a research item
    # when there is one (card 0083).
    research = [it for it in front["news"] if it.get("lane") != "world"] + feed["news"]
    candidates = feed["world"][:2] + research + feed["world"][2:]
    front["news"] = list({it["id"]: it for it in candidates}.values())[:3]
    feed_status = feed_store.status(conn)
    srcs = feed_sources.load()
    weekday = "一二三四五六日"[today.weekday()]
    return {
        "weekday": f"星期{weekday}", "today_label": f"{today.month}月{today.day}日",
        # 推送: the local feed engine (research_lab/feeds). `news` and `digest_status` keep their names for
        # older callers; the old IR&MORE digest is no longer read here (it stopped updating).
        "feed": feed, "feed_counts": feed_store.counts(conn), "st": feed_status,
        "front": front, "home_note": home_notes.for_day(today),
        "feed_journal_count": len(srcs.journals), "feed_news_count": len(srcs.news),
        "news": feed["news"], "news_date": feed["news"][0]["date"] if feed["news"] else None,
        "digest_status": feed_status,
        "now": now, "project": cur_project, "current_week": current, "week_artifact": week_art, "latest": latest,
        "actions": actions,
        "plan_state": plan["state"], "tasks_week": plan["week"], "tasks_done_week": plan["done"],
        "tasks_overdue": plan["overdue"], "tasks_open_total": plan["open_total"], "now_sha": plan["now_sha"],
        "tasks_parents": plan["parents"], "tasks_flat_week": plan["flat_week"], "tasks_flat_overdue": plan["flat_overdue"],
        "agenda": weekly_agenda(current, paths, today), "plan_fridays": (plan["this_friday"], plan["next_friday"]),
        # The home agenda colours due dates with the same rule as /tasks and the issue page, against this
        # page's day: overdue tasks.yml rows and overdue outputs (issues.yml only) both read red.
        "due_tone": partial(dates.tone, today=today),
        "progress_projects": list(project_progress(data.get("projects") or [], current).values()),
        "tiles": {
            "excerpts_week": excerpts_week, "weeks_since_last": weeks_since_last,
            "works_placed": works_placed, "works_target": works_target,
            "cells_resolved": cells_resolved, "cells_total": cells_total, "decisive": decisive["label"] if decisive else None,
        },
        "strip": strip, "pipeline": pipeline, "direction_counts": direction_counts(conn), "recent_excerpts": recent_excerpts, "latest_artifacts": latest_artifacts,
        "goals": goals, "year_label": (data.get("year") or {}).get("label", ""),
        "ai_counts": queue.counts(conn), "ai_mode": queue.mode(conn), "dirty": git_dirty(paths),
        "excerpt_total": conn.execute("SELECT COUNT(*) FROM excerpts").fetchone()[0],
    }


DUE_SOON_DAYS = 7


def weekly_agenda(current: str, paths: Paths | None = None, today: date | None = None) -> dict[str, Any]:
    """The homepage shows this week's actionable tasks, grouped by direction then project: due by this
    Sunday (overdue included) and those finished since Monday (Leo 10-10, no plan week). `current` is
    accepted for older callers and not used.

    Parents provide context, not a second completion button that would cascade to their children.
    A parent ready for review remains a navigable row after its children are finished.
    A closed parent never hides an open child. Historical plan_view fields remain for other callers.
    """
    rows = tasks_file.read(paths)
    by_id = {t['id']: t for t in rows}
    source_refs = {t.get('source') for t in rows if not t.get('parent') and str(t.get('source', '')).startswith('ART-')}
    source_titles = {}
    if source_refs:
        for path in artifacts.list_paths(paths):
            a = artifacts.load(path)
            if a.ref in source_refs:
                source_titles[a.ref] = str(a.get('title'))
    parents = {t.get('parent') for t in rows if t.get('parent')}
    projects = {p['slug']: p for p in datafiles.read('projects', paths) or []}
    directions = {d['slug']: d for d in datafiles.read('directions', paths) or []}
    def in_week(t):
        return tasks_file.due_this_week(t, today) or tasks_file.done_this_week(t, today)
    selected = []
    for t in rows:
        if not in_week(t):
            continue
        is_parent = t['id'] in parents
        if is_parent and (not tasks_file.is_open(t) or any(
                c.get('parent') == t['id'] and tasks_file.due_this_week(c, today) for c in rows)):
            continue
        parent = t.get('parent') or (t.get('source') if t.get('source') in source_titles else '')
        selected.append(dict(t, is_parent=is_parent, parent=parent,
                             parent_title=by_id.get(parent, {}).get('title', source_titles.get(parent, ''))))
    groups = {slug: dict(slug=slug, name=d['name'], projects=[]) for slug, d in directions.items()}
    # Retain orphaned historical tasks in a neutral group rather than silently losing work.
    for slug in dict.fromkeys([*projects, *(t['project'] for t in selected)]):
        p = projects.get(slug, {})
        items = [t for t in selected if t['project'] == slug]
        if not items:
            continue
        direction = p.get('direction', '')
        if direction not in groups:
            groups[direction] = dict(slug=direction, name='未分类', projects=[])
        pending = sorted((t for t in items if tasks_file.is_open(t)),
                         key=lambda t: (t.get('due') or '9999', t['status'] != 'doing', t['id']))
        done = sorted((t for t in items if t['status'] == 'done'),
                      key=lambda t: (t.get('done') or '', t['id']), reverse=True)
        groups[direction]['projects'].append(dict(slug=slug, name=p.get('name', '未归属课题'),
                                                 known=bool(p), pending=pending, done=done))
    brief = [dict(t, project_name=p['name'], project_known=p['known']) for g in groups.values() for p in g['projects'] for t in p['pending']]
    brief.sort(key=lambda t: (t.get('due') or '9999', t['status'] != 'doing', t['id']))
    completed = [dict(t, project_name=p['name'], project_known=p['known']) for g in groups.values() for p in g['projects'] for t in p['done']]
    completed.sort(key=lambda t: (t.get('done') or '', t['id']), reverse=True)
    # Projects are the parent issues. The home shows one collapsed row per project,
    # retaining ancestors for this week's children without exposing a bulk-complete action.
    from .files import issues as issue_file
    all_issues = {r['id']: r for r in issue_file.read()}
    roots = {r['project']: r for r in issue_file.project_roots()}
    parent_agenda = []
    active = [r for r in all_issues.values() if not r['archived']]
    active_parents = {r['parent'] for r in active if r['parent']}
    open_ancestors = set()
    for item in active:
        if not tasks_file.due_this_week(item, today):
            continue
        parent, seen = item['parent'], {item['id']}
        while parent in all_issues and parent not in seen and all_issues[parent]['project'] == item['project']:
            seen.add(parent)
            open_ancestors.add(parent)
            parent = all_issues[parent]['parent']
    week_items = [r for r in active if in_week(r)
                  and r['id'] not in open_ancestors
                  and not (r['id'] in active_parents and r['status'] not in tasks_file.OPEN)]
    for slug in dict.fromkeys(r['project'] for r in week_items):
        selected_rows = [r for r in week_items if r['project'] == slug]
        selected_ids = {r['id'] for r in selected_rows}
        shown = set(selected_ids)
        for ref in selected_ids:
            parent = all_issues[ref]['parent']
            seen = {ref}
            while parent in all_issues and parent not in seen and all_issues[parent]['project'] == slug:
                seen.add(parent)
                shown.add(parent)
                parent = all_issues[parent]['parent']
        members = [all_issues[ref] for ref in shown]
        def tree(row, trail):
            kids = sorted((x for x in members if x['parent'] == row['id'] and x['id'] not in trail),
                          key=lambda x: (x.get('due') or '9999', x['id']))
            return dict(issue=dict(row, postponed=by_id.get(row['id'], {}).get('postponed', 0)),
                        children=[tree(x, trail | {x['id']}) for x in kids],
                        actionable=row['id'] in selected_ids and row['id'] not in active_parents and row['id'] in by_id)
        nodes = [tree(x, {x['id']}) for x in members if x['parent'] not in shown]
        nodes.sort(key=lambda n: (n['issue'].get('due') or '9999', n['issue']['id']))
        root = roots.get(slug, {})
        p = projects.get(slug, {})
        parent_agenda.append(dict(slug=slug, key=root.get('key', ''), title=p.get('name', '未归属课题'), direction=p.get('direction', ''),
                                  url=root.get('url', '/tasks?view=week'), due=root.get('due', ''), status=root.get('status', 'todo'),
                                  nodes=nodes, pending=sum(r['status'] in tasks_file.OPEN for r in selected_rows),
                                  done=sum(r['status'] == 'done' for r in selected_rows), total=len(selected_rows)))
    parent_agenda.sort(key=lambda p: (p['due'] or '9999', p['title']))
    return dict(parents=[p for p in parent_agenda if p['pending']], completed_parents=[p for p in parent_agenda if not p['pending']], brief=brief[:5], recent_done=completed[:3], groups=[g for g in groups.values() if g['projects']],
                pending=sum(tasks_file.is_open(t) for t in selected),
                done=sum(t['status'] == 'done' for t in selected))


def parent_rows(rows: list[dict[str, Any]], current: str, today: date, paths: Paths | None = None) -> list[dict[str, Any]]:
    """Open parent tasks that still have open sub-tasks, earliest due first, each with its progress
    sentence (pm/progress.py) and `subs_this_week`: the open sub-tasks due by this Sunday (overdue
    included) or within DUE_SOON_DAYS (docs/design/TASKS.md §C). `current` is not used."""
    from .pm import progress
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for t in rows:
        if t.get("parent"):
            by_parent.setdefault(t["parent"], []).append(t)
    out = []
    for t in rows:
        subs = by_parent.get(t["id"], [])
        if not tasks_file.is_open(t) or not any(tasks_file.is_open(x) for x in subs):
            continue

        def due_soon(x: dict[str, Any]) -> bool:
            try:
                return bool(x.get("due")) and (date.fromisoformat(x["due"]) - today).days <= DUE_SOON_DAYS
            except ValueError:
                return False
        now = [x for x in subs if tasks_file.is_open(x) and (tasks_file.due_this_week(x, today) or due_soon(x))]
        d = progress.describe(t, subs, paths, today)
        out.append({"task": t, "sentence": d["sentence"], "describe": d, "subs": subs, "subs_this_week": now})
    return sorted(out, key=lambda p: (p["task"].get("due") or "9999", p["task"]["id"]))


def flat_rows(subset: list[dict[str, Any]], parents: list[dict[str, Any]], all_ids: set[str]) -> list[dict[str, Any]]:
    """`subset` without sub-tasks (they show under their parent, or folded under a done one) and without
    the parents that have their own line."""
    shown = {p["task"]["id"] for p in parents}
    return [t for t in subset if t["id"] not in shown and t.get("parent") not in all_ids]


def plan_view(current: str, today: date, paths: Paths | None = None) -> dict[str, Any]:
    """What the 本周 card needs from _data/tasks.yml (docs/design/PM-AGENT.md §2.7, TASKS.md §C).

    `state` is `planning` while some open task is past its due date (the card asks what to do with
    each: 本周五, 下周五, 搁置 or 完成) and on Monday and Tuesday while nothing is due this week (it asks
    for the week's one line); otherwise `running`. Since 0093 there is no plan week: `week` is open
    and due by Sunday but not yet overdue, `overdue` is past due, `done` finished since Monday.

    `parents` (one line each, with the progress sentence and the sub-tasks due now) and `flat_week` /
    `flat_overdue` (the rest, sub-tasks and those parents left out) are the issue-style split the card
    moves to (card 0020)."""
    rows = tasks_file.read(paths)
    open_week = [t for t in rows if tasks_file.flags(t, today=today)["this_week"]]
    done_week = sorted((t for t in rows if tasks_file.done_this_week(t, today)), key=lambda t: (t.get("done") or "", t["id"]), reverse=True)
    overdue = tasks_file.overdue(rows, today=today)
    state = "planning" if overdue or (today.weekday() <= 1 and not open_week) else "running"
    try:
        now_sha = datafiles.file_sha("now", paths)
    except OSError:
        now_sha = None
    parents = parent_rows(rows, current, today, paths)
    ids = {t["id"] for t in rows}
    return {"state": state, "week": open_week, "done": done_week, "overdue": overdue,
            "parents": parents, "flat_week": flat_rows(open_week, parents, ids), "flat_overdue": flat_rows(overdue, parents, ids),
            "open_total": sum(1 for t in rows if tasks_file.is_open(t)), "now_sha": now_sha,
            "this_friday": tasks_file.this_friday(today), "next_friday": tasks_file.next_friday(today)}


def cadence_strip(counting: dict[str, sqlite3.Row], start_week: str, current: str) -> list[dict[str, Any]]:
    """52 ISO weeks from `start_week`, each with the most mature counting artifact of that week."""
    strip = []
    for w in weeks_between(start_week, week_add(start_week, 51)):
        a = counting.get(w)
        strip.append({"week": w, "level": a["level"] if a else None, "title": a["title"] if a else None,
                      "ref": a["ref"] if a else None, "current": w == current, "past": w < current,
                      "empty_past": w < current and a is None})
    return strip


def cadence_start(conn: sqlite3.Connection, rows: list[sqlite3.Row], current: str) -> str:
    first_week = min((a["week"] for a in rows if a["week"]), default=current)
    return tunable(conn, "year.start_week") or first_week


# ---------------------------------------------------------------- Artifacts board (directions)

UNSORTED = "_none"
FOLDER_SECTIONS = {
    "maps": ("literature-map", "landscape"),
    "data": ("analysis", "evidence"),
    "conclusions": ("mechanism", "paragraph", "memo", "draft", "question", "puzzle"),
}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _group_by_type(rows: list[sqlite3.Row], type_order: list[str]) -> list[dict[str, Any]]:
    groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault(r["type"] or "", []).append(r)
    order = [t for t in type_order if t in groups] + sorted(t for t in groups if t not in type_order)
    return [{"type": t, "items": groups[t]} for t in order]


def _shelf(slug: str, d: dict | None, rows: list[sqlite3.Row], type_order: list[str], start: str,
           current: str) -> dict[str, Any]:
    return {
        "slug": slug, "name": (d or {}).get("name") or "未分类", "name_en": (d or {}).get("name_en") or "",
        "description": (d or {}).get("description") or "", "keywords": list((d or {}).get("keywords") or []),
        "count": len(rows), "counting": sum(1 for r in rows if r["counts"]),
        "latest_week": max((r["week"] for r in rows if r["week"]), default=None),
        "groups": _group_by_type(rows, type_order), "strip": cadence_strip(_counting_by_week(rows), start, current),
        "rows": rows, "kinds": _kind_counts(rows),
    }


def _kind_counts(rows: list[sqlite3.Row]) -> dict[str, int]:
    from .i18n import KIND_ORDER, TYPE_KIND
    out = {k: 0 for k in KIND_ORDER}
    for r in rows:
        k = TYPE_KIND.get(r["type"])
        if k:
            out[k] += 1
    return out


def artifact_board(conn: sqlite3.Connection, paths: Paths | None = None, type_: str | None = None,
                   level: str | None = None, status: str | None = None, today: date | None = None) -> dict[str, Any]:
    """One shelf per research direction (plus 未分类), artifacts grouped by type, a 52-week strip each."""
    paths = paths or get_paths()
    artifacts.index(conn, paths)
    rows_all = conn.execute("SELECT * FROM artifacts ORDER BY week DESC, ref DESC").fetchall()
    current = week_of(today)
    start = cadence_start(conn, rows_all, current)
    rows = [r for r in rows_all if (not type_ or r["type"] == type_) and (not level or r["level"] == level)
            and (not status or r["status"] == status)]
    type_order = [str(t["key"]) for t in (datafiles.read("types", paths) or [])]
    directions = [dict(d) for d in (datafiles.read("directions", paths) or [])]
    known = {str(d["slug"]) for d in directions}
    shelves = [_shelf(str(d["slug"]), d, [r for r in rows if r["direction"] == d["slug"]], type_order, start, current)
               for d in directions]
    unsorted = _shelf(UNSORTED, None, [r for r in rows if (r["direction"] or "") not in known], type_order, start, current)
    return {"shelves": shelves, "unsorted": unsorted, "total": len(rows_all), "shown": len(rows),
            "used_types": [t for t in type_order if any(r["type"] == t for r in rows_all)],
            "used_statuses": sorted({r["status"] for r in rows_all if r["status"]}),
            "filters": {"type": type_ or "", "level": level or "", "status": status or ""}, "current_week": current}


def direction_counts(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Per direction: artifacts, counting artifacts, newest week (for the dashboard's 按方向 line)."""
    out: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT direction, COUNT(*) n, SUM(counts) c, MAX(week) w FROM artifacts GROUP BY direction"):
        out[r["direction"] or UNSORTED] = {"count": r["n"], "counting": r["c"] or 0, "latest_week": r["w"]}
    return out


def direction_folder(conn: sqlite3.Connection, slug: str, paths: Paths | None = None,
                     today: date | None = None) -> dict[str, Any] | None:
    """Everything the folder page of one direction shows, or None for an unknown slug."""
    paths = paths or get_paths()
    d = datafiles.direction(slug, paths)
    if d is None:
        return None
    artifacts.index(conn, paths)
    rows_all = conn.execute("SELECT * FROM artifacts ORDER BY week DESC, ref DESC").fetchall()
    current = week_of(today)
    start = cadence_start(conn, rows_all, current)
    rows = [r for r in rows_all if r["direction"] == slug]
    type_order = [str(t["key"]) for t in (datafiles.read("types", paths) or [])]
    shelf = _shelf(slug, d, rows, type_order, start, current)
    stage_keys = [str(s["key"]) for s in (datafiles.read("stages", paths) or [])]
    projects = []
    for p in datafiles.read("projects", paths) or []:
        if str(p.get("direction") or "") != slug:
            continue
        pslug = str(p["slug"])
        projects.append({**dict(p), "stage_index": stage_keys.index(p["stage"]) if p.get("stage") in stage_keys else -1,
                         "artifact_count": sum(1 for r in rows_all if r["project"] == pslug),
                         "m3": _milestone(conn, pslug, "M3")})
    pslugs = [str(p["slug"]) for p in projects]
    n_papers = n_excerpts = 0
    if "direction" in _columns(conn, "papers"):
        n_papers = conn.execute("SELECT COUNT(*) FROM papers WHERE direction = ?", (slug,)).fetchone()[0]
    ex_cols = _columns(conn, "excerpts")
    if "direction" in ex_cols:
        n_excerpts = conn.execute("SELECT COUNT(*) FROM excerpts WHERE direction = ?", (slug,)).fetchone()[0]
    claims = []
    if pslugs:
        claims = conn.execute(
            f"SELECT * FROM claims WHERE status = 'accepted' AND project_slug IN ({','.join('?' * len(pslugs))}) "
            "ORDER BY project_slug, kind, code, id", pslugs).fetchall()
    return {
        "direction": d, "slug": slug, "shelf": shelf, "dir_projects": projects, "claims": claims,
        "maps": [r for r in rows if r["type"] in FOLDER_SECTIONS["maps"]],
        "data_artifacts": [r for r in rows if r["type"] in FOLDER_SECTIONS["data"]],
        "conclusions": [r for r in rows if r["type"] in FOLDER_SECTIONS["conclusions"]],
        "runs": _direction_runs(conn, slug),
        "counts": {"artifacts": len(rows), "counting": shelf["counting"], "projects": len(projects),
                   "papers": n_papers, "excerpts": n_excerpts},
    }


def _milestone(conn: sqlite3.Connection, slug: str, mid: str) -> dict[str, Any] | None:
    try:
        from .method import milestones
    except ImportError:
        return None
    try:
        for m in milestones.evaluate(conn, slug, write=False):
            if m.id == mid:
                f = m.failing
                return {"id": m.id, "title": m.title, "passed": m.passed, "failing": f.detail or f.label if f else ""}
    except Exception:  # noqa: BLE001 - the folder page must render even if the method layer cannot
        return None
    return None


def _direction_runs(conn: sqlite3.Connection, slug: str) -> list[dict[str, Any]]:
    """analysis_runs whose dataset or replication plan carries this direction (when the tables exist)."""
    run_cols = _columns(conn, "analysis_runs")
    if not run_cols:
        return []
    joins, conds = [], []
    if "dataset_id" in run_cols and "direction" in _columns(conn, "datasets"):
        joins.append("LEFT JOIN datasets ds ON ds.id = r.dataset_id")
        conds.append("ds.direction = :d")
    if "plan_id" in run_cols and "direction" in _columns(conn, "replication_plans"):
        joins.append("LEFT JOIN replication_plans rp ON rp.id = r.plan_id")
        conds.append("rp.direction = :d")
    if "direction" in run_cols:
        conds.append("r.direction = :d")
    if not conds:
        return []
    order = "r.created_at DESC" if "created_at" in run_cols else "r.rowid DESC"
    sql = f"SELECT r.* FROM analysis_runs r {' '.join(joins)} WHERE {' OR '.join(conds)} ORDER BY {order} LIMIT 50"
    try:
        return [dict(r) for r in conn.execute(sql, {"d": slug})]
    except sqlite3.Error:
        return []


def data_project(data: dict, slug: str) -> dict | None:
    for p in data.get("projects") or []:
        if str(p.get("slug")) == slug:
            return dict(p)
    return None
