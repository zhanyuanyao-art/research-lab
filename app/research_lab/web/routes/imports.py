"""The import sheet (card 0130, docs/design/LIBRARY-UI.md §3.3): one dialog for dropped or chosen PDFs and pasted
DOIs, arXiv ids or article URLs, with a queue that shows each paper's state. static/import.js sends one file or one
identifier per request and draws the rows from the JSON; possible duplicates are answered here (Same paper / Keep
both) without leaving the dialog. Where the papers go is visible and editable: 方向, 课题 and 集合 default from the
page the sheet opened on (LIBRARY-UI §八 3): a direction page gives its direction, a 课题 page its 课题, anywhere
else the current project's direction and no 课题."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from ... import paper_collections as collections
from ...files import datafiles
from ...intake import directions, extract, identifiers, pipeline
from ..context import DIRECTION_COOKIE, get_conn, render

router = APIRouter()

DUP_REASONS = {"same DOI": "DOI 相同", "similar title": "标题相近", "same file": "文件相同"}


def _http(request: Request):
    """Tests put an httpx.Client with a MockTransport on app.state.intake_http."""
    return getattr(request.app.state, "intake_http", None)


def defaults(request: Request, page: str) -> dict[str, str]:
    """方向 · 课题 · 集合 preselected for the page the sheet opened on."""
    known = {str(d["slug"]) for d in directions.load()}
    u = urlparse(page or "")
    parts = [x for x in (u.path or "").split("/") if x]
    q = {k: v[0] for k, v in parse_qs(u.query).items() if v}
    out = {"direction": "", "project": "", "collection": ""}
    if len(parts) >= 2 and parts[0] == "projects" and datafiles.project(parts[1]):
        out["project"] = parts[1]
    elif parts == ["papers"]:
        if q.get("project") and datafiles.project(q["project"]):
            out["project"] = q["project"]
        out["collection"] = q.get("collection", "")
        if "direction" in q or "direction=" in u.query:
            out["direction"] = q.get("direction", "")
        else:   # the direction bar's pick (card 0066) is what the library shows
            out["direction"] = unquote(request.cookies.get(DIRECTION_COOKIE, ""))
    if out["project"] and not out["direction"]:
        out["direction"] = pipeline.project_direction(out["project"]) or ""
    if out["direction"] not in known:
        out["direction"] = ""
    return out


@router.get("/imports/sheet")
def import_sheet(request: Request, page: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    """The dialog's body, fetched when it opens; `page` is the page it opened on."""
    projects = [(str(p["slug"]), str(p.get("name") or p["slug"])) for p in datafiles.read("projects") or []]
    return render(request, conn, "partials/_import_sheet.html", picks=defaults(request, page), project_list=projects,
                  direction_list=directions.load(), collection_list=collections.listing(conn))


def _target(conn: sqlite3.Connection, form) -> tuple[str | None, str | None, str | None]:
    """The sheet's 放到: unknown values are refused, not silently dropped, so nothing lands somewhere unseen."""
    project = str(form.get("project") or "").strip() or None
    direction = directions.clean(str(form.get("direction") or "").strip() or None)
    collection = str(form.get("collection") or "").strip() or None
    if project and not datafiles.project(project):
        raise HTTPException(400, "没有这个课题，请刷新后再导入。")
    if collection and not collections.get(conn, collection):
        raise HTTPException(400, "集合已不存在，请刷新后再导入。")
    return project, direction, collection


def _title(conn: sqlite3.Connection, paper_id: str | None) -> str | None:
    row = conn.execute("SELECT title FROM papers WHERE id = ?", (paper_id,)).fetchone() if paper_id else None
    return row[0] if row else None


def _dup(conn: sqlite3.Connection, name: str, pending: str, duplicate_of: str, reason: str | None) -> dict[str, Any]:
    return {"state": "dup", "name": name, "pending": pending, "duplicate_of": duplicate_of,
            "duplicate_title": _title(conn, duplicate_of),
            "note": f"可能和库里的《{_title(conn, duplicate_of) or duplicate_of}》是同一篇（{DUP_REASONS.get(reason or '', reason or '')}）"}


