"""Direction-first Linear-style issue workspace. User forms only; no AI mutation tools."""
import logging
import re
from datetime import date, datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from markdown_it import MarkdownIt
from markupsafe import Markup

from ...files import issues as I, tasks as T, datafiles, artifacts as A, dates as D
from ...files.weeks import week_of, week_add
from ...files.yamlio import StaleFileError, sha256_text
from ... import i18n
from ..context import get_conn, render, research_directions, templates

router = APIRouter()
log = logging.getLogger("research_lab.issues")
_md = MarkdownIt('commonmark', {'html': False}).enable('table')
# Lower layers raise English StaleFileErrors when a file changes mid-write; the page shows one message.
STALE = '议题已在别处更新，请刷新后再修改。'


def wants_json(request):
    """Inline edits (title, description, quick sub-issue) ask for JSON; plain forms keep the redirect."""
    return 'application/json' in request.headers.get('accept', '')


def failed(e):
    if isinstance(e, I.AttachmentLinkError):
        return JSONResponse({'error': str(e)}, status_code=409 if e.stale else 400)
    if isinstance(e, StaleFileError):
        return JSONResponse({'error': STALE}, status_code=409)
    return JSONResponse({'error': i18n.error_zh(str(e))}, status_code=400)


def parts(ref):
    """The files I.revision(ref) hashes, in its order."""
    doc = I._documents().get(ref)
    return (I._meta_sha(), T.file_sha(), datafiles.file_sha('projects'), doc.sha if doc else '')


def written(ref, before):
    """The revision after this request's own write, for the page's next save. One write changes one of
    the files; if another changed as well, someone else wrote in between, and the page gets the old
    revision so that its next save is refused instead of silently passing over that change. A second
    writer touching the same file in that window is not detected: that needs the sha the write returned,
    which files/issues.py does not hand back yet."""
    after = parts(ref)
    changed = sum(a != b for a, b in zip(before, after))
    return sha256_text(''.join(before if changed > 1 else after))


SUB_ORDER_COOKIE = 'rl_suborder'


def listed_under(page: str) -> set[str]:
    """Every issue a page's sub-issue list shows, at any depth: a 课题's members, another issue's descendants."""
    owner, rows = I.get(page), I.read()
    if owner is None:
        return set()
    if owner['project_root']:
        return {r['id'] for r in rows if r['project'] == owner['project']}
    return {r['id'] for r in I.descendants(page, rows)}


def children_of(issue, rows):
    """Direct sub-issues; a project's are its members whose parent lies outside the project."""
    if issue['project_root']:
        ids = {r['id'] for r in rows if r['project'] == issue['project']}
        return [r for r in rows if r['project'] == issue['project'] and r['parent'] not in ids and not r['archived']]
    return [r for r in rows if r['parent'] == issue['id']]


def progress(children):
    live = [r for r in children if r['status'] != 'parked']
    return sum(r['status'] == 'done' for r in live), len(live)


def common(direction='', project=''):
    data = {k: datafiles.read(k) or [] for k in ('projects', 'directions')}
    known = {d['slug'] for d in data['directions']}
    projects = {str(p['slug']): dict(p, archive_direction=p.get('direction') if p.get('direction') in known else '_none')
                for p in data['projects']}
    if project in projects:
        direction = projects[project]['archive_direction']
    directions = research_directions(data, direction)
    if direction and direction not in {d['slug'] for d in directions}:
        raise HTTPException(404, '没有这个研究方向。')
    # due_*: the due menu's presets and labels and the list colour (files/dates.py, ISSUES-LINEAR.md §4).
    return dict(issue_workspace=True, wide=True, directions=directions, nav_directions=directions, filter_direction=direction,
                due_presets=D.presets(), due_label=D.label, due_tone=D.tone,
                assignees=I.assignees(),
                active_direction=next((d for d in directions if d['active']), None),
                projects=projects, issue_project=project, issue_states=I.STATES, issue_priorities=I.PRIORITIES,
                issue_labels=I.LABELS, issue_types=i18n.ISSUE_TYPES, current_week=week_of(), next_week=week_add(week_of(), 1))


# 全部待办 (ISSUES-LINEAR.md §2.8): open issues across projects, by due date. Due today sits in 本周 and
# reads red there (files/dates.py tone); weeks run Monday to Sunday, as everywhere since the plan week went.
DUE_GROUPS = (('overdue', '已逾期'), ('week', '本周'), ('next', '下周'), ('later', '以后'), ('none', '无截止'))


def due_group(due, today):
    try:
        d = date.fromisoformat(str(due)) if due else None
    except ValueError:
        d = None
    if d is None:
        return 'none'
    sunday = today + timedelta(days=6 - today.weekday())
    if d < today:
        return 'overdue'
    return 'week' if d <= sunday else 'next' if d <= sunday + timedelta(days=7) else 'later'


