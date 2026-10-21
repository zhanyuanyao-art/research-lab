"""Card 0170: which pages a replication plan request sends. Case: Malesky, Nguyen and Tran 2014 (APSR 108(1),
pp. 144–168). Its headings are ALL-CAPS and share their line with the other column; its main regression
tables sit on pp. 157–161. Before this card the methods fell back to pp. 145–147, and the 12k results budget
held pp. 149 and 151, so Table 3 never reached the model.

The fixtures keep only what the page picking reads from that paper: the headings, a few table rows and the
captions, at their layout positions. Every prose line is filler written for this test, not the paper's text."""
from __future__ import annotations

import re

import pytest

from research_lab.intake.extract import PageText
from research_lab.intake.sections import Section
from research_lab.replication import plans

HEADER = "        American Political Science Review                                                   Vol. 108, No. 1"


def prose(n: int, tag: str = "") -> list[str]:
    """Two-column filler lines, about as long as the paper's."""
    left = f"this filler sentence stands in for running text {tag} in the left"
    right = "and this one stands in for the column on the right side here"
    return [f"        {left}    {right}" for _ in range(n)]


def page(*parts: list[str] | str) -> str:
    lines = [HEADER, "", ""]
    for p in parts:
        lines += [p] if isinstance(p, str) else p
    return "\n".join(lines)


TABLE_2 = [
    "        TABLE 2.  Difference-in-Difference Estimation Between 2008 and 2010 using VHLSS Commune",
    "         Data",
    "",
    "          Model                                 (1)          (2)           (3)           (4)",
    "          Dependent Variable        Commune  Transport    Project     Source",
    "",
    "           Time∗Treatment                  0.086∗∗    0.102∗      0.087    −0.080",
    "                                             (0.037)     (0.053)     (0.059)     (0.077)",
    "           Regional FE               YES     YES     YES     YES",
    "           Observations                 4126     4126      4126      4126",
    "           R-squared                     0.140      0.146      0.054      0.183",
    "",
    "            Regressions employ OLS with standard errors clustered at district level and provincial level.",
    "", "", "",
]
TABLE_3 = [
    "        TABLE 3.  Difference-in-Difference Estimation using VHLSS Commune Data on Aggregate",
    "           Indices",
    "",
    "                                                Panel 1: Between 2008 and 2010",
    "          Model                              (1)              (2)           (3)",
    "           Dependent Variable            index         index       index",
    "",
    "            Time∗Treatment                0.225∗∗     −0.003        0.123∗∗∗",
    "                                            (0.109)        (0.106)      (0.030)",
    "            Regional FE              YES       YES      YES",
    "            Observations                4126        4126       4126",
    "           R-squared                   0.099         0.124       0.139",
    "",
    "            Continuous variables are re-scaled between 0 and 1 before they are added together.",
    "", "", "",
]
FIGURE_3 = [
    "         FIGURE 3.  Difference-in-Difference Analysis of Key Outcome Variables",
    "", "",
    "                                  INFRASTRUCTURE INDEX",
    "                                                      Public Transport (Prob)",
    "                                  HEALTH SERVICES INDEX",
    "                                       EDUCATION INDEX",
    "                                  COMMUNICATIONS INDEX",
    "", "",
    "                                                -.2         -.15          -.1         -.05       0",
    "", "", "",
]


