"""files/dates.py (ISSUES-LINEAR.md §4): presets, the Fridays, labels, list colours and the rule parser.
Saturday 10 October 2026 is the day of Leo's Linear screenshot: its 本周末 was Friday the 16th."""
from datetime import date

import pytest

from research_lab.files import dates as D

SAT = date(2026, 10, 10)
MON = date(2026, 10, 12)
FRI = date(2026, 10, 16)


def test_fridays_and_presets():
    assert D.upcoming_friday(SAT) == FRI and D.upcoming_friday(FRI) == FRI and D.upcoming_friday(MON) == FRI
    assert D.this_friday(MON) == FRI and D.this_friday(SAT) == SAT          # past Friday: today
    assert D.next_friday(SAT) == FRI and D.next_friday(MON) == date(2026, 10, 23)
    assert [(p.key, p.name, p.iso) for p in D.presets(SAT)] == [
        ("tomorrow", "明天", "2026-10-11"), ("friday", "周五", "2026-10-16"),
        ("week", "一周后", "2026-10-17"), ("month", "一个月后", "2026-11-10")]
    assert D.in_months(date(2027, 1, 31), 1) == date(2027, 2, 28) and D.in_months(date(2026, 11, 30), 2) == date(2027, 1, 30)


def test_label_and_tone():
    assert D.label(date(2026, 10, 20), SAT) == "10月20日 周二"
    assert D.label(date(2027, 1, 5), SAT) == "2027年1月5日 周二"
    assert D.tone("2026-10-10", today=SAT) == "overdue" and D.tone("2026-10-01", today=SAT) == "overdue"
    assert D.tone("2026-10-17", today=SAT) == "soon" and D.tone("2026-10-18", today=SAT) == ""
    assert D.tone("2026-10-01", "done", SAT) == "" and D.tone("", today=SAT) == "" and D.tone("bad", today=SAT) == ""


@pytest.mark.parametrize("text,expected", [
    ("今天", "2026-10-10"), ("明天", "2026-10-11"), ("后天", "2026-10-12"), ("tomorrow", "2026-10-11"),
    ("周三", "2026-10-14"), ("星期六", "2026-10-10"), ("周日", "2026-10-11"), ("下周三", "2026-10-14"),
    ("本周五", "2026-10-16"), ("本周日", "2026-10-11"), ("这周末", "2026-10-11"), ("下周末", "2026-10-18"), ("下下周一", "2026-10-19"),
    ("下周三前", "2026-10-14"), ("wed", "2026-10-14"), ("next friday", "2026-10-16"),
    ("3天", "2026-10-13"), ("3天后", "2026-10-13"), ("三天", "2026-10-13"), ("十天后", "2026-10-20"),
    ("2周", "2026-10-24"), ("两周", "2026-10-24"), ("1个月", "2026-11-10"), ("一个月", "2026-11-10"),
    ("半个月", "2026-10-25"), ("24h", "2026-10-11"), ("48h", "2026-10-12"), ("7d", "2026-10-17"),
    ("2w", "2026-10-24"), ("3 days", "2026-10-13"), ("1 week", "2026-10-17"),
    ("10月20日", "2026-10-20"), ("10月20号", "2026-10-20"), ("10月20", "2026-10-20"), ("10-20", "2026-10-20"),
    ("10/20", "2026-10-20"), ("2026-10-20", "2026-10-20"), ("2026年11月3日", "2026-11-03"),
    ("1月5日", "2027-01-05"), ("10月10日", "2026-10-10"), ("Oct 20", "2026-10-20"), ("20 October", "2026-10-20"),
    ("月底", "2026-10-31"), ("下月底", "2026-11-30"), ("下月初", "2026-11-01"), ("月初", "2026-11-01"),
    ("下个月", "2026-11-10"), ("下周", "2026-10-16"), ("截止 10月20日", "2026-10-20"),
])
def test_parse(text, expected):
    assert D.parse(text, SAT) == date.fromisoformat(expected)


@pytest.mark.parametrize("text", ["", "随便", "下周三下午", "3天前", "2月30日", "2026-02-30", "0天", "13/40", "周八", "Foo 20"])
def test_parse_refuses_what_it_cannot_read(text):
    assert D.parse(text, SAT) is None


