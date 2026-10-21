"""推送 in two lanes on screen (card 0064): 首页's 世界要闻 · 研究相关 · 论文, and the 推送 page's news by day, with lane and
region filters."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _news(conn, nid, title, *, lane, importance=None, score=None, region=None, days_ago=0.1):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO feed_news(id, source, title, link, zh_title, published, first_seen, lane, importance, score, region) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (nid, "BBC World" if lane == "world" else "Fulcrum · ISEAS", title,
                                                    f"https://example.org/{nid}", title, ts[:10], ts, lane, importance, score, region))


def test_home_world_news_follows_the_brief_and_research_comes_after(client, conn):
    """Card 0083: 世界要闻 are the brief's events in its order; the outlet's other items stay off (one per outlet)."""
    from datetime import date
    from research_lab.db.connection import set_setting
    from research_lab.feeds import store
    _news(conn, "w1", "苏丹停火谈判恢复", lane="world", region="非洲")
    _news(conn, "w2", "欧盟新一轮制裁", lane="world", region="欧洲")
    _news(conn, "w3", "地方体育新闻", lane="world", region="欧洲")
    _news(conn, "r1", "东盟中心性的新论证", lane="research", region="东南亚")
    day = date.today().isoformat()
    set_setting(conn, "news.brief:" + day, {"date": day, "events": [
        {"title": t, "what": "x", "sources": [{"id": i}]} for t, i in (("苏丹", "w1"), ("欧盟", "w2"))]})
    conn.commit()
    home = store.home(conn)
    assert [it["id"] for it in home["world"]] == ["w1", "w2"]
    assert [it["id"] for it in home["news"]] == ["r1"] and home["lane_totals"] == {"world": 3, "research": 1}
    assert [it["id"] for it in store.headlines(conn)["news"]] == ["w1", "w2", "r1"]
    page = client.get("/").text
    slide = page.split('id="front-news"')[1].split('</article>')[0]
    assert "苏丹" in slide and "地方体育新闻" not in slide          # the brief leads; BBC World has its item already
    assert f'href="/feed?tab=news&amp;day={day}"' in slide and 'id="feed-world"' not in page


def test_feed_page_news_by_day_with_lane_and_region_filters(client, conn):
    _news(conn, "w1", "苏丹停火谈判恢复", lane="world", importance=5, region="非洲", days_ago=0.0002)
    _news(conn, "w2", "欧盟新一轮制裁", lane="world", importance=4, region="欧洲", days_ago=2)
    _news(conn, "r1", "东盟中心性的新论证", lane="research", score=4, region="东南亚", days_ago=0.0004)
    conn.commit()
    page = client.get("/feed?pick=all").text                               # 新闻流, full stream
    assert page.count('class="stream-day') == 2 and "苏丹停火谈判恢复" in page and "东盟中心性的新论证" in page
    world = client.get("/feed?tab=news&lane=world").text
    assert "苏丹停火谈判恢复" in world and "东盟中心性的新论证" not in world
    africa = client.get("/feed?tab=news&region=非洲").text
    assert "苏丹停火谈判恢复" in africa and "欧盟新一轮制裁" not in africa
    assert '<option value="world" selected>' in world and '<option value="非洲" selected>' in africa


def test_load_more_continues_the_day_headers(client, conn):
    from research_lab.feeds import store
    for i in range(store.PAGE + 3):
        _news(conn, f"w{i}", f"世界新闻 {i}", lane="world", importance=3, region="全球", days_ago=0.00005 * i)
    conn.commit()
    page = client.get("/feed?tab=news&pick=all").text
    assert "last_day=" in page and page.count('class="stream-day') == 1
    day = datetime.now(timezone.utc).date().isoformat()
    more = client.get(f"/feed/more?tab=news&offset={store.PAGE}&last_day={day}").text
    assert 'class="stream-day' not in more                                  # same day: no repeated header


def test_news_page_opens_with_the_full_brief_and_earlier_days(client, conn):
    """Legacy brief links still open one edition, including its collapsed overflow."""
    from research_lab.db.connection import set_setting
    def ev(t, src):
        return {"title": t, "what": "发生了什么。", "why": "为什么重要。", "region": "全球", "direction": None,
                "sources": [{"id": src, "source": "BBC World", "url": f"https://example.org/{src}", "title": t}]}
    set_setting(conn, "news.brief:2026-10-01", {"date": "2026-10-01", "events": [ev("十月一日要闻", "a")]})
    set_setting(conn, "news.brief:2026-10-02", {"date": "2026-10-02", "events": [ev(f"十月二日要闻 {i}", f"b{i}") for i in range(8)]})
    conn.commit()
    page = client.get("/feed?tab=news&brief=2026-10-02").text
    block = page.split('id="digest"')[1].split("</section>")[0]
    assert block.count('class="story feed-event') == 8 and "十月二日要闻 7" in block and "助手整理" in block
    assert 'day=2026-10-01' in page and 'aria-label="后一天" title=' not in page
    earlier = client.get("/feed?tab=news&brief=2026-10-01").text
    assert "十月一日要闻" in earlier and "day=2026-10-02" in earlier
    assert 'feed-event' not in client.get("/feed?tab=papers").text


def test_brief_archive_skips_empty_days_and_finds_an_older_brief(client, conn):
    from research_lab.db.connection import set_setting
    ev = {"title": "九月二十八日要闻", "what": "x", "why": None, "region": "全球", "direction": None, "sources": []}
    set_setting(conn, "news.brief:2026-09-28", {"date": "2026-09-28", "events": [ev]})
    set_setting(conn, "news.brief:2026-09-29", {"date": "2026-09-29", "events": []})       # every event was dropped
    conn.commit()
    block = client.get("/feed?tab=news").text.split('id="digest"')[1].split("</section>")[0]  # nothing for today or yesterday
    assert "九月二十八日要闻" in block and "后一天" not in block                             # the empty day is skipped

