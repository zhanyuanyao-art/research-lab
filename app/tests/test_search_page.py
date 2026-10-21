"""/search: Chinese words find outputs mid-sentence, 课题 are searchable, and no ids, slugs or raw Markdown show.
Card 0133: tabs per kind with counts, paper rows with a snippet, page hits grouped per paper, issues with their number."""
from __future__ import annotations

import re

import httpx
import pytest

from research_lab.files import artifacts as A
from research_lab.web.routes.search import plain_text


def _visible(html: str) -> str:
    body = html[html.index('<main'):] if '<main' in html else html
    body = re.sub(r"<script.*?</script>|<[^>]+>", " ", body, flags=re.S)
    return re.sub(r"\s+", " ", body)


def test_chinese_word_mid_sentence_finds_output_without_ids(client, repo):
    path = A.create("memo", "signal-memo", "asean-centrality", title="东盟峰会的表态")
    path.write_text(path.read_text(encoding="utf-8") + "\n## 证据\n\n* 保留的**信号**参照，见 [Loevy](/papers/loevy2015-legal)\n",
                    encoding="utf-8")
    html = client.get("/search?q=信号").text
    hits = html[html.index("<h2>产出</h2>"):]
    assert "东盟峰会的表态" in hits and "<mark>信号</mark>" in hits
    text = _visible(hits)
    assert "ART-" not in text and "memo" not in text and "##" not in text and "](/" not in text and "**" not in text
    assert "结论" in text                                   # one of the four kinds, not the stored type
    assert "Artifacts" not in html


def test_projects_are_searchable(client, repo):
    html = client.get("/search?q=ASEAN").text
    issues = html[html.index("<h2>议题</h2>"):]
    issues = issues[:issues.index("</section>")]
    assert "asean-centrality" in issues and "课题" in issues           # a 课题 is a parent issue, linked to its page


def test_plain_text_drops_markup_and_keeps_hits():
    assert plain_text("## Evidence I need * ASEAN summit") == "Evidence I need ASEAN summit"
    assert plain_text("](/papers/loevy2015-legal)** argues \x02x\x03") == "argues \x02x\x03"
    assert plain_text("Loevy (2015) said") == "Loevy (2015) said"


