"""Card 0187 (docs/design/REPLICATION-v4-DCL.md §3B): the 全书复现清单. Every table and figure of a paper and its appendix is
listed by rules (captions in many spellings, no AI), tied to the author's scripts (output calls, comments, README and
codebook), grouped 正文结果 / 附录稳健性 / 附录其他, ticked by default for the main text, and given a 汇报人 by Leo."""
from __future__ import annotations

from pathlib import Path

import pytest

from research_lab.replication import coverage as cov
from research_lab.replication import plans

from .test_data import data_client, ingest_quiet  # noqa: F401 - data_client is a fixture


def items_of(text: str, source: str = "main", page: str = "7") -> dict[str, cov.Item]:
    return {i.label: i for i in cov.extract_items([cov.SourceText(source, "x.pdf", [(page, text)])])}


# ---------------------------------------------------------------- captions

def test_captions_in_the_spellings_papers_use():
    got = items_of("""
   TABLE 3. Effect of participant hawkishness on foreign policy decisions
   Table A41: Speaker Hawkishness and Speech Act Content
   Figure 5
   Leader search for counsel during meetings
   Fig. 2. Map of the sample
   FIGURE 7 Summary of the results
   Appendix Table 2. Robustness to the sample
   Table A.7: Dotted letter prefix
   Table 4.1 Chapter style numbering
   TABLE II. Roman numerals
   表 3 回归结果
   图2：研究设计
""")
    assert {k: (v.kind, v.num) for k, v in got.items()} == {
        "Table 3": ("table", "3"), "Table A41": ("table", "A41"), "Figure 5": ("figure", "5"), "Figure 2": ("figure", "2"),
        "Figure 7": ("figure", "7"), "Table 2": ("table", "2"), "Table A.7": ("table", "A.7"),
        "Table 4.1": ("table", "4.1"), "Table II": ("table", "II"), "表 3": ("table", "3"), "图 2": ("figure", "2")}
    assert got["Table 3"].caption == "Effect of participant hawkishness on foreign policy decisions"
    assert got["Figure 5"].caption == "Leader search for counsel during meetings"        # the title on the next line
    assert got["Figure 7"].caption == "Summary of the results" and got["Table A41"].caption.startswith("Speaker Hawkishness")
    assert got["表 3"].caption == "回归结果" and got["图 2"].caption == "研究设计"
    assert (got["Table 3"].source, got["Table A41"].source, got["Table 2"].source) == ("main", "appendix", "appendix")


def test_sentences_about_a_table_are_not_captions():
    got = items_of("""
Table A1 displays summary statistics on all meetings of the National Security Council.
Figure 3 illustrates the predicted hawkishness measures.
Table A25 (which is a duplicate of Table 5 in the main text) is provided for convenience.
as shown in Figure 1), the model
Table 2, for example, reports the means.
表3显示了主要结果。
TABLE 6. Real caption
""")
    assert list(got) == ["Table 6"]


def test_a_wrapped_caption_is_joined_and_the_page_furniture_is_not_a_caption():
    got = items_of("""Table A23: Effect of Mean Participant Hawkishness and President’s Hawkishness on Foreign Policy Deci-
sions, Using Negative Binomial Models

   Emergence Model     Leader Model
TABLE 5. Effect of National Security Council principals’ hawkishness on militarized
interstate disputes (MIDs), using monthly data
Table A38: Replication of Models Using Only Observations from the United
States
Table A7: Table A7: Out-of-Sample Performance Metrics
Figure 6. Effect of adviser hawkishness on topic proportions during meetingsUniversity
Table A9: Effect of mean hawkishness
   Poisson     OLS
5.8.1  Propagating Uncertainty of Hawkishness Measures in Time-Unit Analysis  .  .  50
Table A12: Coverage of records .......... 12
""")
    assert got["Table A23"].caption.endswith("Foreign Policy Decisions, Using Negative Binomial Models")
    assert got["Table 5"].caption.endswith("militarized interstate disputes (MIDs), using monthly data")
    assert got["Table A38"].caption.endswith("from the United States")
    assert got["Table A7"].caption == "Out-of-Sample Performance Metrics"
    assert got["Figure 6"].caption == "Effect of adviser hawkishness on topic proportions during meetings"
    assert got["Table A9"].caption == "Effect of mean hawkishness"                      # the column heads are not part of it
    assert "Table A12" not in got                                                       # a table of contents entry


