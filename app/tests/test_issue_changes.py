"""The assistant's write layer (docs/design/ISSUE-AGENT.md §4, §5): field-checked changes and creates
through files/issues.py, recorded, applied, dismissed and undone through pm/changes.py."""
from __future__ import annotations

import pytest

from research_lab.files import artifacts as A, issues as I, tasks as T, updates as U
from research_lab.files.weeks import week_add, week_of
from research_lab.files.yamlio import StaleFileError
from research_lab.pm import changes as CH

THREAD, TASK = "T-2026-0901", "AT-feedbeef01"


@pytest.fixture()
def task(conn):
    conn.execute("INSERT INTO chat_threads(id, title, created_at, updated_at) VALUES (?, '第二章安排', 'x', 'x')", (THREAD,))
    conn.execute("INSERT INTO agent_tasks(id, thread_id, skill, status, prompt, created_at) "
                 "VALUES (?, ?, 'issues', 'done', '把第二章改成进行中', 'x')", (TASK, THREAD))
    return TASK


def new(title="第二章", parent="", project="asean-centrality"):
    return I.create(title, project, parent, I.revision(parent))


def assistant_lines(ref):
    return [c for c in I.comments(ref) if c.get("by") == "assistant"]


def test_direct_change_writes_fields_one_activity_line_and_the_audit(conn, task):
    ref = new()
    out = CH.apply_now(conn, [CH.change_item(ref, "status", "todo", "doing"),
                              CH.change_item(ref, "priority", "none", "high")], task_id=TASK, reason="Leo 点名")
    assert T.get(ref)["status"] == "doing" and T.get(ref)["priority"] == "high"
    assert len(out["applied"]) == 2 and not out["already"]
    (line,) = assistant_lines(ref)                      # one request, one issue, one line
    assert line["text"] == "状态 待办 → 进行中；优先级 无优先级 → 高 · 来自对话《第二章安排》"
    assert line["link"] == f"/chat/{THREAD}"
    rows = CH.rows(conn, out["batch_id"])
    assert {r["status"] for r in rows} == {"applied"} and {r["thread_id"] for r in rows} == {THREAD}
    assert rows[0]["label"] == "第二章" and rows[0]["old"] == "todo" and rows[0]["new"] == "doing"
    assert rows[0]["applied_at"] and rows[0]["reason"] == "Leo 点名"
    (b,) = CH.batches(conn, thread_id=THREAD)
    assert b["status"] == "applied" and b["undoable"] and CH.last_batch(conn, THREAD) == out["batch_id"]
    assert CH.summary(out) == "已改 2 处。"


def test_field_check_skips_only_what_leo_changed(conn, task):
    ref, other = new(), new("另一条")
    batch = CH.propose(conn, [CH.change_item(ref, "status", "todo", "doing"),
                              CH.change_item(ref, "due", "", "2026-12-01")], task_id=TASK)
    I.update(ref, "status", "parked", I.revision(ref))           # Leo, on the issue page
    I.update(other, "priority", "high", I.revision(other))         # elsewhere in tasks.yml: not a conflict
    out = CH.apply(conn, batch)
    assert T.get(ref)["status"] == "parked" and T.get(ref)["due"] == "2026-12-01"
    (skipped,) = out["skipped"]
    assert skipped["note"] == "这条你刚改成了 搁置，我没动" and skipped["status"] == "dismissed"
    assert skipped["result"] == "conflict" and skipped["detail"]["current"] == "parked"
    assert [r["field"] for r in out["applied"]] == ["due"]
    assert CH.summary(out) == "已改 1 处；「第二章」的状态：这条你刚改成了 搁置，我没动。"


def test_apply_twice_applies_once_and_postpones_once(conn, task):
    ref = new()
    T.update(ref, {"due": "2030-10-04"})
    batch = CH.propose(conn, [CH.change_item(ref, "due", "2030-10-04", "2030-10-11")], task_id=TASK)
    first, second = CH.apply(conn, batch), CH.apply(conn, batch)
    assert len(first["applied"]) == 1 and second["already"] and CH.summary(second) == "这批修改已经处理过了。"
    assert T.get(ref)["due"] == "2030-10-11" and T.get(ref)["postponed"] == 1
    assert len(assistant_lines(ref)) == 1
    assert CH.rows(conn, batch)[0]["detail"] == {"old": "2030-10-04", "new": "2030-10-11", "postponed": 1}


