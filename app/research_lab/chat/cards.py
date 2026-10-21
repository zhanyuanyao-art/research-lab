"""Cards under assistant messages, rendered live from the working records with the app's own partials."""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Callable

from fastapi import HTTPException, Request
from markupsafe import Markup, escape

from .. import i18n

log = logging.getLogger("research_lab.chat.cards")

SEARCH_TPL = """
<div class="chat-card-body chat-search">
{% if not r.total %}<p class="muted">没有找到“{{ q }}”。</p>{% endif %}
{% for label, key in [('文献', 'papers'), ('划线', 'excerpts'), ('产出', 'artifacts')] %}{% if r[key] %}
  <h4 class="kicker">{{ label }} · {{ r[key] | length }}</h4>
  <ul class="chat-hits">{% for h in r[key][:6] %}
    {% if key == 'papers' %}<li><a href="/papers/{{ h.id }}">{{ h.title }}</a>{% if h.year %} <span class="muted">（{{ h.year }}）</span>{% endif %}<div class="chat-snip">{{ h.snippet }}</div></li>
    {% elif key == 'excerpts' %}<li><a href="/excerpts/{{ h.id }}">{{ h.paper_title or '划线' }}</a><div class="chat-snip chat-serif">{{ h.snippet }}</div></li>
    {% else %}<li><a href="/artifacts/{{ h.ref }}">{{ h.title }}</a> <span class="muted">{{ h.week | week_short_zh }} · {{ L.kind[L.type_kind[h.type]] }} · {{ L.level[h.level] }}</span><div class="chat-snip">{{ h.snippet }}</div></li>{% endif %}
  {% endfor %}</ul>
{% endif %}{% endfor %}
{% if r.pages %}
  <h4 class="kicker">正文 · {{ r.pages | length }} 篇文献</h4>
  <ul class="chat-hits">{% for g in r.pages[:5] %}<li><a href="/papers/{{ g.paper_id }}">{{ g.title }}</a>
    {% for hit in g.hits[:2] %}<div class="chat-snip chat-serif"><a class="page-ref" href="/papers/{{ g.paper_id }}/read?page={{ (hit.page_label or hit.page_index) | urlencode }}">p. {{ hit.page_label or hit.page_index }}</a> {{ hit.snippet }}</div>{% endfor %}</li>{% endfor %}</ul>
{% endif %}
</div>
<p class="chat-card-foot"><a href="/search?q={{ q | urlencode }}">查看全部结果</a></p>
"""

WEEK_TPL = """
<div class="chat-card-body chat-week">
  {% if row %}
  <ul class="artifact-list">{% with a = row %}{% include "partials/_artifact_row.html" %}{% endwith %}</ul>
  <p>{% if row.counts %}<span class="chip status-active">本周已完成</span>「想清楚了什么」和「主张」都已写好。
     {% else %}<span class="chip status-draft">未完成</span> 写好「想清楚了什么」和「主张」后，这份产出才算本周完成。{% endif %}</p>
  {% else %}
  <p>本周还没有产出。<a href="/artifacts/new">新建一份</a>。</p>
  {% endif %}
  <p>本周有 <strong>{{ basket_count }}</strong> 条划线待整理。</p>
  {% if now.this_week %}<p class="muted">本周计划：{{ now.this_week }}{% if now.next_week %} · 下周：{{ now.next_week }}{% endif %}</p>{% endif %}
</div>
<p class="chat-card-foot">{% if row %}<a href="/artifacts/{{ row.ref }}">打开本周产出</a> · {% endif %}<a href="/">回到首页</a></p>
"""

ARTIFACTS_TPL = """
<div class="chat-card-body">
  {% if rows %}<ul class="artifact-list">{% for a in rows %}{% include "partials/_artifact_row.html" %}{% endfor %}</ul>
  {% else %}<p class="muted">还没有产出。</p>{% endif %}
</div>
<p class="chat-card-foot"><a href="/artifacts">打开研究档案</a></p>
"""

MILESTONES_TPL = """
<div class="chat-card-body chat-ladder">
  {% if next %}<p>下一步：<strong>{{ L.milestone[next.id] }}</strong>{% if next.failing %} — {{ next.failing.detail or next.failing.label }}{% endif %}</p>
  {% else %}<p>当前阶段的各项检查都已通过。</p>{% endif %}
  {% if suggestion %}<p class="chat-alert chat-alert-nudge">这个阶段的检查都已通过，可以进入<strong>{{ suggestion }}</strong>（在课题页确认）。</p>{% endif %}
  <ol class="chat-ladder-list">{% for m in milestones %}
    <li class="{{ 'passed' if m.passed else ('behind' if m.behind else 'open') }}{{ ' next' if next and next.id == m.id else '' }}">
      <span aria-hidden="true">{{ '●' if m.passed else ('◌' if m.behind else '○') }}</span> {{ L.milestone[m.id] }}</li>{% endfor %}</ol>
</div>
<p class="chat-card-foot"><a href="/projects/{{ slug }}">打开课题</a></p>
"""

