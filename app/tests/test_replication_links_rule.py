"""Card 0182: the replication links come from the data-availability page by rule, without the model. Case: Jost et al.
2024, whose Dataverse DOI is broken over two lines and has the journal's margin stamp («online») dropped into the gap.
Fixtures are short synthetic paragraphs; nothing from the PDF is stored here."""
from __future__ import annotations

import pytest

from research_lab.intake.extract import PageText
from research_lab.intake.sections import Section
from research_lab.replication import plans

STAMPED = ("Data Availability StatementCambridge\nby           Replication files for this article may be found at "
           "<https://doi.org/10.7910/DVN/\nonline        GZW94R>.\nPublished        Supplementary Material\n")
DOI = "https://doi.org/10.7910/DVN/GZW94R"


@pytest.mark.parametrize("text", [
    STAMPED,                                                                   # the Jost page, margin stamp in the gap
    "Replication files: https://doi.org/10.7910/DVN/GZW94R.",                  # printed whole, sentence full stop after
    "Replication files: doi:10.7910/DVN/GZW94R",                               # doi: form
    "Replication files: doi:10.7910/DVN/ GZW94R (Harvard Dataverse)",          # a space after the slash
    "Replication files: 10.7910 / DVN / GZW94R",                               # spaced out
    "Data are on Harvard Dataverse, https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi:10.7910/DVN/\nGZW94R",
])
def test_a_dataverse_doi_is_rebuilt_whatever_breaks_it(text):
    assert plans.find_links(text) == [DOI]


def test_a_doi_with_no_id_gives_no_link_and_a_lowercase_word_is_not_an_id():
    assert plans.find_links("Replication files at https://doi.org/10.7910/DVN/ and nothing after it.") == []
    assert plans.find_links("see doi 10.7910/DVN/ available online later") == []


def test_other_repositories_are_recognised_by_their_address():
    text = ("Code: https://github.com/jost/advisers, data: osf.io/ab12c/ and github.com/jost/other. "
            "Also https://zenodo.org/records/123456 and doi:10.5281/zenodo.123456, https://www.cambridge.org/core/terms")
    assert plans.find_links(text) == [
        "https://github.com/jost/advisers", "https://osf.io/ab12c/", "https://github.com/jost/other",
        "https://zenodo.org/records/123456", "https://doi.org/10.5281/zenodo.123456"]
    assert "https://www.cambridge.org/core/terms" not in plans.find_links(text)                   # the publisher's own


def test_the_same_address_spelled_two_ways_is_one_link():
    text = "Files: https://doi.org/10.7910/DVN/GZW94R and doi:10.7910/DVN/GZW94R and http://dx.doi.org/10.7910/DVN/GZW94R"
    assert plans.find_links(text) == [DOI]


def test_an_address_in_the_statement_counts_even_when_it_is_not_a_known_repository():
    assert plans.find_links("Replication data: https://example.edu/~jost/replication.") == ["https://example.edu/~jost/replication"]


def test_the_publishers_own_address_is_not_a_replication_link():
    statement = ("Supplementary material for this article is available at <https://doi.org/10.1017/S0020818323000280>. "
                 "See https://www.cambridge.org/core/terms.")
    assert plans.find_links(statement) == []


def test_merging_adds_what_is_missing_and_replaces_a_cut_off_address():
    model = [{"url": "https://doi.org/10.7910/DVN", "kind": "dataverse", "access": "public", "note": "cut off"},
             {"url": "https://github.com/jost/advisers/", "kind": "github", "access": "public", "note": "code"}]
    found = [{"url": DOI, "kind": "dataverse", "access": "public", "note": "p. 33"},
             {"url": "https://github.com/jost/advisers", "kind": "github", "access": "public", "note": "p. 33"}]
    merged = plans.merge_links(model, found)
    assert [row["url"] for row in merged] == ["https://github.com/jost/advisers/", DOI]               # the model's note stays
    assert merged[0]["note"] == "code"
    assert plans.merge_links([], found) == found and plans.merge_links(found, []) == found