def test_the_first_captioned_label_wins_and_a_bare_one_gives_way():
    pages = [("1", "Table 3.\n\n(1) (2)"), ("2", "Table 3: Effect of x\n"), ("3", "TABLE 3. Later, continued page")]
    got = {i.label: i for i in cov.extract_items([cov.SourceText("main", "x", pages)])}
    assert got["Table 3"].caption == "Effect of x" and got["Table 3"].page == "2"


def test_label_lists_and_ranges():
    assert cov.mentions("see Tables A9 and A10, and Figure 7")[0][2] == ["table:A9", "table:A10", "figure:7"]
    assert cov.mentions("Tables A9–A12")[0][2] == ["table:A9", "table:A10", "table:A11", "table:A12"]
    assert cov.mentions("Table 3, Table 4 & Fig. 2")[0][2] == ["table:3", "table:4", "figure:2"]
    assert cov.norm_num("B.2") == "B2" and cov.parent_key("figure:3A") == "figure:3"


# ---------------------------------------------------------------- groups

APPENDIX = """
5 Full Results and Robustness Checks
5.1 Full Results
Table A9: Effect of Mean Participant Hawkishness
5.2 Disaggregating Results by Meeting Type
Figure A7: Summary of Three Models, Using Formal Meetings
Table A11: Effect of Mean Participant Hawkishness, Formal Meetings
6 Discussion of Leader Results
Table A32: Variance Inflation Factors for Key Covariates
Table A33: Alternative Coding of the Outcome
Table A34: Dropping the Soviet Union
7 Probing the Deliberation Mechanism
7.1 Seeking Counsel
Table A39: Key Terms for the Dictionary Approach
"""


def test_groups_follow_the_caption_and_the_heading_it_sits_under():
    got = items_of(APPENDIX, "appendix")
    assert {k: v.group for k, v in got.items()} == {
        "Table A9": "other",               # full results, though the part is called "… and Robustness Checks"
        "Figure A7": "robust", "Table A11": "robust",
        "Table A32": "other", "Table A33": "robust", "Table A34": "robust", "Table A39": "other"}
    assert got["Table A11"].section == "5.2 Disaggregating Results by Meeting Type"
    main = items_of("TABLE 1. Sample\nTable A1: Appendix material inside the main file", "main")
    assert (main["Table 1"].group, main["Table A1"].group) == ("main", "other")


def test_a_float_set_after_the_next_heading_keeps_its_own_section():
    text = """5.8.1  Propagating Uncertainty of Hawkishness Measures in Time-Unit Analysis
5.9  Statutory Members Only
Table A26: Effect of Principals’ Hawkishness on MIDs, Propagating Uncertainty from Bootstrapping
Table A27: Effect of Mean Hawkishness, Using Only Statutory Members
"""
    got = items_of(text, "appendix")
    assert got["Table A26"].section.startswith("5.8.1") and got["Table A27"].section.startswith("5.9")


# ---------------------------------------------------------------- scripts

def item(label: str, caption: str = "", section: str = "", source: str = "main") -> cov.Item:
    kind = "figure" if label.startswith("Fig") else "table"
    num = label.split()[1]
    sec = section.split()[0] if section else ""
    return cov.Item(source, kind, num, label, caption, "1", section=section, headings=[section] if section else [],
                    sec_ids=[sec] if sec else [])


