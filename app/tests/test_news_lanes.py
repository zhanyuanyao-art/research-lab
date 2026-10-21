"""新闻两条线 (card 0061, docs/design/NEWS-v2.md): world news and research commentary, importance and region."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

NOW = datetime.now(timezone.utc)


def _rss(title: str, link: str) -> str:
    pub = (NOW - timedelta(hours=3)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    return (f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title><item><title>{title}</title>'
            f"<link>{link}</link><pubDate>{pub}</pubDate><description>Summary.</description></item></channel></rss>")


def test_sources_carry_their_lane():
    import yaml
    from research_lab.feeds import sources
    feeds = sources.parse(yaml.safe_load((Path(__file__).parent / "fixtures" / "content" / "_data" / "feeds.yml").read_text(encoding="utf-8"))).news
    world = [f for f in feeds if f.lane == "world"]
    assert len(world) >= 10 and {"BBC World", "Al Jazeera", "UN News"} <= {f.name for f in world}
    assert {"Foreign Affairs", "Crisis Group"} <= {f.name for f in feeds if f.lane == "research"}
    assert not any(re.search(r"[一-鿿]", f.name) for f in world)           # Leo: no Chinese-language media
    assert sources.parse({"news": [{"name": "X", "url": "https://x.org/feed", "lane": "nonsense"}]}).news[0].lane == "research"


def test_migration_adds_lane_importance_and_region(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(feed_news)")}
    assert {"lane", "importance", "region"} <= cols


def test_refresh_stores_the_lane_of_each_source(conn):
    from research_lab.feeds import news
    from research_lab.feeds.sources import NewsFeed

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "feeds.bbci.co.uk":
            return httpx.Response(200, text=_rss("Ceasefire talks resume in Sudan", "https://www.bbc.co.uk/news/a"))
        return httpx.Response(200, text=_rss("Hedging in Southeast Asia", "https://fulcrum.sg/b/"))

    feeds = [NewsFeed("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "world"),
             NewsFeed("Fulcrum · ISEAS", "https://fulcrum.sg/feed/")]
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert news.refresh_news(conn, http, days=2, feeds=feeds)["new"] == 2
    lanes = dict(conn.execute("SELECT source, lane FROM feed_news").fetchall())
    assert lanes == {"BBC World": "world", "Fulcrum · ISEAS": "research"}


def test_scoring_asks_for_importance_and_region_without_a_regional_cap(conn):
    from research_lab.feeds import enrich
    conn.execute("INSERT INTO feed_news(id, source, title, link, first_seen, enrich_batch, lane) VALUES "
                 "('n1', 'BBC World', 'Ceasefire talks resume in Sudan', 'https://www.bbc.co.uk/news/a', ?, 'news:b1', 'world')",
                 (NOW.isoformat(),))
    req, _ = enrich.SPEC.build("news:b1", conn)
    user = req.messages[-1]["content"]
    assert "- importance: integer 0-5" in user and "- region: where it happens" in user and "非洲" in user
    assert "Europe, Latin America or Africa" not in user


def test_apply_stores_importance_and_region_and_the_rule_still_wins(conn):
    from research_lab.feeds import enrich
    ts = NOW.isoformat()
    conn.execute("INSERT INTO feed_news(id, source, title, link, first_seen, enrich_batch, lane) VALUES "
                 "('n1', 'BBC World', 'Ceasefire talks resume in Sudan', 'https://www.bbc.co.uk/news/a', ?, 'news:b1', 'world')", (ts,))
    # already marked by the rule and still in the batch: the assistant's numbers must not stick
    conn.execute("INSERT INTO feed_news(id, source, title, link, first_seen, enrich_batch, lane, category, score, importance) VALUES "
                 "('n2', 'UN News', 'Our Newsletter — Out Now', 'https://news.un.org/x', ?, 'news:b1', 'world', '非新闻', 0, 0)", (ts,))
    conn.execute("INSERT INTO ai_jobs(id, kind, status, request_json, schema_json, created_at) VALUES ('J-1', 'feeds.enrich', 'claimed', ?, '{}', ?)",
                 (json.dumps({"params": {"stream": "news", "ids": {"i1": "n1", "i2": "n2"}}}), ts))
    run = conn.execute("INSERT INTO ai_runs(job_id, provider, model, started_at) VALUES ('J-1', 'test', 'test', ?)", (ts,)).lastrowid
    result = enrich.FeedEnrichment(items=[{"id": "i1", "score": 1, "importance": 5, "region": "非洲", "category": "全球政经"},
                                          {"id": "i2", "score": 4, "importance": 4, "region": "火星", "category": "地区安全"}])
    enrich.SPEC.apply(result, run, conn, "news:b1")
    rows = {r["id"]: r for r in conn.execute("SELECT id, score, importance, region, category FROM feed_news")}
    assert (rows["n1"]["score"], rows["n1"]["importance"], rows["n1"]["region"]) == (1, 5, "非洲")    # low relevance, high weight
    assert (rows["n2"]["importance"], rows["n2"]["category"], rows["n2"]["region"]) == (0, "非新闻", None)


def test_the_brief_hears_every_outlet_not_just_the_busiest(conn):
    """Card 0083: world items carry no importance score any more, so the brief's 80 items take the outlets in turn;
    ~250 items a day from a few busy outlets would otherwise leave only the last hours of one."""
    from research_lab.ai.jobs import news_brief
    for i in range(120):        # one busy outlet, all newer
        ts = (NOW - timedelta(minutes=i)).isoformat()
        conn.execute("INSERT INTO feed_news(id, source, title, link, published, first_seen, lane) VALUES (?,?,?,?,?,?, 'world')",
                     (f"w{i}", "BBC World", f"World story {i}", f"https://bbc.co.uk/{i}", ts, ts))
    for i in range(10):
        ts = (NOW - timedelta(hours=10 + i)).isoformat()
        conn.execute("INSERT INTO feed_news(id, source, title, link, published, first_seen, lane) VALUES (?,?,?,?,?,?, 'world')",
                     (f"u{i}", "UN News", f"UN story {i}", f"https://news.un.org/{i}", ts, ts))
    rows = news_brief.items_for(conn, NOW.date().isoformat())
    assert len(rows) == news_brief.MAX_ITEMS and sum(r["source"] == "UN News" for r in rows) == 10


def test_crisis_group_dates_are_read():
    from research_lab.feeds import news
    assert news.parse_date("Friday, September 25, 2026 - 15:56") == "2026-09-25T15:56:00Z"
    assert news.parse_date("Fri, 25 Sep 2026 15:56:00 +0000") == "2026-09-25T15:56:00Z"
    assert news.parse_date("not a date") is None