def next_due(slug, rows, root):
    """A project's nearest unfinished deadline, its own included: the earliest due date among its open
    issues, overdue ones first by construction."""
    dated = [r for r in rows if r['project'] == slug and r['status'] in T.OPEN and r['due']]
    if root and root['status'] in T.OPEN and root['due']:
        dated.append(root)
    first = min(dated, key=lambda r: (r['due'], r['id']), default=None)
    return dict(next_due=first['due'], next_due_title=first['title']) if first else dict(next_due='', next_due_title='')


@router.get('/issues')
def issues_page(request: Request, conn=Depends(get_conn)):
    q = request.query_params
    direction, view = q.get('direction', ''), q.get('view', 'all')
    project = q.get('project', '')
    ctx = common(direction)
    if not project:
        I.fill_keys()                      # an issue that missed its number at creation gets it before it is listed
        version = I.revision()
        rows = [r for r in I.read() if not r['archived']]
        listed = {slug for slug, p in ctx['projects'].items()
                  if not direction or p['archive_direction'] == direction}
        tabs = dict(projects='/issues' + (f'?direction={direction}' if direction else ''),
                    todo='/issues?view=todo' + (f'&direction={direction}' if direction else ''))
        todo = sorted((r for r in rows if r['project'] in listed and r['status'] in T.OPEN),
                      key=lambda r: (r['due'] or '9999-99-99', r['status'] != 'doing', r['id']))
        if view == 'todo':
            today = datetime.now(D.BJ).date()
            def node(r):
                live = [c for c in rows if c['parent'] == r['id'] and c['status'] != 'parked']
                return dict(issue=r, children=[], context=False, total=len(live), delete_count=I.delete_count(r['id'], rows),
                            done=sum(c['status'] == 'done' for c in live))
            groups = [dict(key=k, title=t, nodes=[node(r) for r in todo if due_group(r['due'], today) == k])
                      for k, t in DUE_GROUPS]
            return render(request, conn, 'pages/issue_projects.html', title='全部待办', view='todo', tabs=tabs,
                          due_groups=[g for g in groups if g['nodes']], todo_total=len(todo),
                          project_total=len(listed), revision=version, **ctx)
        from ...dashboard import project_progress
        progress = project_progress(ctx['projects'].values(), ctx['current_week'], rows)
        roots = {r['project']: r for r in I.project_roots()}
        sections = []
        for d in ctx['directions']:
            if direction and d['slug'] != direction:
                continue
            projects = []
            for slug, p in ctx['projects'].items():
                if p['archive_direction'] != d['slug']:
                    continue
                projects.append(dict(progress[slug], **next_due(slug, rows, roots.get(slug)), key=roots.get(slug, {}).get('key', '')))
            sections.append(dict(direction=d, projects=projects))
        return render(request, conn, 'pages/issue_projects.html', title='研究档案', view='projects', tabs=tabs,
                      sections=sections, todo_total=len(todo), project_total=len(listed), **ctx)
    if project not in ctx['projects']:
        raise HTTPException(404, '没有这个课题。')
    selected = ctx['projects'][project]
    if direction and direction != selected['archive_direction']:
        raise HTTPException(404, '这个课题不属于所选研究方向。')
    return issue_detail(I.project_ref(project), request, conn)


@router.get('/issues/new')
def issue_new(request: Request, conn=Depends(get_conn)):
    direction, project, parent = (request.query_params.get(k, '') for k in ('direction', 'project', 'parent'))
    version = I.revision(parent)
    p = I.get(parent) if parent else None
    if parent and p is None:
        raise HTTPException(404, '没有这个父议题。')
    if p:
        direction, project = p['direction'], p['project']
    ctx = common(direction, project)
    direction = ctx['filter_direction']
    choices = {s: p for s, p in ctx['projects'].items() if not direction or p['archive_direction'] == direction}
    if project not in choices:
        project = next(iter(choices), '')
    template = 'partials/_issue_composer.html' if request.headers.get('X-Issue-Composer') else 'pages/issue.html'
    return render(request, conn, template, title='新建子议题' if parent else '新建议题',
                  creating=True, parent=p, revision=version, project_choices=choices,
                  selected_project=project, **ctx)


@router.post('/issues')
async def issue_create(request: Request, conn=Depends(get_conn)):
    form = await request.form()
    parent = str(form.get('parent') or '')
    quick = wants_json(request) and parent and not request.headers.get('X-Issue-Composer')
    before = parts(parent) if quick else None
    try:
        props = {k: str(form.get(k) or '') for k in ('description', 'status', 'week', 'due', 'priority', 'assignee') if k in form}
        if D.preset(props.get('due', '')):
            props['due'] = D.preset(props['due']).isoformat()   # the composer's 明天 / 周五…, worked out when sent
        if request.headers.get('X-Issue-Composer') or 'labels' in form:
            props['labels'] = form.getlist('labels')
        ref = I.create(str(form.get('title') or ''), str(form.get('project') or ''),
                       parent, str(form.get('revision') or ''), **props)
    except (I.IssueError, T.TaskError, datafiles.DataFileError, StaleFileError) as e:
        return failed(e)
    if props.get('assignee'):
        handover(conn, ref)              # created for an assistant: its plan step opens now (writes nothing yet)
    url = I.url(parent or ref)
    if request.headers.get('X-Issue-Composer'):
        return JSONResponse({'url': url, 'revision': I.revision(parent)})
    if quick:
        return JSONResponse(quick_row(ref, parent, written(parent, before)))
    return RedirectResponse(url, status_code=303)


