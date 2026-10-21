"""Card 0183: a replication source is listed as a package (data, author code, README / codebook), the code is read as text
only, and package.py finds which data file each script reads and which models it fits, so the default pick is the data
the main analysis needs. Code snippets are small fixtures; HTTP is stubbed; nothing is executed."""
from __future__ import annotations

import re
import stat

import httpx
import pytest

from research_lab.replication import fetch, package, plans

from .test_data import data_client  # noqa: F401 - a fixture

MB = 1024 * 1024
CSV = b"country,year,y\nA,2010,1.5\nA,2011,2.5\nB,2010,0.5\nB,2011,0.7\n"


def files(*names: str) -> list[str]:
    return list(names)


# ---------------------------------------------------------------- reading the code

def test_r_reads_resolve_against_the_listing_and_comments_and_strings_are_ignored():
    repo = files("data/meetingData.csv", "data/panel.dta", "data/panel.rds", "data/y.csv", "data/z.csv", "otherdata/x.dta",
                 "data/ghost.csv", "code/a.R")
    text = '''
# fread("./data/ghost.csv") is only a comment
note = "read.csv('./data/ghost.csv')"
a = fread("./data/meetingData.csv")
b = haven::read_dta("./otherdata/x.dta")
c = readRDS(file.path("data", "panel.rds"))
d = read_csv(here::here("data", "y.csv"))
e = read.csv(paste0(dir, "/z.csv"))
f = read_dta(file = "data/panel.dta")
g = read.csv(paste("data", "meetingData.csv", sep = "/"))
h = read.csv(some_variable)
fit = lm(y ~ x, data = a)
'''
    s = package.scan("code/a.R", text, repo, repo)
    assert s.reads == ["data/meetingData.csv", "otherdata/x.dta", "data/panel.rds", "data/y.csv", "data/z.csv",
                       "data/panel.dta"]
    assert "data/ghost.csv" not in s.reads and s.unresolved == []              # a variable path is not even a guess
    assert s.models == {"lm": 1}


def test_a_path_is_resolved_from_the_scripts_folder_the_repository_root_or_its_unique_tail():
    repo = files("data/x.csv", "code/analysis/run.R", "a/same.csv", "b/same.csv", "code/b/same2.csv", "raw/y.csv")
    text = '''
p = fread("../../data/x.csv")
q = fread("./data/x.csv")
r = fread("C:\\\\Users\\\\Eric\\\\Dropbox\\\\io\\\\raw\\\\y.csv")
s = fread("same.csv")
t = fread("nowhere/none.csv")
glm(y ~ x, data = p)
'''
    s = package.scan("code/analysis/run.R", text, repo, repo)
    assert s.reads == ["data/x.csv", "raw/y.csv"]                              # root-relative and a laptop path by its tail
    assert s.unresolved == ["same.csv", "nowhere/none.csv"]                    # two files of that name: no guess


def test_stata_use_import_merge_and_the_model_commands():
    repo = files("data/panel.dta", "data/a.csv", "data/b.dta", "c.csv", "data/x.xlsx", "code/clean.do", "code/main.do")
    text = '''
* use "data/ghost.dta"
use "data/panel.dta", clear
use id year using "$data/b.dta", clear
import delimited using "data/a.csv", clear
import excel "data/x.xlsx", firstrow
insheet using c.csv
merge 1:1 id using "data/b.dta"
do "./code/clean.do"
reg y x
eststo m1: xtreg y x, fe
quietly logit z x
local reg = 1
gen reg = 2
'''
    s = package.scan("code/main.do", text, repo, [f for f in repo if package.is_code(f)])
    assert s.reads == ["data/panel.dta", "data/b.dta", "data/a.csv", "data/x.xlsx", "c.csv"]
    assert s.sources == ["code/clean.do"] and s.models == {"reg": 1, "xtreg": 1, "logit": 1}


