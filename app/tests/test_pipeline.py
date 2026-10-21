"""Card 0195: 一键跑完. Every replication step in one background run; only a data file the lab cannot get and a dependency
Leo must install reach him, each naming the tables it holds up. The heavy steps (fetching the package, coverage scan, R
install and run, compare, the report's AI job, deck, export) are replaced here: nothing runs R, touches the network,
calls a model or starts Claude Code."""
from __future__ import annotations

import inspect
import json
import os
import posixpath
import re
import threading
import time
import types
from pathlib import Path

import pytest

from research_lab.ai import dispatch, queue, registry
from research_lab.replication import coverage, datasets, deck, export, fetch, missing, pipeline, plans, renv, rrun
from research_lab.replication import workspace as W

ITEMS = [
    {"label": "Table 1", "scripts": [{"path": "code/main.R"}], "outputs": []},
    {"label": "Table 2", "scripts": [{"path": "code/main.R"}], "outputs": []},
    {"label": "Table 3", "scripts": [{"path": "code/multilevel.R"}], "outputs": []},
    {"label": "Table 4", "scripts": [{"path": "code/multilevel.R"}], "outputs": []},
    {"label": "Figure 1", "scripts": [{"path": "code/descr.R"}], "outputs": ["fig1.pdf"]},
]
SCRIPTS = ["code/main.R", "code/multilevel.R", "code/descr.R"]
SURVEY = missing.Item("data/survey.dta", "survey.dta", "missing", reason="来源标为受限，不能自动下载",
                      hint="需要向作者申请：数据可得性说明里一般写了联系方式", why="restricted", scripts=["code/descr.R"])
# a file only main.R reads: main.R runs fine, so after the run this one holds nothing up and is dropped
LOOKUP = missing.Item("data/lookup.csv", "lookup.csv", "missing", reason="来源不明", hint="来源不明", why="unknown",
                      scripts=["code/main.R"])
INSTALL = {"ok": False, "installed": ["stargazer"], "timed_out": False, "stopped": False,
           "failed": {"nloptr": "需要 cmake", "lme4": "依赖 nloptr 没装上：需要 cmake"}}
RUN = {"state": "done", "todo": SCRIPTS, "tables": 3, "outputs": [], "run_id": "RUN-2026-0042",
       "failed": {"code/multilevel.R": 'Error in lmer(y ~ x + (1 | g)) : could not find function "lmer"',
                  "code/descr.R": "Error in file(file, \"rb\") : cannot open file 'data/survey.dta'"},
       "skipped": ["code/multilevel.R：lmer() 来自没装上的包 lme4（改脚本修不了），没有请 AI。"]}


class Fakes:
    """The heavy functions, recording the order they are called in."""

    def __init__(self, monkeypatch, plan_id: str):
        self.calls: list[str] = []
        self.items: list[dict] = []
        self.r = True
        self.plan_id = plan_id
        self.review: list[bool] = []
        self.job = {"status": "done", "error": None}
        self.route = ("claude", "")
        self.enqueued: list[tuple[str, str]] = []
        m = monkeypatch
        m.setattr(pipeline, "R_WAIT_S", 1)
        m.setattr(pipeline, "R_POLL_S", 0.05)
        m.setattr(pipeline, "REPORT_POLL_S", 0.01)
        m.setattr(coverage, "items", lambda conn, pid: [dict(x) for x in self.items])
        m.setattr(coverage, "rescan", self.rescan)
        m.setattr(missing, "report", self.report)
        m.setattr(fetch, "fetch", self.fetch)
        m.setattr(rrun, "r_main_scripts", lambda conn, pid, ws, *a, **k: list(SCRIPTS))
        m.setattr(rrun, "detect", lambda: rrun.RInfo("/usr/bin/Rscript", "4.4.1", "/r", "/r/bin/exec/R") if self.r else None)
        m.setattr(rrun, "install", self.install)
        m.setattr(rrun, "run", self.run)
        m.setattr(rrun, "package_list", lambda ws, extra=(): types.SimpleNamespace(
            packages={"lme4": ["multilevel.R"], "stargazer": ["main.R"], "nloptr": []}))
        m.setattr(W, "get_run", self.get_run)
        m.setattr(W, "targets_reviewed", lambda conn, pid: False)       # nothing waits for Leo's review
        m.setattr(W, "compare_run", self.compare)
        m.setattr(registry, "load_all", lambda: {pipeline.REPORT_JOB: object()})
        m.setattr(dispatch, "route", lambda conn: self.route)
        m.setattr(queue, "enqueue", self.enqueue)
        m.setattr(queue, "latest_for", lambda conn, kind, target: None)
        m.setattr(dispatch, "dispatch", self.dispatch)
        m.setattr(queue, "get", lambda conn, job_id: dict(self.job, id=job_id))
        m.setattr(deck, "make", self.deck)
        m.setattr(export, "export_folder", self.export)

    def rescan(self, conn, pid):
        self.calls.append("scan")
        self.items = [dict(x) for x in ITEMS]
        return {"added": len(ITEMS)}

    def report(self, conn, pid, include=None, *, limit=None):
        self.calls.append("missing")
        return missing.Report(pid, items=[SURVEY, LOOKUP], scripts=SCRIPTS, all_scripts=SCRIPTS, total_missing=2)

    def fetch(self, *a, **k):
        raise AssertionError("nothing here is fetchable")

    def install(self, conn, pid, *, step=None, progress=None, **k):
        assert rrun.busy(pid), "the install runs under rrun's own lock"
        progress and progress("装 3 个包…")
        self.calls.append("install")
        return dict(INSTALL)

    def run(self, conn, pid, *, step=None, progress=None, **k):
        assert rrun.busy(pid)
        self.calls.append("run")
        return json.loads(json.dumps(RUN))

    def get_run(self, conn, run_id):
        return {"id": run_id, "plan_id": self.plan_id, "status": "done", "workspace": {},
                "result": {"valid": True, "results": {"tables": [{"label": "Table 1: Who convenes"}, {"label": "Table 2"},
                                                                 {"label": "Table 4"}]}}}

    def compare(self, conn, pid, run_id, *, require_review=True):
        self.calls.append("compare")
        self.review.append(require_review)
        return {"run_id": run_id, "summary": {"total": 5, "counts": {"exact": 3, "within": 0, "direction": 0, "mismatch": 0,
                                                                     "missing": 2}}}

    def enqueue(self, conn, kind, target_id, *, force=False):
        self.enqueued.append((kind, target_id))
        return "J-20261019-0001"

    def dispatch(self, job_id):
        self.calls.append("report")
        if self.job["status"] == "done":                 # the job's apply() writes the report text
            out = W.workspace_dir(self.plan_id) / "exports"
            out.mkdir(parents=True, exist_ok=True)
            (out / "ai_text.json").write_text("{}")
        return "ran"

    def deck(self, conn, pid, **k):
        self.calls.append("deck")
        raise RuntimeError("the deck broke")

    def export(self, conn, pid, **k):
        self.calls.append("export")
        return types.SimpleNamespace(project_name="同盟研究", attached=True)


