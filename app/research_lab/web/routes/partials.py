"""HTMX partials under /_p: capture form, related list, suggestion/claim actions, job status."""
from __future__ import annotations

import html
import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from ... import excerpts as ex, i18n
from ...ai import queue
from ...db.connection import set_setting
from ...search.related import related_with_query
from ..context import get_conn, render
from .excerpts import panel_context, vocab

router = APIRouter(prefix="/_p")


def _note(message: str, cls: str = "done-note") -> HTMLResponse:
    return HTMLResponse(f'<span class="{cls}">{message}</span>')


THEMES = ("system", "light", "dark")


@router.post("/theme")
def set_theme(theme: str = Form(...), conn: sqlite3.Connection = Depends(get_conn)):
    """The top-bar toggle: persists the same `ui.theme` setting the 设置 page edits."""
    if theme not in THEMES:
        raise HTTPException(400, "theme must be system, light or dark")
    set_setting(conn, "ui.theme", theme)
    return HTMLResponse("", status_code=204)


@router.get("/excerpt-form")
def excerpt_form(request: Request, paper_id: str | None = None, file_id: int | None = None, page_index: int | None = None,
                 page_label: str | None = None, quote: str | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    paper = conn.execute("SELECT id, title, year, authors_json FROM papers WHERE id = ?", (paper_id,)).fetchone() if paper_id else None
    project = (ex.default_project(conn, paper["id"]) if paper else None) or ""     # no 课题 unless the paper has one (card 0202)
    papers = [] if paper else conn.execute("SELECT id, title, year FROM papers ORDER BY created_at DESC LIMIT 300").fetchall()
    locator = page_label or (str(page_index + 1) if page_index is not None else "")
    return render(request, conn, "partials/_excerpt_form.html", excerpt_form_fragment=True, paper=paper, papers=papers,
                  file_id=file_id, page_index=page_index, locator=locator, quote=quote or "", project=project,
                  vocab=vocab(conn, project))


@router.get("/related")
def related_partial(request: Request, src: str | None = None, text: str | None = None, limit: int = 12,
                    explain: int = 0, conn: sqlite3.Connection = Depends(get_conn)):
    kwargs: dict = {}
    if src and src.startswith("EXC-"):
        kwargs["excerpt_id"] = src
    elif src and src.startswith("ART-"):
        kwargs["artifact_ref"] = src
    elif text:
        kwargs["text"] = text
    else:
        raise HTTPException(400, "请传入 src=EXC-…|ART-… 或 text=")
    try:
        items, query = related_with_query(conn, limit=max(1, min(limit, 50)), explain=bool(explain), **kwargs)
    except LookupError as e:
        raise HTTPException(404, str(e))
    return render(request, conn, "partials/_related.html", items=items, query=query, explain=bool(explain), src=src or "")


def _panel_or_note(request: Request, conn: sqlite3.Connection, src_type: str, src_id: str, message: str):
    if src_type == "excerpt" and ex.get(conn, src_id) is not None:
        return render(request, conn, "partials/_association_panel.html", **panel_context(conn, src_id))
    return _note(message)


@router.post("/suggestion/{assoc_id}/accept")
def accept_suggestion(assoc_id: int, request: Request, reason: str | None = Form(None), relation: str | None = Form(None),
                      conn: sqlite3.Connection = Depends(get_conn)):
    try:
        row = ex.accept_suggestion(conn, assoc_id, reason=reason, relation=relation)
    except ex.ExcerptError as e:
        raise HTTPException(404 if "unknown" in str(e) else 400, str(e))
    return _panel_or_note(request, conn, row["src_type"], row["src_id"], "已接受")


@router.post("/suggestion/{assoc_id}/reject")
def reject_suggestion(assoc_id: int, request: Request, reason: str | None = Form(None),
                      conn: sqlite3.Connection = Depends(get_conn)):
    try:
        row = ex.reject_suggestion(conn, assoc_id, reason or None)
    except ex.ExcerptError as e:
        raise HTTPException(404 if "unknown" in str(e) else 400, str(e))
    return _panel_or_note(request, conn, row["src_type"], row["src_id"], "已拒绝")


def _claim_excerpt(conn: sqlite3.Connection, claim_id: int) -> str | None:
    import json
    row = conn.execute("SELECT source_excerpts_json FROM claims WHERE id = ?", (claim_id,)).fetchone()
    ids = json.loads(row[0] or "[]") if row else []
    return ids[0] if ids else None


@router.post("/claim/{claim_id}/accept")
def accept_claim(claim_id: int, request: Request, text: str | None = Form(None), conn: sqlite3.Connection = Depends(get_conn)):
    try:
        ex.accept_claim(conn, claim_id, text)
    except ex.ExcerptError as e:
        raise HTTPException(404 if "unknown" in str(e) else 400, str(e))
    src = _claim_excerpt(conn, claim_id)
    return _panel_or_note(request, conn, "excerpt", src or "", "已保留为主张")


@router.post("/claim/{claim_id}/reject")
def reject_claim(claim_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    ex.reject_claim(conn, claim_id)
    src = _claim_excerpt(conn, claim_id)
    return _panel_or_note(request, conn, "excerpt", src or "", "已丢弃")


@router.post("/excerpt/{excerpt_id}/next-question")
def next_question(excerpt_id: str, request: Request, text: str | None = Form(None), conn: sqlite3.Connection = Depends(get_conn)):
    if not text:
        p = panel_context(conn, excerpt_id)["p"]
        text = p.get("next_question") if p.get("done") else None
    try:
        added = ex.send_next_question(text or "")
    except ex.ExcerptError as e:
        raise HTTPException(400, str(e))
    return _note("已设为本周下一步" if added else "本周下一步里已经有这个问题")


@router.post("/excerpt/{excerpt_id}/add-to-artifact")
def add_to_artifact(excerpt_id: str, request: Request, section: str = Form("Evidence"), artifact_ref: str | None = Form(None),
                    type: str | None = Form(None), conn: sqlite3.Connection = Depends(get_conn)):
    try:
        path, ref = ex.add_to_artifact(conn, excerpt_id, section=section, artifact_ref=artifact_ref or None, type_=type or None)
    except ex.ExcerptError as e:
        return HTMLResponse(f'<span class="form-error" role="alert">{html.escape(i18n.error_zh(str(e)))}</span>')
    row = conn.execute("SELECT title FROM artifacts WHERE ref = ?", (ref,)).fetchone()
    title = row["title"] if row and row["title"] else "本周产出"
    return _note(f'已加入 <a href="/artifacts/{html.escape(ref)}">《{html.escape(title)}》</a>')


@router.get("/job/{job_id}/status")
def job_status(job_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    job = queue.get(conn, job_id)
    if job is None:
        raise HTTPException(404, "这个任务已经不存在")
    if job["kind"] == "excerpt.associate" and job["target_id"]:
        latest = queue.latest_for(conn, "excerpt.associate", job["target_id"])
        if job["status"] == "done" or (latest and latest["id"] != job_id):
            return render(request, conn, "partials/_association_panel.html", **panel_context(conn, job["target_id"]))
        return render(request, conn, "partials/_job_status.html", job=job, excerpt_id=job["target_id"])
    return render(request, conn, "partials/_job_status.html", job=job, excerpt_id=None)
