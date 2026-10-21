"""Card 0200: the reader opens on 信息, the DOI and 待补原文 send the paper's page to the default browser, there is no
核验 row, and Copy gives the full reference in the format Leo reads in Chinese IR journals."""
from __future__ import annotations

import json

from research_lab.excerpts import citation
from research_lab.web.routes.papers import source_url

AT = "2026-10-21T08:00:00+08:00"


def row(**kw) -> dict:
    base = {"authors_json": "[]", "title": "", "venue": None, "year": None, "kind": "article", "crossref_json": None,
            "openalex_json": None, "doi": None}
    return {**base, **kw}


def test_leos_example_comes_out_as_he_wrote_it():
    nolte = row(authors_json=json.dumps(["Nolte, Detlef"]), year=2010, venue="Review of International Studies",
                title="How to Compare Regional Powers: Analytical Concepts and Research Topics",
                crossref_json=json.dumps({"volume": "36", "issue": "4", "page": "881–901"}))
    assert citation(nolte) == ('Detlef Nolte, "How to Compare Regional Powers: Analytical Concepts and Research Topics", '
                               "Review of International Studies, Vol. 36, No. 4, 2010, pp. 881-901.")


def test_authors_openalex_pages_and_missing_parts():
    two = row(authors_json=json.dumps(["Coe, Andrew J.", "Vaynman, Jane"]), title="Why Arms Control Is So Rare",
              venue="American Political Science Review", year=2020,
              openalex_json=json.dumps({"biblio": {"volume": "114", "issue": "2", "first_page": "342", "last_page": "355"}}))
    assert citation(two) == ('Andrew J. Coe and Jane Vaynman, "Why Arms Control Is So Rare", American Political Science '
                             "Review, Vol. 114, No. 2, 2020, pp. 342-355.")
    four = row(authors_json=json.dumps(["Jost, Tyler", "Kertzer, Joshua D.", "Min, Eric", "Schub, Robert"]),
               title="Advisers and Aggregation", venue="International Organization", year=2024)
    assert citation(four) == 'Tyler Jost et al., "Advisers and Aggregation", International Organization, 2024.'
    three = row(authors_json=json.dumps([{"given": "A", "family": "One"}, "Two, B", "C Three"]), title="T?")
    assert citation(three) == 'A One, B Two and C Three, "T?".'
    book = row(authors_json=json.dumps(["Acharya, Amitav"]), title="Whose Ideas Matter?", venue="Cornell University Press",
               year=2009, kind="book", crossref_json=json.dumps({"page": "1-20"}))
    assert citation(book) == "Amitav Acharya, Whose Ideas Matter?, Cornell University Press, 2009."
    assert citation(row(title="Untitled", authors_json="not json", crossref_json="{bad")) == '"Untitled".'
    noisy = row(authors_json=json.dumps(["Hamilton-Hart, Natasha", "Professor at the University of Auckland, New Zealand."]),
                title="Unwinding", venue="Contemporary Southeast Asia", year=2026)
    assert citation(noisy) == 'Natasha Hamilton-Hart, "Unwinding", Contemporary Southeast Asia, 2026.'


def test_a_chinese_paper_uses_the_chinese_form():
    qin = row(authors_json=json.dumps(["秦亚青"]), title="关系本位与过程建构", venue="中国社会科学", year=2009,
              crossref_json=json.dumps({"issue": "3", "page": "69-86"}))
    assert citation(qin) == "秦亚青：《关系本位与过程建构》，载《中国社会科学》2009年第3期，第69—86页。"
    book = row(authors_json=json.dumps(["阎学通", "徐进"]), title="国际关系分析", venue="北京大学出版社", year=2008, kind="book")
    assert citation(book) == "阎学通、徐进：《国际关系分析》，北京大学出版社2008年版。"


def test_the_source_is_the_doi_else_a_web_landing_page():
    assert source_url(row(doi="10.1017/S0260210510001324")) == "https://doi.org/10.1017/S0260210510001324"
    assert source_url(row(doi="10.1/a<b>")) == "https://doi.org/10.1/a%3Cb%3E"
    page = {"primary_location": {"landing_page_url": "https://www.jstor.org/stable/1"}}
    assert source_url(row(openalex_json=json.dumps(page))) == "https://www.jstor.org/stable/1"
    for bad in ({"primary_location": {"landing_page_url": "javascript:alert(1)"}}, {"primary_location": None}, []):
        assert source_url(row(openalex_json=json.dumps(bad))) is None
    assert source_url(row()) is None


def _paper(conn, pid, *, doi=None, pdf=True):
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, venue, doi, kind, metadata_status, "
                 "crossref_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (pid, "How to Compare Regional Powers", "h-" + pid, json.dumps(["Nolte, Detlef"]), 2010,
                  "Review of International Studies", doi, "article", "verified",
                  json.dumps({"volume": "36", "issue": "4", "page": "881-901"}), AT, AT))
    if pdf:
        conn.execute("INSERT INTO paper_files(paper_id, sha256, rel_path, original_name, bytes, page_count, has_text_layer, "
                     "extractor, is_primary, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (pid, "sha-" + pid, f"papers/{pid}.pdf", f"{pid}.pdf", 1000, 14, 1, "pymupdf", 1, AT))
    conn.commit()


def test_the_reader_opens_on_info_with_the_doi_outside_and_copy(client, conn):
    _paper(conn, "nolte", doi="10.1017/S0260210510001324")
    page = client.get("/papers/nolte").text
    tabs = page.split('id="side-tabs"')[1].split("</nav>")[0]
    assert tabs.index('data-side-tab="info" class="on"') < tabs.index('data-side-tab="summary"')
    assert 'id="side-summary" class="side-panel" aria-label="中文概要" hidden' in page
    assert 'id="side-info" class="side-panel" aria-label="信息">' in page and 'class="read-page tab-info"' in page
    assert 'data-mtab="info" class="on"' in page
    card = page.split('id="paper-card"')[1].split("</section>")[0]
    assert '<a class="ext" href="https://doi.org/10.1017/S0260210510001324" target="_blank" rel="noopener noreferrer"' in card
    assert "核验" not in card and "Verify" not in card and "编辑引用信息" not in card and 'name="title"' not in card
    assert ('<span class="serif paper-cite" id="paper-cite">Detlef Nolte, &#34;How to Compare Regional Powers&#34;, '
            "Review of International Studies, Vol. 36, No. 4, 2010, pp. 881-901.</span>") in card
    assert 'data-copy-cite="paper-cite">Copy</button>' in card


def test_without_a_pdf_the_reader_offers_the_page_to_get_it(client, conn):
    _paper(conn, "nolte-nopdf", doi="10.1017/S0260210510001324", pdf=False)
    _paper(conn, "bare", pdf=False)
    biblio = client.get("/papers/nolte-nopdf").text.split('class="reading-biblio"')[1].split("</section>")[0]
    assert 'href="https://doi.org/10.1017/S0260210510001324" target="_blank"' in biblio and "Get PDF" in biblio
    bare = client.get("/papers/bare").text
    assert "Get PDF" not in bare and "<dt>DOI</dt><dd>—</dd>" in bare


def test_a_wrong_direction_still_says_so_without_the_edit_form(client, conn):
    _paper(conn, "dir")
    r = client.post("/papers/dir/direction", data={"direction": "made-up"})
    assert r.status_code == 200 and "未知方向" in r.text