@pytest.fixture
def plan(conn):
    return plans.create_blank(conn)


@pytest.fixture
def fakes(monkeypatch, plan):
    return Fakes(monkeypatch, plan)


def steps(st):
    return {s["key"]: s for s in st["steps"]}


def test_runs_every_step_in_order_and_a_failing_step_does_not_stop_the_rest(conn, plan, fakes):
    st = pipeline.run_now(conn, plan)
    assert fakes.calls == ["scan", "missing", "install", "run", "compare", "report", "deck", "export"]
    assert [s["key"] for s in st["steps"]] == [k for k, _ in pipeline.STEPS]
    assert [s["label"] for s in st["steps"]][:2] == ["取复现包", "扫描表图"]
    by = steps(st)
    assert by["fetch"]["state"] == "skipped"                                   # the plan has no replication link
    assert by["scan"]["state"] == "done" and "5" in by["scan"]["detail"]
    assert by["missing"]["state"] == "blocked" and by["missing"]["detail"] == "还缺 1 个数据文件"   # lookup.csv held nothing up
    assert by["install"]["state"] == "blocked"
    assert by["run"]["state"] == "blocked" and "1/3" in by["run"]["detail"]      # both failures are explained by needs
    assert by["compare"]["state"] == "done" and "3 项复现出来" in by["compare"]["detail"]
    assert by["report"]["state"] == "done"
    assert by["deck"]["state"] == "failed" and by["deck"]["detail"] == "这一步出错了"   # an English traceback is not shown
    assert by["export"]["state"] == "done" and "同盟研究" in by["export"]["detail"]
    assert st["state"] == "needs" and st["run_id"] == "RUN-2026-0042"
    assert all(s["started"] and s["ended"] for s in st["steps"])
    assert not any("（" in s["detail"] for s in st["steps"])


def test_compare_runs_without_the_targets_review(conn, plan, fakes):
    st = pipeline.run_now(conn, plan)
    assert fakes.review == [False] and steps(st)["compare"]["state"] == "done"


def test_compare_run_can_skip_the_review_and_still_refuses_by_default(conn, tmp_path):
    src = tmp_path / "panel.csv"
    src.write_text("unit,year,y\n" + "\n".join(f"{u},{y},{u + y}" for u in range(4) for y in range(5)) + "\n")
    plan_id = plans.create_blank(conn)
    plans.attach_dataset(conn, plan_id, datasets.import_file(conn, src, name="面板数据"))
    plans.save_targets(conn, plan_id, [{"table": "Table 2", "model": "(1)", "term": "a", "value": "0.142", "se": "0.051",
                                        "sig": "***", "n": "40", "page": "12"}])
    ws = W.prepare(conn, plan_id)
    (ws / "outputs").mkdir(exist_ok=True)
    (ws / "outputs" / "results.json").write_text(json.dumps({"tables": [{
        "label": "Table 2", "model": "(1)", "estimator": "OLS", "needs_r": False, "notes": [],
        "rows": [{"term": "a", "estimate": 0.1418, "se": 0.0512, "p": 0.006, "ci": None, "n": 40}]}]}))
    (ws / "scripts" / "01_main.py").write_text("print('ok')")
    run_id = W.record(conn, plan_id, {"id": 0, "mode": "ai", "source_run": None}, ws,
                      {"ok": W.has_results(ws), "text": "", "tokens": 0, "code": ["scripts/01_main.py"]})
    assert not W.targets_reviewed(conn, plan_id)
    with pytest.raises(W.WorkspaceError, match="还没审阅"):
        W.compare_run(conn, plan_id, run_id)
    out = W.compare_run(conn, plan_id, run_id, require_review=False)
    assert out["items"][0]["verdict"] == "exact"
    assert W.get_run(conn, run_id)["workspace"]["comparison"]["run_id"] == run_id


def test_needs_are_only_data_and_dependency_and_name_what_they_hold_up(conn, plan, fakes):
    st = pipeline.run_now(conn, plan)
    needs = st["needs"]
    assert [n["kind"] for n in needs] == ["dependency", "data"]
    dep, data = needs
    assert dep["command"] == "brew install cmake" and set(dep["packages"]) == {"nloptr", "lme4"}
    assert "cmake" in dep["why"] and "（" not in dep["why"]
    assert dep["affects"] == ["Table 3"]          # Table 4 was printed before multilevel.R stopped; main.R's tables ran
    assert data["key"] == "data/survey.dta" and data["upload"] is True and data["title"] == "survey.dta"
    assert data["affects"] == ["Figure 1"] and data["where"].startswith("需要向作者申请")
    assert "data/lookup.csv" not in {n.get("key") for n in needs}   # only main.R read it, and main.R ran


