"""/tasks: the 待办 list (本周 · 逾期 · 以后 · 搁置 · 已完成, and `tree` for the issue view with
sub-tasks), creating tasks and sub-tasks, editing, status changes, reordering and moving a due date
to this or next Friday (/postpone; no plan week since 0093). Data lives in _data/tasks.yml through
files.tasks; the page is the
only writer besides the chat commands. Row actions post with HTMX and swap the whole board back
(`hx-select="#task-board"`), so every row's sha and group placement stay current after each change."""
from __future__ import annotations

import re
import sqlite3
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from markupsafe import escape

from ...files import datafiles
from ...files import tasks as T
from ...files.weeks import week_add, week_of
from ...files.yamlio import StaleFileError
from ..context import get_conn, render

router = APIRouter()

FORM_FIELDS = ("title", "project", "note", "due", "words", "file")
STALE = "待办文件在此页面打开后已被修改，本次没有写入；请确认后再提交一次。"
# `next` may only be a path on this app: one leading slash, not `//host` or `/\host`, no scheme.
SAFE_NEXT_RE = re.compile(r"^/(?![/\\])[^\s]*$")


def safe_next(url: str | None) -> str | None:
    url = (url or "").strip()
    return url if url and SAFE_NEXT_RE.match(url) and "://" not in url else None


def _open_duplicate(values: dict[str, str]) -> dict | None:
    """An open task with the same title under the same 课题 (one-click sources would otherwise pile up)."""
    for t in T.read():
        if T.is_open(t) and t["project"] == values.get("project") and t["title"] == values.get("title"):
            return t
    return None


def _inline_result(text: str, ok: bool = True) -> HTMLResponse:
    """What an HTMX one-click source (the milestone ladder, chat cards) swaps in place of its button."""
    return HTMLResponse(f'<span class="chip {"ok" if ok else "warn"}">{escape(text)}</span> '
                        f'<a class="small" href="/tasks">查看待办</a>')


def _s(form, name: str) -> str:
    return str(form.get(name) or "").strip()


def _list_url(project: str | None, **params: str) -> str:
    q = {k: v for k, v in ({"project": project or ""} | params).items() if v}
    return "/tasks" + ("?" + "&".join(f"{k}={quote(v)}" for k, v in q.items()) if q else "")


