"""_data/tasks.yml contract (files.tasks) and the /tasks page: round trip, sha guard, status, due dates and
postponements (no plan week since 0093), overdue and 本周 queries, validation, the create / status /
postpone routes, the 新建 menu entry and `rl tasks migrate-week`."""
from __future__ import annotations

import re
from datetime import date

import pytest

from research_lab.files import tasks as T
from research_lab.files.weeks import week_add, week_of
from research_lab.files.yamlio import StaleFileError

TODAY = date(2026, 10, 1)          # ISO week 2026-W40
WEEK = week_of(TODAY)


def _add(title="Put Goh and Ba on the literature map", project="asean-centrality", **kw):
    return T.add({"title": title, "project": project, **kw}, today=TODAY)


# ---------------------------------------------------------------- file contract

def test_missing_file_reads_empty_and_first_add_creates_it(repo):
    p = repo / "_data" / "tasks.yml"
    p.unlink(missing_ok=True)
    assert T.read() == [] and T.get("T-2026-0001") is None
    empty_sha = T.file_sha()
    tid, sha = T.add({"title": "First", "project": "asean-centrality"}, expected_sha=empty_sha, today=TODAY)
    assert tid == "T-2026-0001" and p.exists() and sha == T.file_sha()
    text = p.read_text()
    assert text.startswith("# One entry per 待办") and "\n- id: T-2026-0001\n" in text
    assert "created: '2026-10-01'" in text                 # quoted: a bare date would read back as datetime.date
    t = T.read()[0]
    assert "week:" not in text and "postponed: 0" in text      # no plan week is written any more
    assert t == {"id": "T-2026-0001", "title": "First", "project": "asean-centrality", "status": "todo",
                 "parent": None, "due": None, "words": None, "file": None,
                 "origin": "user", "source": "", "created": "2026-10-01", "done": None, "updated": "2026-10-01",
                 "postponed": 0, "note": "", "week": "", "carried": 0}     # week, carried: read-only compatibility


def test_ids_increment_per_year_and_order_is_kept(repo):
    _add("a"); _add("b", week=week_add(WEEK, 1)); _add("c", status="parked")
    ids = [t["id"] for t in T.read()]
    assert ids == ["T-2026-0001", "T-2026-0002", "T-2026-0003"]
    assert T.next_id(T.read(), date(2027, 1, 5)) == "T-2027-0001"


def test_update_touches_one_field_and_respects_sha(repo):
    tid, sha = _add(note="old")
    p = repo / "_data" / "tasks.yml"
    before = p.read_text()
    new_sha = T.update(tid, {"note": "new"}, expected_sha=sha)
    after = p.read_text()
    changed = [l for l in after.splitlines() if l not in before.splitlines()]
    assert changed == [f"  updated: '{date.today().isoformat()}'", "  note: new"] and new_sha == T.file_sha()
    with pytest.raises(StaleFileError):
        T.update(tid, {"title": "x"}, expected_sha=sha)             # stale
    assert T.get(tid)["title"] == "Put Goh and Ba on the literature map"
    with pytest.raises(T.TaskError):
        T.update(tid, {"id": "T-2026-9999"})
    assert T.update(tid, {"note": "new"}) == new_sha              # no-op leaves the file alone
    with pytest.raises(KeyError):
        T.update("T-2026-0042", {"note": "x"})


def test_status_stamps_done_and_clears_it(repo):
    tid, _ = _add()
    T.set_status(tid, "done", today=TODAY)
    assert T.get(tid)["status"] == "done" and T.get(tid)["done"] == "2026-10-01"
    T.set_status(tid, "doing")
    assert T.get(tid)["status"] == "doing" and T.get(tid)["done"] is None
    with pytest.raises(T.TaskError):
        T.set_status(tid, "someday")
    with pytest.raises(StaleFileError):
        T.set_status(tid, "parked", expected_sha="0" * 64)