def test_no_r_is_a_dependency_need_and_the_independent_steps_still_run(conn, plan, fakes):
    fakes.r = False
    st = pipeline.run_now(conn, plan)
    by = steps(st)
    assert "install" not in fakes.calls and "run" not in fakes.calls
    assert by["install"]["state"] == "blocked" and by["run"]["state"] == "blocked"
    assert by["compare"]["state"] == "blocked"
    assert by["report"]["state"] == "done" and by["export"]["state"] == "done"
    r = next(n for n in st["needs"] if n["kind"] == "dependency")
    assert r["command"] == "brew install r"
    assert r["affects"] == ["Table 1", "Table 2", "Table 3", "Table 4", "Figure 1"]
    data = next(n for n in st["needs"] if n["kind"] == "data" and n["key"] == "data/survey.dta")
    assert data["affects"] == ["Figure 1"]


def test_existing_list_is_not_rescanned_and_listed_files_are_fetched_first(conn, plan, fakes, monkeypatch):
    fakes.items = [dict(x) for x in ITEMS]
    unfetched = missing.Item("data/meet.csv", "meet.csv", "missing", reason="复现包里有，还没下载", why="unfetched", size=900,
                             scripts=["code/main.R"])
    fetched: list = []
    reports = iter([[unfetched, SURVEY], [SURVEY]])
    monkeypatch.setattr(missing, "report", lambda conn, pid, include=None, *, limit=None:
                        missing.Report(pid, items=next(reports), scripts=SCRIPTS, total_missing=1))
    monkeypatch.setattr(fetch, "read_listing", lambda pid: {"replication/data/meet.csv": {"size": 900, "src": "doi:10.7910/DVN/X"}})
    monkeypatch.setattr(fetch, "fetch", lambda conn, src, picks, plan_id=None: fetched.append((src, picks)) or
                        [fetch.Imported("meet.csv", dataset="DS-2026-0001")])
    st = pipeline.run_now(conn, plan)
    by = steps(st)
    assert by["scan"]["state"] == "skipped" and "scan" not in fakes.calls
    assert fetched == [("doi:10.7910/DVN/X", ["replication/data/meet.csv"])]
    assert "自动取回 1 个" in by["missing"]["detail"]
    assert [n["key"] for n in st["needs"] if n["kind"] == "data"] == ["data/survey.dta"]


# ---------------------------------------------------------------- 取复现包

SRC = "https://doi.org/10.7910/DVN/PKG"


def remote(name: str, kind: str, size: int = 400) -> fetch.Remote:
    return fetch.Remote(name, f"https://dataverse.example/{name}", size, kind=kind)


class Package:
    """A replication package as fetch lists it, and a download that saves code as text the way fetch does."""

    def __init__(self, monkeypatch, plan_id: str):
        self.plan_id = plan_id
        self.listing = fetch.Listing("dataverse", "Replication Data", files=[remote("data/meetings.csv", "data")],
                                     code=[remote("code/main.R", "code"), remote("code/appendix.R", "code"),
                                           remote("otherdata/old.R", "code"), remote("code/huge.R", "code", 5 * fetch.MB)],
                                     docs=[remote("README.md", "doc"), remote("archive/README_old.txt", "doc")])
        self.downloads: list[list[str]] = []
        self.auto_calls = 0
        monkeypatch.setattr(fetch, "_client", lambda http: types.SimpleNamespace(close=lambda: None))
        monkeypatch.setattr(fetch, "list_source", lambda src, http=None, cap=None: self.listing)
        monkeypatch.setattr(fetch, "plan_auto", self.plan_auto)
        monkeypatch.setattr(fetch, "_download_all", self.download)

    def plan_auto(self, listing, http=None, *, main=None):
        self.auto_calls += 1
        by = {r.name: r for r in listing.everything}
        return fetch.AutoPlan(listing, data=[by["data/meetings.csv"]], code=[by["code/main.R"]], docs=[by["README.md"]],
                              texts={"code/main.R": "x <- 1\n"})

    def download(self, conn, src, chosen, plan_id, client, max_bytes, texts=None):
        assert src == SRC and plan_id == self.plan_id
        self.downloads.append([r.name for r in chosen])
        out = []
        for r in chosen:
            if r.kind == "data":
                out.append(fetch.Imported(r.name, dataset="DS-2026-0009"))
                continue
            path = W.workspace_dir(plan_id) / fetch._author_path(r.name, False)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text((texts or {}).get(r.name, "# text\n"))
            out.append(fetch.Imported(r.name, saved=str(path)))
        return out


def test_fetch_takes_the_package_and_every_author_script_then_rescans(conn, plan, fakes, monkeypatch):
    plans.save_links(conn, plan, [{"url": SRC, "kind": "dataverse", "access": "public"},
                                  {"url": "https://example.org/restricted", "kind": "other", "access": "request"}])
    pkg = Package(monkeypatch, plan)
    fakes.items = [dict(x) for x in ITEMS]                      # a list already exists …
    st = pipeline.run_now(conn, plan)
    by = steps(st)
    assert pkg.downloads == [["data/meetings.csv", "code/main.R", "README.md", "code/appendix.R", "otherdata/old.R"]]
    assert by["fetch"]["state"] == "done" and by["fetch"]["detail"] == "取回 3 个脚本、1 份说明、1 个数据集"
    assert "scan" in fakes.calls and by["scan"]["state"] == "done"          # … and is scanned again for the new scripts
    assert "code/huge.R" not in pkg.downloads[0]                           # larger than a script the lab reads
    assert (W.workspace_dir(plan) / "author" / "code" / "appendix.R").is_file()

    pkg.listing.code.append(remote("code/robust.R", "code"))               # the next run: only what is not saved yet
    fakes.calls.clear()
    st = pipeline.run_now(conn, plan)
    assert pkg.auto_calls == 1 and pkg.downloads[1] == ["code/robust.R"]
    assert steps(st)["fetch"]["detail"] == "取回 1 个脚本" and "scan" in fakes.calls

    fakes.calls.clear()
    st = pipeline.run_now(conn, plan)                                       # nothing new: no download, no rescan
    assert len(pkg.downloads) == 2 and steps(st)["fetch"]["state"] == "skipped" and "scan" not in fakes.calls