R_MAIN = '''#### Table 3: Effect of Participant Hawkishness on Foreign Policy Decisions ####
stargazer(fit1, fit2, out = "tables/table3_main.tex", title = "Table 3")

#### Figure 7: Summary of Three Models ####
plotMainHawkCoefs("")
ggsave(filename="./figures/coefPlotMain.pdf", height=4, width=7, units='in')
x <- "# Table 9 inside a string is not a comment"
'''
R_FILES = '''pdf("output/Figure_A16.pdf")
plot(1)
dev.off()
write.csv(res, "output/table_a41.csv")
print(xtable(res), file = "output/summary_stats.tex")
'''
STATA = '''* Table 2: Decisions against adversaries
esttab m1 m2 ///
   using "tables/t2.tex", replace
graph export "figs/fig4.pdf", replace  // Figure 4
'''
PY = '''# Table 6: Placebo test
df.to_latex("out/tab6.tex")
# Figure 8
plt.savefig("fig8.png")
'''
README = '''Replication guide

meetingAnalysis.R: Table 3, Table 4 and Figure 7
Figure 9 is drawn by makeFigs.R
Table 12
Run plots.R
coefPlotMain.pdf is the paper's Figure 11.

Appendix
5.2: Disaggregating Results by Meeting Type
meetingAnalysisApp.R
'''


def scripts_of(it: cov.Item) -> dict[str, str]:
    return {s["path"]: s["how"] for s in it.scripts}


def test_scripts_found_through_output_calls_comments_and_file_names():
    items = [item("Table 3"), item("Figure 7"), item("Figure A16"), item("Table A41"), item("Table 2"), item("Figure 4"),
             item("Table 6"), item("Figure 8"), item("Table 9")]
    ev = cov.map_scripts(items, {"code/main.R": R_MAIN, "code/files.R": R_FILES, "code/run.do": STATA, "code/an.py": PY}, {})
    by = {i.label: i for i in items}
    assert by["Table 3"].scripts[0] | {"how": "comment"} == by["Table 3"].scripts[0]
    assert (by["Table 3"].scripts[0]["path"], by["Table 3"].scripts[0]["via"], by["Table 3"].scripts[0]["ref"]) == ("main.R", "stargazer", "main.R:1")
    assert by["Table 3"].scripts[0]["full"] == "code/main.R" and by["Table 3"].scripts[0]["have"] is True
    assert by["Table 3"].outputs == ["tables/table3_main.tex"]
    assert by["Figure 7"].outputs == ["figures/coefPlotMain.pdf"] and by["Figure 7"].scripts[0]["via"] == "ggsave"
    assert scripts_of(by["Figure A16"]) == {"files.R": "filename"} and by["Figure A16"].outputs == ["output/Figure_A16.pdf"]
    assert scripts_of(by["Table A41"]) == {"files.R": "filename"}
    assert scripts_of(by["Table 2"]) == {"run.do": "comment"} and by["Table 2"].scripts[0]["via"] == "esttab"
    assert by["Table 2"].outputs == ["tables/t2.tex"]                       # the using-line after "///"
    assert scripts_of(by["Figure 4"]) == {"run.do": "comment"} and by["Figure 4"].outputs == ["figs/fig4.pdf"]
    assert by["Table 6"].outputs == ["out/tab6.tex"] and by["Figure 8"].outputs == ["fig8.png"]
    assert by["Table 9"].scripts == []                                      # a label inside a string is not a header
    assert "output/summary_stats.tex" in {c.file for _, c in ev.orphans}