def quick_row(ref, parent, revision):
    """The new sub-issue's row as the parent page renders it, and the parent's progress."""
    out = dict(revision=revision, id=ref, url=I.url(ref))
    try:
        rows = I.read()
        owner = I.get(parent)
        row = next(r for r in rows if r['id'] == ref)
        ctx = common(owner['direction'], owner['project'])
        html = templates().env.get_template('partials/_issue_rows.html').render(
            nodes=[dict(issue=row, children=[], context=False, total=0, done=0)], row_delete=True,
            task_revision=I.revision(), **ctx)
        done, total = progress(children_of(owner, rows))
        out.update(row=html, done=done, total=total)
    except Exception:
        # The sub-issue exists already; an error reply would make the page retry and add it twice.
        # Without a row the page reloads its list instead.
        log.exception('quick sub-issue %s was created but its row did not render', ref)
    return out


@router.get('/issues/due')
def due_parse(text: str = ''):
    """The due menu's live reading of what Leo types (files/dates.py parse): the date, or none when the
    rules cannot read it. Declared before /issues/{ref}."""
    d, note = D.read(text)
    # The note rides on the label (本周五已过，按下周五算), so the menu shows it without a script change.
    return JSONResponse({'date': d.isoformat(), 'label': D.label(d) + (f'，{note}' if note else ''), 'note': note}
                        if d else {'date': None, 'label': None, 'note': ''})


@router.get('/issues/{ref}')
def issue_page(ref: str, request: Request, conn=Depends(get_conn)):
    key = I.normal_key(ref)
    if key:
        # /issues/APUS-0002 (附：议题编号): the page itself; a key the issue had before it changed direction, or one
        # typed in another case or without padding, goes to the current key's address; a 课题's to its page.
        target = I.resolve(key)
        if not target:
            raise HTTPException(404, '没有这个编号的议题。')
        if target.startswith('PRJ-') or I.key_of(target) != ref:
            return RedirectResponse(I.key_url(target) if not target.startswith('PRJ-') else I.url(target), status_code=303)
        return issue_detail(target, request, conn)
    if ref.startswith('PRJ-'):
        if not I.get(ref):
            raise HTTPException(404, '没有这个课题。')
        return RedirectResponse(I.url(ref), status_code=303)
    return issue_detail(ref, request, conn)


def issue_detail(ref, request, conn):
    I.fill_keys()                          # the key file only: it is not part of the revision taken next
    # Capture before rendering any mutable fields: a concurrent edit must only produce a stale form.
    version = I.revision(ref)
    issue = I.get(ref)
    if issue is None:
        raise HTTPException(404, '没有这个议题。')
    rows = I.read()
    filters_ctx = {}
    children = children_of(issue, rows)
    if issue['project_root']:
        q = request.query_params
        archived = q.get('archive') == '1' or q.get('view') == 'archived'
        groups, count = I.board(view='all', group='none', archived=archived, project=issue['project'])
        nodes = [n for g in groups for n in g['items']]
        filters_ctx = dict(archived=archived)
    else:
        def node(r, trail):
            kids = [s for s in rows if s['parent'] == r['id'] and s['id'] not in trail]
            live_kids = [s for s in kids if s['status'] != 'parked']
            return dict(issue=r, children=[node(s, trail | {s['id']}) for s in kids], context=False,
                        total=len(live_kids), done=sum(s['status'] == 'done' for s in live_kids))
        nodes = [node(r, {ref, r['id']}) for r in children]
    def sort_due(items):
        """按截止 (no plan week since 0093): open before done or parked, then the due date, undated last;
        a parent counts its earliest open sub-issue's due date when that is sooner (derived here, never
        stored). Stable, so siblings stay together; no flattening across parent boundaries."""
        def key(n):
            dues = [n['issue']['due']] + [c['issue']['due'] for c in n['children'] if c['issue']['status'] in T.OPEN]
            return (n['issue']['status'] in ('done', 'parked'), min([d for d in dues if d] or ['9999-99-99']))
        items.sort(key=key)
        for n in items:
            sort_due(n['children'])
    # 手动 (card 0147): the order Leo dragged, which is tasks.yml's (outputs first, as read() lists them). A per-viewer
    # choice kept in a cookie, so the workspace's own refreshes draw the same order.
    manual = request.cookies.get(SUB_ORDER_COOKIE) == 'manual'
    if not manual:
        sort_due(nodes)
    done, total = progress(children)
    related_rows = rows + I.project_roots()
    # Related is bidirectional in the view; writes store only explicitly chosen relations.
    related = [r for r in related_rows if r['id'] != ref and (r['id'] in issue['related'] or ref in r['related'])]
    a = I._documents().get(ref) if issue['artifact'] else None
    from .artifacts import describe
    material = describe(A.editable_body(a.body))['desc_md'] if a else ''
    notes = A.ai_notes(a.body) if a else ''
    parent = next((r for r in rows if r['id'] == issue['parent']), None)
    if not parent and not issue['project_root']:
        parent = I.get(I.project_ref(issue['project']))
    can_sub = issue['project_root'] or issue['artifact'] or not (T.get(ref) or {}).get('parent')
    return render(request, conn, 'pages/issue.html', title=issue['title'], issue=issue, creating=False,
                  parent=parent, revision=version, nodes=nodes, can_sub=can_sub,
                  done=done, total=total,
                  related=related, related_choices=[r for r in related_rows if r['id'] != ref],
                  description_html=Markup(_md.render(issue['description'])),
                  material_html=Markup(_md.render(material)), notes_html=Markup(_md.render(notes)),
                  comments=I.comments(ref), attachments=I.attachments(ref), materials=_materials(conn, ref),
                  # card 0143: only a 待办-type issue can be deleted; a sub-issue row's Delete carries a task's revision
                  can_delete=not issue['project_root'] and not issue['artifact'], delete_count=I.delete_count(ref, rows),
                  task_revision=I.revision(), row_delete=True, manual_order=manual,
                  **filters_ctx, **common(issue['direction'], issue['project']))


