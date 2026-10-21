"""The daily edition and the full stream stay distinct without changing selection rules."""
from datetime import date, timedelta
from html import unescape
from pathlib import Path
import re
from xml.etree import ElementTree

import pytest

from research_lab.db.connection import get_setting, set_setting
from research_lab.feeds import store


def news(conn, item_id, day, *, source="BBC · World", lane="world"):
    conn.execute("INSERT INTO feed_news(id, source, title, link, summary, zh_summary, published, first_seen, lane) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (item_id, source, "Original " + item_id,
                 "https://example.org/" + item_id, "ENGLISH BODY SHOULD STAY CLOSED", "普通报道摘要",
                 day, day + "T09:00:00", lane))


def edition(conn, day, n=8):
    events = []
    for i in range(n):
        item_id = f"world-{day}-{i}"
        news(conn, item_id, day)
        events.append({"title": f"事件标题{i}", "what": f"事情经过{i}", "why": f"意义{i}", "region": "全球",
                       "sources": [{"id": item_id, "source": "BBC · World", "url": "https://example.org/" + item_id}]})
    news(conn, "research-" + day, day, lane="research")
    set_setting(conn, "news.brief:" + day, {"date": day, "events": events})
    set_setting(conn, "feeds.picks:" + day, {"news": [e["sources"][0]["id"] for e in events[:6]] + ["research-" + day], "papers": []})
    conn.commit()
    return events


def test_world_is_one_list_with_six_visible_events_and_collapsed_overflow(client, conn):
    day = (date.today() - timedelta(days=2)).isoformat()
    edition(conn, day)
    before = get_setting(conn, "feeds.picks:" + day)
    page = client.get("/feed?day=" + day).text
    assert page.count("<h3>世界要闻</h3>") == 1 and "世界政治简报" not in page
    first, rest = page.split('<details class="feed-overflow">', 1)
    assert len(re.findall('id="fe-[^"]+"', first)) == 6
    assert len(re.findall('id="fe-[^"]+"', rest)) == 2 and "另外 2 件" in rest
    for i in range(8):
        assert page.count(f"事件标题{i}</a>") == 1
        assert page.count(f"事情经过{i}") == 1
    assert "普通报道摘要" not in first and 'brief=' not in page
    assert get_setting(conn, "feeds.picks:" + day) == before


def test_one_day_pager_unites_brief_and_pick_days_and_accepts_legacy_links(client, conn):
    first, middle, last = [(date.today() - timedelta(days=i)).isoformat() for i in (5, 4, 3)]
    edition(conn, first, 1)
    edition(conn, last, 1)
    news(conn, "middle-research", middle, lane="research")
    set_setting(conn, "feeds.picks:" + middle, {"news": ["middle-research"], "papers": []})
    conn.commit()
    page = client.get(f"/feed?brief={first}").text
    assert page.count('class="edition-nav"') == 1 and f"day={middle}" in page and not re.search(r'href="[^\"]*brief=', page)
    middle_page = client.get(f"/feed?day={middle}&brief={first}").text
    assert "middle-research" in middle_page and "事件标题0" not in middle_page
    assert f"day={first}" in middle_page and f"day={last}" in middle_page
    assert "事件标题0" in client.get(f"/feed?day={last}").text
    assert client.get("/feed?day=2026-99-99").status_code == 200


def test_event_actions_keep_the_event_and_the_selected_report(client, conn):
    day = (date.today() - timedelta(days=2)).isoformat()
    ev = edition(conn, day, 1)[0]
    selected = ev["sources"][0]["id"]
    news(conn, "other-report", day, source="Other")
    ev["sources"].insert(0, {"id": "other-report", "source": "Other", "url": "https://example.org/other"})
    set_setting(conn, "news.brief:" + day, {"date": day, "events": [ev]})
    conn.commit()
    page = client.get("/feed?day=" + day).text
    assert f'hx-post="/feed/news/{selected}/save"' in page
    assert 'hx-post="/feed/news/other-report/save"' not in page
    shape = {"event_day": day, "event_index": "0"}
    saved = client.post(f"/feed/news/{selected}/save", data=shape).text
    assert f'id="fe-{day}-0"' in saved and "事件标题0" in saved and "事情经过0" in saved
    assert 'aria-pressed="true"' in saved and '"event_day"' in unescape(saved)
    dismissed = client.post(f"/feed/news/{selected}/dismiss", data=shape).text
    assert "已忽略" in dismissed and "Undo" in dismissed
    assert f'hx-post="/feed/news/{selected}/restore"' in client.get("/feed?day=" + day).text
    restored = client.post(f"/feed/news/{selected}/restore", data=shape).text
    assert "事情经过0" in restored and "已忽略" not in restored
    assert store.get(conn, "news", "other-report")["status"] == "new"


def test_overlapping_event_sources_keep_each_selected_report(client, conn):
    day = (date.today() - timedelta(days=2)).isoformat()
    events = edition(conn, day, 2)
    first, second = [ev["sources"][0] for ev in events]
    for ev in events:
        ev["sources"] = [first, second]
    set_setting(conn, "news.brief:" + day, {"date": day, "events": events})
    conn.commit()
    page = client.get("/feed?day=" + day).text
    for index, report in enumerate((first, second)):
        row = page.split(f'id="fe-{day}-{index}"', 1)[1].split("</li>", 1)[0]
        assert f'hx-post="/feed/news/{report["id"]}/save"' in row
    result = client.post(f'/feed/news/{first["id"]}/save', data={"event_day": day, "event_index": "²"})
    assert result.status_code == 200