def test_due_menu_page_and_reading_route(client, repo):
    from datetime import timedelta
    from research_lab.files import issues as I
    ref = I.create('截止菜单验收', 'us-rok-reassurance', '', I.revision())
    page = client.get(f'/issues/{ref}').text
    menu = page.split('<aside class="issue-properties"', 1)[1].split('data-prop="direction"', 1)[0]
    assert 'placeholder="试试：明天、3天、下周三、10月20日"' in menu and '<kbd>⇧D</kbd>' in menu and 'Custom…' in menu
    for p in D.presets():
        assert f'value="{p.key}"' in menu and D.label(p.date) in menu      # the key goes up; the date is shown
    assert 'Clear due date' not in menu and 'type="date"' not in menu               # no native picker, nothing to clear yet
    got = client.get('/issues/due', params={'text': '明天'}).json()
    tomorrow = date.today() + timedelta(days=1)
    assert got == {'date': tomorrow.isoformat(), 'label': D.label(tomorrow), 'note': ''}
    assert client.get('/issues/due', params={'text': '随便'}).json() == {'date': None, 'label': None, 'note': ''}
    I.update(ref, 'due', date.today().isoformat(), I.revision(ref))
    page = client.get(f'/issues/{ref}').text
    assert 'Clear due date' in page and 'class="due-overdue"' in page                 # due today reads red


def test_sub_issue_rows_are_coloured_by_due_date(client, repo):
    from datetime import timedelta
    from research_lab.files import issues as I
    parent = I.create('父议题', 'us-rok-reassurance', '', I.revision())
    for title, days, status in (('今天到期', 0, 'todo'), ('五天后', 5, 'todo'), ('很久以后', 30, 'todo'), ('做完了', -3, 'done')):
        ref = I.create(title, 'us-rok-reassurance', parent, I.revision(parent))
        I.update(ref, 'due', (date.today() + timedelta(days=days)).isoformat(), I.revision(ref))
        if status != 'todo':
            I.update(ref, 'status', status, I.revision(ref))
    rows = client.get(f'/issues/{parent}').text.split('class="issue-subtree"', 1)[1]
    def row(title):
        return rows.split(title, 1)[1].split('</div>\n  </div>', 1)[0]
    assert 'issue-row-date due-overdue' in row('今天到期') and 'issue-row-date due-soon' in row('五天后')
    assert 'due-' not in row('很久以后') and 'due-' not in row('做完了')


def test_a_passed_day_this_week_reads_as_next_weeks_with_a_note():
    """Review of 0094: 本周五 said on a Sunday is next Friday, and the menu says so."""
    sun = date(2026, 10, 11)
    assert D.read("本周五", sun) == (date(2026, 10, 16), "本周五已过，按下周五算")
    assert D.read("这周三前", sun) == (date(2026, 10, 14), "本周三已过，按下周三算")
    assert D.read("本周日", sun) == (sun, "") and D.read("下周五", sun) == (date(2026, 10, 16), "")
    assert D.read("随便", sun) == (None, "")
    # 0100 review: lead-in words and punctuation must not hide a passed day (Theo has no preview)
    for said in ("到本周五", "在本周五", "本周五。", "挪到本周五", "改到 本周五", "截止本周五前"):
        assert D.read(said, sun) == (date(2026, 10, 16), "本周五已过，按下周五算"), said
        assert D.parse(said, sun) == date(2026, 10, 16), said


def test_today_is_beijing_time_and_presets_resolve_when_pressed(client, repo):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from research_lab.files import issues as I
    bj = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    assert D._today(None) == bj and D.preset("tomorrow") == bj + timedelta(days=1) and D.preset("nope") is None
    ref = I.create('预设验收', 'us-rok-reassurance', '', I.revision())
    r = client.post(f'/issues/{ref}/property', data={'field': 'due', 'value': 'week', 'revision': I.revision(ref)},
                    headers={'Accept': 'application/json'})
    assert r.status_code == 200 and I.get(ref)['due'] == (bj + timedelta(days=7)).isoformat()