def test_unreadable_code_still_brings_the_scripts_without_a_data_pick(conn, plan, fakes, monkeypatch):
    plans.save_links(conn, plan, [{"url": SRC, "kind": "dataverse", "access": "public"}])
    pkg = Package(monkeypatch, plan)
    monkeypatch.setattr(fetch, "plan_auto", lambda listing, http=None, *, main=None: (_ for _ in ()).throw(
        fetch.FetchError("作者代码读不了")))
    st = pipeline.run_now(conn, plan)
    assert pkg.downloads == [["code/main.R", "code/appendix.R", "otherdata/old.R", "README.md"]]
    assert steps(st)["fetch"]["state"] == "done"


def test_a_source_that_cannot_be_listed_fails_the_fetch_but_not_the_run(conn, plan, fakes, monkeypatch):
    plans.save_links(conn, plan, [{"url": SRC, "kind": "dataverse", "access": "public"}])
    monkeypatch.setattr(fetch, "list_source", lambda src, http=None, cap=None: (_ for _ in ()).throw(
        fetch.FetchError("Dataverse 没有回应，稍后再试")))
    monkeypatch.setattr(fetch, "_client", lambda http: types.SimpleNamespace(close=lambda: None))
    st = pipeline.run_now(conn, plan)
    by = steps(st)
    assert by["fetch"]["state"] == "failed" and "没有回应" in by["fetch"]["detail"]
    assert by["scan"]["state"] == "done" and by["export"]["state"] == "done"


# ---------------------------------------------------------------- 报告: the AI job

def test_report_queues_the_job_and_waits_for_its_text(conn, plan, fakes):
    st = pipeline.run_now(conn, plan)
    assert fakes.enqueued == [(pipeline.REPORT_JOB, plan)]
    assert steps(st)["report"]["state"] == "done" and steps(st)["report"]["detail"] == "报告写好了"


def test_report_is_skipped_until_the_job_exists(conn, plan, fakes, monkeypatch):
    monkeypatch.setattr(registry, "load_all", lambda: {})
    st = pipeline.run_now(conn, plan)
    assert steps(st)["report"]["state"] == "skipped" and not fakes.enqueued


@pytest.mark.parametrize("why, text", [("daily limit of 20 Claude Code runs reached", "今天的助手用量到上限了"),
                                       ("AI 已暂停", "助手已暂停"), ("Claude Code CLI not found", "助手现在用不了")])
def test_report_fails_plainly_when_the_engine_cannot_run(conn, plan, fakes, why, text):
    fakes.route = ("queue", why)
    st = pipeline.run_now(conn, plan)
    assert steps(st)["report"]["state"] == "failed" and steps(st)["report"]["detail"] == text
    assert not fakes.enqueued                                            # nothing left queued to spend later


@pytest.mark.parametrize("job, text", [({"status": "failed", "error": "ValidationError: …"}, "写报告时出错了"),
                                       ({"status": "queued", "error": "usage limit, resting"}, "助手暂时用不了，稍后会接着写"),
                                       ({"status": "claimed", "error": None}, "五分钟内没写完，助手会接着写")])
def test_report_fails_plainly_when_the_job_does_not_finish(conn, plan, fakes, monkeypatch, job, text):
    monkeypatch.setattr(pipeline, "REPORT_WAIT_S", 0.05)
    fakes.job = job
    st = pipeline.run_now(conn, plan)
    assert steps(st)["report"]["state"] == "failed" and steps(st)["report"]["detail"] == text
    assert steps(st)["deck"]["state"] == "failed" and steps(st)["export"]["state"] == "done"


# ---------------------------------------------------------------- one run at a time, the state file

def test_state_persists_to_pipeline_json(conn, plan, fakes):
    pipeline.run_now(conn, plan)
    path = W.plan_root(plan) / "logs" / "pipeline.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["state"] == "needs" and saved["plan_id"] == plan
    assert [s["key"] for s in saved["steps"]] == [k for k, _ in pipeline.STEPS]
    st = pipeline.state(plan)
    assert st["running"] is False and st["needs"] == saved["needs"]


def test_a_second_start_is_refused_while_one_runs(conn, plan, fakes, monkeypatch):
    gate, entered = threading.Event(), threading.Event()

    def slow_items(c, pid):
        entered.set()
        assert gate.wait(10)
        return []
    monkeypatch.setattr(coverage, "items", slow_items)
    monkeypatch.setattr(coverage, "rescan", lambda c, pid: (_ for _ in ()).throw(coverage.CoverageError("论文里没有找到带标题的表或图。")))
    assert pipeline.start(conn, plan) is True
    try:
        assert entered.wait(10)
        assert pipeline.running(plan)
        assert pipeline.start(conn, plan) is False
        with pytest.raises(pipeline.PipelineError):
            pipeline.run_now(conn, plan)
        st = pipeline.state(plan)
        assert st["running"] is True and st["current"] == "scan"
        assert steps(st)["scan"]["state"] == "running" and steps(st)["missing"]["state"] == "waiting"
    finally:
        gate.set()
        assert pipeline.join(plan, 20)
    st = pipeline.state(plan)
    assert st["running"] is False and st["state"] == "needs"
    assert steps(st)["scan"]["state"] == "failed" and "没有找到" in steps(st)["scan"]["detail"]
    assert pipeline.start(conn, plan) is True                    # the lock went with the thread
    assert pipeline.join(plan, 20)