@router.post('/issues/{ref}/property')
async def issue_property(ref: str, request: Request, conn=Depends(get_conn)):
    form = await request.form()
    field = str(form.get('field') or '')
    value = form.getlist('value') if field in ('labels', 'related') else str(form.get('value') or '')
    if field == 'description':
        value = value.replace('\r\n', '\n')  # browsers post textarea line breaks as CRLF
    elif field == 'title':
        # One line. Only ASCII runs collapse: full-width spaces and NBSP in a title are deliberate.
        value = re.sub(r'[ \t\r\n\f\v]+', ' ', value)
    if field == 'due' and D.preset(value):
        value = D.preset(value).isoformat()      # 明天, 周五…: worked out now, not when the page was drawn
    before = parts(ref) if wants_json(request) else None
    if field == 'type':
        return set_type(ref, value, str(form.get('revision') or ''), request, before)
    try:
        # actor='user': Leo's change also writes a grey line in the activity (card 0097). That is a second file
        # (issues.yml beside tasks.yml), so the page takes the revision update() built from its own writes.
        rev = I.update(ref, field, value, str(form.get('revision') or ''), actor='user')
    except (I.IssueError, T.TaskError, datafiles.DataFileError, StaleFileError) as e:
        return failed(e)
    if field == 'assignee':
        rev = handover(conn, ref, rev)
    if wants_json(request):
        # The stored value can differ from the typed one (trimmed, spaces collapsed); the page keeps it
        # as the field's base so the next refresh does not read the difference as someone else's edit.
        return JSONResponse({'revision': rev, 'value': I.get(ref)[field]})
    return RedirectResponse(I.url(ref), status_code=303)


def handover(conn, ref, rev=None):
    """交给助手 (card 0116): giving the issue to an assistant opens its plan step, taking it away stops it, at once.
    Stopping may write to the issue (the 开始 button comes off, 「已停下」). Those writes are this request's own, so
    the page gets the revision after them, unless someone else wrote first: then it keeps the old one and its next
    save is refused as stale. A failure here leaves the assignment saved; the next sweep retries it."""
    from ...agent import tasks as agent_tasks
    ours = rev is not None and I.revision(ref) == rev
    try:
        agent_tasks.sync_handover(conn, ref)
    except Exception:  # noqa: BLE001
        log.exception('hand-over for %s did not start', ref)
    return I.revision(ref) if ours else rev


def set_type(ref, value, revision, request, before):
    """类型 (ISSUES-LINEAR.md §5) belongs to the 课题: it is written to projects.yml, and sub-issues read it
    from there, so it is set on the 课题's page only. Checked against the page's revision like any field."""
    row = I.get(ref)
    try:
        if row is None:
            raise I.IssueError('找不到这个议题。')
        if not row['project_root']:
            raise I.IssueError('类型设在课题上，子议题跟着课题。')
        if value and value not in i18n.ISSUE_TYPES:
            raise I.IssueError('类型只能是论文、课题或手稿。')
        if revision != I.revision(ref):
            raise StaleFileError(STALE)
        datafiles.update_project(row['project'], {'type': value}, datafiles.file_sha('projects'))
    except (I.IssueError, datafiles.DataFileError, StaleFileError) as e:
        return failed(e)
    if wants_json(request):
        return JSONResponse({'revision': written(ref, before), 'value': value})
    return RedirectResponse(I.url(ref), status_code=303)


@router.post('/issues/{ref}/comments')
async def issue_comment(ref: str, request: Request):
    """A comment, optionally with one uploaded file (card 0176): the file lands in the 课题's drafts/ folder, hangs on
    the comment and joins the issue's attachments; `attach` fields name drafts/ or private/ files already there."""
    form = await request.form()
    try:
        revision, rels = form.get('revision'), [str(a) for a in form.getlist('attach') if str(a).strip()]
        up = form.get('file')
        if up is not None and getattr(up, 'filename', ''):
            before = {a['rel'] for a in I.attachments(ref)}
            revision = I.attach_upload(ref, up.filename, await up.read(), revision, folder_key='drafts')
            rels += sorted({a['rel'] for a in I.attachments(ref)} - before)
        text = str(form.get('text') or '').strip() or ('附件：' + '、'.join(r.split('/', 1)[-1] for r in rels) if rels else '')
        I.comment(ref, text, revision, attachments=rels)
    except (I.IssueError, StaleFileError) as e:
        return failed(e)
    return RedirectResponse(I.url(ref), status_code=303)


