"""The paper reader page: sidebar context, the reading-view switch without a translation, and the
excerpt form's page default when nothing is selected."""
from __future__ import annotations

import re

import httpx


def _ingest(sample_pdf):
    from research_lab.db.connection import connect
    from research_lab.intake import pipeline
    conn = connect()
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="down"))) as http:
        pid = pipeline.ingest(conn, sample_pdf, http=http, ai=False).paper_id
    return conn, pid


def test_reader_sidebar_keeps_direction_colours(client, sample_pdf):
    conn, pid = _ingest(sample_pdf)
    page = client.get(f"/papers/{pid}").text
    nav_dirs = re.findall(r'<a class="nav-dir[^"]*"[^>]*style="--dc: ([^"]*)"', page)
    assert nav_dirs and all(c.startswith("var(--d-") for c in nav_dirs)
    library = client.get("/papers").text
    assert 'class="tabs dir-filter"' in library and 'id="library"' in library
    assert nav_dirs == re.findall(r'<a class="nav-dir[^"]*"[^>]*style="--dc: ([^"]*)"', library)  # app navigation stays stable
    # the 方向 property's menu lists every direction (card 0129)
    picker = page.split('aria-label="设定方向"')[1].split("</form>")[0]
    assert all(f'value="{d}"' in picker for d in ("alliance-us", "sea-asean", "strategy-signaling", "periphery-nts"))
    conn.close()


def test_views_are_dimmed_without_a_translation(client, sample_pdf):
    from research_lab.db.connection import now_iso
    from research_lab.intake import pipeline
    conn, pid = _ingest(sample_pdf)
    page = client.get(f"/papers/{pid}").text
    assert 'aria-label="阅读视图"' not in page
    assert 'data-reader-info="translation-card"' in page
    assert 'id="pdf-viewer"' in page

    f = pipeline.primary_file(conn, pid)
    conn.execute("INSERT INTO paper_translations(paper_id, file_id, page_index, lang_from, lang_to, text, created_at) "
                 "VALUES (?, ?, 0, 'en', 'zh', '第一页译文', ?)", (pid, f["id"], now_iso()))
    page = client.get(f"/papers/{pid}").text
    views = page.split('aria-label="阅读视图"')[1].split("</nav>")[0]
    assert "view=trans" in views and "view=both" in views and "aria-disabled" not in views
    assert 'data-reader-info="translation-card"' not in page
    conn.close()


def test_excerpt_form_takes_the_reader_page_without_a_selection(client, sample_pdf):
    # excerpts.js sends the reader's current page (page_index + page_label) when nothing is selected.
    conn, pid = _ingest(sample_pdf)
    form = client.get("/_p/excerpt-form", params={"paper_id": pid, "page_index": 1, "page_label": "245"}).text
    assert 'name="page_index" value="1"' in form and 'name="locator" value="245"' in form
    bare = client.get("/_p/excerpt-form", params={"paper_id": pid, "page_index": 2}).text
    assert 'name="locator" value="3"' in bare
    conn.close()


# ---------------------------------------------------------------- card 0129: one header row, side panel, list walk

import json  # noqa: E402

AT = "2026-10-0{}T08:00:00+00:00"