JOB_TPL = """
<div class="chat-card-body chat-job">
  {% if job.status == 'claimed' %}<p class="chat-job-line"><span class="chat-pulse" aria-hidden="true"></span>助手正在处理…</p>
  {% elif job.status == 'queued' %}<p class="chat-job-line"><span class="chat-pulse" aria-hidden="true"></span>已交给助手，完成后会出现在这里</p>
  {% elif job.status == 'failed' %}<p class="chat-job-line crit">处理失败{% if job.error %}<span class="muted"> · {{ job.error[:240] }}</span>{% endif %}</p>
  {% elif job.status == 'cancelled' %}<p class="chat-job-line muted">已取消</p>
  {% else %}<p class="chat-job-line">已完成</p>{% endif %}
</div>
"""


TASK_TPL = """
{% set open = t.status in ('queued', 'running') %}
<div class="chat-card-body chat-task">
  <div class="task-head">
    <span class="badge {{ {'waiting': 'warning', 'done': 'success', 'failed': 'danger'}.get(t.status, '') }}">{% if open %}<span class="chat-pulse" aria-hidden="true"></span>{% endif %}{{ {'queued': '排队中', 'running': '进行中', 'waiting': '等你回复', 'done': '已完成', 'failed': '失败', 'cancelled': '已停止'}[t.status] }}</span>
    <span class="muted small">{{ skill_title }}{% if t.engine %} · {{ {'claude': 'Claude Code', 'deepseek': 'DeepSeek'}.get(t.engine, t.engine) }}{% endif %}</span>
    {% if open %}<button type="button" class="btn ghost small task-stop" hx-post="/chat/task/{{ t.id }}/cancel" hx-target="#msg-{{ t.message_id }}" hx-swap="outerHTML">停止</button>{% endif %}
  </div>
  {% if events %}
  {% if open %}<ol class="task-steps">{% for e in events %}<li class="k-{{ e.kind }}">{{ e.text }}</li>{% endfor %}</ol>
  {% else %}<details class="task-trace"><summary>过程 · {{ events | selectattr('kind', 'equalto', 'tool') | list | length }} 步</summary>
    <ol class="task-steps">{% for e in events %}<li class="k-{{ e.kind }}">{{ e.text }}</li>{% endfor %}</ol></details>{% endif %}
  {% elif open %}<p class="muted small">{{ '正在理解你的请求…' if t.status == 'running' else '前面还有任务，马上开始。' }}</p>{% endif %}
  {% if t.status == 'failed' and t.error %}<p class="crit small">{{ t.error }}</p>{% endif %}
  {% if research %}<p>{% if research.status == 'accepted' %}<a class="btn secondary small" href="/artifacts/{{ research.artifact_ref }}">打开已采纳产出</a>{% else %}<a class="btn small {{ 'secondary' if research.status == 'working' else '' }}" href="/chat/research/{{ t.research_session_id }}/review">{{ '查看整理进度' if research.status == 'working' else '审阅产出' }}</a><span class="muted small"> · 第 {{ research.revision }} 版 · {{ '整理中' if research.status == 'working' else '待审阅，尚未归档' }}</span>{% endif %}</p>{% elif t.research_session_id %}<p><a class="btn secondary small" href="/chat/research/{{ t.research_session_id }}/review">查看研究进度</a><span class="muted small"> · 尚未形成待审阅产出</span></p>{% endif %}
  {% if t.status == 'waiting' %}<p class="muted small">在下方回复，助手会接着做。</p>{% endif %}
  {% if not open and t.output_tokens %}<p class="task-usage muted small">用量：输入 {{ '{:,}'.format((t.input_tokens or 0) + (t.cache_tokens or 0)) }}（缓存 {{ '{:,}'.format(t.cache_tokens or 0) }}）· 输出 {{ '{:,}'.format(t.output_tokens or 0) }} tokens</p>{% endif %}
</div>
"""


def _task(conn, ref, ctx) -> str:
    from ..agent import skills, tasks
    t = tasks.get(conn, ref)
    if t is None:
        return _missing("个任务")
    try:
        title = skills.get(t["skill"]).title
    except KeyError:
        title = t["skill"]
    from ..research import store as research_store
    research = research_store.latest(conn, t["research_session_id"]) if t["research_session_id"] else None
    return _tpl(TASK_TPL, {**ctx, "t": t, "events": tasks.events(conn, ref), "skill_title": title, "research": research})


def _base(request: Request, conn: sqlite3.Connection) -> dict[str, Any]:
    """base_context once per request; partials need ai_mode, types, levels, projects."""
    from ..web.context import base_context
    cached = getattr(request.state, "chat_base_ctx", None)
    if cached is None:
        cached = base_context(request, conn)
        request.state.chat_base_ctx = cached
    return cached


def _tpl(name_or_src: str, ctx: dict[str, Any]) -> str:
    from ..web.context import templates
    env = templates().env
    t = env.get_template(name_or_src) if name_or_src.endswith(".html") else env.from_string(name_or_src)
    return t.render(ctx)


def _missing(what: str) -> str:
    return f'<p class="muted">这{escape(what)}已经不存在了。</p>'


