"""Journal radar (雷达): the digest's papers tab and the news (时评) tab with the news.arrange job."""
from __future__ import annotations

import sqlite3
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from markupsafe import escape

from ... import digest
from ...ai import dispatch, queue
from ...files import datafiles
from ...intake import directions
from ..context import get_conn, render
from .papers import subnav

router = APIRouter()

NEWS_KIND = "news.arrange"
TABS = {"papers": "论文", "news": "时评"}


def _news_job(conn: sqlite3.Connection, date_: str) -> sqlite3.Row | None:
    return queue.latest_for(conn, NEWS_KIND, date_)


def news_context(conn: sqlite3.Connection, date_: str | None) -> dict:
    news = digest.news_for(conn, date_ or None)
    ctx: dict = {"news": news, "news_dates": digest.news_dates(conn)[-10:][::-1], "arrangement": None, "news_job": None,
                 "news_polling": False, "clustered": [], "unclustered": []}
    if news is None:
        return ctx
    arr = digest.arrangement(conn, news.date)
    job = _news_job(conn, news.date)
    by_id = news.by_id()
    dir_rows = directions.load()
    order = {str(d["slug"]): i for i, d in enumerate(dir_rows)}
    names = {str(d["slug"]): str(d.get("name") or d["slug"]) for d in dir_rows}
    groups: dict[str | None, list[dict]] = {}
    used: set[str] = set()
    links: dict[str, list[dict]] = {}
    if arr:
        for link in arr.get("project_links") or []:
            links.setdefault(link.get("item_id"), []).append(link)
        for c in arr.get("clusters") or []:
            items = [by_id[i] for i in c.get("item_ids") or [] if i in by_id]
            if not items:
                continue
            used.update(i.id for i in items)
            groups.setdefault(c.get("direction"), []).append({**c, "items": items})
    clustered = [{"slug": slug, "name": names.get(slug or "", "未归入方向" if slug is None else slug), "clusters": cs}
                 for slug, cs in sorted(groups.items(), key=lambda kv: order.get(kv[0] or "", 99))]
    ctx.update(arrangement=arr, news_job=job, clustered=clustered, project_links=links,
               unclustered=[i for i in news.items if i.id not in used] if arr else [],
               news_polling=bool(job and (job["status"] == "claimed" or (job["status"] == "queued" and queue.online(conn)))))
    return ctx


@router.get("/digest")
def digest_page(request: Request, tab: str = "papers", date: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    tab = tab if tab in TABS else "papers"
    d = digest.latest(conn)
    saved = {i.index: digest.saved_paper_for(conn, i) for i in (d.items if d else [])}
    st = digest.status(d)
    ctx = {"digest": d, "status": st, "saved": saved, "digest_status": st, "digest_saved": saved, "tab": tab, "tabs": TABS,
           "direction_names": {str(x["slug"]): str(x.get("name") or x["slug"]) for x in directions.load()}}
    if tab == "news":
        ctx.update(news_context(conn, date))
    return render(request, conn, "pages/digest.html", title="旧简报存档", **ctx, **subnav("/digest"))


@router.post("/digest/save/{index}", response_class=HTMLResponse)
def digest_save(index: int, request: Request, project: str | None = Form(None), direction: str | None = Form(None),
                conn: sqlite3.Connection = Depends(get_conn)):
    d = digest.latest(conn)
    if d is None or not 0 <= index < len(d.items):
        raise HTTPException(404, "没有这条简报条目")
    project = project or str((datafiles.read("now") or {}).get("project") or "")
    paper_id = digest.save_item(conn, d.items[index], project or None, http=getattr(request.app.state, "intake_http", None),
                                direction=direction)
    return HTMLResponse(f'<a class="chip status-active" href="/papers/{escape(paper_id)}">已保存 → {escape(paper_id)}</a>')


@router.post("/digest/news/arrange")
async def news_arrange(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    date_ = str(form.get("date") or "")
    news = digest.news_for(conn, date_ or None)
    if news is None or not news.items:
        raise HTTPException(404, "没有这一期时评")
    for row in conn.execute("SELECT id FROM ai_jobs WHERE kind = ? AND target_id = ? AND status = 'queued'",
                            (NEWS_KIND, news.date)).fetchall():
        queue.cancel(conn, row["id"])
    job_id = queue.enqueue(conn, NEWS_KIND, news.date)
    if job_id:
        try:
            dispatch.dispatch(job_id)
        except Exception:  # noqa: BLE001 - stays queued for `rl agent`
            pass
    url = "/digest?" + urlencode({"tab": "news", "date": news.date})
    if request.headers.get("hx-request") == "true" and request.headers.get("hx-current-url"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)