def test_postpone_counts_later_due_dates_and_wakes_parked(repo):
    tid, _ = _add(due="2030-10-04")
    T.postpone(tid, "2030-10-11")
    T.postpone(tid, "2030-10-18")
    t = T.get(tid)
    assert t["due"] == "2030-10-18" and t["postponed"] == 2 and t["carried"] == 2 and t["week"] == "2030-W42"
    T.postpone(tid, "2030-10-04")                                 # pulling the date forward is not a postponement
    assert T.get(tid)["postponed"] == 2 and T.get(tid)["due"] == "2030-10-04"
    T.update(tid, {"due": "2030-11-01"})                          # the property editor counts the same way
    assert T.get(tid)["postponed"] == 3
    T.update(tid, {"due": ""})
    T.update(tid, {"due": "2030-12-01"})                          # a first due date is not a postponement
    assert T.get(tid)["postponed"] == 3
    T.update(tid, {"due": "2030-12-08"}, adjust_postponed=0)      # undo writes a fixed change instead
    assert T.get(tid)["postponed"] == 3
    T.set_status(tid, "parked")
    T.postpone(tid, "2030-12-15")
    assert T.get(tid)["status"] == "todo"
    T.set_status(tid, "done")
    with pytest.raises(T.TaskError):
        T.postpone(tid, "2030-12-22")
    with pytest.raises(T.TaskError):
        T.postpone(tid, "next friday")


def test_a_week_from_an_older_caller_becomes_that_weeks_friday(repo):
    tid, _ = _add(week="2030-W46")                                # add, update and carry all take a week
    assert T.get(tid)["due"] == "2030-11-15" and "week:" not in T.path().read_text()
    T.update(tid, {"week": "2030-W47"})
    assert T.get(tid)["due"] == "2030-11-22" and T.get(tid)["postponed"] == 1
    T.carry(tid, "2030-W48")
    assert T.get(tid)["due"] == "2030-11-29" and T.get(tid)["postponed"] == 2
    keep, _ = _add(week="2030-W46", due="2030-11-11")             # a due date given too wins
    assert T.get(keep)["due"] == "2030-11-11"
    with pytest.raises(T.TaskError):
        T.carry(tid, "2030-48")
    assert T.friday("2026-W41") == "2026-10-09" and T.sunday("2026-W41") == "2026-10-11"
    assert T.this_friday(date(2026, 10, 7)) == "2026-10-09" and T.this_friday(date(2026, 10, 10)) == "2026-10-10"
    assert T.next_friday(date(2026, 10, 7)) == "2026-10-16" and T.next_friday(date(2026, 10, 11)) == "2026-10-16"


def test_queries(repo):
    _add("overdue", due="2026-09-28")
    _add("this week", due="2026-10-03")
    _add("next week", due="2026-10-09")
    _add("no due")
    _add("parked old", due="2026-09-20", status="parked")
    done_id, _ = _add("done", due="2026-10-02")
    T.set_status(done_id, "done", today=TODAY)
    _add("other project", project="us-rok-reassurance", due="2026-10-04")
    rows = T.read()
    assert [t["title"] for t in T.overdue(rows, today=TODAY)] == ["overdue"]
    assert [t["title"] for t in rows if T.due_this_week(t, TODAY)] == ["overdue", "this week", "other project"]
    assert [t["title"] for t in rows if T.done_this_week(t, TODAY)] == ["done"]
    assert [t["title"] for t in T.for_project("us-rok-reassurance", rows)] == ["other project"]
    g = T.groups(data=rows, today=TODAY)
    assert {k: [t["title"] for t in v] for k, v in g.items()} == {
        "overdue": ["overdue"], "week": ["this week", "other project"], "later": ["next week", "no due"],
        "parked": ["parked old"], "done": ["done"]}
    f = T.flags(rows[0], today=TODAY)
    assert f["past_due"] and f["late"] and f["week_due"] and not f["this_week"] and f["due_days"] == -3
    assert T.flags(rows[1], today=TODAY)["this_week"] and not T.flags(rows[2], today=TODAY)["week_due"]


