"""一键跑完 on a replication plan (card 0195): the button that starts replication/pipeline.py and the status block the plan
page polls. The block is partials/_run_all_status.html; included without `run` it loads itself from
GET /data/plans/<plan>/pipeline, so the plan page needs no new context."""
from __future__ import annotations

import sqlite3
import time
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from ...replication import pipeline, plans
from ..context import get_conn, render

router = APIRouter()


def _plan_or_404(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    try:
        p = plans.get(conn, plan_id)
    except Exception:  # noqa: BLE001 - a malformed id is simply not a plan
        p = None
    if p is None:
        raise HTTPException(404, "没有这个复现方案")
    return p


def _go(request: Request, plan_id: str, **flash: str) -> Response:
    url = f"/data/plans/{plan_id}"
    if any(flash.values()):
        url += "?" + urlencode({k: v for k, v in flash.items() if v})
    url += "#run-all"
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


@router.post("/data/plans/{plan_id}/run-all")
def plan_run_all(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Leo's one click: every step of the replication in the background (a sync route, so FastAPI runs it off the event
    loop; the run itself is a thread with its own connection)."""
    _plan_or_404(conn, plan_id)
    try:
        started = pipeline.start(conn, plan_id)
    except pipeline.PipelineError as e:
        return _go(request, plan_id, err=str(e))
    return _go(request, plan_id, msg="已开始一键跑完。" if started else "已经在跑了，等这一轮结束。")


def _epoch(stamp: Any) -> float | None:
    try:
        return time.mktime(time.strptime(str(stamp), "%Y-%m-%d %H:%M:%S"))   # pipeline._stamp(), local time
    except (TypeError, ValueError, OverflowError):
        return None


def took(seconds: float | None) -> str:
    """"12 秒", "3 分 5 秒", "1 小时 4 分": the box's clock (card 0203); replication.js ticks the same format."""
    if seconds is None or seconds < 0:
        return ""
    s = int(seconds)
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分 {s % 60} 秒" if s % 60 else f"{s // 60} 分"
    return f"{s // 3600} 小时 {s % 3600 // 60} 分"


def timing(run: dict[str, Any]) -> dict[str, Any]:
    """Start time for the live clock, how long the run and each step took, and where the run is (step n of 9)."""
    start, end = _epoch(run.get("started")), _epoch(run.get("ended"))
    now = time.time()
    run["since"] = int(start) if start else None
    run["took"] = took(((now if run.get("running") else end) or now) - start) if start else ""
    run["ended_at"] = str(run.get("ended") or "")[11:16]
    for s in run.get("steps", []):
        a, b = _epoch(s.get("started")), _epoch(s.get("ended"))
        s["took"] = took((b or now) - a) if a and (b or s.get("state") == "running") else ""
    keys = [s.get("key") for s in run.get("steps", [])]
    run["at"] = keys.index(run["current"]) + 1 if run.get("current") in keys else None
    return run


@router.get("/data/plans/{plan_id}/pipeline")
def plan_pipeline(plan_id: str, request: Request, poll: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    """The status block. While a run is going it polls itself (?poll=1); the poll that sees the run end reloads the page,
    so the sections the run changed (清单, 待补数据, 展示稿) are drawn again."""
    p = _plan_or_404(conn, plan_id)
    run = pipeline.state(plan_id)
    if poll and not run["running"] and request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return render(request, conn, "partials/_run_all_status.html", p=p, run=timing(run))