def malesky_pages() -> list[PageText]:
    texts = {
        "144": page(prose(60, "on the title page")),
        "145": page(prose(14), "        filler words on the left side of this page    THE LITERATURE ON DECENTRALIZATION",
                    prose(40)),
        "149": page(["        FIGURE 1.  Organization of the Vietnamese Political System Based on the",
                     "         Revised 1992 Constitution", "", "", ""], prose(30)),
        "150": page(prose(40), "     EXPERIMENTATION WITH DPC REMOVAL                     filler on the right column",
                    prose(20)),
        "155": page(prose(30, "before the design"),
                    "     OUR EMPIRICAL DESIGN                                    filler words on the right column",
                    prose(28, "about the commune panel")),
        "156": page(prose(60, "describing the survey waves")),
        "157": page(TABLE_2, prose(8, "after table two"),
                    "      ROBUSTNESS TESTS                             filler words on the right column", prose(12)),
        "158": page(FIGURE_3, prose(20)),
        "159": page(TABLE_3, prose(20)),
        "160": page(["         FIGURE 4.  Randomization Inference Test on Total Number of Significant Outcomes", "", "",
                     "                    0   1   2   3   4   5   6   7   8   9  10  11  12", "", "", ""], prose(40)),
        "161": page(["        FIGURE 5.  Test of Motivated Interference in Experimental Performance", "", ""], prose(45)),
        "163": page(prose(40), "          TESTING THE CAUSAL                 filler words on the right column", prose(20)),
        "166": page(prose(30), "       REFERENCES                                                ments: filler title",
                    ["        Author, A. 2001. Filler Title. Filler Press."] * 20),
        "167": page(["        TABLE A1.  A table printed after the references", "",
                     "          Observations         4126      4126", "", "", ""],
                    ["        Author, B. 2002. Filler Title. Filler Press."] * 30),
    }
    return [PageText(i, label, text) for i, (label, text) in enumerate(texts.items())]


@pytest.fixture()
def malesky(conn, monkeypatch):
    """The paper as Leo's library has it: pages stored, no sections stored (imported before this card)."""
    from research_lab.intake import pipeline
    pages = malesky_pages()
    monkeypatch.setattr(pipeline, "primary_file", lambda conn, pid: {"id": 97})
    monkeypatch.setattr(pipeline, "load_pages", lambda conn, fid: pages)
    monkeypatch.setattr(pipeline, "load_sections", lambda conn, fid: [])
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, created_at, updated_at) "
                 "VALUES ('malesky2014-impact', 'The Impact of Recentralization on Public Services', 'h', "
                 "'[\"Edmund J. Malesky\"]', 2014, '2026-10-13', '2026-10-13')")
    return "malesky2014-impact"


def _labels(text: str) -> list[str]:
    return re.findall(r"\[p\. (\w+)\]", text)


def test_methods_come_from_the_all_caps_design_section(conn, malesky):
    mat = plans.methods_material(conn, malesky)
    assert mat["source"] == "sections" and mat["headings"] == ["OUR EMPIRICAL DESIGN"]
    assert [label for label, _ in mat["pieces"]] == ["155", "156", "157"]
    joined = "\n".join(t for _, t in mat["pieces"])
    # the heading shares its line with the right column, which carries the section above it too: whole first page
    assert "before the design" in joined and "about the commune panel" in joined
    assert "ROBUSTNESS TESTS" not in joined and "after table two" in joined


def test_the_preview_carries_the_main_tables_and_leaves_out_the_prose(conn, malesky):
    req, _ = plans.build(conn, malesky)
    text = req.messages[-1]["content"]
    paper = text.split("<paper_text>")[1].split("</paper_text>")[0]
    results = text.split("<results_pages>")[1].split("</results_pages>")[0]
    assert "OUR EMPIRICAL DESIGN" in text and _labels(paper) == ["155", "156", "157"]
    assert "TABLE 2." in paper and "TABLE 3." in results and "0.225∗∗" in results and "(0.109)" in results
    assert "FIGURE 3." in results and "-.15" in results
    assert set(_labels(results)) <= {"149", "158", "159", "160", "161"} and "159" in _labels(results)
    assert _labels(results) == sorted(_labels(results), key=int)                 # page order kept
    assert "157" not in _labels(results)                                         # Table 2 already went with the methods
    assert "TABLE A1." not in text                                               # nothing after the references
    assert "filler sentence stands in" not in results                            # table blocks, not whole pages
    assert len(text) < 60_000                                                    # about 1–2万 token in all


