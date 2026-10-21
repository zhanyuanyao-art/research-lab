"""推送: the full paper and news streams (/feed), item actions (收藏 / 忽略 / 一键导入) and the refresh
button. Items are addressed by content key; every action answers with the re-rendered row."""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import date, datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ...db.connection import get_setting
from ...feeds import brief as brief_mod, digest, importer, scheduler, sources, store
from ..context import base_context, chosen_direction, get_conn, render, templates

router = APIRouter()

TABS = {"news": "新闻", "papers": "论文", "saved": "收藏"}   # news first (Leo, 10-02)
UNITS = {"papers": "篇", "news": "条"}
# 今日精选 is the default (card 0070); 全部条目 is the full stream with its filters.
PICKS = {"today": "今日精选", "all": "全部条目"}
REVIEWS_SHOWN = 90   # about two months of Leo's journals' book reviews (card 0154)
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _filters(view: str, direction: str, source: str, q: str, lane: str = "", region: str = "", pick: str = "") -> dict:
    view = view if view in store.VIEWS else "unread"
    f = {"view": view, "direction": direction.strip() or None, "source": source.strip() or None, "q": q.strip(),
         "lane": lane if lane in ("world", "research") else None, "region": region.strip() or None}
    if pick not in ("today", "all"):   # a link with filters (lane=world from 首页) means browsing, which is 全部条目
        # not the direction: the direction bar's pick is remembered across pages and narrows 今日精选 too
        browsing = view != "unread" or any(f[k] for k in ("source", "q", "lane", "region"))
        pick = "all" if browsing else "today"
    return {**f, "pick": pick}


def _more_qs(tab: str, f: dict) -> str:
    return urlencode({"tab": tab, **{k: v for k, v in f.items() if v}})


def _day_header(day: str) -> str:
    return f'<li class="stream-day" role="heading" aria-level="3">{int(day[5:7])}月{int(day[8:10])}日</li>' if len(day) >= 10 else ""


def _row(request: Request, conn: sqlite3.Connection, item: dict, *, compact: bool, note: str | None = None,
         expanded: bool = False, event_day: str = "", event_index: str = "", layout: str = "stream") -> HTMLResponse:
    ctx = {**base_context(request, conn), "it": item, "compact": compact, "note": note, "expanded": expanded,
           "layout": layout if layout in ("story", "stream") else "stream"}
    # Re-render the same event after an action, not the report's different title.
    b = brief_mod.get(conn, event_day) if _valid_day(event_day) else None
    if (item["stream"] == "news" and item.get("lane") == "world" and b and event_index.isascii()
            and event_index.isdigit() and len(event_index) < 4):
        index = int(event_index)
        if index < len(b["events"]):
            ev = b["events"][index]
            if any(src.get("id") == item["id"] for src in ev.get("sources", [])):
                ctx.update(event={"event": ev, "item": item, "index": index}, event_day=event_day,
                           short_source=store.short_source)
                return templates().TemplateResponse(request, "partials/_feed_event.html", ctx)
    return templates().TemplateResponse(request, "partials/_feed_item.html", ctx)


async def _flag(request: Request, name: str) -> bool:
    form = await request.form()
    return str(form.get(name) or request.query_params.get(name) or "") == "1"


async def _shape(request: Request) -> dict:
    """How the row an action answers with is drawn: a grid cell, or a line of 全部条目 (layout=stream, card 0106)."""
    form = await request.form()
    return {"compact": await _flag(request, "compact"), "expanded": await _flag(request, "expanded"),
            "event_day": str(form.get("event_day") or ""), "event_index": str(form.get("event_index") or ""),
            # without a layout the answer is the plain line, as a cell always says layout=story
            "layout": str(form.get("layout") or request.query_params.get("layout") or "stream")}


def _valid_day(value: str) -> bool:
    if not DAY_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _edition(conn: sqlite3.Connection, requested: str, today: dict) -> dict:
    days = set(digest.days(conn))
    for (key,) in conn.execute("SELECT key FROM settings WHERE key LIKE 'news.brief:%'"):
        day = key.split(":", 1)[1]
        if _valid_day(day) and brief_mod.get(conn, day):
            days.add(day)
    if today["news"] or today["papers"]:
        days.add(today["date"])
    days = sorted(d for d in days if _valid_day(d))
    day = requested if _valid_day(requested) else (days[-1] if days else today["date"])
    issue = today if day == today["date"] else digest.items(conn, day)
    earlier, later = [d for d in days if d < day], [d for d in days if d > day]
    b = brief_mod.get(conn, day)
    return {"digest": issue, "brief": {**b, "date": day} if b else None, "day": day,
            "day_prev": earlier[-1] if earlier else None, "day_next": later[0] if later else None,
            "weekday": "星期" + "一二三四五六日"[date.fromisoformat(day).weekday()]}


