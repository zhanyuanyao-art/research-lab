"""Card 0079: the Friday 本周进度 (pm/scheduler.py, docs/design/ISSUE-AGENT.md §6, §10b) and the preset
周一排期 skill. The digest is computed for a Friday three weeks on, so 进行中 issues are stalled without
touching any dates. No plan week since 0093: suggestions move a due date to the next Friday."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

import pytest

from research_lab.db.connection import get_setting, set_setting
from research_lab.files import issues as I
from research_lab.files.weeks import week_add, week_of, week_start
from research_lab.pm import changes as CH
from research_lab.pm import scheduler as S

PROJECT = "asean-centrality"
W0 = week_of()
W3 = week_add(W0, 3)
FRI = week_start(W3) + timedelta(days=4)
NEXT_FRI = (FRI + timedelta(days=7)).isoformat()


def day(d: date) -> str:
    return d.isoformat()


def at(day: date, h: int, m: int = 0) -> datetime:
    return datetime.combine(day, time(h, m))


def new(title, **props):
    ref = I.create(title, PROJECT, "", I.revision(""))
    for field, value in props.items():
        I.update(ref, field, value, I.revision(ref))
    return ref


@pytest.fixture()
def items(conn):
    late = new("读 Acharya 第三章", due=day(FRI - timedelta(days=15)))
    carried = new("数据清洗", due=day(FRI - timedelta(days=21)))
    I.update(carried, "due", day(FRI - timedelta(days=14)), I.revision(carried))
    I.update(carried, "due", day(FRI - timedelta(days=7)), I.revision(carried))   # postponed twice
    stalled = new("第二章", due=day(FRI), status="doing")
    overdue = new("会议纪要", due=day(FRI - timedelta(days=2)))
    calm = new("下周的事", due=day(FRI + timedelta(days=7)))
    open_now = new("本周还没做", due=day(FRI + timedelta(days=1)))
    done = new("本周做完了", due=day(FRI), status="done")
    return dict(late=late, carried=carried, stalled=stalled, overdue=overdue, calm=calm, open_now=open_now, done=done)


def rows_of(conn, batch):
    return {(r["target"], r["field"], r["new"]) for r in CH.rows(conn, batch)}


def per_kind(conn, out):
    return {kind: rows_of(conn, b) for kind, b in out["batches"].items()}


def test_due_window():
    assert not S.due(at(FRI - timedelta(days=1), 23, 59))         # Thursday
    assert not S.due(at(FRI, 16, 59))
    assert S.due(at(FRI, 17, 0)) and S.due(at(FRI, 17, 5))
    assert S.due(at(FRI + timedelta(days=1), 9)) and S.due(at(FRI + timedelta(days=2), 23, 59))   # catch-up
    assert not S.due(at(FRI + timedelta(days=3), 9))              # Monday: a new week


def test_sends_once_on_friday_with_suggestions_only(conn, items):
    before = {r["id"]: (r["due"], r["status"]) for r in I.read()}
    assert S.send_if_due(conn, at(FRI - timedelta(days=1), 17, 5)) is None     # Thursday
    out = S.send_if_due(conn, at(FRI, 17, 5))
    assert out and out["week"] == W3 and list(out["batches"]) == ["move", "week", "park"]
    assert get_setting(conn, f"pm.friday_sent:{W3}")["message_id"] == out["message_id"]
    msg = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (out["message_id"],)).fetchone()
    assert msg["role"] == "assistant" and msg["card_kind"] == "progress" and msg["card_ref"] == f"week@{FRI.isoformat()}"
    thread = conn.execute("SELECT * FROM chat_threads WHERE id = ?", (out["thread_id"],)).fetchone()
    assert thread["title"] == "本周进度"
    nxt = NEXT_FRI
    got = per_kind(conn, out)
    moves, week, parks = got["move"], got["week"], got["park"]
    assert {(items["late"], "due", nxt), (items["carried"], "due", nxt), (items["overdue"], "due", nxt)} <= moves
    assert {(items["carried"], "status", "parked"), (items["stalled"], "status", "parked")} <= parks
    # Friday ends Leo's week: what is due by Sunday may move too, on its own card (not the overdue one: it is above)
    assert {(items["open_now"], "due", nxt), (items["stalled"], "due", nxt)} <= week
    assert not {t for t, _, _ in week} & {items["overdue"], items["done"], items["late"], items["calm"]}
    mine = set(items.values())
    assert not any(t in (items["calm"], items["done"]) for t, _, _ in moves | parks)
    assert not any(t == items["stalled"] for t, f, _ in moves if f == "due")   # due this week: not overdue
    # suggestions only: every row proposed, nothing written to the files, no task and no model behind it
    all_rows = [r for b in out["batches"].values() for r in CH.rows(conn, b)]
    assert {r["status"] for r in all_rows} == {"proposed"} and {r["actor"] for r in all_rows} == {"assistant"}
    assert all(r["message_id"] == out["message_id"] and r["task_id"] is None for r in all_rows)
    assert {r["id"]: (r["due"], r["status"]) for r in I.read() if r["id"] in mine} == {k: v for k, v in before.items() if k in mine}
    assert conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0] == 0
    # once per week: later that evening and on Saturday nothing more
    assert S.send_if_due(conn, at(FRI, 18)) is None and S.send_if_due(conn, at(FRI + timedelta(days=1), 10)) is None
    assert conn.execute("SELECT COUNT(*) FROM chat_messages WHERE thread_id = ?", (out["thread_id"],)).fetchone()[0] == 1


def test_catch_up_on_saturday_and_thread_reuse(conn, items):
    sat = FRI + timedelta(days=1)
    first = S.send_if_due(conn, at(sat, 9, 30))           # the app was closed on Friday evening
    assert first and first["week"] == W3
    second = S.send_if_due(conn, at(FRI + timedelta(days=7), 17, 1))
    assert second and second["thread_id"] == first["thread_id"] and second["week"] == week_add(W3, 1)
    assert conn.execute("SELECT COUNT(*) FROM chat_threads WHERE title = '本周进度'").fetchone()[0] == 1
    # an archived conversation is not posted into: the next week starts a new one
    conn.execute("UPDATE chat_threads SET archived = 1 WHERE id = ?", (first["thread_id"],))
    third = S.send_if_due(conn, at(FRI + timedelta(days=14), 17, 1))
    assert third and third["thread_id"] != first["thread_id"]


def test_digest_off_sends_nothing(conn, items):
    set_setting(conn, "pm.friday_digest", False)
    assert S.send_if_due(conn, at(FRI, 17, 5)) is None
    assert get_setting(conn, f"pm.friday_sent:{W3}") is None          # turning it back on still catches up
    set_setting(conn, "pm.friday_digest", True)
    assert S.send_if_due(conn, at(FRI + timedelta(days=2), 20))


@pytest.mark.parametrize("rule,gone,kept", [
    # overdue covers the overdue moves, Friday's due-this-week moves and the postponed-twice 搁置 (pm.rule.slipped is gone)
    ("pm.rule.overdue", {("move", "overdue"), ("move", "late"), ("park", "carried"), ("week", "open_now")}, {("park", "stalled")}),
    ("pm.rule.stalled", {("park", "stalled")}, {("park", "carried"), ("move", "late")}),
])
def test_each_rule_toggle(conn, items, rule, gone, kept):
    set_setting(conn, rule, False)
    out = S.send_if_due(conn, at(FRI, 17, 5))
    got = {(kind, t) for kind, rows in per_kind(conn, out).items() for t, _, _ in rows}
    for kind, name in gone:
        assert (kind, items[name]) not in got
    for kind, name in kept:
        assert (kind, items[name]) in got
    if rule == "pm.rule.overdue":
        assert "week" not in out["batches"]


def test_all_rules_off_posts_only_the_card(conn, items):
    for key in ("pm.rule.overdue", "pm.rule.stalled"):
        set_setting(conn, key, False)
    out = S.send_if_due(conn, at(FRI, 17, 5))
    assert out and out["batches"] == {}
    assert conn.execute("SELECT COUNT(*) FROM assistant_changes").fetchone()[0] == 0


def test_lease_and_marker_prevent_double_posting(conn, items):
    from research_lab.ai import lease
    from datetime import timezone
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute("INSERT INTO worker_lease(name, holder, pid, port, acquired_at, renewed_at) VALUES (?,?,?,?,?,?)",
                 (S.LEASE, "mac-app:1", 1, 4180, stamp, stamp))
    assert S.send_if_due(conn, at(FRI, 17, 5)) is None                  # the other process is on it
    conn.execute("DELETE FROM worker_lease WHERE name = ?", (S.LEASE,))
    set_setting(conn, f"pm.friday_sent:{W3}", {"at": "other"})          # and it already posted
    assert S.send_if_due(conn, at(FRI, 17, 5)) is None
    assert conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 0
    assert not lease.holds(conn, S.LEASE)


def test_marker_claimed_in_the_post_transaction(conn, items, monkeypatch):
    # a process that read no marker but loses the insert race posts nothing
    real = S.get_setting
    monkeypatch.setattr(S, "get_setting", lambda c, k, d=None: None if k.startswith("pm.friday_sent") else real(c, k, d))
    set_setting(conn, f"pm.friday_sent:{W3}", {"at": "other"})
    assert S.send_if_due(conn, at(FRI, 17, 5)) is None
    assert conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 0


def test_failed_post_leaves_no_marker(conn, items, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk")
    monkeypatch.setattr(S.changes, "propose", boom)
    with pytest.raises(RuntimeError):
        S.send_if_due(conn, at(FRI, 17, 5))
    assert get_setting(conn, f"pm.friday_sent:{W3}") is None            # retried at the next tick
    assert conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 0


def test_timer_follows_env(monkeypatch):
    monkeypatch.delenv("RL_PM_AUTO", raising=False)
    monkeypatch.setenv("RL_FEEDS_AUTO", "0")
    assert not S.enabled() and not S.start()
    monkeypatch.setenv("RL_PM_AUTO", "1")
    assert S.enabled()
    monkeypatch.setenv("RL_PM_AUTO", "0")
    assert not S.enabled()


def plan_card(html: str, batch: str) -> str:
    start = html.index(f'id="plan-{batch}"')
    return html[html.rindex("<div", 0, start):html.index(f'id="plan-{batch}-dismiss"')]   # head, rows, buttons


def ticks(card: str) -> list[bool]:
    return [" checked" in m for m in re.findall(r'<input type="checkbox" class="plan-tick"[^>]*>', card)]


def test_card_renders_in_the_thread(client, items):
    from research_lab.chat import service
    from research_lab.db.connection import connect
    c = connect()
    try:
        out = S.send_if_due(c, at(FRI, 17, 5))
        move, week, park = out["batches"]["move"], out["batches"]["week"], out["batches"]["park"]
        assert [CH.digest_kind(b) for b in (move, week, park)] == ["move", "week", "park"]
        # Leo typed in the conversation since: the cards still say where they came from
        service.add_message(c, out["thread_id"], "user", "数据清洗那条先别动")
    finally:
        c.close()
    html = client.get(f"/chat/{out['thread_id']}").text
    assert "chat-progress" in html
    moves, weeks, parks = plan_card(html, move), plan_card(html, week), plan_card(html, park)
    for card in (moves, weeks, parks):
        assert "来自「每周五的进度」" in card and "先别动" not in card
    # 挪到下周五 for overdue keeps 0078's defaults (ticked; postponed 3+ would start unticked);
    # this week's unfinished and 搁置 are opt-in row by row
    assert ticks(moves) and all(ticks(moves))
    assert ticks(weeks) and not any(ticks(weeks))
    assert ticks(parks) and not any(ticks(parks))
    assert re.search(r"data-plan-apply disabled", parks) and "Apply <span data-plan-count>0</span>" in parks


def test_park_rows_elsewhere_keep_their_tick(client, items):
    """Only the digest's 搁置 rows start unticked: a card the assistant made from Leo's request does not."""
    from research_lab.chat import cards
    from research_lab.db.connection import connect
    c = connect()
    try:
        batch = CH.propose(c, [CH.change_item(items["stalled"], "status", "doing", "parked", label="第二章")],
                           reason="把没动的都搁置")
        view = cards.plan_view(c, batch, {"request": None})
    finally:
        c.close()
    assert view["rows"][0]["ticked"] and view["source"] != cards.DIGEST_SOURCE


def test_ticking_a_park_row_applies_only_that_row(client, items):
    from research_lab.db.connection import connect
    c = connect()
    try:
        out = S.send_if_due(c, at(FRI, 17, 5))
        park = out["batches"]["park"]
        rows = CH.rows(c, park)
    finally:
        c.close()
    pick = next(r for r in rows if r["target"] == items["stalled"])
    r = client.post(f"/chat/changes/{park}/apply", data={"shown": ",".join(str(x["id"]) for x in rows), "ids": [str(pick["id"])]})
    assert r.status_code == 200
    assert I.get(items["stalled"])["status"] == "parked" and I.get(items["carried"])["status"] == "todo"


def test_digest_batch_ids():
    ids = {k: CH.digest_batch_id(k) for k in CH.DIGEST_KINDS}
    assert {k: CH.digest_kind(b) for k, b in ids.items()} == {k: k for k in CH.DIGEST_KINDS}
    assert CH.digest_kind("AC-0123456789ab") == "" and CH.digest_kind(None) == "" and CH.digest_kind("AC-friday-x-1") == ""
    with pytest.raises(ValueError):
        CH.digest_batch_id("other")
    # the card reads the origin from pm.changes, never from the scheduler
    from pathlib import Path
    from research_lab.chat import cards
    assert "scheduler" not in Path(cards.__file__).read_text(encoding="utf-8")


def test_tests_never_start_the_timer(repo):
    import os
    assert os.environ["RL_PM_AUTO"] == "0" and not S.enabled()


def test_weekly_plan_skill_is_a_light_preset():
    from research_lab.agent import skills, tools
    s = skills.get("weekly-plan")
    assert s.tier == "light" and s.runs_tasks and s.max_tool_calls <= 6
    assert set(s.tools) <= set(tools.TOOLS) and "change_issues" in s.tools and "create_issues" not in s.tools
    for text in ("周一排期", "排一下这周", "这周怎么排？", "帮我排一下下周", "把落后的排一下", "安排一下这周的议题"):
        assert skills.pick(text).name == "weekly-plan", text
    # explicit scheduling asks only: research and travel sentences that mention 这周安排 go elsewhere
    for text in ("这周安排去北京开会，帮我整理一下会议材料", "这周安排的文献阅读进度"):
        assert skills.pick(text).name != "weekly-plan", text
    assert skills.pick("下周要做什么").name == "task"
    # the other two presets (card 0077): 拆子议题 → issues skill, 本周进度 → the fixed phrase
    assert skills.pick("把 ASEAN 那篇文献地图拆成三个子议题").name == "issues"
    from research_lab.chat import router
    assert router.match("本周进度")[0] is router.cmd_progress