def test_python_read_calls_and_model_calls():
    repo = files("data/p.csv", "data/q.dta", "data/r.parquet", "helpers.py", "run.py")
    text = '''
import pandas as pd
import statsmodels.formula.api as smf
import statsmodels.api as sm
import helpers
df = pd.read_csv("data/p.csv")  # pd.read_csv("data/ghost.csv")
q = pandas.read_stata(os.path.join("data", "q.dta"))
r = pd.read_parquet(DATA / "r.parquet")
m = smf.ols("y ~ x", df).fit()
l = sm.Logit(y, X).fit()
'''
    s = package.scan("run.py", text, repo, repo)
    assert s.reads == ["data/p.csv", "data/q.dta", "data/r.parquet"] and s.sources == ["helpers.py"]
    assert s.models == {"smf.ols": 1, "sm.Logit": 1}


# ---------------------------------------------------------------- the main scripts

CASE = files("data/meetingData.csv", "data/monthlyData.csv", "data/principalsHawkData.csv", "data/dictionaryData.csv",
             "data/meetingLongData.csv", "data/panel.RData", "code/meetingAnalysis.R", "code/monthlyAnalysis.R",
             "code/hawkRegressions.R", "code/loadPackages.R", "code/makeCoefPlots.R", "code/meetingAnalysisApp.R",
             "code/meetingAnalysisIter.R", "code/regressIter.R", "code/dictionaryAnalysis.R", "code/setup.R")
CODE = {
    "code/meetingAnalysis.R": 'source("./code/loadPackages.R")\nsource("./code/makeCoefPlots.R")\n'
                              'd = fread("./data/meetingData.csv")\nf1 = glm(a ~ b, data = d, family = poisson)\nf2 = lm(a ~ b, data = d)\n',
    "code/monthlyAnalysis.R": 'source("./code/loadPackages.R")\nm = fread("./data/monthlyData.csv")\nglm(n ~ h, data = m)\n',
    "code/hawkRegressions.R": 'p = read.csv(file.path("data", "principalsHawkData.csv"))\nlm(h ~ x, data = p)\n',
    "code/loadPackages.R": 'library(data.table)\n',
    "code/makeCoefPlots.R": 'refit = function(d) lm(y ~ x, data = d)\n',
    "code/meetingAnalysisApp.R": 'load("./data/panel.RData")\nglm(a ~ b, data = d)\n',
    "code/meetingAnalysisIter.R": 'source("./code/regressIter.R")\nd = fread("./data/meetingLongData.csv")\n',
    "code/regressIter.R": 'f = function(d) glm(a ~ b, data = d)\n',
    "code/dictionaryAnalysis.R": 'mad = fread("./data/dictionaryData.csv")\nlm(p ~ h, data = mad)\n',
    "code/setup.R": 'setwd("C:/Users/Eric/Dropbox/analysis/")\n',
}
UNUSABLE = {"data/meetingLongData.csv": "超过 500 MB 上限，不下载", "data/dictionaryData.csv": "大于 50 MB，不默认勾选",
            "data/panel.RData": "需要 R"}


def test_main_scripts_are_the_entry_scripts_that_read_data_and_fit_models():
    pkg = package.analyze(CODE, CASE, unusable=UNUSABLE)
    assert pkg.main == ["code/meetingAnalysis.R", "code/monthlyAnalysis.R", "code/hawkRegressions.R"]
    assert pkg.default_data == ["data/meetingData.csv", "data/monthlyData.csv", "data/principalsHawkData.csv"]
    assert pkg.helpers == ["code/loadPackages.R", "code/makeCoefPlots.R", "code/regressIter.R"]    # never main
    assert pkg.reads["code/meetingAnalysis.R"] == ["data/meetingData.csv"]
    assert pkg.models["code/meetingAnalysis.R"] == {"glm": 1, "lm": 2}         # its own two plus the helper's refit
    assert package.format_models(pkg.models["code/meetingAnalysis.R"]) == "glm, lm ×2"
    assert pkg.readers_of("data/meetingData.csv") == ["code/meetingAnalysis.R"]
    assert pkg.reads["code/meetingAnalysisIter.R"] == ["data/meetingLongData.csv"] and "code/setup.R" not in pkg.reads["code/setup.R"]


