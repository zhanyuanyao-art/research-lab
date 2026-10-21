"""Card 0212: Theo steers a replication from a sentence, through the functions the replication page's buttons call.
Reading the plan, the 复现清单 changed directly and undone, the assistant's own picks and every run waiting on a card,
a question changing nothing, and replication sentences reaching the replication-control skill. Nothing here runs R,
downloads, calls a model or starts Claude Code: the starters are replaced."""
from __future__ import annotations

import pytest

from research_lab.agent import replication_tools as RT
from research_lab.agent import skills, tools
from research_lab.chat import cards
from research_lab.pm import changes as CH
from research_lab.replication import coverage, pipeline, plans, rrun
from research_lab.replication import workspace as W

from .test_pipeline import fakes, plan  # noqa: F401

REAL_ITEMS = coverage.items
URL = "https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi:10.7910/DVN/ABCDEF"


def item(label: str, kind: str, num: str, source: str = "main", group: str = "main") -> coverage.Item:
    return coverage.Item(source=source, kind=kind, num=num, label=label, caption=f"{label}: a caption", page="5",
                         group=group, scripts=[{"path": "code/main.R"}] if label != "Figure 1" else [])


@pytest.fixture
def listed(conn, plan):  # noqa: F811
    """A plan titled like a paper, with Table 1, Table 2, Figure 1 and Figure 3 in the main text (ticked) and Table A1
    in the appendix (not ticked)."""
    plans.save_fields(conn, plan, {"title": "Advisors and Leaders"})
    coverage.save_scan(conn, plan, coverage.Scan(
        items=[item("Table 1", "table", "1"), item("Table 2", "table", "2"), item("Figure 1", "figure", "1"),
               item("Figure 3", "figure", "3"), item("Table A1", "table", "A1", "appendix", "robust")],
        sources=[{"source": "main", "name": "paper", "pages": 30}, {"source": "appendix", "name": "appendix", "pages": 9}]))
    return plan


def ask(conn, prompt, *, page=None, parent=None):
    """A request as the router stores it, on the replication-control skill. Returns the task id."""
    from research_lab.agent import tasks
    from research_lab.chat import service
    tid = tasks.get(conn, parent)["thread_id"] if parent else service.create_thread(conn, "复现")
    service.add_message(conn, tid, "user", prompt, card_kind="page" if page else None, card_ref=page)
    ph = service.add_message(conn, tid, "assistant", "", card_kind="task")
    return tasks.create(conn, prompt, thread_id=tid, message_id=ph.id, skill="replication-control", parent_id=parent)


def call(conn, name, args, task_id=None):
    text, err = tools.call(conn, name, args, 6000, task_id=task_id)
    assert not err, text
    return text


def ticked(conn, plan_id):
    return {r["label"]: r["ticked"] for r in coverage.items(conn, plan_id)}


def batch_of(conn, task_id):
    (b,) = CH.batches(conn, task_id=task_id)
    return b


# ---------------------------------------------------------------- reading

def test_the_skill_holds_only_tools_that_exist():
    sk = skills.get("replication-control")
    assert sk.runs_tasks and set(sk.tools) <= set(tools.TOOLS)
    assert "Leo" not in sk.instructions and "International" not in sk.instructions
    assert "Leo" not in skills.get("methods").instructions


def test_list_and_get_show_the_plan_its_run_and_what_needs_the_user(conn, listed, fakes, monkeypatch):  # noqa: F811
    pipeline.run_now(conn, listed)
    monkeypatch.setattr(coverage, "items", REAL_ITEMS)       # the fakes replaced the list the run read
    line = call(conn, "list_replications", {"query": "advisors"})
    assert f"[Advisors and Leaders](/data/plans/{listed})" in line and "能跑的都跑完了" in line and "勾选 4/5" in line
    text = call(conn, "get_replication", {"plan": "Advisors"})
    assert "运行作者代码 卡住" in text and "展示稿 出错" in text
    assert "缺依赖" in text and "brew install cmake" in text and "等数据 survey.dta" in text
    assert "正文结果 4/4：✓ Table 1；✓ Table 2；✓ Figure 1 没对上脚本；✓ Figure 3" in text
    assert "附录稳健性 0/1：✗ Table A1" in text
    assert "论文报告值还没审阅" in text and "一键跑完走作者的 R 代码" in text
    assert "（" not in text


def test_the_plan_is_the_page_the_user_is_on(conn, listed):
    other = plans.create_blank(conn)
    plans.save_fields(conn, other, {"title": "Who Convenes Asia"})
    task = ask(conn, "这个复现到哪了", page=f"/data/plans/{listed}/detail")
    assert "Advisors and Leaders" in call(conn, "get_replication", {"plan": "这个"}, task)
    text, err = tools.call(conn, "get_replication", {"plan": "这个"}, 6000, task_id=ask(conn, "复现到哪了"))
    assert err and "Advisors and Leaders" in text and "Who Convenes Asia" in text      # two plans, no page: ask


# ---------------------------------------------------------------- 复现清单