@router.post('/issues/{ref}/comments/{key}/edit')
async def issue_comment_edit(ref: str, key: str, request: Request):
    """Leo rewrites one of his comments (card 0097); the page redraws the thread and takes the revision."""
    form = await request.form()
    try:
        rev = I.edit_comment(ref, key, form.get('text'), str(form.get('revision') or ''))
    except (I.IssueError, StaleFileError) as e:
        return failed(e)
    return JSONResponse({'revision': rev}) if wants_json(request) else RedirectResponse(I.url(ref), status_code=303)


@router.post('/issues/{ref}/comments/{key}/delete')
async def issue_comment_delete(ref: str, key: str, request: Request):
    form = await request.form()
    try:
        rev = I.delete_comment(ref, key, str(form.get('revision') or ''))
    except (I.IssueError, StaleFileError) as e:
        return failed(e)
    return JSONResponse({'revision': rev}) if wants_json(request) else RedirectResponse(I.url(ref), status_code=303)


# ---------------------------------------------------------------- attachments and materials (card 0099)
# Writes answer like the property menus: a redirect to the issue, whose HTML the page swaps in (issues.js
# submitForm), or the JSON error the form shows.

def _done(ref, request):
    return RedirectResponse(I.url(ref), status_code=303)


@router.post('/issues/{ref}/delete')
async def issue_delete(ref: str, request: Request):
    """Leo's Delete (card 0143), from the issue page or row's context menu, after he confirmed. The page's
    revision is checked: an old page gets 409 and nothing is deleted. Answers JSON (where to go, how to undo) to
    the page's script, a redirect to the parent to a plain form."""
    form = await request.form()
    revision, page = str(form.get('revision') or ''), str(form.get('page') or '')
    try:
        if page and page != ref:
            # A sub-issue row's Delete sends the page it sits on and that page's live revision: the page is current,
            # so the row's issue is checked at its revision of now, once it is known to be listed on that page.
            meta_sha, task_sha, project_sha, _ = I._check(page, revision)
            if ref not in listed_under(page):
                raise I.IssueError('这条子议题不在这个页面上，请刷新后再删除。')
            revision = I._revision(meta_sha, task_sha, project_sha)
        out = I.delete(ref, revision)
    except (I.IssueError, T.TaskError, StaleFileError) as e:
        return failed(e)
    if wants_json(request):
        return JSONResponse({**out, 'undo': f'/issues/{ref}/restore'})
    return RedirectResponse(out['url'], status_code=303)


@router.post('/issues/{ref}/move')
async def issue_move(ref: str, request: Request):
    """Reorder one sub-issue among its siblings on the page it is listed on (card 0147): Leo's drag sends `before`
    (the sibling it now sits above; empty for last), Alt+↑/↓ sends step=up|down. The page's revision is checked (an
    old page gets 409, nothing moves) and both issues must be 待办-type siblings on that page. The order is
    tasks.yml's file order; keys are not touched."""
    form = await request.form()
    page, revision = str(form.get('page') or ''), str(form.get('revision') or '')
    try:
        _, task_sha, _, _ = I._check(page, revision)
    except StaleFileError as e:
        return failed(e)
    owner = I.get(page)
    if owner is None:
        return failed(I.IssueError('找不到这个议题。'))
    tasks = [str(t['id']) for t in T.read()]
    order = {tid: i for i, tid in enumerate(tasks)}
    moving = I.get(ref)
    visible = listed_under(page)
    siblings = [r['id'] for r in I.read() if moving and r['id'] in visible and r['id'] in order
                and r['parent'] == moving['parent'] and r['project'] == moving['project'] and not r['archived']]
    siblings.sort(key=order.get)
    before = str(form.get('before') or '')
    step = str(form.get('step') or '')
    if ref not in siblings or (before and before not in siblings):
        return failed(I.IssueError('只能在同一个议题的子议题之间调整顺序。'))
    try:
        if step in ('up', 'down'):
            i = siblings.index(ref)
            if step == 'up' and i > 0:
                T.move(ref, siblings[i - 1], task_sha)
            elif step == 'down' and i < len(siblings) - 1:
                T.move(siblings[i + 1], ref, task_sha)      # the next one comes up above it
        elif before != ref:
            rest = [x for x in siblings if x != ref]
            if before:
                target = before
            else:   # last among its siblings: just after the last one, not at the end of the whole file
                after = order[rest[-1]] if rest else order[ref]
                target = next((tid for tid in tasks[after + 1:] if tid != ref), None)
            T.move(ref, target, task_sha)
    except (T.TaskError, KeyError, StaleFileError) as e:
        return failed(e if not isinstance(e, KeyError) else I.IssueError('找不到这个议题。'))
    return JSONResponse({'revision': I.revision(page)})