def test_the_readme_and_codebook_tie_labels_and_sections_to_scripts():
    items = [item("Table 3"), item("Table 4"), item("Figure 7"), item("Figure 9"), item("Table 12"), item("Figure 11"),
             item("Table A11", "x", "5.2 Disaggregating Results by Meeting Type", "appendix"),
             item("Table A99", "x", "5.3 Something Else", "appendix")]
    code = {"code/meetingAnalysis.R": "# nothing here\n", "code/makeFigs.R": 'ggsave("figures/coefPlotMain.pdf")\n'}
    cov.map_scripts(items, code, {"README.md": README})
    by = {i.label: i for i in items}
    assert scripts_of(by["Table 3"]) == {"meetingAnalysis.R": "doc"} and by["Table 3"].scripts[0]["have"] is True
    assert scripts_of(by["Table 4"]) == {"meetingAnalysis.R": "doc"} and scripts_of(by["Figure 7"]) == {"meetingAnalysis.R": "doc"}
    assert scripts_of(by["Figure 9"]) == {"makeFigs.R": "doc"}                        # same line, either side of the label
    assert by["Table 12"].scripts == [] or by["Table 12"].scripts[0]["path"] == "plots.R"   # the next line, a line-start label
    assert scripts_of(by["Figure 11"]) == {"makeFigs.R": "doc"} and by["Figure 11"].outputs == ["figures/coefPlotMain.pdf"]
    a = by["Table A11"].scripts[0]
    assert (a["path"], a["how"], a["have"]) == ("meetingAnalysisApp.R", "doc", False) and "5.2" in a["ref"]
    assert by["Table A99"].scripts == []
    assert "没有保存在工作区" in cov.hit_note(a) and "5.2" in cov.hit_note(a)


def test_codebook_rows_that_wrap_and_labels_in_prose():
    book = """Main manuscript

Item in Manuscript
Relevant File
Table 3: Effect of Participant Hawkishness on Foreign
Policy Decisions
meetingAnalysis.R
Figure 7: Summary of Three Models of Trait
Aggregation
meetingAnalysis.R
Table 5: Effect of NSC Principals' Hawkishness on
MIDs, Using Monthly Data
monthlyAnalysis.R
The data in meetingData.csv are used in Table 3 of the paper, see getAdmin.R for how they are built.
"""
    items = [item("Table 3"), item("Figure 7"), item("Table 5")]
    cov.map_scripts(items, {}, {"Codebook.pdf.txt": book})
    assert [scripts_of(i) for i in items] == [{"meetingAnalysis.R": "doc"}, {"meetingAnalysis.R": "doc"}, {"monthlyAnalysis.R": "doc"}]


def test_a_copied_comment_header_maps_by_its_title_and_words_map_the_leftovers():
    script = '''#### Table A50: Distribution of Agenda Items Across All Meetings ####
print(topicTable, include.rownames=F)
#### Table A50: OLS Regressions on the Relationship Between Meeting Topics and Average Hawkishness of Meeting Participants ####
stargazer(topicFit, topicFitF)
'''
    items = [item("Table A50", "Distribution of Agenda Items Across All Meetings"),
             item("Table A51", "OLS Regressions on the Relationship Between Meeting Topics and Average Hawkishness of Meeting Participants"),
             item("Figure 12", "Deliberation patterns by administration, leader hawkishness")]
    loose = 'png("figs/deliberationAdminPatterns.png")\nplot(1)\n'
    cov.map_scripts(items, {"agenda.R": script, "late.R": loose}, {})
    assert scripts_of(items[0]) == {"agenda.R": "comment"}
    assert items[1].scripts[0]["via"] == "title" and items[1].scripts[0]["path"] == "agenda.R"
    assert scripts_of(items[2]) == {"late.R": "words"} and items[2].outputs == ["figs/deliberationAdminPatterns.png"]


# ---------------------------------------------------------------- stored: the plan page and its numbers

def pdf(path: Path, pages: list[str]) -> Path:
    import pymupdf
    doc = pymupdf.open()
    for t in pages:
        doc.new_page().insert_text((60, 70), t, fontsize=10)
    doc.save(path)
    doc.close()
    return path