def _paper_summary(conn, ref, ctx) -> str:
    from ..web.routes.papers import summary_context
    try:
        sc = summary_context(conn, ref)
    except HTTPException:
        return _missing("篇文献")
    p = sc["paper"]
    head = (f'<header class="chat-card-head"><a href="/papers/{escape(p["id"])}"><strong>{escape(p["title"])}</strong></a> '
            f'<a class="btn small secondary" href="/papers/{escape(p["id"])}/read">阅读</a></header>')
    return head + _tpl("partials/_summary_card.html", {**ctx, **sc})


def _association(conn, ref, ctx) -> str:
    from ..web.routes.excerpts import panel_context
    if not conn.execute("SELECT 1 FROM excerpts WHERE id = ?", (ref,)).fetchone():
        return _missing("条划线")
    head = f'<header class="chat-card-head"><a href="/excerpts/{escape(ref)}">这条划线</a> <span class="muted">的关联建议</span></header>'
    return head + _tpl("partials/_association_panel.html", {**ctx, **panel_context(conn, ref)})


def _excerpt(conn, ref, ctx) -> str:
    from .. import excerpts as ex
    from ..web.routes.excerpts import view
    row = ex.get(conn, ref)
    if row is None:
        return _missing("条划线")
    return _tpl("partials/_excerpt_row.html", {**ctx, "e": view(row), "full": False, "with_panel": False})


def _hunt(conn, ref, ctx) -> str:
    from ..files import datafiles
    from ..method import hunt, matrix
    if not datafiles.project(ref):
        return _missing("个课题")
    side = {"hunt": hunt.hunt_list(conn, ref), "coverage": matrix.coverage(conn, ref),
            "verdicts": matrix.verdicts(conn, ref), "slug": ref}
    foot = f'<p class="chat-card-foot"><a href="/projects/{escape(ref)}/matrix">打开证据矩阵</a></p>'
    return _tpl("partials/_hunt_list.html", {**ctx, **side}) + foot


def _week(conn, ref, ctx) -> str:
    from .. import weekly
    from ..config import get_paths
    from ..files import artifacts, datafiles
    paths = get_paths()
    row = weekly.week_row(conn, ref, paths)
    art = None
    if row and (paths.root / row["path"]).exists():
        art = artifacts.load(paths.root / row["path"])
    groups = weekly.basket(conn, ref, art.body if art else None)
    n = sum(len(g["excerpts"]) for g in groups)
    try:
        now = datafiles.read("now") or {}
    except Exception:  # noqa: BLE001
        now = {}
    return _tpl(WEEK_TPL, {**ctx, "row": row, "week": ref, "basket_count": n, "now": now})


def _search(conn, ref, ctx) -> str:
    from ..web.routes.search import search_all
    return _tpl(SEARCH_TPL, {**ctx, "q": ref, "r": search_all(conn, ref)})