def test_named_targets_change_at_once_and_undo(conn, listed):
    task = ask(conn, "只复现表 2 和图 3", page=f"/replicate/{listed}")
    text = call(conn, "set_replication_targets", {"plan": "这个", "tick": ["Table 2", "Figure 3"], "only": True}, task)
    assert text.startswith("已改 2 处。") and "撤销" in text
    assert ticked(conn, listed) == {"Table 1": False, "Table 2": True, "Figure 1": False, "Figure 3": True, "Table A1": False}
    b = batch_of(conn, task)
    assert b["undoable"] and {r["kind"] for r in b["rows"]} == {"replication"}
    view = cards.plan_view(conn, b["batch_id"], {})
    assert view["state"] == "applied" and view["head"] == "助手已改 2 处"
    assert {(x["label"], x["what"], x["old"], x["new"]) for x in view["plain"]} == {
        ("Table 1 · Advisors and Leaders", "复现清单", "复现", "不复现"),
        ("Figure 1 · Advisors and Leaders", "复现清单", "复现", "不复现")}
    back = CH.undo(conn, b["batch_id"])
    assert len(back["undone"]) == 2 and all(ticked(conn, listed)[k] for k in ("Table 1", "Figure 1"))


def test_undo_leaves_what_the_user_changed_since(conn, listed):
    task = ask(conn, "表 1 由小王汇报")
    assert call(conn, "set_replication_targets", {"plan": listed, "tick": ["表 1"], "owner": "小王"}, task).startswith("已改 1 处")
    row = next(r for r in coverage.items(conn, listed) if r["label"] == "Table 1")
    assert row["owner"] == "小王"
    coverage.save_marks(conn, listed, [row["id"]], [row["id"]], {row["id"]: "小李"})     # the user's own edit, on the page
    out = CH.undo(conn, batch_of(conn, task)["batch_id"])
    assert out["kept"] and out["kept"][0]["note"] == "这一项你后来改成了 小李，我没动"
    assert next(r for r in coverage.items(conn, listed) if r["label"] == "Table 1")["owner"] == "小李"


def test_the_assistants_own_pick_waits_on_a_card(conn, listed):
    task = ask(conn, "把稳健性检验里跟顾问有关的表也勾上")
    text = call(conn, "set_replication_targets", {"plan": listed, "tick": ["Table A1"]}, task)
    assert "card waiting" in text and not ticked(conn, listed)["Table A1"]
    b = batch_of(conn, task)
    assert b["status"] == "proposed"
    CH.apply(conn, b["batch_id"])
    assert ticked(conn, listed)["Table A1"]


def test_a_question_changes_nothing_and_a_yes_to_its_lines_does(conn, listed):
    first = ask(conn, "要不要只复现表 2？")
    text = call(conn, "set_replication_targets", {"plan": listed, "tick": ["Table 2"], "only": True}, first)
    assert text.startswith("not done") and "「Advisors and Leaders」复现清单：不复现 Table 1、Figure 1、Figure 3" in text
    assert not CH.batches(conn, task_id=first) and ticked(conn, listed)["Table 1"]
    lines = [x for x in text.splitlines() if x.startswith("「")]
    conn.execute("UPDATE agent_tasks SET status = 'waiting', question = ? WHERE id = ?",
                 ("要只复现表 2 吗？\n" + "\n".join(lines), first))
    yes = ask(conn, "好", parent=first)
    out = call(conn, "set_replication_targets", {"plan": listed, "tick": ["Table 2"], "only": True}, yes)
    assert out.startswith("已改 3 处") and not ticked(conn, listed)["Table 1"]


def test_an_unknown_label_is_refused_with_the_list(conn, listed):
    text, err = tools.call(conn, "set_replication_targets", {"plan": listed, "tick": ["Table 9"]}, 6000,
                           task_id=ask(conn, "复现表 9"))
    assert err and "Table 9" in text and "Table A1" in text


# ---------------------------------------------------------------- data

def test_a_link_the_user_typed_is_added_at_once_and_undone(conn, listed):
    task = ask(conn, f"给这篇复现加个数据链接 {URL}")
    text = call(conn, "add_replication_data", {"plan": listed, "url": URL, "note": "作者的复现包"}, task)
    assert text.startswith("已改 1 处") and "撤销" in text
    assert [l["url"] for l in plans.get(conn, listed)["plan"]["links"]] == [URL]
    assert plans.get(conn, listed)["plan"]["links"][0]["kind"] == "dataverse"
    CH.undo(conn, batch_of(conn, task)["batch_id"])
    assert plans.get(conn, listed)["plan"]["links"] == []


def test_a_link_the_user_did_not_type_waits_on_a_card_and_a_bad_one_is_refused(conn, listed):
    task = ask(conn, "把论文里写的那个复现包链接加上")
    assert "card waiting" in call(conn, "add_replication_data", {"plan": listed, "url": URL}, task)
    assert plans.get(conn, listed)["plan"]["links"] == []
    text, err = tools.call(conn, "add_replication_data", {"plan": listed, "url": "file:///etc/passwd"}, 6000, task_id=task)
    assert err and "not a public http(s) link" in text


