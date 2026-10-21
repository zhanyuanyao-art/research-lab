from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ... import weekly
from ...config import get_paths
from ...files import artifacts, datafiles
from ...files.weeks import week_of
from ...files.yamlio import StaleFileError
from ..context import get_conn, render

router = APIRouter()

NOW_WEEK_KEYS = ["this_week", "next_week", "puzzle"]


def week_context(conn: sqlite3.Connection, request: Request, row: sqlite3.Row | None = None, **extra):
    """`row` pins the output shown (a refused save shows the one being saved, which is not this
    week's once the week has turned); by default it is this week's latest output."""
    paths = get_paths()
    week = week_of()
    row = row or weekly.week_row(conn, week, paths)
    art = artifacts.load(paths.root / row["path"]) if row else None
    groups = weekly.basket(conn, week, art.body if art else None)
    n_basket = sum(len(g["excerpts"]) for g in groups)
    now, now_sha = weekly.now_form(paths)
    draft = None
    if row:
        from ...ai import queue
        last = queue.latest_for(conn, "artifact.draft", row["ref"])
        if last and last["status"] != "cancelled":
            draft = weekly.draft_view(conn, last["id"])
    ctx = {
        "title": "本周", "week": week, "row": row, "art": art,
        "text": art.text() if art else "", "sha": art.sha if art else "",
        "groups": groups, "basket_count": n_basket,
        "now_fields": {k: now.get(k, "") for k in NOW_WEEK_KEYS}, "now_sha": now_sha,
        "commit_message": weekly.commit_message(row), "git": weekly.git_status(paths),
        "draft": draft, "preset_type": request.query_params.get("type", ""),
        "default_project": str(now.get("project", "")),
        "saved": request.query_params.get("saved"), "inserted": request.query_params.get("inserted"),
    }
    ctx.update(extra)
    return ctx


@router.get("/week")
def week_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return render(request, conn, "pages/week.html", **week_context(conn, request))


@router.post("/week/save")
async def week_save(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    ref, text, sha = str(form.get("ref", "")), str(form.get("text", "")), str(form.get("sha", ""))
    paths = get_paths()
    artifacts.index(conn, paths)
    row = weekly.row_for(conn, ref)
    if row is None:
        return render(request, conn, "pages/week.html", **week_context(conn, request, error=f"没有 artifact {ref}。"))
    path = paths.root / row["path"]
    try:
        weekly.save_text(path, text, sha)
    except StaleFileError:
        ctx = week_context(conn, request, row=row, conflict=weekly.conflict(path, text.replace("\r\n", "\n")))
        ctx["text"] = text
        resp = render(request, conn, "pages/week.html", **ctx)
        resp.status_code = 409
        return resp
    except weekly.WeeklyError as e:
        ctx = week_context(conn, request, row=row, error=str(e))
        ctx["text"], ctx["sha"] = text, sha
        resp = render(request, conn, "pages/week.html", **ctx)
        resp.status_code = 400
        return resp
    artifacts.index(conn, paths)
    return RedirectResponse("/week?saved=1", status_code=303)


@router.post("/week/draft")
async def week_draft(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    ref = str(form.get("ref", ""))
    ids = [str(x) for x in form.getlist("excerpt_ids")]
    try:
        job_id = weekly.start_draft(conn, ref, ids)
    except weekly.WeeklyError as e:
        return render(request, conn, "partials/_draft_result.html", draft=None, error=str(e), sha="")
    row = weekly.row_for(conn, ref)
    a = artifacts.load(get_paths().root / row["path"])
    return render(request, conn, "partials/_draft_result.html", draft=weekly.draft_view(conn, job_id), sha=a.sha)


@router.get("/week/draft/{job_id}")
def week_draft_status(job_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    view = weekly.draft_view(conn, job_id)
    if view is None:
        return HTMLResponse("<p class='muted'>没有这个任务。</p>", status_code=404)
    sha = ""
    a = weekly.load_ref(conn, view["job"]["target_id"]) if view["job"]["kind"] == "artifact.draft" else None
    if a:
        sha = a.sha
    return render(request, conn, "partials/_draft_result.html", draft=view, sha=sha)


@router.post("/week/insert")
async def week_insert(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    job_id, sha = str(form.get("job_id", "")), str(form.get("sha", ""))
    indices = [int(x) for x in form.getlist("bullets") if str(x).isdigit()]
    try:
        _, n = weekly.insert_evidence(conn, job_id, indices, sha)
    except StaleFileError:
        return HTMLResponse("<p class='crit'>■ 此页面打开后 artifact 已被修改，本次没有写入。"
                            "请<a href='/week'>刷新</a>后再插入一次。</p>", status_code=409)
    except weekly.WeeklyError as e:
        return HTMLResponse(f"<p class='warn'>▲ {_esc(str(e))}</p>", status_code=400)
    return Response(status_code=204, headers={"HX-Redirect": f"/week?inserted={n}"})


@router.post("/week/now")
async def week_now(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Returns the whole page; the form swaps in only #now-panel via hx-select."""
    form = await request.form()
    fields = {k: str(form.get(k, "")) for k in NOW_WEEK_KEYS if k in form}
    status, message = 200, "已保存到 _data/now.yml。"
    try:
        weekly.save_now(fields, str(form.get("sha", "")), NOW_WEEK_KEYS)
    except StaleFileError:
        status, message = 409, "此页面打开后 now.yml 已在磁盘上被修改，本次没有写入；请刷新查看最新内容。"
    except weekly.WeeklyError as e:
        status, message = 400, str(e)
    ctx = week_context(conn, request, now_message=message, now_status=status)
    if status != 200:
        ctx["now_fields"].update(fields)
    resp = render(request, conn, "pages/week.html", **ctx)
    resp.status_code = status
    return resp


@router.post("/week/commit")
async def week_commit(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Returns the whole page; the form swaps in only #commit-panel via hx-select."""
    form = await request.form()
    ref, message = str(form.get("ref", "")), str(form.get("message", ""))
    status, result, error = 200, None, None
    try:
        result = weekly.commit(conn, ref, message)
    except weekly.WeeklyError as e:
        status, error = 400, str(e)
    ctx = week_context(conn, request, commit_result=result, commit_error=error)
    if error:
        ctx["commit_message"] = message
    resp = render(request, conn, "pages/week.html", **ctx)
    resp.status_code = status
    return resp


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
