"""待补数据 (card 0188): the panel on a replication plan page (what the author's scripts read that the lab does not
have) and the upload that fills one row. The page includes partials/_plan_missing.html, which loads the panel from
GET /data/plans/<plan>/missing, so the plan page itself needs no new context."""
from __future__ import annotations

import logging
import sqlite3
import uuid
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import UploadFile

from ...config import get_paths
from ...replication import datasets, fetch, missing, plans
from ..context import get_conn, render

router = APIRouter()
log = logging.getLogger("research_lab.web.missing")
CHUNK = 1 << 20
ENVELOPE = 1 << 20                      # what a multipart body adds to the file's own bytes


def _plan_or_404(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    p = plans.get(conn, plan_id)
    if p is None:
        raise HTTPException(404, f"没有复现方案 {plan_id}")
    return p


def _panel(request: Request, conn: sqlite3.Connection, p: dict[str, Any], scope: str, **ctx: Any) -> Response:
    gaps = missing.report(conn, p["id"], missing.ALL if scope == "all" else None)
    base: dict[str, Any] = {"error": "", "uploaded": None}
    if ctx.get("uploaded"):                    # the plan's dataset list is sent again, out of band, as after an Import
        base["attached"] = [d for d in (datasets.get(conn, i) for i in p["plan"].get("datasets", [])) if d]
        base["all_datasets"] = datasets.list_all(conn)
    return render(request, conn, "partials/_plan_missing.html", p=p, gaps=gaps, scope=scope, **{**base, **ctx})


@router.get("/data/plans/{plan_id}/missing")
def plan_missing(plan_id: str, request: Request, scripts: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    return _panel(request, conn, _plan_or_404(conn, plan_id), "all" if scripts == "all" else "")


def _answer(request: Request, conn: sqlite3.Connection, p: dict[str, Any], scope: str, *, error: str = "",
            done: missing.Uploaded | None = None) -> Response:
    if request.headers.get("hx-request") == "true":
        return _panel(request, conn, p, scope, error=error, uploaded=done)
    flash = {"err": error} if error else {"msg": f"已补：{done.item.name}"} if done else {}
    return RedirectResponse(f"/data/plans/{p['id']}/detail?{urlencode(flash)}#missing", status_code=303)


@router.post("/data/plans/{plan_id}/missing/upload")
async def plan_missing_upload(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Fill one row with the uploaded file. The row is named by `path` and must be one the panel lists as missing; the
    file is received into the cache with the size cap checked as it arrives, then imported like a fetch."""
    p = _plan_or_404(conn, plan_id)
    cap = fetch.MAX_MB * fetch.MB
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap + ENVELOPE:           # refused before the body is read and spooled to disk
        return _answer(request, conn, p, "", error=f"文件超过上限 {fetch.MAX_MB} MB，请先拆分或转成 Parquet。")
    form = await request.form()
    scope = "all" if form.get("scope") == "all" else ""
    up = form.get("file")
    if not isinstance(up, UploadFile) or not up.filename:
        return _answer(request, conn, p, scope, error="先选一个要上传的文件。")
    problem = fetch.upload_problem(PurePosixPath(up.filename.replace("\\", "/")).name)
    if problem:                                                           # the extension alone says no: nothing is written
        return _answer(request, conn, p, scope, error=problem)
    incoming = get_paths().cache / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    tmp = incoming / f"{uuid.uuid4().hex}.part"
    try:
        with tmp.open("wb") as fh:
            while chunk := await up.read(CHUNK):
                fh.write(chunk)
                if fh.tell() > cap:
                    raise missing.MissingError(f"文件超过上限 {fetch.MAX_MB} MB，请先拆分或转成 Parquet。")
        done = await run_in_threadpool(missing.upload, conn, plan_id, str(form.get("path") or ""), tmp, up.filename)
    except missing.MissingError as e:
        return _answer(request, conn, p, scope, error=str(e))
    except Exception:  # noqa: BLE001 - the panel says so instead of a silent 500
        log.exception("upload for %s failed", plan_id)
        return _answer(request, conn, p, scope, error="导入时出错了，稍后再试。")
    finally:
        tmp.unlink(missing_ok=True)
    return _answer(request, conn, _plan_or_404(conn, plan_id), scope, done=done)