def test_a_tight_budget_goes_to_the_regression_tables_first(conn, malesky, monkeypatch):
    # the fixture's table blocks are about 780 characters each, the figure captions 70–470
    monkeypatch.setattr(plans, "RESULTS_CHARS", 800)
    assert [label for label, _ in plans.results_material(conn, malesky, {})] == ["157"]        # Table 2 scores highest
    monkeypatch.setattr(plans, "RESULTS_CHARS", 1600)
    assert [label for label, _ in plans.results_material(conn, malesky, {})] == ["157", "159"]  # not p. 149's figure
    monkeypatch.setattr(plans, "RESULTS_CHARS", 300)
    [(label, body)] = plans.results_material(conn, malesky, {})
    assert label == "157" and body.endswith("[…]")                              # the best page alone, cut to fit


def test_pages_sent_with_the_methods_stay_out_only_when_their_tables_went_too(conn, malesky):
    pages = {p.label: p.text for p in malesky_pages()}
    assert "157" not in [l for l, _ in plans.results_material(conn, malesky, {"157": pages["157"]})]
    cut = "\n".join(pages["157"].splitlines()[:4])                               # the methods stopped above the table
    assert "157" in [l for l, _ in plans.results_material(conn, malesky, {"157": cut})]
    assert "157" not in [l for l, _ in plans.results_material(conn, malesky, {"157"})]   # a bare label set: skipped


# ── the pieces ───────────────────────────────────────────────────────────────────────────────────────────────

def test_a_table_block_runs_from_the_caption_through_the_rows_and_notes():
    text = page(prose(5, "above"), TABLE_2, prose(10, "below"))
    [block] = plans.table_blocks(text)
    assert block.startswith("TABLE 2.") and "R-squared" in block and "Regressions employ OLS" in block
    assert "above" not in block and "below" not in block


@pytest.mark.parametrize("line,caption", [
    ("        TABLE 2.  Difference-in-Difference Estimation", True),
    ("         FIGURE 3.  Difference-in-Difference Analysis", True),
    ("Table 4: Robustness", True),
    ("Table A1 Balance on covariates", True),
    ("表 2 回归结果", True),
    ("             Table 2 presents the regressions of commune outcomes    on the right", False),
    ("Table reports only the first seven outcome variables", False),
])
def test_caption_lines_are_not_sentences_about_tables(line, caption):
    assert bool(plans.CAPTION_LINE_RE.match(line)) is caption


def test_regression_tables_outscore_figures():
    assert plans.table_score("\n".join(TABLE_3)) > plans.table_score("\n".join(FIGURE_3)) > 0
    assert plans.table_score("\n".join(prose(20))) == 0


# ── card 0182: a landscape table with no caption, pages by keywords, the availability page ────────────────────
# Case: Jost et al. 2024 (International Organization 78(2)). Its main table is on a landscape page whose stored text
# came out in the wrong order with no «TABLE 3», so the captions-only rule missed it; its methods section was not
# detected, so the request began with the introduction; the Dataverse DOI on its Data Availability page was broken
# over two lines by the journal's margin stamp and that page never went. Fixtures are filler, not the paper's text.

LANDSCAPE_ROWS = [
    "                              (1)           (2)           (3)",
    "          Mean hawkishness     5.101***     0.881**       2.983***",
    "                              (1.450)       (0.443)       (0.951)",
    "          Defense              0.195***     0.066***      0.134***",
    "                              (0.048)       (0.022)       (0.044)",
    "          Intelligence         0.315***     0.135***      0.321***",
    "                              (0.100)       (0.041)       (0.097)",
    "          Observations         2,685         2,685         2,650",
    "",
    "          Notes: Estimates for past experience omitted. * p < .10; ** p < .05; *** p < .01.",
    "", "", "",
]
DESIGN_LINES = [
    "        Research design. We estimate the effect of adviser hawkishness on the decision. The data set covers",
    "        950 decisions. The dependent variable is a conflictual decision; we measure hawkishness with a survey",
    "        and we code every speech act. The regression sample is the meetings with at least one decision.",
]
MEASURE_LINES = [
    "        Measurement. We operationalize hawkishness as the average of the survey items. The independent",
    "        variable is the advisers' score; control variables are the number of attendees and the standard",
    "        errors are clustered by meeting. We estimate two specifications of the model on this sample.",
]
THEORY_LINES = [
    "        The theory says that groups aggregate preferences and that leaders weigh advice. The argument is",
    "        about how institutions shape which voices reach the president, and why a model of the leader alone",
    "        misses the room. This is the logic we develop before turning to evidence in the next section.",
]
DA_LINES = [
    "          Data Availability StatementCambridge",
    "by           Replication files for this article may be found at <https://doi.org/10.7910/DVN/",
    "online        GZW94R>.",
    "Published        Supplementary Material",
    "",
    "            Supplementary material for this article is available at <https://doi.org/10.1017/",
    "           S0020818323000280.>",
    "", "", "",
    "                 106. Preston 2001; Reiter and Stam 2002.",
]