def _back(request: Request, url: str) -> Response:
    """303 for a plain form; HX-Redirect for an HTMX post whose target is the whole page."""
    if request.headers.get("hx-request") == "true" and not request.headers.get("hx-target"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def _source_titles(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> dict[str, str]:
    """Output titles for the tasks hung under outputs, so a row can say 来自产出《…》 and link to it."""
    refs = sorted({t["source"] for t in rows if str(t.get("source") or "").startswith("ART-")})
    if not refs:
        return {}
    marks = ",".join("?" * len(refs))
    return {r["ref"]: r["title"] for r in conn.execute(f"SELECT ref, title FROM artifacts WHERE ref IN ({marks})", refs)}


def _page(request: Request, conn: sqlite3.Connection, *, project: str | None = None, form: dict | None = None,
          error: str | None = None, open_new: bool = False, editing: dict | None = None, status: int = 200,
          saved: str | None = None, view: str = "week"):
    current = week_of()
    rows = T.read()
    if project:
        rows = [t for t in rows if t["project"] == project]
    groups = T.groups(current, rows)
    order = [str(p["slug"]) for p in (datafiles.read("projects") or [])]
    view = view if view in T.VIEWS else "week"
    tree = T.tree(rows, view, current, project_order=order)
    counts = {k: len(v) for k, v in groups.items()}
    projects = {str(p["slug"]): dict(p) for p in (datafiles.read("projects") or [])}
    now = datafiles.read("now") or {}
    values = {"title": "", "project": project or str(now.get("project") or ""), "due": "", "note": ""}
    values.update({k: v for k, v in (form or {}).items() if k in values})
    resp = render(request, conn, "pages/tasks.html", title="待办", groups=groups, counts=counts, projects=projects,
                  filter_project=project or "", current_week=current, next_week=week_add(current, 1),
                  sha=T.file_sha(), values=values, error=error,
                  open_new=open_new or bool(error and not editing), editing=editing, saved=saved,
                  statuses=T.STATUSES, total=len(T.read()), tree=tree, view=view, file_problem=T.problem(),
                  source_titles=_source_titles(conn, rows))
    resp.status_code = status
    return resp


@router.get("/tasks")
def tasks_page(request: Request, project: str = "", saved: str = "", view: str = "week",
               conn: sqlite3.Connection = Depends(get_conn)):
    return _page(request, conn, project=project or None, saved=saved or None, view=view)


@router.get("/tasks/new")
def tasks_new(request: Request, project: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    return _page(request, conn, project=project or None, open_new=True)


@router.post("/tasks")
async def tasks_create(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """The 待办 page's form (plain POST, sha-guarded) and the one-click sources (HTMX, no sha): a failing
    milestone check posts title / project / source / note and gets a chip back in place of its button.
    `source` is an internal value (milestone id); pages show its Chinese name, never the id."""
    form = await request.form()
    values = {k: _s(form, k) for k in FORM_FIELDS}
    source = _s(form, "source")
    back_to = _s(form, "filter") or None
    # One-click sources (milestone ladder, chat cards) get a chip back in place of their button; a form
    # that asks for the page back (reply=page, the output page's 待办 row) is redirected to `next` instead.
    inline = (request.headers.get("hx-request") == "true" and bool(request.headers.get("hx-target"))
              and _s(form, "reply") != "page")
    if source:
        values["source"] = source
        dup = _open_duplicate(values)
        if dup is not None:
            if inline:
                return _inline_result("已在待办中")
            return _back(request, safe_next(_s(form, "next")) or _list_url(back_to))
    try:
        T.add(values, _s(form, "sha") or None)
    except StaleFileError:
        return _page(request, conn, project=back_to, form=values, error=STALE, status=409)
    except T.TaskError as e:
        if inline:
            return _inline_result(str(e), ok=False)
        return _page(request, conn, project=back_to, form=values, error=str(e), status=400)
    if inline:
        return _inline_result("已加入待办")
    return _back(request, safe_next(_s(form, "next")) or _list_url(back_to, saved="created"))


@router.get("/tasks/{task_id}/edit")
def task_edit(task_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    t = T.get(task_id)
    if t is None:
        return _page(request, conn, error="找不到这条待办。", status=404)
    return _page(request, conn, editing=t, form={k: t.get(k) or "" for k in FORM_FIELDS})


@router.post("/tasks/{task_id}/edit")
async def task_update(task_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    changes = {k: _s(form, k) for k in FORM_FIELDS if k in form}
    t = T.get(task_id)
    if t is None:
        return _page(request, conn, error="找不到这条待办。", status=404)
    try:
        T.update(task_id, changes, _s(form, "sha") or None)
    except StaleFileError:
        return _page(request, conn, editing=t, form=changes, error=STALE, status=409)
    except T.TaskError as e:
        return _page(request, conn, editing=t, form=changes, error=str(e), status=400)
    return _back(request, safe_next(_s(form, "next")) or _list_url(_s(form, "filter") or None, saved="edited", view=_s(form, "view")))


@router.post("/tasks/{task_id}/status")
async def task_status(task_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    try:
        T.set_status(task_id, _s(form, "status"))
    except KeyError:
        return _page(request, conn, error="找不到这条待办。", status=404)
    except T.TaskError as e:
        return _page(request, conn, project=_s(form, "filter") or None, error=str(e), status=400)
    return _back(request, safe_next(_s(form, "next")) or _list_url(_s(form, "filter") or None, view=_s(form, "view")))


POSTPONE_TO = {"this_friday": T.this_friday, "next_friday": T.next_friday}


@router.post("/tasks/{task_id}/postpone")
@router.post("/tasks/{task_id}/carry")          # a board rendered before 0093 posts here with a week
async def task_postpone(task_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """New due date: `to` = this_friday | next_friday (the row buttons, n / w), or an ISO `due`; an old
    form's `week` means that week's Friday. Nothing given: next Friday."""
    form = await request.form()
    to, week = _s(form, "to"), _s(form, "week")
    due = POSTPONE_TO[to]() if to in POSTPONE_TO else (_s(form, "due") or (T.week_due(week) if T.WEEK_RE.match(week) else T.next_friday()))
    try:
        T.postpone(task_id, due)
    except KeyError:
        return _page(request, conn, error="找不到这条待办。", status=404)
    except T.TaskError as e:
        return _page(request, conn, project=_s(form, "filter") or None, error=str(e), status=400)
    return _back(request, safe_next(_s(form, "next")) or _list_url(_s(form, "filter") or None, view=_s(form, "view")))


@router.post("/tasks/{task_id}/sub")
async def task_sub(task_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Add a sub-task: a title and an optional due date come in; the 课题 and parent are the parent's."""
    form = await request.form()
    parent = T.get(task_id)
    if parent is None:
        return _page(request, conn, error="找不到这条待办。", status=404)
    values = {"title": _s(form, "title"), "project": parent["project"], "parent": task_id}
    if _s(form, "due"):
        values["due"] = _s(form, "due")
    try:
        T.add(values, _s(form, "sha") or None)
    except StaleFileError:
        return _page(request, conn, project=_s(form, "filter") or None, error=STALE, status=409)
    except T.TaskError as e:
        return _page(request, conn, project=_s(form, "filter") or None, error=str(e), status=400)
    return _back(request, safe_next(_s(form, "next")) or _list_url(_s(form, "filter") or None, view=_s(form, "view")))


@router.post("/tasks/{task_id}/move")
async def task_move(task_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Reorder: `before` is the task this one goes in front of; empty puts it last."""
    form = await request.form()
    try:
        T.move(task_id, _s(form, "before") or None, _s(form, "sha") or None)
    except KeyError:
        return _page(request, conn, error="找不到这条待办。", status=404)
    except StaleFileError:
        return _page(request, conn, project=_s(form, "filter") or None, error=STALE, status=409)
    return _back(request, safe_next(_s(form, "next")) or _list_url(_s(form, "filter") or None, view=_s(form, "view")))
