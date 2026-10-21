"""Coach uses existing research writers and the regular PDF import path."""
from __future__ import annotations

import sqlite3
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from ... import coach
from ...db.connection import set_setting
from ...files import datafiles, issues, tasks
from ...files.yamlio import StaleFileError
from ..context import get_conn, render

router = APIRouter()


def page(request, conn, *, error="", form=None, status=200):
    guide = coach.state(conn)
    requested = request.query_params.get("step", "")
    step = int(requested) if requested in ("1", "2", "3", "4") else guide["step"]
    step = min(step, guide["step"])
    library = [dict(r) for r in conn.execute(
        "SELECT p.id,p.title FROM papers p WHERE EXISTS (SELECT 1 FROM paper_files f WHERE f.paper_id=p.id) "
        "ORDER BY p.created_at DESC LIMIT 100")]
    response = render(request, conn, "pages/guide.html", title="开始使用", guide=guide, step=step,
                      error=error, values=form or {}, library=library,
                      projects_sha=datafiles.file_sha("projects"), directions_sha=datafiles.file_sha("directions"),
                      revision=issues.revision())
    response.status_code = status
    return response


@router.get("/guide")
def guide_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return page(request, conn)


@router.post("/guide/{action}")
async def guide_action(action: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if action == "start":
        coach.start(conn)
    elif action in ("dismiss", "finish"):
        set_setting(conn, "coach.hidden", True)
        return RedirectResponse("/", status_code=303)
    elif action == "import":
        from .papers import CHUNK, extract, papers_upload
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > extract.MAX_BYTES + CHUNK:
            raise HTTPException(413, "upload too large")
        # Parse once with the Coach's one-file limit before delegating to the library.
        async with request.form(max_files=1, max_fields=20) as form:
            try:
                coach.require_project(conn, form)
            except coach.CoachError as exc:
                return page(request, conn, error=str(exc), status=400)
            response = await papers_upload(request, conn)
            dest = response.headers.get("HX-Redirect") or response.headers.get("location", "")
            # Keep standard rejection/duplicate-selection screens when import did not open a reader.
            path = urlsplit(dest).path
            if path.startswith("/papers/") and len(path.strip("/").split("/")) == 2:
                paper_id = path.split("/")[-1]
                coach.choose_paper(conn, {"project": form["project"], "paper": paper_id})
            else:
                return response
    else:
        handlers = {"project": coach.choose_project, "paper": coach.choose_paper,
                    "review": coach.review, "next": coach.next_step}
        if action not in handlers:
            raise HTTPException(404)
        form = {k: str(v) for k, v in (await request.form()).items()}
        try:
            handlers[action](conn, form)
        except StaleFileError:
            return page(request, conn, error="内容已在别处更新，请检查后再提交。", form=form, status=409)
        except (coach.CoachError, datafiles.DataFileError, issues.IssueError, tasks.TaskError) as exc:
            return page(request, conn, error=str(exc), form=form, status=400)
    return RedirectResponse("/guide", status_code=303)
