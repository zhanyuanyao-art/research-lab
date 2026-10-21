"""The one 研究方向 bar of 文献 · 数据 · 产出 · 推送 (card 0066): the same partial on all four pages, a pick
remembered across them in a cookie until 全部, nothing remembered for clients without the cookie."""
from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from research_lab.db.connection import set_setting
from research_lab.web.context import DIRECTION_COOKIE

PAGES = ("/papers", "/data", "/artifacts", "/feed")
SEA, US = "sea-asean", "alliance-us"


def bar(html: str) -> str:
    m = re.search(r'<nav class="tabs dir-filter" aria-label="研究方向">.*?</nav>', html, re.S)
    assert m, "no direction bar"
    return m.group(0)


def active(html: str) -> str:
    """The label of the highlighted entry: 全部 or a direction's name."""
    on = re.findall(r'<a href="[^"]*"(?: style="[^"]*")? class="on" aria-current="true">(?:<span class="dir-dot"[^>]*></span>)?([^<]+)', bar(html))
    assert len(on) == 1, on
    return on[0]


def _dataset(conn, ds_id: str, name: str, direction: str | None):
    at = "2026-10-01T00:00:00+00:00"
    conn.execute("INSERT INTO datasets(id, name, source_kind, original_name, rows, cols, schema_json, profile_json, direction, "
                 "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (ds_id, name, "csv", f"{name}.csv", 10, 3, "{}", "{}", direction, at, at))


@pytest.fixture()
def datasets(conn):
    _dataset(conn, "DS-2026-0001", "东盟峰会出席", SEA)
    _dataset(conn, "DS-2026-0002", "同盟承诺", US)
    conn.commit()


def test_the_same_bar_heads_all_four_pages(client):
    for page in PAGES:
        r = client.get(page)
        assert r.status_code == 200, page
        html = r.text
        assert active(html) == "全部", page
        links = bar(html)
        assert f"direction={SEA}" in links and f"direction={US}" in links and "东南亚与东盟研究" in links
        assert '<span class="dir-dot" aria-hidden="true"></span>' in links and "--dc:" in links
        assert "/static/direction-filter.css" in html
        assert DIRECTION_COOKIE not in r.headers.get("set-cookie", "")   # opening a page remembers nothing
    assert "/static/direction-filter.css" not in client.get("/issues").text


def test_a_pick_is_remembered_across_pages_until_all(client, datasets):
    r = client.get(f"/data?direction={SEA}&tab=datasets")
    assert f"{DIRECTION_COOKIE}={SEA}" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
    assert "东盟峰会出席" in r.text and "同盟承诺" not in r.text

    data = client.get("/data?tab=datasets").text                       # no ?direction=: the cookie decides
    assert active(data) == "东南亚与东盟研究" and "东盟峰会出席" in data and "同盟承诺" not in data
    arts = client.get("/artifacts").text
    assert active(arts) == "东南亚与东盟研究"
    assert 'href="/artifacts/ART-2026-4001"' in arts and 'href="/artifacts/ART-2026-4004"' not in arts
    papers = client.get("/papers").text
    assert active(papers) == "东南亚与东盟研究" and '<span class="current">东南亚与东盟研究</span>' in papers
    feed = client.get("/feed").text
    assert active(feed) == "东南亚与东盟研究" and 'id="digest"' in feed       # still 今日精选, not the stream

    switched = client.get(f"/artifacts?direction={US}")                # another page, another pick
    assert f"{DIRECTION_COOKIE}={US}" in switched.headers["set-cookie"]
    assert active(client.get("/data").text) == "联盟政治与美国研究"

    cleared = client.get("/papers?direction=")                         # 全部 forgets
    assert f'{DIRECTION_COOKIE}=""' in cleared.headers["set-cookie"] or "Max-Age=0" in cleared.headers["set-cookie"]
    for page in PAGES:
        assert active(client.get(page).text) == "全部", page
    assert "同盟承诺" in client.get("/data?tab=datasets").text


def test_the_bar_keeps_the_page_and_drops_one_shot_notices(client):
    html = client.get("/artifacts?view=week&group=direction&deleted=x").text
    links = bar(html)
    assert f'href="/artifacts?view=week&amp;group=direction&amp;direction={SEA}"' in links
    assert 'href="/artifacts?view=week&amp;group=direction&amp;direction="' in links
    assert "deleted" not in links


def test_only_known_directions_are_remembered(client, datasets):
    for url in ("/papers?direction=none", "/papers?direction=_none", "/data?direction=made-up"):
        assert DIRECTION_COOKIE not in client.get(url).headers.get("set-cookie", ""), url
    assert "/static/direction-filter.css" in client.get("/papers?direction=none").text   # still filters that page
    client.cookies.set(DIRECTION_COOKIE, "removed-direction")          # a direction deleted since
    data = client.get("/data?tab=datasets").text
    assert active(data) == "全部" and "同盟承诺" in data and "东盟峰会出席" in data


def test_a_project_or_a_folder_is_not_narrowed_by_the_remembered_direction(client):
    client.get(f"/data?direction={US}")
    papers = client.get("/papers?project=asean-centrality").text       # a 课题 has its own direction
    assert active(papers) == "全部"
    arts = client.get("/artifacts?project=asean-centrality").text
    assert active(arts) == "全部" and 'href="/artifacts/ART-2026-4001"' in arts
    folders = client.get("/papers?view=folders").text                  # folders cut across directions: no bar
    assert 'class="tabs dir-filter"' not in folders and "/static/direction-filter.css" not in folders
    # with a direction picked, 课题 lists only that direction's projects
    us = client.get("/artifacts").text
    select = us.split('<select name="project"', 1)[1].split("</select>", 1)[0]
    assert 'value="us-rok-reassurance"' in select and 'value="asean-centrality"' not in select
    assert f'<input type="hidden" name="direction" value="{US}">' in us


def _news(conn, nid: str, day: str, lane: str, direction: str | None):
    conn.execute("INSERT INTO feed_news(id, source, title, link, zh_title, zh_summary, published, first_seen, lane, direction, status) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (nid, "BBC World", "Original " + nid, "https://example.org/" + nid,
                                                     "中文标题" + nid, "中文摘要" + nid, day, day + "T09:00:00", lane, direction, "new"))