# ── through the plan ───────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def paper(conn, monkeypatch):
    from research_lab.intake import pipeline
    pages = [PageText(0, "1", "Title page\n" + "filler " * 40),
             PageText(1, "2", "Discussion filler.\n\n" + STAMPED + "\nSupplementary material at <https://doi.org/10.1017/\n"
                      "S0020818323000280.>\n\n\n\nFootnote: http://192.168.0.7/private-share and "
                      "http://localhost:8000/data for the local copy."),
             PageText(2, "3", "References\nSmith, A. 2019. Replication data. Harvard Dataverse. https://doi.org/10.7910/DVN/AAAAAA")]
    monkeypatch.setattr(pipeline, "primary_file", lambda conn, pid: {"id": 77})
    monkeypatch.setattr(pipeline, "load_pages", lambda conn, fid: pages)
    monkeypatch.setattr(pipeline, "load_sections", lambda conn, fid: [Section("references", "References", 2, 2, 0)])
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, created_at, updated_at) "
                 "VALUES ('jost2024-advisers', 'Advisers', 'h', '[]', 2024, '2026-10-17', '2026-10-17')")
    return "jost2024-advisers"


def test_the_page_rule_finds_the_link_and_leaves_the_cited_data_set_alone(conn, paper):
    assert [row["url"] for row in plans.availability_links(conn, paper)] == [DOI]
    assert plans.availability_links(conn, paper)[0]["kind"] == "dataverse"
    assert "第 2 页" in plans.availability_links(conn, paper)[0]["note"]


def test_an_address_that_is_not_public_is_dropped_by_the_link_check(conn, paper):
    page = {"label": "2", "statement": "Data: http://192.168.0.7/private-share and http://localhost:8000/data"}
    assert plans.find_links(page["statement"])                                                       # the rule alone finds them
    assert plans.links_from_pages([page]) == []                                                      # the Link check drops both


def test_apply_puts_the_rule_links_in_the_plan_without_the_model_naming_them(conn, paper):
    result = plans.Result.model_validate({"unit_of_analysis": "meeting"})
    plan = plans.get(conn, plans.apply(result, None, conn, paper))["plan"]
    assert [row["url"] for row in plan["links"]] == [DOI]
    assert plan["links"][0]["access"] == "public"


def test_apply_does_not_repeat_a_link_the_model_also_gave(conn, paper):
    result = plans.Result.model_validate({"links": [
        {"url": "https://doi.org/10.7910/DVN/GZW94R.", "kind": "dataverse", "note": "Dataverse"},
        {"url": "https://doi.org/10.7910/DVN", "note": "cut off"},
        {"url": "http://localhost/secret", "note": "private"}]})
    plan = plans.get(conn, plans.apply(result, None, conn, paper))["plan"]
    assert [row["url"] for row in plan["links"]] == [DOI] and plan["links"][0]["note"] == "Dataverse"
    assert any("链接" in c for c in plan["caveats"])                                                   # the private one was dropped, said so


def test_a_paper_with_no_availability_page_keeps_the_models_links(conn, monkeypatch):
    from research_lab.intake import pipeline
    monkeypatch.setattr(pipeline, "primary_file", lambda conn, pid: {"id": 78})
    monkeypatch.setattr(pipeline, "load_pages", lambda conn, fid: [PageText(0, "1", "Just prose about alliances.")])
    monkeypatch.setattr(pipeline, "load_sections", lambda conn, fid: [])
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, created_at, updated_at) "
                 "VALUES ('plain2024-paper', 'Plain', 'h2', '[]', 2024, '2026-10-17', '2026-10-17')")
    result = plans.Result.model_validate({"links": [{"url": "https://osf.io/abcde/", "note": "model"}]})
    plan = plans.get(conn, plans.apply(result, None, conn, "plain2024-paper"))["plan"]
    assert [row["url"] for row in plan["links"]] == ["https://osf.io/abcde/"]


# ── review fixes: where on the page a link is read from ────────────────────────────────────────────────────

BODY = ("Our measure follows Smith (2019), whose replication data are at https://doi.org/10.7910/DVN/AAAAAA and whose code is "
        "at https://github.com/smith/code. We extend it.\n\n")
