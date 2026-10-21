"""Card 0113: the 首页 agenda colours due dates with files/dates.py tone, as /tasks and the issue page do.
An overdue output lives only in issues.yml, never in tasks_overdue, and still reads red."""
from __future__ import annotations

from datetime import date

from research_lab.files import tasks as T
from research_lab.files.weeks import week_of
from .test_pm_home import render_home

MONDAY = date(2026, 9, 28)       # 2026-W40
WEDNESDAY = date(2026, 9, 30)
W40 = week_of(MONDAY)


def deadline(html: str, iso: str) -> str:
    """The <time> tag of the first agenda row due on `iso`."""
    return '<time' + html.split(f'<time datetime="{iso}"', 1)[1].split('>', 1)[0]


def test_an_overdue_output_reads_red_on_the_home(conn, repo):
    from research_lab.files import artifacts as A
    from research_lab.files import issues as I
    ref = A.load(A.create('evidence', 'overdue-output', 'us-iran-talks', title='逾期的产出', week=W40)).ref
    I.update(ref, 'due', MONDAY.isoformat(), I.revision(ref))
    assert ref not in {t['id'] for t in T.read()}           # not a tasks.yml row, so not in tasks_overdue
    html = render_home(conn, WEDNESDAY)
    assert f'href="/issues/{ref}"' in html
    assert 'class="due-overdue"' in deadline(html, MONDAY.isoformat())


def test_home_due_colours_follow_dates_tone(conn, repo):
    from research_lab.files import issues as I
    T.add({'title': '今天到期的核对', 'project': 'us-rok-reassurance', 'due': '2026-09-30'}, today=WEDNESDAY)
    T.add({'title': '周六到期的整理', 'project': 'us-rok-reassurance', 'due': '2026-10-03'}, today=WEDNESDAY)
    done, _ = T.add({'title': '已完成的检索', 'project': 'us-rok-reassurance', 'due': '2026-10-02'}, today=WEDNESDAY)
    T.set_status(done, 'done', today=WEDNESDAY)
    root = I.project_ref('us-rok-reassurance')
    I.update(root, 'due', '2026-10-06', I.revision(root))   # a 课题's own date, six days off
    html = render_home(conn, WEDNESDAY)
    assert 'class="due-overdue"' in deadline(html, '2026-09-30')
    assert 'class="due-soon"' in deadline(html, '2026-10-03')
    assert 'class=' not in deadline(html, '2026-10-02')      # done reads grey
    summary = html.split('data-agenda-key="project-us-rok-reassurance"', 1)[1].split('</summary>', 1)[0]
    assert 'datetime="2026-10-06" title="截止日期" class="due-soon"' in summary
