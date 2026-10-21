"""进度检查 (pm/checkup.py, docs/design/ISSUE-AGENT.md §6): pure signals, scopes, the rendered text,
and run() over real files with the pm.stalled_days setting."""
from __future__ import annotations

from datetime import date

from research_lab.pm import checkup as C

TODAY = date(2026, 10, 8)          # Thursday of ISO week 2026-W41; 本周 runs to Sunday the 11th
W41 = "2026-W41"
PROJECTS = [{"slug": "asean", "name": "东盟课题"}, {"slug": "sino-jp", "name": "中日关系课题"},
            {"slug": "quiet", "name": "空课题"}]


def row(ref, title, *, project="asean", parent="", status="todo", due="", done="", archived=False, root=False):
    url = f"/issues?project={project}" if root else f"/issues/{ref}"
    return dict(id=ref, title=title, project=project, parent=parent, status=status, due=due, done=done,
                archived=archived, project_root=root, url=url, created="2026-09-01", labels=[], related=[])


def sample():
    rows = [
        row("T1", "第二章", status="doing"),
        row("T2", "第三节", parent="T1", due="2026-10-06"),
        row("T3", "第一节", parent="T1", status="done", done="2026-10-06"),
        row("T4", "读 Acharya 第三章", due="2026-10-10"),
        row("T5", "数据清洗", status="doing", due="2026-10-20"),
        row("T6", "整理会议纪要", status="done", done="2026-10-07"),
        row("T7", "旧任务", status="parked", due="2026-09-01"),
        row("T8", "中日一", project="sino-jp", status="done", done="2026-10-07"),
        row("T9", "中日二", project="sino-jp", status="done", done="2026-10-05"),
    ]
    tasks = [{"id": "T4", "postponed": 2}, {"id": "T7", "postponed": 5}]
    touched = {"T1": "2026-09-01", "T5": "2026-09-23", "T2": "2026-10-07"}
    return rows, tasks, touched


def report(scope="week", target="", **kw):
    rows, tasks, touched = sample()
    return C.check(rows, tasks=tasks, touched=touched, projects=PROJECTS, roots=[], scope=scope, target=target,
                   today=TODAY, **kw)


def test_signals_rules():
    kw = dict(today=TODAY, week=W41)
    assert C.signals(row("a", "x", due="2026-10-06"), **kw) == {"overdue": 2}
    assert C.signals(row("a", "x", due="2026-10-15"), **kw) == {"due_soon": 7}
    assert C.signals(row("a", "x", due="2026-10-16"), **kw) == {}
    assert C.signals(row("a", "x"), **kw) == {}                       # no due date, no plan week: nothing is late
    assert C.signals(row("a", "x"), task={"postponed": 2}, **kw) == {"postponed": 2}
    assert C.signals(row("a", "x"), task={"postponed": 1}, **kw) == {}
    stalled = row("a", "x", status="doing")
    assert C.signals(stalled, touched="2026-09-24", **kw) == {"stalled": 14}
    assert C.signals(stalled, touched="2026-09-25", **kw) == {}
    assert C.signals(stalled, touched="2026-09-30", stalled_days=7, **kw) == {"stalled": 8}
    assert C.signals(row("a", "x", status="todo"), touched="2026-01-01", **kw) == {}   # only 进行中 stalls
    # closed, parked and archived issues never need attention
    for r in (row("a", "x", status="done", due="2026-01-01"), row("a", "x", status="parked", due="2026-01-01"),
              row("a", "x", due="2026-01-01", archived=True)):
        assert C.signals(r, **kw) == {}
    # a parent with open sub-issues is not stalled itself; its own deadline still counts
    parent = row("p", "x", status="doing", due="2026-10-01")
    assert C.signals(parent, touched="2026-01-01", open_children=True, **kw) == {"overdue": 7}
    # a 课题 only has deadlines
    root = row("PRJ-asean", "东盟课题", root=True, status="doing", due="2026-10-10")
    assert C.signals(root, touched="2026-01-01", task={"postponed": 3}, **kw) == {"due_soon": 2}


def test_week_report_summaries_and_ranking():
    r = report()
    assert r["title"] == "第 41 周进度" and r["subtitle"] == "截至 10 月 8 日周四" and r["week"] == W41
    asean, sino = r["projects"]
    assert [p["slug"] for p in r["projects"]] == ["asean", "sino-jp"]          # 空课题 has nothing this week
    # leaves: the parent 第二章 counts through its sub-issues; parked 旧任务 is out of the denominator.
    # 本周: due by Sunday (第三节 overdue, Acharya) plus finished since Monday (第一节, 会议纪要)
    assert (asean["week_total"], asean["week_done"], asean["done"], asean["total"]) == (4, 2, 2, 5)
    assert (sino["week_total"], sino["week_done"]) == (2, 2)
    assert [a["id"] for a in r["attention"]] == ["T2", "T5", "T4"]
    t2, t5, t4 = r["attention"]
    assert t2["display"] == "第二章 · 第三节" and t2["text"] == "逾期 2 天"
    assert t5["text"] == "进行中，15 天没动"
    assert t4["text"] == "2 天后截止，截止已推迟 2 次，建议搁置或拆小"
    assert "T1" not in {a["id"] for a in r["attention"]}                      # its sub-issues speak for it