def test_r_busy_elsewhere_is_waited_for_then_reported(conn, plan, fakes):
    ws = W.workspace_dir(plan)
    ws.mkdir(parents=True, exist_ok=True)
    held = rrun.Step(rrun.ws_of(plan), "install").__enter__()       # a click on 复现工作区 holds R
    try:
        st = pipeline.run_now(conn, plan)
    finally:
        held.__exit__(None, None, None)
    by = steps(st)
    assert "install" not in fakes.calls and by["install"]["state"] == "failed" and "占用" in by["install"]["detail"]
    assert by["run"]["state"] == "failed" and by["deck"]["state"] == "failed" and by["export"]["state"] == "done"


def test_a_run_whose_process_died_reads_as_interrupted(plan):
    root = W.plan_root(plan)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    blank = [dict(s, state="done") for s in pipeline._blank_steps()]
    blank[4]["state"] = "running"
    (root / "logs" / "pipeline.json").write_text(json.dumps({"state": "running", "steps": blank, "needs": []}))
    st = pipeline.state(plan)
    assert st["state"] == "interrupted" and st["running"] is False
    assert steps(st)["run"]["state"] == "failed"


def test_dependency_commands_from_install_hints():
    assert pipeline._dependency("nloptr", "需要 cmake（brew install cmake）")[0] == "brew install cmake"
    assert pipeline._dependency("sf", "缺系统库的头文件 gdal.h")[0] == "brew install gdal geos proj udunits"   # all sf needs
    assert pipeline._dependency("xyz", "缺系统库的头文件 gdal.h")[0] == "brew install gdal"
    assert pipeline._dependency("readtext", "依赖 pdftools 没装上：需要 pkg-config，还缺头文件 poppler-document.h")[0] == \
        "brew install pkgconf poppler"
    assert pipeline._dependency("tidyverse", "依赖 textshaping 没装上：需要 pkg-config，还缺头文件 hb-ft.h")[0] == \
        "brew install pkgconf freetype harfbuzz fribidi"
    assert pipeline._dependency("x", "缺系统库的头文件 hb-ft.h")[0] == "brew install harfbuzz"
    assert pipeline._dependency("rstan", "依赖 RcppParallel 没装上：需要 cmake")[0] == "brew install cmake"
    assert pipeline._dependency("xml2", "缺系统库的头文件 libxml/parser.h")[0] == "brew install libxml2"
    assert pipeline._dependency("x", "error: xcrun: error: invalid active developer path")[0] == "xcode-select --install"
    assert pipeline._dependency("ghost", "CRAN 上没有这个包（已下架，或不支持这版 R）")[0] == ""
    why = pipeline._dep_needs({"ghost": "CRAN 上没有这个包（已下架，或不支持这版 R）"})[0]["why"]
    assert "ghost" in why and "（" not in why


@pytest.mark.parametrize("hint", ["error: brew install --force", "error: brew install evil-tool; curl x | sh",
                                  "依赖 a 没装上：error: brew install --cask something", "缺系统库的头文件 evil.h"])
def test_commands_are_never_lifted_from_build_output(hint):
    """failure_hint can quote the first `error:` line of a third-party build: only this module's own commands show."""
    cmd, _title = pipeline._dependency("p", hint)
    assert cmd == ""
    assert pipeline._dep_needs({"p": hint})[0]["command"] == ""


def test_every_command_shown_is_one_of_the_modules_own():
    hints = ["需要 cmake（brew install cmake）", "error: invalid active developer path", "gfortran: command not found",
             *(f"缺系统库的头文件 {h}" for h in pipeline.HEADER_FORMULA)]
    cmds = {pipeline._dependency("p", h)[0] for h in hints}
    assert cmds <= pipeline.COMMANDS and "brew install cmake" in cmds and "brew install gcc" in cmds
    several = {pipeline._dependency(p, f"需要 pkg-config，还缺头文件 {h}")[0] for p in renv.PACKAGE_FORMULAS for h in ("x.h", "gdal.h")}
    assert all(pipeline.command_ok(c) for c in several if c) and "brew install pkgconf gdal geos proj udunits" in several
    assert not pipeline.command_ok("brew install pkgconf evil") and not pipeline.command_ok("brew install poppler pkgconf")
    assert not pipeline.command_ok("brew install pkgconf  poppler") and not pipeline.command_ok("brew install")


# what rrun.hint_of says for the real log of test_replication_rrun.REAL_LOG, pkg-config missing
REAL_HINTS = {
    "readtext": "依赖 pdftools 没装上：需要 pkg-config，还缺头文件 poppler-document.h",
    "tidyverse": "依赖 textshaping 没装上：需要 pkg-config，还缺头文件 hb-ft.h",
    "nloptr": "需要 cmake", "lme4": "依赖 nloptr 没装上：需要 cmake", "glmmTMB": "依赖 nloptr 没装上：需要 cmake",
    "rstan": "依赖 RcppParallel 没装上：需要 cmake", "rstanarm": "依赖 nloptr 没装上：需要 cmake",
    "crfsuite": "已从 CRAN 下架，存档里的 0.4.2 版也没装上：error: no matching function for call to 'crf1d_create'",
    "NPC": "CRAN 上没有这个包，存档里也没有", "apsrtable": "CRAN 上没有这个包，存档里也没有",
    "zoo": "没有装完",
}