def test_a_helper_that_reads_and_fits_still_credits_the_script_that_loads_it():
    code = {"run.R": 'source("lib.R")\nout = go()\n', "lib.R": 'go = function() { d = read.csv("data/d.csv"); lm(y ~ x, d) }\n'}
    pkg = package.analyze(code, ["run.R", "lib.R", "data/d.csv"])
    assert pkg.main == ["run.R"] and pkg.helpers == ["lib.R"] and pkg.default_data == ["data/d.csv"]
    assert pkg.models["run.R"] == {"lm": 1}


def test_robustness_variants_by_name_or_by_the_readme_and_scripts_that_need_unusable_data_are_not_main():
    pkg = package.analyze(CODE, CASE, unusable=UNUSABLE)
    assert pkg.not_main["code/meetingAnalysisApp.R"].startswith("名字像") and pkg.not_main["code/meetingAnalysisIter.R"].startswith("名字像")
    assert "dictionaryData.csv" in pkg.not_main["code/dictionaryAnalysis.R"] and "大于 50 MB" in pkg.not_main["code/dictionaryAnalysis.R"]
    readme = "meetingAnalysis.R   Performs the meeting-level analysis (Section 4.1)\nmonthlyAnalysis.R   Performs a robustness check of the monthly results\n"
    pkg = package.analyze(CODE, CASE, docs={"README.md": readme}, unusable=UNUSABLE)
    assert "code/monthlyAnalysis.R" not in pkg.main and "robust" in pkg.not_main["code/monthlyAnalysis.R"]
    assert pkg.default_data == ["data/meetingData.csv", "data/principalsHawkData.csv"]


def test_when_every_candidate_looks_secondary_the_rule_still_answers_and_main_can_be_named():
    code = {"appendix_run.R": 'x = read.csv("data/d.csv")\nlm(y ~ x, x)\n'}
    pkg = package.analyze(code, ["appendix_run.R", "data/d.csv"])
    assert pkg.main == ["appendix_run.R"] and pkg.default_data == ["data/d.csv"]
    named = package.analyze(CODE, CASE, unusable=UNUSABLE, main=["meetingAnalysisApp.R"])
    assert named.main == ["code/meetingAnalysisApp.R"] and named.default_data == [] and named.left_out == {"data/panel.RData": "需要 R"}
    with pytest.raises(ValueError, match="没有这个脚本"):
        package.analyze(CODE, CASE, main=["nope.R"])


def test_classify_puts_code_documents_data_and_the_rest_apart():
    assert [fetch.classify(n) for n in ["a/run.R", "x.Rmd", "clean.do", "m.py", "q.sql", "go.sh", "d/p.csv", "p.dta", "Replication Codebook.pdf",
                                        "README.md", "README", "notes.txt", "fig.pdf", "x.RData", "codebook.csv"]] == [
        "code", "code", "code", "code", "code", "code", "data", "data", "doc", "doc", "doc", "other", "other", "other", "data"]


# ---------------------------------------------------------------- a source shaped like the Jost et al. package, HTTP stubbed

def _pdf(text: str) -> bytes:
    import pymupdf
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    return doc.tobytes()