def test_a_week_from_the_assistant_becomes_a_due_date_item(conn, task):
    """Rows stored before card 0093 may still say week: change_item and create_item translate them to a due date.
    change_fields itself takes no week any more (card 0107 removed the assistant's compatibility layer)."""
    ref = new()
    T.update(ref, {"due": "2030-10-04"})
    item = CH.change_item(ref, "week", "2030-W40", "2030-W45")
    assert (item["field"], item["old"], item["new"]) == ("due", "2030-10-04", T.friday("2030-W45"))
    made = CH.create_item("新的", parent=ref, week="2030-W45")
    assert "week" not in made["new"] and made["new"]["due"] == T.friday("2030-W45")
    out = I.change_fields(ref, [("week", "2030-W40", "2030-W46")])
    assert [f["field"] for f in out["failed"]] == ["week"] and T.get(ref)["due"] == "2030-10-04"


def test_card_rows_can_be_unticked_edited_or_dismissed(conn, task):
    parent = new()
    batch = CH.propose(conn, [CH.create_item("补 Acharya 第三章的笔记", parent=parent, week=week_add(week_of(), 1)),
                              CH.create_item("核对会议纪要", parent=parent)], task_id=TASK)
    first, second = (r["id"] for r in CH.rows(conn, batch))
    out = CH.apply(conn, batch, ids=[first], edits={first: "补 Acharya 第三、四章的笔记"})
    assert CH.apply(conn, batch, edits={first: "第二次点击"})["already"]                   # a second click changes nothing
    (made,) = out["applied"]
    assert [r["id"] for r in out["dismissed"]] == [second]
    child = T.get(made["target"])
    assert child["title"] == "补 Acharya 第三、四章的笔记" and child["origin"] == "assistant" and child["parent"] == parent
    assert [t["title"] for t in T.children(parent)] == ["补 Acharya 第三、四章的笔记"]
    assert assistant_lines(made["target"])[0]["text"] == "新建了这个议题 · 来自对话《第二章安排》"
    with pytest.raises(ValueError):
        CH.revise(conn, first, "已经执行过的不能再改")
    other = CH.propose(conn, [CH.change_item(parent, "status", "todo", "done")], task_id=TASK)
    assert CH.dismiss(conn, other) == 1 and CH.apply(conn, other)["already"]
    assert T.get(parent)["status"] == "todo" and CH.batches(conn, task_id=TASK)[-1]["status"] == "dismissed"


def test_undo_restores_values_and_the_postponement_count(conn, task):
    ref = new()
    T.update(ref, {"due": "2030-10-04"})
    out = CH.apply_now(conn, [CH.change_item(ref, "due", "2030-10-04", "2030-10-11"), CH.change_item(ref, "status", "todo", "doing"),
                              CH.change_item(ref, "labels", [], ["写作"])], task_id=TASK)
    assert T.get(ref)["postponed"] == 1
    back = CH.undo(conn, out["batch_id"])
    t = T.get(ref)
    assert (t["due"], t["postponed"], t["status"], t["labels"]) == ("2030-10-04", 0, "todo", [])
    assert len(back["undone"]) == 3 and CH.summary(back) == "已撤销 3 处。"
    assert assistant_lines(ref)[-1]["text"].startswith("撤销：")
    assert CH.undo(conn, out["batch_id"])["already"] and CH.last_batch(conn, THREAD) is None
    assert {r["status"] for r in CH.rows(conn, out["batch_id"])} == {"undone"}


def test_undo_leaves_alone_what_leo_changed_afterwards(conn, task):
    ref = new()
    out = CH.apply_now(conn, [CH.change_item(ref, "status", "todo", "doing"),
                              CH.change_item(ref, "priority", "none", "high")], task_id=TASK)
    I.update(ref, "status", "done", I.revision(ref))
    back = CH.undo(conn, out["batch_id"])
    assert T.get(ref)["status"] == "done" and T.get(ref)["priority"] == "none"
    (kept,) = back["kept"]
    assert kept["field"] == "status" and kept["status"] == "applied" and kept["note"] == "这条你后来改成了 已完成，我没动"
    assert CH.summary(back) == "已撤销 1 处；「第二章」的状态：这条你后来改成了 已完成，我没动。"
    assert CH.undo(conn, out["batch_id"])["already"] and CH.last_batch(conn, THREAD) is None    # left alone for good


