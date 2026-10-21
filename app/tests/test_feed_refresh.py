"""推送刷新节奏 (card 0063): the timer runs once a day; 首页 and 推送 refetch news that is older than 30 minutes
when they are opened, and the status keeps polling until today's picks are translated."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx

NOW = datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def test_the_timer_is_daily_and_a_news_refresh_does_not_postpone_it(conn):
    from research_lab.db.connection import set_setting
    from research_lab.feeds import scheduler, store
    assert scheduler.INTERVAL == 24 * 3600 and scheduler.NEWS_FRESH == 30 * 60
    set_setting(conn, store.LAST_KEY, {"kind": "news", "at": _iso(NOW), "full_at": _iso(NOW - timedelta(hours=25))})
    assert scheduler.seconds_until_due(conn) == 0.0                     # the daily full refresh is due regardless
    set_setting(conn, store.LAST_KEY, {"at": _iso(NOW - timedelta(hours=2)), "full_at": _iso(NOW - timedelta(hours=2))})
    assert 21 * 3600 < scheduler.seconds_until_due(conn) <= 22 * 3600


def test_news_only_refresh_leaves_papers_and_keeps_the_full_refresh_time(conn, monkeypatch):
    from research_lab.db.connection import set_setting
    from research_lab.feeds import scheduler, sources, store
    full_at = _iso(NOW - timedelta(hours=5))
    set_setting(conn, store.LAST_KEY, {"at": full_at, "full_at": full_at})
    hosts: list[str] = []
    pub = (NOW - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S +0000")

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(200, text=f'<?xml version="1.0"?><rss version="2.0"><channel><item><title>Talks resume</title>'
                                        f"<link>https://www.bbc.co.uk/news/x</link><pubDate>{pub}</pubDate></item></channel></rss>")

    monkeypatch.setattr(sources, "load", lambda paths=None: sources.Sources(
        news=[sources.NewsFeed("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml")]))
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        s = scheduler.refresh_news(conn, http, ai=False)
    assert s["kind"] == "news" and s["news"]["new"] == 1 and s["full_at"] == full_at
    assert hosts == ["feeds.bbci.co.uk"]                                # no journal was asked
    assert store.last_refresh(conn)["full_at"] == full_at


def test_opening_a_page_refreshes_stale_news_only(conn, monkeypatch):
    from research_lab.db.connection import set_setting
    from research_lab.feeds import brief, scheduler, store
    # today's 8 o'clock edition has run (card 0104), so only the news-age rule is under test at any hour
    set_setting(conn, scheduler.TODAY_KEY, {"at": brief.now_bj().isoformat()})
    monkeypatch.setattr(scheduler, "daily_due", lambda conn, now=None: False)
    started: list[dict] = []
    monkeypatch.setattr(scheduler, "start_background", lambda http=None, **kw: started.append(kw) or True)
    monkeypatch.setenv("RL_FEEDS_AUTO", "1")
    set_setting(conn, store.LAST_KEY, {"at": _iso(NOW - timedelta(minutes=10))})
    assert not scheduler.refresh_news_on_view(conn) and started == []
    set_setting(conn, store.LAST_KEY, {"at": _iso(NOW - timedelta(minutes=45))})
    assert scheduler.refresh_news_on_view(conn) and started == [{"news_only": True}]
    monkeypatch.setenv("RL_FEEDS_AUTO", "0")
    assert not scheduler.refresh_news_on_view(conn)


def test_home_and_the_news_tab_ask_for_fresh_news(client, conn, monkeypatch):
    from research_lab.feeds import scheduler
    calls: list[str] = []
    monkeypatch.setattr(scheduler, "refresh_news_on_view", lambda conn, http=None: calls.append("x") or False)
    client.get("/")
    client.get("/feed?tab=news")
    client.get("/feed?tab=papers")
    assert len(calls) == 2


def test_status_keeps_polling_while_the_picks_are_translated_then_offers_them(client, conn, monkeypatch):
    from research_lab.ai import dispatch
    from research_lab.db.connection import set_setting
    from research_lab.feeds import store
    monkeypatch.setattr(dispatch, "route", lambda conn: ("claude", ""))       # an engine that can run
    from research_lab.feeds import scheduler
    monkeypatch.setattr(scheduler, "running", lambda: False)                 # no refresh thread left by another test
    # the refresh has just finished (NOW is taken at import, which can be a minute before this test runs)
    set_setting(conn, store.LAST_KEY, {"at": _iso(datetime.now(timezone.utc)), "news": {"new": 3, "failed": []},
                                       "papers": {"new": 0, "failed": []}, "ai_jobs": ["J-20991231-0001"]})
    conn.execute("INSERT INTO ai_jobs(id, kind, status, request_json, schema_json, created_at) VALUES "
                 "('J-20991231-0001', 'feeds.enrich', 'queued', '{}', '{}', ?)", (_iso(NOW),))
    conn.commit()
    since = int(time.time())
    waiting = client.get(f"/feed/status?polling=1&since={since}").text
    assert 'hx-trigger="every 3s"' in waiting and "正在翻译今日精选" in waiting
    late = client.get(f"/feed/status?polling=1&since={since - 400}").text              # gives up after FOLLOW_ENRICH_S
    assert 'hx-trigger' not in late and "刷新完成" in late
    conn.execute("UPDATE ai_jobs SET status = 'done' WHERE id = 'J-20991231-0001'")
    conn.commit()
    done = client.get(f"/feed/status?polling=1&since={since}").text
    assert 'hx-trigger' not in done and "刷新完成：论文 0 篇、新闻 3 条" in done and "location.reload()" in done


def test_the_poll_chain_starts_its_clock_once_and_carries_it(client, conn, monkeypatch):
    """Card 0063 review: a page renders the poll without `since` (no 422); the first poll starts the clock and
    every later hop keeps the same start, so the five-minute cap can fire."""
    import re
    from research_lab.db.connection import set_setting
    from research_lab.feeds import scheduler, store
    monkeypatch.setattr(scheduler, "running", lambda: True)
    page = client.get("/feed?tab=papers").text
    url = re.search(r'hx-get="(/feed/status[^"]*)"', page).group(1)
    assert "since" not in url
    first = client.get(url.replace("&amp;", "&"))
    assert first.status_code == 200
    start = re.search(r"since=(\d+)", first.text).group(1)
    time.sleep(1.1)
    second = client.get(f"/feed/status?polling=1&since={start}").text
    assert re.search(r"since=(\d+)", second).group(1) == start


def test_new_items_reload_only_after_a_refresh_inside_the_chain(client, conn, monkeypatch):
    from research_lab.feeds import scheduler
    monkeypatch.setattr(scheduler, "running", lambda: False)
    from research_lab.db.connection import set_setting
    from research_lab.feeds import store
    old = _iso(NOW - timedelta(hours=2))
    set_setting(conn, store.LAST_KEY, {"at": old, "news": {"new": 3, "failed": []}, "papers": {"new": 0, "failed": []}})
    conn.commit()
    stale = client.get(f"/feed/status?polling=1&since={int(time.time())}").text      # nothing ran during this chain
    assert "刷新完成" in stale and "location.reload()" not in stale
    set_setting(conn, store.LAST_KEY, {"at": _iso(datetime.now(timezone.utc)), "news": {"new": 3, "failed": []},
                                       "papers": {"new": 0, "failed": []}})
    conn.commit()
    assert "location.reload()" in client.get(f"/feed/status?polling=1&since={int(time.time()) - 5}").text


def test_no_waiting_for_scores_when_no_engine_can_run(client, conn, monkeypatch):
    from research_lab.feeds import scheduler
    monkeypatch.setattr(scheduler, "running", lambda: False)
    from research_lab.db.connection import set_setting
    from research_lab.feeds import store
    set_setting(conn, store.LAST_KEY, {"at": _iso(NOW), "news": {"new": 2, "failed": []}, "papers": {"new": 0, "failed": []},
                                       "ai_jobs": ["J-20991231-0002"]})
    conn.execute("INSERT INTO ai_jobs(id, kind, status, request_json, schema_json, created_at) VALUES "
                 "('J-20991231-0002', 'feeds.enrich', 'queued', '{}', '{}', ?)", (_iso(NOW),))
    conn.commit()
    page = client.get(f"/feed/status?polling=1&since={int(time.time())}").text      # agent mode in tests: no engine
    assert "hx-trigger" not in page and "刷新完成" in page