def test_one_need_per_root_cause_names_the_root_and_what_depends_on_it(monkeypatch):
    monkeypatch.setattr(renv, "missing_formulas", lambda formulas: [f for f in formulas if f != "poppler" and f != "freetype"])
    needs = {n["title"]: n for n in pipeline._dep_needs(REAL_HINTS)}
    cmake = needs["安装 cmake"]
    assert cmake["command"] == "brew install cmake" and set(cmake["packages"]) == {"nloptr", "lme4", "glmmTMB", "rstan", "rstanarm"}
    assert cmake["why"].startswith("nloptr、RcppParallel 需要 cmake；") and "依赖它们" in cmake["why"]
    pdf = needs["安装 pkgconf"]                                       # poppler is there: only pkg-config is missing
    assert pdf["command"] == "brew install pkgconf" and pdf["packages"] == ["readtext"]
    assert pdf["why"] == "pdftools 需要 pkg-config，还缺头文件 poppler-document.h；readtext 依赖它"
    fonts = needs["安装 pkgconf、harfbuzz、fribidi"]
    assert fonts["command"] == "brew install pkgconf harfbuzz fribidi" and fonts["packages"] == ["tidyverse"]
    gone = needs["CRAN 上找不到的包"]
    assert gone["command"] == "" and set(gone["packages"]) == {"NPC", "apsrtable"}
    crf = needs["crfsuite 装不上"]
    assert crf["command"] == "" and crf["packages"] == ["crfsuite"] and "0.4.2" in crf["why"]
    assert len(needs) == 5 and all("（" not in n["why"] and pipeline.command_ok(n["command"]) or not n["command"]
                                   for n in needs.values())
    order = [n["command"] != "" for n in pipeline._dep_needs(REAL_HINTS)]
    assert order == sorted(order, reverse=True)                       # what one command fixes comes first


def test_a_command_lists_every_formula_when_the_mac_has_them_all(monkeypatch):
    monkeypatch.setattr(renv, "missing_formulas", lambda formulas: [])
    need = pipeline._dep_needs({"readtext": REAL_HINTS["readtext"]})[0]
    assert need["command"] == "brew install pkgconf poppler"
    monkeypatch.setattr(renv, "missing_formulas", lambda formulas: (_ for _ in ()).throw(OSError("no")))
    assert pipeline._dep_needs({"readtext": REAL_HINTS["readtext"]})[0]["command"] == "brew install pkgconf poppler"


def test_archived_packages_from_the_install_are_named_in_the_step(conn, plan, fakes, monkeypatch):
    monkeypatch.setattr(rrun, "install", lambda conn, pid, **k: dict(INSTALL, ok=True, failed={}, archived={"ri": "0.9"}))
    st = pipeline.run_now(conn, plan)
    assert steps(st)["install"]["detail"] == "1 个 R 包就绪，1 个已下架的从 CRAN 存档装上"


def test_needs_are_capped_and_names_cut(conn, plan, fakes, monkeypatch):
    huge = "x" * 1_000_000
    failed = {f"p{i}": f"缺系统库的头文件 h{i}.h" for i in range(2_000)}      # 2000 headers no formula covers
    failed[huge] = "需要 cmake（brew install cmake）"
    failed.update({f"c{i}": "需要 cmake（brew install cmake）" for i in range(40)})
    monkeypatch.setattr(rrun, "install", lambda conn, pid, **k: dict(INSTALL, failed=failed))
    monkeypatch.setattr(rrun, "run", lambda conn, pid, **k: dict(json.loads(json.dumps(RUN)), failed={
        "code/multilevel.R": "Error: there is no package called ‘c0’ " + " ".join(f"p{i}" for i in range(400))}))
    st = pipeline.run_now(conn, plan)
    path = W.plan_root(plan) / "logs" / "pipeline.json"
    assert path.stat().st_size < pipeline.STATE_CHARS
    deps = [n for n in st["needs"] if n["kind"] == "dependency"]
    assert len(deps) <= pipeline.MAX_DEP_NEEDS and st["needs_more"] > 0
    cmake = next(n for n in deps if n["command"] == "brew install cmake")
    assert len(cmake["packages"]) == pipeline.MAX_PACKAGES and cmake["packages_more"] == 41 - pipeline.MAX_PACKAGES
    assert all(len(p) <= pipeline.NAME_CHARS for n in deps for p in n["packages"])
    assert all(len(n["title"]) <= pipeline.NAME_CHARS and len(n["why"]) <= pipeline.DETAIL_CHARS for n in deps)
    again = pipeline.state(plan)
    assert len(again["steps"]) == len(pipeline.STEPS) and again["needs"] == st["needs"]


def test_a_state_too_large_to_read_back_is_slimmed_not_cut(plan, monkeypatch):
    monkeypatch.setattr(pipeline, "STATE_CHARS", 4_000)
    run = pipeline._Run(plan, pipeline._root(plan))
    need = {"kind": "data", "uid": "u", "title": "f.csv", "why": "y" * 100, "affects": ["Table 1"] * 12, "affects_more": 0,
            "upload": True, "scripts": []}
    run.st["needs"] = [dict(need, key=f"data/{'k' * 200}{i}.csv") for i in range(30)]
    run.step("scan")["detail"] = "d" * 140
    run.save()
    st = pipeline.state(plan)
    assert len(st["steps"]) == len(pipeline.STEPS) and st["steps"][1]["detail"] == ""    # read back, details dropped
    assert st["needs"] == [] and st["needs_more"] == 30
    run.st["needs"] = [dict(need, key=f"data/{'k' * 200}{i}.csv") for i in range(6)]
    run.save()
    st = pipeline.state(plan)                                               # the needs fit once explanations go
    assert len(st["needs"]) == 6 and "why" not in st["needs"][0] and st["needs"][0]["upload"] is True
    run.st["needs"] = [dict(need, key="data/f0.csv")]
    run.save()
    st = pipeline.state(plan)
    assert st["needs"][0]["why"] == "y" * 100 and st["steps"][1]["detail"] == "d" * 140


def test_running_is_decided_by_the_lock_alone(plan):
    root = pipeline._root(plan)
    hold = pipeline._Hold(root)
    assert hold.take()
    try:
        (root / "logs" / "pipeline.json").write_text('{"state": "done", "steps": [')          # unreadable mid-run
        st = pipeline.state(plan)
        assert st["running"] is True and st["state"] == "running" and len(st["steps"]) == len(pipeline.STEPS)
        (root / "logs" / "pipeline.json").write_text(json.dumps({"state": "needs", "steps": [], "needs": [{"kind": "data"}]}))
        st = pipeline.state(plan)                                              # the last run's file, a new run's lock
        assert st["running"] is True and st["needs"] == [] and st["steps"][0]["state"] == "waiting"
    finally:
        hold.release()
    assert pipeline.state(plan)["running"] is False


