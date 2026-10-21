"""/replicate/<plan id>: the replication workspace (card 0158, docs/design/REPLICATION-v2.md 第三步). Leo sees the
plan, its datasets, the author scripts that will be translated (card 0184) and a token estimate, then Start, Stop and Re-run; progress is polled; finished runs list their
tables and figures. Nothing spends tokens without his click (replication/workspace.py)."""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse, Response

from ...replication import export, plans, rrun, workspace as W
from ..context import get_conn, render

log = logging.getLogger("research_lab.web.replicate")
router = APIRouter()

FILE_TYPES = {".png": "image/png", ".json": "application/json", ".csv": "text/csv; charset=utf-8",
              ".py": "text/plain; charset=utf-8", ".log": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8",
              ".jsonl": "text/plain; charset=utf-8", ".r": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
              ".tex": "text/plain; charset=utf-8", ".tsv": "text/plain; charset=utf-8"}


def _go(request: Request, plan_id: str, **flash: str) -> Response:
    url = f"/replicate/{plan_id}"
    if flash:
        url += "?" + urlencode({k: v for k, v in flash.items() if v})
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def _plan(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    try:
        p = plans.get(conn, plan_id)
    except Exception:  # noqa: BLE001 - a malformed id is simply not a plan
        p = None
    if p is None:
        raise HTTPException(404, "没有这个复现方案")
    return p


def _status_ctx(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    job = W.current_job(conn, plan_id)
    progress: dict[str, Any] = {}
    if job is not None:
        ws = W.workspace_dir(plan_id)
        try:                                         # the live workspace, read never through a link (0158 review)
            log, calls = W.safe_read(ws, "logs/run.log"), W.safe_read(ws, "logs/calls.jsonl")
        except W.WorkspaceError:
            log, calls = "", ""
        lines = []
        for x in calls.splitlines():
            try:
                lines.append(json.loads(x))
            except ValueError:
                continue
        files = W.regular_files(ws / "scripts") + W.regular_files(ws / "outputs")
        progress = {"log": log[-4000:], "calls": lines[-12:], "n_calls": len(lines),
                    "scripts": sorted(f.name for f in files if f.suffix == ".py"),
                    "results": any(f == ws / W.RESULTS for f in files),
                    "figures": sorted(f.name for f in files if f.suffix == ".png")}
    return {"job": dict(job) if job is not None else None, "progress": progress, "recent": W.jobs(conn, plan_id, 5)}


def _ctx(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    from ...replication import datasets
    plan = _plan(conn, plan_id)
    sets = [datasets.get(conn, d) for d in plan["plan"].get("datasets") or []]
    author = W.author_code(conn, plan_id)             # read once: the card and the estimate both use it
    low, high = W.estimate(conn, plan_id, author)
    targets = W.targets(conn, plan_id)
    runs = W.runs(conn, plan_id)
    latest = W.latest_valid(conn, plan_id)
    try:
        from ...replication.compare import LABELS, VERDICTS
    except ImportError:                       # card 0160 not merged: no Compare button
        LABELS, VERDICTS = {}, ()
    try:
        folder = export.preview(conn, plan_id)
    except Exception:  # noqa: BLE001 - the card is a convenience: the page still opens without it
        log.exception("export card for %s", plan_id)
        folder = {"ticked": 0, "groups": [], "project": "", "issue_key": "", "note": "", "ok": False}
    return {"plan": plan, "datasets": [d for d in sets if d], "targets": targets, "export_folder": folder,
            "reviewed": W.targets_reviewed(conn, plan_id), "estimate": (low, high), "author": author, "script_cap_kb": W.SCRIPT_CAP // 1024, "runs": runs,
            "latest_valid": latest["id"] if latest else None, "not_comparable": {r["id"]: W.not_comparable(r) for r in runs},
            "printed": {t["tid"]: {"value": t.get("value") or "", "se": t.get("se") or ""} for t in targets},
            "verdicts": VERDICTS, "verdict_labels": LABELS, "can_compare": bool(VERDICTS),
            "max_turns": W.MAX_TURNS, "max_rounds": rrun.MAX_ROUNDS, "r": rrun.overview(conn, plan_id, author), **_status_ctx(conn, plan_id)}


@router.get("/replicate/{plan_id}")
def replicate_page(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    ctx = _ctx(conn, plan_id)
    q = request.query_params
    return render(request, conn, "pages/data_workspace.html", title="复现工作区", wide=True, msg=q.get("msg", ""),
                  err=q.get("err", ""), **ctx)


@router.get("/replicate/{plan_id}/status")
def replicate_status(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """The live block, polled while a job runs; once it ends the whole page reloads to show the new run."""
    _plan(conn, plan_id)
    ctx = _status_ctx(conn, plan_id)
    if ctx["job"] is None and request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return render(request, conn, "partials/_replicate_status.html", plan={"id": plan_id}, max_turns=W.MAX_TURNS, **ctx)


@router.post("/replicate/{plan_id}/start")
def replicate_start(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _plan(conn, plan_id)
    try:
        W.start(conn, plan_id, mode="ai")
    except W.WorkspaceError as e:
        return _go(request, plan_id, err=str(e))
    return _go(request, plan_id, msg="已开始。可以随时点 Stop。")


@router.get("/replicate/{plan_id}/r/status")
def replicate_r_status(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """The R card's live block (card 0186), polled while an R step runs; once it ends the whole page reloads."""
    _plan(conn, plan_id)
    r = rrun.live(plan_id)
    if not r["running"] and request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Refresh": "true"})
    return render(request, conn, "partials/_replicate_r_status.html", plan={"id": plan_id}, r=r)


@router.post("/replicate/{plan_id}/r/install")
def replicate_r_install(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Leo's click on Install R packages (card 0186): the one networked step. Fetches missing helper scripts as text,
    installs the packages the scripts ask for from CRAN into the plan's own R library."""
    _plan(conn, plan_id)
    try:
        rrun.spawn(conn, plan_id, "install")
    except W.WorkspaceError as e:
        return _go(request, plan_id, err=str(e))
    return _go(request, plan_id, msg="开始装 R 包了（要联网，可能要几十分钟）。可以随时点 Stop。")


@router.post("/replicate/{plan_id}/r/run")
def replicate_r_run(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Leo's click on Run author code (R) (card 0186): the authors' R scripts, as they are, in the sandbox."""
    _plan(conn, plan_id)
    try:
        rrun.spawn(conn, plan_id, "run")
    except W.WorkspaceError as e:
        return _go(request, plan_id, err=str(e))
    return _go(request, plan_id, msg="开始运行作者的 R 代码。没有报错就不会调用 AI。")


@router.post("/replicate/{plan_id}/r/stop")
def replicate_r_stop(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _plan(conn, plan_id)
    return _go(request, plan_id, msg="已停止 R 这一步。" if rrun.stop(plan_id) else "")


@router.post("/replicate/{plan_id}/export-folder")
async def replicate_export_folder(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Leo's click on Export folder (card 0191): the plan's ticked 复现清单 items as a data folder, zipped into the paper's 课题
    drafts/ and attached to the issue that names the paper. Off the event loop (it copies files and reads author text, and
    stops itself past export.EXPORT_SECONDS); a GET never reaches it, the app's mutation guard sees to cross-site posts."""
    _plan(conn, plan_id)
    try:
        done = await run_in_threadpool(export.export_folder, conn, plan_id)
    except export.ExportError as e:
        return _go(request, plan_id, err=str(e))
    except Exception:  # noqa: BLE001 - the page says so instead of a silent 500
        log.exception("export folder for %s failed", plan_id)
        return _go(request, plan_id, err="导出时出错了，什么都没有保存，稍后再试。")
    return _go(request, plan_id, msg=done.message)


@router.post("/replicate/{plan_id}/stop")
def replicate_stop(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _plan(conn, plan_id)
    return _go(request, plan_id, msg="已停止，已经写出的代码和结果留在这次运行里。" if W.stop(conn, plan_id) else "",
               err="" if W.current_job(conn, plan_id) is None else "没能停止，请刷新再试。")


@router.post("/replicate/{plan_id}/runs/{run_id}/rerun")
def replicate_rerun(plan_id: str, run_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _plan(conn, plan_id)
    try:
        W.start(conn, plan_id, mode="rerun", source_run=run_id)
    except W.WorkspaceError as e:
        return _go(request, plan_id, err=str(e))
    return _go(request, plan_id, msg=f"正在重跑 {run_id} 的代码，不调用 AI。")


@router.post("/replicate/{plan_id}/runs/{run_id}/compare")
def replicate_compare(plan_id: str, run_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Step 4 (card 0166): the run's tables against the paper's reported values, kept with the run."""
    _plan(conn, plan_id)
    try:
        W.compare_run(conn, plan_id, run_id)
    except (W.WorkspaceError, ImportError) as e:
        return _go(request, plan_id, err=str(e) or "对照还没有上线。")
    return _go(request, plan_id, msg=f"已对照 {run_id}，结果在这次运行下面。")


@router.post("/replicate/{plan_id}/runs/{run_id}/report")
def replicate_report(plan_id: str, run_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Step 5 (card 0166): one draft 复现报告 per click, then to the report; never over an existing one."""
    _plan(conn, plan_id)
    try:
        ref = W.report_run(conn, plan_id, run_id)
    except W.WorkspaceError as e:
        return _go(request, plan_id, err=str(e))
    url = f"/artifacts/{ref}"
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


@router.get("/replicate/{plan_id}/runs/{run_id}/files/{rel:path}")
def replicate_file(plan_id: str, run_id: str, rel: str, conn: sqlite3.Connection = Depends(get_conn)):
    """A frozen file of a run: a figure, a table, a script or a log. Served as data, never as a page."""
    _plan(conn, plan_id)
    run = W.get_run(conn, run_id)
    if run is None or run["plan_id"] != plan_id:
        raise HTTPException(404, "没有这次运行")
    try:
        path = W.run_file(plan_id, run_id, rel)
    except W.WorkspaceError:
        raise HTTPException(404, "没有这个文件") from None
    media = FILE_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media, headers={"X-Content-Type-Options": "nosniff",
                                                         "Content-Security-Policy": "default-src 'none'"})