MAIN_PAGES = [
    "Advisers and Aggregation\nA study of national security meetings\nT. Jost\nAbstract\nWe study who decides in meetings.\n",
    "4. Results\nTABLE 3. Effect of participant hawkishness on foreign policy decisions\n(1) (2)\nMean 0.231 0.198\n"
    "FIGURE 7. Summary of the results of three models of trait aggregation\n",
    "TABLE 5. Effect of principals' hawkishness on disputes, using monthly data\nN 1,204\nReferences\nCallaway, B. 2021.\n",
]
APPENDIX_PAGES = [
    "Online Appendix\n5 Full Results and Robustness Checks\n5.1 Full Results\nTable A9: Effect of mean participant hawkishness\n",
    "5.2 Removing the Soviet Union\nTable A21: Effect of mean hawkishness, Removing Decisions Involving the USSR\n"
    "Figure A12: Summary of three models, Removing Decisions Involving the USSR\n",
]
AUTHOR_R = "#### Table 3: Effect of Participant Hawkishness ####\nstargazer(fit, out = 'tables/t3.tex')\n"


def setup_plan(conn, tmp_path: Path, *, appendix: bool = False) -> tuple[str, str]:
    from research_lab.config import get_paths
    from research_lab.intake import pipeline
    paper_id = ingest_quiet(conn, pdf(tmp_path / "jost.pdf", MAIN_PAGES))
    if appendix:
        pipeline.attach_file(conn, pdf(tmp_path / "appendix.pdf", APPENDIX_PAGES), paper_id, ai=False)
    plan_id = plans.create_blank(conn, paper_id)
    code = get_paths().data / "replication" / plan_id / "workspace" / "author" / "code"
    code.mkdir(parents=True)
    (code / "meetingAnalysis.R").write_text(AUTHOR_R)
    (code.parent / "README.md").write_text("meetingAnalysis.R\nTable 5\n")      # a label under the file name is not a row
    return paper_id, plan_id


def test_scan_stores_the_list_and_ticks_the_main_text(conn, tmp_path):
    _, plan_id = setup_plan(conn, tmp_path)
    done = cov.rescan(conn, plan_id)
    assert (done["added"], done["total"], done["mapped"]) == (3, 3, 1)
    rows = cov.items(conn, plan_id)
    assert [(r["label"], r["group"], r["ticked"], r["source"]) for r in rows] == [
        ("Table 3", "main", True, "main"), ("Figure 7", "main", True, "main"), ("Table 5", "main", True, "main")]
    t3 = rows[0]
    assert t3["page"] == "2" and t3["scripts"][0]["path"] == "meetingAnalysis.R" and "stargazer" in t3["scripts"][0]["note"]
    assert rows[0]["outputs"] == ["tables/t3.tex"] and rows[1]["scripts"] == []
    page = cov.overview(conn, plan_id)
    assert [(g["label"], g["total"], g["ticked"], g["mapped"]) for g in page["groups"]] == [("正文结果", 3, 3, 1)]
    assert page["has_appendix"] is False


