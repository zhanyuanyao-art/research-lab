"""/agent: the AI job queue, per-job detail, and run/requeue/cancel actions."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from markdown_it import MarkdownIt

from ...ai import cost, dispatch, queue
from ...config import get_paths, has_deepseek_key
from ...db.connection import get_setting
from ..context import get_conn, render

router = APIRouter()

# Prompts embed paper text; raw HTML in them must not render.
_safe_md = MarkdownIt("commonmark", {"html": False}).enable("table")

TARGET_URLS = {"paper": "/papers/{}", "excerpt": "/excerpts/{}", "artifact": "/artifacts/{}", "week": "/week",
               "harvest": "/harvest/{}", "project": "/projects/{}"}


def target_url(target_type: str | None, target_id: str | None) -> str | None:
    pattern = TARGET_URLS.get(target_type or "")
    if not pattern or not target_id:
        return None
    return pattern.format(target_id)


def age(iso: str | None, now: datetime | None = None) -> str:
    if not iso:
        return ""
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    s = int(((now or datetime.now(timezone.utc)) - then).total_seconds())
    if s < 60:
        return "刚刚"
    for unit, size in ((" 天前", 86400), (" 小时前", 3600), (" 分钟前", 60)):
        if s >= size:
            return f"{s // size}{unit}"
    return ""


def _job_costs(conn: sqlite3.Connection) -> dict[str, float]:
    return {r[0]: float(r[1] or 0) for r in
            conn.execute("SELECT job_id, SUM(cost_usd) FROM ai_runs GROUP BY job_id")}


def _ai_state(conn: sqlite3.Connection) -> dict:
    where, why = dispatch.route(conn)
    return {"mode_setting": get_setting(conn, "ai.mode", "auto"),
            "mode": queue.mode(conn), "route": where, "route_why": why, "has_key": has_deepseek_key(),
            "spent": cost.spent_today(conn), "cap": cost.daily_cap(conn), "model": dispatch.configured_model(conn)}


@router.get("/agent")
def agent_page(request: Request, status: str | None = None, note: str | None = None,
               conn: sqlite3.Connection = Depends(get_conn)):
    rows = queue.list_jobs(conn, status=status or None, limit=200)
    costs = _job_costs(conn)
    jobs = [{**dict(r), "url": target_url(r["target_type"], r["target_id"]), "age": age(r["created_at"]),
             "cost": costs.get(r["id"])} for r in rows]
    state = _ai_state(conn)
    return render(request, conn, "pages/agent.html", title="队列", wide=True, jobs=jobs, status=status or "",
                  counts=queue.counts(conn), ai=state, note=note,
                  can_run=state["mode"] == "deepseek" and state["has_key"],
                  polling=any(j["status"] == "claimed" for j in jobs))


def _runs(conn: sqlite3.Connection, job_id: str) -> list[dict]:
    root = get_paths().root
    out = []
    for r in conn.execute("SELECT * FROM ai_runs WHERE job_id = ? ORDER BY id", (job_id,)):
        d = dict(r)
        if d.get("log_path"):
            try:
                d["log_rel"] = str(Path(d["log_path"]).relative_to(root))
            except ValueError:
                d["log_rel"] = d["log_path"]
        out.append(d)
    return out


@router.get("/agent/{job_id}")
def agent_job(request: Request, job_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    job = queue.get(conn, job_id)
    if job is None:
        raise HTTPException(404, f"没有任务 {job_id}")
    try:
        md = queue.render_markdown(job)
    except KeyError:
        md = f"# AI 任务 {job['id']} · {job['kind']} · {job['status']}\n\n没有为 `{job['kind']}` 注册任务规范。"
    result = json.dumps(json.loads(job["result_json"]), indent=2, ensure_ascii=False) if job["result_json"] else None
    state = _ai_state(conn)
    return render(request, conn, "pages/agent_job.html", title=job_id, wide=True, job=dict(job),
                  md_html=_safe_md.render(md), result=result, runs=_runs(conn, job_id),
                  url=target_url(job["target_type"], job["target_id"]), age=age(job["created_at"]), ai=state,
                  can_run=state["mode"] == "deepseek" and state["has_key"],
                  input_files=json.loads(job["input_files_json"] or "[]"))


def _back(request: Request, next_url: str, note: str | None = None) -> Response:
    dest = next_url if next_url.startswith("/agent") else "/agent"
    if note:
        dest += ("&" if "?" in dest else "?") + "note=" + note
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": dest})
    return RedirectResponse(dest, status_code=303)


def _existing(conn: sqlite3.Connection, job_id: str):
    job = queue.get(conn, job_id)
    if job is None:
        raise HTTPException(404, f"没有任务 {job_id}")
    return job


@router.post("/agent/handover/{task_id}/start")
def handover_start(request: Request, task_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    """开始 under a hand-over plan (card 0116): the run step starts; back to the issue, where its comments land."""
    from ...agent import tasks as agent_tasks
    from ...files import issues
    try:
        ref, _ = agent_tasks.start_handover(conn, task_id)
    except agent_tasks.HandoverError as e:
        if request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"error": str(e)}, status_code=409)
        raise HTTPException(409, str(e)) from None
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"ok": True, "url": issues.url(ref)})
    return RedirectResponse(issues.url(ref), status_code=303)


@router.post("/agent/{job_id}/run")
def agent_run(request: Request, job_id: str, next: str = Form("/agent"), conn: sqlite3.Connection = Depends(get_conn)):
    job = _existing(conn, job_id)
    if job["status"] in ("failed", "cancelled"):
        queue.requeue(conn, job_id)
    outcome = dispatch.dispatch(job_id)
    return _back(request, next, f"{outcome}:{job_id}")


@router.post("/agent/{job_id}/requeue")
def agent_requeue(request: Request, job_id: str, next: str = Form("/agent"), conn: sqlite3.Connection = Depends(get_conn)):
    _existing(conn, job_id)
    queue.requeue(conn, job_id)
    return _back(request, next)


@router.post("/agent/{job_id}/cancel")
def agent_cancel(request: Request, job_id: str, next: str = Form("/agent"), conn: sqlite3.Connection = Depends(get_conn)):
    _existing(conn, job_id)
    queue.cancel(conn, job_id)
    return _back(request, next)