# ---------------------------------------------------------------- runs

def test_run_all_starts_only_after_the_card_is_clicked(conn, listed, monkeypatch):
    started = []
    monkeypatch.setattr(pipeline, "start", lambda c, pid: started.append(pid) or True)
    task = ask(conn, "把这篇一键跑完", page=f"/data/plans/{listed}")
    text = call(conn, "run_replication", {"plan": "这个", "action": "run_all"}, task)
    assert "card waiting" in text and "报告这一步调用 AI" in text and started == []
    b = batch_of(conn, task)
    view = cards.plan_view(conn, b["batch_id"], {})
    assert view["state"] == "proposed" and view["rows"][0]["what"] == "一键跑完" and view["rows"][0]["new"] == "开始"
    assert "调用 AI" in view["reason"]
    again = call(conn, "run_replication", {"plan": "这个", "action": "run_all"}, task)     # the same card, not a second row
    assert "card waiting" in again and len(CH.rows(conn, b["batch_id"])) == 1
    out = CH.apply(conn, b["batch_id"])
    assert started == [listed] and out["applied"][0]["note"].startswith("已开始一键跑完")
    assert not batch_of(conn, task)["undoable"]                                             # a started run is not undone
    assert CH.apply(conn, b["batch_id"])["already"] and started == [listed]


def test_a_question_about_running_makes_no_card(conn, listed, monkeypatch):
    monkeypatch.setattr(pipeline, "start", lambda c, pid: pytest.fail("must not start"))
    task = ask(conn, "要不要一键跑完？")
    text = call(conn, "run_replication", {"plan": listed, "action": "run_all"}, task)
    assert text.startswith("not done") and "「Advisors and Leaders」一键跑完：开始" in text
    assert not CH.batches(conn, task_id=task)


def test_routes_refuse_plainly_when_they_cannot_start(conn, listed, monkeypatch):
    task = ask(conn, "用 Python 翻译路线复现一遍")
    text = call(conn, "run_replication", {"plan": listed, "action": "python"}, task)
    assert text.startswith("not proposed") and "复现目标还没审阅" in text and not CH.batches(conn, task_id=task)
    monkeypatch.setattr(rrun, "find_rscript", lambda: None)
    assert "brew install r" in call(conn, "run_replication", {"plan": listed, "action": "r_run"}, ask(conn, "跑作者的 R 代码"))
    assert "没有可以对照的运行" in call(conn, "run_replication", {"plan": listed, "action": "compare"}, ask(conn, "复现重新对照"))


def test_the_r_route_runs_through_rrun_after_the_click_and_stops_at_once(conn, listed, monkeypatch):
    spawned, stopped = [], []
    monkeypatch.setattr(rrun, "find_rscript", lambda: "/usr/local/bin/Rscript")
    monkeypatch.setattr(rrun, "spawn", lambda c, pid, kind: spawned.append(kind))
    task = ask(conn, "这篇复现走 R，跑作者的 R 代码")
    call(conn, "run_replication", {"plan": listed, "action": "r_run"}, task)
    assert spawned == []
    CH.apply(conn, batch_of(conn, task)["batch_id"])
    assert spawned == ["run"]
    monkeypatch.setattr(rrun, "busy", lambda pid: True)
    monkeypatch.setattr(rrun, "stop", lambda pid: stopped.append(pid) or True)
    monkeypatch.setattr(W, "current_job", lambda c, pid: None)
    text = call(conn, "stop_replication", {"plan": listed, "what": "r"}, ask(conn, "停掉 R"))
    assert stopped == [listed] and "已停止 R 这一步" in text and "撤销" not in text


def test_the_deck_is_regenerated_from_a_card(conn, listed, monkeypatch):
    from research_lab.replication import deck
    made = []
    monkeypatch.setattr(deck, "make", lambda c, pid, **k: made.append(pid) or type("D", (), {"slides": 12, "notes": []})())
    task = ask(conn, "重新生成展示稿")
    assert "card waiting" in call(conn, "run_replication", {"plan": listed, "action": "deck"}, task) and not made
    out = CH.apply(conn, batch_of(conn, task)["batch_id"])
    assert made == [listed] and out["applied"][0]["note"] == "展示稿已生成，共 12 页"


# ---------------------------------------------------------------- routing

@pytest.mark.parametrize("text", ["只复现表 2 和图 3", "Jost 那篇复现进度怎么样了", "把这篇一键跑完", "重新生成展示稿",
                                  "停掉 R", f"给 Jost 复现加个数据链接 {URL}", "用 Python 翻译路线跑一遍复现"])
def test_replication_sentences_reach_the_skill(text):
    assert skills.pick(text).name == "replication-control"


@pytest.mark.parametrize("text, skill", [("给这篇做一个复现方案", "methods"),
                                         ("这篇文章的研究设计和变量是怎么操作化的", "methods"),
                                         ("把复现那条子议题改成进行中", "issues")])
def test_plans_designs_and_issues_stay_where_they_were(text, skill):
    assert skills.pick(text).name == skill
    assert skills.pick("复现那条这周做完").name != "replication-control"
