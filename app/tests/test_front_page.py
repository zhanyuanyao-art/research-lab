"""首页: the newspaper carousel, three items per push stream with 查看更多, and the day's note."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(microsecond=0).isoformat()


def seed(conn, n_news=7, n_papers=7):
    for i in range(n_news):   # ages 0–1.8 days, never on a boundary; one outlet each; n4 hits a direction keyword
        conn.execute("INSERT INTO feed_news(id, source, title, link, zh_title, angle, direction, published, first_seen) "
                     "VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"n{i}", f"Outlet {i}", f"News {i}", f"https://example.org/n{i}", f"新闻标题{i}",
                      f"为什么重要{i}", "alliance-us" if i == 4 else None, _iso(i * 0.3)[:10], _iso(i * 0.3)))
    for i in range(n_papers):
        conn.execute("INSERT INTO feed_papers(id, title, zh_title, journal, journal_name, authors_json, radar, direction, published, first_seen) "
                     "VALUES (?,?,?,?,?,?,?,'sea-asean',?,?)",
                     (f"p{i}", f"Paper {i}", f"论文标题{i}", "TPR", "The Pacific Review", '["Tan"]', 1 if i == 3 else 0,
                      _iso(i)[:10], _iso(i + 2)))


def test_headlines_order_and_fresh_counts(conn):
    from research_lab.feeds import store
    seed(conn)
    h = store.headlines(conn)
    assert [it["id"] for it in h["news"]] == ["n4", "n0", "n1"]                     # a keyword hit, then the newest; 1 lead + 2 seconds
    assert len(h["papers"]) == 3
    assert h["papers"][0]["id"] == "p3"                                  # a radar hit leads among recent papers
    assert h["fresh"] == {"papers": 0, "news": 4}                        # first seen in the last 24 hours (0–0.9 days)
    assert h["news"][0]["display_title"] == "新闻标题4" and h["news"][0]["angle"] == "为什么重要4"


def test_daily_note_is_stable_per_day():
    from research_lab import home_notes
    d = date(2026, 10, 1)
    assert home_notes.for_day(d) == home_notes.for_day(d) and home_notes.for_day(d) in home_notes.NOTES
    assert home_notes.for_day(d) != home_notes.for_day(d + timedelta(days=1))


def test_home_page_front_carousel_and_push_limit(client, repo):
    from research_lab import home_notes
    from research_lab.db.connection import connect
    conn = connect()
    seed(conn)
    conn.close()
    page = client.get("/").text
    assert 'aria-roledescription="carousel"' in page and "研究日报" not in page
    assert page.count('role="tab"') == 2 and "新闻" in page and "论文" in page and "课题进展" in page
    assert "新闻标题4" in page and "论文标题3" in page
    # each stream page: one lead and two → rows in the 本周 page's frame (card 0055); no byline or fresh-count line
    assert page.count('class="today front-stream"') == 2 and page.count('<span class="row-title"') == 4
    assert 'id="home-progress"' in page and 'class="front-foot"' not in page
    assert 'class="byline"' not in page and "过去一天新增" not in page
    # no progress line or countdown (card 0044: Leo read it as being hurried); the layout lives in front.css
    assert "front-countdown" not in page and "front.css" in page
    head = page.split('class="front-controls"')[1].split('</header>')[0]
    assert head.index('data-front="refresh"') < head.index('data-front="prev"') < head.index('role="tablist"') < head.index('data-front="next"')
    assert 'href="/feed?tab=papers"' in page and 'href="/feed?tab=news"' in page
    assert 'id="feed-tabs"' not in page and "查看更多" not in page
    assert home_notes.for_day() in page
    assert "front.js" in page


def test_home_empty_streams_keep_carousel_and_secondary_entrypoints(client, repo):
    page = client.get("/").text
    assert 'aria-roledescription="carousel"' in page and page.count('role="tab"') == 2
    assert page.count("Today's picks") >= 2 and 'href="/feed?tab=papers"' in page and 'id="home-progress"' in page


def _front(page: str, slide: str) -> str:
    return page.split(f'id="front-{slide}"')[1].split('</article>')[0]


def test_front_papers_keep_chinese_and_the_english_original(client, repo):
    """Card 0044: papers lead with the Chinese title, the English original as one small line under every
    title, a Chinese deck under the lead; what has no Chinese yet says 待翻译 instead of showing only English."""
    from research_lab.db.connection import connect
    conn = connect()
    seed(conn, n_papers=0)
    rows = [("p0", "Hedging in Southeast Asia", "东南亚的对冲", "文章认为东盟以议程设置维持召集地位。", 0),
            ("p1", "Alliance Credibility", "同盟可信度", None, 1),
            ("p2", "Untranslated Paper", None, None, 2)]
    for pid, title, zh, zh_abs, age in rows:
        conn.execute("INSERT INTO feed_papers(id, title, zh_title, zh_abstract, journal, journal_name, authors_json, score, published, first_seen) "
                     "VALUES (?,?,?,?,?,?,?,4,?,?)", (pid, title, zh, zh_abs, "TPR", "The Pacific Review", "[]", _iso(age)[:10], _iso(age)))
    conn.commit()
    conn.close()
    papers = _front(client.get("/").text, "papers")
    lead = papers.split('class="actions-col"')[0]
    assert "东南亚的对冲" in lead and '<p class="orig" lang="en">Hedging in Southeast Asia</p>' in lead
    assert '<p class="deck">文章认为东盟以议程设置维持召集地位。</p>' in lead and "待翻译" not in lead
    rows = papers.split('class="todo-strip"')[1].split("</a>")
    # → rows (card 0055): the Chinese title, the English original on hover
    assert "同盟可信度" in rows[0] and 'title="Alliance Credibility"' in rows[0]
    # no Chinese title: the English title stands, marked 待翻译
    assert 'class="zh-missing">待翻译' in rows[1] and 'lang="en"' in rows[1] and "Untranslated Paper" in rows[1]


def test_front_paper_lead_without_chinese_summary_says_pending(client, repo):
    from research_lab.db.connection import connect
    conn = connect()
    seed(conn, n_papers=1)
    conn.commit()
    conn.close()
    page = client.get("/").text
    assert '<p class="deck pending">中文摘要待翻译</p>' in _front(page, "papers")
    assert "中文摘要待翻译" not in _front(page, "news") and 'class="orig"' not in _front(page, "news")


def test_home_keeps_announcements_off_and_shows_three_per_stream(client, repo):
    """Cards 0050, 0083: a site's own announcement never reaches 首页; nothing waits for a score any more, and each
    stream shows three."""
    from research_lab.db.connection import connect
    from research_lab.feeds import news, store
    conn = connect()
    rows = [("out", "The Navigator’s September Issue — Out Now", "9DASHLINE《领航者》九月刊现已发布", "9DASHLINE")]
    rows += [(f"good{i}", f"Relevant {i}", f"相关评论{i}", f"Outlet {i}") for i in range(5)]
    for nid, title, zh, source in rows:
        conn.execute("INSERT INTO feed_news(id, source, title, link, zh_title, published, first_seen) VALUES (?,?,?,?,?,?,?)",
                     (nid, source, title, f"https://example.org/{nid}", zh, _iso(0.1)[:10], _iso(0.1)))
    for pid in ("p-a", "p-b", "p-c", "p-d"):
        conn.execute("INSERT INTO feed_papers(id, title, journal, journal_name, authors_json, published, first_seen) "
                     "VALUES (?,?,?,?,?,?,?)", (pid, f"Paper {pid}", "TPR", "The Pacific Review", "[]", _iso(0.2)[:10], _iso(0.2)))
    assert news.flag_not_news(conn) == 1
    assert conn.execute("SELECT category, score, enriched_at IS NOT NULL FROM feed_news WHERE id = 'out'").fetchone()[:] == ("非新闻", 0, 1)
    home = store.home(conn)
    assert len(home["news"]) == store.HOME_LIMIT == 3 and {it["id"] for it in home["news"]} <= {f"good{i}" for i in range(5)}
    assert len(home["papers"]) == 3
    assert {it["id"] for it in store.headlines(conn)["news"]} <= {f"good{i}" for i in range(5)}
    conn.commit()
    conn.close()
    page = client.get("/").text
    assert "领航者" not in page and "Out Now" not in page
    assert "领航者" not in client.get("/feed?tab=news").text                  # off 精选 (card 0067)
    assert "领航者" in client.get("/feed?tab=news&pick=all").text             # kept under 全部条目, not deleted


def test_assistant_score_never_overrides_the_not_news_rule(conn):
    from research_lab.feeds import enrich, news
    conn.execute("INSERT INTO feed_news(id, source, title, link, first_seen, enrich_batch) VALUES "
                 "('x', '9DASHLINE', 'Our Newsletter — Out Now', 'https://example.org/x', ?, 'news:b1')", (_iso(0),))
    news.flag_not_news(conn)
    job = conn.execute("INSERT INTO ai_jobs(id, kind, status, request_json, schema_json, created_at) VALUES "
                       "('J-1', 'feeds.enrich', 'claimed', ?, '{}', ?)", ('{"params": {"stream": "news", "ids": {"i1": "x"}}}', _iso(0)))
    run = conn.execute("INSERT INTO ai_runs(job_id, provider, model, started_at) VALUES ('J-1', 'test', 'test', ?)", (_iso(0),)).lastrowid
    enrich.SPEC.apply(enrich.FeedEnrichment(items=[{"id": "i1", "score": 5, "category": "地区安全"}]), run, conn, "news:b1")
    assert conn.execute("SELECT score, category FROM feed_news WHERE id = 'x'").fetchone()[:] == (0, "非新闻")


NEWS_LOOKALIKES = [
    "China May Issue New Export Controls on Rare Earths", "Taiwan Becomes a New Issue in US-Japan Talks",
    "The Current Issue Dividing ASEAN", "Why Voters Register for the Election in Indonesia",
    "Seoul Wants Washington to Join Us in Deterrence", "Philippines Now Live-Fires Near Scarborough",
    "Trump Administration Now Hiring Hawks for the NSC", "中国军队招聘制度改革的战略含义",
    "Episodic Summitry Alone Cannot Reset India-China Relations", "Bookshelf: China’s new tech titans",
    "Trump Is Out of Options on Iran", "The March Issue That Split the Quad", "Podcasts and Propaganda in Southeast Asia",
    "New CSIS Report Now Available: Deterrence in the Taiwan Strait", "《外交事务》月刊文章：美国同盟的可信度",
    "Episode 3 of the Taiwan Crisis Simulation Shows Gaps", "Japan’s May Issue of Defense White Paper Raises Alarm",
]
ANNOUNCEMENTS = [
    "The Navigator’s September Issue — Out Now", "Our October Newsletter Is Out", "The September 2026 Issue",
    "Podcast: ASEAN at the Crossroads", "Asia Matters | Podcast", "Episode 42: Deterrence in the Strait",
    "Webinar: Maritime Law After the Ruling", "Call for Papers: Asian Security Workshop", "We’re hiring a research fellow",
    "9DASHLINE《领航者》九月刊现已发布", "研究院招聘启事", "播客｜东南亚观察",
]


def test_not_news_rule_is_narrow():
    from research_lab.feeds.news import is_not_news
    assert [t for t in NEWS_LOOKALIKES if is_not_news(t)] == []
    assert [t for t in ANNOUNCEMENTS if not is_not_news(t)] == []


def test_items_the_narrowed_rule_no_longer_matches_come_back_unscored(conn):
    from research_lab.feeds import news
    conn.execute("INSERT INTO feed_news(id, source, title, link, category, score, enriched_at, first_seen) VALUES "
                 "('old', 'The Diplomat', 'Taiwan Becomes a New Issue in US-Japan Talks', 'https://example.org/old', '非新闻', 0, ?, ?)",
                 (_iso(0), _iso(0)))
    assert news.flag_not_news(conn) == 0
    assert conn.execute("SELECT category, score, enriched_at FROM feed_news WHERE id = 'old'").fetchone()[:] == (None, None, None)


def test_translation_never_decides_not_news(conn):
    """The assistant's 现已发布 for "Now Available" must not hide a think-tank report."""
    from research_lab.feeds import news
    conn.execute("INSERT INTO feed_news(id, source, title, link, zh_title, score, first_seen) VALUES (?,?,?,?,?,4,?)",
                 ("csis", "CSIS", "New CSIS Report Now Available: Deterrence in the Taiwan Strait", "https://example.org/csis",
                  "CSIS新报告现已发布：台海威慑的三个缺口", _iso(0)))
    assert news.flag_not_news(conn) == 0
    assert conn.execute("SELECT category, score FROM feed_news WHERE id = 'csis'").fetchone()[:] == (None, 4)


def test_home_progress_is_below_brief_tasks_and_carousel_has_only_news_and_papers(client, repo):
    page = client.get('/').text
    assert page.index('id="front"') < page.index('<h2>待办</h2>') < page.index('id="home-progress"')
    assert '<h2>推送</h2>' not in page and 'id="feed-tabs"' not in page
    tabs = page.split('class="front-tabs"') if 'class="front-tabs"' in page else page.split('class="segmented small front-tabs"')
    tabs = tabs[1].split('</div>')[0]
    assert tabs.index('front-tab-news') < tabs.index('front-tab-papers')
    assert 'front-tab-progress' not in page
    assert 'id="front-tab-news" aria-controls="front-news" aria-selected="true"' in tabs


def test_stream_slides_carry_no_padding_of_their_own():
    """The .today frame pads all three pages alike; the slide must not add its own on top (card 0055 review)."""
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1] / "research_lab/web/static/front.css").read_text(encoding="utf-8")
    assert ".front-slide.front-news, .front-slide.front-papers { padding: 0; }" in css
