"""Per-request DB connection and the shared template context."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import quote, unquote, urlencode

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from ..config import APP_VERSION, build_label, get_paths
from ..db.connection import connect, get_setting, migrate
from ..files import datafiles
from ..files.weeks import week_of
from ..intake.extract import reflow
from .. import i18n

_md = MarkdownIt("commonmark", {"html": True}).enable("table")
_templates: Jinja2Templates | None = None

# Primary sections: (label, href, path prefixes that light the item up). Secondary pages keep their
# own routes and highlight the section they belong to.
NAV = [
    ("首页", "/", ("/projects", "/pipeline", "/digest", "/now", "/feed"), "house"),
    ("文献", "/papers", ("/papers", "/excerpts"), "book-open"),
    ("数据", "/data", ("/data",), "database"),
    ("研究档案", "/issues", ("/issues", "/artifacts", "/week", "/harvest"), "folder"),
]
FOOT_NAV = [
    ("助手", "/chat", ("/chat", "/assistant"), "sparkles"),     # labelled with the assistant's name in base_context
    ("设置", "/settings", ("/settings", "/agent"), "settings"),
]
# Identity colors exist for the preset directions only; user-added directions use the neutral slot
# rather than a generated hue (docs/design/PRODUCT.md, color carries meaning).
PRESET_DIRECTION_COLORS = {"alliance-us", "sea-asean", "strategy-signaling", "periphery-nts"}


def direction_color(slug: str | None) -> str:
    return f"var(--d-{slug})" if slug in PRESET_DIRECTION_COLORS else "var(--d-other)"
# Sub-tab presets for partials/_subnav.html (see the contract at the top of that file).
SUBNAVS = {
    "artifacts": [("书架", "/artifacts"), ("本周", "/week"), ("回顾", "/artifacts/review")],
    "settings": [("设置", "/settings"), ("队列", "/agent")],
    "papers": [("文献库", "/papers"), ("片段", "/excerpts"), ("雷达", "/digest")],
    "projects": [("项目与里程碑", "/projects"), ("研究管线", "/pipeline")],
}


def _active(path: str, href: str, prefixes: tuple[str, ...] = ()) -> bool:
    return path == href or any(path == p or path.startswith(p + "/") for p in (prefixes or (href,)) if p != "/")


def asset_url(path: str) -> str:
    """Static URL with the file's mtime as a version, so an updated app never serves a cached stylesheet."""
    try:
        v = int((get_paths().web_static / path).stat().st_mtime)
    except OSError:
        v = 0
    return f"/static/{path}?v={v}"