@router.post('/issues/{ref}/restore')
async def issue_restore(ref: str, request: Request):
    """Undo of a delete: the issue and its sub-issues come back as they were."""
    form = await request.form()
    try:
        out = I.restore(ref, str(form.get('revision') or ''))
    except (I.IssueError, T.TaskError, StaleFileError) as e:
        return failed(e)
    if wants_json(request):
        return JSONResponse(out)
    return RedirectResponse(out['url'], status_code=303)


def _materials(conn, ref):
    """The strip's materials, each paper still in the library with its Replicate link (card 0178): the newest of its
    replication plans, else the new-plan page, which shows the paper and generates nothing until Leo asks there (only
    its POST queues). Datasets and papers since deleted get none."""
    items = I.materials(conn, ref)
    papers = [m['item'].removeprefix('paper:') for m in items if m['kind'] == 'paper' and m['url']]
    newest = {}
    if papers:
        rows = conn.execute(f"SELECT paper_id, id FROM replication_plans WHERE paper_id IN ({','.join('?' * len(papers))}) "
                            "ORDER BY created_at, id", papers)
        newest = {paper_id: plan_id for paper_id, plan_id in rows}      # ascending, so the last one per paper stays
    for m in items:
        if m['kind'] == 'paper' and m['url']:
            pid = m['item'].removeprefix('paper:')
            m['plan'] = newest.get(pid)
            m['replicate'] = ('/data/plans/' + quote(m['plan'], safe='') if m['plan']
                              else '/data/plans/new?paper=' + quote(pid, safe=''))
    return items


# Dropped files use the same human-only, version-checked mutations as the attachment strip.
OFFICE_TYPES = {'.docx', '.doc', '.pages', '.odt', '.rtf', '.xlsx', '.xls', '.pptx', '.key'}


def _drop_file(conn, ref, filename, data, revision, mode, paper_id, http):
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from ...files import deliverables
    from ...intake import pipeline

    I._check(ref, revision)
    issue = I.get(ref)
    if not issue:
        raise I.IssueError('找不到这个议题。')
    if mode not in ('auto', 'attach', 'import', 'link', 'separate'):
        raise I.IssueError('没有这个上传选项。')
    name = deliverables.clean_name(filename)
    suffix = Path(name).suffix.lower()
    if len(data) > I.ATTACH_MAX:
        raise I.IssueError('附件不能超过 200 MB。')
    if suffix != '.pdf' or mode == 'attach':
        if mode not in ('auto', 'attach'):
            raise I.IssueError('文献库只接收 PDF。')
        folder = 'drafts' if suffix in OFFICE_TYPES else 'private'
        revision = I.attach_upload(ref, name, data, revision, folder_key=folder)
        return {'state': 'done', 'revision': revision, 'note': '已存为产出' if folder == 'drafts' else '已附上'}
    if mode == 'link':
        revision = I.set_material(conn, ref, 'paper:' + paper_id, True, revision)
        return {'state': 'done', 'revision': revision, 'note': '已关联文献'}
    with TemporaryDirectory(prefix='rl-issue-drop-') as tmp:
        path = Path(tmp) / 'upload.pdf'
        path.write_bytes(data)
        try:
            existing = pipeline.find_existing(conn, path)
        except pipeline.RejectedFile as e:
            if mode in ('import', 'separate'):
                raise
            return {'state': 'choice', 'revision': revision, 'note': str(e), 'importable': False}
        if existing:
            if not existing['confirmed'] and mode not in ('import', 'separate'):
                title = conn.execute('SELECT title FROM papers WHERE id=?', (existing['paper_id'],)).fetchone()[0]
                return {'state': 'duplicate', 'revision': revision, 'paper_id': existing['paper_id'],
                        'note': 'DOI 线索待确认：' + title, 'import_action': 'import'}
            if existing['confirmed']:
                revision = I.set_material(conn, ref, 'paper:' + existing['paper_id'], True, revision)
                return {'state': 'done', 'revision': revision, 'note': '已在文献库，已关联'}
        if mode == 'auto':
            return {'state': 'choice', 'revision': revision, 'note': '尚未入库', 'importable': True}
        # Check again before the explicitly requested import can write to the library.
        I._check(ref, revision)
        result = pipeline.ingest(conn, path, project=issue.get('project'), direction=issue.get('direction'),
                                 original_name=name, http=http, hold_duplicate=False, allow_duplicate=mode == 'separate')
        if not result.paper_id and result.duplicate_of:
            title = conn.execute('SELECT title FROM papers WHERE id=?', (result.duplicate_of,)).fetchone()[0]
            return {'state': 'duplicate', 'revision': revision, 'paper_id': result.duplicate_of,
                    'note': '可能已入库：' + title, 'import_action': 'separate'}
        if not result.paper_id:
            raise I.IssueError('未能导入，请稍后再试。')
        try:
            revision = I.set_material(conn, ref, 'paper:' + result.paper_id, True, revision)
        except StaleFileError:
            # The chosen library import stays; a stale issue must not be overwritten.
            return {'state': 'conflict', 'error': '文献已导入；议题已更新，请刷新后关联。',
                    'paper_id': result.paper_id}
        return {'state': 'done', 'revision': revision, 'note': '已导入文献并关联'}