def jost_like_pages() -> list[PageText]:
    texts = {
        "1": page(prose(30, "on the title page")),
        "2": page(prose(40, "in the introduction")),
        "3": page(THEORY_LINES * 4, prose(10)),
        "4": page(DESIGN_LINES * 5, prose(6, "after the design")),
        "5": page(MEASURE_LINES * 5, prose(6)),
        "6": page(prose(10, "above the table"), LANDSCAPE_ROWS, prose(8, "below the table")),
        "7": page(["        FIGURE 2.  Data set construction", "", ""], prose(30)),
        "8": page(prose(14, "the discussion"), DA_LINES, prose(4)),
        "9": page(["        References", ""], ["        Smith, A. 2019. Replication data for a study. Harvard Dataverse. "
                                              "https://doi.org/10.7910/DVN/AAAAAA"] * 12),
    }
    return [PageText(i, label, text) for i, (label, text) in enumerate(texts.items())]


@pytest.fixture()
def jost_like(conn, monkeypatch):
    from research_lab.intake import pipeline
    pages = jost_like_pages()
    monkeypatch.setattr(pipeline, "primary_file", lambda conn, pid: {"id": 98})
    monkeypatch.setattr(pipeline, "load_pages", lambda conn, fid: pages)
    # as the library has it: no methods heading, the references stored
    monkeypatch.setattr(pipeline, "load_sections", lambda conn, fid: [Section("references", "References", 8, 8, 3)])
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, created_at, updated_at) "
                 "VALUES ('jost2024-advisers', 'Advisers and Aggregation', 'h', '[\"Tyler Jost\"]', 2024, '2026-10-17', '2026-10-17')")
    return "jost2024-advisers"


def test_a_regression_table_with_no_caption_is_found_by_its_cells_and_cut_to_its_block(conn, jost_like):
    rows = dict(plans.results_material(conn, jost_like, {}))
    assert "6" in rows and "5.101***" in rows["6"] and "(0.443)" in rows["6"] and "Notes: Estimates" in rows["6"]
    assert "above the table" not in rows["6"] and "below the table" not in rows["6"]       # the block, not the page
    assert "7" in rows and rows["7"].startswith("FIGURE 2.")                               # captioned pages as before
    assert not {"1", "2", "3", "4", "5", "8", "9"} & set(rows)                             # prose pages, nothing from the references


def test_a_few_p_values_in_prose_do_not_make_a_table(conn):
    text = page(prose(6), "        The effect is small (0.05) and the second one is smaller (0.02) as well.", prose(6))
    assert not plans.looks_like_regression(text)
    assert plans.page_blocks(text) == ([], False)
    assert plans.looks_like_regression("\n".join(LANDSCAPE_ROWS))
    assert not plans.looks_like_regression("\n".join(prose(10) + ["        the formula m1* and m2* with 5 cases*"]))


def test_an_uncaptioned_block_stays_off_the_captioned_block_beside_it():
    text = page(LANDSCAPE_ROWS, prose(4, "between"), TABLE_2, prose(6))
    blocks, captioned = plans.page_blocks(text)
    assert captioned and len(blocks) == 2
    assert "5.101***" in blocks[0] and "TABLE 2." not in blocks[0] and blocks[1].startswith("TABLE 2.")
    assert "between" not in "".join(blocks)