def test_undo_deletes_an_untouched_created_issue_and_parks_a_touched_one(conn, task):
    parent = new()
    out = CH.apply_now(conn, [CH.create_item("没人动过", parent=parent), CH.create_item("Leo 评论过", parent=parent),
                              CH.create_item("Leo 完成了", parent=parent)], task_id=TASK)
    untouched, commented, finished = (r["target"] for r in out["applied"])
    I.comment(commented, "这条我要留着。", I.revision(commented))
    I.update(finished, "status", "done", I.revision(finished))
    back = CH.undo(conn, out["batch_id"])
    assert T.get(untouched) is None and set(I._metadata()[untouched]) == {"retired"}       # a tombstone
    assert T.get(commented)["status"] == "parked" and T.get(finished)["status"] == "done"
    results = {r["target"]: (r["status"], r["result"]) for r in CH.rows(conn, out["batch_id"])}
    assert results == {untouched: ("undone", "deleted"), commented: ("undone", "parked"), finished: ("applied", "kept")}
    assert T.get(parent) is not None and [t["id"] for t in T.children(parent)] == [commented, finished]


def test_remove_is_only_for_untouched_assistant_tasks(repo):
    mine = new("Leo 自己建的")
    with pytest.raises(T.TaskError):
        T.remove(mine)
    ref, _ = I.create_checked("助手建的", parent=mine)
    with pytest.raises(T.TaskError):
        T.remove(mine)                                     # has a sub-task now, and is Leo's anyway
    assert I.remove_created(mine) == "kept" and T.get(mine)["status"] == "todo"
    assert I.remove_created(ref, I.created_state(ref)) == "deleted" and I.remove_created(ref) == "gone"


def test_output_issue_lines_go_to_its_record_and_link_back(conn, task, client):
    path = A.create("evidence", "assistant-touch", "us-iran-talks", week=week_of())
    ref = A.load(path).ref
    before = path.read_bytes()
    CH.apply_now(conn, [CH.change_item(ref, "status", "todo", "doing")], task_id=TASK)
    assert I.get(ref)["status"] == "doing" and path.read_bytes() == before
    (u,) = U.for_target(ref)
    assert u["by"] == "assistant" and u["link"] == f"/chat/{THREAD}" and u["text"].startswith("状态 待办 → 进行中")
    assert f'<a href="/chat/{THREAD}">状态 待办 → 进行中' in client.get("/issues/" + ref).text


def test_research_content_is_out_of_reach(conn, task, repo):
    ref = next(r["id"] for r in I.read() if r["artifact"])
    doc = I._documents()[ref]
    before, projects = doc.path.read_bytes(), (repo / "_data/projects.yml").read_bytes()
    root = I.project_ref("asean-centrality")
    out = I.change_fields(ref, [("Claim", "", "AI 写的主张"), ("level", "seed", "paper-ready"), ("assignee", "", "助手")])
    assert [f["field"] for f in out["failed"]] == ["Claim", "level", "assignee"] and not out["applied"]
    out = I.change_fields(root, [("title", "ASEAN Centrality", "新名字"), ("description", "", "新研究问题"),
                                 ("project", "asean-centrality", "us-iran-talks")])
    assert len(out["failed"]) == 3 and not out["applied"]
    assert doc.path.read_bytes() == before and (repo / "_data/projects.yml").read_bytes() == projects
    assert not assistant_lines(ref) and not U.for_target(ref)


def test_validation_runs_before_writes_and_bad_values_never_land(repo):
    ref = new()
    out = I.change_fields(ref, [("status", "todo", "doing"), ("week", week_of(), "2026-W60"), ("labels", [], ["伪造"])])
    assert [f["field"] for f in out["failed"]] == ["week", "labels"] and [a["field"] for a in out["applied"]] == ["status"]
    assert T.get(ref)["due"] is None and not T.get(ref).get("labels")
    out = I.change_fields(ref, [("status", "进行中", "进行中"), ("priority", "无优先级", "高")])   # labels read as keys
    assert out["same"] == [{"field": "status", "value": "doing"}] and T.get(ref)["priority"] == "high"
    with pytest.raises(I.IssueError):
        I.change_fields("T-2026-9999", [("status", "todo", "doing")])


def test_a_concurrent_write_is_retried_not_lost(repo, monkeypatch):
    ref = new()
    real, calls = I.update, []

    def racing(*args, **kw):
        calls.append(args)
        if len(calls) == 1:
            T.update(ref, {"note": "Leo 正在写"})          # Leo saves another field mid-way
            raise StaleFileError("tasks.yml changed")
        return real(*args, **kw)

    monkeypatch.setattr(I, "update", racing)
    out = I.change_fields(ref, [("status", "todo", "doing")])
    assert len(calls) == 2 and [a["field"] for a in out["applied"]] == ["status"]
    assert T.get(ref)["status"] == "doing" and T.get(ref)["note"] == "Leo 正在写"