@router.post('/issues/{ref}/drop')
async def issue_drop(ref: str, request: Request, conn=Depends(get_conn)):
    from starlette.concurrency import run_in_threadpool
    from ...files.deliverables import DeliverableError
    from ...intake.pipeline import RejectedFile
    form = await request.form()
    upload = form.get('file')
    if upload is None or not hasattr(upload, 'read'):
        return failed(I.IssueError('先选一个文件。'))
    data = await upload.read(I.ATTACH_MAX + 1)
    try:
        out = await run_in_threadpool(_drop_file, conn, ref, upload.filename or '', data,
                                     str(form.get('revision') or ''), str(form.get('mode') or 'auto'),
                                     str(form.get('paper_id') or ''), getattr(request.app.state, 'intake_http', None))
    except (I.IssueError, StaleFileError, DeliverableError, RejectedFile) as e:
        return failed(e)
    if out['state'] == 'conflict':
        return JSONResponse(out, status_code=409)
    if out['state'] == 'done':
        out['materials'] = templates().env.get_template('partials/_issue_materials.html').render(
            issue=I.get(ref), revision=out['revision'], attachments=I.attachments(ref), materials=_materials(conn, ref))
    return JSONResponse(out)


@router.post('/issues/{ref}/attachments')
async def issue_attach(ref: str, request: Request):
    form = await request.form()
    upload = form.get('file')
    if upload is None or not hasattr(upload, 'read'):
        return failed(I.IssueError('先选一个文件。'))
    data = await upload.read(I.ATTACH_MAX + 1)          # one byte over the cap is enough to refuse
    try:
        I.attach_upload(ref, upload.filename or '', data, str(form.get('revision') or ''))
    except (I.IssueError, StaleFileError) as e:
        return failed(e)
    return _done(ref, request)


@router.post('/issues/{ref}/attachments/remove')
async def issue_detach(ref: str, request: Request):
    form = await request.form()
    try:
        I.detach(ref, str(form.get('rel') or ''), str(form.get('revision') or ''))
    except (I.IssueError, StaleFileError) as e:
        return failed(e)
    return _done(ref, request)


@router.post('/issues/{ref}/attachments/move')
async def issue_attach_move(ref: str, request: Request):
    form = await request.form()
    try:
        I.move_attachment(ref, str(form.get('rel') or ''), str(form.get('to') or ''), str(form.get('revision') or ''))
    except (I.IssueError, StaleFileError) as e:
        return failed(e)
    return _done(ref, request)


@router.post('/issues/{ref}/attachments/open')
async def issue_attach_open(ref: str, request: Request):
    """用系统程序打开 (executables are only revealed in Finder, files/deliverables.open_with_system). The file is never
    served to the page, so nothing uploaded can run in it."""
    from ...files import deliverables
    form = await request.form()
    try:
        path = I.attachment_path(ref, str(form.get('rel') or ''))
    except I.IssueError as e:
        return failed(e)
    deliverables.open_with_system(path)
    return JSONResponse({'ok': True})


@router.get('/issues/{ref}/materials/search')
def issue_material_search(ref: str, request: Request, q: str = '', conn=Depends(get_conn)):
    if not I.get(ref):
        raise HTTPException(404, '找不到这个议题。')
    return templates().TemplateResponse(request, 'partials/_issue_materials.html',
                                        {'request': request, 'picker': I.search_materials(conn, ref, q), 'issue': I.get(ref)})


@router.post('/issues/{ref}/materials')
async def issue_material(ref: str, request: Request, conn=Depends(get_conn)):
    form = await request.form()
    # The picker sends toggle=<item> (an item already linked is unlinked, 再按一次取消); a chip's × sends item + on=0.
    item = str(form.get('toggle') or form.get('item') or '')
    on = item not in [m['item'] for m in I.materials(conn, ref)] if form.get('toggle') else str(form.get('on') or '1') != '0'
    try:
        I.set_material(conn, ref, item, on, str(form.get('revision') or ''))
    except (I.IssueError, StaleFileError) as e:
        return failed(e)
    return _done(ref, request)


# ---------------------------------------------------------------- a paper's issues (card 0131, LIBRARY-UI.md §3.6, 卡 F)
# The reader's 议题 row and the library rows read issue links through the template global below; the toggle writes the
# issue's own materials list (I.set_material, version-checked). These routes live here because the link is the
# issue's: routes/papers.py never writes issues.
templates().env.globals['paper_issues'] = lambda paper_id, revisions=False: I.paper_issues(paper_id, with_revisions=revisions)
PAPER_ID_RE = re.compile(r'^[A-Za-z0-9_.:\-]{1,120}$')


def _paper_or_404(conn, paper_id):
    if not PAPER_ID_RE.match(paper_id) or not conn.execute('SELECT 1 FROM papers WHERE id = ?', (paper_id,)).fetchone():
        raise HTTPException(404, '文献库里没有这篇文献。')


