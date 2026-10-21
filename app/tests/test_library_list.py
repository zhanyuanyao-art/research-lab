"""The library list (card 0128, docs/design/LIBRARY-UI.md §3.1、§四、§五): 全部 grouped by 方向, fixed-track rows whose
mark slots keep their place, the 概要 mark in three states, the preview fragment, and old links still working."""
from __future__ import annotations

import json
import re

import pytest

from research_lab.web.routes.papers import byline

SEA, US = "sea-asean", "alliance-us"
AT = "2026-10-0{}T08:00:00+00:00"


def _paper(conn, pid: str, title: str, *, direction=None, authors=("Tan, See Seng",), year=2023, summary=None,
           read_depth=None, day=1, file=True, kind="article", venue=None):
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, venue, kind, direction, read_depth, metadata_status, "
                 "ai_summary_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (pid, title, "h-" + pid, json.dumps(list(authors)), year, venue, kind, direction, read_depth, "verified",
                  json.dumps(summary, ensure_ascii=False) if summary else None, AT.format(day), AT.format(day)))
    if file:
        conn.execute("INSERT INTO paper_files(paper_id, sha256, rel_path, original_name, bytes, page_count, has_text_layer, extractor, "
                     "is_primary, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (pid, "sha-" + pid, f"papers/{pid}.pdf", f"{pid}.pdf", 1000, 19, 1, "pymupdf", 1, AT.format(day)))


FULL = {"research_question": {"text": "大国为何持续出席东盟主导的论坛？", "pages": ["2", "3"]},
        "argument": {"text": "出席本身就是承诺的信号。", "pages": ["4"]},
        "mechanism": {"text": "MECHANISM STAYS IN THE READER", "pages": []}}
LIGHT = {"research_question": {"text": "轻读的问题", "pages": ["1"]}, "argument": {"text": "轻读的论点", "pages": []}, "mode": "light"}


@pytest.fixture()
def library(conn):
    _paper(conn, "sea-full", "ASEAN Centrality Revisited", direction=SEA, summary=FULL, read_depth="read", day=3,
           authors=("Tan, See Seng", "Acharya, Amitav"), venue="Pacific Review")
    _paper(conn, "sea-light", "Who Convenes Asia?", direction=SEA, summary=LIGHT, read_depth="skimmed", day=2,
           authors=("Ba, Alice", "Kuik, Cheng-Chwee", "Goh, Evelyn"))
    _paper(conn, "us-none", "Reassuring Seoul", direction=US, day=1, authors=())
    _paper(conn, "loose", "A Paper Without a Direction", day=4, authors=("国际关系研究所",), year=None)
    _paper(conn, "no-file", "The Struggle for Order", direction=SEA, file=False)
    conn.execute("INSERT INTO paper_projects(paper_id, project_slug) VALUES ('sea-full', 'asean-centrality')")
    conn.commit()


def lines(html: str) -> dict[str, str]:
    return {m.group(1): m.group(0) for m in re.finditer(r'<li class="paper-line" data-paper-id="([^"]+)".*?</li>', html, re.S)}


def groups(html: str) -> list[str]:
    return re.findall(r'<section class="lib-group" data-line-group>\s*<header><a class="dir-tag"[^>]*>([^<]+)</a>', html)


def test_all_is_grouped_by_direction_in_directions_yml_order(client, library):
    page = client.get("/papers").text
    assert groups(page) == ["联盟政治与美国研究", "东南亚与东盟研究", "未分类"]          # no empty groups; 未分类 last
    sea = page.split(">东南亚与东盟研究</a>", 1)[1].split("</section>", 1)[0]
    assert '<span class="n">2</span>' in sea and "篇未读" not in sea               # one read, one reading
    assert '<span class="unread">1 篇未读</span>' in page.split(">联盟政治与美国研究</a>", 1)[1].split("</section>", 1)[0]
    assert sea.index("sea-full") < sea.index("sea-light")                           # newest import first
    assert "The Struggle for Order" not in page                                     # no original: behind 待补原文
    assert 'href="/papers?direction=sea-asean"' in page and 'href="/papers?direction=_none"' in page
    assert "3 篇有原文" not in page and "4 篇有原文 · 2 篇还没有概要" in page and "待补原文 1</a>" in page
    assert client.get("/papers?view=all").text.count('class="lib-group"') == 3        # old ?view=all is 全部
    flat = client.get("/papers?group=none").text
    assert 'class="lib-group"' not in flat and len(lines(flat)) == 4


def test_rows_have_fixed_tracks_and_mark_slots_that_keep_their_place(client, library):
    rows = lines(client.get("/papers").text)
    assert set(rows) == {"sea-full", "sea-light", "us-none", "loose"}
    for row in rows.values():
        order = [row.index(c) for c in ('class="issue-state', 'class="pl-title"', 'class="pl-marks"', 'class="pl-by"',
                                        'class="pl-date', 'class="hover-acts"')]
        assert order == sorted(order)
        marks = row.split('class="pl-marks">', 1)[1].split('<span class="pl-by">', 1)[0]
        assert len(re.findall(r'<span class="(?:dir-dot|sig)\b', marks)) == 4              # 方向 · 概要 · 课题 · 议题
    assert 'class="dir-dot none"' in rows["sea-full"]                                    # grouped: no repeated 方向
    full = rows["sea-full"]
    assert 'class="sig on tip"' in full and 'data-tip="中文概要：完整"' in full
    assert 'data-tip="课题\nASEAN Centrality"' in full and "Tan、Acharya · 2023" in full
    assert 'class="issue-state done tip"' in full and 'data-tip="读完"' in full
    light = rows["sea-light"]
    assert 'class="sig light tip"' in light and "轻读" in light and "Ba 等 · 2023" in light
    assert 'class="issue-state doing tip"' in light
    none = rows["us-none"]
    assert "中文概要" not in none and none.count('<span class="sig" aria-hidden="true"></span>') == 3
    assert '<span class="pl-by">2023</span>' in none and 'data-tip="未读"' in none
    assert '<span class="pl-by">国际关系研究所</span>' in rows["loose"]
    assert "10月3日" in full and "导入于 10月3日" in full


def test_a_direction_is_one_ungrouped_list_and_other_lists_mark_the_direction(client, library):
    page = client.get("/papers?direction=sea-asean").text
    assert 'class="lib-group"' not in page and list(lines(page)) == ["sea-full", "sea-light"]
    assert 'class="dir-dot none"' in page and '<span class="current">东南亚与东盟研究</span>' in page
    flat = lines(client.get("/papers?direction=&group=none").text)
    assert 'data-tip="东南亚与东盟研究"' in flat["sea-full"] and 'data-tip="未分类"' in flat["loose"]
    assert list(lines(client.get("/papers?direction=&group=none&sort=title").text)) == ["loose", "sea-full", "us-none", "sea-light"]
    project = lines(client.get("/papers?project=asean-centrality").text)
    assert list(project) == ["sea-full"]
    missing = client.get("/papers?src=missing").text
    assert list(lines(missing)) == ["no-file"] and 'data-tip="待补原文"' in missing and 'data-file="0"' in missing


def test_the_preview_shows_question_argument_projects_and_actions(client, library):
    peek = client.get("/papers/sea-full/peek")
    assert peek.status_code == 200 and "<html" not in peek.text
    text = peek.text
    assert "东南亚与东盟研究" in text and "期刊论文" in text and "19 页" in text
    assert "Tan, See Seng、Acharya, Amitav · 2023 · Pacific Review" in text
    assert "研究问题" in text and "大国为何持续出席东盟主导的论坛？" in text and "核心论点" in text
    assert 'href="/papers/sea-full/read?page=2"' in text and "MECHANISM STAYS IN THE READER" not in text
    assert 'href="/projects/asean-centrality"' in text and ">Read<" in text.replace("</svg>", ">") and "Open PDF" in text
    light = client.get("/papers/sea-light/peek").text
    assert "轻读的问题" in light and "轻读概要" in light
    bare = client.get("/papers/us-none/peek").text
    assert "还没有中文概要" in bare
    from research_lab.db.connection import connect
    assert connect().execute("SELECT last_opened_at FROM papers WHERE id = 'sea-full'").fetchone()[0] is None  # a peek is not an open
    assert client.get("/papers/missing-paper/peek").status_code == 404


def test_the_page_carries_the_bar_toolbar_menu_template_and_scripts(client, library):
    page = client.get("/papers").text
    assert 'class="tabs dir-filter"' in page and 'role="toolbar"' in page and "data-line-filter" in page
    assert 'id="lib-peek"' in page and 'id="paper-line-menu"' in page and "/static/papers.js" in page
    menu = page.split('id="paper-line-menu">', 1)[1].split("</template>", 1)[0]
    assert "Mark as read" in menu and "Set direction" in menu and "Add to project" in menu and "Open PDF" in menu
    assert 'formaction="/papers/__ID__/status"' in menu and 'action="/papers/__ID__/archive"' in menu
    assert '>Import</label>' in page and 'id="dropzone"' in page
    js = (client.get("/static/papers.js").text)
    assert "/peek" in js and "'j'" in js and "paper-line-menu" in js


@pytest.mark.parametrize("authors, year, expected", [
    (["Coe, Andrew J.", "Vaynman, Jane"], 2020, "Coe、Vaynman · 2020"),
    (["Andrew Coe", "Jane Vaynman", "X Y"], 2020, "Coe 等 · 2020"),
    (["FEARON, JAMES D."], 1995, "Fearon · 1995"),
    (["E3/EU+3 and Iran"], 2013, "E3/EU+3 and Iran · 2013"),
    (["王缉思"], None, "王缉思"),
    ([], None, "—"),
])
def test_byline(authors, year, expected):
    assert byline(authors, year) == expected
