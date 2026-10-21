"""每日世界政治简报 (card 0062): the job's request and guardrails, when it is queued, and the 首页 slide."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

NOW = datetime.now(timezone.utc)


def _news(conn, nid, title, *, lane="world", importance=None, category=None, hours_ago=2, source="BBC World"):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO feed_news(id, source, title, link, summary, published, first_seen, lane, importance, category) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)", (nid, source, title, f"https://example.org/{nid}", "Summary.", ts, ts, lane,
                                                  importance, category))


def test_the_request_carries_the_days_world_news_and_weighty_research_items(conn):
    from research_lab.ai.jobs import news_brief
    _news(conn, "w1", "Ceasefire talks resume in Sudan")
    _news(conn, "w2", "Our newsletter — out now", category="非新闻")
    _news(conn, "w3", "An old story", hours_ago=40)
    _news(conn, "r1", "Seoul weighs nuclear latency", lane="research", importance=4, source="War on the Rocks")
    _news(conn, "r2", "A minor op-ed", lane="research", importance=2, source="The Diplomat")
    req, _ = news_brief.SPEC.build("2026-10-02", conn)
    user = req.messages[-1]["content"]
    assert "Ceasefire talks resume in Sudan" in user and "Seoul weighs nuclear latency" in user
    assert "newsletter" not in user and "An old story" not in user and "A minor op-ed" not in user
    assert "6 to 8 events" in user and "never invent facts" in user and req.thinking is None
    assert "$120 billion = 1200亿美元" in user                 # a live run once wrote 120亿 for $120bn
    assert "简体中文" in req.messages[0]["content"]               # and another answered in traditional characters
    assert set(req.params["ids"].values()) == {"w1", "r1"}


def test_apply_keeps_only_events_backed_by_given_items(conn):
    from research_lab.ai.jobs import news_brief
    from research_lab.db.connection import get_setting
    for i in range(3):
        _news(conn, f"w{i}", f"Story {i}")
    req, _ = news_brief.SPEC.build("2026-10-02", conn)
    conn.execute("INSERT INTO ai_jobs(id, kind, status, request_json, schema_json, created_at) VALUES ('J-1', 'news.brief', 'claimed', ?, '{}', ?)",
                 (json.dumps(req.to_json()), NOW.isoformat()))
    run = conn.execute("INSERT INTO ai_runs(job_id, provider, model, started_at) VALUES ('J-1', 'test', 'test', ?)", (NOW.isoformat(),)).lastrowid
    labels = {v: k for k, v in req.params["ids"].items()}
    result = news_brief.NewsBrief(events=[
        {"title": "苏丹停火谈判恢复", "what": "双方重回谈判桌。", "why": "影响红海航运。", "region": "非洲", "direction": "nonsense",
         "item_ids": [labels["w0"], labels["w1"], "i99"]},
        {"title": "无来源的事件", "what": "编造的。", "item_ids": ["i99"]},
        {"title": "火星新闻", "what": "地区不对。", "region": "火星", "item_ids": [labels["w2"]]},
        {"title": "东亚新闻", "what": "繁体地区名。", "region": "東亞", "item_ids": [labels["w2"]]},
    ] + [{"title": f"多余 {n}", "what": "x", "item_ids": [labels["w2"]]} for n in range(10)])
    news_brief.SPEC.apply(result, run, conn, "2026-10-02")
    b = get_setting(conn, "news.brief:2026-10-02")
    titles = [e["title"] for e in b["events"]]
    assert titles[0] == "苏丹停火谈判恢复" and "无来源的事件" not in titles and len(titles) <= 8
    first = b["events"][0]
    assert [s["id"] for s in first["sources"]] == ["w0", "w1"] and first["sources"][0]["url"] == "https://example.org/w0"
    assert first["direction"] is None and b["events"][1]["region"] is None and b["events"][2]["region"] == "东亚"


def test_the_brief_is_queued_once_a_morning_with_enough_news(conn):
    from research_lab.db.connection import set_setting
    from research_lab.feeds import brief
    for i in range(brief.MIN_ITEMS):
        _news(conn, f"w{i}", f"Story {i}")
    morning = datetime.now().replace(hour=6, minute=30)
    assert brief.maybe_enqueue(conn, dispatch_jobs=False, now=morning) is None          # before BRIEF_HOUR
    later = datetime.now().replace(hour=8, minute=0)
    job = brief.maybe_enqueue(conn, dispatch_jobs=False, now=later)
    assert job and brief.maybe_enqueue(conn, dispatch_jobs=False, now=later) is None   # queued already
    conn.execute("UPDATE ai_jobs SET status = 'failed' WHERE id = ?", (job,))
    assert brief.maybe_enqueue(conn, dispatch_jobs=False, now=later)                    # a failed one is retried
    set_setting(conn, f"news.brief:{later.date().isoformat()}", {"events": [{"title": "x", "what": "y", "sources": []}]})
    conn.execute("UPDATE ai_jobs SET status = 'failed' WHERE kind = 'news.brief'")
    assert brief.maybe_enqueue(conn, dispatch_jobs=False, now=later) is None            # written already


def test_too_little_news_writes_no_brief(conn):
    from research_lab.feeds import brief
    _news(conn, "w1", "Only story")
    assert brief.maybe_enqueue(conn, dispatch_jobs=False, now=datetime.now().replace(hour=9)) is None


def test_the_brief_runs_under_the_feeds_skill():
    from research_lab.agent import skills
    assert skills.for_job("news.brief").name == "feeds"


def test_home_shows_the_brief_as_todays_news_page(client, conn, monkeypatch):
    from research_lab import dashboard
    real = dashboard.build
    b = {"date": "2026-10-02", "events": [
        {"title": "苏丹停火谈判恢复", "what": "双方重回谈判桌。", "why": "影响红海航运。", "region": "非洲", "direction": None,
         "sources": [{"id": "w0", "source": "BBC World", "url": "https://example.org/w0", "title": "x"},
                     {"id": "w1", "source": "Al Jazeera", "url": "https://example.org/w1", "title": "y"}]},
        {"title": "欧盟通过新一轮对俄制裁", "what": "a", "why": None, "region": "欧洲", "direction": None,
         "sources": [{"id": "w2", "source": "DW · World", "url": "https://example.org/w2", "title": "z"},
                     {"id": "w3", "source": "DW · World", "url": "https://example.org/w3", "title": "z2"}]},
        {"title": "东盟外长会讨论缅甸", "what": "b", "why": None, "region": "东南亚", "direction": "sea-asean", "sources": []},
    ]}
    monkeypatch.setattr(dashboard, "build", lambda conn: {**real(conn), "brief": b})
    page = client.get("/").text
    news = page.split('id="front-news"')[1].split("</article>")[0]
    assert 'class="today front-stream front-brief"' in news and "苏丹停火谈判恢复" in news and "BBC World、Al Jazeera" in news
    assert news.count("Today's picks") == 1 and "欧盟通过新一轮对俄制裁" in news and "东盟外长会讨论缅甸" in news
    assert "助手整理" in news and "1 家报道" in news and "昨天" not in news


def test_home_route_passes_the_latest_brief(client, conn):
    from datetime import date
    from research_lab.db.connection import set_setting
    set_setting(conn, f"news.brief:{date.today().isoformat()}", {"date": date.today().isoformat(), "events": [
        {"title": "联合国安理会就加沙停火决议投票", "what": "决议获得通过。", "why": None, "region": "中东", "direction": None,
         "sources": [{"id": "w9", "source": "UN News", "url": "https://news.un.org/x", "title": "x"}]}]})
    conn.commit()
    news = client.get("/").text.split('id="front-news"')[1].split("</article>")[0]
    assert "front-brief" in news and "联合国安理会就加沙停火决议投票" in news and news.count("Today's picks") == 1


def test_yesterdays_brief_says_so(client, conn):
    from datetime import date, timedelta
    from research_lab.db.connection import set_setting
    from research_lab.feeds import brief
    y = (date.today() - timedelta(days=1)).isoformat()
    set_setting(conn, f"news.brief:{y}", {"date": y, "events": [
        {"title": "昨日要闻", "what": "x", "why": None, "region": "全球", "direction": None,
         "sources": [{"id": "a", "source": "DW · World", "url": "https://example.org/a", "title": "a"},
                     {"id": "b", "source": "DW · World", "url": "https://example.org/b", "title": "b"}]}]})
    conn.commit()
    assert brief.latest(conn)["yesterday"] is True
    news = client.get("/").text.split('id="front-news"')[1].split("</article>")[0]
    assert f"昨天 · {int(y[5:7])}月{int(y[8:10])}日的简报" in news
    assert f"day={y}" in news


def test_a_brief_that_keeps_failing_stops_for_the_day(conn):
    from research_lab.feeds import brief
    for i in range(brief.MIN_ITEMS):
        _news(conn, f"w{i}", f"Story {i}")
    later = datetime.now().replace(hour=8, minute=0)
    for _ in range(brief.MAX_TRIES):
        job = brief.maybe_enqueue(conn, dispatch_jobs=False, now=later)
        conn.execute("UPDATE ai_jobs SET status = 'failed' WHERE id = ?", (job,))
    assert brief.maybe_enqueue(conn, dispatch_jobs=False, now=later) is None