def test_an_unmigrated_file_reads_its_plan_week_as_due_on_sunday(repo):
    """Until `rl tasks migrate-week` runs, an open task with a week and no due date is due that Sunday
    (Leo 10-10), `carried` reads as `postponed`, and writing a due date drops the old week."""
    T.path().write_text("""- id: T-2026-0001
  title: 旧的本周待办
  project: asean-centrality
  status: todo
  week: 2026-W41
  origin: user
  created: '2026-10-05'
  done:
  carried: 2
  note: ''
- id: T-2026-0002
  title: 旧的已完成
  project: asean-centrality
  status: done
  week: 2026-W40
  origin: user
  created: '2026-09-28'
  done: '2026-10-01'
  carried: 0
  note: ''
""", encoding="utf-8")
    a, b = T.read()
    assert a["due"] == "2026-10-11" and a["week"] == "2026-W41" and a["postponed"] == 2
    assert b["due"] is None and b["week"] == ""
    T.postpone(a["id"], "2026-10-16")
    text = T.path().read_text()
    assert T.get(a["id"])["postponed"] == 3 and "week: 2026-W41" not in text and "postponed: 3" in text
    assert "carried: 2" not in text and "week: 2026-W40" in text   # the untouched entry waits for the migration


def test_migrate_week_dry_run_then_write(repo):
    from typer.testing import CliRunner
    from research_lab.cli import app
    from research_lab.files import issues as I
    T.path().write_text("""# One entry per 待办. Order is priority. `week` is the ISO week it is planned for; `status` is
# todo | doing | done | parked; `carried` counts moves to a later week. Written by the app, safe to hand-edit.
- id: T-2026-0001
  title: 没截止的
  project: asean-centrality
  status: doing
  week: 2026-W41
  origin: user
  created: '2026-10-05'
  done:
  updated: '2026-10-05'
  carried: 1
  note: ''
- id: T-2026-0002
  title: 有截止的
  project: asean-centrality
  status: todo
  week: 2026-W41
  due: '2026-10-20'
  origin: user
  created: '2026-10-05'
  done:
  carried: 0
  note: ''
- id: T-2026-0003
  title: 搁置的
  project: asean-centrality
  status: parked
  week: 2026-W39
  origin: user
  created: '2026-09-21'
  done:
  carried: 0
  note: ''
""", encoding="utf-8")
    I.path().write_text("PRJ-asean-centrality:\n  week: 2026-W42\n  status: doing\n", encoding="utf-8")
    before = T.path().read_text(), I.path().read_text()
    run = CliRunner().invoke(app, ["tasks", "migrate-week", "--dry-run"])
    assert run.exit_code == 0, run.output
    assert (T.path().read_text(), I.path().read_text()) == before                 # a dry run writes nothing
    assert "截止设为 2026-10-11" in run.output and "保留截止 2026-10-20" in run.output and "去掉计划周 2026-W39" in run.output
    assert "carried 改名 postponed，次数 1" in run.output and "截止设为 2026-10-18" in run.output and "预演" in run.output
    run = CliRunner().invoke(app, ["tasks", "migrate-week"])
    assert run.exit_code == 0 and "已写入：3 条待办、1 条议题属性" in run.output
    text = T.path().read_text()
    assert "week:" not in text and "carried" not in text and "`week` is the ISO week" not in text
    assert "postponed: 1" in text and "updated: '2026-10-05'" in text           # rewriting keys stamps nothing
    a, b, c = T.read()
    assert (a["due"], b["due"], c["due"]) == ("2026-10-11", "2026-10-20", None)
    assert "week" not in I.path().read_text() and I.get("PRJ-asean-centrality")["due"] == "2026-10-18"
    assert "没有计划周要迁移" in CliRunner().invoke(app, ["tasks", "migrate-week"]).output


def test_validation(repo):
    with pytest.raises(T.TaskError):
        _add("")
    with pytest.raises(T.TaskError):
        _add("x", project="no-such-project")
    with pytest.raises(T.TaskError):
        _add("x", week="2026-41")
    with pytest.raises(T.TaskError):
        _add("x", status="later")
    with pytest.raises(T.TaskError):
        _add("y" * 121)
    assert T.read() == []


# ---------------------------------------------------------------- page

