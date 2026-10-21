"""Card 0115 (ISSUES-LINEAR.md §2.8): 研究档案 lists one compact row per 课题 with its type, progress, nearest
unfinished due date (coloured by files/dates.py tone) and stage; 全部待办 lists open issues across projects
grouped by due date: 已逾期, 本周, 下周, 以后, 无截止."""
from datetime import date, datetime, timedelta

from research_lab.files import datafiles, dates as D, issues as I
from research_lab.web.routes.issues import due_group

SLUG = 'us-rok-reassurance'


def new(title, project=SLUG, due='', status=''):
    ref = I.create(title, project, '', I.revision())
    if due:
        I.update(ref, 'due', due, I.revision(ref))
    if status:
        I.update(ref, 'status', status, I.revision(ref))
    return ref


def row(html, slug):
    return html.split(f'<a class="project-row" href="/issues?project={slug}"', 1)[1].split('</a>', 1)[0]


def test_project_rows_show_type_progress_nearest_open_due_and_stage(client, repo):
    datafiles.update_project(SLUG, {'type': '论文'})
    new('早已完成的检索', due='2019-01-01', status='done')       # done: never the nearest deadline
    new('逾期的核对', due='2020-01-02')
    html = client.get('/issues').text
    assert 'aria-current="page">课题' in html and 'href="/issues?view=todo"' in html
    r = row(html, SLUG)
    assert '类型：论文' in r and 'issue-progress-ring' in r and '已投稿' in r
    assert 'class="project-row-due due-overdue" title="最近截止：逾期的核对"' in r and '1月2日' in r
    assert 'project-progress-card' not in html                     # the home keeps the cards


def test_a_project_without_dated_open_work_leaves_the_due_cell_empty(client, repo):
    for ref in [r['id'] for r in I.read() if r['project'] == 'us-iran-talks' and r['due']]:
        I.update(ref, 'due', '', I.revision(ref))
    root = I.project_ref('us-iran-talks')
    I.update(root, 'due', '', I.revision(root))
    assert 'project-row-due' not in row(client.get('/issues').text, 'us-iran-talks')
    I.update(root, 'due', '2020-03-04', I.revision(root))           # the 课题's own date counts
    assert 'title="最近截止：' in row(client.get('/issues').text, 'us-iran-talks')


def test_all_open_issues_group_by_due(client, repo):
    today = datetime.now(D.BJ).date()
    monday = today - timedelta(days=today.weekday())
    refs = {
        'overdue': new('上周就该交的', due=(today - timedelta(days=1)).isoformat()),
        'week': new('今天到期的', due=today.isoformat()),
        'next': new('下周三到期的', due=(monday + timedelta(days=9)).isoformat()),
        'later': new('一个月后的', due=(today + timedelta(days=40)).isoformat()),
        'none': new('还没定截止的'),
    }
    done = new('已经完成的', due=today.isoformat(), status='done')
    other = new('别的方向的议题', project='us-iran-talks', due=today.isoformat())
    html = client.get('/issues?view=todo').text
    assert 'aria-current="page">全部待办' in html
    keys = ['overdue', 'week', 'next', 'later', 'none']
    order = [html.index(f'data-due-group="{k}"') for k in keys]
    assert order == sorted(order)
    sections = {k: html.split(f'data-due-group="{k}"', 1)[1].split('</section>', 1)[0] for k in keys}
    for key, ref in refs.items():
        assert f'href="/issues/{ref}"' in sections[key], key
        assert all(f'href="/issues/{ref}"' not in s for k, s in sections.items() if k != key)
    assert f'href="/issues/{done}"' not in html
    today_row = sections['week'].split(f'href="/issues/{refs["week"]}"', 1)[1].split('issue-tree-item', 1)[0]
    assert 'issue-row-date due-overdue' in today_row                # due today reads red (dates.tone)
    assert 'issue-project-tag' in today_row
    filtered = client.get('/issues?view=todo&direction=alliance-us').text
    assert f'href="/issues/{refs["week"]}"' in filtered and f'href="/issues/{other}"' not in filtered
    assert 'href="/issues?direction=alliance-us"' in filtered     # the 课题 tab keeps the direction


def test_due_group_weeks_run_monday_to_sunday():
    sunday = date(2026, 10, 11)
    assert due_group('2026-10-10', sunday) == 'overdue'
    assert due_group('2026-10-11', sunday) == 'week'
    assert due_group('2026-10-12', sunday) == 'next'
    assert due_group('2026-10-18', sunday) == 'next'
    assert due_group('2026-10-19', sunday) == 'later'
    assert due_group('', sunday) == due_group('not-a-date', sunday) == 'none'
    monday = date(2026, 10, 12)
    assert due_group('2026-10-18', monday) == 'week' and due_group('2026-10-25', monday) == 'next'
