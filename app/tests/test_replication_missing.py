"""Card 0188 (REPLICATION-v4-DCL.md §3C): 待补数据. The data files the author's scripts read, minus what the plan holds, with why
each is missing and where it may come from; an upload per row that goes through fetch's fences and turns the row 已补.
Nothing here touches the network: the listing is written the way fetch records it, or listed through a stubbed transport."""
from __future__ import annotations

import io
import os
import stat
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from research_lab.replication import datasets, fetch, missing, plans, workspace

CSV = b"country,year,y\nA,2010,1.5\nB,2011,2.5\nC,2012,0.5\n"
SRC = "doi:10.7910/DVN/ABC123"
MAIN_R = '''
source("./code/helper.R")
meetings <- fread("./data/meetingData.csv")
survey   <- haven::read_dta("data/restricted_survey.dta")
carter   <- read_dta("../otherdata/CarterSmith/carter_panel.dta")
big      <- read.csv("data/big_panel.csv")
mid      <- read.csv("data/mid_panel.csv")
models   <- readRDS("data/models.rds")
later    <- read.csv("data/never_picked.csv")
cleaned  <- read.csv("data/cleaned.csv")
fit <- lm(y ~ x, data = meetings)
'''
PREP_R = '''
meetings <- fread("./data/meetingData.csv")
write.csv(meetings, "data/cleaned.csv")
fit <- glm(y ~ x, data = meetings, family = binomial)
'''
APPENDIX_R = '''
meetings <- fread("./data/meetingData.csv")
extra <- read.csv("data/appendix_extra.csv")
fit <- lm(y ~ x, data = extra)
'''
HELPER_R = 'legacy <- read.csv("data/helper_lookup.csv")\n'
LISTING = fetch.Listing("dataverse", "Replication Data for: Advisers", [
    fetch.Remote("data/restricted_survey.dta", "https://x/1", 2048, fetch.RESTRICTED_NOTE, restricted=True),
    fetch.Remote("data/big_panel.csv", "https://x/2", 600 * fetch.MB),
    fetch.Remote("data/mid_panel.csv", "https://x/3", 80 * fetch.MB),
    fetch.Remote("data/models.rds", "https://x/4", 900, "需要 R"),
    fetch.Remote("data/never_picked.csv", "https://x/5", 3000),
    fetch.Remote("data/meetingData.csv", "https://x/6", len(CSV)),
])


def author(plan_id: str, files: dict[str, str]) -> Path:
    ws = workspace.workspace_dir(plan_id)
    for rel, text in files.items():
        p = ws / "author" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return ws


def hold(conn, plan_id: str, tmp_path: Path, name: str = "meetingData.csv") -> str:
    src = tmp_path / "src" / name
    src.parent.mkdir(exist_ok=True)
    src.write_bytes(CSV)
    ds = datasets.import_file(conn, src, original_name=name)
    plans.attach_dataset(conn, plan_id, ds)
    return ds


@pytest.fixture
def plan(conn, tmp_path):
    plan_id = plans.create_blank(conn)
    author(plan_id, {"code/mainAnalysis.R": MAIN_R, "code/prepData.R": PREP_R, "code/appendixRobust.R": APPENDIX_R,
                     "code/helper.R": HELPER_R})
    hold(conn, plan_id, tmp_path)
    fetch.record_listing(plan_id, SRC, LISTING)
    return plan_id


def by_key(rep: missing.Report) -> dict[str, missing.Item]:
    return {i.key: i for i in rep.items}


def upload(conn, plan_id: str, key: str, data: bytes, filename: str, tmp_path: Path, **kw) -> missing.Uploaded:
    src = tmp_path / "incoming" / "x.part"
    src.parent.mkdir(exist_ok=True)
    src.write_bytes(data)
    return missing.upload(conn, plan_id, key, src, filename, **kw)


