"""推送降噪 (card 0067): section rules per source, stale batches cancelled, a daily full refresh that actually runs,
and 精选 as the 推送 page's default."""
from __future__ import annotations

import pytest

from datetime import datetime, timedelta, timezone


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


def _news(conn, nid, *, source="BBC World", lane="world", link=None, title=None, importance=None, score=None,
          category=None, seen=None):
    ts = seen or _ago(hours=1)
    conn.execute("INSERT INTO feed_news(id, source, title, link, published, first_seen, lane, importance, score, category) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)", (nid, source, title or f"Story {nid}", link or f"https://example.org/{nid}",
                                                  ts, ts, lane, importance, score, category))


def _rules(monkeypatch, *feeds):
    from research_lab.feeds import sources
    monkeypatch.setattr(sources, "load", lambda paths=None: sources.Sources(news=list(feeds)))


def test_section_rules_parse_from_feeds_yml():
    from research_lab.feeds import sources
    s = sources.parse({"news": [
        {"name": "The Guardian · World", "url": "https://www.theguardian.com/world/rss", "lane": "world", "keep": ["/world/", "no-slash"]},
        {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml", "lane": "world", "drop": ["/sports/"]},
    ]})
    guardian, aj = s.news
    assert guardian.keep == ("/world/",) and aj.drop == ("/sports/",)              # a rule must be a path
    assert guardian.off_section("https://www.theguardian.com/football/2026/oct/08/x")
    assert not guardian.off_section("https://www.theguardian.com/world/2026/oct/08/x")
    assert aj.off_section("https://www.aljazeera.com/sports/2026/10/8/x") and not aj.off_section("https://www.aljazeera.com/news/x")


def test_off_section_items_leave_the_queue_and_come_back_when_a_rule_is_narrowed(conn, monkeypatch):
    from research_lab.feeds import news, sources
    _news(conn, "g1", source="The Guardian · World", link="https://www.theguardian.com/world/2026/oct/08/talks")
    _news(conn, "g2", source="The Guardian · World", link="https://www.theguardian.com/football/2026/oct/08/derby")
    _news(conn, "b1", link="https://www.bbc.co.uk/sport/x")                         # no rule for this source
    guardian = sources.NewsFeed("The Guardian · World", "https://www.theguardian.com/world/rss", "world", keep=("/world/",))
    _rules(monkeypatch, guardian)
    news.flag_not_news(conn)
    cat = {r["id"]: r["category"] for r in conn.execute("SELECT id, category FROM feed_news")}
    assert cat == {"g1": None, "g2": "非新闻", "b1": None}
    _rules(monkeypatch, sources.NewsFeed(guardian.name, guardian.url, "world", keep=("/world/", "/football/")))
    news.flag_not_news(conn)
    assert conn.execute("SELECT category, score FROM feed_news WHERE id = 'g2'").fetchone()[:] == (None, None)


def test_a_batch_left_queued_for_days_is_cancelled_and_only_picks_are_sent(conn, monkeypatch):
    from research_lab.ai import queue
    from research_lab.feeds import enrich
    _rules(monkeypatch)
    for i in range(3):
        _news(conn, f"old{i}", seen=_ago(days=5))                                # outside the window: never picked
    _news(conn, "r1", source="The Diplomat", lane="research")
    stale = enrich.enqueue_pending(conn, dispatch_jobs=False)
    assert len(stale) == 1
    conn.execute("UPDATE ai_jobs SET created_at = ? WHERE id = ?", (_ago(hours=50), stale[0]))
    again = enrich.enqueue_pending(conn, dispatch_jobs=False)
    assert queue.get(conn, stale[0])["status"] == "cancelled" and len(again) == 1  # the released pick goes again
    batches = {r["id"]: r["enrich_batch"] for r in conn.execute("SELECT id, enrich_batch FROM feed_news")}
    assert batches["r1"] == queue.get(conn, again[0])["target_id"] and all(batches[f"old{i}"] is None for i in range(3))


def test_opening_the_app_runs_the_daily_full_refresh_when_it_is_due(conn, monkeypatch):
    from research_lab.db.connection import set_setting
    from research_lab.feeds import brief, scheduler, store
    started: list[dict] = []
    monkeypatch.setattr(scheduler, "start_background", lambda http=None, **kw: started.append(kw) or True)
    monkeypatch.setattr(scheduler, "start_today", lambda *a, **kw: pytest.fail("the whole day's refresh started (network)"))
    monkeypatch.setenv("RL_FEEDS_AUTO", "1")
    # Today's 8:00 edition already ran: without this, between 8:00 and 8:05 Beijing time the news refresh of five
    # minutes ago predates the edition, daily_due() is true and the real start_today() fetches (card 0108).
    set_setting(conn, scheduler.TODAY_KEY, {"at": brief.now_bj().isoformat(), "kind": "daily"})
    set_setting(conn, store.LAST_KEY, {"kind": "news", "at": _ago(minutes=5), "full_at": _ago(hours=30)})
    assert scheduler.refresh_news_on_view(conn) and started == [{}]              # papers too, though news is fresh


def test_feed_page_opens_on_the_days_digest(client, conn):
    """精选 is the 推送 page's default (card 0067): announcements stay out, 世界要闻 take one item per outlet (card
    0083), and 全部条目 keeps every item."""
    _news(conn, "w_big", title="Ceasefire talks resume")
    _news(conn, "w_more", title="Local football result", seen=_ago(hours=3))      # the same outlet, older
    _news(conn, "r_hit", source="The Diplomat", lane="research", title="ASEAN hedging, revisited")
    _news(conn, "r_ann", source="The Diplomat", lane="research", title="Our September issue", category="非新闻")
    conn.commit()
    page = client.get("/feed?tab=news").text
    assert "Ceasefire talks resume" in page and "ASEAN hedging, revisited" in page
    assert "Local football result" not in page and "Our September issue" not in page
    assert 'id="digest"' in page and '新闻<span class="count">2</span>' in page
    everything = client.get("/feed?tab=news&pick=all").text
    assert "Local football result" in everything


def test_review_fixes_scalar_rules_broken_yml_and_papers_lookback(conn, monkeypatch):
    """Card 0067 review: a bare-string rule is one path; a broken feeds.yml heals nothing; papers look back
    from their own last fetch, not from the last news-only refresh."""
    from research_lab.db.connection import set_setting
    from research_lab.feeds import news, papers, scheduler, sources, store
    aj = sources.parse({"news": [{"name": "Al Jazeera", "url": "https://x/rss", "drop": "/video/"}]}).news[0]
    assert aj.drop == ("/video/",) and not aj.off_section("https://www.aljazeera.com/news/x")
    _news(conn, "v1", source="Al Jazeera", link="https://www.aljazeera.com/video/x")
    _rules(monkeypatch, aj)
    news.flag_not_news(conn)
    _rules(monkeypatch)                                                          # feeds.yml unreadable: no sources
    news.flag_not_news(conn)
    assert conn.execute("SELECT category FROM feed_news WHERE id = 'v1'").fetchone()[0] == "非新闻"
    seen: list[int] = []
    monkeypatch.setattr(papers, "refresh_papers", lambda conn, http, days: seen.append(days) or {"new": 0, "failed": []})
    monkeypatch.setattr(news, "refresh_news", lambda conn, http, days: {"new": 0, "failed": []})
    set_setting(conn, store.LAST_KEY, {"kind": "news", "at": _ago(minutes=5), "full_at": _ago(days=8)})
    scheduler.refresh_all(conn, http=object(), ai=False)
    assert seen and seen[0] == papers.default_days(_ago(days=8))