def _visible_text(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def _redirect(r) -> str:
    """Where a mutation sent the browser: 303 for a plain form, 204 + HX-Redirect for an HTMX post
    (the test client sends HX-Request on everything, like the app's own pages do)."""
    assert r.status_code in (204, 303), r.status_code
    return r.headers.get("location") or r.headers.get("HX-Redirect")


def test_tasks_page_create_and_menu(client, repo):
    page = client.get("/tasks").text
    assert "还没有待办" in page and 'href="/tasks/new"' in page
    home = client.get("/").text
    assert "New issue" in home and 'href="/issues/new?' in home  # one global create entry
    assert 'name="week"' not in client.get("/tasks/new").text and 'name="due"' in client.get("/tasks/new").text
    # due today, so it is on the default 本周 view (due by Sunday) whatever day the tests run
    r = client.post("/tasks", data={"title": "读完 Ba 的第三章", "project": "asean-centrality", "due": date.today().isoformat(),
                                    "note": "", "sha": T.file_sha()})
    assert _redirect(r) == "/tasks?saved=created"
    page = client.get("/tasks?saved=created").text
    assert "读完 Ba 的第三章" in page and "已加入待办" in page and "ASEAN Centrality" in page and "今天截止" in page
    assert "T-2026" not in _visible_text(page)                    # ids live in URLs only
    tid = T.read()[0]["id"]
    assert f"/tasks/{tid}/status" in page and f"/tasks/{tid}/postpone" in page and "Next Fri" in page
    assert client.get("/tasks?project=asean-centrality").text.count("读完 Ba 的第三章") >= 1
    assert "读完 Ba 的第三章" not in client.get("/tasks?project=us-rok-reassurance").text


def test_tasks_status_postpone_and_edit_routes(client, repo):
    tid, _ = _add("Write the puzzle paragraph", due=date.today().isoformat())
    assert _redirect(client.post(f"/tasks/{tid}/status", data={"status": "doing"})) == "/tasks"
    assert T.get(tid)["status"] == "doing"
    assert _redirect(client.post(f"/tasks/{tid}/postpone", data={"to": "next_friday", "filter": "asean-centrality"})) == "/tasks?project=asean-centrality"
    assert T.get(tid)["postponed"] == 1 and T.get(tid)["due"] == T.next_friday()
    page = client.get("/tasks?view=all").text
    assert "截止推迟 1 次" in page and "进行中" in page and "移到下周" not in page and "移到本周" not in page
    assert _redirect(client.post(f"/tasks/{tid}/carry", data={"week": week_add(week_of(), 2)})) == "/tasks"   # an old board
    assert T.get(tid)["due"] == T.friday(week_add(week_of(), 2))
    assert _redirect(client.post(f"/tasks/{tid}/status", data={"status": "done", "view": "all"})) == "/tasks?view=all"
    assert "已完成" in client.get("/tasks?view=all").text and T.get(tid)["done"]
    assert client.post(f"/tasks/{tid}/postpone", data={"to": "this_friday"}).status_code == 400
    form = client.get(f"/tasks/{tid}/edit").text
    assert 'value="Write the puzzle paragraph"' in form
    r = client.post(f"/tasks/{tid}/edit", data={"title": "Write the puzzle paragraph (v2)", "project": "asean-centrality",
                                                "note": "", "sha": T.file_sha()})
    assert _redirect(r) == "/tasks?saved=edited" and T.get(tid)["title"] == "Write the puzzle paragraph (v2)"
    assert client.post("/tasks/T-2026-0099/status", data={"status": "done"}).status_code == 404
    hx = client.post(f"/tasks/{tid}/status", data={"status": "todo"}, headers={"HX-Target": "task-board"}, follow_redirects=False)
    assert hx.status_code == 303                                   # a row action: htmx follows it and hx-selects the board


def test_tasks_create_rejects_stale_and_invalid(client, repo):
    good = {"title": "x", "project": "asean-centrality", "week": WEEK, "note": ""}
    stale = client.post("/tasks", data={**good, "sha": "0" * 64})
    assert stale.status_code == 409 and "没有写入" in stale.text and T.read() == []
    bad = client.post("/tasks", data={**good, "title": "", "sha": T.file_sha()})
    assert bad.status_code == 400 and "标题" in bad.text
    bad2 = client.post("/tasks", data={**good, "project": "nope", "sha": T.file_sha()})
    assert bad2.status_code == 400 and "课题" in bad2.text and T.read() == []


def test_sub_tasks_like_linear_issues(repo):
    """A chapter is a parent (due, target words, Word file); its sections are sub-tasks with their own due."""
    from research_lab.files import tasks as T
    proj = "asean-centrality"
    other = next(p["slug"] for p in __import__("research_lab.files.datafiles", fromlist=["read"]).read("projects") if p["slug"] != proj)
    ch, _ = T.add({"title": "第二章", "project": proj, "due": "2030-12-20", "words": "20000",
                   "file": "第二章.docx"}, today=TODAY)
    s1, _ = T.add({"title": "1. 地区秩序", "project": proj, "parent": ch, "due": "2030-11-20"}, today=TODAY)
    s2, _ = T.add({"title": "2. 周边环境", "project": proj, "parent": ch, "due": "2030-12-05"}, today=TODAY)
    text = T.path().read_text(encoding="utf-8")
    assert "  status: todo\n  parent: " in text and "due: '2030-12-20'\n  words: 20000\n  file: 第二章.docx\n" in text
    parent = T.get(ch)
    assert parent["words"] == 20000 and parent["due"] == "2030-12-20"   # a parent keeps its own due date
    assert [t["id"] for t in T.children(ch)] == [s1, s2] and T.progress(parent) == {"done": 0, "total": 2}
    T.set_status(s1, "done", today=TODAY)
    assert T.get(ch)["due"] == "2030-12-20" and T.progress(T.get(ch)) == {"done": 1, "total": 2}
    T.move(s2, s1)
    assert [t["id"] for t in T.children(ch)] == [s2, s1]
    # one level, same 课题, never itself; a parent cannot switch 课题 without its sub-tasks
    with pytest.raises(T.TaskError, match="不能再建子任务"):
        T.add({"title": "x", "project": proj, "parent": s1}, today=TODAY)
    with pytest.raises(T.TaskError, match="同一个课题"):
        T.add({"title": "x", "project": other, "parent": ch}, today=TODAY)
    with pytest.raises(T.TaskError, match="自己的子任务"):
        T.update(ch, {"parent": ch})
    with pytest.raises(T.TaskError, match="不能再挂"):
        T.update(ch, {"parent": T.add({"title": "y", "project": proj}, today=TODAY)[0]})
    with pytest.raises(T.TaskError, match="单独换课题"):
        T.update(ch, {"project": other})
    with pytest.raises(T.TaskError, match="YYYY-MM-DD"):
        T.update(s2, {"due": "2030-02-30"})
    with pytest.raises(T.TaskError, match="文件名"):
        T.update(ch, {"file": "../x.docx"})
    T.update(s2, {"parent": "", "due": ""})                                  # cleared fields leave the file
    assert T.get(s2)["parent"] is None and "due: '2030-12-05'" not in T.path().read_text(encoding="utf-8")



def _chapter(proj="asean-centrality"):
    ch, _ = T.add({"title": "第二章", "project": proj, "due": "2030-12-20", "words": "20000", "file": "第二章.docx"}, today=TODAY)
    a, _ = T.add({"title": "一节", "project": proj, "parent": ch, "due": "2030-11-20"}, today=TODAY)
    b, _ = T.add({"title": "二节", "project": proj, "parent": ch, "due": "2030-12-05"}, today=TODAY)
    return ch, a, b


def test_add_keeps_the_due_date_as_given(repo):
    t1, _ = T.add({"title": "交稿", "project": "asean-centrality", "due": "2030-11-20"}, today=TODAY)
    t2, _ = T.add({"title": "早就该交", "project": "asean-centrality", "due": "2020-01-10"}, today=TODAY)
    t3, _ = T.add({"title": "没截止", "project": "asean-centrality"}, today=TODAY)
    assert T.get(t1)["due"] == "2030-11-20" and T.get(t2)["due"] == "2020-01-10" and T.get(t3)["due"] is None
    assert T.get(t2)["week"] == "2020-W02" and "week:" not in T.path().read_text()


def test_parent_keeps_its_own_due_date_and_parks_with_its_sub_tasks(repo):
    ch, a, b = _chapter()
    T.postpone(a, "2030-12-01")                                     # a section moving leaves the parent alone
    assert T.get(ch)["due"] == "2030-12-20" and T.get(a)["postponed"] == 1
    T.set_status(a, "done", today=TODAY)
    T.set_status(ch, "parked")                                      # parking the parent parks what is still open
    assert [T.get(x)["status"] for x in (ch, a, b)] == ["parked", "done", "parked"]
    T.postpone(ch, "2030-12-27")                                    # back from parked: together, nothing left behind
    assert [T.get(x)["status"] for x in (ch, a, b)] == ["todo", "done", "todo"]
    assert T.get(ch)["due"] == "2030-12-27" and T.get(b)["due"] == "2030-12-05"
    T.set_status(ch, "parked")
    T.set_status(ch, "doing")
    assert T.get(b)["status"] == "todo"
    T.set_status(ch, "done", today=TODAY)                            # a parent can be done any time (Leo, 10-01)
    assert T.get(ch)["status"] == "done" and T.get(b)["status"] == "todo"


def test_tree_views(repo):
    week = week_of(date(2030, 11, 20))
    ch, a, b = _chapter()
    plain, _ = T.add({"title": "读 Goh", "project": "asean-centrality", "due": "2030-11-22"}, today=TODAY)
    later, _ = T.add({"title": "以后再说", "project": "asean-centrality", "due": "2031-03-07"}, today=TODAY)
    late, _ = T.add({"title": "拖着的", "project": "asean-centrality", "due": "2030-10-04"}, today=TODAY)
    rows = T.read()
    today = date(2030, 11, 18)                                       # a Monday: 本周 runs to the 24th

    def ids(view):
        return [(n["task"]["id"], [s["task"]["id"] for s in n["subs"]])
                for blk in T.tree(rows, view, week, project_order=["asean-centrality"], today=today) for n in blk["items"]]
    assert ids("week") == [(ch, [a, b]), (plain, []), (late, [])]  # due by Sunday, overdue included
    assert ids("overdue") == [(late, [])]
    assert {i for i, _ in ids("all")} == {ch, plain, later, late}
    [blk] = T.tree(rows, "week", week, today=today)
    node = blk["items"][0]
    assert node["progress"] == {"done": 0, "total": 2} and node["subs"][0]["flags"]["due_soon"]
    assert node["subs"][0]["flags"]["due_days"] == 2 and not node["subs"][1]["flags"]["due_soon"]
    T.set_status(ch, "done", today=TODAY)                            # done parent: open sections folded under it
    rows = T.read()
    assert (ch, [a, b]) in ids("week")  # open work stays visible even when its parent is done
    assert a not in [i for i, _ in ids("week")]  # no duplicate standalone child
    [blk] = T.tree(rows, "all", week, today=today)
    assert [n["task"]["id"] for n in blk["closed"]] == [ch] and [s["task"]["id"] for s in blk["closed"][0]["subs"]] == [a, b]


def test_tasks_under_an_output_and_the_done_line(repo):
    """ARCHIVE-ISSUES.md §D: a task with source ART-… is that output's sub-issue; finishing it writes one
    system line into the output's 进度记录, once."""
    from research_lab.files import updates
    ref = "ART-2026-4001"
    a, _ = T.add({"title": "补一个反例", "project": "asean-centrality", "source": ref}, today=TODAY)
    b, _ = T.add({"title": "改定义", "project": "asean-centrality", "source": ref}, today=TODAY)
    T.add({"title": "无关", "project": "asean-centrality", "source": "M1"}, today=TODAY)
    assert [t["id"] for t in T.for_source(ref)] == [a, b]
    T.set_status(a, "doing", today=TODAY)
    assert updates.for_target(ref) == []
    T.set_status(a, "done", today=TODAY)
    T.update(b, {"status": "done"})
    T.set_status(a, "done", today=TODAY)                                   # no change, no second line
    assert [u["text"] for u in updates.for_target(ref)][::-1] == ["完成待办「补一个反例」", "完成待办「改定义」"]
    assert {u["by"] for u in updates.for_target(ref)} == {"system"}
    # one owner per task: a sub-task shows under its parent, not under the output; its source matches the parent's
    p, _ = T.add({"title": "把定义推到可入文", "project": "asean-centrality", "source": ref}, today=TODAY)
    s, _ = T.add({"title": "查 Acharya", "project": "asean-centrality", "parent": p, "source": ref}, today=TODAY)
    assert s not in [t["id"] for t in T.for_source(ref)] and p in [t["id"] for t in T.for_source(ref)]
    with pytest.raises(T.TaskError, match="来源要和父任务一致"):
        T.add({"title": "x", "project": "asean-centrality", "parent": p, "source": "M1"}, today=TODAY)