def _paper(conn, pid, title, *, direction="sea-asean", day=1, summary=None, authors=("Coe, Andrew J.", "Vaynman, Jane"),
           venue="American Political Science Review", doi=None):
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, venue, doi, kind, direction, metadata_status, "
                 "ai_summary_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (pid, title, "h-" + pid, json.dumps(list(authors)), 2020, venue, doi, "article", direction, "unverified",
                  json.dumps(summary, ensure_ascii=False) if summary else None, AT.format(day), AT.format(day)))
    conn.execute("INSERT INTO paper_files(paper_id, sha256, rel_path, original_name, bytes, page_count, has_text_layer, extractor, "
                 "is_primary, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (pid, "sha-" + pid, f"papers/{pid}.pdf", f"{pid}.pdf", 1000, 14, 1, "pymupdf", 1, AT.format(day)))


LIGHT = {"research_question": {"text": "为什么军备控制罕见？", "pages": ["1"]},
         "argument": {"text": "透明与安全之间的权衡。", "pages": ["1", "2", "6"]}, "mode": "light"}


def test_the_reader_is_one_header_row_with_a_side_panel(client, conn):
    _paper(conn, "coe2020", "Why Arms Control Is So Rare", summary=LIGHT, doi="10.1017/s0003055419000731")
    conn.commit()
    page = client.get("/papers/coe2020").text
    crumbs = page.split('aria-label="位置"')[1].split("</nav>")[0]
    assert 'href="/papers"' in crumbs and 'href="/papers?direction=sea-asean"' in crumbs and "Coe、Vaynman · 2020" in crumbs
    head = page.split('<header class="read-head">')[1].split("</header>")[0]
    assert "<h1" in head and "Coe、Vaynman · 2020 · American Political Science Review" in head
    assert "Coe, Andrew J.、Vaynman, Jane" in head and "doi:10.1017/s0003055419000731" in head     # the byline's tooltip
    assert 'id="side-toggle"' in head and "Open PDF" in head and "Plan replication" in head and "Regenerate" in head
    assert 'id="reading-information"' not in page and "收起概要" not in page and 'class="btn ghost small" href="/papers' not in head
    props = page.split('id="paper-props"')[1].split("</dl>")[0]
    assert [m for m in re.findall(r"<dt>([^<]+)</dt>", props)] == ["状态", "方向", "课题", "议题"]
    side = page.split('id="side-summary"')[1].split('id="side-info"')[0]
    assert "为什么军备控制罕见？" in side and 'class="sum-light"' in side and ">Full summary</button>" in side
    assert side.index('id="paper-props"') < side.index('id="summary-card"')
    info = page.split('id="side-info"')[1]
    assert all(f"<h3>{h}</h3>" in info for h in ("引用信息", "文件", "译文", "整理")) and ">Copy</button>" in info and ">Add file</label>" in info
    assert ">Verify" not in info and "核验" not in info and "编辑引用信息" not in info                # card 0200
    assert 'data-mtab="doc"' in page and 'value="auto"' in page                                   # phone rail; zoom starts at 自动缩放


def test_previous_and_next_walk_the_list_the_paper_came_from(client, conn):
    _paper(conn, "b-paper", "Beta Alliance", day=1)
    _paper(conn, "a-paper", "Alpha Alliance", day=2)
    _paper(conn, "c-paper", "Gamma Alliance", day=3)
    _paper(conn, "other", "Delta Signals", direction="strategy-signaling", day=4)
    conn.commit()
    listing = client.get("/papers?direction=sea-asean&sort=title").text
    assert 'href="/papers/b-paper?from=direction%3Dsea-asean%26sort%3Dtitle"' in listing             # the row carries its list
    page = client.get("/papers/b-paper", params={"from": "direction=sea-asean&sort=title"}).text
    head = page.split('<header class="read-head">')[1].split("</header>")[0]
    assert '<span class="read-pos" title="在来源列表里的位置">2 / 3</span>' in head
    assert 'href="/papers/a-paper?from=direction%3Dsea-asean%26sort%3Dtitle" rel="prev"' in head
    assert 'href="/papers/c-paper?from=direction%3Dsea-asean%26sort%3Dtitle" rel="next"' in head
    plain = client.get("/papers/b-paper").text                                                   # no from: its 方向, newest first
    assert 'href="/papers/a-paper" rel="prev"' in plain and "3 / 3" in plain and 'rel="next"' not in plain
    every = client.get("/papers/b-paper", params={"from": ""}).text                              # 全部, grouped by 方向
    assert "3 / 4" in every and 'href="/papers/other?from=" rel="next"' in every
    stale = client.get("/papers/b-paper", params={"from": "collection=gone"}).text               # a list that is gone
    assert "3 / 3" in stale


def test_reader_keys_and_find_are_wired(client):
    js = client.get("/static/paper-reader.js").text
    assert "event.key === '['" in js and "'\\\\'" in js and "params.get('find')" in js and "currentScaleValue = 'auto'" in js
