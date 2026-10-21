"""推送 wide layout, scheme 1 (card 0106, docs/design/FEED-LAYOUT.md): a newspaper grid up to 1440px, marks with
keyboard-reachable tooltips instead of tags, 收藏 / 忽略 on hover, the paper title opens the reader."""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import research_lab
from research_lab.db.connection import set_setting

STATIC = Path(research_lab.__file__).parent / "web" / "static"


def _news(conn, nid, day, *, lane="world", source="BBC World", direction=None, angle=None, status="new"):
    conn.execute("INSERT INTO feed_news(id, source, title, link, zh_title, zh_summary, published, first_seen, lane, direction, "
                 "angle, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (nid, source, "Original " + nid, "https://example.org/" + nid, "中文标题" + nid, "中文摘要" + nid, day,
                  day + "T09:00:00", lane, direction, angle, status))


def _edition(conn, day, sources=7):
    events = []
    for i in range(7):
        ids = [f"w{i}-{k}" for k in range(sources if i == 0 else 1)]
        for k, nid in enumerate(ids):
            _news(conn, nid, day, source=f"Outlet {k}")
        events.append({"title": f"事件{i}", "what": f"经过{i}", "why": f"意义{i}", "region": "中东" if i == 0 else "全球",
                       "direction": None, "sources": [{"id": nid, "source": f"Outlet {k}", "url": "https://example.org/" + nid}
                                                      for k, nid in enumerate(ids)]})
    set_setting(conn, "news.brief:" + day, {"date": day, "events": events})
    return events


def _paper(conn, pid, **kw):
    from research_lab.feeds import papers
    papers.insert(conn, {"id": pid, "doi": "10.9/" + pid, "title": "English " + pid, "journal": "TEST", "pool": "hunt",
                         "authors": ["Ada Lee", "Bo Lin"], "published": "2026-10-07", "abstract": "ABSTRACT", **kw})


def test_news_is_a_grid_with_a_lead_and_a_research_rail(client, conn):
    day = (date.today() - timedelta(days=1)).isoformat()
    _edition(conn, day)
    _news(conn, "r1", day, lane="research", source="War on the Rocks")
    set_setting(conn, "feeds.picks:" + day, {"news": ["w0-0", "r1"], "papers": []})
    conn.commit()
    page = client.get("/feed?day=" + day).text
    assert 'class="feed-bar"' in page and 'class="feed-layout"' in page
    assert 'class="story-grid world"' in page and 'class="story feed-event lead"' in page
    assert page.count('class="story feed-event') == 7 and 'class="feed-rail' in page and 'class="story-grid research-wide"' in page
    assert '<div class="kicker">中东</div>' in page                   # region above the title
    assert "numbered" not in page and "feed-number" not in page        # position already says the order


def test_many_outlets_show_two_and_a_list(client, conn):
    day = (date.today() - timedelta(days=1)).isoformat()
    _edition(conn, day)
    conn.commit()
    lead = client.get("/feed?day=" + day).text.split('class="story feed-event lead"', 1)[1].split("</article>", 1)[0]
    byline = lead.split('class="byline">', 1)[1].split("</span>", 1)[0]
    assert byline == "Outlet 0、Outlet 1"
    assert "等 7 家" in lead and lead.count('target="_blank" rel="noopener">Outlet') == 7   # every report in the menu


def test_papers_drop_radar_and_buttons_and_the_title_reads(client, conn):
    _paper(conn, "open", oa_pdf_url="https://example.org/p.pdf", radar=["deterrence"])
    _paper(conn, "closed", is_oa=0)
    conn.commit()
    page = client.get("/feed?tab=papers").text
    assert 'class="story-grid papers"' in page and "雷达" not in page and "badge info" not in page
    assert 'hx-post="/feed/papers/open/read"' in page and 'href="https://doi.org/10.9/open"' in page
    assert 'aria-label="阅读原文"' not in page and "download" not in page
    foot = page.split('id="fp-open"', 1)[1].split("</article>", 1)[0].split('class="foot"', 1)[1]
    order = [foot.index(k) for k in ('class="dir-dot', 'class="access-mark', 'class="byline"', 'class="hover-acts"')]
    assert order == sorted(order)                                       # §八: the source line keeps its order
    assert "TEST<span class=\"sep\"></span>10月7日<span class=\"sep\"></span>Ada Lee、Bo Lin" in foot


