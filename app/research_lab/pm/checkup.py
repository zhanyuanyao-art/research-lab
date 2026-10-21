"""进度检查 (docs/design/ISSUE-AGENT.md §6): what is late, stuck or due soon, per 课题, without a model.

`check` is a pure function over issue rows (files.issues.read / project_roots), task dicts
(files.tasks.read, for `postponed`) and a last-touched map (files.issues.last_touched); `run(conn)`
gathers those and reads pm.stalled_days (tunables: 7–30, default 14). `render(report)` is the Chinese Markdown the assistant
posts (titles as links, at most MAX_LINES attention lines); the progress card can draw the report
dict itself instead.

    run(conn, scope='week' | 'project' | 'issue', target='', today=None) -> report
    check(rows, *, tasks, touched, projects, roots, scope, target, today, stalled_days, word_counts) -> report
    signals(row, task=, touched=, open_children=, today=, week=, stalled_days=) -> {kind: number}
    leaves(rows, slug) · project_summary(rows, project, week) · stalled_days(conn) · rules(conn) · render(report)

Signals of an open issue (todo or doing, not archived), in the order lines are ranked:
    overdue   the due date has passed                  pm.progress.is_overdue        → days overdue
    stalled   doing, no change or comment for N days    files.issues.last_touched     → idle days
    postponed due date moved later 2 times or more      tasks.yml postponed           → times
    due_soon  due within 7 days                         files.tasks.flags()['due_soon'] → days left
There is no plan week any more (docs/design/ISSUE-AGENT.md §6, ISSUES-LINEAR.md §3): 本周 is what is
due by Sunday, overdue included, plus what was finished since Monday. A parent with open sub-issues is
not stalled by itself: its progress is theirs, so the sub-issue carries the line. A 课题 (project
root) only has due dates. RULES maps a kind to the preset rule that can switch it off (pm.rule.*).

report = {scope, target, title, subtitle, week, today, stalled_days,
          projects: [project summary], issue: issue summary | None,
          attention: [item, ranked], shown: [the first MAX_LINES items]}
project summary = {slug, name, url, todo, doing, done, total, week_total, week_done, week_open} (week_*: 本周)
item = {id, title, display, url, project, kinds: {kind: number}, text}
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .. import i18n
from .. import tunables
from ..files import datafiles, issues
from ..files import tasks as T
from ..files.weeks import week_of
from . import progress

STALLED_DAYS = 14          # pm.stalled_days' default in tunables.py (Leo, 10-08); check() falls back to it
DUE_SOON_DAYS = 7          # files.tasks.flags()['due_soon']
POSTPONED_MIN = 2          # docs/design/PM-AGENT.md: two postponements suggest parking or splitting
MAX_LINES = 5
KINDS = ("overdue", "stalled", "postponed", "due_soon")
RULES = {"overdue": "pm.rule.overdue", "stalled": "pm.rule.stalled"}
WEEKDAYS = "一二三四五六日"


def stalled_days(conn=None) -> int:
    """pm.stalled_days through the tunables registry: out of range or unreadable reads as the default."""
    return int(tunables.tunable(conn, "pm.stalled_days") if conn is not None else tunables.default("pm.stalled_days"))


def rules(conn=None) -> dict[str, bool]:
    """kind → whether its preset rule (RULES, tunables, default on) is on. Only the Friday digest's
    suggestions follow them (pm/scheduler.py); a progress Leo asks for always shows every kind."""
    return {kind: bool(tunables.tunable(conn, key) if conn is not None else tunables.default(key))
            for kind, key in RULES.items()}


# ---------------------------------------------------------------- pure parts

def leaves(rows: list[dict], slug: str) -> list[dict]:
    """A 课题's countable issues: not archived, not parked, and not a parent of an active one in it.
    The 首页 课题 bars (dashboard.project_progress) count these too."""
    active = [r for r in rows if r['project'] == slug and not r['archived'] and r['status'] != 'parked']
    by_id = {r['id']: r for r in rows if r['project'] == slug}
    parents = set()
    for row in active:
        ancestor, visited = row.get('parent'), {row['id']}
        while ancestor in by_id and ancestor not in visited:
            visited.add(ancestor)
            parents.add(ancestor)
            ancestor = by_id[ancestor].get('parent')
    return [r for r in active if r['id'] not in parents]


def this_week(row: dict, today: date | None = None) -> bool:
    """本周: open and due by Sunday (overdue included), or a task finished since Monday."""
    return T.due_this_week(row, today) or T.done_this_week(row, today)


def project_summary(rows: list[dict], project: dict, week: str = '', today: date | None = None) -> dict:
    """`week` is accepted for older callers and not used: 本周 is this_week(row, today)."""
    slug = str(project['slug'])
    ls = leaves(rows, slug)
    planned = [r for r in ls if this_week(r, today)]
    return dict(slug=slug, name=str(project.get('name') or slug), url=issues.url(issues.project_ref(slug)),
                **{s: sum(r['status'] == s for r in ls) for s in ('todo', 'doing', 'done')}, total=len(ls),
                week_total=len(planned), week_done=sum(r['status'] == 'done' for r in planned),
                week_open=sum(r['status'] in T.OPEN for r in planned))


def signals(row: dict, *, task: dict | None = None, touched: str = '', open_children: bool = False,
            today: date, week: str, stalled_days: int = STALLED_DAYS) -> dict[str, int]:
    if row.get('archived') or row.get('status') not in T.OPEN:
        return {}
    out: dict[str, int] = {}
    f = T.flags(row, week, today)
    if progress.is_overdue(row, today) and f['due_days'] is not None:
        out['overdue'] = -f['due_days']
    if f['due_soon']:
        out['due_soon'] = f['due_days']
    if row.get('project_root'):
        return out
    if not open_children:
        if row['status'] == 'doing' and touched:
            try:
                idle = (today - date.fromisoformat(touched[:10])).days
            except ValueError:
                idle = 0
            if idle >= stalled_days:
                out['stalled'] = idle
    postponed = int((task or {}).get('postponed') or 0)
    if postponed >= POSTPONED_MIN:
        out['postponed'] = postponed
    return out


def _reason(kind: str, n: int, row: dict, today: date) -> str:
    if kind == 'overdue':
        return f"逾期 {n} 天"
    if kind == 'stalled':
        return f"进行中，{n} 天没动"
    if kind == 'due_soon':
        return "今天截止" if n == 0 else f"{n} 天后截止"
    return f"截止已推迟 {n} 次，建议搁置或拆小"


def _text(kinds: dict[str, int], row: dict, today: date) -> str:
    order = ('overdue', 'stalled', 'due_soon', 'postponed')     # the suggestion reads last
    return "，".join(_reason(k, kinds[k], row, today) for k in order if k in kinds)


def _rank(item: dict) -> tuple:
    kinds = item['kinds']
    first = min(KINDS.index(k) for k in kinds)
    weight = kinds[KINDS[first]]
    return (first, weight if KINDS[first] == 'due_soon' else -weight, item['display'])


def _date_line(today: date) -> str:
    return f"截至 {today.month} 月 {today.day} 日周{WEEKDAYS[today.weekday()]}"


def check(rows: list[dict], *, tasks: list[dict] | None = None, touched: dict[str, str] | None = None,
          projects: list[dict] | None = None, roots: list[dict] | None = None, scope: str = 'week',
          target: str = '', today: date | None = None, stalled_days: int = STALLED_DAYS,
          word_counts: dict[str, int | None] | None = None) -> dict[str, Any]:
    """The progress report for this week (everything), one 课题 (target = slug or PRJ-slug) or one
    issue and its sub-issues (target = ref). Deterministic: same inputs, same report."""
    today = today or date.today()
    week = week_of(today)
    tasks_by_id = {t['id']: t for t in tasks or []}
    touched = touched or {}
    projects = list(projects or [])
    roots = list(roots or [])
    if scope == 'issue' and target.startswith('PRJ-'):
        scope, target = 'project', target[4:]
    if scope == 'project' and target.startswith('PRJ-'):
        target = target[4:]
    by_id = {r['id']: r for r in rows}
    kids: dict[str, list[dict]] = {}
    for r in rows:
        if r.get('parent'):
            kids.setdefault(r['parent'], []).append(r)

    if scope == 'issue':
        head = by_id.get(target)
        pool = [head] + issues.descendants(target, rows) if head else []
        pool_roots: list[dict] = []
    elif scope == 'project':
        pool = [r for r in rows if r['project'] == target]
        pool_roots = [r for r in roots if r['project'] == target]
    else:
        scope = 'week'
        pool, pool_roots = list(rows), list(roots)

    names = {str(p['slug']): str(p.get('name') or p['slug']) for p in projects}
    attention = []
    for r in pool + pool_roots:
        kinds = signals(r, task=tasks_by_id.get(r['id']), touched=touched.get(r['id'], ''),
                        open_children=any(c['status'] in T.OPEN and not c['archived'] for c in kids.get(r['id'], [])),
                        today=today, week=week, stalled_days=stalled_days)
        if not kinds:
            continue
        parent = by_id.get(r.get('parent') or '')
        display = f"{parent['title']} · {r['title']}" if parent else r['title']
        attention.append(dict(id=r['id'], title=r['title'], display=display, url=r['url'], project=r['project'],
                              kinds=kinds, text=_text(kinds, r, today)))
    attention.sort(key=_rank)

    summaries, issue_summary = [], None
    if scope == 'issue' and pool:
        head = pool[0]
        task = tasks_by_id.get(head['id']) or {}
        subs = kids.get(head['id'], [])
        merged = dict(head, file=task.get('file'), words=task.get('words'))
        issue_summary = dict(id=head['id'], title=head['title'], url=head['url'], status=head['status'],
                             status_label=i18n.ISSUE_STATUS.get(head['status'], head['status']),
                             progress=progress.counted(subs),
                             sentence=progress.sentence(merged, subs, (word_counts or {}).get(head['id']), today,
                                                        with_title=False))
    elif scope == 'project':
        project = next((p for p in projects if str(p['slug']) == target), {'slug': target, 'name': target})
        summaries.append(project_summary(rows, project, week, today))
    else:
        flagged = {a['project'] for a in attention}
        for p in projects:
            s = project_summary(rows, p, week, today)
            if s['week_total'] or s['slug'] in flagged:
                summaries.append(s)

    week_words = issues.display('week', week, today=today) + "进度"
    if scope == 'issue':
        title = f"{issue_summary['title'] if issue_summary else '议题'} · 进度"
    elif scope == 'project':
        title = f"{names.get(target, target)} · {week_words}"
    else:
        title = week_words
    return dict(scope=scope, target=target, title=title, subtitle=_date_line(today), week=week,
                today=today.isoformat(), stalled_days=stalled_days, projects=summaries, issue=issue_summary,
                attention=attention, shown=attention[:MAX_LINES])


# ---------------------------------------------------------------- gathering and words

def run(conn=None, scope: str = 'week', target: str = '', today: date | None = None) -> dict[str, Any]:
    """check() over the files as they are now. Reads only; never calls a model."""
    rows = issues.read()
    tasks = T.read()
    word_counts = {}
    if scope == 'issue' and target:
        task = next((t for t in tasks if t['id'] == target), None)
        if task and task.get('file'):
            word_counts[target] = progress.word_count_for(task)
    return check(rows, tasks=tasks, touched=issues.last_touched(rows), projects=datafiles.read('projects') or [],
                 roots=issues.project_roots(), scope=scope, target=target, today=today,
                 stalled_days=stalled_days(conn), word_counts=word_counts)


def _md(text: str) -> str:
    return str(text).replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]')


def _link(title: str, url: str) -> str:
    return f"[{_md(title)}]({url})"


def _project_line(s: dict) -> str:
    if not s['week_total']:
        week = "本周没有到期的事"
    elif s['week_done'] == s['week_total']:
        week = f"本周 {s['week_total']} 项全部完成"
    else:
        week = f"本周 {s['week_total']} 项完成 {s['week_done']} 项"
    subs = f"；子议题 {s['done']}/{s['total']}" if s['total'] and s['total'] != s['week_total'] else ""
    return f"{_link(s['name'], s['url'])}：{week}{subs}"


def _item_line(a: dict) -> str:
    return f"- {_link(a['display'], a['url'])}：{a['text']}"


def render(report: dict) -> str:
    """The §6 sample as Markdown: a header, one line per 课题 (or the issue), its attention lines
    under it, then the count. At most MAX_LINES attention lines; the rest are on the issue pages."""
    out = [f"**{_md(report['title'])}** · {report['subtitle']}", ""]
    shown = report['shown']
    placed: set[str] = set()
    if report['issue']:
        s = report['issue']
        tail = f" · {s['sentence']}" if s['sentence'] else ""
        out.append(f"{_link(s['title'], s['url'])}：{s['status_label']}{tail}")
        out += [_item_line(a) for a in shown]
        placed = {a['id'] for a in shown}
        out.append("")
    for s in report['projects']:
        out.append(_project_line(s))
        mine = [a for a in shown if a['project'] == s['slug'] and a['id'] not in placed]
        out += [_item_line(a) for a in mine]
        placed |= {a['id'] for a in mine}
        out.append("")
    rest = [a for a in shown if a['id'] not in placed]
    if rest:
        out += [_item_line(a) for a in rest] + [""]
    n = len(report['attention'])
    if not n:
        out.append("没有要注意的。")
    elif n <= MAX_LINES:
        out.append(f"要注意的共 {n} 条")
    else:
        out.append(f"要注意的共 {n} 条；最多列 {MAX_LINES} 条，其余到[研究档案](/issues)看")
    return "\n".join(out).strip() + "\n"