def test_hostile_text_is_read_in_linear_time():
    """Install logs and R error output come from outside: megabyte-sized hostile input stays under a second."""
    nasty = [("（" * 400_000) + "x", "brew install " + "a" * 1_000_000, "头文件 " + "/" * 1_000_000,
             'could not find function "' * 50_000, "‘" * 1_000_000, ("lme4" + "." * 3) * 250_000]
    pkgs = {f"pkg{i}" for i in range(300)} | {"lme4", "data.table"}
    for text in nasty:
        t0 = time.monotonic()
        pipeline.plain(text)
        pipeline._dependency("lme4", text)
        pipeline._mentions(text, pkgs)
        pipeline._dep_needs({"lme4": text, "nloptr": text})
        assert time.monotonic() - t0 < 1.0


# ---------------------------------------------------------------- Stop, sizes, hostile structures

def test_stopping_the_install_ends_the_run(conn, plan, fakes, monkeypatch):
    monkeypatch.setattr(rrun, "install", lambda conn, pid, **k: dict(INSTALL, stopped=True, failed={}))
    st = pipeline.run_now(conn, plan)
    by = steps(st)
    assert by["install"]["state"] == "failed" and "停止" in by["install"]["detail"]
    for key in ("run", "compare", "report", "deck", "export"):
        assert by[key]["state"] == "skipped" and by[key]["detail"] == "已停止"
    assert "run" not in fakes.calls and "export" not in fakes.calls and st["state"] == "stopped"


def test_stopping_the_run_ends_the_run(conn, plan, fakes, monkeypatch):
    monkeypatch.setattr(rrun, "run", lambda conn, pid, **k: dict(json.loads(json.dumps(RUN)), state="stopped"))
    st = pipeline.run_now(conn, plan)
    by = steps(st)
    assert by["run"]["state"] == "failed" and [by[k]["state"] for k in ("compare", "report", "deck", "export")] == ["skipped"] * 4
    assert not fakes.enqueued and "export" not in fakes.calls and st["state"] == "stopped"


def test_unsized_scripts_count_as_the_largest_the_lab_reads(conn, plan, fakes, monkeypatch):
    plans.save_links(conn, plan, [{"url": SRC, "kind": "dataverse", "access": "public"}])
    pkg = Package(monkeypatch, plan)
    monkeypatch.setattr(fetch, "plan_auto", lambda listing, http=None, *, main=None: fetch.AutoPlan(listing))
    pkg.listing.code = [remote(f"code/s{i}.R", "code", None) for i in range(10)]
    pkg.listing.docs = [remote(f"doc{i}/README.pdf", "doc", None) for i in range(5)]
    pipeline.run_now(conn, plan)
    got = pkg.downloads[0]
    n_code = sum(1 for x in got if x.startswith("code/s"))
    assert n_code == pipeline.FETCH_CODE_BYTES // W.AUTHOR_FILE                 # 8 MB / 2 MB: four unsized scripts
    assert sum(1 for x in got if x.endswith(".pdf")) == pipeline.FETCH_DOC_BYTES // fetch.PDF_MAX


def test_unsized_listed_data_counts_as_the_download_cap(conn, plan, fakes, monkeypatch):
    items = [missing.Item(f"data/u{i}.csv", f"u{i}.csv", "missing", reason="复现包里有，还没下载", why="unfetched", size=None,
                          scripts=["code/main.R"]) for i in range(5)]
    monkeypatch.setattr(fetch, "read_listing", lambda pid: {f"data/u{i}.csv": {"size": None, "src": SRC} for i in range(5)})
    picked: list = []
    monkeypatch.setattr(fetch, "fetch", lambda conn, src, picks, plan_id=None: picked.extend(picks) or [])
    assert pipeline._fetch_listed(conn, plan, items) == 0
    assert len(picked) == pipeline.FETCH_BYTES // (fetch.MAX_MB * fetch.MB)


def mutual_scripts(plan_id: str, n: int = 300) -> list[str]:
    names = [f"code/s{i}.R" for i in range(n)]
    body = "".join(f'source("{posixpath.basename(x)}")\n' for x in names) + "library(lme4)\n"
    for x in names:
        p = W.workspace_dir(plan_id) / "author" / x
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return names


def test_three_hundred_mutually_sourcing_scripts_stay_under_a_second(conn, plan):
    names = mutual_scripts(plan)
    ctx = pipeline._Ctx(plan, items=[{"label": f"Table {i + 1}", "scripts": [{"path": x}]} for i, x in enumerate(names)],
                        r_scripts=names[:60], loaders={"lme4": ["s150.R"]})
    t0 = time.monotonic()
    labels, more = ctx.affects(names[:1])
    up = ctx.loading({"lme4"})
    labels_all, _ = ctx.affects(names)
    assert time.monotonic() - t0 < 1.0
    assert len(labels) + more == 200                       # rtext reads at most 200 source() paths per script
    assert up == names[:60] and len(labels_all) == pipeline.MAX_AFFECTS