@router.get('/papers/{paper_id}/issues/search')
def paper_issue_search(paper_id: str, request: Request, q: str = '', conn=Depends(get_conn)):
    _paper_or_404(conn, paper_id)
    return templates().TemplateResponse(request, 'partials/_paper_props.html', {
        'request': request, 'issue_picker': I.issues_for_paper(conn, paper_id, q), 'paper': {'id': paper_id}})


@router.post('/papers/{paper_id}/issues')
async def paper_issue_toggle(paper_id: str, request: Request, conn=Depends(get_conn)):
    """Link or unlink one issue, by Leo's click on the paper's page. The picker sends pick='<ref> <revision> <1|0>'
    (a linked issue offers 0); an Unlink sends ref, revision and on=0. Answers JSON to the page's script, a redirect
    back (`next`, a /papers/ path only) to a plain form."""
    _paper_or_404(conn, paper_id)
    form = await request.form()
    ref, revision, on = str(form.get('ref') or ''), str(form.get('revision') or ''), str(form.get('on') or '1')
    if form.get('pick'):
        ref, revision, on = (str(form.get('pick')).split() + ['', '', ''])[:3]
    target = I.resolve(ref)
    if not target:
        return failed(I.IssueError('找不到这个议题。'))
    try:
        rev = I.set_material(conn, target, f'paper:{paper_id}', on != '0', revision)
    except (I.IssueError, StaleFileError) as e:
        return failed(e)
    if wants_json(request):
        return JSONResponse({'revision': rev, 'linked': on != '0', 'issues': I.paper_issues(paper_id)})
    nxt = str(form.get('next') or '')
    return RedirectResponse(nxt if nxt.startswith('/papers/') and '//' not in nxt else f'/papers/{paper_id}', status_code=303)


# ---------------------------------------------------------------- Split with Theo (card 0052)
# The sub-issue block of an output: Theo reads it (ai/jobs/artifact_split.py) and suggests sub-issues as a checklist;
# only Add writes, and only what is ticked. The block is a fragment the page loads into itself (issues.js), so every
# answer here is that fragment; a plain form post without htmx goes back to the issue.

def _split(request, conn, ref, *, error='', flash='', posted=None):
    from ...ai.jobs import artifact_split as S
    from ...chat.cards import title_problem
    s = S.state(conn, ref)
    if posted:
        for r in s.get('rows', []):
            if r['id'] in posted['titles']:
                r['title'] = posted['titles'][r['id']]
            r['ticked'] = r['id'] in posted['ticked']
            r['problem'] = r['ticked'] and title_problem(r['title'])
    if request.headers.get('hx-request') != 'true' and request.method == 'POST':
        return RedirectResponse(I.url(ref), status_code=303)
    return render(request, conn, 'partials/_split_theo.html', ref=ref, s=s, error=error or s.get('error', ''), flash=flash)


@router.get('/issues/{ref}/split')
def issue_split(ref: str, request: Request, conn=Depends(get_conn)):
    return _split(request, conn, ref)


@router.post('/issues/{ref}/split')
def issue_split_start(ref: str, request: Request, conn=Depends(get_conn)):
    from ...ai.jobs import artifact_split as S
    try:
        S.start(conn, ref)
    except LookupError as e:
        return _split(request, conn, ref, error=str(e))
    return _split(request, conn, ref)


@router.post('/issues/{ref}/split/{batch_id}/add')
async def issue_split_add(ref: str, batch_id: str, request: Request, conn=Depends(get_conn)):
    from starlette.concurrency import run_in_threadpool
    from ...ai.jobs import artifact_split as S
    from ...chat.cards import title_problem
    form = await request.form()
    ticked = {int(v) for v in form.getlist('ids') if str(v).isascii() and str(v).isdecimal()}
    titles = {int(k[6:]): ' '.join(str(v).split()) for k, v in form.items()
              if k.startswith('title-') and k[6:].isascii() and k[6:].isdecimal()}
    if any(title_problem(t) for rid, t in titles.items() if rid in ticked):
        return _split(request, conn, ref, error='有子议题的标题需要改一下，改好再加入。',
                      posted={'ticked': ticked, 'titles': titles})
    out = await run_in_threadpool(S.add, conn, ref, batch_id, sorted(ticked), titles)
    made = [r for r in out.get('applied', []) if r.get('action') == 'create']
    resp = _split(request, conn, ref, flash=f'已加入 {len(made)} 条子议题。' if made else
                  ('这批建议已经处理过了。' if out.get('already') else '没有勾选，什么都没写入。'))
    if made and request.headers.get('hx-request') == 'true':
        resp.headers['HX-Refresh'] = 'true'          # the new rows are drawn by the page itself
    return resp


@router.post('/issues/{ref}/split/{batch_id}/dismiss')
def issue_split_dismiss(ref: str, batch_id: str, request: Request, conn=Depends(get_conn)):
    from ...ai.jobs import artifact_split as S
    S.dismiss(conn, ref, batch_id)
    return _split(request, conn, ref)
