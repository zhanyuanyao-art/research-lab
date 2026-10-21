"""Artifacts: the direction board, direction folders and their management, the artifact page with its
reading and editing tabs, and the new-artifact form. Every write carries the sha the user saw."""
from __future__ import annotations

import re
from urllib.parse import quote
import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from ruamel.yaml.comments import CommentedSeq

from ... import dashboard, i18n, weekly
from ...config import get_paths
from ...files import artifacts, datafiles, updates
from ...files import tasks as tasks_file
from ...files.weeks import week_of
from ...files.yamlio import StaleFileError, sha256_text
from ..context import chosen_direction, get_conn, render

router = APIRouter()
REF_RE = re.compile(r"^ART-\d{4}-\d{4}$")
STALE_MSG = "文件在此页面打开后已在磁盘上被修改，本次没有写入任何内容；页面现在显示的是最新文件。"


def _form_str(form, name: str) -> str:
    return str(form.get(name) or "").strip()


def _status(resp, code: int):
    resp.status_code = code
    return resp


# ---------------------------------------------------------------- board

ARCHIVE_VIEWS = ("all", "week", "archived")


def _board_context(conn: sqlite3.Connection, request: Request, **extra: Any) -> dict[str, Any]:
    """研究档案 as an issue list (docs/design/ARCHIVE-ISSUES.md §C, §H): one group per 方向 (the group head is
    the old folder card, kind counts included), a table row per output with its 待办 x/y, the 截止 derived
    from those tasks and the time of its latest 记录; filters 本周 · 全部 · 已归档, 课题 and 类别 (kind).
    Archived outputs (weekly.ARCHIVED) only show in 已归档. Rows sort by latest record, then week."""
    q = request.query_params
    view = q.get("view") if q.get("view") in ARCHIVE_VIEWS else "all"
    group = q.get("group") if q.get("group") in ("status", "direction") else "status"   # Linear groups by status
    kind, project = q.get("kind") or "", q.get("project") or ""
    direction = chosen_direction(request, scoped=bool(project))   # the direction bar, remembered across pages
    board = dashboard.artifact_board(conn, get_paths(), type_=q.get("type") or None, level=q.get("level") or None)
    current = board["current_week"]
    tasks = tasks_file.read()
    latest = updates.latest_at()
    comments: dict[str, int] = {}
    for u in updates.read():
        comments[u["target"]] = comments.get(u["target"], 0) + 1

    def keep(r) -> bool:
        if direction and (r["direction"] or "") != direction:
            return False
        archived = (r["status"] or "") in weekly.ARCHIVED
        if (view == "archived") != archived:
            return False
        if view == "week" and r["week"] != current:
            return False
        if kind and i18n.TYPE_KIND.get(r["type"]) != kind:
            return False
        return not project or r["project"] == project

    def row(r) -> dict[str, Any]:
        top = tasks_file.for_source(r["ref"], tasks)
        live = [t for t in top if t.get("status") != "parked"]
        kids = [x for t in top for x in tasks_file.children(t["id"], tasks)]
        dues = [t["due"] for t in top + kids if tasks_file.is_open(t) and t.get("due")]
        return {**dict(r), "progress": {"done": sum(1 for t in live if t.get("status") == "done"), "total": len(live)},
                "due": max(dues) if dues else None, "latest": latest.get(r["ref"]), "comments": comments.get(r["ref"], 0)}

    groups = []
    for shelf in board["shelves"] + [board["unsorted"]]:
        if direction and shelf["slug"] != direction:
            continue
        rows = sorted((row(r) for r in shelf["rows"] if keep(r)), key=lambda x: (x["latest"] or "", x["week"] or "", x["ref"]), reverse=True)
        groups.append({**shelf, "items": rows, "unsorted": shelf is board["unsorted"]})
    if group == "status":
        # one group per 成熟度, the issue status here: 初稿 → 成形 → 可入文
        flat = sorted((x for g in groups for x in g["items"]), key=lambda x: (x["latest"] or "", x["week"] or "", x["ref"]), reverse=True)
        groups = [{"slug": lvl, "name": i18n.LEVEL[lvl], "items": [x for x in flat if x["level"] == lvl], "unsorted": False, "status": True}
                  for lvl in i18n.LEVEL]
        # a file with no level, or one outside the three, still counts in 全部 N, so it gets a group of its own
        groups.append({"slug": "", "name": "未标成熟度", "items": [x for x in flat if x["level"] not in i18n.LEVEL],
                       "unsorted": False, "status": True})
    unarchived = lambda rows: sum(1 for r in rows if (r["status"] or "") not in weekly.ARCHIVED)  # noqa: E731
    all_rows = [r for s in board["shelves"] + [board["unsorted"]] for r in s["rows"]
                if not direction or (r["direction"] or "") == direction]
    counts = {"all": unarchived(all_rows),
              "week": sum(1 for r in all_rows if r["week"] == current and (r["status"] or "") not in weekly.ARCHIVED),
              "archived": sum(1 for r in all_rows if (r["status"] or "") in weekly.ARCHIVED)}
    return {"title": "研究档案", "statuses": weekly.STATUSES, **board, "groups": groups, "view": view, "view_counts": counts, "group": group,
            "filter_kind": kind, "filter_project": project, "dir_filter": direction,
            "dir_counts": {sh["slug"]: unarchived(sh["rows"]) for sh in board["shelves"]},
            "dir_total": sum(unarchived(sh["rows"]) for sh in board["shelves"] + [board["unsorted"]]), "shown": sum(len(g["items"]) for g in groups),
            "i18n_due": i18n.due_zh, **extra}