def _event_rows(conn: sqlite3.Connection, b: dict, world: list[dict]) -> list[dict]:
    # Keep the chosen report stable after saving/dismissing it, including archived editions. Only world-lane
    # reports carry an event's actions, as in digest._world: the brief also takes important research-lane
    # reports, and those already have their own row under 研究相关.
    picked = list((get_setting(conn, digest.KEY + b["date"], {}) or {}).get("news", []))
    picked += [it["id"] for it in world if it["id"] not in picked]
    weights = {f.name: f.weight for f in sources.load().news}
    boost = digest.knobs(conn).boost
    rows, used = [], set()
    for index, ev in enumerate(b["events"]):
        reports = [it for src in ev.get("sources", []) if src.get("id")
                   and (it := store.get(conn, "news", src["id"])) and it.get("lane") == "world"]
        available = [it for it in reports if it["id"] not in used]
        chosen = next((it for item_id in picked for it in available if it["id"] == item_id), None)
        if chosen is None and reports:
            chosen = max(available or reports, key=lambda it: (weights.get(it["source"], digest.NEWS_WEIGHT) if boost else 0,
                                                  it.get("published") or it["first_seen"], it["first_seen"]))
        if chosen:
            used.add(chosen["id"])
        rows.append({"event": ev, "item": chosen, "index": index})
    return rows