def zip_of(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ---------------------------------------------------------------- what is missing

def test_the_main_scripts_read_files_minus_what_is_present_and_what_the_scripts_write_themselves(conn, plan):
    rep = missing.report(conn, plan)
    got = by_key(rep)
    assert set(got) == {"data/restricted_survey.dta", "otherdata/CarterSmith/carter_panel.dta", "data/big_panel.csv",
                        "data/mid_panel.csv", "data/models.rds", "data/never_picked.csv", "data/helper_lookup.csv"}
    assert "data/meetingData.csv" not in got                      # attached by its original name
    assert "data/cleaned.csv" not in got                          # prepData.R writes it: an intermediate file
    assert "data/appendix_extra.csv" not in got                   # only the appendix script reads it, and it is not counted
    assert sorted(rep.scripts) == ["code/mainAnalysis.R", "code/prepData.R"] and "code/appendixRobust.R" in rep.all_scripts
    assert rep.more_scripts and all(i.state == "missing" for i in rep.items)
    assert got["data/never_picked.csv"].scripts == ["code/mainAnalysis.R"]
    assert got["data/helper_lookup.csv"].scripts == ["code/mainAnalysis.R"]            # read by a helper the script loads


def test_the_scripts_counted_are_chosen_by_include(conn, plan):
    everything = missing.report(conn, plan, include=missing.ALL)
    assert "data/appendix_extra.csv" in by_key(everything) and "code/appendixRobust.R" in everything.scripts
    assert by_key(everything)["data/appendix_extra.csv"].scripts == ["code/appendixRobust.R"]
    one = missing.report(conn, plan, include=["appendixRobust.R"])
    assert [i.key for i in one.items] == ["data/appendix_extra.csv"] and one.scripts == ["code/appendixRobust.R"]
    named = missing.report(conn, plan, include="code/appendixRobust.R")
    assert named.scripts == ["code/appendixRobust.R"]
    assert missing.report(conn, plan, include=["nope.R"]).unknown == ["nope.R"]
    assert [i.key for i in missing.missing_for(conn, plan, include=["appendixRobust.R"])] == ["data/appendix_extra.csv"]


def test_nothing_is_missing_when_every_data_file_is_held(conn, tmp_path):
    plan_id = plans.create_blank(conn)
    author(plan_id, {"code/main.R": 'd <- fread("./data/meetingData.csv")\nfit <- lm(y ~ x, data = d)\n'})
    hold(conn, plan_id, tmp_path)
    rep = missing.report(conn, plan_id)
    assert rep.items == [] and rep.scripts == ["code/main.R"] and missing.waiting_scripts(conn, plan_id) == {}


def test_a_plan_without_author_code_says_so(conn):
    rep = missing.report(conn, plans.create_blank(conn))
    assert rep.items == [] and "还没有作者的脚本" in rep.note


def test_a_file_in_the_workspace_counts_as_present(conn, plan):
    ws = workspace.workspace_dir(plan)
    (ws / "data").mkdir(exist_ok=True)
    (ws / "data" / "never_picked.csv").write_bytes(CSV)
    (ws / "outputs").mkdir(exist_ok=True)
    (ws / "outputs" / "big_panel.csv").write_bytes(CSV)           # the AI's own output is not data the scripts read
    keys = set(by_key(missing.report(conn, plan)))
    assert "data/never_picked.csv" not in keys and "data/big_panel.csv" in keys


def test_waiting_scripts_names_the_files_each_script_still_needs(conn, plan):
    waiting = missing.waiting_scripts(conn, plan)                  # every script by default: coverage items can come from any
    assert set(waiting["code/appendixRobust.R"]) == {"appendix_extra.csv"}
    assert "never_picked.csv" in waiting["code/mainAnalysis.R"] and "code/prepData.R" not in waiting
    assert "appendix_extra.csv" not in sum(missing.waiting_scripts(conn, plan, include=None).values(), [])


# ---------------------------------------------------------------- why, and where from

def test_each_missing_file_gets_a_reason_and_a_source_hint(conn, plan):
    got = by_key(missing.report(conn, plan, include=missing.ALL))
    r = got["data/restricted_survey.dta"]
    assert r.why == "restricted" and "受限" in r.reason and "申请" in r.hint
    big = got["data/big_panel.csv"]
    assert big.why == "oversize" and big.size == 600 * fetch.MB and "600 MB" in big.reason and "500 MB" in big.reason
    mid = got["data/mid_panel.csv"]
    assert mid.why == "oversize" and "80 MB" in mid.reason and "50 MB" in mid.reason and "List files" in mid.hint
    assert got["data/models.rds"].why == "unsupported" and "需要 R" in got["data/models.rds"].reason
    never = got["data/never_picked.csv"]
    assert never.why == "unfetched" and "还没下载" in never.reason and "data/never_picked.csv" in never.hint
    carter = got["otherdata/CarterSmith/carter_panel.dta"]
    assert carter.why == "other-paper" and "来自其他论文的复现包" in carter.hint and "CarterSmith" in carter.hint
    unknown = got["data/appendix_extra.csv"]
    assert unknown.why == "unknown" and unknown.hint == "来源不明"


@pytest.mark.parametrize("key,folder", [("otherdata/CarterSmith/x.dta", "CarterSmith"), ("data/HorowitzStam2015.dta", "HorowitzStam"),
                                        ("HorowitzStam/data.csv", "HorowitzStam"), ("external/other.csv", "other"),
                                        ("data/MeetingData.csv", None), ("data/meetingData.csv", None),
                                        ("data/ProvincialCovariates.dta", None), ("/Users/JostKertzer/Dropbox/x.csv", None),
                                        ("/Users/jost/otherdata/Smith2/x.csv", "Smith2")])
def test_another_papers_package_is_told_by_the_shape_of_the_path(key, folder):
    assert missing._other_paper(key) == folder


def test_a_link_marked_as_needing_a_request_explains_a_file_nothing_lists(conn, tmp_path):
    plan_id = plans.create_blank(conn)
    plans.save_links(conn, plan_id, [{"url": "https://example.org/request-form", "kind": "other", "access": "request"}])
    author(plan_id, {"code/main.R": 'd <- read.csv("data/private.csv")\nfit <- lm(y ~ x, data = d)\n'})
    item = missing.report(conn, plan_id).items[0]
    assert item.why == "restricted" and "需申请" in item.reason and "example.org/request-form" in item.hint


def test_without_a_recorded_listing_the_reason_says_so(conn):
    plan_id = plans.create_blank(conn)
    author(plan_id, {"code/main.R": 'd <- read.csv("data/private.csv")\nfit <- lm(y ~ x, data = d)\n'})
    item = missing.report(conn, plan_id).items[0]
    assert item.why == "unknown" and item.hint == "来源不明" and "清单" in item.reason


def test_stata_reads_without_an_extension_mean_dta_and_saves_are_written_files(conn):
    plan_id = plans.create_blank(conn)
    author(plan_id, {"do/main.do": 'use "data/panel", clear\nsave "work/step.dta", replace\nuse work/step, clear\n'
                                  'use data/other.dta\nreg y x\n'})
    assert {i.key for i in missing.report(conn, plan_id).items} == {"data/panel.dta", "data/other.dta"}


def test_written_names_across_languages():
    code = {"a.R": 'saveRDS(x, "out/a.rds"); save(x, y, file = "b.RData"); fwrite(d, "c.csv")\n# write.csv(d, "no.csv")\n',
            "b.py": 'df.to_csv("d.csv"); df.to_parquet("sub/e.parquet")\n',
            "c.do": 'save "f.dta", replace\nexport delimited using "g.csv", replace\noutsheet using h.csv\n'}
    assert missing.written_names(code) == {"a.rds", "b.rdata", "c.csv", "d.csv", "e.parquet", "f.dta", "g.csv", "h.csv"}


def test_dataverse_marks_a_restricted_file_and_every_fetch_records_the_listing(conn, plan):
    payload = {"status": "OK", "data": {"latestVersion": {"files": [
        {"label": "private.tab", "restricted": True, "dataFile": {"id": 1, "filename": "private.tab", "filesize": 10,
                                                                    "originalFileName": "private.dta", "originalFileSize": 99}},
        {"label": "open.csv", "restricted": False, "dataFile": {"id": 2, "filename": "open.csv", "filesize": 20}}]}}}
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    listing = fetch.list_source("10.7910/DVN/XYZ", client)
    private, openf = listing.files
    assert private.restricted and not private.fetchable and private.note == fetch.RESTRICTED_NOTE
    assert not openf.restricted and openf.fetchable
    with pytest.raises(fetch.FetchError, match="受限"):
        fetch.pick(listing, ["private.dta"])
    fetch.record_listing(plan, "https://other.example/files", listing)
    recorded = fetch.read_listing(plan)
    assert recorded["private.dta"]["restricted"] is True and recorded["open.csv"]["size"] == 20
    assert recorded["data/big_panel.csv"]["size"] == 600 * fetch.MB          # the first source's entries stay
    fetch.record_listing(plan, SRC, fetch.Listing("dataverse", "t", [fetch.Remote("data/only.csv", "https://x/9", 5)]))
    assert "data/big_panel.csv" not in fetch.read_listing(plan) and "data/only.csv" in fetch.read_listing(plan)
    assert "private.dta" in fetch.read_listing(plan)


# ---------------------------------------------------------------- upload

def test_an_upload_fills_the_row_imports_a_dataset_and_turns_it_filled(conn, plan, tmp_path):
    before = len(conn.execute("SELECT id FROM datasets").fetchall())
    done = upload(conn, plan, "data/never_picked.csv", CSV, "download (3).csv", tmp_path)
    assert len(done.datasets) == 1 and len(conn.execute("SELECT id FROM datasets").fetchall()) == before + 1
    ds = datasets.get(conn, done.datasets[0])
    assert ds["original_name"] == "never_picked.csv" and "人工补交" in ds["note"] and "data/never_picked.csv" in ds["note"]
    assert done.datasets[0] in plans.get(conn, plan)["plan"]["datasets"]                  # attached to the plan
    rep = missing.report(conn, plan, include=missing.ALL)
    assert "data/never_picked.csv" not in {i.key for i in rep.missing}
    item = by_key(rep)["data/never_picked.csv"]
    assert item.state == "filled" and item.filled and item.dataset == done.datasets[0] and item.filled_with == "download (3).csv"
    assert item.scripts == ["code/mainAnalysis.R"] and item.dataset_name
    assert "data/never_picked.csv" not in {i.key for i in missing.missing_for(conn, plan)}
    assert "never_picked.csv" not in missing.waiting_scripts(conn, plan).get("code/mainAnalysis.R", [])
    assert [i.key for i in rep.filled] == ["data/never_picked.csv"]
    # unlinking the dataset puts the row back
    plans.detach_dataset(conn, plan, done.datasets[0])
    assert "data/never_picked.csv" in {i.key for i in missing.missing_for(conn, plan)}


def test_a_file_of_another_format_still_fills_the_row_it_was_given_for(conn, plan, tmp_path):
    import pandas as pd
    buf = io.BytesIO()
    pd.read_csv(io.BytesIO(CSV)).to_excel(buf, index=False)
    done = upload(conn, plan, "data/never_picked.csv", buf.getvalue(), "whatever.xlsx", tmp_path)
    assert datasets.get(conn, done.datasets[0])["original_name"] == "whatever.xlsx"       # the extension differs: its own name
    assert by_key(missing.report(conn, plan))["data/never_picked.csv"].state == "filled"


def test_a_zip_fills_every_file_it_holds_and_the_rows_follow(conn, plan, tmp_path):
    archive = zip_of({"inner/never_picked.csv": CSV, "inner/big_panel.csv": CSV, "inner/notes.txt": b"x"})
    done = upload(conn, plan, "data/never_picked.csv", archive, "bundle.zip", tmp_path)
    assert len(done.datasets) == 2
    got = by_key(missing.report(conn, plan))
    assert got["data/never_picked.csv"].state == "filled" and "data/big_panel.csv" not in {i.key for i in missing.missing_for(conn, plan)}
    assert datasets.get(conn, got["data/never_picked.csv"].dataset)["original_name"] == "never_picked.csv"


def test_a_filename_with_path_parts_cannot_reach_outside(conn, plan, tmp_path):
    for evil in ("../../../evil.csv", "..\\..\\evil.csv", "/etc/evil.csv", "a/../../evil.csv"):
        upload(conn, plan, "data/never_picked.csv", CSV, evil, tmp_path)
        plans.detach_dataset(conn, plan, missing.read_fills(plan)["data/never_picked.csv"]["dataset"])
    assert not (tmp_path / "evil.csv").exists() and not (tmp_path.parent / "evil.csv").exists()
    ds = datasets.get(conn, conn.execute("SELECT id FROM datasets ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert "/" not in ds["original_name"] and ds["original_name"] == "never_picked.csv"
    assert os.listdir(tmp_path / "incoming") == []                                       # the received file was moved, not kept


def test_a_path_that_is_not_a_row_of_the_list_is_refused(conn, plan, tmp_path):
    for key in ("../../etc/passwd.csv", "data/meetingData.csv", "data/cleaned.csv", "", "data/never_picked.CSV"):
        with pytest.raises(missing.MissingError, match="待补清单"):
            upload(conn, plan, key, CSV, "x.csv", tmp_path)
    assert not (workspace.plan_root(plan) / missing.FILLS_FILE).exists()


def test_the_size_cap_and_the_formats_are_fetchs(conn, plan, tmp_path):
    with pytest.raises(missing.MissingError, match="超过上限"):
        upload(conn, plan, "data/never_picked.csv", CSV, "x.csv", tmp_path, max_bytes=10)
    with pytest.raises(missing.MissingError, match="需要 R"):
        upload(conn, plan, "data/models.rds", b"RDS", "models.rds", tmp_path)
    with pytest.raises(missing.MissingError, match="另存为 .xlsx"):
        upload(conn, plan, "data/never_picked.csv", b"x", "old.xls", tmp_path)
    for name in ("run.exe", "notes.txt", "script.R", "noextension"):
        with pytest.raises(missing.MissingError, match="不支持的文件类型"):
            upload(conn, plan, "data/never_picked.csv", b"x", name, tmp_path)
    with pytest.raises(missing.MissingError):                                              # not a table at all
        upload(conn, plan, "data/never_picked.csv", b"\x00\x01\x02" * 50, "x.dta", tmp_path)
    assert fetch.upload_problem("a.CSV") is None and fetch.upload_problem("a.zip") is None


def test_a_zip_with_an_outside_path_or_a_link_is_refused_and_nothing_is_attached(conn, plan, tmp_path):
    before = plans.get(conn, plan)["plan"]["datasets"][:]
    with pytest.raises(missing.MissingError, match="越界"):
        upload(conn, plan, "data/never_picked.csv", zip_of({"../evil.csv": CSV}), "bundle.zip", tmp_path)
    link = io.BytesIO()
    with zipfile.ZipFile(link, "w") as zf:
        info = zipfile.ZipInfo("l.csv")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(missing.MissingError, match="链接"):
        upload(conn, plan, "data/never_picked.csv", link.getvalue(), "bundle.zip", tmp_path)
    assert plans.get(conn, plan)["plan"]["datasets"] == before and not (tmp_path.parent / "evil.csv").exists()


def test_the_dataset_is_copied_into_a_prepared_workspace_and_not_into_an_unprepared_one(conn, plan, tmp_path):
    ws = workspace.workspace_dir(plan)
    done = upload(conn, plan, "data/never_picked.csv", CSV, "never_picked.csv", tmp_path)
    assert done.staged is False and not (ws / "data").exists()                              # prepare makes data/ at the next run
    plans.detach_dataset(conn, plan, done.datasets[0])
    (ws / "data").mkdir()
    (ws / "data" / "keep.txt").write_text("x")
    os.chmod(ws / "data", 0o555)                                                            # as prepare leaves it
    done = upload(conn, plan, "data/never_picked.csv", CSV, "never_picked.csv", tmp_path)
    ds_id = done.datasets[0]
    assert done.staged is True
    assert {p.name for p in (ws / "data").iterdir()} == {"keep.txt", f"{ds_id}.parquet", f"{ds_id}.csv", f"{ds_id}.columns.json"}
    assert (ws / "data" / f"{ds_id}.csv").read_text().startswith("country,year,y")
    assert not os.access(ws / "data" / f"{ds_id}.csv", os.W_OK) and not os.access(ws / "data", os.W_OK)   # read-only again
    assert (ws / "data" / "keep.txt").exists()
    os.chmod(ws / "data", 0o755)


# ---------------------------------------------------------------- the page

def test_the_partial_alone_loads_itself_and_draws_nothing_without_a_plan():
    from research_lab.web.context import templates
    t = templates().env.get_template("partials/_plan_missing.html")
    html = t.render(p={"id": "RP-2026-0004"})
    assert 'hx-get="/data/plans/RP-2026-0004/missing"' in html and 'hx-trigger="load"' in html and 'id="plan-missing"' in html
    assert t.render().strip() == ""


def test_the_panel_lists_the_rows_with_an_upload_each_and_can_widen_to_every_script(client, conn, plan):
    html = client.get(f"/data/plans/{plan}/missing").text
    assert 'id="plan-missing"' in html and "还缺 7 个文件" in html and "按 2 个脚本算" in html
    assert "data/never_picked.csv" in html and "来自其他论文的复现包：CarterSmith" in html and "来源标为受限" in html
    assert html.count('name="file"') == 7 and html.count('hx-post="/data/plans/%s/missing/upload"' % plan) == 7
    assert 'hx-encoding="multipart/form-data"' in html and "appendix_extra" not in html and "等数据" in html
    assert "DS-" not in html and f"scripts=all" in html and "共 3 个脚本" in html
    wide = client.get(f"/data/plans/{plan}/missing?scripts=all").text
    assert "appendix_extra.csv" in wide and "还缺 8 个文件" in wide and "只看主分析脚本" in wide
    assert client.get("/data/plans/RP-2026-9999/missing").status_code == 404


def test_the_upload_route_fills_the_row_and_sends_the_dataset_list_again(client, conn, plan):
    r = client.post(f"/data/plans/{plan}/missing/upload", data={"path": "data/never_picked.csv", "scope": ""},
                    files={"file": ("never_picked.csv", CSV, "text/csv")})
    assert r.status_code == 200 and "还缺 6 个文件" in r.text and "已补" in r.text
    assert 'id="plan-datasets"' in r.text and 'hx-swap-oob="true"' in r.text
    assert missing.read_fills(plan)["data/never_picked.csv"]["file"] == "never_picked.csv"
    assert os.listdir(workspace.plan_root(plan).parent.parent / "cache" / "incoming") == []


def test_the_upload_route_refuses_what_the_list_does_not_hold_and_bad_files(client, conn, plan):
    bad = client.post(f"/data/plans/{plan}/missing/upload", data={"path": "../../x.csv"},
                      files={"file": ("x.csv", CSV, "text/csv")})
    assert bad.status_code == 200 and "不在待补清单里" in bad.text and not missing.read_fills(plan)
    exe = client.post(f"/data/plans/{plan}/missing/upload", data={"path": "data/never_picked.csv"},
                      files={"file": ("virus.exe", b"MZ", "application/octet-stream")})
    assert "不支持的文件类型" in exe.text and not missing.read_fills(plan)
    none = client.post(f"/data/plans/{plan}/missing/upload", data={"path": "data/never_picked.csv"})
    assert "先选一个要上传的文件" in none.text
    assert client.post("/data/plans/RP-2026-9999/missing/upload", data={"path": "x"},
                       files={"file": ("x.csv", CSV)}).status_code == 404


def test_without_htmx_the_upload_redirects_back_to_the_plan(client, conn, plan):
    r = client.post(f"/data/plans/{plan}/missing/upload", data={"path": "data/never_picked.csv"},
                    files={"file": ("never_picked.csv", CSV)}, headers={"HX-Request": "", "Origin": "http://127.0.0.1:4100"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/data/plans/{plan}/detail?msg=") and r.headers["location"].endswith("#missing")


def test_two_datasets_with_one_name_do_not_hide_the_file_from_the_scan(conn, plan, tmp_path):
    hold(conn, plan, tmp_path, "never_picked.csv")
    hold(conn, plan, tmp_path, "never_picked.csv")                  # a repeated name is ambiguous to package.resolve
    assert "data/never_picked.csv" not in {i.key for i in missing.missing_for(conn, plan)}


# ---------------------------------------------------------------- the author's text is not trusted to be small
# The panel is computed on every plan page load and every upload, so adversarial scripts must cost a bounded, small time.

def seconds(fn, *args, **kw):
    start = time.perf_counter()
    result = fn(*args, **kw)
    return result, time.perf_counter() - start


@pytest.mark.parametrize("call", ["write.csv(", "save("])
def test_a_text_of_nothing_but_write_calls_is_read_in_bounded_time(call, conn):
    text = call * (360_000 // len(call))
    names, took = seconds(missing.written_names, {"author/code/a.R": text})
    assert names == set() and took < 1.0, took
    plan_id = plans.create_blank(conn)
    author(plan_id, {"code/a.R": text + '\nd <- read.csv("data/real.csv")\nfit <- lm(y ~ x, data = d)\n'})
    rep, took = seconds(missing.report, conn, plan_id)
    assert [i.key for i in rep.items] == ["data/real.csv"] and took < 1.0, took


def test_the_write_scan_is_charged_by_a_budget_and_still_finds_names_inside_it():
    code = {"a.R": 'write.csv(d, "first.csv")\n' + "write.csv(" * 5000, "b.R": 'write.csv(d, "second.csv")\n'}
    assert missing.written_names(code, budget=10_000) == {"first.csv"}              # a.R spent the allowance, b.R is never read
    assert missing.written_names({"a.R": 'write.csv(d, "first.csv")', "b.R": 'write.csv(d, "second.csv")'}) == {"first.csv", "second.csv"}
    window = 'write.csv(d, file = "x.csv"' + " " * 700 + ', "far.csv")'
    assert missing.written_names({"a.R": window}) == {"x.csv"}                       # a call is read to MAX_ARG characters, no further


def test_twenty_thousand_distinct_reads_stay_linear_and_the_panel_draws_a_capped_list(conn, tmp_path):
    plan_id = plans.create_blank(conn)
    lines = "\n".join(f'x{i} <- read.csv("data/f{i}.csv")' for i in range(20_000))
    author(plan_id, {"code/main.R": lines + "\nfit <- lm(y ~ x, data = x0)\n"})
    rep, took = seconds(missing.report, conn, plan_id)
    assert took < 1.0, took
    assert rep.truncated and rep.total_missing == missing.MAX_FILES and len(rep.items) == missing.MAX_ROWS
    assert rep.hidden == missing.MAX_FILES - missing.MAX_ROWS and rep.scripts == ["code/main.R"]


def test_the_row_cap_counts_what_it_hides_and_the_whole_list_stays_available(conn, tmp_path):
    plan_id = plans.create_blank(conn)
    lines = "\n".join(f'x{i} <- read.csv("data/f{i}.csv")' for i in range(3000))
    author(plan_id, {"code/main.R": lines + "\nfit <- lm(y ~ x, data = x0)\n"})
    fetch.record_listing(plan_id, SRC, fetch.Listing("dataverse", "t", [fetch.Remote(f"data/f{i}.csv", "https://x/1", 100) for i in range(0, 3000, 2)]))
    rep, took = seconds(missing.report, conn, plan_id)
    assert took < 1.0, took
    assert not rep.truncated and rep.total_missing == 3000 and len(rep.items) == missing.MAX_ROWS and rep.hidden == 2700
    every, took = seconds(missing.missing_for, conn, plan_id)
    assert len(every) == 3000 and took < 1.0, took
    waiting, took = seconds(missing.waiting_scripts, conn, plan_id)
    assert len(waiting["code/main.R"]) == 3000 and took < 1.0, took
    assert {i.why for i in every} == {"unfetched", "unknown"}


def test_many_paths_ending_in_one_name_merge_without_a_pairwise_pass(conn):
    plan_id = plans.create_blank(conn)
    reads = "\n".join(f'read.csv("a{i}/b/x.csv")' for i in range(3000)) + '\nread.csv("x.csv")\nfit <- lm(y ~ x)\n'
    author(plan_id, {"code/main.R": reads})
    rep, took = seconds(missing.report, conn, plan_id, limit=None)
    assert took < 1.0, took
    assert rep.total_missing == 3000 and "x.csv" not in {i.key for i in rep.items}   # the bare name joined one of the longer paths
    assert len([i for i in rep.items if i.key == "a0/b/x.csv"]) == 1


def test_the_panel_says_how_many_rows_it_leaves_out(client, conn):
    plan_id = plans.create_blank(conn)
    lines = "\n".join(f'x{i} <- read.csv("data/f{i}.csv")' for i in range(350))
    author(plan_id, {"code/main.R": lines + "\nfit <- lm(y ~ x, data = x0)\n"})
    html = client.get(f"/data/plans/{plan_id}/missing").text
    assert "还缺 350 个文件" in html and "还有 50 个文件没有列出" in html and html.count('name="file"') == missing.MAX_ROWS


def test_the_upload_route_refuses_by_length_and_by_extension_before_writing(client, conn, plan):
    cap = fetch.MAX_MB * fetch.MB
    big = client.post(f"/data/plans/{plan}/missing/upload", data={"path": "data/never_picked.csv"},
                      files={"file": ("x.csv", CSV)}, headers={"Content-Length": str(cap + 10 * fetch.MB)})
    assert big.status_code == 200 and f"超过上限 {fetch.MAX_MB} MB" in big.text
    incoming = workspace.plan_root(plan).parent.parent / "cache" / "incoming"
    exe = client.post(f"/data/plans/{plan}/missing/upload", data={"path": "data/never_picked.csv"},
                      files={"file": ("tool.exe", b"MZ")})
    assert "不支持的文件类型" in exe.text and not (incoming.exists() and os.listdir(incoming))