def test_full_stream_is_titles_only_and_filters_are_in_one_menu(client, conn):
    day = (date.today() - timedelta(days=2)).isoformat()
    edition(conn, day)
    page = client.get("/feed?pick=all").text
    assert 'id="digest"' not in page and 'class="edition-nav"' not in page
    assert 'class="feed-overflow"' not in page and "事情经过0" not in page
    assert "ENGLISH BODY SHOULD STAY CLOSED" not in page and "普通报道摘要" not in page
    menu = page.split('<details class="menu"><summary class="btn secondary small">', 1)[1].split('</details>', 1)[0]
    assert all(f'name="{name}"' in menu for name in ("lane", "region", "source"))
    assert 'name="direction"' not in menu and 'class="tabs dir-filter"' in page   # the direction bar (card 0066)
    filtered = client.get("/feed?pick=all&lane=research").text
    assert "research-" + day in filtered and "Original world-" not in filtered
    saved = client.post(f"/feed/news/world-{day}-0/save").text
    assert "普通报道摘要" not in saved and "ENGLISH BODY SHOULD STAY CLOSED" not in saved


def test_paper_summary_authors_access_and_actions_use_the_same_row(client, conn):
    from research_lab.feeds import papers
    for key, access in (("open", {"oa_pdf_url": "https://example.org/p.pdf"}), ("closed", {"is_oa": 0})):
        papers.insert(conn, {"id": key, "doi": "10.9/" + key, "title": key, "journal": "TEST", "pool": "hunt",
                            "authors": ["Ada Lee, University of Test", "Bo Lin", "Cora Chen", "Dee Wu"],
                            "published": "2026-10-07", "abstract": "ENGLISH ABSTRACT", **access})
    conn.commit()
    page = client.get("/feed?tab=papers").text
    assert page.count("摘要待翻译") == 2 and "ENGLISH ABSTRACT" not in page
    assert "Ada Lee、Bo Lin、Cora Chen 等" in page and "University of Test" not in page
    # card 0106: access is a lock mark with a tooltip; the paper title itself reads (no separate button)
    assert 'class="access-mark open tip"' in page and 'data-tip="可获取原文"' in page
    assert 'class="access-mark closed tip"' in page and 'data-tip="需订阅"' in page
    assert 'title="阅读原文"' in page and 'title="查看文献"' in page and 'hx-post="/feed/papers/open/read"' in page
    assert 'aria-label="阅读原文"' not in page and "10月7日" in page
    assert "2026-10-07</span>" not in page
    all_items = client.get("/feed?tab=papers&pick=all").text
    assert "摘要待翻译" not in all_items and "Ada Lee" not in all_items


@pytest.mark.parametrize("path", ["/feed", "/feed?pick=all", "/feed?tab=papers", "/feed?tab=saved"])
def test_feed_highlights_home_in_sidebar(client, path):
    nav = client.get(path).text.split('<nav class="nav"', 1)[1].split('</nav>', 1)[0]
    assert '<a class="nav-item active" href="/" aria-current="page">' in nav


def test_chevron_left_exists_and_home_does_not_match_unrelated_routes():
    from research_lab.web.context import _active, NAV
    icons = Path(__file__).parents[1] / "research_lab/web/static/icons.svg"
    assert any(el.get("id") == "chevron-left" for el in ElementTree.parse(icons).iter())
    _, href, prefixes, _ = NAV[0]
    assert _active("/", href, prefixes) and _active("/feed/more", href, prefixes)
    assert not _active("/feedback", href, prefixes) and not _active("/papers", href, prefixes)


def test_event_actions_use_the_world_report_not_the_research_one(client, conn):
    """The brief also takes important research-lane reports; those keep their own row under 研究相关."""
    day = (date.today() - timedelta(days=2)).isoformat()
    ev = edition(conn, day, 1)[0]
    world_id = ev["sources"][0]["id"]
    research_id = "research-" + day
    ev["sources"].insert(0, {"id": research_id, "source": "BBC · World", "url": "https://example.org/" + research_id})
    set_setting(conn, "news.brief:" + day, {"date": day, "events": [ev]})
    set_setting(conn, "feeds.picks:" + day, {"news": [research_id, world_id], "papers": []})
    conn.commit()
    page = client.get("/feed?day=" + day).text
    row = page.split(f'id="fe-{day}-0"', 1)[1].split("</article>", 1)[0]
    assert f'hx-post="/feed/news/{world_id}/save"' in row and f"/feed/news/{research_id}/" not in row
    assert page.count(f'hx-post="/feed/news/{research_id}/save"') == 1
    saved = client.post(f"/feed/news/{research_id}/save", data={"event_day": day, "event_index": "0"}).text
    assert f'id="fe-{day}-0"' not in saved


def test_tab_counts_follow_the_edition_on_screen(client, conn):
    day = (date.today() - timedelta(days=2)).isoformat()
    edition(conn, day, 2)
    page = client.get("/feed").text
    assert f"day={day}" in page
    tab = re.search(r'<a href="/feed\?tab=news[^"]*"[^>]*>新闻<span class="count">(\d+)</span>', page)
    assert tab and int(tab.group(1)) > 0