CODEBOOK = _pdf("Replication codebook. meetingAnalysis.R reproduces Table 3.")
# id → (folder, name, size, content or None when it must never be requested, original name of an ingested csv)
SOURCE = {
    1: ("data", "meetingData.tab", None, CSV, "meetingData.csv"), 2: ("data", "monthlyData.tab", None, CSV, "monthlyData.csv"),
    3: ("data", "principalsHawkData.tab", None, CSV, "principalsHawkData.csv"),
    4: ("data", "dictionaryData.tab", 203 * MB, None, "dictionaryData.csv"),
    5: ("data", "meetingLongData.tab", 804 * MB, None, "meetingLongData.csv"),
    6: ("data", "panel.RData", 12 * MB, None, None),
    20: ("code", "meetingAnalysis.R", None, CODE["code/meetingAnalysis.R"].encode(), None),
    21: ("code", "monthlyAnalysis.R", None, CODE["code/monthlyAnalysis.R"].encode(), None),
    22: ("code", "hawkRegressions.R", None, CODE["code/hawkRegressions.R"].encode(), None),
    23: ("code", "loadPackages.R", None, CODE["code/loadPackages.R"].encode(), None),
    24: ("code", "makeCoefPlots.R", None, CODE["code/makeCoefPlots.R"].encode(), None),
    25: ("code", "meetingAnalysisApp.R", None, CODE["code/meetingAnalysisApp.R"].encode(), None),
    26: ("code", "meetingAnalysisIter.R", None, CODE["code/meetingAnalysisIter.R"].encode(), None),
    27: ("code", "regressIter.R", None, CODE["code/regressIter.R"].encode(), None),
    28: ("code", "dictionaryAnalysis.R", None, CODE["code/dictionaryAnalysis.R"].encode(), None),
    29: ("code", "setup.R", None, CODE["code/setup.R"].encode(), None),
    30: (None, "Advisers - Replication Codebook.pdf", None, CODEBOOK, None),
    31: ("figures", "meetPlot.pdf", 100_000, None, None),
}
DOI = "10.7910/DVN/PKG123"
MAIN_SCRIPTS = ["code/meetingAnalysis.R", "code/monthlyAnalysis.R", "code/hawkRegressions.R"]
MAIN_DATA = ["data/meetingData.csv", "data/monthlyData.csv", "data/principalsHawkData.csv"]


def _dataverse() -> dict:
    out = []
    for fid, (folder, name, size, content, original) in SOURCE.items():
        df = {"id": fid, "filename": name, "filesize": size or len(content or b"")}
        if original:
            df.update(originalFileName=original, originalFileSize=size or len(content or b""))
        out.append({"label": name, "directoryLabel": folder, "dataFile": df} if folder else {"label": name, "dataFile": df})
    return {"status": "OK", "data": {"latestVersion": {"files": out, "metadataBlocks": {"citation": {"fields": [
        {"typeName": "title", "value": "Replication Data for: Advisers"}]}}}}}


