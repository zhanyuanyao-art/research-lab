"""Card 0005: 待办 on the 首页 本周 card — dashboard fields and plan state, the plan form (one POST decides
every overdue task and writes now.this_week), the week checklist tick, and the actions. Since 0093 本周 is
due by Sunday (overdue included) plus finished since Monday; a `week` given here becomes that Friday."""
from __future__ import annotations

import re
from datetime import date

from research_lab import dashboard
from research_lab.files import datafiles
from research_lab.files import tasks as T
from research_lab.files.weeks import week_add, week_of

MONDAY = date(2026, 9, 28)       # 2026-W40
WEDNESDAY = date(2026, 9, 30)
W40, W39 = week_of(MONDAY), week_add(week_of(MONDAY), -1)


def visible(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def _redirect(r) -> str:
    assert r.status_code in (204, 303), r.status_code
    return r.headers.get("location") or r.headers.get("HX-Redirect")


def render_home(conn, today: date) -> str:
    """The 首页 as rendered for a given date (the route only knows date.today())."""
    from starlette.requests import Request
    from research_lab.web.context import base_context, templates
    scope = {"type": "http", "method": "GET", "path": "/", "raw_path": b"/", "query_string": b"", "scheme": "http",
             "server": ("127.0.0.1", 4100), "root_path": "", "headers": [(b"host", b"127.0.0.1:4100")]}
    req = Request(scope)
    ctx = base_context(req, conn)
    ctx.update(dashboard.build(conn, today=today), title="首页", planned=False, plan_error="")
    return templates().env.get_template("pages/today.html").render(ctx)


# ---------------------------------------------------------------- dashboard fields

def test_plan_view_states_and_actions(conn, repo):
    v = dashboard.plan_view(W40, MONDAY)
    assert v["state"] == "planning" and v["week"] == [] and v["overdue"] == [] and len(v["now_sha"]) == 64
    assert any(a["kind"] == "plan" for a in dashboard.build(conn, today=MONDAY)["actions"])

    T.add({"title": "上周没做完的", "project": "asean-centrality", "week": W39}, today=MONDAY)
    for i in range(6):
        T.add({"title": f"本周第 {i + 1} 件", "project": "asean-centrality", "week": W40}, today=MONDAY)
    done_id, _ = T.add({"title": "已经做完的", "project": "asean-centrality", "week": W40}, today=MONDAY)
    T.set_status(done_id, "done", today=MONDAY)
    T.add({"title": "下周的", "project": "asean-centrality", "week": week_add(W40, 1)}, today=MONDAY)

    v = dashboard.plan_view(W40, MONDAY)
    assert v["state"] == "planning"                                   # something is overdue: the card asks about it
    assert [t["title"] for t in v["week"]] == [f"本周第 {i + 1} 件" for i in range(6)]
    assert [t["title"] for t in v["done"]] == ["已经做完的"] and [t["title"] for t in v["overdue"]] == ["上周没做完的"]
    assert v["open_total"] == 8 and (v["this_friday"], v["next_friday"]) == ("2026-10-02", "2026-10-09")

    mon = dashboard.build(conn, today=MONDAY)
    assert mon["plan_state"] == "planning" and len(mon["tasks_week"]) == 6 and len(mon["tasks_overdue"]) == 1
    assert [a["text"] for a in mon["actions"] if a["kind"] == "plan"] == ["有 1 项待办已过截止，先定下怎么处理。"]
    T.set_status(T.read()[0]["id"], "parked")
    assert dashboard.plan_view(W40, MONDAY)["state"] == "running"    # nothing overdue, something due: no form
    wed = dashboard.build(conn, today=WEDNESDAY)
    assert not any(a["kind"] in ("plan", "task") for a in wed["actions"]) and len(wed["actions"]) <= 3


# ---------------------------------------------------------------- rendering

def test_monday_plan_form_and_running_checklist(conn, repo):
    html = render_home(conn, MONDAY)
    assert 'id="home-tasks"' in html and 'action="/plan"' in html and "Plan the week" in html and 'name="this_week"' in html
    assert f'value="{datafiles.file_sha("now")}"' in html and "已过截止" not in html

    T.add({"title": "读完 Ba 第三章", "project": "asean-centrality", "week": W39}, today=MONDAY)
    html = render_home(conn, MONDAY)
    tid = T.read()[0]["id"]
    assert "过期的怎么处理" in html and "1 项待办已过截止" in html and f'name="decision-{tid}"' in html
    assert html.count(f'name="decision-{tid}"') == 4 and "本周五" in html and "下周五" in html and "搁置" in html and "已完成" in html
    assert "T-2026" not in visible(html)

    T.carry(tid, W40)
    html = render_home(conn, MONDAY)
    assert 'action="/plan"' not in html and f'action="/plan/task/{tid}"' in html and "读完 Ba 第三章" in html
    assert "全部课题" in html and 'title="截止推迟过 1 次"' in html
    html = render_home(conn, WEDNESDAY)
    assert 'action="/plan"' not in html                               # from Wednesday an unplanned week is only a line
    for t in T.read():
        T.set_status(t["id"], "parked")
    html = render_home(conn, WEDNESDAY)
    assert f'action="/plan/task/{tid}"' not in html and 'action="/plan"' not in html


# ---------------------------------------------------------------- routes

def test_plan_post_decides_leftovers_and_writes_now(client, repo):
    last = week_add(week_of(), -1)
    a, _ = T.add({"title": "移过来", "project": "asean-centrality", "week": last})
    b, _ = T.add({"title": "放一放", "project": "asean-centrality", "week": last})
    c, _ = T.add({"title": "其实做完了", "project": "asean-centrality", "week": last})
    sha = datafiles.file_sha("now")
    d, _ = T.add({"title": "下周五再交", "project": "asean-centrality", "week": last})
    r = client.post("/plan", data={f"decision-{a}": "friday", f"decision-{b}": "park", f"decision-{c}": "done",
                                   f"decision-{d}": "next_friday", "this_week": "写完研究版图第二节", "sha": sha})
    assert _redirect(r) == "/?planned=1#home-tasks"
    assert T.get(a)["due"] == T.this_friday() and T.get(a)["status"] == "todo" and T.get(a)["postponed"] == 1
    assert T.get(d)["due"] == T.next_friday()
    assert T.get(b)["status"] == "parked" and T.get(c)["status"] == "done"
    assert (datafiles.read("now") or {}).get("this_week") == "写完研究版图第二节"
    page = client.get("/?planned=1").text
    assert "本周计划已定" in page and "移过来" in page and "放一放" not in page
    stale = client.post("/plan", data={"this_week": "another", "sha": sha})
    assert _redirect(stale) == "/?plan_error=stale#home-tasks"
    assert (datafiles.read("now") or {}).get("this_week") == "写完研究版图第二节"
    assert "没有写入" in client.get("/?plan_error=stale").text
    bogus = client.post("/plan", data={"decision-T-2026-0999": "done", "this_week": "写完研究版图第二节", "sha": datafiles.file_sha("now")})
    assert _redirect(bogus) == "/?planned=1#home-tasks"           # an unknown task is skipped, not a 500


def test_checklist_tick_and_undo(client, repo):
    tid, _ = T.add({"title": "勾掉我", "project": "asean-centrality", "due": date.today().isoformat()})
    page = client.get("/").text
    assert "勾掉我" in page and f'action="/plan/task/{tid}"' in page and "T-2026" not in visible(page)
    assert _redirect(client.post(f"/plan/task/{tid}", data={"status": "done"})) == "/#home-tasks"
    assert T.get(tid)["status"] == "done"
    page = client.get("/").text
    assert "<s>勾掉我</s>" in page and "撤销完成" in page
    assert _redirect(client.post(f"/plan/task/{tid}", data={"status": "todo"})) == "/#home-tasks"
    assert T.get(tid)["status"] == "todo" and T.get(tid)["done"] is None
    hx = client.post(f"/plan/task/{tid}", data={"status": "done"}, headers={"HX-Target": "home-tasks"}, follow_redirects=False)
    assert hx.status_code == 303                                      # htmx follows it and hx-selects #home-tasks


def test_plan_view_shows_a_parent_as_one_line_with_its_sections_due_now(repo):
    """TASKS.md §C: a chapter is one line with its progress sentence; its sections planned this week or
    due within 7 days sit under it; neither the parent nor its sections repeat in the flat lists."""
    p = "asean-centrality"
    ch, _ = T.add({"title": "第二章", "project": p, "due": "2026-10-20", "words": "20000", "file": "第二章.docx"}, today=MONDAY)
    a, _ = T.add({"title": "一节", "project": p, "parent": ch, "week": W40, "due": "2026-10-02"}, today=MONDAY)
    b, _ = T.add({"title": "二节", "project": p, "parent": ch, "week": week_add(W40, 2), "due": "2026-10-03"}, today=MONDAY)
    c, _ = T.add({"title": "三节", "project": p, "parent": ch, "week": week_add(W40, 3), "due": "2026-10-20"}, today=MONDAY)
    plain, _ = T.add({"title": "读 Goh", "project": p, "week": W40}, today=MONDAY)
    old, _ = T.add({"title": "上周剩的", "project": p, "week": W39}, today=MONDAY)
    plan = dashboard.plan_view(W40, WEDNESDAY)
    [par] = plan["parents"]
    assert par["task"]["id"] == ch and [t["id"] for t in par["subs_this_week"]] == [a, b]     # c is weeks away
    assert par["sentence"] == "第二章 · 距截止 20 天 · 0/3 节 · 找不到 Word 文件"
    assert [t["id"] for t in plan["flat_week"]] == [plain] and [t["id"] for t in plan["flat_overdue"]] == [old]
    assert {t["id"] for t in plan["week"]} == {a, b, plain}                                    # due by Sunday, not overdue
    T.set_status(ch, "done", today=WEDNESDAY)                                                    # done parent: folded, gone
    plan = dashboard.plan_view(W40, WEDNESDAY)
    assert plan["parents"] == [] and [t["id"] for t in plan["flat_week"]] == [plain]
    from research_lab.db.connection import connect, migrate
    conn = connect()
    migrate(conn)
    ctx = dashboard.build(conn, today=WEDNESDAY)                                                 # what the home template gets
    conn.close()
    assert ctx["tasks_parents"] == [] and [t["id"] for t in ctx["tasks_flat_week"]] == [plain]



def test_home_groups_leaf_tasks_and_keeps_closed_parent_context(conn, repo):
    """Home shows at most five actionable rows total; closed parents cannot hide open children."""
    p = "asean-centrality"
    ch, _ = T.add({"title": "第二章 周边环境的变化", "project": p, "due": "2026-10-20", "file": "第二章.docx"}, today=MONDAY)
    a, _ = T.add({"title": "地区秩序变化", "project": p, "parent": ch, "week": W40, "due": "2026-10-02"}, today=MONDAY)
    T.add({"title": "三节", "project": p, "parent": ch, "week": week_add(W40, 3)}, today=MONDAY)
    for i in range(6):
        T.add({"title": f"普通 {i}", "project": p, "week": W40}, today=MONDAY)
    html = render_home(conn, WEDNESDAY)
    assert f'href="/issues/{a}"' in html and "第二章 周边环境的变化" in visible(html)
    assert "找不到 Word 文件" not in html and "在 Word 中打开" not in html
    assert f'action="/plan/task/{a}"' in html and f'action="/plan/task/{ch}"' not in html
    assert "地区秩序变化" in html and "三节" not in html
    assert len(re.findall(r'action="/plan/task/', html)) == 7
    assert html.count('data-agenda-key="project-asean-centrality"') == 1 and 'class="agenda-parent" data-agenda-key="project-asean-centrality" open' not in html
    assert 'href="/issues?project=asean-centrality"' in html
    assert "T-2026" not in visible(html)
    T.update(ch, {"status": "done"}, cascade_status=False)
    html = render_home(conn, WEDNESDAY)
    assert f'action="/plan/task/{a}"' in html and "地区秩序变化" in html


def test_home_direction_project_groups_do_not_duplicate_or_leak_weeks(repo):
    a, _ = T.add({"title": "联盟任务", "project": "us-rok-reassurance", "week": W40}, today=MONDAY)
    b, _ = T.add({"title": "信号任务", "project": "us-iran-talks", "week": W40}, today=MONDAY)
    T.add({"title": "下周再做", "project": "us-rok-reassurance", "week": week_add(W40, 1)}, today=MONDAY)
    T.add({"title": "上周未完", "project": "us-iran-talks", "week": W39}, today=MONDAY)
    T.set_status(b, "done", today=WEDNESDAY)
    v = dashboard.weekly_agenda(W40, today=WEDNESDAY)
    assert v['pending'] == 2 and v['done'] == 1                     # 上周未完 is overdue, so it is 本周; 下周再做 is not
    assert [g['slug'] for g in v['groups']] == ['alliance-us', 'strategy-signaling']
    assert [g['projects'][0]['slug'] for g in v['groups']] == ['us-rok-reassurance', 'us-iran-talks']
    assert v['groups'][0]['projects'][0]['pending'][0]['id'] == a
    assert v['groups'][1]['projects'][0]['done'][0]['id'] == b


def test_parent_ready_to_review_stays_visible_without_cascading_control(conn, repo):
    p, _ = T.add({'title': '整体验收论文', 'project': 'asean-centrality', 'week': W40}, today=MONDAY)
    c, _ = T.add({'title': '核对证据', 'project': 'asean-centrality', 'parent': p, 'week': W40}, today=MONDAY)
    T.set_status(c, 'done')
    v = dashboard.weekly_agenda(W40)
    assert v['pending'] == 1
    assert v['groups'][0]['projects'][0]['pending'][0]['id'] == p
    html = render_home(conn, WEDNESDAY)
    assert f'href="/issues/{p}"' in html and f'action="/plan/task/{p}"' not in html
    assert '整体验收论文' in html


def test_home_keeps_actionable_artifact_children_without_repeating_parent_copy(conn, repo):
    from research_lab.files import artifacts as A
    from research_lab.files import issues as I
    path = A.create('evidence', '既有产出父议题', 'us-rok-reassurance', week=W40)
    ref = A.load(path).ref
    # an output has no plan week of its own, so the child is planned explicitly for the rendered week
    child = I.create('核对产出里的来源', 'us-rok-reassurance', ref, I.revision(ref), due=T.week_due(W40))
    html = render_home(conn, WEDNESDAY)
    assert f'action="/plan/task/{child}"' in html
    assert f'href="/issues/{child}"' in html and '既有产出父议题' in visible(html)
    assert I.get(child)["parent"] == ref


def test_project_progress_counts_leaves_without_double_counting_parents():
    today, later = date.today().isoformat(), date.fromordinal(date.today().toordinal() + 30).isoformat()
    def row(id, status='todo', parent='', project='a', due=today, archived=False):
        return dict(id=id, status=status, parent=parent, project=project, due=due, archived=archived)
    rows = [row('parent','done'), row('child','doing','parent'), row('complete','done','parent'),
            row('later',due=later), row('park','parked'), row('archive','done',archived=True),
            row('other','doing',project='b')]
    result = dashboard.project_progress([dict(slug='a'),dict(slug='b'),dict(slug='empty')], W40, rows)
    assert result['a'] == dict(slug='a',total=3,done=1,doing=1,todo=1,week=1)
    assert result['b']['total'] == 1 and result['empty']['total'] == 0
    assert rows[0]['status'] == 'done'  # read-only, even when a closed parent has an open child


def test_home_and_direction_share_progress_and_task_response_updates_both(client, repo):
    import re
    tid, _ = T.add({'title':'本轮可视化验收','project':'us-rok-reassurance','week':week_of()})
    def bar(html):
        card = re.split(r'class="project-(?:progress-card|row)',html,maxsplit=1)[1].split('</a>',1)[0]   # home card, 研究档案 row
        return re.search('role="img" aria-label="([^"]+)"',card)[1]
    before = client.get('/').text
    assert bar(before) == bar(client.get('/issues?direction=alliance-us').text)
    assert 'hx-select-oob="#home-progress"' in before
    response = client.post(f'/plan/task/{tid}',data={'status':'done'},headers={'HX-Target':'home-tasks'},follow_redirects=False)
    assert response.status_code == 303
    after = client.get('/').text
    assert bar(after) != bar(before)
    assert bar(after) == bar(client.get('/issues?direction=alliance-us').text)


def test_home_parent_list_groups_all_this_week_tasks(repo):
    for slug in ('us-rok-reassurance','asean-centrality'):
        for i in range(4):
            T.add(dict(title=f'{slug} {i}',project=slug,week=W40),today=MONDAY)
    agenda = dashboard.weekly_agenda(W40)
    selected = [p for p in agenda['parents'] if p['slug'] in ('us-rok-reassurance','asean-centrality')]
    assert agenda['pending'] == 8 and len(selected) == 2
    assert sum(sum(n['issue']['id'].startswith('T-') for n in p['nodes']) for p in selected) == 8


def test_progress_ignores_parents_across_inactive_middle_nodes():
    due = date.today().isoformat()
    rows = [dict(id='output', project='a', status='todo', parent='', archived=False, due=due),
            dict(id='middle', project='a', status='parked', parent='output', archived=False, due=due),
            dict(id='child', project='a', status='doing', parent='middle', archived=False, due=due)]
    for archived in (False, True):
        rows[1]['archived'] = archived
        p = dashboard.project_progress([dict(slug='a')],W40,rows)['a']
        assert p == dict(slug='a',total=1,todo=0,doing=1,done=0,week=1)


def test_home_parent_direction_and_deadline_are_explicit(conn, repo):
    from research_lab.files import issues as I
    parent = I.project_ref('us-rok-reassurance')
    I.update(parent,'due','2026-10-20',I.revision(parent))
    T.add({'title':'本周证据核对','project':'us-rok-reassurance','week':W40,'due':'2026-10-03'},today=MONDAY)
    html = render_home(conn,WEDNESDAY)
    root = html.split('data-agenda-key="project-us-rok-reassurance"',1)[1].split('</summary>',1)[0]
    assert 'US–ROK Reassurance' in root and '联盟政治与美国研究' in root
    assert '2026-10-20' in root and '2026-10-03' not in root
    assert 'action="/plan/task/' not in root and ' open' not in root
    assert 'datetime="2026-10-03"' in html


def test_home_includes_this_week_artifact_issue_without_task_write(conn, repo):
    from research_lab.files import artifacts as A
    from research_lab.files import issues as I
    path = A.create('evidence','只有产出议题的课题','us-iran-talks',week=W40)
    issue = A.load(path)
    before = path.read_bytes()
    # written this week is not planned for this week: outputs stay off the 待办 until scheduled
    assert not any(p['slug'] == 'us-iran-talks' for p in dashboard.weekly_agenda(W40)['parents'])
    I.update(issue.ref, 'due', T.week_due(W40), I.revision(issue.ref))
    agenda = dashboard.weekly_agenda(W40)
    project = next(p for p in agenda['parents'] if p['slug']=='us-iran-talks')
    assert any(n['issue']['id']==issue.ref for n in project['nodes'])
    html = render_home(conn,WEDNESDAY)
    assert f'href="/issues/{issue.ref}"' in html and f'action="/plan/task/{issue.ref}"' not in html
    assert path.read_bytes() == before


def test_home_parent_counts_walk_through_inactive_intermediary(repo, monkeypatch):
    from research_lab.files import issues as I
    def row(ref,status,parent='',archived=False):
        return dict(id=ref,status=status,parent=parent,project='us-rok-reassurance',due=date.today().isoformat(),
                    done='',archived=archived)
    rows = [row('output','todo'),row('middle','parked','output'),row('leaf','doing','middle')]
    monkeypatch.setattr(I,'read',lambda:rows)
    for archived in (False,True):
        rows[1]['archived'] = archived
        project = next(p for p in dashboard.weekly_agenda(W40)['parents'] if p['slug']=='us-rok-reassurance')
        assert project['pending'] == project['total'] == 1
        assert project['nodes'][0]['issue']['id'] == 'output'
        assert project['nodes'][0]['children'][0]['children'][0]['issue']['id'] == 'leaf'