def local_time(value: str | None, fmt: str = "%m-%d %H:%M") -> str:
    """A stored ISO timestamp in this Mac's local time. Naive values are UTC (now_iso and SQLite defaults)."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime(fmt)


def date_zh(value: str | None) -> str:
    try:
        day = datetime.fromisoformat(str(value)[:10])
    except ValueError:
        return ""
    return f"{day.month}月{day.day}日"


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def templates() -> Jinja2Templates:
    global _templates
    if _templates is None:
        t = Jinja2Templates(directory=str(get_paths().web_templates))
        t.env.filters["md"] = lambda s: _md.render(s or "")
        t.env.filters["short_week"] = lambda w: (w or "").split("-W")[-1] and "W" + (w or "").split("-W")[-1]
        t.env.filters["week_zh"] = i18n.week_label
        t.env.filters["week_short_zh"] = i18n.short_week
        t.env.filters["reflow"] = reflow
        t.env.filters["local_time"] = local_time
        t.env.filters["date_zh"] = date_zh
        from ..files.markdown import render as render_markdown
        t.env.filters["markdown"] = render_markdown   # issue descriptions (and later comments): files/markdown.py
        t.env.globals["APP_VERSION"] = APP_VERSION
        t.env.globals["BUILD_LABEL"] = build_label()
        t.env.globals["asset"] = asset_url
        t.env.globals["direction_url"] = direction_url
        from ..files.issues import owner_name
        t.env.globals["workspace_owner"] = owner_name
        # Safety net for partials rendered without base_context (htmx swaps): base_context overrides these.
        t.env.globals.update(L=i18n.Labels(), direction_map={}, directions=[], subnavs={})
        _templates = t
    return _templates


def by_key(items: list[dict] | None, key: str = "key") -> dict[str, dict]:
    return {str(i[key]): dict(i) for i in (items or [])}


def research_directions(data, active=''):
    """Direction navigation counts projects, including legal projects without a direction."""
    rows = list(data.get('directions') or [])
    known = {d['slug'] for d in rows}
    counts = {}
    for p in data.get('projects') or []:
        slug = p.get('direction') if p.get('direction') in known else '_none'
        counts[slug] = counts.get(slug, 0) + 1
    if counts.get('_none'):
        rows.append(dict(slug='_none', name='未分类'))
    return [dict(d, color=direction_color(d['slug']), count=counts.get(d['slug'], 0),
                 active=d['slug'] == active) for d in rows]


def base_context(request: Request, conn: sqlite3.Connection) -> dict[str, Any]:
    from ..distribution import is_trial
    data = datafiles.read_all()
    from ..ai import queue
    path = request.url.path
    nav = [{"label": l, "href": h, "icon": ic, "active": _active(path, h, pre)} for l, h, pre, ic in NAV]
    from ..agent.skills import assistant
    who = assistant()
    foot_nav = [{"label": who.name if h == "/chat" else l, "href": h, "icon": ic, "active": _active(path, h, pre)}
                for l, h, pre, ic in FOOT_NAV]
    subnavs = {k: [{"label": l, "href": h} for l, h in items] for k, items in SUBNAVS.items()}
    directions = research_directions(data)
    for d in directions:
        d['active'] = path.startswith(f"/artifacts/d/{d['slug']}")
    if any(d["active"] for d in directions):
        for item in nav:
            item["active"] = item["active"] and item["href"] != "/issues"
    return {
        "maintainer": not is_trial(),
        "request": request, "nav": nav, "foot_nav": foot_nav, "subnavs": subnavs, "data": data,
        "assistant_name": who.name, "assistant_intro": who.intro,
        "L": i18n.Labels(data), "nav_directions": directions,
        "directions": [d for d in directions if d['slug'] != '_none'],
        "direction_map": {str(d.get("slug")): d for d in directions if d['slug'] != '_none'},
        "types": by_key(data.get("types")), "levels": by_key(data.get("levels")), "stages": data.get("stages") or [],
        "projects": {str(p["slug"]): dict(p) for p in (data.get("projects") or [])},
        "current_week": week_of(), "week_label": i18n.week_label(week_of()), "ai_counts": queue.counts(conn), "ai_mode": queue.mode(conn),
        "theme": get_setting(conn, "ui.theme", "system"), "direction_color": direction_color,
        # the Mac window only, not a browser pointed at the same server (desktop.DESKTOP_UA)
        "desktop": os.environ.get("RL_DESKTOP") == "1" and "ResearchLabDesktop" in request.headers.get("user-agent", ""),
    }


# The 研究方向 bar of 文献 · 数据 · 产出 · 推送 (partials/_direction_filter.html, card 0066). A cookie, not a
# setting: it is this browser's view, so the page smoke, tests and agents reading these pages with no cookie
# always get the unfiltered page, and opening a page never writes the database.
DIRECTION_COOKIE = "rl_direction"
DIRECTION_COOKIE_AGE = 400 * 24 * 3600   # the longest lifetime browsers keep
# one-shot notices, paging and 推送's legacy brief= that a switch of direction should not carry over
_DIRECTION_URL_DROP = {"direction", "added", "dup", "rejected", "pending", "deleted", "saved", "msg", "err", "offset", "brief"}


def _direction_slugs() -> set[str]:
    return {str(d["slug"]) for d in datafiles.read("directions") or [] if d.get("slug")}


def _remembered_direction(request: Request) -> str:
    return unquote(request.cookies.get(DIRECTION_COOKIE, ""))


def chosen_direction(request: Request, *, scoped: bool = False) -> str:
    """The direction a 文献 · 数据 · 产出 · 推送 page filters by. A link that names one wins (`?direction=`,
    empty for 全部); otherwise the one last picked on any of these pages, until 全部 is picked. A page already
    narrowed to a 课题 or a folder (`scoped`) ignores the remembered one: the 课题 has its own direction, and
    a folder cuts across them."""
    q = request.query_params
    if "direction" in q:
        return (q.get("direction") or "").strip()
    if scoped:
        return ""
    slug = _remembered_direction(request)
    return slug if slug in _direction_slugs() else ""


def direction_url(request: Request, slug: str) -> str:
    """The current page with `direction` set to `slug`; 全部 keeps an empty `direction=` so it clears the pick."""
    params = [(k, v) for k, v in request.query_params.multi_items() if k not in _DIRECTION_URL_DROP]
    return request.url.path + "?" + urlencode(params + [("direction", slug)])


def _remember_direction(request: Request, response: Any) -> None:
    """A page that shows the bar keeps a known direction named in its URL, and forgets on 全部. Other values
    (`none` 未归档, `_none` 未分类 on 文献) filter this page only."""
    if "direction" not in request.query_params:
        return
    slug = (request.query_params.get("direction") or "").strip()
    current = _remembered_direction(request)
    if not slug and current:
        response.delete_cookie(DIRECTION_COOKIE, path="/")
    elif slug and slug != current and slug in _direction_slugs():
        response.set_cookie(DIRECTION_COOKIE, quote(slug), max_age=DIRECTION_COOKIE_AGE, path="/",
                            httponly=True, samesite="lax")


def render(request: Request, conn: sqlite3.Connection, template: str, **ctx: Any):
    context = base_context(request, conn)
    context.update(ctx)
    # Keep the global issue composer in the project/direction of legacy material pages too.
    parts = request.url.path.strip('/').split('/')
    project = parts[1] if len(parts) > 1 and parts[0] == 'projects' else ''
    if ctx.get('a') is not None:
        project = str(ctx['a'].get('project') or '')
    if project in context['projects']:
        context.setdefault('issue_project', project)
        context.setdefault('filter_direction', context['projects'][project].get('direction', ''))
    elif len(parts) > 2 and parts[:2] == ['artifacts', 'd']:
        context.setdefault('filter_direction', parts[2])
    if ctx.get('filter_direction'):
        for item in context['nav']:
            if item['href'] == '/issues':
                item['active'] = False
    response = templates().TemplateResponse(request, template, context)
    if "dir_filter" in ctx:   # the page shows the direction bar
        _remember_direction(request, response)
    return response
