"""Excerpts: list with the review inbox, detail with the association panel, capture, re-associate."""
from __future__ import annotations

import html
import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import excerpts as ex, i18n
from ...ai import queue
from ...ai.jobs import excerpt_associate as assoc_job
from ...files.weeks import week_of
from ..context import get_conn, render

router = APIRouter()

FILTERS = ("inbox", "suggestions", "linked", "used", "discarded", "all")
ARTIFACT_SECTIONS = ("Evidence", "Why it matters", "What this challenges", "Next question")


def is_hx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def view(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["uses"] = json.loads(d.get("uses_json") or "[]")
    d["cite"] = ex.cite_zh(d.get("paper_authors"), d.get("paper_year"), d.get("paper_title"))
    d["loc"] = ex.locator_label(d.get("locator"))
    d["quote_short"] = ex.one_line(d.get("quote") or "", 320)
    return d


def panel_context(conn: sqlite3.Connection, excerpt_id: str) -> dict[str, Any]:
    return {"p": assoc_job.panel(conn, excerpt_id), "reject_reasons": ex.REJECT_REASONS, "relations": ex.RELATIONS}


def error_fragment(message: str) -> HTMLResponse:
    return HTMLResponse(f'<p class="form-error" role="alert">{html.escape(i18n.error_zh(message))}</p>',
                        headers={"HX-Retarget": "#excerpt-form-errors", "HX-Reswap": "innerHTML"})


def _review_groups(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    ids = [r[0] for r in conn.execute(
        "SELECT src_id, MAX(id) m FROM associations WHERE src_type = 'excerpt' AND status = 'proposed' GROUP BY src_id "
        "ORDER BY m DESC LIMIT ?", (limit,))]
    for r in conn.execute("SELECT source_excerpts_json FROM claims WHERE origin = 'ai-suggested' AND status = 'proposed' "
                          "ORDER BY id DESC LIMIT ?", (limit,)):
        for x in json.loads(r[0] or "[]"):
            if x not in ids:
                ids.append(x)
    groups = []
    for i in ids[:limit]:
        row = ex.get(conn, i)
        if row is not None and row["status"] != "discarded":
            groups.append({"e": view(row), **panel_context(conn, i)})
    return groups


def list_context(conn: sqlite3.Connection, filter: str, paper: str | None, project: str | None, use: str | None) -> dict[str, Any]:
    status = filter if filter in ("inbox", "linked", "used", "discarded") else None
    rows = [] if filter == "suggestions" else ex.list_excerpts(conn, status=status, paper=paper or None,
                                                               project=project or None, use=use or None)
    counts = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM excerpts GROUP BY status")}
    counts["suggestions"] = conn.execute("SELECT COUNT(DISTINCT src_id) FROM associations WHERE src_type='excerpt' "
                                         "AND status='proposed'").fetchone()[0]
    counts["all"] = sum(v for k, v in counts.items() if k in ("inbox", "linked", "used"))
    # Only papers that have highlights can narrow this list; keep the current filter selectable.
    papers = conn.execute("SELECT id, title, year FROM papers WHERE id = ? OR id IN (SELECT paper_id FROM excerpts) "
                          "ORDER BY title COLLATE NOCASE", (paper or "",)).fetchall()
    return {
        "filter": filter, "paper_filter": paper or "", "project_filter": project or "", "use_filter": use or "",
        "rows": [view(r) for r in rows], "counts": counts, "filters": FILTERS, "papers": papers, "uses_vocab": ex.USES,
        "review": _review_groups(conn) if filter in ("inbox", "suggestions", "all") and not (paper or project or use) else [],
        "paper": None,
    }


@router.get("/excerpts")
def excerpts_page(request: Request, filter: str = "inbox", paper: str | None = None, project: str | None = None,
                  use: str | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    if filter not in FILTERS:
        filter = "inbox"
    return render(request, conn, "pages/excerpts.html", title="划线", wide=True,
                  **list_context(conn, filter, paper, project, use))


def excerpt_context(conn: sqlite3.Connection, excerpt_id: str) -> dict[str, Any]:
    row = ex.get(conn, excerpt_id)
    if row is None:
        raise HTTPException(404, f"没有片段 {excerpt_id}")
    history = []
    for r in conn.execute("SELECT * FROM associations WHERE src_type='excerpt' AND src_id=? AND status != 'proposed' "
                          "ORDER BY id DESC", (excerpt_id,)):
        history.append({"assoc": r, **assoc_job.describe_target(conn, r["dst_type"], r["dst_id"])})
    claims = conn.execute("SELECT * FROM claims WHERE source_excerpts_json LIKE ? AND status != 'proposed' ORDER BY id DESC",
                          (f'%"{excerpt_id}"%',)).fetchall()
    week_art = ex.current_week_artifact(conn)
    return {
        "e": view(row), "tags": ex.tags(conn, excerpt_id), "warnings": ex.check(conn, excerpt_id),
        "history": history, "claims_history": claims, "week_artifact": week_art, "week": week_of(),
        "suggested_type": None if week_art else ex.suggested_artifact_type(conn, excerpt_id),
        "sections": ARTIFACT_SECTIONS, "vocab": vocab(conn, row["project_slug"]),
        **panel_context(conn, excerpt_id),
    }


def vocab(conn: sqlite3.Connection, project: str | None) -> dict[str, Any]:
    camps = list(ex.CAMPS) + [r[0] for r in conn.execute("SELECT id FROM camps WHERE project_slug = ? ORDER BY sort, id", (project or "",))
                              if r[0] not in ex.CAMPS]
    cases = list(ex.CASES) + [r[0] for r in conn.execute("SELECT id FROM cases ORDER BY sort, id") if r[0] not in ex.CASES]
    concepts = conn.execute("SELECT id, label FROM concepts ORDER BY label").fetchall()
    claims = conn.execute("SELECT id, code, statement FROM claims WHERE project_slug = ? AND kind = 'explanation' "
                          "AND status = 'accepted' ORDER BY code, id", (project or "",)).fetchall()
    return {"kinds": ex.KINDS, "uses": ex.USES, "camps": camps, "cases": cases, "concepts": concepts, "claims": claims,
            "strengths": ex.STRENGTHS, "source_types": ex.SOURCE_TYPES, "stances": ex.STANCES}


@router.get("/excerpts/{excerpt_id}")
def excerpt_page(excerpt_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    ctx = excerpt_context(conn, excerpt_id)
    return render(request, conn, "pages/excerpt.html", title="划线 · " + ctx["e"]["quote_short"][:24], **ctx)


async def form_fields(request: Request) -> dict[str, Any]:
    form = await request.form()

    def one(name: str) -> str | None:
        v = form.get(name)
        return str(v).strip() if v not in (None, "") else None

    def as_int(name: str) -> int | None:
        v = one(name)
        return int(v) if v is not None and v.lstrip("-").isdigit() else None

    explanations = []
    for key in form.keys():
        if key.startswith("explanation_") and form.get(key):
            explanations.append({"claim_id": key.removeprefix("explanation_"), "stance": str(form.get(key))})
    return {
        "paper_id": one("paper_id"), "quote": str(form.get("quote") or ""), "my_note": str(form.get("my_note") or ""),
        "locator": one("locator") or "", "kind": one("kind") or "quote", "uses": [str(u) for u in form.getlist("uses")],
        "camp": one("camp"), "cases": one("cases"), "concepts": one("concepts"), "explanations": explanations,
        "strength": one("strength"), "source_type": one("source_type") or "secondary-scholarly", "project": one("project"),
        "file_id": as_int("file_id"), "page_index": as_int("page_index"), "private": bool(form.get("private")),
    }


@router.post("/excerpts")
async def create_excerpt(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    fields = await form_fields(request)
    try:
        if not fields["paper_id"]:
            raise ex.ExcerptError("请选择这条划线出自哪篇文献")
        excerpt_id = ex.create(conn, **fields)
    except ex.ExcerptError as e:
        if is_hx(request):
            return error_fragment(str(e))
        ctx = list_context(conn, "inbox", None, None, None)
        return render(request, conn, "pages/excerpts.html", title="划线", wide=True, form_error=i18n.error_zh(str(e)), **ctx)
    ex.enqueue_associate(conn, excerpt_id)
    if not is_hx(request):
        return RedirectResponse(f"/excerpts/{excerpt_id}", status_code=303)
    row = ex.get(conn, excerpt_id)
    resp = render(request, conn, "partials/_excerpt_row.html", e=view(row), with_panel=True, **panel_context(conn, excerpt_id))
    resp.headers["HX-Trigger"] = json.dumps({"excerpt-saved": {"id": excerpt_id, "verified": bool(row["quote_verified"])}})
    return resp


@router.post("/excerpts/{excerpt_id}/reassociate")
def reassociate(excerpt_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if ex.get(conn, excerpt_id) is None:
        raise HTTPException(404, f"没有片段 {excerpt_id}")
    ex.enqueue_associate(conn, excerpt_id)
    if not is_hx(request):
        return RedirectResponse(f"/excerpts/{excerpt_id}", status_code=303)
    return render(request, conn, "partials/_association_panel.html", **panel_context(conn, excerpt_id))


@router.post("/excerpts/{excerpt_id}/edit")
async def edit_excerpt(excerpt_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    fields = await form_fields(request)
    changes = {k: fields[k] for k in ("quote", "my_note", "locator", "kind", "uses", "camp", "cases", "concepts",
                                      "explanations", "strength", "source_type", "private")}
    changes["cases"] = changes["cases"] or ""
    changes["concepts"] = changes["concepts"] or ""
    try:
        ex.update(conn, excerpt_id, **changes)
    except ex.ExcerptError as e:
        if is_hx(request):
            return error_fragment(str(e))
        raise HTTPException(400, str(e))
    if is_hx(request):
        return HTMLResponse("", headers={"HX-Redirect": f"/excerpts/{excerpt_id}"})
    return RedirectResponse(f"/excerpts/{excerpt_id}", status_code=303)


@router.post("/excerpts/{excerpt_id}/discard")
def discard_excerpt(excerpt_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        ex.discard(conn, excerpt_id)
    except ex.ExcerptError as e:
        raise HTTPException(404, str(e))
    if is_hx(request):
        return HTMLResponse("", headers={"HX-Redirect": "/excerpts"})
    return RedirectResponse("/excerpts", status_code=303)
