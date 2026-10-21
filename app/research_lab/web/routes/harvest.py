from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import weekly
from ...config import get_paths
from ...files import artifacts, datafiles, harvests
from ...files.weeks import weeks_between
from ...files.yamlio import StaleFileError
from ..context import get_conn, render

router = APIRouter()

KIND_ZH = {"monthly": "月度", "quarterly": "季度", "annual": "年度"}


@router.get("/harvest")
def harvest_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    paths = get_paths()
    artifacts.index(conn, paths)
    items = []
    for h in harvests.list_all(paths):
        n = len(harvests.covered_artifacts(conn, h.from_week, h.to_week)) if h.from_week and h.to_week else 0
        items.append({"h": h, "n": n})
    monthly = harvests.period_for("monthly")
    return render(request, conn, "pages/harvests.html", title="收获", items=items, due=harvests.due(conn, paths),
                  suggestions={"monthly": monthly, "quarterly": harvests.period_for("quarterly"),
                               "annual": harvests.period_for("annual"),
                               "previous_monthly": harvests.previous_period(monthly)},
                  error=request.query_params.get("error"))


@router.get("/harvest/new")
def harvest_new(request: Request, kind: str = "monthly", period: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    paths = get_paths()
    kind = kind if kind in harvests.KINDS else "monthly"
    period = period or harvests.period_for(kind)
    error = None
    try:
        if harvests.kind_of(period) != kind:
            error = f"{period} 不是{KIND_ZH.get(kind, kind)}周期。"
    except ValueError as e:
        error = str(e)
    if error:
        return render(request, conn, "pages/harvests.html", title="新建收获", items=[], due=harvests.due(conn, paths),
                      suggestions={"monthly": harvests.period_for("monthly"), "quarterly": harvests.period_for("quarterly"),
                                   "annual": harvests.period_for("annual"), "previous_monthly": ""}, error=error)
    if harvests.get(period, paths):
        return RedirectResponse(f"/harvest/{period}", status_code=303)
    start, end = harvests.period_range(period)
    artifacts.index(conn, paths)
    covered = harvests.covered_artifacts(conn, start, end)
    return render(request, conn, "pages/harvest.html", title=f"新建收获 · {period}", harvest_title=harvests.period_title(period), new=True, kind=kind,
                  period=period, from_week=start, to_week=end, covered=covered,
                  axis=_axis(start, end, covered), h=None)


@router.post("/harvest")
async def harvest_create(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    kind, period = str(form.get("kind", "monthly")), str(form.get("period", "")).strip()
    try:
        harvests.create(kind, period)
    except FileExistsError:
        pass
    except ValueError as e:
        return RedirectResponse(f"/harvest?error={_q(str(e))}", status_code=303)
    return RedirectResponse(f"/harvest/{period}", status_code=303)


def _axis(start: str, end: str, covered: list[sqlite3.Row]) -> list[dict]:
    by_week: dict[str, list] = {}
    for r in covered:
        by_week.setdefault(r["week"], []).append(r)
    return [{"week": w, "arts": by_week.get(w, [])} for w in weeks_between(start, end)]


def _review(conn: sqlite3.Connection, period: str):
    from ...ai import queue
    job = queue.latest_for(conn, "harvest.review", period)
    if job is None or job["status"] == "cancelled":
        return None
    return weekly.draft_view(conn, job["id"])


def _detail_context(conn: sqlite3.Connection, period: str, **extra):
    paths = get_paths()
    h = harvests.get(period, paths)
    if h is None:
        return None
    artifacts.index(conn, paths)
    covered = harvests.covered_artifacts(conn, h.from_week, h.to_week)
    refs = {r["ref"] for r in covered}
    relations = []
    for r in covered:
        for dst in json.loads(r["related_json"] or "[]"):
            relations.append({"a": r["ref"], "b": dst, "inside": dst in refs})
    counting = [r for r in covered if r["counts"]]
    projects = datafiles.read("projects", paths) or []
    by_project: dict[str, list] = {}
    for r in covered:
        by_project.setdefault(r["project"], []).append(r)
    pipeline = [{"slug": str(p["slug"]), "name": p.get("name", p["slug"]), "stage": p.get("stage", ""),
                 "artifacts": by_project.get(str(p["slug"]), [])} for p in projects]
    return {
        "title": h.title, "h": h, "new": False, "kind": h.kind, "period": h.name, "from_week": h.from_week,
        "to_week": h.to_week, "covered": covered, "counting": counting, "axis": _axis(h.from_week, h.to_week, covered),
        "relations": relations, "pipeline": pipeline, "review": _review(conn, h.name), "text": h.text(), "sha": h.sha,
        **extra,
    }


@router.get("/harvest/review/{job_id}")
def harvest_review_status(job_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    view = weekly.draft_view(conn, job_id)
    if view is None:
        return HTMLResponse("<p class='muted'>没有这个任务。</p>", status_code=404)
    return render(request, conn, "partials/_draft_result.html", draft=view, sha="")


@router.get("/harvest/{name}")
def harvest_detail(name: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        ctx = _detail_context(conn, name)
    except ValueError:
        ctx = None
    if ctx is None:
        return RedirectResponse(f"/harvest?error={_q('还没有收获 ' + name + '。')}", status_code=303)
    ctx["saved"] = request.query_params.get("saved")
    return render(request, conn, "pages/harvest.html", **ctx)


@router.post("/harvest/{name}/save")
async def harvest_save(name: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    text, sha = str(form.get("text", "")).replace("\r\n", "\n"), str(form.get("sha", ""))
    try:
        harvests.save_text(name, text, sha)
    except StaleFileError:
        path = harvests.path_for(name)
        ctx = _detail_context(conn, name, conflict=weekly.conflict(path, text))
        ctx["text"] = text
        resp = render(request, conn, "pages/harvest.html", **ctx)
        resp.status_code = 409
        return resp
    except Exception as e:  # noqa: BLE001 - YAML parse errors vary
        ctx = _detail_context(conn, name, error=f"未保存：{e}")
        if ctx is None:
            return RedirectResponse("/harvest", status_code=303)
        ctx["text"], ctx["sha"] = text, sha
        resp = render(request, conn, "pages/harvest.html", **ctx)
        resp.status_code = 400
        return resp
    return RedirectResponse(f"/harvest/{name}?saved=1", status_code=303)


@router.post("/harvest/{name}/review")
def harvest_review(name: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    from ...ai import queue
    try:
        harvests.kind_of(name)
    except ValueError as e:
        return HTMLResponse(f"<p class='warn'>▲ {e}</p>", status_code=400)
    job_id = queue.enqueue(conn, "harvest.review", name)
    if job_id is None:
        job = queue.latest_for(conn, "harvest.review", name)
        job_id = job["id"]
    else:
        from ...ai import dispatch as ai_dispatch
        ai_dispatch.dispatch(job_id)
    return render(request, conn, "partials/_draft_result.html", draft=weekly.draft_view(conn, job_id), sha="")


def _q(s: str) -> str:
    from urllib.parse import quote
    return quote(s)