@router.post("/imports/files")
async def import_file(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """One PDF per request, so the queue shows each file's own result and a long batch never hits the form limits."""
    from .papers import _save_upload
    async with request.form(max_files=1, max_fields=8) as form:
        project, direction, collection = _target(conn, form)
        up = next((f for f in form.getlist("file") if isinstance(f, UploadFile) and f.filename), None)
        if up is None:
            raise HTTPException(400, "没有收到文件")
        name = Path(up.filename or "upload.pdf").name
        tmp: Path | None = None
        try:
            tmp = await _save_upload(up)
            r = await run_in_threadpool(pipeline.ingest, conn, tmp, project=project, direction=direction, http=_http(request),
                                        original_name=name)
        except extract.RejectedFile as e:
            return JSONResponse({"state": "error", "name": name, "note": f"没有导入：{e}"})
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
    if r.pending:
        return JSONResponse(_dup(conn, name, r.pending, r.duplicate_of, r.reason))
    paper_id = r.paper_id or r.duplicate_of
    if collection and paper_id:
        collections.assign(conn, collection, paper_id)
    if not r.created:
        return JSONResponse({"state": "exists", "name": name, "paper_id": paper_id, "title": _title(conn, paper_id),
                             "note": "这份文件已经在文献库里"})
    note = "已入库 · 轻读概要排队中" if r.job_id else "已入库"
    return JSONResponse({"state": "ok", "name": name, "paper_id": paper_id, "title": _title(conn, paper_id),
                         "note": "；".join([note, *r.notes]) if r.notes else note})


@router.post("/imports/identifiers")
async def import_identifier(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """One line of the paste box per request: a DOI, an arXiv id or a URL with a DOI."""
    form = await request.form()
    project, direction, collection = _target(conn, form)
    ident = identifiers.parse_one(str(form.get("id") or ""))
    added = await run_in_threadpool(identifiers.add, conn, ident, project=project, direction=direction, http=_http(request))
    if collection and added.paper_id and added.state in ("ok", "biblio", "exists"):
        collections.assign(conn, collection, added.paper_id)
    out = {"state": added.state, "name": ident.raw, "paper_id": added.paper_id, "title": added.title, "note": added.note}
    if added.state == "dup":
        out.update(_dup(conn, added.title or ident.raw, added.pending or "", added.duplicate_of or "", "same DOI"))
        out["note"] = added.note + f"：《{_title(conn, added.duplicate_of) or added.duplicate_of}》"
    return JSONResponse(out)


@router.post("/imports/pending/{sha12}/{answer}")
async def answer_pending(request: Request, sha12: str, answer: str, conn: sqlite3.Connection = Depends(get_conn)):
    """Same paper: the held PDF becomes a second file of the paper it looks like. Keep both: it is imported on its
    own. Either way the sheet's 集合 applies."""
    form = await request.form()
    _project, _direction, collection = _target(conn, form)
    if answer == "same":
        paper_id = str(form.get("paper_id") or "")
        if not pipeline.get_paper(conn, paper_id):
            raise HTTPException(404, "没有这篇文献")
        try:
            await run_in_threadpool(pipeline.attach_pending, conn, sha12, paper_id)
        except LookupError as e:
            raise HTTPException(404, str(e))
        note = "已附为这篇的第二个文件"
    elif answer == "keep":
        try:
            r = await run_in_threadpool(pipeline.import_pending, conn, sha12, http=_http(request))
        except LookupError as e:
            raise HTTPException(404, str(e))
        paper_id, note = r.paper_id, "已单独导入 · 轻读概要排队中"
    else:
        raise HTTPException(404, "没有这个选项")
    if collection and paper_id:
        collections.assign(conn, collection, paper_id)
    return JSONResponse({"state": "ok", "paper_id": paper_id, "title": _title(conn, paper_id), "note": note})
