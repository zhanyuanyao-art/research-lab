"""首页, plus the two writes the 本周 card makes: 过期的怎么处理 / 定下本周计划 (one POST decides every
overdue task and writes now.this_week) and the checklist tick. Both come back to / so HTMX can swap #home-tasks;
the task writes themselves go through files.tasks like the 待办 page's."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from ... import dashboard, weekly
from ...files import tasks as T
from ...files.yamlio import StaleFileError
from ..context import get_conn, render

router = APIRouter()
from .guide import router as guide_router
router.include_router(guide_router)

DECISIONS = ("friday", "next_friday", "park", "done", "carry")   # carry: a form rendered before 0093 (本周 → 本周五)


def _home(request: Request, url: str = "/") -> Response:
    if request.headers.get("hx-request") == "true" and not request.headers.get("hx-target"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


@router.get("/")
def today(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    from ... import coach
    guide = coach.state(conn)
    if guide["active"] and not guide["hidden"] and guide["step"] == 1:
        from .guide import page
        return page(request, conn)
    from ...feeds import brief, scheduler
    scheduler.refresh_news_on_view(conn, getattr(request.app.state, "feeds_http", None))
    q = request.query_params
    # the day's world-politics brief is the carousel's 今日要闻 page (card 0062); dashboard.build stays as it is
    return render(request, conn, "pages/today.html", **{"brief": brief.latest(conn), **dashboard.build(conn)}, title="首页",
                  planned=q.get("planned") == "1", plan_error=q.get("plan_error") or "", guide=guide)


@router.post("/plan")
async def plan_submit(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """`decision-<id>` = friday | next_friday | park | done for each overdue task shown (the due date
    moves to this or next Friday), `this_week` + `sha` for now.yml. Task decisions apply first; a stale
    now.yml then reports without undoing them."""
    form = await request.form()
    for key, value in form.multi_items():
        if not key.startswith("decision-") or value not in DECISIONS:
            continue
        task_id = key[len("decision-"):]
        try:
            if value in ("friday", "carry", "next_friday"):
                T.postpone(task_id, T.next_friday() if value == "next_friday" else T.this_friday())
            else:
                T.set_status(task_id, "parked" if value == "park" else "done")
        except (KeyError, T.TaskError):
            continue                       # the task changed under this form; the re-rendered card shows the truth
    if "this_week" in form:
        try:
            weekly.save_now({"this_week": str(form.get("this_week") or "")}, str(form.get("sha") or ""), ["this_week"])
        except StaleFileError:
            return _home(request, "/?plan_error=stale#home-tasks")
        except weekly.WeeklyError:
            return _home(request, "/?plan_error=now#home-tasks")
    return _home(request, "/?planned=1#home-tasks")


@router.post("/plan/task/{task_id}")
async def plan_task(task_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Tick (done) or untick (todo) one task on the 本周 checklist."""
    form = await request.form()
    status = str(form.get("status") or "done")
    if status not in ("done", "todo", "doing"):
        status = "done"
    try:
        T.set_status(task_id, status)
    except (KeyError, T.TaskError):
        pass
    return _home(request, "/#home-tasks")