def test_three_hundred_failed_scripts_and_packages_stay_under_a_second(plan):
    scripts = [f"code/f{i}.R" for i in range(300)]
    pkgs = [f"pkg{i}" for i in range(300)]
    text = "Error in library(x) : " + " ".join(pkgs) + " could not find function \"lmer\" " + "z" * 3000
    ctx = pipeline._Ctx(plan, r_scripts=scripts[:60], loaders={p: [f"f{i}.R"] for i, p in enumerate(pkgs)})
    ctx.dep_needs = [{"kind": "dependency", "title": "t", "why": "w", "affects": [], "affects_more": 0, "command": "",
                      "packages": pkgs[i::20]} for i in range(20)]
    ctx.outcome = {"todo": scripts, "failed": {s: text for s in scripts}, "skipped": [f"{s}：{text}" for s in scripts]}
    t0 = time.monotonic()
    explained = pipeline._explained(ctx, ctx.outcome["failed"], ctx.outcome["skipped"])
    dep, _data = pipeline._final_needs(None, ctx)
    assert time.monotonic() - t0 < 1.0
    assert explained == set(scripts) and len(dep) == 20


def test_a_stale_report_text_does_not_count(conn, plan, fakes, monkeypatch):
    out = W.workspace_dir(plan) / "exports"
    out.mkdir(parents=True, exist_ok=True)
    old = out / "ai_text.json"
    old.write_text("{}")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    monkeypatch.setattr(fakes, "dispatch", lambda job_id: "ran")             # the job ends without writing a new one
    monkeypatch.setattr(dispatch, "dispatch", fakes.dispatch)
    st = pipeline.run_now(conn, plan)
    assert steps(st)["report"]["state"] == "failed" and steps(st)["report"]["detail"] == "报告没有写出来"


def test_compare_without_targets_says_so(conn, plan, fakes, monkeypatch):
    monkeypatch.setattr(W, "compare_run", lambda conn, pid, run_id, require_review=True: {"summary": {"total": 0, "counts": {}}})
    st = pipeline.run_now(conn, plan)
    assert steps(st)["compare"]["detail"] == "方案里还没有复现目标，结果已存"


def test_fetch_helpers_the_pipeline_relies_on_keep_their_signatures():
    """pipeline.py calls three of fetch's private helpers (it does not own fetch.py): a change there must show here."""
    assert list(inspect.signature(fetch._client).parameters) == ["http"]
    assert list(inspect.signature(fetch._download_all).parameters) == ["conn", "src", "chosen", "plan_id", "client",
                                                                       "max_bytes", "texts"]
    assert list(inspect.signature(fetch._author_path).parameters) == ["name", "text_of_pdf"]
    assert list(inspect.signature(missing._listed).parameters) == ["key", "by_base"]
    assert list(inspect.signature(missing._by_base).parameters) == ["listing"]


# ---------------------------------------------------------------- the routes and the block

def test_run_all_route_starts_and_redirects(client, conn, plan, monkeypatch):
    started = []
    monkeypatch.setattr(pipeline, "start", lambda c, pid: started.append(pid) or len(started) == 1)
    r = client.post(f"/data/plans/{plan}/run-all")
    assert r.status_code == 204 and r.headers["HX-Redirect"].startswith(f"/data/plans/{plan}?msg=")
    assert r.headers["HX-Redirect"].endswith("#run-all")
    r = client.post(f"/data/plans/{plan}/run-all")
    assert r.status_code == 204 and "msg=" in r.headers["HX-Redirect"]
    monkeypatch.setattr(pipeline, "start", lambda c, pid: (_ for _ in ()).throw(pipeline.PipelineError("这份方案的助手会话正在运行")))
    r = client.post(f"/data/plans/{plan}/run-all")
    assert r.status_code == 204 and "err=" in r.headers["HX-Redirect"]
    assert started == [plan, plan]
    assert client.post("/data/plans/RP-1999-9999/run-all").status_code == 404


def buttons(html: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", b).strip() for b in re.findall(r"<button\b[^>]*>(.*?)</button>", html, re.S)]


def test_status_block_renders_steps_and_needs(client, conn, plan, fakes):
    """Buttons in short English (card 0127); headings, statuses and hints in Chinese."""
    r = client.get(f"/data/plans/{plan}/pipeline")
    assert r.status_code == 200 and buttons(r.text) == ["Run all"] and "every 3s" not in r.text
    pipeline.run_now(conn, plan)
    r = client.get(f"/data/plans/{plan}/pipeline")
    assert r.status_code == 200
    html = r.text
    assert buttons(html) == ["Run again", "Upload"]
    assert "缺依赖" in html and "等数据" in html
    assert "需要你" in html and "brew install cmake" in html and "user-select: all" in html
    assert "survey.dta" in html and f'action="/data/plans/{plan}/missing/upload"' in html
    assert 'name="path" value="data/survey.dta"' in html and "hx-preserve" in html
    assert "Table 3" in html and "Figure 1" in html and "取复现包" in html and "展示稿" in html
    assert "every 3s" not in html and "RUN-2026" not in html and "（" not in html


def test_status_block_polls_while_running_and_reloads_once_done(client, conn, plan, monkeypatch):
    root = W.plan_root(plan)
    run = pipeline._Run(plan, pipeline._root(plan))
    hold = pipeline._Hold(pipeline._root(plan))
    assert hold.take()
    try:
        run.begin("install")
        r = client.get(f"/data/plans/{plan}/pipeline?poll=1")
        assert r.status_code == 200 and "every 3s" in r.text and "准备环境 · 第 4/9 步" in r.text and not buttons(r.text)
    finally:
        hold.release()
    run.finish()
    r = client.get(f"/data/plans/{plan}/pipeline?poll=1")
    assert r.status_code == 204 and r.headers.get("HX-Refresh") == "true"
    assert (root / "logs" / "pipeline.json").is_file()


def test_placeholder_loads_the_block(repo):
    from research_lab.web.context import templates
    html = templates().env.get_template("partials/_run_all_status.html").render(p={"id": "RP-2026-0001"})
    assert 'hx-get="/data/plans/RP-2026-0001/pipeline"' in html and 'hx-trigger="load"' in html


def test_router_is_registered():
    from research_lab.web import app
    assert "run_all" in app.ROUTERS and "pipeline" in app.ROUTERS
    assert Path(pipeline.__file__).name == "pipeline.py"