@pytest.mark.parametrize("line,caption", [
    ("Table III.   Civilizations Involved in Conflict-Years, 1946–2001", True),
    ("TABLE IV", True),
    ("             Table V shows the estimates    on the right", False),
    ("Table In the second wave the commune", False),
])
def test_roman_numeral_captions(line, caption):
    assert bool(plans.CAPTION_LINE_RE.match(line)) is caption


def test_with_no_methods_heading_the_methods_are_the_pages_that_talk_about_design(conn, jost_like):
    mat = plans.methods_material(conn, jost_like)
    assert mat["source"] == "pages" and mat["picked"] == "keywords"
    assert [label for label, _ in mat["pieces"]] == ["4", "5"]                             # page order
    texts = {p.label: p.text for p in jost_like_pages()}
    assert plans.method_density(texts["4"]) > plans.METHOD_PAGE_MIN > plans.method_density(texts["3"])
    joined = "\n".join(t for _, t in mat["pieces"])
    assert "in the introduction" not in joined and "5.101***" not in joined               # not the first pages, not the table


def test_when_no_page_talks_about_design_the_methods_stay_pages_two_on(conn, jost_like, monkeypatch):
    pages = [PageText(i, str(i + 1), page(prose(30, f"filler {i}"))) for i in range(4)]
    from research_lab.intake import pipeline
    monkeypatch.setattr(pipeline, "load_pages", lambda conn, fid: pages)
    mat = plans.methods_material(conn, jost_like)
    assert mat["source"] == "pages" and mat["picked"] == "order" and mat["pieces"][0][0] == "2"


def test_the_request_carries_table_3_the_availability_page_and_the_link(conn, jost_like):
    req, _ = plans.build(conn, jost_like)
    text = req.messages[-1]["content"]
    paper = text.split("<paper_text>")[1].split("</paper_text>")[0]
    results = text.split("<results_pages>")[1].split("</results_pages>")[0]
    avail = text.split("<availability>")[1].split("</availability>")[0]
    assert "No methods section was detected" in text and "picked by keywords" in text
    assert _labels(paper) == ["4", "5"] and "5.101***" in results and _labels(avail) == ["8"]
    assert "Replication files for this article" in avail and "Smith, A. 2019" not in text          # not a cited data set
    assert "https://doi.org/10.7910/DVN/GZW94R" in text.split("</availability>")[1].split("Task:")[0]
    assert "106. Preston" in avail                                                                 # the window runs to the page end
    assert len(text) < 60_000


def test_an_availability_page_already_sent_with_the_methods_is_not_sent_twice(conn, jost_like, monkeypatch):
    monkeypatch.setattr(plans, "METHOD_PAGE_MIN", 0.0)                                             # every body page is methods
    monkeypatch.setattr(plans, "FALLBACK_CHARS", 100_000)
    req, _ = plans.build(conn, jost_like)
    text = req.messages[-1]["content"]
    assert "8" in _labels(text.split("<paper_text>")[1].split("</paper_text>")[0]) and "<availability>" not in text


def test_a_cited_data_set_in_the_references_is_not_an_availability_page(conn, jost_like):
    assert [d["label"] for d in plans.availability_pages(conn, jost_like)] == ["8"]


# ── sideways pages ─────────────────────────────────────────────────────────────────────────────────────────

TABLE_3_ROWS = ["TABLE 3. Effect of hawkishness on decisions",
                "Variable        (1)        (2)",
                "Hawkishness   0.120**   0.881***",
                "              (0.052)   (0.443)",
                "Defense       0.195***  0.066",
                "              (0.048)   (0.022)",
                "Observations   2,685     2,650"]


def sideways_pdf(path, *, rotate_sheet: bool):
    """A landscape table set sideways, one text line per row running up the sheet (what a rotated table is in a
    journal PDF), then an ordinary page. With `rotate_sheet` the sheet also carries /Rotate 90."""
    import pymupdf
    doc = pymupdf.open()
    sheet = doc.new_page(width=432, height=648)
    for k, row in enumerate(TABLE_3_ROWS):
        sheet.insert_text((100 + 16 * k, 600), row, fontsize=9, rotate=90, fontname="cour")
    if rotate_sheet:
        sheet.set_rotation(90)
    doc.new_page(width=432, height=648).insert_text((72, 72), "Ordinary page with prose and (0.05) once.", fontsize=11)
    doc.save(path)
    doc.close()