def _artifacts(conn, ref, ctx) -> str:
    from ..config import get_paths
    from ..files import artifacts
    artifacts.index(conn, get_paths())
    if ref == "all":
        rows = conn.execute("SELECT * FROM artifacts ORDER BY ref DESC LIMIT 12").fetchall()
    elif ref.startswith("ART-"):
        rows = conn.execute("SELECT * FROM artifacts WHERE ref = ?", (ref,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM artifacts WHERE project = ? ORDER BY ref DESC LIMIT 12", (ref,)).fetchall()
    return _tpl(ARTIFACTS_TPL, {**ctx, "rows": rows, "ref": ref})


def _milestones(conn, ref, ctx) -> str:
    from ..files import datafiles
    from ..method import milestones
    if not datafiles.project(ref):
        return _missing("个课题")
    ms = milestones.evaluate(conn, ref, write=False)
    nxt = next((m for m in ms if not m.passed and not m.behind), None)
    return _tpl(MILESTONES_TPL, {**ctx, "milestones": ms, "next": nxt, "slug": ref,
                                 "suggestion": milestones.stage_suggestion(conn, ref, ms)})


def _job_wait(conn, ref, ctx) -> str:
    from ..ai import queue
    job = queue.get(conn, ref)
    if job is None:
        return _missing("个任务")
    if job["status"] == "done":
        if job["kind"] == "paper.summarize":
            return render_card(conn, "paper_summary", job["target_id"], ctx["request"])
        if job["kind"] == "excerpt.associate":
            return render_card(conn, "association", job["target_id"], ctx["request"])
        if job["kind"] == "chat.reply":
            card = (json.loads(job["result_json"] or "{}") or {}).get("card") or {}
            if card.get("kind") and card.get("kind") != "job_wait":
                return render_card(conn, card["kind"], str(card.get("ref") or ""), ctx["request"])
            return ""
    return _tpl(JOB_TPL, {**ctx, "job": job})


# Row actions inside a chat card post to the 待办 routes with hx-swap="none ignoreTitle:true" (the id
# makes htmx send HX-Target, so the server answers 303 rather than HX-Redirect; ignoreTitle keeps the
# followed page's <title> off this tab) and then re-fetch the enclosing message, which re-renders the
# card from the file. No ids in visible text: titles and 课题 names only.
_REFRESH = ("var a=this.closest('.chat-msg'); if (a) htmx.ajax('GET', '/chat/message/' + a.dataset.messageId, "
            "{target: a, swap: 'outerHTML'})")
TASK_ROW_TPL = """
{% import "partials/ui.html" as ui with context %}
<li class="list-item chat-task-row">
  <form id="card-{{ t.id }}-done" hx-post="/tasks/{{ t.id }}/status" hx-swap="none ignoreTitle:true" hx-on::after-request="{{ refresh }}">
    <button class="btn ghost icon small" type="submit" name="status" value="done" title="标记完成" aria-label="标记完成">{{ ui.icon('circle-dot') }}</button>
  </form>
  <div class="body">
    <span class="title">{{ t.title }}</span>
    <div class="sub">
      {% if projects.get(t.project) %}<a href="/projects/{{ t.project }}">{{ projects[t.project].name }}</a>{% endif %}
      {% if show_week and t.due %}<span>截止 {{ t.due[5:7] | int }}月{{ t.due[8:10] | int }}日</span>{% endif %}
      {% if t.status == 'doing' %}<span class="chip ok">{{ L.task_status[t.status] }}</span>{% endif %}
      {% if t.postponed %}<span class="chip warn">截止推迟 {{ t.postponed }} 次</span>{% endif %}
      {% if t.source and L.milestone[t.source] != t.source %}<span>来自「{{ L.milestone[t.source] }}」</span>{% endif %}
    </div>
  </div>
  {% if actions %}
  <div class="trail">
    <form id="card-{{ t.id }}-carry" hx-post="/tasks/{{ t.id }}/postpone" hx-swap="none ignoreTitle:true" hx-on::after-request="{{ refresh }}">
      <button class="btn ghost small" type="submit" name="to" value="this_friday">改到本周五</button>
      <button class="btn ghost small" type="submit" name="to" value="next_friday">改到下周五</button>
    </form>
    <form id="card-{{ t.id }}-park" hx-post="/tasks/{{ t.id }}/status" hx-swap="none ignoreTitle:true" hx-on::after-request="{{ refresh }}">
      <button class="btn ghost small" type="submit" name="status" value="parked">搁置</button>
    </form>
  </div>
  {% endif %}
</li>
"""
PARENTS_TPL = """
{% if parents %}<h4 class="kicker">父任务 · {{ parents | length }}</h4>
<ul class="list">{% for p in parents %}
  <li class="list-item chat-task-parent"><div class="body">
    <span class="title">{{ p.sentence }}</span>
    {% if p.subs_this_week %}<ul class="list">{% for t in p.subs_this_week %}{{ row(t, show_week=true) }}{% endfor %}</ul>
    {% else %}<div class="sub"><span>本周没有到期的节</span></div>{% endif %}
  </div></li>
{% endfor %}</ul>{% endif %}
"""
TASKS_TPL = """
{% import "partials/ui.html" as ui with context %}
<div class="chat-card-body chat-tasks">
  {{ parents_block }}
  {% if g.overdue %}<h4 class="kicker">逾期 · {{ g.overdue | length }}</h4>
  <ul class="list">{% for t in g.overdue %}{{ row(t, actions=true, show_week=true) }}{% endfor %}</ul>{% endif %}
  <h4 class="kicker">本周到期 · {{ g.week | length }}</h4>
  {% if g.week %}<ul class="list">{% for t in g.week %}{{ row(t) }}{% endfor %}</ul>
  {% else %}<p class="muted">本周没有到期的待办。</p>{% endif %}
  {% if g.later %}<p class="muted small">以后还有 {{ g.later | length }} 条。</p>{% endif %}
</div>
<p class="chat-card-foot"><a href="{{ '/tasks?project=' ~ slug if slug else '/tasks' }}">打开待办</a></p>
"""
PLAN_TPL = """
{% import "partials/ui.html" as ui with context %}
<div class="chat-card-body chat-plan">
  {{ parents_block }}
  {% if g.overdue %}
  <h4 class="kicker">已过截止 · {{ g.overdue | length }}</h4>
  <ul class="list">{% for t in g.overdue %}{{ row(t, actions=true, show_week=true) }}{% endfor %}</ul>
  {% endif %}
  <h4 class="kicker">本周到期 · {{ g.week | length }}</h4>
  {% if g.week %}<ul class="list">{% for t in g.week %}{{ row(t) }}{% endfor %}</ul>
  {% else %}<p class="muted">本周没有到期的待办。<a href="/tasks/new">新建一条</a>，或从课题页的检查添加。</p>{% endif %}
  {% if now_sha %}
  <form id="card-plan-now" class="stack" hx-post="/week/now" hx-swap="none ignoreTitle:true" hx-on::after-request="{{ refresh }}">
    <input type="hidden" name="sha" value="{{ now_sha }}">
    <div class="field"><label for="plan-this-week">本周只做一件事</label>
      <input type="text" id="plan-this-week" name="this_week" value="{{ now.this_week or '' }}" placeholder="一句话，写在首页本周卡上"></div>
    <div class="form-actions"><button class="btn small" type="submit">定下本周计划</button></div>
  </form>
  {% endif %}
</div>
<p class="chat-card-foot"><a href="/tasks">打开待办</a> · <a href="/">回到首页</a></p>
"""


def _task_rows_env(ctx: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Shared context for the 待办 cards: a `row` macro-like callable, projects, the current week."""
    from ..web.context import templates
    env = templates().env
    row_t = env.from_string(TASK_ROW_TPL)
    base = {**ctx, "refresh": _REFRESH, "current_week": ctx.get("current_week"), **extra}

    def row(t, actions: bool = False, show_week: bool = False) -> Markup:
        return Markup(row_t.render({**base, "t": t, "actions": actions, "show_week": show_week}))

    parents_block = Markup(env.from_string(PARENTS_TPL).render({**base, "row": row})) if base.get("parents") else ""
    return {**base, "row": row, "parents_block": parents_block}


def issue_split(rows: list[dict[str, Any]], week: str) -> tuple[dict[str, list], list[dict[str, Any]]]:
    """The 待办 groups with sub-tasks moved under their parents (dashboard.parent_rows), so a chapter
    is one line with its progress sentence instead of a parent and its sections side by side."""
    from datetime import date
    from ..dashboard import flat_rows, parent_rows
    from ..files import tasks as T
    parents = parent_rows(rows, week, date.today())
    ids = {t["id"] for t in rows}
    g = T.groups(week, rows)
    return {k: flat_rows(v, parents, ids) if k in ("week", "overdue", "later") else v for k, v in g.items()}, parents


def _tasks(conn, ref, ctx) -> str:
    from ..files import tasks as T
    from ..files.weeks import week_of
    rows = T.read()
    slug = None if ref in ("", "all") else ref
    if slug:
        rows = T.for_project(slug, rows)
    g, parents = issue_split(rows, week_of())
    return _tpl(TASKS_TPL, _task_rows_env(ctx, g=g, slug=slug, parents=parents))


def _plan(conn, ref, ctx) -> str:
    from .. import weekly
    from ..files import tasks as T
    from ..files.weeks import week_of
    week = ref if ref and "-W" in ref else week_of()
    g, parents = issue_split(T.read(), week)
    now, now_sha = weekly.now_form()
    return _tpl(PLAN_TPL, _task_rows_env(ctx, g=g, now=now or {}, now_sha=now_sha, week=week, parents=parents))


# ---------------------------------------------------------------- 进度卡 (docs/design/ISSUE-AGENT.md §6)
# card_kind "progress", card_ref "week" | "project:<slug>" | "issue:<ref>", optionally "@YYYY-MM-DD" to pin the
# day the report is computed for (a reply in the history keeps its week). Drawn from checkup's report dict;
# checkup.render() is the same report as Markdown for the model and plain-text places. No model call.

PROGRESS_TPL = """
<div class="chat-card-body chat-progress">
  <header class="chat-card-head"><strong>{{ r.title }}</strong><span class="muted small">{{ r.subtitle }}</span></header>
  {% if r.issue %}<section class="progress-group">
    <p class="progress-line"><a href="{{ r.issue.url }}">{{ r.issue.title }}</a><span class="muted">{{ r.issue.status_label }}{% if r.issue.sentence %} · {{ r.issue.sentence }}{% endif %}</span></p>
    {{ items(head_items) }}
  </section>{% endif %}
  {% for g in groups %}<section class="progress-group">
    <p class="progress-line"><a href="{{ g.s.url }}">{{ g.s.name }}</a><span class="muted">{{ g.week }}{% if g.subs %}；子议题 {{ g.subs }}{% endif %}</span></p>
    {% if g.s.week_total %}<div class="meter progress-meter" role="img" aria-label="本周 {{ g.s.week_total }} 项完成 {{ g.s.week_done }} 项"><span style="width: {{ (100 * g.s.week_done / g.s.week_total) | round | int }}%"></span></div>{% endif %}
    {{ items(g.rows) }}
  </section>{% endfor %}
  {% if rest %}<section class="progress-group">{{ items(rest) }}</section>{% endif %}
</div>
<p class="chat-card-foot">{% if not total %}没有要注意的。{% elif total <= limit %}要注意的共 {{ total }} 条{% else %}要注意的共 {{ total }} 条；最多列 {{ limit }} 条，其余到<a href="/issues">研究档案</a>看{% endif %}</p>
"""
PROGRESS_ITEMS_TPL = """
{% if rows %}<ul class="progress-items">{% for a in rows %}
  <li><a href="{{ a.url }}">{{ a.display }}</a><span class="badge {{ tone(a) }}">{{ a.text }}</span></li>{% endfor %}</ul>{% endif %}
"""
_TONE = {"overdue": "danger", "stalled": "warning", "postponed": "warning", "due_soon": "info"}


def progress_ref(ref: str) -> tuple[str, str, Any]:
    """'project:asean@2026-10-08' → ('project', 'asean', date). Unknown scopes read as the week."""
    from datetime import date
    body, _, day = ref.partition("@")
    scope, _, target = body.partition(":")
    try:
        today = date.fromisoformat(day) if day else None
    except ValueError:
        today = None
    return (scope, target, today) if scope in ("project", "issue") and target else ("week", "", today)


def _progress(conn, ref, ctx) -> str:
    from ..pm import checkup
    from ..files import datafiles
    scope, target, today = progress_ref(ref)
    if scope == "issue" and not target.startswith("PRJ-") and not any(r["id"] == target for r in _issue_rows(ctx)):
        return _missing("个议题")
    slug = target[4:] if target.startswith("PRJ-") else target
    if (scope == "project" or target.startswith("PRJ-")) and not datafiles.project(slug):
        return _missing("个课题")
    r = checkup.run(conn, scope=scope, target=target, today=today)
    env_items = _env().from_string(PROGRESS_ITEMS_TPL)

    def items(rows: list[dict]) -> Markup:
        return Markup(env_items.render(rows=rows, tone=lambda a: _TONE.get(next(k for k in checkup.KINDS if k in a["kinds"]), "")))

    placed: set[str] = set()
    head_items = r["shown"] if r["issue"] else []
    placed |= {a["id"] for a in head_items}
    groups = []
    for s in r["projects"]:
        mine = [a for a in r["shown"] if a["project"] == s["slug"] and a["id"] not in placed]
        placed |= {a["id"] for a in mine}
        groups.append({"s": s, "rows": mine, "week": _week_words(s),
                       "subs": f"{s['done']}/{s['total']}" if s["total"] and s["total"] != s["week_total"] else ""})
    rest = [a for a in r["shown"] if a["id"] not in placed]
    return _tpl(PROGRESS_TPL, {**ctx, "r": r, "groups": groups, "rest": rest, "head_items": head_items, "items": items,
                               "total": len(r["attention"]), "limit": checkup.MAX_LINES})


def _week_words(s: dict) -> str:
    """checkup.render's wording for one 课题's week."""
    if not s["week_total"]:
        return "本周没有安排"
    if s["week_done"] == s["week_total"]:
        return f"本周 {s['week_total']} 项全部完成"
    return f"本周 {s['week_total']} 项完成 {s['week_done']} 项"


# ---------------------------------------------------------------- 确认卡 (docs/design/ISSUE-AGENT.md §5)
# A batch of assistant_changes (pm/changes.py) hangs under the chat message whose id it carries, or, until
# 0077 attaches it, under its task's message. partials/_issue_plan.html draws it; the 执行 / 不用了 / 撤销
# posts in web/routes/chat.py swap the whole card. Everything is rendered from the rows, never from the post.

UNTICK_POSTPONED = 3  # the §5 mockup: a task already postponed 3 times starts unticked, with 搁置 as the hint
SOURCE_CHARS = 24
DIGEST_SOURCE = "每周五的进度"


def _env():
    from ..web.context import templates
    return templates().env


def _issue_rows(ctx: dict[str, Any]) -> list[dict]:
    """issues.read() once per request: a thread can carry several cards."""
    from ..files import issues
    request = ctx.get("request")
    cached = getattr(request.state, "chat_issue_rows", None) if request is not None else None
    if cached is None:
        cached = issues.read() + issues.project_roots()
        if request is not None:
            request.state.chat_issue_rows = cached
    return cached


def task_open(conn, task_id: str | None) -> bool:
    from ..agent import tasks
    if not task_id:
        return False
    t = tasks.get(conn, task_id)
    return bool(t and t["status"] in tasks.OPEN)


def plan_locked(conn, rows: list[dict]) -> bool:
    """A card is executed after the model has finished (§9): while its task runs, rows may still be added."""
    return any(r["status"] == "proposed" for r in rows) and task_open(conn, rows[0]["task_id"])


def _plan_source(conn, first: dict) -> str:
    """Leo's words the batch came from: the task's prompt, else the user message before the card, else the thread.
    The Friday digest's batches have no words behind them; their id says where they came from."""
    from ..pm import changes
    if changes.digest_kind(first["batch_id"]):
        return DIGEST_SOURCE
    text = ""
    if first["task_id"]:
        r = conn.execute("SELECT prompt FROM agent_tasks WHERE id = ?", (first["task_id"],)).fetchone()
        text = r[0] if r else ""
    if not text and first["message_id"]:
        r = conn.execute("SELECT content FROM chat_messages WHERE role = 'user' AND id < ? AND thread_id = "
                         "(SELECT thread_id FROM chat_messages WHERE id = ?) ORDER BY id DESC LIMIT 1",
                         (first["message_id"], first["message_id"])).fetchone()
        text = r[0] if r else ""
    if not text and first["thread_id"]:
        r = conn.execute("SELECT title FROM chat_threads WHERE id = ?", (first["thread_id"],)).fetchone()
        text = r[0] if r else ""
    text = " ".join(str(text or "").split())
    return text if len(text) <= SOURCE_CHARS else text[:SOURCE_CHARS] + "…"


def _plain(value: Any) -> str:
    if value is None or value == "":
        return "无"
    if isinstance(value, bool):
        return "开" if value else "关"
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _plan_row(r: dict, rows_by_id: dict[str, dict], tasks_by_id: dict[str, dict], titles: dict[str, str],
              projects: dict[str, str]) -> dict[str, Any]:
    from ..files import issues
    v: dict[str, Any] = {"id": r["id"], "action": r["action"], "status": r["status"], "result": r["result"] or "",
                         "note": r["note"] or "", "hint": "", "ticked": True, "url": "", "title": "", "where": "",
                         "invalid": False}
    if r["kind"] != "issue":                       # settings (card 0080) and kinds to come: say it plainly
        v.update(label=r["label"] or r["target"], what=r["field"], old=_plain(r["old"]), new=_plain(r["new"]))
        return v
    target = r["target"]
    row = rows_by_id.get(target)
    v["label"] = row["title"] if row else (r["label"] or "已删除的议题")
    if row and r["result"] not in ("deleted", "gone"):
        v["url"] = row["url"]
    if r["action"] == "create":
        new = r["new"] or {}
        parent = rows_by_id.get(new.get("parent") or "")
        v.update(title=str(new.get("title") or r["label"]), what="新建子议题" if parent else "新建议题")
        where = [f"在「{parent['title']}」下" if parent else projects.get(new.get("project") or "", "")]
        if new.get("week") and not new.get("due"):          # a row proposed before 0093
            where.append("截止 " + issues.display("due", issues.week_due(new["week"])))
        if new.get("due"):
            where.append("截止 " + issues.display("due", new["due"]))
        v["where"] = " · ".join(w for w in where if w)
        return v
    if r["action"] == "comment":
        text = " ".join(str((r["new"] or {}).get("text") or "").split())
        v.update(what="记一条评论", old="", new=text if len(text) <= 40 else text[:40] + "…")
        return v
    field = r["field"]
    # once written, detail holds the values really stored (the proposal may have said 进行中 for doing)
    written = r["detail"] if r["status"] in ("applied", "undone") and "new" in (r["detail"] or {}) else r
    v.update(what=i18n.ISSUE_FIELD.get(field, field), old=issues.display(field, written["old"], titles=titles),
             new=issues.display(field, written["new"], titles=titles))
    if r["status"] != "proposed":
        return v
    from ..pm import changes
    if changes.digest_kind(r["batch_id"]) in changes.DIGEST_OPT_IN:
        v["ticked"] = False          # the Friday digest's 搁置 and this week's unfinished: Leo opts in row by row
    if row is None:
        v["hint"] = "这条议题已经不在了"
        return v
    current = issues.value_of(row, field)
    if issues.same(field, current, r["new"]):
        v["hint"] = "已经是这样了"
    elif not issues.same(field, current, r["old"]):
        v["hint"] = f"你已经改成了 {issues.display(field, current, titles=titles)}，执行时会跳过"
    elif field == "due":
        postponed = int((tasks_by_id.get(target) or {}).get("postponed") or 0)
        if postponed >= UNTICK_POSTPONED and _later(r["new"], current):
            v.update(hint=f"截止已推迟 {postponed} 次，也可以搁置", ticked=False)
        elif postponed:
            v["hint"] = f"截止已推迟 {postponed} 次"
    return v


def _later(new: Any, current: Any) -> bool:
    """A later due date is another postponement; an earlier one (pulled forward) is not."""
    new, current = str(new or ""), str(current or "")
    return bool(new and current and new > current)


def title_problem(title: str) -> str:
    """Why an edited sub-issue title cannot be used, in Chinese; '' when it can (files/tasks.py TITLE_MAX)."""
    from ..files import tasks as T
    if not title:
        return "标题不能空着"
    return f"标题不要超过 {T.TITLE_MAX} 个字" if len(title) > T.TITLE_MAX else ""


def plan_view(conn, batch_id: str, ctx: dict[str, Any], *, flash: str = "",
              posted: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """What _issue_plan.html draws for one batch: the state, a head line, the rows and the controls.
    posted = {"ticked": {ids}, "titles": {id: text}} redraws a refused 执行 as Leo left it."""
    from ..files import datafiles
    from ..files import tasks as T
    from ..pm import changes
    rows = changes.rows(conn, batch_id)
    if not rows:
        return None
    issue_rows = _issue_rows(ctx)
    rows_by_id = {r["id"]: r for r in issue_rows}
    titles = {r["id"]: r["title"] for r in issue_rows}
    projects = {str(p["slug"]): str(p.get("name") or p["slug"]) for p in datafiles.read("projects") or []}
    tasks_by_id = {t["id"]: t for t in T.read()} if any(r["field"] == "due" and r["status"] == "proposed" for r in rows) else {}
    views = {r["id"]: _plan_row(r, rows_by_id, tasks_by_id, titles, projects) for r in rows}
    if posted is not None:
        for rid, v in views.items():
            if v["status"] != "proposed":
                continue
            v["ticked"] = rid in posted["ticked"]
            if v["action"] == "create" and rid in posted["titles"]:
                v["title"] = posted["titles"][rid]
                v["invalid"] = bool(v["ticked"] and title_problem(v["title"]))
                if v["invalid"]:
                    v["hint"] = title_problem(v["title"])

    proposed = [r for r in rows if r["status"] == "proposed"]
    applied = [r for r in rows if r["status"] == "applied" and r["result"] != "kept"]
    kept = [r for r in rows if r["status"] == "applied" and r["result"] == "kept"]
    undone = [r for r in rows if r["status"] == "undone"]
    skipped = [r for r in rows if r["status"] == "dismissed" and r["result"]]
    unticked = [r for r in rows if r["status"] == "dismissed" and not r["result"]]
    undoable = changes._undoable(rows)      # the rule pm.changes.undo itself uses

    def tone(r: dict) -> str:
        if r["result"] == "error":
            return "crit"
        return "warn" if r["result"] in ("conflict", "kept") else ("muted" if r["result"] in ("noop", "gone") else "")

    more = ""
    if proposed:
        # rows already settled while the card waited (an issue deleted by an undo: result gone) stay in sight
        state, head, shown = "proposed", f"助手想做 {len(proposed)} 处修改", skipped
    elif undoable or (applied and not undone):
        state, head, shown = "applied", f"助手已改 {len(applied)} 处", applied + skipped
        if unticked:
            more = f"另有 {len(unticked)} 处没勾选，没有执行。"
    elif undone or kept:
        state, shown = "undone", kept + [r for r in rows if r["result"] == "error" and r["status"] == "applied"] + undone
        head = f"已撤销 {len(undone)} 处修改" if undone else "没有撤销"
        if kept:
            head += f"；{len(kept)} 处你后来改过，留着没动"
    else:
        state, shown = "dismissed", skipped + unticked
        head = "没有执行" + ("：你选了不用了" if unticked and not skipped else "")
    lines = [dict(views[r["id"]], tone=tone(r)) for r in shown]
    plain = [x for x in lines if not (x["note"] or x["tone"])]
    first = rows[0]
    reason = " ".join(str(first["reason"] or "").split())
    return {"batch_id": batch_id, "state": state, "head": head, "source": _plan_source(conn, first),
            "reason": reason[:120], "rows": [views[r["id"]] for r in proposed],
            # rows with a note stay in sight; a long run of plain ones folds
            "flagged": [x for x in lines if x["note"] or x["tone"]], "plain": plain, "fold": len(plain) > 3,
            "more": more, "undoable": undoable, "locked": plan_locked(conn, rows),
            "ticked": sum(views[r["id"]]["ticked"] for r in proposed), "shown": ",".join(str(r["id"]) for r in proposed),
            "flash": flash, "title_max": T.TITLE_MAX}


def render_plan(conn, batch_id: str, request: Request, *, flash: str = "", posted: dict[str, Any] | None = None) -> str:
    """One confirmation card, or '' for an unknown batch. Never raises: a broken card says so."""
    try:
        p = plan_view(conn, batch_id, _base(request, conn), flash=flash, posted=posted)
        return _tpl("partials/_issue_plan.html", {**_base(request, conn), "p": p}) if p else ""
    except Exception as e:  # noqa: BLE001 - one broken card must not take the conversation down
        log.exception("plan card %s failed", batch_id)
        return (f'<div class="chat-card chat-card-plan" id="plan-{escape(batch_id)}"><p class="crit">'
                f'Could not render this card ({escape(type(e).__name__)}: {escape(str(e)[:200])}).</p></div>')


def plan_batches(conn, message_id: int, task_id: str | None = None) -> list[str]:
    """Batch ids under one message, oldest first: those attached to it, and its task's unattached ones."""
    q = "SELECT batch_id, MIN(id) AS first FROM assistant_changes WHERE message_id = ?"
    args: list[Any] = [message_id]
    if task_id:
        q += " OR (message_id IS NULL AND task_id = ?)"
        args.append(task_id)
    return [r[0] for r in conn.execute(q + " GROUP BY batch_id ORDER BY first", args)]


def render_plans(conn, message_id: int, task_id: str | None, request: Request) -> str:
    return "".join(render_plan(conn, b, request) for b in plan_batches(conn, message_id, task_id))


RENDERERS: dict[str, Callable[[sqlite3.Connection, str, dict[str, Any]], str]] = {
    "tasks": _tasks, "plan": _plan,
    "paper_summary": _paper_summary, "association": _association, "hunt": _hunt, "week": _week, "search": _search, "task": _task,
    "excerpt": _excerpt, "artifacts": _artifacts, "milestones": _milestones, "job_wait": _job_wait,
    "progress": _progress,
}


def render_card(conn: sqlite3.Connection, kind: str | None, ref: str | None, request: Request) -> str:
    """HTML for one card, or '' when the message has none. Never raises: a broken card says so."""
    if not kind:
        return ""
    fn = RENDERERS.get(kind)
    if fn is None or not ref:
        return ""
    try:
        inner = fn(conn, ref, _base(request, conn))
    except Exception as e:  # noqa: BLE001 - one broken card must not take the conversation down
        log.exception("card %s %s failed", kind, ref)
        inner = f'<p class="crit">Could not render this card ({escape(type(e).__name__)}: {escape(str(e)[:200])}).</p>'
    if not inner:
        return ""
    return str(Markup(f'<div class="chat-card chat-card-{escape(kind)}" data-card-kind="{escape(kind)}" '
                      f'data-card-ref="{escape(ref)}">{inner}</div>'))