def test_marks_have_tooltips_that_the_keyboard_reaches(client, conn):
    from research_lab.intake import directions
    day = (date.today() - timedelta(days=1)).isoformat()
    slug = directions.load()[0]["slug"]
    _news(conn, "r1", day, lane="research", direction=slug, angle="和盟友安抚的关系")
    set_setting(conn, "feeds.picks:" + day, {"news": ["r1"], "papers": []})
    _paper(conn, "closed", is_oa=0)
    conn.commit()
    page = client.get("/feed?day=" + day).text
    dot = re.search(r'<span class="dir-dot tip"[^>]*>', page).group(0)
    assert 'tabindex="0"' in dot and "和盟友安抚的关系" in dot and "data-tip=" in dot and "aria-label=" in dot
    lock = re.search(r'<span class="access-mark closed tip"[^>]*>', client.get("/feed?tab=papers").text).group(0)
    assert 'tabindex="0"' in lock and 'data-tip="需订阅"' in lock
    css = (STATIC / "app.css").read_text()
    assert ".tip:focus-visible::after" in css and ".tip:hover::after" in css


def test_actions_appear_on_hover_and_a_saved_item_keeps_its_mark(client, conn):
    day = (date.today() - timedelta(days=1)).isoformat()
    _news(conn, "r1", day, lane="research")
    set_setting(conn, "feeds.picks:" + day, {"news": ["r1"], "papers": []})
    conn.commit()
    cell = client.post("/feed/news/r1/save", data={"layout": "story"}).text
    assert cell.lstrip().startswith('<article class="story') and 'class="hover-acts"' in cell
    assert 'aria-pressed="true"' in cell and "#bookmark-check" in cell
    gone = client.post("/feed/news/r1/dismiss", data={"layout": "story"}).text
    assert 'class="story dismissed"' in gone and "Undo" in gone            # the cell stays in place
    css = (STATIC / "app.css").read_text()
    assert '.hover-acts > .btn[aria-pressed="true"]' in css and "@media (hover: none) { .hover-acts > .btn" in css


def test_full_stream_is_one_line_per_item(client, conn):
    day = (date.today() - timedelta(days=1)).isoformat()
    for i in range(3):
        _news(conn, f"s{i}", day, lane="research")
    conn.commit()
    page = client.get("/feed?pick=all").text
    assert 'class="stream-list"' in page and page.count('class="stream-row"') == 3 and 'class="stream-day"' in page
    assert 'class="stream-tools"' in page and "中文摘要" not in page
    row = client.post("/feed/news/s0/save").text
    assert row.lstrip().startswith('<li class="stream-row"')


def test_the_page_fills_up_to_1440_and_the_marks_are_app_components():
    css = (STATIC / "feeds.css").read_text()
    assert ".content:has(> .feed-page) { max-width: calc(1440px + 64px); }" in css
    assert "repeat(12, minmax(0, 1fr))" in css
    assert ".feed-page { max-width: 760px; }" not in css
    # Card 0120: .tip, .dir-dot, .access-mark and .hover-acts are defined once, in app.css; feeds.css only places them.
    app = (STATIC / "app.css").read_text()
    for rule in (".tip {", ".tip[data-tip]::after {", ".dir-dot {", ".dir-dot::before {", ".access-mark {", ".hover-acts {"):
        starts = re.compile("^" + re.escape(rule), re.M)       # the definition, not a placement like `.story .hover-acts {`
        assert len(starts.findall(app)) == 1 and not starts.search(css), rule