@pytest.fixture()
def web(monkeypatch):
    """A Dataverse that serves SOURCE; a file whose content is None fails the test if anything asks for it."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/datasets/:persistentId/":
            return httpx.Response(200, json=_dataverse())
        m = re.fullmatch(r"/api/access/datafile/(\d+)", request.url.path)
        if m and SOURCE[int(m.group(1))][3] is not None:
            return httpx.Response(200, content=SOURCE[int(m.group(1))][3])
        raise AssertionError(f"nothing may ask for {request.url}")

    def new() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

    monkeypatch.setattr(fetch, "_client", lambda http: http if http is not None else new())   # the CLI and the page build their own
    return new(), calls


def test_listing_has_code_docs_and_data_apart_and_greys_out_what_is_over_its_cap(web):
    client, calls = web
    listing = fetch.list_source(DOI, client)
    assert [f.name for f in listing.files] == ["data/meetingData.csv", "data/monthlyData.csv", "data/principalsHawkData.csv",
                                               "data/dictionaryData.csv", "data/meetingLongData.csv"]
    assert len(listing.code) == 10 and listing.docs[0].name == "Advisers - Replication Codebook.pdf"
    assert [f.name for f in listing.other] == ["data/panel.RData", "figures/meetPlot.pdf"]
    big = next(f for f in listing.files if f.name.endswith("meetingLongData.csv"))
    assert not big.fetchable and "500 MB" in big.note and listing.files[3].fetchable          # 203 MB is under the cap
    with pytest.raises(fetch.FetchError, match="暂时取不了"):
        fetch.pick(listing, ["meetingLongData.csv"])
    assert fetch.pick(listing, ["1", "meetingAnalysis.R", "Advisers - Replication Codebook.pdf"])[1].kind == "code"
    assert not any("datafile" in c for c in calls)                                           # listing downloads nothing
    small = fetch.list_source(DOI, client, cap=100 * MB)
    assert not small.files[3].fetchable and "100 MB" in small.files[3].note


def test_auto_picks_exactly_the_data_the_main_scripts_read_and_never_asks_for_the_big_files(web, conn):
    client, calls = web
    plan = plans.create_blank(conn)
    auto, results = fetch.fetch_auto(conn, DOI, plan, http=client)
    assert auto.pkg.main == MAIN_SCRIPTS and [r.name for r in auto.data] == MAIN_DATA
    assert [r.name for r in auto.docs] == ["Advisers - Replication Codebook.pdf"]
    assert [r.error for r in results] == [None] * 7
    from research_lab.replication import datasets
    attached = [datasets.get(conn, d)["name"] for d in plans.get(conn, plan)["plan"]["datasets"]]
    assert sorted(attached) == ["meetingData", "monthlyData", "principalsHawkData"]
    assert sorted(r.saved for r in results if r.saved) == sorted(
        ["author/code/meetingAnalysis.R", "author/code/monthlyAnalysis.R", "author/code/hawkRegressions.R",
         "author/Advisers - Replication Codebook.pdf.txt"])
    asked = {int(c.rsplit("/", 1)[1]) for c in calls if c.startswith("/api/access/datafile/")}
    assert asked.isdisjoint({4, 5, 6, 31})                                                    # dictionary, long, RData, figure


def test_code_is_saved_as_text_only_under_the_plans_workspace(web, conn):
    from research_lab.replication import workspace
    client, _ = web
    plan = plans.create_blank(conn)
    out = fetch.fetch(conn, DOI, ["meetingAnalysis.R", "Advisers - Replication Codebook.pdf"], plan_id=plan, http=client)
    ws = workspace.workspace_dir(plan)
    script = ws / "author/code/meetingAnalysis.R"
    assert [r.saved for r in out] == ["author/code/meetingAnalysis.R", "author/Advisers - Replication Codebook.pdf.txt"]
    assert script.read_text() == CODE["code/meetingAnalysis.R"] and not script.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    assert "Replication codebook" in (ws / "author/Advisers - Replication Codebook.pdf.txt").read_text()
    assert not (ws / "author/Advisers - Replication Codebook.pdf").exists()                    # the PDF itself is not kept
    no_plan = fetch.fetch(conn, DOI, ["meetingAnalysis.R"], http=client)
    assert no_plan[0].saved is None and "工作区" in no_plan[0].error
    with pytest.raises(fetch.FetchError):
        fetch._as_text(b"MZ\x00\x00binary", "tool.R")
    assert fetch._as_text("x <- 1 # é".encode("latin-1"), "latin.R") == "x <- 1 # é"
    assert fetch._author_path("../../etc/x.R", False).as_posix() == "author/etc/x.R"


# ---------------------------------------------------------------- the command and the plan page

def test_the_cli_dry_run_prints_the_mapping_and_writes_nothing(web, repo, conn):
    from typer.testing import CliRunner
    from research_lab.cli import data as cli
    from research_lab.replication import datasets, workspace
    before = len(datasets.list_all(conn))
    out = CliRunner().invoke(cli.app, ["fetch", DOI, "--dry-run", "--auto"])
    assert out.exit_code == 0, out.output
    for name in [*MAIN_SCRIPTS, *MAIN_DATA]:
        assert name in out.output
    assert "主分析 (3)" in out.output and "glm, lm ×2" in out.output and "默认选择·数据 (3)" in out.output
    assert "dictionaryAnalysis.R  要用的 dictionaryData.csv 默认不取" in out.output and "Replication Codebook.pdf" in out.output
    assert len(datasets.list_all(conn)) == before and not (workspace.plan_root("RP-2026-0001")).exists()
    named = CliRunner().invoke(cli.app, ["fetch", DOI, "--dry-run", "--main", "monthlyAnalysis.R"])
    assert named.exit_code == 0 and "主分析 (1)" in named.output and "默认选择·数据 (1)" in named.output
    assert CliRunner().invoke(cli.app, ["fetch", DOI, "--auto"]).exit_code != 0                  # --auto needs a plan
    listed = CliRunner().invoke(cli.app, ["fetch", DOI])
    assert "Code (10)" in listed.output and "Docs (1)" in listed.output and "Data (5)" in listed.output and "[超过 500 MB 上限" in listed.output


def test_the_cli_auto_downloads_the_default_pick_into_the_plan(web, repo, conn):
    from typer.testing import CliRunner
    from research_lab.cli import data as cli
    plan = plans.create_blank(conn)
    conn.commit()
    out = CliRunner().invoke(cli.app, ["fetch", DOI, "--plan", plan, "--auto"])
    assert out.exit_code == 0, out.output
    assert out.output.count("已存 author/") == 4 and f"已挂到方案 {plan}" in out.output
    assert len(plans.get(conn, plan)["plan"]["datasets"]) == 3


def test_plan_page_lists_code_docs_and_data_with_the_main_analysis_ticked(data_client, conn, web):
    client, calls = web
    plan = plans.create_blank(conn)
    plans.save_links(conn, plan, [{"url": f"https://doi.org/{DOI}", "kind": "dataverse"}])
    conn.commit()
    page = data_client.get(f"/data/plans/{plan}/detail").text
    v = re.search(r"/links/0/files\?v=([0-9a-f]+)", page).group(1)
    html = data_client.get(f"/data/plans/{plan}/links/0/files?v={v}").text
    for title in ("Code", "Docs", "Data"):
        assert f"<strong>{title}</strong>" in html
    box = html[html.index("<strong>主分析</strong>"):html.index("<form")]
    assert all(s in box for s in MAIN_SCRIPTS) and "glm, lm ×2" in box and "data/principalsHawkData.csv" in box
    assert "没有用 AI" in box and "dictionaryAnalysis.R" not in box

    def checked(name: str) -> bool:
        return bool(re.search(rf'value="{re.escape(name)}" aria-label="[^"]*" checked', html))
    assert all(checked(n) for n in [*MAIN_DATA, *MAIN_SCRIPTS, "Advisers - Replication Codebook.pdf"])
    assert not checked("data/dictionaryData.csv") and not checked("code/meetingAnalysisApp.R") and not checked("code/loadPackages.R")
    assert re.search(r'value="data/meetingLongData.csv"[^>]*disabled', html) and "超过 500 MB 上限" in html
    assert "主分析读它：meetingAnalysis.R" in html and "大于 50 MB，没有默认勾选" in html and "被别的脚本调用" in html
    assert not any("datafile" in c and c.endswith(("/4", "/5")) for c in calls)

    r = data_client.post(f"/data/plans/{plan}/links/0/import", data={
        "sources_version": v, "pick": [*MAIN_DATA, *MAIN_SCRIPTS, "Advisers - Replication Codebook.pdf"]})
    assert r.status_code == 200 and "3 个已导入并关联到这份方案，4 个代码和说明书已存进工作区" in r.text
    assert "存成文本 <span class=\"mono\">author/code/meetingAnalysis.R</span>" in r.text


def test_the_new_plan_page_says_how_the_pages_were_picked(data_client, repo, sample_pdf, monkeypatch):
    """data_plan.html follows methods_material: sections, keyword-picked pages (card 0182's `picked`, read defensively), or the old fallback."""
    from .test_data import ingest_quiet_via_conn
    paper = ingest_quiet_via_conn(repo, sample_pdf)
    pieces = [("3", "design text"), ("5", "data text")]

    def page(material: dict) -> str:
        monkeypatch.setattr(plans, "methods_material", lambda conn, paper_id: material)
        return data_client.get(f"/data/plans/new?paper={paper}").text

    html = page({"source": "sections", "headings": ["Research Design"], "pieces": pieces})
    assert "将发送方法和数据章节" in html and "Research Design" in html and "以及结果表格和数据可得性那一页" in html
    html = page({"source": "pages", "picked": "keywords", "headings": [], "pieces": pieces})
    assert "将发送按关键词挑出的研究设计和数据页" in html and "页 3、5" in html and "以及结果表格和数据可得性那一页" in html
    html = page({"source": "pages", "picked": "order", "headings": [], "pieces": pieces})
    assert "没有识别到方法部分，将发送第 2 页到参考文献之前的正文" in html and "以及结果表格" not in html
    html = page({"source": "pages", "headings": [], "pieces": pieces})                      # before card 0182 merges: no `picked`
    assert "没有识别到方法部分" in html


# ---------------------------------------------------------------- review fixes: long lines, read allowance, links, author/ never runs

def test_scanning_a_very_long_line_stays_linear():
    import time
    n = 100_000                                                 # 200 KB on one line
    lines = {"x.py": ["a." * n + "a\n", "import " + "a." * n + "a\n", "sm." * n + "sm\n", "smf." * n + "ols(\n"],
             "x.R": ["a." * n + "a\n", "a::" * n + "a\n", "a$" * n + "a\n", 'read.csv("data/a.csv")\n' * 9000],
             "x.do": ["use " + "a " * n + "\n", ": " * n + "\n", 'use "' * n + "\n"]}
    for path, texts in lines.items():
        for text in texts:
            t = time.perf_counter()
            package.scan(path, text, ["data/a.csv"], ["data/a.csv"])
            assert time.perf_counter() - t < 1.0, (path, text[:20])
    s = package.scan("x.py", 'import pandas as pd\na = pd.read_csv("data/a.csv")\nb = a.b.read_excel("data/b.xlsx")\n'
                     'c = f(z).read_json("data/c.json")\nm = sm.OLS(y, X)\n', ["data/a.csv", "data/b.xlsx", "data/c.json"], [])
    assert s.reads == ["data/a.csv", "data/b.xlsx", "data/c.json"] and s.models == {"sm.OLS": 1}


def test_listing_reads_scripts_only_up_to_the_allowance_and_stops_streaming_when_it_is_spent(monkeypatch):
    seen = {"bytes": 0, "files": set()}

    class Counting(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(10):
                seen["bytes"] += 10_000
                yield b"x <- 1\n" * 1250

    def handler(request: httpx.Request) -> httpx.Response:
        seen["files"].add(request.url.path)
        return httpx.Response(200, stream=Counting())

    monkeypatch.setattr(fetch, "SCAN_BYTES", 250_000)
    listing = fetch.Listing("direct", "x", code=[fetch.Remote(f"code/s{i}.R", f"https://files.example.org/s{i}.R", 100_000, "", "code")
                                                 for i in range(20)])
    auto = fetch.plan_auto(listing, httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen["bytes"] <= 900_000 < 2_000_000 and len(auto.texts) <= 3        # 20 files of 100 KB are 2 MB
    assert len(seen["files"]) < 20                                                # later files were not even requested


def test_a_planted_link_refuses_the_save_before_any_folder_is_made_outside(conn, tmp_path):
    import os
    from research_lab.replication import workspace
    plan = plans.create_blank(conn)
    ws = workspace.workspace_dir(plan)
    ws.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    code = fetch.Remote("code/etc/run.R", "https://x/1", 5, "", "code")
    os.symlink(outside, ws / "author")
    with pytest.raises(fetch.FetchError, match="链接"):
        fetch.save_text(plan, code, "x <- 1")
    assert list(outside.iterdir()) == []
    os.unlink(ws / "author")
    (ws / "author").mkdir()
    os.symlink(outside, ws / "author" / "code")
    with pytest.raises(fetch.FetchError, match="链接"):
        fetch.save_text(plan, code, "x <- 1")
    assert list(outside.iterdir()) == []                                        # no code/, no etc/ beside it


def test_the_authors_code_in_the_workspace_is_never_a_script_to_run(conn):
    from research_lab.replication import workspace
    plan = plans.create_blank(conn)
    ws = workspace.workspace_dir(plan)
    for rel in ("author/code/run.py", "scripts/ok.py"):
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text("print(1)\n")
    assert workspace.script_path(ws, "scripts/ok.py").name == "ok.py"
    with pytest.raises(workspace.WorkspaceError, match="author/"):
        workspace.script_path(ws, "author/code/run.py")