def test_comments_through_the_assistant_and_they_are_never_undone(conn, task, client):
    ref = new()
    out = CH.apply_now(conn, [CH.comment_item(ref, "导师说第三节要压缩", by="user", via="assistant"),
                              CH.change_item(ref, "status", "todo", "doing")], task_id=TASK)
    note = next(c for c in I.comments(ref) if c["text"] == "导师说第三节要压缩")
    assert note["by"] == "user" and note["via"] == "assistant" and note["link"] == f"/chat/{THREAD}"
    assert "经助手录入" in client.get("/issues/" + ref).text
    CH.undo(conn, out["batch_id"])
    assert any(c["text"] == "导师说第三节要压缩" for c in I.comments(ref)) and T.get(ref)["status"] == "todo"
    statuses = {r["action"]: r["status"] for r in CH.rows(conn, out["batch_id"])}
    assert statuses == {"comment": "applied", "set": "undone"}
    with pytest.raises(I.IssueError):
        I.comment(ref, "x", I.revision(ref), link="https://example.com")
    with pytest.raises(U.UpdateError):
        U.add("ART-2026-4020", "assistant", "x", link="//example.com")


def test_other_kinds_plug_in_through_register(conn, task):
    calls = []

    def apply_fn(conn, rows, src):
        calls.append(("apply", [r["target"] for r in rows], src["title"]))
        return [{"id": r["id"], "status": "applied"} for r in rows]

    def undo_fn(conn, rows, src):
        calls.append(("undo", [r["target"] for r in rows]))
        return [{"id": r["id"], "status": "undone"} for r in rows]

    CH.register("test-setting", apply_fn, undo_fn)
    try:
        out = CH.apply_now(conn, [CH.change_item("feeds.news_ratio", "value", 0.3, 0.5, label="新闻比例")],
                           kind="test-setting", task_id=TASK)
        assert len(out["applied"]) == 1 and len(CH.undo(conn, out["batch_id"])["undone"]) == 1
        assert calls == [("apply", ["feeds.news_ratio"], "第二章安排"), ("undo", ["feeds.news_ratio"])]
        with pytest.raises(ValueError):
            CH.propose(conn, [CH.change_item("x", "y", 1, 2)], kind="nope")
    finally:
        CH._HANDLERS.pop("test-setting", None)


# ---------------------------------------------------------------- review fixes (0075 L2 review)

def test_undoing_a_move_to_an_earlier_date_is_not_a_postponement(conn, task):
    ref = new()
    T.update(ref, {"due": "2030-10-11"})
    out = CH.apply_now(conn, [CH.change_item(ref, "due", "2030-10-11", "2030-10-04")], task_id=TASK)
    assert T.get(ref)["postponed"] == 0 and CH.rows(conn, out["batch_id"])[0]["detail"]["postponed"] == 0
    CH.undo(conn, out["batch_id"])
    assert T.get(ref)["due"] == "2030-10-11" and T.get(ref)["postponed"] == 0


def test_a_deleted_id_is_never_issued_again_and_old_cards_aimed_at_it_drop(conn, task):
    parent = new()
    made = CH.apply_now(conn, [CH.create_item("会被撤销", parent=parent)], task_id=TASK)
    gone = made["applied"][0]["target"]
    old_card = CH.propose(conn, [CH.change_item(gone, "status", "todo", "done"),
                                 CH.create_item("挂在它下面", parent=gone)], task_id=TASK)
    assert CH.undo(conn, made["batch_id"])["undone"][0]["result"] == "deleted" and T.get(gone) is None
    assert {(r["status"], r["result"]) for r in CH.rows(conn, old_card)} == {("dismissed", "gone")}
    fresh = new("Leo 新建的")
    assert fresh != gone and gone in T.retired_ids()
    assert CH.apply(conn, old_card)["already"] and T.get(fresh)["status"] == "todo"


def test_no_snapshot_counts_as_touched(repo):
    parent = new()
    ref, _ = I.create_checked("没有快照", parent=parent)
    assert I.remove_created(ref) == "parked" and T.get(ref)["status"] == "parked"