def test_leos_ticks_and_owners_survive_a_rescan_and_the_appendix_adds_its_groups(conn, tmp_path):
    from research_lab.intake import pipeline
    paper_id, plan_id = setup_plan(conn, tmp_path)
    cov.rescan(conn, plan_id)
    ids = {r["label"]: r["id"] for r in cov.items(conn, plan_id)}
    n = cov.save_marks(conn, plan_id, list(ids.values()), [ids["Table 3"], ids["Table 5"]],
                       {ids["Table 3"]: "  小林  ", ids["Figure 7"]: "x" * 90})
    assert n == 2                                                              # Figure 7 unticked, with an owner; Table 3 an owner
    pipeline.attach_file(conn, pdf(tmp_path / "appendix.pdf", APPENDIX_PAGES), paper_id, ai=False)
    done = cov.rescan(conn, plan_id)
    assert (done["added"], done["kept"], done["dropped"]) == (3, 3, 0)
    rows = {r["label"]: r for r in cov.items(conn, plan_id)}
    assert (rows["Table 3"]["owner"], rows["Table 3"]["ticked"], rows["Figure 7"]["ticked"]) == ("小林", True, False)
    assert len(rows["Figure 7"]["owner"]) == cov.OWNER_CHARS
    assert [(r["label"], r["group"], r["ticked"]) for r in rows.values() if r["source"] == "appendix"] == [
        ("Table A9", "other", False), ("Table A21", "robust", False), ("Figure A12", "robust", False)]
    assert [g["label"] for g in cov.overview(conn, plan_id)["groups"]] == ["正文结果", "附录稳健性", "附录其他"]
    # the appendix file goes: a scan that reads no appendix leaves what Leo wrote beside its items alone …
    appendix_file = conn.execute("SELECT id FROM paper_files WHERE paper_id = ? AND is_primary = 0", (paper_id,)).fetchone()[0]
    pipeline.remove_file(conn, paper_id, appendix_file)
    assert cov.rescan(conn, plan_id)["dropped"] == 0 and len(cov.items(conn, plan_id)) == 6
    # … and one that reads a new appendix drops what that file no longer has
    pipeline.attach_file(conn, pdf(tmp_path / "appendix2.pdf", [APPENDIX_PAGES[0]]), paper_id, ai=False)
    done = cov.rescan(conn, plan_id)
    assert done["dropped"] == 2 and {r["label"] for r in cov.items(conn, plan_id) if r["source"] == "appendix"} == {"Table A9"}
    assert cov.save_marks(conn, plan_id, [987654], [987654], {}) == 0          # an id of no item of this plan changes nothing


def test_the_command_line_takes_an_appendix_by_path_and_reads_without_writing(conn, tmp_path, capsys):
    _, plan_id = setup_plan(conn, tmp_path)
    appendix = pdf(tmp_path / "appendix.pdf", APPENDIX_PAGES)
    before = conn.execute("SELECT COUNT(*) FROM replication_coverage").fetchone()[0]
    assert cov.main([plan_id, "--appendix", str(appendix)]) == 0
    out = capsys.readouterr().out
    assert "正文结果：3 项，对上脚本 1 项" in out and "附录稳健性：2 项" in out and "附录其他：1 项" in out and "合计 6 项" in out
    assert conn.execute("SELECT COUNT(*) FROM replication_coverage").fetchone()[0] == before == 0
    assert conn.execute("SELECT COUNT(*) FROM replication_coverage_sources").fetchone()[0] == 0
    assert cov.main([plan_id, "--appendix", str(appendix), "--save"]) == 0     # now stored, with its page texts
    assert conn.execute("SELECT page_count FROM replication_coverage_sources").fetchone()[0] == 2
    done = cov.rescan(conn, plan_id)                                           # the page's Scan finds the stored appendix too
    assert done["total"] == 6 and {r["source"] for r in cov.items(conn, plan_id)} == {"main", "appendix"}
    cov.add_source(conn, plan_id, appendix)                                    # the same PDF twice is stored once
    assert conn.execute("SELECT COUNT(*) FROM replication_coverage_sources").fetchone()[0] == 1


def test_a_plan_without_text_says_so_and_deleting_the_plan_clears_the_list(conn, tmp_path):
    blank = plans.create_blank(conn)
    with pytest.raises(cov.CoverageError):
        cov.rescan(conn, blank)
    _, plan_id = setup_plan(conn, tmp_path)
    cov.rescan(conn, plan_id)
    plans.delete(conn, plan_id)
    assert conn.execute("SELECT COUNT(*) FROM replication_coverage").fetchone()[0] == 0