def _paper(conn, pid: str, direction: str):
    from research_lab.feeds import papers
    papers.insert(conn, {"id": pid, "doi": "10.9/" + pid, "title": "English " + pid, "journal": "TEST", "pool": "hunt",
                         "authors": ["Ada Lee"], "published": "2026-10-07", "abstract": "ABSTRACT", "direction": direction})


def test_feed_narrows_research_and_papers_but_keeps_world_news(client, conn):
    day = (date.today() - timedelta(days=1)).isoformat()
    _news(conn, "world1", day, "world", None)
    _news(conn, "seanews", day, "research", SEA)
    _news(conn, "usnews", day, "research", US)
    _paper(conn, "seapaper", SEA)
    _paper(conn, "uspaper", US)
    set_setting(conn, "feeds.picks:" + day, {"news": ["world1", "seanews", "usnews"], "papers": ["seapaper", "uspaper"]})
    conn.commit()
    everything = client.get(f"/feed?day={day}").text
    assert "中文标题seanews" in everything and "中文标题usnews" in everything

    news = client.get(f"/feed?day={day}&direction={SEA}").text
    assert 'id="digest"' in news                                        # a direction is a lens, not browsing
    assert "中文标题world1" in news and "中文标题seanews" in news and "中文标题usnews" not in news
    papers = client.get(f"/feed?tab=papers&day={day}").text             # remembered on the next tab
    assert active(papers) == "东南亚与东盟研究"
    assert "English seapaper" in papers and "English uspaper" not in papers

    stream = client.get("/feed?pick=all").text
    assert "中文标题seanews" in stream and "中文标题usnews" not in stream
    form = stream.split('id="feed-filters"', 1)[1].split("</form>", 1)[0]
    assert f'<input type="hidden" name="direction" value="{SEA}">' in form   # the search keeps the pick
    assert 'name="direction" aria-label' not in form                         # no second direction picker