@router.get("/feed")
def feed_page(request: Request, tab: str = "news", view: str = "unread", source: str = "",
              q: str = "", lane: str = "", region: str = "", pick: str = "", brief: str = "", day: str = "",
              kind: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    tab = tab if tab in TABS else "news"
    # 论文 has two kinds (card 0154): Articles (今日精选 and 全部条目) and Book reviews, a list of their own
    kind = "review" if tab == "papers" and kind == "review" else "article"
    direction = chosen_direction(request)   # the direction bar, remembered across pages
    if tab == "news":
        scheduler.refresh_news_on_view(conn, getattr(request.app.state, "feeds_http", None))
    srcs = sources.load()
    counts = store.counts(conn)
    issue = digest.items(conn)   # today's, for the tab counts: what is left to read in it
    unread = lambda items: sum(1 for it in items if it["status"] == "new")  # noqa: E731
    ctx: dict = {"tab": tab, "tabs": TABS, "views": store.VIEWS, "st": store.status(conn),
                 "tab_counts": {"papers": unread(issue["papers"]), "news": unread(issue["news"]),
                                "saved": counts["papers"]["saved"] + counts["news"]["saved"]},
                 "journal_count": len(srcs.journals), "news_count": len(srcs.news), "compact": False,
                 "short_source": store.short_source, "unread_count": counts.get(tab, {}).get("new", 0),
                 "kind": kind, "kinds": store.KINDS,
                 "kind_counts": {"article": counts["papers"]["new"], "review": counts["reviews"]["new"]}}
    if tab == "saved":
        sections = []
        for stream, label, empty in (("papers", "论文", "还没有收藏论文"), ("news", "新闻", "还没有收藏新闻")):
            items, total = store.list_items(conn, stream, view="saved", direction=direction or None, limit=200)
            sections.append({"stream": stream, "label": label, "items": items, "total": total, "unit": UNITS[stream],
                             "empty": empty})
        reviews, n_reviews = store.list_items(conn, "papers", view="saved", kind="review", direction=direction or None, limit=200)
        if reviews:   # saved book reviews get their own block, and none at all until there is one
            sections.insert(1, {"stream": "papers", "kind": "review", "label": "书评", "items": reviews, "total": n_reviews,
                                "unit": "篇", "empty": ""})
        ctx["saved_sections"] = sections
        ctx["tab_counts"]["saved"] += counts["reviews"]["saved"]
    elif kind == "review":
        # newest first, saved ones too; dismissed ones leave. No 今日精选, no AI summary: a review is read for its book.
        items, total = store.list_items(conn, "papers", view="all", kind="review", direction=direction or None, limit=REVIEWS_SHOWN)
        ctx.update(reviews=items, reviews_total=total, f=_filters(view, direction, source, q, lane, region, "all"), unit="篇")
    else:
        f = _filters(view, direction, source, q, lane, region, pick)
        from ...feeds.enrich import REGIONS
        ctx.update(regions=REGIONS, picks=PICKS, f=f, unit=UNITS[tab])
        if f["pick"] == "today":
            ctx.update(_edition(conn, day if _valid_day(day) else brief, issue))
            if direction:
                # 世界要闻 is the world's news, not a research direction's: the bar narrows 研究相关 and 论文
                d = ctx["digest"] = dict(ctx["digest"])
                before = {"papers": len(d["papers"]), "news": len(d["news"])}
                for key in ("research", "papers"):
                    d[key] = [it for it in d[key] if it.get("direction") == direction]
                d["news"] = d["world"] + d["research"]
                # what the remembered direction hid, so an empty edition says so rather than 「今天还没有」
                from ...intake import directions as directions_mod
                ctx["hidden_by_direction"] = {k: before[k] - len(d[k]) for k in before}
                ctx["direction_name"] = next((str(x.get("name") or x["slug"]) for x in directions_mod.load()
                                              if x["slug"] == direction), direction)
            # The tab links carry day=, so their counts follow the edition on screen: with nothing yet today the
            # page opens on the latest day, and today's zeros would sit beside a full list.
            ctx["tab_counts"].update(papers=unread(ctx["digest"]["papers"]), news=unread(ctx["digest"]["news"]))
            if tab == "news" and ctx["brief"]:
                ctx["events"] = _event_rows(conn, ctx["brief"], ctx["digest"]["world"])
        else:
            items, total = store.list_items(conn, tab, limit=store.PAGE, **{k: v for k, v in f.items() if k != "pick"})
            ctx.update(items=items, total=total, source_options=store.source_options(conn, tab),
                       next_offset=store.PAGE if total > store.PAGE else None,
                       more_qs=_more_qs(tab, f) + (f"&last_day={items[-1]['date']}" if tab == "news" and items else ""))
    return render(request, conn, "pages/feed.html", title="推送", dir_filter=direction, **ctx)


@router.get("/feed/more", response_class=HTMLResponse)
def feed_more(request: Request, tab: str = "papers", view: str = "unread", direction: str = "", source: str = "",
              q: str = "", lane: str = "", region: str = "", pick: str = "all", last_day: str = "", offset: int = 0,
              conn: sqlite3.Connection = Depends(get_conn)):
    if tab not in store.STREAMS:
        raise HTTPException(404, "没有这个分栏")
    f = _filters(view, direction, source, q, lane, region, pick)
    offset = max(0, offset)
    items, total = store.list_items(conn, tab, limit=store.PAGE, offset=offset, **{k: v for k, v in f.items() if k != "pick"})
    nxt = offset + store.PAGE
    t = templates()
    ctx = {**base_context(request, conn), "compact": False, "layout": "stream"}
    html = ""
    day = last_day
    for it in items:   # news reads by day (card 0064): a header where the date changes, also across pages
        if tab == "news" and it["date"] != day:
            html += _day_header(it["date"])
            day = it["date"]
        html += t.get_template("partials/_feed_item.html").render({**ctx, "it": it})
    html += t.get_template("partials/_feed_more.html").render(
        {**ctx, "next_offset": nxt if total > nxt else None, "more_qs": _more_qs(tab, f) + (f"&last_day={day}" if day else "")})
    return HTMLResponse(html)


def _item_or_404(conn: sqlite3.Connection, stream: str, item_id: str) -> dict:
    if stream not in store.STREAMS:
        raise HTTPException(404, "没有这个分栏")
    item = store.get(conn, stream, item_id)
    if item is None:
        raise HTTPException(404, "没有这条推送")
    return item


@router.post("/feed/{stream}/{item_id}/save", response_class=HTMLResponse)
async def feed_save(stream: str, item_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _item_or_404(conn, stream, item_id)
    return _row(request, conn, store.toggle_saved(conn, stream, item_id), **await _shape(request))


@router.post("/feed/{stream}/{item_id}/dismiss", response_class=HTMLResponse)
async def feed_dismiss(stream: str, item_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _item_or_404(conn, stream, item_id)
    return _row(request, conn, store.set_status(conn, stream, item_id, "dismissed"), **await _shape(request))


@router.post("/feed/{stream}/{item_id}/restore", response_class=HTMLResponse)
async def feed_restore(stream: str, item_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _item_or_404(conn, stream, item_id)
    return _row(request, conn, store.set_status(conn, stream, item_id, "new"), **await _shape(request))


@router.post("/feed/papers/{item_id}/read", response_class=HTMLResponse)
@router.post("/feed/papers/{item_id}/import", response_class=HTMLResponse)
async def feed_import(item_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _item_or_404(conn, "papers", item_id)
    shape = await _shape(request)
    http = getattr(request.app.state, "intake_http", None)
    result = await run_in_threadpool(importer.import_item, conn, item_id, http=http)
    if request.url.path.endswith("/read"):
        url = f"/papers/{result.paper_id}"
        if request.headers.get("HX-Request") == "true":
            return HTMLResponse("", headers={"HX-Redirect": url})
        return RedirectResponse(url, status_code=303)
    return _row(request, conn, store.get(conn, "papers", item_id), note=result.note, **shape)


FOLLOW_ENRICH_S = 300   # a status poll keeps waiting for the translation and scoring of new items this long at most


def _status_response(request: Request, conn: sqlite3.Connection, *, primary: bool, finished: bool = False,
                     following: bool = False, since: int | None = None, fresh: bool = False) -> HTMLResponse:
    ctx = {**base_context(request, conn), "st": store.status(conn), "primary": primary, "finished": finished,
           "following": following, "since": since, "fresh": fresh}
    return templates().TemplateResponse(request, "partials/_feed_status.html", ctx)


@router.post("/feed/refresh", response_class=HTMLResponse)
def feed_refresh(request: Request, primary: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    # Leo's own refresh: today's downloadable articles follow it into the library (card 0156)
    scheduler.start_background(getattr(request.app.state, "feeds_http", None), manual=True)
    return _status_response(request, conn, primary=primary == "1")


@router.get("/feed/status", response_class=HTMLResponse)
def feed_status(request: Request, primary: str = "", polling: str = "", since: str = "",
                conn: sqlite3.Connection = Depends(get_conn)):
    """While a refresh runs the status polls itself; after it, it keeps polling while the new items are being
    translated and scored (so 首页 shows them once they qualify), for FOLLOW_ENRICH_S at most. The first poll
    (from a page, without `since`) starts the clock; every later poll carries that same start along."""
    start = int(since) if since.isdigit() else int(time.time())
    st = store.status(conn)
    from ...ai import dispatch
    engine_on = dispatch.route(conn)[0] != "queue"      # with no engine running, waiting for the scoring is pointless
    following = polling == "1" and engine_on and st["enriching"] > 0 and time.time() - start < FOLLOW_ENRICH_S
    done = polling == "1" and not scheduler.running() and not following
    # Show the new items only when a refresh actually finished during this poll chain (another process may have
    # held the lease, leaving the last summary's numbers from an earlier run).
    last = store.last_refresh(conn) or {}
    try:
        fresh = datetime.fromisoformat(last.get("at") or "").timestamp() >= start - 30
    except ValueError:
        fresh = False
    return _status_response(request, conn, primary=primary == "1", finished=done, following=bool(following),
                            since=start, fresh=fresh)


# ---------------------------------------------------------------- 首页: 刷新今天 (card 0104)

def _today_state(conn: sqlite3.Connection) -> dict:
    left = scheduler.manual_left(conn)
    title = (f"刷新今天的新闻和论文 · 今天还能刷新 {left} 次" if left > 0
             else f"今天已手动刷新，明早 {brief_mod.daily_hour(conn)} 点自动更新")
    return {"running": scheduler.today_running(), "left": left, "limit": scheduler.MANUAL_LIMIT, "title": title}


@router.get("/feed/refresh-today")
def refresh_today_state(conn: sqlite3.Connection = Depends(get_conn)):
    """The carousel's refresh button: how many manual refreshes are left today and whether one is running."""
    return JSONResponse(_today_state(conn))


@router.post("/feed/refresh-today")
def refresh_today_start(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Fetch news and papers, rewrite today's brief, recompute today's picks, in the background. The page polls
    GET /feed/refresh-today and then swaps the carousel from /feed/front; it never waits here."""
    result = scheduler.start_today(conn, getattr(request.app.state, "feeds_http", None), manual=True)
    state = {**_today_state(conn), "result": result}
    if result == "limit":
        return JSONResponse(state, status_code=429)
    return JSONResponse({**state, "running": True})


@router.get("/feed/front", response_class=HTMLResponse)
def front_fragment(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """首页's carousel alone, as / renders it, for the swap after 刷新今天."""
    from ... import dashboard
    ctx = {**base_context(request, conn), "brief": brief_mod.latest(conn), **dashboard.build(conn)}
    return templates().TemplateResponse(request, "partials/_front_carousel.html", ctx)