@router.get("/artifacts")
def artifact_board(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return render(request, conn, "pages/artifacts.html", **_board_context(conn, request))


# ---------------------------------------------------------------- new artifact

def _new_context(request: Request, form: dict | None = None, **extra: Any) -> dict[str, Any]:
    q = request.query_params
    form = form or {}
    direction = form.get("direction", q.get("direction", ""))
    now_project = str((datafiles.read("now") or {}).get("project", ""))
    project = form.get("project") or q.get("project") or ""
    if not project and direction:
        project = next((str(p["slug"]) for p in datafiles.read("projects") or []
                        if str(p.get("direction") or "") == direction), "")
    project = project or now_project
    if not direction:
        direction = next((str(p.get("direction") or "") for p in datafiles.read("projects") or []
                          if str(p.get("slug")) == project), "")
    return {"title": "新建产出", "form": form, "preset_type": form.get("type", q.get("type", "")),
            "preset_direction": direction, "default_project": project, **extra}


@router.get("/artifacts/review")
def artifacts_review(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    from ... import dashboard
    return render(request, conn, "pages/review.html", **dashboard.build(conn), title="积累回顾")


@router.get("/artifacts/new")
def artifact_new(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return render(request, conn, "pages/artifact_new.html", **_new_context(request))


@router.post("/artifacts")
async def artifact_create(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    nxt = _form_str(form, "next")
    try:
        path = weekly.create_artifact(_form_str(form, "type"), _form_str(form, "slug"), _form_str(form, "title"),
                                      _form_str(form, "level") or "seed", _form_str(form, "project"),
                                      direction=_form_str(form, "direction") or None)
    except weekly.WeeklyError as e:
        ctx = _new_context(request, {k: str(v) for k, v in form.items()}, error=str(e))
        return _status(render(request, conn, "pages/artifact_new.html", **ctx), 400)
    artifacts.index(conn, get_paths())
    ref = artifacts.load(path).ref
    target = nxt if nxt in ("/week",) else f"/artifacts/{ref}?tab=edit"
    return RedirectResponse(target, status_code=303)


# ---------------------------------------------------------------- directions

def _directions_context(conn: sqlite3.Connection, **extra: Any) -> dict[str, Any]:
    paths = get_paths()
    artifacts.index(conn, paths)
    counts = dashboard.direction_counts(conn)
    rows = []
    for d in datafiles.read("directions", paths) or []:
        slug = str(d["slug"])
        use = datafiles.direction_usage(slug, paths)
        n = (counts.get(slug) or {}).get("count", 0)
        rows.append({**dict(d), "keywords_text": ", ".join(str(k) for k in (d.get("keywords") or [])),
                     "usage": use, "artifact_count": n,
                     "deletable": not use["projects"] and not use["artifacts"] and not n})
    return {"title": "管理研究方向", "rows": rows, "sha": datafiles.file_sha("directions", paths), **extra}


@router.get("/artifacts/directions")
def directions_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return render(request, conn, "pages/directions.html",
                  **_directions_context(conn, saved=request.query_params.get("saved")))


def _directions_error(request: Request, conn: sqlite3.Connection, message: str, code: int, form: dict | None = None):
    ctx = _directions_context(conn, error=message, form=form or {})
    return _status(render(request, conn, "pages/directions.html", **ctx), code)


@router.post("/artifacts/directions")
async def direction_add(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    values = {k: _form_str(form, k) for k in ("slug", "key", "name", "name_en", "keywords", "description")}
    values["key"] = values["key"].strip().upper()     # the issue prefix; checked (and then fixed for good) by add_direction
    try:
        datafiles.add_direction(values, _form_str(form, "sha") or None)
    except StaleFileError:
        return _directions_error(request, conn, STALE_MSG, 409, values)
    except datafiles.DataFileError as e:
        return _directions_error(request, conn, str(e), 400, values)
    return RedirectResponse(f"/artifacts/directions?saved={values['slug']}", status_code=303)


@router.post("/artifacts/directions/{slug}")
async def direction_update(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    changes = {k: _form_str(form, k) for k in ("name", "name_en", "keywords", "description") if k in form}
    try:
        datafiles.update_direction(slug, changes, _form_str(form, "sha") or None)
    except KeyError:
        return _directions_error(request, conn, f"没有方向 {slug}。", 404)
    except StaleFileError:
        return _directions_error(request, conn, STALE_MSG, 409)
    except datafiles.DataFileError as e:
        return _directions_error(request, conn, str(e), 400)
    return RedirectResponse(f"/artifacts/directions?saved={slug}", status_code=303)


@router.post("/artifacts/directions/{slug}/delete")
async def direction_delete(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    artifacts.index(conn, get_paths())
    n = conn.execute("SELECT COUNT(*) FROM artifacts WHERE direction = ?", (slug,)).fetchone()[0]
    if n:
        return _directions_error(request, conn, f"方向 {slug} 下还有 {n} 个 artifact，不能删除。", 400)
    try:
        datafiles.delete_direction(slug, _form_str(form, "sha") or None)
    except KeyError:
        return _directions_error(request, conn, f"没有方向 {slug}。", 404)
    except StaleFileError:
        return _directions_error(request, conn, STALE_MSG, 409)
    except datafiles.DataFileError as e:
        return _directions_error(request, conn, str(e), 400)
    return RedirectResponse("/artifacts/directions?saved=deleted", status_code=303)


@router.get("/artifacts/d/{slug}")
def direction_folder(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    folder = dashboard.direction_folder(conn, slug, get_paths())
    if folder is None:
        ctx = _board_context(conn, request, error=f"没有研究方向 {slug}。")
        return _status(render(request, conn, "pages/artifacts.html", **ctx), 404)
    return render(request, conn, "pages/direction.html", title=str(folder["direction"].get("name") or slug), **folder)


# ---------------------------------------------------------------- artifact page (阅读 / 编辑)

def _detail_context(conn: sqlite3.Connection, ref: str, tab: str = "read", **extra: Any) -> dict[str, Any] | None:
    paths = get_paths()
    a = weekly.load_ref(conn, ref, paths)
    if a is None:
        return None
    row = weekly.row_for(conn, ref)
    linked = {str(x) for x in (a.get("related") or [])}
    all_refs = [dict(r) for r in conn.execute("SELECT ref, title FROM artifacts WHERE ref != ? ORDER BY week DESC, ref DESC", (ref,))
                if r["ref"] not in linked]
    level = str(a.get("level", ""))
    try:
        next_minor = weekly.bump_minor(str(a.get("version", "v0.1")))
    except weekly.WeeklyError:
        next_minor = None
    ordered = [r[0] for r in conn.execute("SELECT ref FROM artifacts ORDER BY ref")]
    i = ordered.index(ref)
    return {
        "title": str(a.get("title", ref)), "a": a, "row": row, "front": {k: a.get(k) for k in a.front}, "tab": tab, "wide": tab == "edit",
        "direction": row["direction"] if row else None, "direction_explicit": bool(str(a.get("direction", "") or "")),
        "related": weekly.related_view(conn, a), "versions": weekly.versions(row["path"], paths),
        "is_repo": weekly.is_repo(paths), "all_refs": all_refs, "counts": artifacts.counts(a.body),
        "next_minor": next_minor, "can_promote": level != "paper-ready",
        "prev_ref": ordered[i - 1] if i > 0 else None, "next_ref": ordered[i + 1] if i + 1 < len(ordered) else None,
        "statuses": weekly.STATUSES, "edit_body": artifacts.editable_body(a.body), "ai_notes": artifacts.ai_notes(a.body),
        **describe(artifacts.editable_body(a.body)),
        "notes_md": re.sub(r"\A\s*## AI notes[^\n]*\n", "", artifacts.ai_notes(a.body)),   # its own heading is the panel's
        "related_text": ", ".join(str(r) for r in (a.get("related") or [])),
        **_issue_context(ref),
        **extra,
    }


# The template's English section headings, as the 描述 shows them (the file keeps the English ones).
DESC_HEADINGS = {"what did i figure out that i didn't know last week?": "想清楚了什么", "what did i figure out": "想清楚了什么",
                 "claim": "主张", "evidence": "证据", "why it matters": "为什么重要", "what this challenges": "挑战了什么",
                 "next question": "下一个问题", "sources": "来源"}
# One template placeholder per line, in the shapes the file holds them: as templates/artifact.md writes them
# and as Toast UI re-serialises them after an in-place save (`*` bullets, renumbered lists, escaped dots).
_ELLIPSIS = r"(?:(?:\\?\.){3}|…)"
_PLACEHOLDER_LINE = re.compile(
    rf"^\s*(?:>\s*)?(?:[-*+]\s+|\d+[.)]\s+)?{_ELLIPSIS}\s*$"
    r"|^\s*(?:>\s*)?\*\*I currently think:\*\*\s*$"
    r"|^\s*(?:>\s*)?\*\*Why I think so\*\*\s*[—-]+\s*cases, works, data:\s*$"
    r"|^\s*[-*+]\s+Author \\?\(Year\\?\)\.\s+[*_]Title[*_]\.\s*$"
    # the same scaffolding as editor.js shows it in Chinese, should a save ever come back unswapped
    r"|^\s*\*\*我目前认为：\*\*\s*$|^\s*\*\*为什么这么认为\*\*：案例、文献、数据\s*$|^\s*[-*+]\s+作者（年份）。[*_]标题[*_]。\s*$")
# The template's lead-in labels in front of Leo's own words on the same line ("**I currently think:**IPE 视角下…"):
# the section heading already says it, and without a space after the colon Markdown shows the asterisks.
_LEAD_LABEL = re.compile(r"^(\s*(?:>\s*)?)\*\*(?:I currently think:|Why I think so|我目前认为：|为什么这么认为)\*\*\s*(?:[—-]+\s*cases, works, data:\s*|：案例、文献、数据\s*)?[:：]?\s*(?=\S)")
DESC_PREVIEW_CHARS = 400


def describe(body: str) -> dict[str, Any]:
    """The 描述 of an output as its issue page shows it (docs/design/ARCHIVE-ISSUES.md §B, Leo's 10-01
    redesign): only what Leo wrote. Template hints and sections that still hold only the template's
    placeholders are left out, the template's English headings read in Chinese, and the AI notes are
    not part of it (they sit in their own folded panel). The file itself is not touched."""
    text = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    kept: list[str] = []
    for i, part in enumerate(re.split(r"(?m)^(?=## )", text)):
        head, _, rest = part.partition("\n") if part.startswith("## ") else ("", "", part)
        lines = [_LEAD_LABEL.sub(r"\1", ln) for ln in rest.splitlines() if not _PLACEHOLDER_LINE.match(ln)]
        if not any(ln.strip() for ln in lines):
            continue
        if head:
            name = head[3:].strip()
            head = "## " + DESC_HEADINGS.get(name.lower(), name)
        section = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        kept.append((head + "\n" if head else "") + section + "\n")
    md = "\n".join(kept).strip()
    plain = re.sub(r"[#>*_`\[\]()-]|\s+", "", md)
    return {"desc_md": md, "desc_chars": len(plain), "desc_long": len(plain) > DESC_PREVIEW_CHARS}


UPDATES_SHOWN = 30


def _issue_context(ref: str) -> dict[str, Any]:
    """What the output page shows as an issue (docs/design/ARCHIVE-ISSUES.md §B, §H): the 待办 hung under
    it (top-level tasks with source = ref, each with its own sub-tasks), their progress x/y over the
    top-level ones that are not parked, the 截止 derived from them (latest due among open ones; outputs
    carry no due of their own, §H.2), and the 记录 oldest-first like an activity feed, the latest 30."""
    current = week_of()
    rows = tasks_file.read()
    top = tasks_file.for_source(ref, rows)
    nodes = []
    for t in top:
        subs = tasks_file.children(t["id"], rows)
        nodes.append({"task": t, "flags": tasks_file.flags(t, current),
                      "subs": [{"task": x, "flags": tasks_file.flags(x, current)} for x in subs],
                      "progress": {"done": sum(1 for x in subs if x.get("status") == "done"),
                                   "total": sum(1 for x in subs if x.get("status") != "parked")}})
    live = [t for t in top if t.get("status") != "parked"]
    every = top + [x["task"] for n in nodes for x in n["subs"]]
    dues = [t["due"] for t in every if tasks_file.is_open(t) and t.get("due")]
    # The same thread as the output's issue page (card 0097): its 记录 and the issues.yml comments and events.
    from ...files import issues as issues_file
    recs = issues_file.comments(ref)
    return {"issue_tasks": nodes, "issue_progress": {"done": sum(1 for t in live if t.get("status") == "done"), "total": len(live)},
            "issue_due": max(dues) if dues else None, "issue_updates": recs[-UPDATES_SHOWN:], "updates_sha": updates.file_sha(),
            "issue_updates_more": max(0, len(recs) - UPDATES_SHOWN), "current_week": current, "i18n_due": i18n.due_zh}


def _not_found(request: Request, conn: sqlite3.Connection, ref: str):
    ctx = _board_context(conn, request, error=f"没有 artifact {ref}。")
    return _status(render(request, conn, "pages/artifacts.html", **ctx), 404)


def _detail(request: Request, conn: sqlite3.Connection, ref: str, code: int = 200, tab: str | None = None, **extra: Any):
    tab = tab or ("edit" if request.query_params.get("tab") == "edit" else "read")
    ctx = _detail_context(conn, ref, tab, **extra) if REF_RE.match(ref) else None
    if ctx is None:
        return _not_found(request, conn, ref)
    return _status(render(request, conn, "pages/artifact.html", **ctx), code)


@router.get("/artifacts/{ref}")
def artifact_detail(ref: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _detail(request, conn, ref, saved=request.query_params.get("saved"))


@router.post("/artifacts/{ref}/front")
async def artifact_front(ref: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    a = weekly.load_ref(conn, ref) if REF_RE.match(ref) else None
    before = _watched(a) if a else None
    try:
        changed = weekly.update_front(
            conn, ref, _form_str(form, "sha"),
            level=_form_str(form, "level") or None, status=_form_str(form, "status") or None,
            version=_form_str(form, "version") or None, related_add=_form_str(form, "related_add") or None,
            bump=form.get("bump") == "minor", confirmed=form.get("confirm") == "yes")
    except StaleFileError:
        return _detail(request, conn, ref, 409, error=STALE_MSG)
    except weekly.WeeklyError as e:
        return _detail(request, conn, ref, 400, error=str(e))
    after = weekly.load_ref(conn, ref) if changed and before is not None else None
    if after is not None:
        _record_changes(ref, before, after)
    return RedirectResponse(f"/artifacts/{ref}?saved={','.join(changed) or 'nothing'}", status_code=303)


@router.post("/artifacts/{ref}/direction")
async def artifact_direction(ref: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Change only `direction` in the front matter (sha-guarded)."""
    form = await request.form()
    new, sha = _form_str(form, "direction"), _form_str(form, "sha")
    known = [str(d["slug"]) for d in datafiles.read("directions") or []]
    if new and new not in known:
        return _detail(request, conn, ref, 400, error=f"未知研究方向：{new}")
    a = weekly.load_ref(conn, ref) if REF_RE.match(ref) else None
    if a is None:
        return _not_found(request, conn, ref)
    if a.sha != sha:
        return _detail(request, conn, ref, 409, error=STALE_MSG)
    if not artifacts.set_front(a, "direction", new or None, after="project"):
        return RedirectResponse(f"/artifacts/{ref}?saved=nothing", status_code=303)
    try:
        artifacts.save(a, sha)
    except StaleFileError:
        return _detail(request, conn, ref, 409, error=STALE_MSG)
    artifacts.index(conn, get_paths())
    return RedirectResponse(f"/artifacts/{ref}?saved=direction", status_code=303)


META_FIELDS = ("title", "type", "level", "status", "version", "project", "direction", "related")


def _watched(a: artifacts.Artifact) -> dict[str, str]:
    """The front-matter values whose changes go into the output's 记录 as system lines."""
    return {k: str(a.get(k) or "") for k in ("level", "status", "version")}


def _record_changes(ref: str, before: dict[str, str], a: artifacts.Artifact) -> None:
    """One system line per watched change (docs/design/ARCHIVE-ISSUES.md §B, §H.4): maturity, version
    (a 记一次想法变化 bump; ordinary saves of the body write nothing), archive and restore. Outputs have
    no due date of their own (§H.2): it is the latest due among their open 待办."""
    after = _watched(a)
    if before["level"] != after["level"] and after["level"]:
        updates.record(ref, f"成熟度：{i18n.LEVEL.get(before['level'], before['level'] or '未定')} → {i18n.LEVEL.get(after['level'], after['level'])}")
    if before["status"] != after["status"]:
        if after["status"] in weekly.ARCHIVED and before["status"] not in weekly.ARCHIVED:
            updates.record(ref, f"已归档 · {i18n.STATUS[after['status']]}")
        elif before["status"] in weekly.ARCHIVED and after["status"] not in weekly.ARCHIVED:
            updates.record(ref, "从归档恢复")
        else:
            updates.record(ref, f"状态：{i18n.STATUS.get(after['status'], after['status'])}")
    if before["version"] != after["version"] and after["version"]:
        updates.record(ref, f"想法有变化：{before['version'] or 'v0.1'} → {after['version']}")



def _apply_meta(conn: sqlite3.Connection, a: artifacts.Artifact, form) -> list[str]:
    """Validate and apply the edit tab's front-matter form. Returns the changed keys."""
    paths = get_paths()
    types = [str(t["key"]) for t in datafiles.read("types", paths) or []]
    levels = [str(l["key"]) for l in datafiles.read("levels", paths) or []] or list(weekly.LEVEL_ORDER)
    projects = [str(p["slug"]) for p in datafiles.read("projects", paths) or []]
    directions = [str(d["slug"]) for d in datafiles.read("directions", paths) or []]
    v = {k: _form_str(form, k) for k in META_FIELDS if k in form}
    if "title" in v and not v["title"]:
        raise weekly.WeeklyError("标题不能为空。")
    if v.get("type") and types and v["type"] not in types:
        raise weekly.WeeklyError(f"未知类型：{v['type']}")
    if v.get("level") and v["level"] not in levels:
        raise weekly.WeeklyError(f"未知成熟度：{v['level']}")
    if v.get("status") and v["status"] not in weekly.STATUSES:
        raise weekly.WeeklyError(f"未知状态：{v['status']}")
    if v.get("version") and not weekly.VERSION_RE.match(v["version"]):
        raise weekly.WeeklyError(f"版本号 {v['version']} 不是 v0.1 这样的格式。")
    if v.get("project") and projects and v["project"] not in projects:
        raise weekly.WeeklyError(f"未知项目：{v['project']}")
    if v.get("direction") and v["direction"] not in directions:
        raise weekly.WeeklyError(f"未知研究方向：{v['direction']}")
    related = None
    if "related" in v:
        related = [r for r in re.split(r"[\s,，;；]+", v["related"]) if r]
        for r in related:
            if not REF_RE.match(r):
                raise weekly.WeeklyError(f"{r} 不是 artifact 编号（ART-YYYY-WWNN）。")
            if r == a.ref:
                raise weekly.WeeklyError("artifact 不能关联自己。")
            if weekly.row_for(conn, r) is None:
                raise weekly.WeeklyError(f"没有 artifact {r}。")
    old_level = str(a.get("level", ""))
    if v.get("level") and weekly.LEVEL_ORDER.get(v["level"], 0) > weekly.LEVEL_ORDER.get(old_level, 0) \
            and not artifacts.counts(a.body):
        raise weekly.WeeklyError("提升成熟度之前，请先写好「想清楚了什么」和「主张」两节。")
    changed = []
    for k in ("title", "type", "level", "status", "version", "project"):
        if v.get(k) and str(a.get(k, "")) != v[k]:
            artifacts.set_front(a, k, v[k])
            changed.append(k)
    if "direction" in v and str(a.get("direction", "") or "") != v["direction"]:
        artifacts.set_front(a, "direction", v["direction"] or None, after="project")
        changed.append("direction")
    if related is not None and [str(x) for x in (a.get("related") or [])] != related:
        seq = CommentedSeq(related)
        seq.fa.set_flow_style()
        artifacts.set_front(a, "related", seq)
        changed.append("related")
    return changed


def _back(form, default: str) -> str:
    """Where a row-menu action returns to: the page it was used on, if it is a path inside the app (one
    leading slash, not //host or /\\host, no scheme: the same rule as the 待办 routes)."""
    from .tasks import safe_next
    return safe_next(_form_str(form, "next")) or default


def _manage(request: Request, conn: sqlite3.Connection, ref: str, form, apply) -> Response:
    """Shared shape of the file-management actions: load, sha check, apply, reindex, go back."""
    a = weekly.load_ref(conn, ref) if REF_RE.match(ref) else None
    if a is None:
        return _not_found(request, conn, ref)
    sha = _form_str(form, "sha")
    if a.sha != sha:
        return _detail(request, conn, ref, 409, error=STALE_MSG)
    try:
        url = apply(a, sha)
    except StaleFileError:
        return _detail(request, conn, ref, 409, error=STALE_MSG)
    except weekly.WeeklyError as e:
        return _detail(request, conn, ref, 400, error=str(e))
    artifacts.index(conn, get_paths())
    return RedirectResponse(url, status_code=303)


@router.post("/artifacts/{ref}/rename")
async def artifact_rename(ref: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Rename = change the title. The file name and ref stay, so links and git history keep working."""
    form = await request.form()

    def apply(a, sha):
        if _apply_meta(conn, a, {"title": _form_str(form, "title")}):
            artifacts.save(a, sha)
        return _back(form, f"/artifacts/{ref}") + ("&" if "?" in _back(form, "") else "?") + "saved=title"
    return _manage(request, conn, ref, form, apply)


@router.post("/artifacts/{ref}/project")
async def artifact_project(ref: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Move an output to another 课题 (its direction follows unless set explicitly)."""
    form = await request.form()

    def apply(a, sha):
        if _apply_meta(conn, a, {"project": _form_str(form, "project")}):
            artifacts.save(a, sha)
        return _back(form, f"/artifacts/{ref}") + ("&" if "?" in _back(form, "") else "?") + "saved=project"
    return _manage(request, conn, ref, form, apply)


@router.post("/artifacts/{ref}/delete")
async def artifact_delete(ref: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Move the file to app/data/trash (recoverable by hand); the index drops the row."""
    form = await request.form()

    def apply(a, sha):
        row = weekly.row_for(conn, ref)
        artifacts.trash(a, sha)
        default = f"/artifacts/d/{row['direction']}" if row and row["direction"] else "/artifacts"
        back = _back(form, default)
        if back.startswith(f"/artifacts/{ref}"):
            back = default
        return back + ("&" if "?" in back else "?") + "deleted=" + quote(str(a.get("title") or ref))
    return _manage(request, conn, ref, form, apply)


@router.post("/artifacts/{ref}/meta")
async def artifact_meta(ref: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    a = weekly.load_ref(conn, ref) if REF_RE.match(ref) else None
    if a is None:
        return _not_found(request, conn, ref)
    sha = _form_str(form, "sha")
    if a.sha != sha:
        return _detail(request, conn, ref, 409, tab="edit", error=STALE_MSG)
    before = _watched(a)
    try:
        changed = _apply_meta(conn, a, form)
        if changed:
            artifacts.save(a, sha)
    except StaleFileError:
        return _detail(request, conn, ref, 409, tab="edit", error=STALE_MSG)
    except weekly.WeeklyError as e:
        return _detail(request, conn, ref, 400, tab="edit", error=str(e))
    if changed:
        _record_changes(ref, before, a)
    artifacts.index(conn, get_paths())
    back = _back(form, f"/artifacts/{ref}?tab=edit")
    return RedirectResponse(back + ("&" if "?" in back else "?") + f"saved={','.join(changed) or 'nothing'}", status_code=303)


@router.post("/artifacts/{ref}/body")
async def artifact_body(ref: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Save the body from the editor. The AI notes section and managed excerpt blocks are restored
    from disk (files.artifacts.merge_protected). The editor script sends `X-RL-Editor: 1` and gets
    200 → status fragment + X-RL-Sha; 409 → diff fragment (the editor keeps the user's text). A plain
    form post (no script) is redirected back to the edit tab."""
    form = await request.form()
    body, sha = str(form.get("body") or ""), _form_str(form, "sha")
    from_editor = request.headers.get("x-rl-editor") == "1"
    a = weekly.load_ref(conn, ref) if REF_RE.match(ref) else None
    if a is None:
        return HTMLResponse(f"<span class='crit'>■ 没有 artifact {ref}。</span>", status_code=404)
    try:
        new_sha = artifacts.save_body(a, body, sha)
    except StaleFileError:
        disk = a.path.read_text(encoding="utf-8")
        proposed = "---\n" + artifacts.dump_yaml(a.front) + "---\n" + artifacts.merge_protected(a.body, body)
        current_sha = sha256_text(disk)
        ctx = {"conflict": {"diff": weekly.diff_text(disk, proposed, "磁盘上的当前版本", "你的编辑"),
                            "current_sha": current_sha}, "a": a, "from_editor": from_editor, "mine": body}
        resp = render(request, conn, "partials/_editor_conflict.html", **ctx)
        resp.status_code = 409
        resp.headers["X-RL-Current-Sha"] = current_sha
        return resp
    artifacts.index(conn, get_paths())
    if not from_editor:
        return RedirectResponse(f"/artifacts/{ref}?tab=edit&saved=body", status_code=303)
    stamp = datetime.now().strftime("%H:%M")
    html = f"<span class='ok'>● 已保存 {stamp}</span>"
    missing = artifacts.missing_earned(a.body)
    if missing:
        names = "、".join(f"“{m}”" for m in missing)
        html += f" <span class='muted'>· {names} 写完后，这份产出才算本周完成</span>"
    return HTMLResponse(html, headers={"X-RL-Sha": new_sha, "X-RL-Saved-At": stamp})


@router.get("/artifacts/{ref}/body")
def artifact_body_source(ref: str, conn: sqlite3.Connection = Depends(get_conn)):
    """The editable body and sha as they are on disk now (the conflict view's 载入磁盘版本)."""
    a = weekly.load_ref(conn, ref) if REF_RE.match(ref) else None
    if a is None:
        return Response(status_code=404)
    return Response(artifacts.editable_body(a.body), media_type="text/markdown; charset=utf-8",
                    headers={"X-RL-Sha": a.sha})


@router.post("/artifacts/{ref}/archive")
async def artifact_archive(ref: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """归档 writes one of the two retirement statuses the front matter already has: absorbed-into-paper
    for a paper-ready output, superseded for the rest (ARCHIVE-ISSUES.md §H.2); the previous status is
    kept in `archived_from` for 恢复. Maturity is untouched. Archived outputs leave the archive list but
    keep their file, links and history."""
    form = await request.form()

    def apply(a, sha):
        before = _watched(a)
        if _form_str(form, "action") == "restore":
            if a.get("status") not in weekly.ARCHIVED:
                return _back(form, f"/artifacts/{ref}")
            back_to = str(a.get("archived_from") or "draft")
            artifacts.set_front(a, "status", back_to if back_to in ("draft", "active") else "draft")
            artifacts.set_front(a, "archived_from", None)
        else:
            if a.get("status") in weekly.ARCHIVED:
                return _back(form, f"/artifacts/{ref}")
            artifacts.set_front(a, "archived_from", str(a.get("status") or "draft"), after="status")
            artifacts.set_front(a, "status", "absorbed-into-paper" if a.get("level") == "paper-ready" else "superseded")
        artifacts.save(a, sha)
        _record_changes(ref, before, a)
        url = _back(form, f"/artifacts/{ref}")
        return url + ("&" if "?" in url else "?") + ("saved=restored" if _form_str(form, "action") == "restore" else "saved=archived")
    return _manage(request, conn, ref, form, apply)


@router.post("/artifacts/{ref}/updates")
async def artifact_update_add(ref: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Leo writes one line under the output's 记录 (纯文本，一句一条)."""
    form = await request.form()
    if not REF_RE.match(ref) or weekly.load_ref(conn, ref) is None:   # load_ref refreshes the index first
        return _not_found(request, conn, ref)
    try:
        updates.add(ref, "user", _form_str(form, "text"), _form_str(form, "sha") or None)
    except updates.UpdateError as e:
        return _detail(request, conn, ref, 400, error=str(e))
    except StaleFileError:
        return _detail(request, conn, ref, 409, error=STALE_MSG)
    return RedirectResponse(f"/artifacts/{ref}#updates", status_code=303)


async def _own_update(request: Request, conn: sqlite3.Connection, ref: str, uid: str, change) -> Response:
    """Edit or delete one of Leo's comments under an output (card 0097). The page asks with fetch and redraws the
    thread itself; a plain form post comes back to the page."""
    if not REF_RE.match(ref) or weekly.load_ref(conn, ref) is None:
        return _not_found(request, conn, ref)
    if not any(u["id"] == uid and u["target"] == ref for u in updates.read()):
        return _detail(request, conn, ref, 404, error="找不到这条评论。")
    form = await request.form()
    # The page names the updates.yml it was drawn from (?sha=, card 0112): a change made elsewhere since is refused.
    sha = request.query_params.get("sha") or _form_str(form, "sha") or None
    asked = request.headers.get("HX-Request") == "true"
    try:
        change(form, sha)
    except updates.UpdateError as e:
        return JSONResponse({"error": str(e)}, status_code=400) if asked else _detail(request, conn, ref, 400, error=str(e))
    except StaleFileError:
        msg = "评论已在别处更新，请刷新页面后再改。"
        return JSONResponse({"error": msg}, status_code=409) if asked else _detail(request, conn, ref, 409, error=msg)
    return RedirectResponse(f"/artifacts/{ref}#updates", status_code=303)


@router.post("/artifacts/{ref}/updates/{uid}/edit")
async def artifact_update_edit(ref: str, uid: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return await _own_update(request, conn, ref, uid, lambda form, sha: updates.edit(uid, _form_str(form, "text"), expected_sha=sha))


@router.post("/artifacts/{ref}/updates/{uid}/delete")
async def artifact_update_delete(ref: str, uid: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return await _own_update(request, conn, ref, uid, lambda form, sha: updates.delete(uid, expected_sha=sha))
