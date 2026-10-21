"""Card 0078: the confirmation card under an assistant reply (docs/design/ISSUE-AGENT.md §5) and its
执行 / 不用了 / 撤销 posts, and the §6 progress card. The writes themselves are pm/changes.py (card 0075)."""
from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from research_lab.chat import service
from research_lab.files import issues as I, tasks as T
from research_lab.files.weeks import week_add, week_of
from research_lab.pm import changes as CH

PROJECT = "asean-centrality"


@pytest.fixture()
def db(client):
    from research_lab.db.connection import connect
    c = connect()
    yield c
    c.close()


def visible(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def new(title, parent="", **props):
    ref = I.create(title, PROJECT, parent, I.revision(parent))
    for field, value in props.items():
        I.update(ref, field, value, I.revision(ref))
    return ref


def conversation(db, prompt="把落后的都挪到下周", status="done"):
    """A thread with Leo's message and the assistant's task reply, as chat/router.start_task leaves them."""
    tid = service.create_thread(db, "排期")
    service.add_message(db, tid, "user", prompt)
    task_id = f"AT-{tid[-4:]}c0ffee"
    msg = service.add_message(db, tid, "assistant", "", card_kind="task", card_ref=task_id)
    db.execute("INSERT INTO agent_tasks(id, thread_id, message_id, skill, status, prompt, created_at) "
               "VALUES (?, ?, ?, 'issues', ?, ?, 'x')", (task_id, tid, msg.id, status, prompt))
    return tid, task_id, msg.id


def message(client, mid) -> str:
    r = client.get(f"/chat/message/{mid}")
    assert r.status_code == 200
    return r.text


def card(html: str, batch: str) -> str:
    start = html.index(f'id="plan-{batch}"')
    return html[html.rindex("<div", 0, start):]


def lines_of(ref):
    return [c["text"] for c in I.comments(ref) if c.get("by") == "assistant"]


@pytest.fixture()
def late(db):
    """The §5 mockup: three overdue tasks and one postponed three times already, a card moving them to next Friday."""
    today, nxt = date.today(), T.next_friday()
    refs = []
    for title, back, postponed in (("读 Acharya 第三章", 14, 2), ("整理东盟会议纪要", 7, 0), ("第三节", 7, 0), ("数据清洗", 21, 3)):
        refs.append(new(title))
        T.update(refs[-1], {"due": (today - timedelta(days=back)).isoformat()}, adjust_postponed=postponed)
    tid, task_id, mid = conversation(db)
    items = [CH.change_item(r, "due", T.get(r)["due"], nxt) for r in refs]
    batch = CH.propose(db, items, task_id=task_id, reason="已经逾期")
    return dict(refs=refs, tid=tid, task=task_id, mid=mid, batch=batch, next=nxt,
                ids=[r["id"] for r in CH.rows(db, batch)])


def apply(client, s, ticked, **extra):
    data = {"shown": ",".join(map(str, s["ids"])), "ids": [str(i) for i in ticked], **extra}
    return client.post(f"/chat/changes/{s['batch']}/apply", data=data)


# ---------------------------------------------------------------- render states

def test_proposed_card_reads_like_the_mockup(client, db, late):
    html = card(message(client, late["mid"]), late["batch"])
    text = visible(html)
    assert "助手想做 4 处修改" in text and "来自「把落后的都挪到下周」" in text
    nxt = I.display("due", late["next"])
    old = I.display("due", (date.today() - timedelta(days=14)).isoformat())
    assert f"读 Acharya 第三章 截止 {old} → 改为 {nxt}" in text
    assert "截止已推迟 2 次" in text and "截止已推迟 3 次，也可以搁置" in text
    boxes = re.findall(r'<input type="checkbox" class="plan-tick" name="ids" value="(\d+)"([^>]*)>', html)
    assert [int(v) for v, _ in boxes] == late["ids"]
    assert [" checked" in rest for _, rest in boxes] == [True, True, True, False]       # the fourth move starts unticked
    assert "Apply <span data-plan-count>3</span>" in html and "Dismiss" in text
    assert f'hx-post="/chat/changes/{late["batch"]}/apply"' in html and f'hx-target="#plan-{late["batch"]}"' in html
    assert not re.search(r"\b(T-20\d\d|AC-|AT-|PRJ-)", text)               # titles only, never ids


def test_card_hangs_under_an_attached_message_and_in_the_dock(client, db, late):
    other = service.add_message(db, late["tid"], "assistant", "好的，这些等你确认。")
    CH.attach(db, late["batch"], other.id)
    assert f'id="plan-{late["batch"]}"' in message(client, other.id)
    assert f'id="plan-{late["batch"]}"' not in message(client, late["mid"])      # attached elsewhere: shown once
    dock = client.get(f"/chat/dock?thread={late['tid']}").text
    assert dock.count(f'id="plan-{late["batch"]}"') == 1 and "助手想做 4 处修改" in visible(dock)
    assert f'id="plan-{late["batch"]}"' in client.get(f"/chat/{late['tid']}").text


def test_create_rows_have_an_editable_title(client, db):
    parent = new("第二章")
    tid, task_id, mid = conversation(db, "把第二章拆成两个子议题")
    batch = CH.propose(db, [CH.create_item("补 Acharya 第三章的笔记", parent=parent, week=week_add(week_of(), 1)),
                            CH.create_item("核对会议纪要", parent=parent)], task_id=task_id)
    html = card(message(client, mid), batch)
    first = CH.rows(db, batch)[0]["id"]
    assert f'name="title-{first}" value="补 Acharya 第三章的笔记"' in html
    assert "新建子议题" in visible(html) and "在「第二章」下" in visible(html)


def test_card_waits_while_the_task_runs(client, db, late):
    db.execute("UPDATE agent_tasks SET status = 'running' WHERE id = ?", (late["task"],))
    html = card(message(client, late["mid"]), late["batch"])
    assert "助手还在处理，结束后可以执行" in visible(html)
    assert re.search(r"data-plan-apply disabled", html) and html.count("disabled") >= 6
    r = apply(client, late, late["ids"])
    assert "助手还在处理，结束后再执行" in visible(r.text)
    assert {x["status"] for x in CH.rows(db, late["batch"])} == {"proposed"}
    db.execute("UPDATE agent_tasks SET status = 'done' WHERE id = ?", (late["task"],))
    assert "data-plan-apply disabled" not in card(message(client, late["mid"]), late["batch"])


# ---------------------------------------------------------------- 执行

def test_apply_only_ticked_rows_with_edited_titles(client, db):
    parent = new("第二章")
    tid, task_id, mid = conversation(db, "第二章拆一下，再把它改成进行中")
    batch = CH.propose(db, [CH.create_item("补 Acharya 第三章的笔记", parent=parent),
                            CH.create_item("核对会议纪要", parent=parent),
                            CH.change_item(parent, "status", "todo", "doing")], task_id=task_id)
    a, b, c = (r["id"] for r in CH.rows(db, batch))
    r = client.post(f"/chat/changes/{batch}/apply",
                    data={"shown": f"{a},{b},{c}", "ids": [str(a), str(c)], f"title-{a}": "  补 Acharya 第三、四章的笔记 ",
                          f"title-{b}": "没勾选的标题不算"})
    assert r.status_code == 200
    text = visible(r.text)
    assert "助手已改 2 处" in text and "另有 1 处没勾选，没有执行" in text and "Undo" in text
    assert [t["title"] for t in T.children(parent)] == ["补 Acharya 第三、四章的笔记"]
    assert T.get(parent)["status"] == "doing"
    assert [x["status"] for x in CH.rows(db, batch)] == ["applied", "dismissed", "applied"]
    child = T.children(parent)[0]
    assert f'href="/issues/{child["id"]}"' in r.text                     # the new issue links to its page
    assert r.text.lstrip().startswith("<div") and f'id="plan-{batch}"' in r.text   # just the card, for the swap


@pytest.mark.parametrize("bad, problem", [("   ", "标题不能空着"), ("长" * 121, "标题不要超过 120 个字")])
def test_a_cleared_or_too_long_title_keeps_the_card_waiting(client, db, bad, problem):
    parent = new("第二章")
    tid, task_id, mid = conversation(db, "把第二章拆成两个子议题")
    batch = CH.propose(db, [CH.create_item("补 Acharya 第三章的笔记", parent=parent),
                            CH.create_item("核对会议纪要", parent=parent),
                            CH.change_item(parent, "priority", "none", "high")], task_id=task_id)
    a, b, c = (r["id"] for r in CH.rows(db, batch))
    assert 'maxlength="120"' in card(message(client, mid), batch)
    r = client.post(f"/chat/changes/{batch}/apply",
                    data={"shown": f"{a},{b},{c}", "ids": [str(a), str(c)], f"title-{a}": bad, f"title-{b}": "第二条"})
    text = visible(r.text)
    assert r.status_code == 200 and "有新议题的标题需要改一下" in text and problem in text and "助手想做 3 处修改" in text
    assert {x["status"] for x in CH.rows(db, batch)} == {"proposed"}                 # nothing written, nothing dismissed
    assert T.children(parent) == [] and T.get(parent).get("priority") != "high"
    # drawn as Leo left it: his ticks and titles, the bad box marked
    boxes = re.findall(r'class="plan-tick" name="ids" value="(\d+)"([^>]*)>', r.text)
    assert [" checked" in rest for _, rest in boxes] == [True, False, True]
    assert re.search(rf'name="title-{a}" value="{bad.strip()}" maxlength="120"\s+aria-label="新议题的标题" data-plan-title aria-invalid="true"', r.text)
    assert f'name="title-{b}" value="第二条"' in r.text
    # an unticked row's empty title does not block the rest
    ok = client.post(f"/chat/changes/{batch}/apply",
                     data={"shown": f"{a},{b},{c}", "ids": [str(b), str(c)], f"title-{a}": "", f"title-{b}": "第二条"})
    assert "助手已改 2 处" in visible(ok.text) and [t["title"] for t in T.children(parent)] == ["第二条"]


def test_forged_ids_are_ignored(client, db, late):
    shown = ",".join(map(str, late["ids"])) + ",²,x"
    r = client.post(f"/chat/changes/{late['batch']}/apply",
                    data={"shown": shown, "ids": ["²", "١", "abc", str(late["ids"][0])]})
    assert r.status_code == 200 and "助手已改 1 处" in visible(r.text)


def test_an_earlier_due_date_is_no_postponement_and_stays_ticked(client, db):
    ref = new("数据清洗")
    far, sooner = (date.today() + timedelta(days=21)).isoformat(), (date.today() + timedelta(days=7)).isoformat()
    T.update(ref, {"due": far}, adjust_postponed=3)
    tid, task_id, mid = conversation(db, "数据清洗提前到下周")
    batch = CH.propose(db, [CH.change_item(ref, "due", far, sooner)], task_id=task_id)
    html = card(message(client, mid), batch)
    assert "截止已推迟 3 次" in visible(html) and "也可以搁置" not in visible(html)
    assert re.search(r'class="plan-tick" name="ids" value="\d+"[^>]* checked', html)


def test_cap_failure_reason_points_to_tomorrow():
    from research_lab.web.routes.chat import failure_reason
    assert failure_reason("daily cap $0.50 reached") == "今天的用量上限到了，明天自动恢复"


def test_double_apply_applies_once(client, db, late):
    first = apply(client, late, late["ids"][:3])
    second = apply(client, late, late["ids"][:3])
    assert first.status_code == second.status_code == 200
    assert "这批修改已经处理过了" in visible(second.text) and "这批修改已经处理过了" not in visible(first.text)
    t = T.get(late["refs"][0])
    assert t["due"] == late["next"] and t["postponed"] == 3             # 2 before, one move
    assert len(lines_of(late["refs"][0])) == 1
    assert T.get(late["refs"][3])["due"] != late["next"]                # unticked: left alone


def test_apply_skips_and_marks_what_leo_changed_since(client, db, late):
    ref = late["refs"][1]
    mine = (date.today() + timedelta(days=30)).isoformat()
    I.update(ref, "due", mine, I.revision(ref))                         # Leo, on the issue page, after the card
    html = card(message(client, late["mid"]), late["batch"])
    assert f"你已经改成了 {I.display('due', mine)}，执行时会跳过" in visible(html)
    r = apply(client, late, late["ids"][:3])
    text = visible(r.text)
    assert f"这条你刚改成了 {I.display('due', mine)}，我没动" in text
    assert "助手已改 2 处" in text and T.get(ref)["due"] == mine


def test_a_row_added_after_the_card_was_drawn_is_not_applied_blind(client, db, late):
    CH.propose(db, [CH.change_item(late["refs"][0], "priority", "none", "high")], task_id=late["task"], batch_id=late["batch"])
    r = apply(client, late, late["ids"])
    assert "请再看一眼再执行" in visible(r.text) and "助手想做 5 处修改" in visible(r.text)
    assert {x["status"] for x in CH.rows(db, late["batch"])} == {"proposed"}
    assert client.post(f"/chat/changes/{late['batch']}/apply", data={"ids": [str(late["ids"][0])]}).status_code == 400


# ---------------------------------------------------------------- 不用了 · 撤销

def test_dismiss_then_nothing_to_apply(client, db, late):
    r = client.post(f"/chat/changes/{late['batch']}/dismiss")
    text = visible(r.text)
    assert r.status_code == 200 and "没有执行：你选了不用了" in text and "执行" not in text.replace("没有执行", "")
    assert 'class="chat-card chat-card-plan is-dismissed"' in r.text
    assert "这批修改已经处理过了" in visible(apply(client, late, late["ids"]).text)
    assert T.get(late["refs"][0])["due"] != late["next"]


def test_direct_change_shows_undo_and_undo_keeps_what_leo_changed(client, db):
    ref = new("第二章")
    tid, task_id, mid = conversation(db, "第二章改成进行中，优先级调高")
    out = CH.apply_now(db, [CH.change_item(ref, "status", "todo", "doing"),
                            CH.change_item(ref, "priority", "none", "high")], task_id=task_id)
    html = card(message(client, mid), out["batch_id"])
    assert "助手已改 2 处" in visible(html) and f'hx-post="/chat/changes/{out["batch_id"]}/undo"' in html
    I.update(ref, "status", "done", I.revision(ref))                    # Leo changed one of them afterwards
    r = client.post(f"/chat/changes/{out['batch_id']}/undo")
    text = visible(r.text)
    assert "已撤销 1 处修改；1 处你后来改过，留着没动" in text
    assert "这条你后来改成了 已完成，我没动" in text and "/undo" not in r.text      # nothing left to undo
    assert T.get(ref)["status"] == "done" and T.get(ref)["priority"] == "none"
    again = client.post(f"/chat/changes/{out['batch_id']}/undo")
    assert "这批修改已经处理过了" in visible(again.text) and T.get(ref)["priority"] == "none"


def test_undone_card_is_plain(client, db, late):
    apply(client, late, late["ids"][:3])
    r = client.post(f"/chat/changes/{late['batch']}/undo")
    assert "已撤销 3 处修改" in visible(r.text) and "is-undone" in r.text and "/undo" not in r.text
    t = T.get(late["refs"][0])
    assert t["due"] == (date.today() - timedelta(days=14)).isoformat() and t["postponed"] == 2


def test_rows_aimed_at_an_issue_an_undo_deleted_are_shown_settled(client, db):
    parent, other = new("第二章"), new("引言")
    tid, task_id, mid = conversation(db, "在第二章下加一个子议题：补笔记")
    made = CH.apply_now(db, [CH.create_item("补笔记", parent=parent)], task_id=task_id)
    child = made["applied"][0]["target"]
    card_b = CH.propose(db, [CH.change_item(child, "status", "todo", "doing"),
                             CH.change_item(other, "status", "todo", "doing")], task_id=task_id)
    client.post(f"/chat/changes/{made['batch_id']}/undo")
    assert T.get(child) is None
    html = card(message(client, mid), card_b)
    text = visible(html)
    assert "助手想做 1 处修改" in text and "这条议题已经撤销删除" in text and "plan-settled" in html
    assert len(re.findall(r'class="plan-tick"', html)) == 1
    undone = visible(card(message(client, mid), made["batch_id"]))
    assert "已撤销 1 处修改" in undone and "新建子议题「补笔记」" in undone


def test_applied_rows_show_the_values_really_written(client, db):
    ref = new("第二章")
    tid, task_id, mid = conversation(db, "第二章改成进行中")
    out = CH.apply_now(db, [CH.change_item(ref, "status", "待办", "进行中")], task_id=task_id)
    assert CH.rows(db, out["batch_id"])[0]["detail"]["new"] == "doing"
    assert "状态 待办 → 改为 进行中" in visible(card(message(client, mid), out["batch_id"]))


def test_long_applied_card_folds_plain_rows(client, db):
    refs = [new(f"第 {i} 节") for i in range(5)]
    tid, task_id, mid = conversation(db, "这五节都改成进行中")
    out = CH.apply_now(db, [CH.change_item(r, "status", "todo", "doing") for r in refs], task_id=task_id)
    html = card(message(client, mid), out["batch_id"])
    assert "<details class=\"plan-fold\"><summary>查看 5 处</summary>" in html


# ---------------------------------------------------------------- no model, guard, unknown

def test_works_while_ai_is_paused_and_the_cap_is_spent(client, db, late, monkeypatch):
    from research_lab.ai import dispatch
    from research_lab.db.connection import set_setting
    set_setting(db, dispatch.PAUSE_KEY, True)
    set_setting(db, "ai.daily_cap_usd", 0)

    def no_model(*a, **k):
        raise AssertionError("a confirmation card never calls a model")
    monkeypatch.setattr(dispatch, "dispatch", no_model)
    assert apply(client, late, late["ids"][:1]).status_code == 200
    assert T.get(late["refs"][0])["due"] == late["next"]
    assert client.post(f"/chat/changes/{late['batch']}/undo").status_code == 200
    assert T.get(late["refs"][0])["due"] == (date.today() - timedelta(days=14)).isoformat()


def test_posts_need_the_app_origin(client, db, late):
    data = {"shown": ",".join(map(str, late["ids"])), "ids": [str(late["ids"][0])]}
    url = f"/chat/changes/{late['batch']}/apply"
    del client.headers["HX-Request"]
    for headers in ({}, {"Origin": "https://evil.example"}):
        assert client.post(url, data=data, headers=headers).status_code == 403
        assert client.post(f"/chat/changes/{late['batch']}/dismiss", headers=headers).status_code == 403
    assert {x["status"] for x in CH.rows(db, late["batch"])} == {"proposed"}
    # the app's own page without htmx: applied, then back to the conversation
    r = client.post(url, data=data, headers={"Origin": "http://127.0.0.1:4100"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/chat/{late['tid']}#plan-{late['batch']}"
    assert T.get(late["refs"][0])["due"] == late["next"]


def test_unknown_batch_is_404(client):
    for action in ("apply", "dismiss", "undo"):
        assert client.post(f"/chat/changes/AC-000000000000/{action}", data={"shown": ""}).status_code == 404


# ---------------------------------------------------------------- 进度卡

def test_progress_card(client, db):
    this = week_of()
    late_ref = new("读 Acharya 第三章", due=T.week_due(week_add(this, -1)))
    new("第二章", due=T.week_due(this), status="done")
    tid = service.create_thread(db, "本周进度")
    msg = service.add_message(db, tid, "assistant", "", card_kind="progress", card_ref="week")
    html = message(client, msg.id)
    text = visible(html)
    assert f"{I.display('week', this)}进度" in text and "截至" in text
    assert "本周 2 项完成 1 项" in text                                  # 本周: the overdue one plus the finished one
    assert f'href="/issues/{late_ref}"' in html and "逾期" in text and "计划周" not in text and 'class="badge danger"' in html
    assert "要注意的共 1 条" in text and 'class="meter progress-meter"' in html
    proj = service.add_message(db, tid, "assistant", "", card_kind="progress", card_ref=f"project:{PROJECT}")
    assert "读 Acharya 第三章" in visible(message(client, proj.id))
    one = service.add_message(db, tid, "assistant", "", card_kind="progress", card_ref=f"issue:{late_ref}")
    assert "读 Acharya 第三章 · 进度" in visible(message(client, one.id))
    for bad in ("project:no-such-project", "issue:T-1999-0001"):
        m = service.add_message(db, tid, "assistant", "", card_kind="progress", card_ref=bad)
        assert "已经不存在了" in visible(message(client, m.id))


def test_progress_ref_parsing():
    from datetime import date
    from research_lab.chat.cards import progress_ref
    assert progress_ref("week") == ("week", "", None)
    assert progress_ref("project:asean@2026-10-08") == ("project", "asean", date(2026, 10, 8))
    assert progress_ref("issue:T-2026-0001@bad") == ("issue", "T-2026-0001", None)
    assert progress_ref("nonsense:x") == ("week", "", None)
