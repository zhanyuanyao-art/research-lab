"""首页 刷新今天 and the 8 o'clock edition (card 0104, Leo 10-11): the carousel's refresh button, the manual refresh
that rewrites today's brief and picks (once a day), and the daily refresh at 8 Beijing time. No AI, no network."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from research_lab.db.connection import get_setting, set_setting

BJ = ZoneInfo("Asia/Shanghai")


def _bj(h, m=0, day=11):
    return datetime(2026, 10, day, h, m, tzinfo=BJ)


def _news(conn, nid, *, lane="world", hours=1, source=None):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO feed_news(id, source, title, link, summary, published, first_seen, lane) VALUES (?,?,?,?,?,?,?,?)",
                 (nid, source or f"Outlet {nid}", f"Story {nid}", f"https://example.org/{nid}", "S.", ts, ts, lane))


def _brief(conn, day, title):
    set_setting(conn, f"news.brief:{day}", {"date": day, "events": [
        {"title": title, "what": "发生了什么。", "why": None, "region": "全球", "direction": None, "sources": []}]})


# ------------------------------------------------------------------------------- the button

def test_carousel_shows_a_refresh_button_instead_of_pause(client):
    page = client.get("/").text
    assert 'data-front="refresh"' in page and "刷新今天的新闻和论文" in page
    assert 'data-front="pause"' not in page and "暂停轮播" not in page
    fragment = client.get("/feed/front")
    assert fragment.status_code == 200 and 'id="front"' in fragment.text and 'data-front="refresh"' in fragment.text


def test_front_js_refreshes_in_place():
    from pathlib import Path
    import research_lab
    js = (Path(research_lab.__file__).parent / "web" / "static" / "front.js").read_text()
    assert "/feed/refresh-today" in js and "/feed/front" in js and "location.reload" not in js
    assert "mouseenter" in js   # rotation still pauses while hovered


# ----------------------------------------------------------------------- the manual refresh

def test_manual_refresh_is_once_a_day(client, conn, monkeypatch):
    from research_lab.feeds import brief, scheduler
    calls = []

    def fake_refresh_today(c, http=None, *, kind="daily", **kw):
        calls.append(kind)
        _brief(c, brief.today_bj().isoformat(), "新的简报")
        set_setting(c, scheduler.TODAY_KEY, {"at": brief.now_bj().isoformat(), "kind": kind})
        return {"kind": kind}
    monkeypatch.setattr(scheduler, "refresh_today", fake_refresh_today)
    state = client.get("/feed/refresh-today").json()
    assert state["left"] == 1 and state["title"] == "刷新今天的新闻和论文 · 今天还能刷新 1 次"
    first = client.post("/feed/refresh-today")
    assert first.status_code == 200 and first.json()["result"] == "started"
    scheduler.wait()
    assert calls == ["manual"] and brief.get(conn, brief.today_bj().isoformat())["events"][0]["title"] == "新的简报"
    second = client.post("/feed/refresh-today")
    assert second.status_code == 429 and second.json()["result"] == "limit"
    assert second.json()["title"] == "今天已手动刷新，明早 8 点自动更新"
    assert calls == ["manual"]
    after = client.get("/feed/refresh-today").json()
    assert after["left"] == 0 and not after["running"]
    assert get_setting(conn, scheduler.MANUAL_KEY + brief.today_bj().isoformat()) == 1


def test_the_limit_is_per_beijing_day(conn):
    from research_lab.feeds import scheduler
    set_setting(conn, scheduler.MANUAL_KEY + "2026-10-10", 1)
    assert scheduler.manual_left(conn, "2026-10-10") == 0 and scheduler.manual_left(conn, "2026-10-11") == 1


def test_refresh_today_rewrites_brief_and_recomputes_picks(conn, monkeypatch):
    from research_lab.feeds import brief, digest, scheduler
    day = brief.today_bj().isoformat()
    _brief(conn, day, "早上的简报")
    for i in range(4):
        _news(conn, f"old{i}", lane="research", hours=3)
    digest.fill(conn, day)
    morning = digest._issue(conn, day)["news"]
    assert morning
    for i in range(4):
        _news(conn, f"new{i}", lane="research", hours=0)
    monkeypatch.setattr(scheduler, "refresh_all", lambda c, http=None, **kw: {"papers": {"new": 0}, "news": {"new": 4}})

    def fake_rewrite(c, d, **kw):
        _brief(c, d, "中午重写的简报")
        return "done"
    monkeypatch.setattr(brief, "rewrite", fake_rewrite)
    out = scheduler.refresh_today(conn, kind="manual", dispatch_jobs=False)
    assert out["brief"] == "done" and brief.get(conn, day)["events"][0]["title"] == "中午重写的简报"
    issue = get_setting(conn, digest.KEY + day)
    assert set(issue["seen"]) >= set(morning)
    assert {f"new{i}" for i in range(4)} <= set(issue["news"])          # items not shown yet come first
    assert get_setting(conn, scheduler.TODAY_KEY)["kind"] == "manual"


def test_recompute_fills_from_seen_items_on_a_quiet_day(conn):
    from research_lab.feeds import brief, digest
    day = brief.today_bj().isoformat()
    for i in range(3):
        _news(conn, f"r{i}", lane="research")
    first = digest.fill(conn, day)["news"]
    again = digest.recompute(conn, day)["news"]
    assert set(again) == set(first)        # nothing new: the same items rather than empty slots


def test_seen_picks_do_not_come_back_tomorrow(conn):
    from research_lab.feeds import digest
    set_setting(conn, digest.KEY + "2026-10-10", {"news": ["a"], "papers": [], "seen": ["b"]})
    assert {"a", "b"} <= digest._picked_before(conn, "2026-10-11", 3)


def test_brief_rewrite_queues_a_new_job_even_when_one_is_written(conn, monkeypatch):
    from research_lab.feeds import brief
    day = brief.today_bj().isoformat()
    for i in range(brief.MIN_ITEMS):
        _news(conn, f"w{i}")
    _brief(conn, day, "旧的")
    conn.execute("INSERT INTO ai_jobs(id, kind, status, target_id, request_json, schema_json, created_at) "
                 "VALUES ('J-old', 'news.brief', 'done', ?, '{}', '{}', ?)", (day, datetime.now(timezone.utc).isoformat()))
    assert brief.rewrite(conn, day, dispatch_jobs=False, wait=0) == "pending"
    jobs = conn.execute("SELECT status FROM ai_jobs WHERE kind = 'news.brief' AND target_id = ?", (day,)).fetchall()
    assert sorted(j[0] for j in jobs) == ["done", "queued"]
    assert brief.rewrite(conn, day, dispatch_jobs=False, wait=0) == "pending"   # reuses the queued one
    assert conn.execute("SELECT COUNT(*) FROM ai_jobs WHERE kind = 'news.brief' AND status = 'queued'").fetchone()[0] == 1


def test_brief_rewrite_respects_the_budget_and_needs_news(conn, monkeypatch):
    from research_lab.feeds import brief, enrich
    day = brief.today_bj().isoformat()
    assert brief.rewrite(conn, day, dispatch_jobs=False, wait=0) == "few"
    for i in range(brief.MIN_ITEMS):
        _news(conn, f"w{i}")
    monkeypatch.setattr(enrich, "over_budget", lambda c: True)
    assert brief.rewrite(conn, day, dispatch_jobs=False, wait=0) == "budget"


def test_an_empty_rewrite_keeps_the_brief_already_shown(conn):
    from research_lab.ai.jobs import news_brief
    _brief(conn, "2026-10-11", "保留")
    news_brief.SPEC.apply(news_brief.NewsBrief(events=[]), 0, conn, "2026-10-11")
    assert get_setting(conn, "news.brief:2026-10-11")["events"][0]["title"] == "保留"


# --------------------------------------------------------------------- the 8 o'clock edition

def test_daily_refresh_is_due_from_8_beijing_time(conn):
    from research_lab.feeds import scheduler
    assert not scheduler.daily_due(conn, _bj(7, 59))
    assert scheduler.daily_due(conn, _bj(8, 0))
    set_setting(conn, scheduler.TODAY_KEY, {"at": _bj(8, 5).isoformat(), "kind": "daily"})
    assert not scheduler.daily_due(conn, _bj(23, 0))
    assert not scheduler.daily_due(conn, _bj(7, 0, day=12))
    assert scheduler.daily_due(conn, _bj(8, 0, day=12))


def test_a_manual_refresh_before_8_does_not_count_as_the_days_edition(conn):
    from research_lab.feeds import scheduler
    set_setting(conn, scheduler.TODAY_KEY, {"at": _bj(7, 30).isoformat(), "kind": "manual"})
    assert scheduler.daily_due(conn, _bj(8, 1))


def test_time_zone_is_beijing_not_the_macs(conn):
    from research_lab.feeds import scheduler
    utc = timezone.utc
    assert scheduler.daily_due(conn, datetime(2026, 10, 11, 0, 30, tzinfo=utc))       # 08:30 in Beijing
    assert not scheduler.daily_due(conn, datetime(2026, 10, 10, 23, 30, tzinfo=utc))  # 07:30 in Beijing
    assert scheduler.daily_due(conn, datetime(2026, 10, 10, 20, 30, tzinfo=ZoneInfo("America/New_York")))  # 08:30 BJ
    assert scheduler.seconds_until_daily(conn, _bj(7, 0)) == 3600


def test_a_stored_brief_hour_moves_the_edition(conn):
    from research_lab.feeds import brief, scheduler
    assert brief.daily_hour(conn) == 8
    set_setting(conn, "feeds.brief_hour", 9)
    assert brief.daily_hour(conn) == 9 and not scheduler.daily_due(conn, _bj(8, 30))


def test_the_brief_is_not_queued_before_8(conn):
    from research_lab.feeds import brief
    for i in range(brief.MIN_ITEMS):
        _news(conn, f"w{i}")
    assert brief.maybe_enqueue(conn, dispatch_jobs=False, now=_bj(7, 30)) is None
    assert brief.maybe_enqueue(conn, dispatch_jobs=False, now=datetime.now(BJ).replace(hour=8, minute=5))


def test_before_8_the_page_shows_yesterdays_brief(conn):
    from datetime import date
    from research_lab.feeds import brief
    _brief(conn, "2026-10-10", "昨天的事")
    b = brief.latest(conn, date(2026, 10, 11))
    assert b["yesterday"] and b["events"][0]["title"] == "昨天的事"


def test_opening_the_page_after_8_runs_the_days_refresh(conn, monkeypatch):
    from research_lab.feeds import scheduler
    started = []
    monkeypatch.setattr(scheduler, "daily_due", lambda c, now=None: True)
    monkeypatch.setattr(scheduler, "start_today", lambda c, http=None, *, manual, **kw: started.append(manual) or "started")
    monkeypatch.setenv("RL_FEEDS_AUTO", "1")
    assert scheduler.refresh_news_on_view(conn) is True and started == [False]


def test_no_automatic_refresh_when_feeds_auto_is_off(conn, monkeypatch):
    from research_lab.feeds import scheduler
    started = []
    monkeypatch.setattr(scheduler, "daily_due", lambda c, now=None: True)
    monkeypatch.setattr(scheduler, "start_today", lambda *a, **kw: started.append(1) or "started")
    monkeypatch.setenv("RL_FEEDS_AUTO", "0")
    assert scheduler.refresh_news_on_view(conn) is False and scheduler.start() is False and started == []


def test_the_daily_run_is_not_repeated_by_a_second_caller(conn, monkeypatch):
    from research_lab.feeds import scheduler
    ran = []
    monkeypatch.setattr(scheduler, "refresh_today", lambda *a, **kw: ran.append(1) or {})
    monkeypatch.setattr(scheduler, "daily_due", lambda c, now=None: False)
    assert scheduler.run_today(kind="daily", lock_wait=1) is None and ran == []


def test_two_clicks_at_once_get_one_manual_refresh(conn):
    from research_lab.feeds import scheduler
    assert scheduler._claim_manual(conn, "2026-10-11") is True
    assert scheduler._claim_manual(conn, "2026-10-11") is False
    assert scheduler.manual_used(conn, "2026-10-11") == 1


def test_the_daily_run_checks_again_once_it_holds_the_lease(conn, monkeypatch):
    from research_lab.feeds import scheduler
    answers = iter([True, False])            # due when it starts waiting, done by the other process once it may run
    ran = []
    monkeypatch.setattr(scheduler, "daily_due", lambda c, now=None: next(answers))
    monkeypatch.setattr(scheduler, "refresh_today", lambda *a, **kw: ran.append(1) or {})
    assert scheduler.run_today(kind="daily", lock_wait=1) is None and ran == []


def test_recompute_keeps_saved_picks_in_place(conn):
    from research_lab.feeds import brief, digest
    day = brief.today_bj().isoformat()
    for i in range(3):
        _news(conn, f"r{i}", lane="research", hours=3)
    picked = digest.fill(conn, day)["news"]
    conn.execute("UPDATE feed_news SET status = 'saved' WHERE id = ?", (picked[0],))
    for i in range(3):
        _news(conn, f"n{i}", lane="research", hours=0)
    issue = digest.recompute(conn, day)
    assert issue["news"][0] == picked[0]
    stored = get_setting(conn, digest.KEY + day)
    assert picked[0] not in stored["seen"] and set(picked[1:]) <= set(stored["seen"])