def test_render_matches_the_design_sample():
    assert C.render(report()) == (
        "**第 41 周进度** · 截至 10 月 8 日周四\n\n"
        "[东盟课题](/issues?project=asean)：本周 4 项完成 2 项；子议题 2/5\n"
        "- [第二章 · 第三节](/issues/T2)：逾期 2 天\n"
        "- [数据清洗](/issues/T5)：进行中，15 天没动\n"
        "- [读 Acharya 第三章](/issues/T4)：2 天后截止，截止已推迟 2 次，建议搁置或拆小\n\n"
        "[中日关系课题](/issues?project=sino-jp)：本周 2 项全部完成\n\n"
        "要注意的共 3 条\n")


def test_at_most_five_lines_and_link_text_is_escaped():
    rows = [row(f"L{i}", f"落后 [{i}]", due="2026-10-01") for i in range(7)]
    r = C.check(rows, projects=PROJECTS, today=TODAY)
    assert len(r["attention"]) == 7 and len(r["shown"]) == 5
    text = C.render(r)
    assert text.count("\n- ") == 5 and "要注意的共 7 条；最多列 5 条，其余到[研究档案](/issues)看" in text
    assert r"[落后 \[0\]](/issues/L0)" in text


def test_project_and_issue_scopes():
    p = report("project", "PRJ-asean")
    assert p["scope"] == "project" and p["target"] == "asean" and p["title"] == "东盟课题 · 第 41 周进度"
    assert [s["slug"] for s in p["projects"]] == ["asean"] and {a["project"] for a in p["attention"]} == {"asean"}
    i = report("issue", "T1")
    assert i["issue"]["progress"] == {"done": 1, "total": 2} and i["issue"]["status_label"] == "进行中"
    assert [a["id"] for a in i["attention"]] == ["T2"] and i["projects"] == []
    text = C.render(i)
    assert text.startswith("**第二章 · 进度**") and "[第二章](/issues/T1)：进行中 · 1/2 项" in text
    assert "- [第二章 · 第三节](/issues/T2)：逾期 2 天" in text
    assert report("issue", "PRJ-sino-jp")["scope"] == "project"
    empty = C.check([], projects=PROJECTS, today=TODAY)
    assert empty["projects"] == [] and C.render(empty).endswith("没有要注意的。\n")


def test_run_reads_files_setting_and_updated_dates(conn, repo):
    from research_lab.db.connection import set_setting
    from research_lab.files import issues as I, tasks as T
    from research_lab.files.weeks import week_of
    week = week_of()
    old = date.fromordinal(date.today().toordinal() - 10)
    ref, _ = T.add({"title": "十天没动的议题", "project": "asean-centrality", "status": "doing"}, today=old)
    assert T.get(ref)["updated"] == old.isoformat()
    assert ref not in {a["id"] for a in C.run(conn)["attention"]}            # default: 14 days
    set_setting(conn, "pm.stalled_days", 7)
    hit = next(a for a in C.run(conn)["attention"] if a["id"] == ref)
    assert hit["kinds"]["stalled"] == 10 and hit["text"] == "进行中，10 天没动"
    I.comment(ref, "今天推进了一点。", I.revision(ref))                          # a comment is a touch
    assert ref not in {a["id"] for a in C.run(conn)["attention"]}
    for bad in ("nonsense", 5, 31):                                          # tunables: 7–30
        set_setting(conn, "pm.stalled_days", bad)
        assert C.stalled_days(conn) == 14
    set_setting(conn, "pm.stalled_days", 30)
    assert C.stalled_days(conn) == 30 and C.stalled_days(None) == 14
    r = C.run(conn, "project", "asean-centrality")
    assert r["projects"][0]["slug"] == "asean-centrality" and r["week"] == week
    assert C.render(C.run(conn, "issue", ref)).startswith("**十天没动的议题 · 进度**")


def test_dashboard_project_progress_uses_the_same_leaves():
    from research_lab import dashboard
    rows, _, _ = sample()
    got = dashboard.project_progress(PROJECTS[:1], W41, rows, today=TODAY)["asean"]
    s = C.project_summary(rows, PROJECTS[0], W41, today=TODAY)
    assert (got["todo"], got["doing"], got["done"], got["total"], got["week"]) == \
           (s["todo"], s["doing"], s["done"], s["total"], s["week_open"]) == (2, 1, 2, 5, 2)


def test_kinds_map_to_the_preset_rule_keys():
    from research_lab import tunables
    assert C.RULES == {"overdue": "pm.rule.overdue", "stalled": "pm.rule.stalled"}   # pm.rule.slipped retired with the plan week
    assert all(k in C.KINDS for k in C.RULES) and all(tunables.entry(v) for v in C.RULES.values())