STATEMENT = "Data Availability Statement\nReplication files for this article may be found at <https://doi.org/10.7910/DVN/GZW94R>.\n"


@pytest.fixture()
def library(conn, monkeypatch):
    """pages + stored sections → the paper id, as the library would hold them."""
    from research_lab.intake import pipeline

    def put(pages, sections):
        monkeypatch.setattr(pipeline, "primary_file", lambda conn, pid: {"id": 79})
        monkeypatch.setattr(pipeline, "load_pages", lambda conn, fid: pages)
        monkeypatch.setattr(pipeline, "load_sections", lambda conn, fid: sections)
        return "any2024-paper"
    return put


def test_third_party_data_cited_in_the_body_of_the_page_is_not_this_papers_link(conn, library):
    below = "\nAcknowledgments\nWe thank the authors of https://github.com/thirdparty/tool for help.\n"
    pid = library([PageText(0, "1", BODY + STATEMENT + below)], [])
    [found] = plans.availability_pages(conn, pid)
    assert found["statement"].startswith("Data Availability Statement") and "GZW94R" in found["statement"]
    assert "AAAAAA" not in found["statement"] and "thirdparty" not in found["statement"]
    assert [row["url"] for row in plans.availability_links(conn, pid)] == [DOI]


def test_the_statement_ends_at_the_next_heading_or_a_blank_gap():
    lines = (STATEMENT + "Supplementary Material\nSee https://example.org/other.\n").splitlines()
    assert "example.org" not in plans._statement(lines) and "GZW94R" in plans._statement(lines)
    gap = (STATEMENT + "\n\nFootnote 3. See https://example.org/other.\n").splitlines()
    assert "example.org" not in plans._statement(gap)
    stamped = STAMPED.splitlines()                                                       # «Published   Supplementary Material»
    assert plans.find_links(plans._statement(stamped)) == [DOI]


@pytest.mark.parametrize("text", [
    "Files: 10.7910/DVN/\n\nFIGURE 3. Results by administration",                     # blank line, then a capital word
    "Files: 10.7910/DVN/\n   \nGZW94R",                                               # blank line of spaces, then a real-looking id
    "Files: 10.7910/DVN/\nFIGURE 3 shows the results",                                 # one break, but no digit in the word
    "Files: 10.7910/DVN/ FIGURE 3 shows the results",
    "Files: 10.7910/\n\nDVN/GZW94R",
])
def test_a_doi_is_not_stitched_across_a_blank_line_or_to_a_capital_word(text):
    assert plans.find_links(text) == []


@pytest.mark.parametrize("text", [
    "Files: 10.7910/DVN/\n   GZW94R",
    "Files: 10.7910/DVN/GZW9-\n4R",
    "Files: 10.7910/\nDVN/GZW94R",
    "Files: 10.7910/DVN/ABCDEF",
])
def test_one_line_break_or_hyphenation_still_joins_a_doi(text):
    assert len(plans.find_links(text)) == 1 and plans.find_links(text)[0].startswith("https://doi.org/10.7910/DVN/")


@pytest.mark.parametrize("heading,section", [
    ("References", Section("references", "References", 0, 0, 5)),
    ("References", Section("references", "References", 0, 0, -1)),                       # line unknown: found by its text
    ("Referencesavailable", Section("references", "Referencesavailable", 0, 0, -1)),      # the margin stamp glued on
])
def test_a_statement_on_the_page_where_the_references_begin_is_kept_and_the_references_are_not(conn, library, heading, section):
    page = (BODY + STATEMENT + "\n\n" + heading + "\nSmith, A. 2019. Replication data. Harvard Dataverse. "
            "https://doi.org/10.7910/DVN/BBBBBB\n")
    pid = library([PageText(0, "1", page), PageText(1, "2", "Jones 2020. https://doi.org/10.7910/DVN/CCCCCC")], [section])
    found = plans.availability_pages(conn, pid)
    assert [d["label"] for d in found] == ["1"]                                           # the page after is left out
    assert "BBBBBB" not in found[0]["window"] and "Smith, A. 2019" not in found[0]["text"]
    assert [row["url"] for row in plans.availability_links(conn, pid)] == [DOI]