def test_excerpts_filters_use_titles_and_product_words(client, repo, conn):
    ts = "2026-10-01T00:00:00+00:00"
    for pid, title, year in (("tan2020-asean", "ASEAN and the Long Peace", 2020), ("nobody2019-x", "Never Highlighted", 2019)):
        conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, created_at, updated_at) VALUES (?, ?, ?, '[]', ?, ?, ?)",
                     (pid, title, "h-" + pid, year, ts, ts))
    conn.execute("INSERT INTO excerpts(id, paper_id, locator, quote, my_note, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                 ("EXC-2026-9001", "tan2020-asean", "3", "a quote", "", ts, ts))
    conn.commit()
    html = client.get("/excerpts?filter=all").text
    select = html[html.index('name="paper"'):]
    select = select[:select.index("</select>")]
    assert "全部文献" in select and "ASEAN and the Long Peace（2020）" in select
    assert ">tan2020-asean<" not in select and "Never Highlighted" not in select
    assert "全部课题" in html and "全部论文" not in html and "全部项目" not in html


def test_like_wildcards_match_literally(client, repo):
    from research_lab.web.routes.search import like_pattern
    assert like_pattern("5%_x") == "%5\\%\\_x%"
    a = A.create("memo", "wild-memo", "asean-centrality", title="通配之外")
    a.write_text(a.read_text(encoding="utf-8") + "\n这里写着支持某率。\n", encoding="utf-8")
    assert "通配之外" not in client.get("/search?q=支持_率").text     # _ is not "any one character"
    a.write_text(a.read_text(encoding="utf-8") + "\n字面的支持_率。\n", encoding="utf-8")
    assert "通配之外" in client.get("/search?q=支持_率").text



def _section(html: str, name: str) -> str:
    part = html[html.index(f"<h2>{name}</h2>"):]
    return part[:part.index("</section>")]


def test_tabs_count_each_kind_and_a_tab_shows_only_its_kind(client, repo):
    a = A.create("memo", "tab-memo", "asean-centrality", title="召集权的备忘")
    a.write_text(a.read_text(encoding="utf-8") + "\n召集权从相互否决里来。\n", encoding="utf-8")
    from research_lab.files import tasks as T
    T.add({"title": "核对召集权的说法", "project": "asean-centrality"})
    html = client.get("/search?q=召集权").text
    start = html.index('class="tabs srch-tabs"')
    tabs = html[start:html.index("</nav>", start)]
    for name in ("全部", "文献", "正文", "议题", "产出", "划线"):
        assert f">{name} <span class=\"count\">" in tabs
    assert '议题 <span class="count">1</span>' in tabs and '产出 <span class="count">1</span>' in tabs
    assert "核对召集权的说法" in _section(html, "议题") and "召集权的备忘" in _section(html, "产出")
    only = client.get("/search?q=召集权&tab=issues").text
    assert "<h2>议题</h2>" in only and "<h2>产出</h2>" not in only
    assert 'aria-current="page">议题' in only and 'name="tab" value="issues"' in only


def test_issues_carry_their_number_and_link_by_it(client, repo):
    from research_lab.files import issues as I, tasks as T
    from research_lab.files.yamlio import load_yaml, save_yaml
    keys = {"alliance-us": "APUS", "sea-asean": "SEAS", "strategy-signaling": "SSIG", "periphery-nts": "NTSP"}
    path = repo / "_data/directions.yml"
    data = load_yaml(path)
    for d in data:
        if not d.get("key"):
            d.insert(1, "key", keys[str(d["slug"])])
    save_yaml(path, data)
    ref = T.add({"title": "编号检索的待办", "project": "asean-centrality"})[0]
    I.sync_keys(create=True)
    key = I.key_of(ref)
    assert key.startswith("SEAS-")
    issues = _section(client.get("/search?q=编号检索").text, "议题")
    assert f'<span class="key">{key}</span>编号检索的待办' in issues and f'href="/issues/{key}"' in issues
    found = client.get(f"/search?q={key.lower()}", follow_redirects=False)        # typing the number opens it
    assert found.status_code == 303 and found.headers["location"] == f"/issues/{key}"


@pytest.fixture()
def alliance_paper(conn, tmp_path):
    import pymupdf
    from research_lab.intake import pipeline
    doc = pymupdf.open()
    doc.insert_page(0, text="Patrons and Clients\nArms transfers in an alliance\nB. Writer\n")
    for i in range(5):
        doc.new_page().insert_text((72, 72), f"Page {i + 2}: the alliance holds while the patron pays, case {i}.", fontsize=11)
    pdf = tmp_path / "patrons.pdf"
    doc.save(pdf)
    doc.close()
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500, text="down"))) as http:
        return pipeline.ingest(conn, pdf, http=http, ai=False)


def test_page_hits_group_under_their_paper_with_the_rest_counted(client, repo, alliance_paper):
    html = client.get("/search?q=alliance").text
    pages = _section(html, "正文")
    group = pages[pages.index('class="page-group"'):]
    assert group.count('class="page-hit"') == 3 and "还有 " in group and " 处</a>" in group
    pid = alliance_paper.paper_id
    assert f'href="/papers/{pid}/read?page=' in group and "&amp;find=alliance" in group
    assert 'class="paper-line"' in group and 'data-state="unread"' in group and f'href="/papers/{pid}"' in group
    assert "<mark>alliance</mark>" in group
    head = pages[:pages.index("</header>")]
    assert "处 · 1 篇" in head


def test_empty_and_no_hit_pages(client, repo):
    blank = client.get("/search").text
    assert "输入议题编号" in blank and "srch-tabs" not in blank
    none = client.get("/search?q=zzqqxxnothing").text
    assert "没有找到「zzqqxxnothing」" in none and '全部 <span class="count">0</span>' in none



def test_paper_results_are_the_library_rows_with_the_snippet_kept(client, repo, alliance_paper):
    """Card 0137: card 0128's partials/_paper_line.html, the same row as the 文献 list, and the snippet line under it."""
    html = client.get("/search?q=Patrons").text
    papers = _section(html, "文献")
    hit = papers[papers.index('<li class="srch-hit has-snip">'):]
    hit = hit[:hit.index('</p>') + 4]
    assert '<ul class="paper-lines"><li class="paper-line"' in hit and 'class="pl-title"' in hit and 'class="pl-by"' in hit
    assert 'class="pl-marks"' in hit and 'class="pl-date' in hit and 'class="hover-acts"' in hit     # every track of the list
    assert '<span class="dir-dot tip"' in hit            # results mix directions, so the 方向 mark shows (show_dir)
    assert hit.index("</ul>") < hit.index('<p class="srch-snip">') and "<mark>Patrons</mark>" in hit
    assert "papers.css" in html and "search.css" in html