def test_the_plan_page_lists_scans_and_saves(data_client, conn, tmp_path):
    _, plan_id = setup_plan(conn, tmp_path, appendix=True)
    conn.commit()
    html = data_client.get(f"/data/plans/{plan_id}/detail").text
    assert "全书复现清单" in html and ">Scan</button>" in html and "还没有清单" in html
    r = data_client.post(f"/data/plans/{plan_id}/coverage/scan", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("#coverage") and "msg=" in r.headers["location"]
    html = data_client.get(f"/data/plans/{plan_id}/detail").text
    for text in ("正文结果", "附录稳健性", "附录其他", "Table 3", "Figure A12", "meetingAnalysis.R", "汇报人", ">Save</button>"):
        assert text in html
    ids = {r["label"]: r["id"] for r in cov.items(conn, plan_id)}
    assert len(ids) == 6
    form = {"cov_id": [str(i) for i in ids.values()], "cov_tick": [str(ids["Table A21"]), str(ids["Table 3"])],
            f"cov_owner_{ids['Table 3']}": "小林", f"cov_owner_{ids['Figure 7']}": ""}
    r = data_client.post(f"/data/plans/{plan_id}/coverage", data=form, follow_redirects=False)
    assert r.status_code == 303
    rows = {r["label"]: r for r in cov.items(conn, plan_id)}
    assert [l for l, r in rows.items() if r["ticked"]] == ["Table 3", "Table A21"] and rows["Table 3"]["owner"] == "小林"
    html = data_client.get(f"/data/plans/{plan_id}/detail").text
    assert 'value="小林"' in html and f'name="cov_tick" value="{ids["Table A21"]}" checked' in html
    assert data_client.post("/data/plans/RP-9999-0001/coverage/scan", follow_redirects=False).status_code == 404


# ---------------------------------------------------------------- somebody else's text: bounded and linear

def within(seconds: float, fn, *args, **kw):
    import time
    start = time.perf_counter()
    out = fn(*args, **kw)
    took = time.perf_counter() - start
    assert took < seconds, f"{getattr(fn, '__name__', fn)} took {took:.2f}s"
    return out


def one_page(text: str, source: str = "main") -> list[cov.SourceText]:
    return [cov.SourceText(source, "x.pdf", [("1", text)])]


def test_hostile_text_cannot_stall_the_scan():
    """Each input below took seconds to hours before the ceilings and the linear patterns (card 0187 review); each must
    now finish in under a second."""
    # a caption or heading line of 20k "." — the table-of-contents pattern used to be quadratic
    assert within(1, cov.extract_items, one_page("Table 3. " + ". " * 40_000)) == []
    assert within(1, cov.extract_items, one_page("5.8.1  Foo bar" + " . " * 20_000 + "\nTable A1: x", "appendix"))[0].label == "Table A1"
    long_caption = within(1, cov.extract_items, one_page("Table 3: " + "word " * 40_000))
    assert len(long_caption[0].caption) <= cov.CAPTION_CHARS
    assert within(1, cov.extract_items, one_page(("Table 3. " + ". " * 250 + "\n") * 3_000)) == []
    # a label comment followed by 80k blanks (or one 2 MB line): the trailing-filler pattern was quadratic
    for pad in (80_000, 2_000_000):
        info = within(1, cov.scan_script, "a.R", "# Table 1" + " " * pad + "x\npdf('a.pdf')\n")
        assert [e.keys for e in info.labels] == [["table:1"]] and [c.name for c in info.calls] == ["pdf"]
    assert within(1, cov.scan_script, "a.py", "a." * 100_000 + "savefig('x.png')\n").calls == []
    # 30k label comments against 30k pdf() calls, in one script: counted and capped, matched by line number
    r = "\n".join(f"# Table A{i % 999 + 1}: Effect number {i}\npdf('a{i}.pdf')" for i in range(30_000))
    info = within(1, cov.scan_script, "a.R", r)
    assert len(info.labels) == cov.MAX_LABELS and len(info.calls) == cov.MAX_CALLS
    ev = within(1, cov.gather_evidence, {"a.R": r}, {})
    assert 0 < len(ev.hits) <= cov.MAX_LABELS and len(ev.orphans) <= cov.MAX_ORPHANS
    # 900 captions against 20k output calls no label covers, and against 40k captioned comments
    items = [item(f"Table {i}", f"Effect of thing {i} on outcome measure number {i}") for i in range(900)]
    orphans = {f"s{k}.R": "\n".join(f"png('output/results_thing_{k}_{j}_effect.png')" for j in range(1_000)) for k in range(20)}
    ev = within(1, cov.map_scripts, items, orphans, {})
    assert len(ev.orphans) == cov.MAX_ORPHANS
    commented = {f"c{k}.R": "\n".join(f"# Table {k * 2000 + j}: Effect of thing {j} on outcome measure number {j}\nstargazer(x)"
                                      for j in range(2_000)) for k in range(20)}
    commented = {k: v.replace("# Table", "# Table A") for k, v in commented.items()}
    within(1, cov.map_scripts, items, commented, {})
    # Stata: 15k esttab statements, each used to split the whole file again
    stata = within(1, cov.scan_script, "a.do", "\n".join('esttab m1 using "tables/t%d.tex", replace' % i for i in range(15_000)))
    assert len(stata.calls) == cov.MAX_CALLS and stata.calls[0].file == "tables/t0.tex"
    # a README with 60k label+script lines, and one 400 KB line of path pieces
    labels, _ = within(1, cov.doc_links, "Table 1 meetingAnalysis.R\n" * 60_000)
    assert 0 < len(labels) <= cov.MAX_MARKS
    assert within(1, cov.doc_links, "a/" * 200_000) == ([], [])
    assert within(1, cov.doc_links, "Table 1, " * 100_000)[0] == []
    assert len(within(1, cov.doc_links, "Appendix\n" + "5.1: Foo x.R\n" * 50_000)[1]) <= cov.MAX_MARKS


def test_only_the_first_items_are_listed_and_the_rest_are_counted():
    labels = [f"{a}{b}{n}" for a in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for b in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for n in (1, 2, 3, 4)]
    pages = [(str(p), "\n".join(f"Table {x}: Caption of {x}" for x in labels[p * 300:(p + 1) * 300])) for p in range(7)]
    items, omitted = within(1, cov.extract, [cov.SourceText("main", "x.pdf", pages)])
    assert (len(items), omitted) == (cov.MAX_ITEMS, 7 * 300 - cov.MAX_ITEMS) and items[0].label == "Table AA1"
    assert len(cov.extract_items([cov.SourceText("main", "x.pdf", pages)], limit=10)) == 10


def test_the_ceiling_is_said_on_the_page(data_client, conn, tmp_path, monkeypatch):
    monkeypatch.setattr(cov, "MAX_ITEMS", 2)
    _, plan_id = setup_plan(conn, tmp_path)
    conn.commit()
    r = data_client.post(f"/data/plans/{plan_id}/coverage/scan", follow_redirects=False)
    from urllib.parse import unquote_plus
    assert r.status_code == 303 and "另有 1 项未列出" in unquote_plus(r.headers["location"])
    assert len(cov.items(conn, plan_id)) == 2 and cov.overview(conn, plan_id)["omitted"] == 1
    assert "另有 1 项未列出：清单最多列 2 项" in data_client.get(f"/data/plans/{plan_id}/detail").text


def test_ids_that_only_look_like_digits_are_ignored(data_client, conn, tmp_path):
    _, plan_id = setup_plan(conn, tmp_path)
    conn.commit()
    cov.rescan(conn, plan_id)
    first = cov.items(conn, plan_id)[0]["id"]
    form = {"cov_id": [str(first), "²", "٣", "-1", "1e3", ""], "cov_tick": ["²", "٣", str(first)]}
    r = data_client.post(f"/data/plans/{plan_id}/coverage", data=form, follow_redirects=False)
    assert r.status_code == 303 and cov.items(conn, plan_id)[0]["ticked"] is True