def test_issue_changes_take_the_assistant_whitelist_whoever_the_actor(conn, task, repo):
    root = I.project_ref("asean-centrality")
    projects = (repo / "_data/projects.yml").read_bytes()
    with pytest.raises(ValueError):
        CH.propose(conn, [CH.change_item(root, "title", "ASEAN Centrality", "新名字")], task_id=TASK, actor="user")
    out = I.change_fields(root, [("title", "ASEAN Centrality", "新名字"), ("description", "", "新研究问题")], actor="user")
    assert len(out["failed"]) == 2 and (repo / "_data/projects.yml").read_bytes() == projects
    batch = CH.propose(conn, [CH.create_item("带负责人", parent=new(), assignee="Leo")], task_id=TASK)
    (failed,) = CH.apply(conn, batch)["failed"]
    assert failed["note"] == "助手不能设置这个属性。"


def test_leos_words_through_the_assistant_are_always_marked(conn, task):
    ref = new()
    raw = {"action": "comment", "target": ref, "field": "", "old": None, "label": "",
           "new": {"text": "原话一", "by": "user", "via": ""}}
    CH.apply_now(conn, [raw, CH.comment_item(ref, "原话二", by="user")], task_id=TASK)
    mine = [c for c in I.comments(ref) if c["by"] == "user"]
    assert [(c["text"], c["via"]) for c in mine] == [("原话一", "assistant"), ("原话二", "assistant")]


def test_undo_compares_against_what_was_really_stored(conn, task):
    ref = new()
    out = CH.apply_now(conn, [CH.change_item(ref, "title", "第二章", "第二章  补充")], task_id=TASK)
    assert T.get(ref)["title"] == "第二章 补充"
    assert CH.rows(conn, out["batch_id"])[0]["detail"] == {"old": "第二章", "new": "第二章 补充"}
    back = CH.undo(conn, out["batch_id"])
    assert len(back["undone"]) == 1 and not back["kept"] and T.get(ref)["title"] == "第二章"


def test_rows_are_recorded_as_they_land_so_a_crash_never_repeats_them(conn, task, monkeypatch):
    parent = new()
    batch = CH.propose(conn, [CH.create_item("第一条", parent=parent), CH.create_item("第二条", parent=parent)],
                       task_id=TASK)
    real, calls = I.create_checked, []

    def crash_on_second(*a, **kw):
        calls.append(a)
        if len(calls) == 2:
            raise RuntimeError("power cut")
        return real(*a, **kw)

    monkeypatch.setattr(I, "create_checked", crash_on_second)
    with pytest.raises(RuntimeError):
        CH.apply(conn, batch)
    monkeypatch.setattr(I, "create_checked", real)
    assert [r["status"] for r in CH.rows(conn, batch)] == ["applied", "proposed"]
    CH.apply(conn, batch)
    assert sorted(t["title"] for t in T.children(parent)) == ["第一条", "第二条"]


def test_undo_reports_a_failing_delete_per_row(conn, task, monkeypatch):
    out = CH.apply_now(conn, [CH.create_item("删不掉", parent=new())], task_id=TASK)

    def broken(*a, **kw):
        raise T.TaskError("待办文件读不出来")

    monkeypatch.setattr(I, "remove_created", broken)
    back = CH.undo(conn, out["batch_id"])
    (failed,) = back["failed"]
    assert failed["status"] == "applied" and failed["note"] == "待办文件读不出来"


def test_tombstone_keeps_what_leo_added_meanwhile(repo):
    ref = new()
    I.comment(ref, "我刚写的", I.revision(ref))
    I._retire(ref)
    entry = I._metadata()[ref]
    assert entry["comments"][0]["text"] == "我刚写的" and "retired" in entry


def test_output_activity_retries_a_stale_record_file(repo, monkeypatch):
    ref = next(r["id"] for r in I.read() if r["artifact"])
    real, calls = U.add, []

    def stale_once(*a, **kw):
        calls.append(a)
        if len(calls) == 1:
            raise StaleFileError("updates.yml changed")
        return real(*a, **kw)

    monkeypatch.setattr(U, "add", stale_once)
    assert I.activity(ref, "状态 待办 → 进行中") and len(calls) == 2
    assert all(len(c) > 3 and c[3] for c in calls)                      # each try passes the sha it read
    assert [u["text"] for u in U.for_target(ref)] == ["状态 待办 → 进行中"]


def test_dismiss_takes_ids_as_text(conn, task):
    batch = CH.propose(conn, [CH.change_item(new(), "status", "todo", "doing")], task_id=TASK)
    (row,) = CH.rows(conn, batch)
    assert CH.dismiss(conn, batch, ids=[str(row["id"])]) == 1