@pytest.mark.parametrize("rotate_sheet", [False, True])
def test_a_sideways_table_page_is_read_upright_and_an_ordinary_page_is_not_touched(tmp_path, rotate_sheet):
    import pymupdf
    from research_lab.intake import extract
    pdf = tmp_path / "landscape.pdf"
    sideways_pdf(pdf, rotate_sheet=rotate_sheet)
    got = extract.extract(pdf)
    assert [line.strip() for line in got.pages[0].text.splitlines() if line.strip()][:4] == [
        "TABLE 3. Effect of hawkishness on decisions", "Variable        (1)        (2)",
        "Hawkishness   0.120**   0.881***", "(0.052)   (0.443)"]
    assert "Observations   2,685     2,650" in got.pages[0].text
    doc = pymupdf.open(pdf)
    assert extract.sideways_turn(doc[0]) == 270 and extract.sideways_turn(doc[1]) == 0
    assert got.pages[1].text == doc[1].get_text("text", sort=True, flags=extract.TEXT_FLAGS)     # same call as before
    doc.close()


def test_a_stored_scrambled_page_is_read_again_from_the_pdf_when_a_request_is_built(conn, repo, monkeypatch):
    from research_lab.config import get_paths
    from research_lab.intake import pipeline
    rel = "papers/old/landscape.pdf"
    (get_paths().data / "papers/old").mkdir(parents=True)
    sideways_pdf(get_paths().data / rel, rotate_sheet=True)
    scrambled = "   ".join(["0.120** (0.052) 0.881*** (0.443) 2,685"] * 12)          # one line, 400+ characters wide
    stored = [PageText(0, "1", "\n".join(["decisions", scrambled, "of (1)"])), PageText(1, "2", "Ordinary page.")]
    monkeypatch.setattr(pipeline, "primary_file", lambda conn, pid: {"id": 5, "rel_path": rel})
    monkeypatch.setattr(pipeline, "load_pages", lambda conn, fid: stored)
    monkeypatch.setattr(pipeline, "load_sections", lambda conn, fid: [])
    assert plans._scrambled(stored[0].text) and not plans._scrambled(stored[1].text)
    [(label, body)] = plans.results_material(conn, "old", {})
    assert label == "1" and body.startswith("TABLE 3. Effect of hawkishness") and "Observations   2,685     2,650" in body
    assert "TABLE 3." in plans.page_texts(conn, "old")["1"]                           # the check in apply() reads it too


def test_a_missing_pdf_leaves_the_stored_text_in_charge(conn, monkeypatch):
    from research_lab.intake import pipeline
    stored = [PageText(0, "1", "   ".join(["0.120** (0.052)"] * 40))]
    monkeypatch.setattr(pipeline, "primary_file", lambda conn, pid: {"id": 5, "rel_path": "papers/gone.pdf"})
    monkeypatch.setattr(pipeline, "load_pages", lambda conn, fid: stored)
    assert plans.page_texts(conn, "gone") == {"1": stored[0].text}


def test_one_mention_of_replication_materials_is_enough_but_plain_talk_of_data_availability_is_not(conn, jost_like, monkeypatch):
    from research_lab.intake import pipeline
    pages = jost_like_pages()
    pages[1] = PageText(1, "2", page(prose(4), "        Replication materials are available from the authors on request.", prose(4)))
    pages[2] = PageText(2, "3", page(prose(4), "        Limits on data availability in the region are severe in the early years.", prose(4)))
    monkeypatch.setattr(pipeline, "load_pages", lambda conn, fid: pages)
    found = plans.availability_pages(conn, jost_like)
    assert [d["label"] for d in found] == ["2", "8"]
    assert "Replication materials are available" in found[0]["window"]
    assert [row["url"] for row in plans.links_from_pages(found)] == ["https://doi.org/10.7910/DVN/GZW94R"]
