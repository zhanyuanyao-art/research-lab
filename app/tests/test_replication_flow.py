"""Replication v2 steps 4 and 5 on the workspace page (card 0166): Compare next to the latest valid run (every verdict,
the summary, kept with the run and its run id), error runs refused with the reason, Report as one draft per run then
to it, the interpreter guard (0158 review) and what the run log says about the session."""
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

from research_lab.replication import datasets, plans, workspace as W

TARGETS = [  # T1 … T5: exact, within, direction, mismatch, missing
    {"table": "Table 2", "model": "(1)", "term": "a", "value": "0.142", "se": "0.051", "sig": "***", "n": "40", "page": "12"},
    {"table": "Table 2", "model": "(1)", "term": "b", "value": "0.20", "se": "(0.05)", "sig": "***", "n": "40", "page": "12"},
    {"table": "Table 2", "model": "(1)", "term": "c", "value": "0.50", "se": "(0.10)", "sig": "***", "n": "40", "page": "12"},
    {"table": "Table 2", "model": "(1)", "term": "d", "value": "0.30", "se": "(0.10)", "sig": "***", "n": "40", "page": "13"},
    {"table": "Table 2", "model": "(1)", "term": "e", "value": "0.30", "se": "(0.10)", "sig": "***", "n": "40", "page": "13"},
]
ROWS = [{"term": "a", "estimate": 0.1418, "se": 0.0512, "p": 0.006, "ci": None, "n": 40},
        {"term": "b", "estimate": 0.23, "se": 0.05, "p": 0.0001, "ci": None, "n": 40},
        {"term": "c", "estimate": 0.9, "se": 0.1, "p": 0.0001, "ci": None, "n": 40},
        {"term": "d", "estimate": -0.25, "se": 0.1, "p": 0.01, "ci": None, "n": 40}]
RESULTS = {"tables": [{"label": "Table 2", "model": "(1)", "estimator": "OLS", "rows": ROWS, "notes": ["按 unit 聚类"],
                       "needs_r": False}]}


@pytest.fixture()
def plan(conn, tmp_path):
    src = tmp_path / "panel.csv"
    src.write_text("unit,year,y\n" + "\n".join(f"{u},{y},{u + y}" for u in range(4) for y in range(5)) + "\n")
    ds = datasets.import_file(conn, src, name="面板数据")
    plan_id = plans.create_blank(conn)
    plans.attach_dataset(conn, plan_id, ds)
    plans.save_targets(conn, plan_id, TARGETS)
    plans.review_targets(conn, plan_id)
    return plan_id


def where(r) -> str:
    """Where a post lands: Location for a plain form, HX-Redirect for htmx (the test client sends HX-Request)."""
    return r.headers.get("location") or r.headers.get("hx-redirect") or ""


def a_run(conn, plan_id, results=RESULTS, ok=True):
    """A finished workspace run as execute() keeps it, with results.json as given."""
    ws = W.prepare(conn, plan_id)
    (ws / "outputs").mkdir(exist_ok=True)
    (ws / "outputs" / "results.json").write_text(json.dumps(results))
    (ws / "scripts" / "01_main.py").write_text("print('ok')")
    job = {"id": 0, "mode": "ai", "source_run": None}
    return W.record(conn, plan_id, job, ws, {"ok": ok and W.has_results(ws), "text": "", "tokens": 9,
                                             "code": ["scripts/01_main.py"]})


# ---------------------------------------------------------------- Compare

def test_compare_shows_every_verdict_with_the_summary_and_keeps_it_with_the_run(conn, plan, client):
    run_id = a_run(conn, plan)
    page = client.get(f"/replicate/{plan}").text
    assert 'data-replicate-compare' in page and f'/replicate/{plan}/runs/{run_id}/compare' in page
    r = client.post(f"/replicate/{plan}/runs/{run_id}/compare", follow_redirects=False)
    assert r.status_code in (204, 303) and "msg=" in where(r) and "err=" not in where(r)
    run = W.get_run(conn, run_id)
    cmp = run["workspace"]["comparison"]
    assert cmp["run_id"] == run_id and cmp["at"]
    assert {i["id"]: i["verdict"] for i in cmp["items"]} == {"T1": "exact", "T2": "within", "T3": "direction",
                                                              "T4": "mismatch", "T5": "missing"}
    page = client.get(f"/replicate/{plan}").text
    block = page.split("data-replicate-comparison", 1)[1].split("</section>", 1)[0]
    for v in ("exact", "within", "direction", "mismatch", "missing"):
        assert f'data-verdict="{v}"' in block, v
    for label in ("完全一致", "在误差范围内", "方向一致但大小或显著性不同", "对不上", "没找到对应输出"):
        assert label in block
    assert "共 5 项复现目标" in block and "对不上的是 T4" in block
    assert "<td>0.142</td>" in block and "(0.05) / 0.050" in block                  # the paper's text as printed
    assert "符号相反" in block and "Table 2 里没有 e" in block


def test_compare_sits_next_to_the_latest_valid_run_only(conn, plan, client):
    first = a_run(conn, plan)
    bad = a_run(conn, plan, results={"tables": [{"label": "Table 2", "rows": "0.14"}]})
    page = client.get(f"/replicate/{plan}").text
    assert page.count("data-replicate-compare") == 1 and f"/replicate/{plan}/runs/{first}/compare" in page
    assert f"/replicate/{plan}/runs/{bad}/compare" not in page


def test_an_error_run_cannot_be_compared_and_the_page_says_why(conn, plan, client):
    bad = a_run(conn, plan, results={"tables": [{"label": "Table 2", "rows": "0.14"}]})
    run = W.get_run(conn, bad)
    assert run["status"] == "error" and run["result"]["valid"] is False
    with pytest.raises(W.WorkspaceError, match="不符合格式"):
        W.compare_run(conn, plan, bad)
    r = client.post(f"/replicate/{plan}/runs/{bad}/compare", follow_redirects=False)
    assert "err=" in where(r)
    page = client.get(f"/replicate/{plan}").text
    assert "data-replicate-not-comparable" in page and "不能对照" in page and "data-replicate-compare" not in page
    assert "comparison" not in W.get_run(conn, bad)["workspace"]


def test_compare_needs_reviewed_targets(conn, plan):
    run_id = a_run(conn, plan)
    conn.execute("UPDATE replication_plans SET targets_reviewed_at = NULL WHERE id = ?", (plan,))
    with pytest.raises(W.WorkspaceError, match="还没审阅"):
        W.compare_run(conn, plan, run_id)


# ---------------------------------------------------------------- Report

@pytest.fixture()
def report_module(monkeypatch):
    """report.generate as card 0162 has it, faked to a ref (the real one writes a 研究档案); calls recorded."""
    calls = []

    def generate(conn, plan_id, comparison=None, *, run_id=None, extensions=(), project=None):
        calls.append({"plan_id": plan_id, "comparison": comparison, "run_id": run_id})
        return f"ART-2026-{9000 + len(calls)}"
    try:
        from research_lab.replication import report
        monkeypatch.setattr(report, "generate", generate)
    except ImportError:
        fake = types.ModuleType("research_lab.replication.report")
        fake.generate = generate
        monkeypatch.setitem(sys.modules, "research_lab.replication.report", fake)
    monkeypatch.setattr(W, "_artifact_exists", lambda conn, ref: True)
    return calls


def test_report_makes_one_draft_per_run_and_goes_to_it(conn, plan, client, report_module):
    run_id = a_run(conn, plan)
    client.post(f"/replicate/{plan}/runs/{run_id}/compare")
    r = client.post(f"/replicate/{plan}/runs/{run_id}/report", follow_redirects=False)
    assert r.status_code in (204, 303) and where(r) == "/artifacts/ART-2026-9001"
    (call,) = report_module
    assert call["run_id"] == run_id and call["comparison"]["run_id"] == run_id     # the comparison of this very run
    assert W.get_run(conn, run_id)["workspace"]["report"] == "ART-2026-9001"
    again = client.post(f"/replicate/{plan}/runs/{run_id}/report", follow_redirects=False)
    assert "err=" in where(again) and len(report_module) == 1                          # never overwritten
    with pytest.raises(W.WorkspaceError, match="不会覆盖"):
        W.report_run(conn, plan, run_id)
    page = client.get(f"/replicate/{plan}").text
    assert 'href="/artifacts/ART-2026-9001"' in page


def test_report_without_a_comparison_passes_none_and_error_runs_get_none(conn, plan, report_module):
    run_id = a_run(conn, plan)
    W.report_run(conn, plan, run_id)
    assert report_module[0]["comparison"] is None
    bad = a_run(conn, plan, results={"tables": []})
    with pytest.raises(W.WorkspaceError):
        W.report_run(conn, plan, bad)
    assert len(report_module) == 1


# ---------------------------------------------------------------- the interpreter guard (0158 review)

def test_the_sandbox_reads_only_a_real_virtualenv_not_home(tmp_path):
    home = tmp_path / "home"
    venv = home / "lab" / ".venv"
    venv.mkdir(parents=True)
    interp = tmp_path / "pythons" / "cpython-3.14"
    interp.mkdir(parents=True)
    with pytest.raises(W.WorkspaceError, match="pyvenv.cfg"):
        W.check_interpreter(venv, interp, home)
    (venv / "pyvenv.cfg").write_text("home = /x\n")
    W.check_interpreter(venv, interp, home)                                  # a real venv beside a real interpreter
    for bad in (home, tmp_path, Path("/")):
        with pytest.raises(W.WorkspaceError, match="主目录"):
            W.check_interpreter(venv, bad, home)
    (home / "pyvenv.cfg").write_text("")
    with pytest.raises(W.WorkspaceError, match="主目录"):
        W.check_interpreter(home, interp, home)                              # $HOME itself dressed as a venv


def test_a_script_does_not_run_with_an_interpreter_outside_a_virtualenv(conn, plan, tmp_path, monkeypatch):
    fake = tmp_path / "loose" / "bin"
    fake.mkdir(parents=True)
    os.symlink(sys.executable, fake / "python")
    monkeypatch.setattr(W, "venv_python", lambda: fake / "python")
    ws = W.prepare(conn, plan)
    (ws / "scripts" / "x.py").write_text("print(1)")
    with pytest.raises(W.WorkspaceError, match="不是虚拟环境"):
        W.sandbox_command(ws, "scripts/x.py")


# ---------------------------------------------------------------- the session in the run log

INIT = {"type": "system", "subtype": "init", "model": "claude-sonnet", "permissionMode": "dontAsk",
        "tools": ["Read", "Write", "Edit", "Glob", "Grep", "mcp__workspace__run_python", "mcp__workspace__check_results"],
        "mcp_servers": [{"name": "workspace", "status": "connected"}]}
RESULT = {"type": "result", "subtype": "success", "is_error": False, "result": "做完了", "num_turns": 6,
          "usage": {"input_tokens": 1000, "output_tokens": 200}}


def test_the_stream_is_read_and_the_session_is_logged():
    out = "\n".join(json.dumps(x) for x in (INIT, {"type": "assistant", "message": {}}, RESULT))
    result, init = W.parse_stream(out)
    assert result["result"] == "做完了" and init["model"] == "claude-sonnet" and W.tokens_of(result) == 1200
    note = W.session_note(init)
    assert "工具 Read, Write" in note[0] and "MCP workspace: connected" in note[0]
    assert not any("注意" in x for x in note) and "CLAUDE.md" in note[-1] and "claudeMdExcludes" in note[-1]
    risky = W.session_note({**INIT, "tools": INIT["tools"] + ["Bash"], "mcp_servers": [{"name": "workspace", "status": "failed"}]})
    assert any("白名单以外的工具：Bash" in x for x in risky) and any("没有连上" in x for x in risky)
    assert W.parse_stream(json.dumps({"result": "plain", "num_turns": 1}))[0]["result"] == "plain"   # an older json reply
    assert W.session_note({})[0].startswith("会话：CLI 没有报告")


def test_the_cli_streams_its_events(conn, plan):
    ws = W.prepare(conn, plan)
    cmd = W.claude_command("/usr/bin/true", "sonnet", ws)
    assert cmd[cmd.index("--output-format") + 1] == "stream-json" and "--verbose" in cmd


# ---------------------------------------------------------------- report.py (0162 review notes)

def test_a_real_report_carries_this_runs_comparison(conn, plan, client):
    run_id = a_run(conn, plan)
    client.post(f"/replicate/{plan}/runs/{run_id}/compare")
    r = client.post(f"/replicate/{plan}/runs/{run_id}/report", follow_redirects=False)
    ref = where(r).rsplit("/", 1)[-1]
    assert where(r).startswith("/artifacts/ART-") and client.get(where(r)).status_code == 200
    from research_lab.files import artifacts
    text = artifacts.load(next(p for p in artifacts.list_paths() if artifacts.load(p).ref == ref)).body
    assert "完全一致" in text and "对不上" in text and run_id in text
    assert "err=" in where(client.post(f"/replicate/{plan}/runs/{run_id}/report", follow_redirects=False))


def test_report_refuses_a_comparison_of_another_run_and_picks_the_kept_one(conn, plan):
    from research_lab.replication import report
    first, second = a_run(conn, plan), a_run(conn, plan)
    cmp = W.compare_run(conn, plan, first)
    with pytest.raises(plans.PlanError, match=first):
        report.generate(conn, plan, cmp, run_id=second)
    kept = W.get_run(conn, first)
    assert report.matching_comparison(None, kept)["run_id"] == first           # none given: the run's own
    assert report.matching_comparison(None, W.get_run(conn, second)) is None    # the other run has none
    stored = {**kept, "workspace": None, "workspace_json": json.dumps(kept["workspace"])}
    assert report.matching_comparison(None, stored)["run_id"] == first          # as runs.list_all hands it over


def test_code_excerpts_show_home_paths_as_tilde():
    from research_lab.replication import report
    home = str(Path.home())
    assert report.tilde(f"open('{home}/lab/data.csv')  # /Users/someone/x.py") == "open('~/lab/data.csv')  # ~/x.py"
    view = {"id": "RUN-2026-0001", "label": "复现工作区", "extension": False,
            "code": [{"name": "scripts/01.py", "text": f"df = read('{home}/Projects/lab/app/data/x.parquet')\n# /Users/leo/y"}]}
    md = "\n".join(report._code([view]))
    assert "~/Projects/lab/app/data/x.parquet" in md and "/Users/" not in md and home not in md


# ---------------------------------------------------------------- card 0169: concurrency and ~ everywhere

def other_conn():
    from research_lab.db.connection import connect
    return connect()


def test_compare_while_a_report_is_written_keeps_both(conn, plan, report_module, monkeypatch):
    """0166 review: a Compare landing while Report writes must not drop the report mark, nor the report the comparison."""
    from research_lab.replication import report
    run_id = a_run(conn, plan)
    inner = []

    def generate(c, plan_id, comparison=None, *, run_id=None, **kw):
        c2 = other_conn()
        try:
            inner.append(W.compare_run(c2, plan_id, run_id))          # another request, mid-report
        finally:
            c2.close()
        return "ART-2026-9100"
    monkeypatch.setattr(report, "generate", generate)
    assert W.report_run(conn, plan, run_id) == "ART-2026-9100"
    ws = W.get_run(conn, run_id)["workspace"]
    assert ws["report"] == "ART-2026-9100" and ws["comparison"]["at"] == inner[0]["at"] and "report_pending" not in ws


def test_a_report_landing_while_compare_computes_survives_the_compare(conn, plan, report_module, monkeypatch):
    from research_lab.replication import compare
    run_id = a_run(conn, plan)
    real = compare.compare

    def slow(*a, **k):
        c2 = other_conn()
        try:
            W.report_run(c2, plan, run_id)                             # Report finishes while Compare is computing
        finally:
            c2.close()
        return real(*a, **k)
    monkeypatch.setattr(compare, "compare", slow)
    W.compare_run(conn, plan, run_id)
    ws = W.get_run(conn, run_id)["workspace"]
    assert ws["report"].startswith("ART-") and ws["comparison"]["run_id"] == run_id


def test_a_second_report_click_while_the_first_is_written_is_refused(conn, plan, report_module, monkeypatch):
    from research_lab.replication import report
    run_id = a_run(conn, plan)
    refused, calls = [], []

    def generate(c, plan_id, comparison=None, *, run_id=None, **kw):
        calls.append(run_id)
        c2 = other_conn()
        try:
            W.report_run(c2, plan_id, run_id)
        except W.WorkspaceError as e:
            refused.append(str(e))
        finally:
            c2.close()
        return "ART-2026-9200"
    monkeypatch.setattr(report, "generate", generate)
    W.report_run(conn, plan, run_id)
    assert calls == [run_id] and refused and "正在生成" in refused[0]
    assert W.get_run(conn, run_id)["workspace"]["report"] == "ART-2026-9200"


def test_concurrent_report_clicks_make_one_report(conn, plan, report_module, monkeypatch):
    import threading
    import time as _time
    from research_lab.replication import report
    run_id = a_run(conn, plan)
    calls, outcomes = [], []

    def generate(c, plan_id, comparison=None, *, run_id=None, **kw):
        calls.append(1)
        _time.sleep(0.3)
        return f"ART-2026-93{len(calls):02d}"
    monkeypatch.setattr(report, "generate", generate)
    gate = threading.Barrier(3)

    def click():
        c = other_conn()
        gate.wait()
        try:
            outcomes.append(W.report_run(c, plan, run_id))
        except W.WorkspaceError as e:
            outcomes.append(str(e))
        finally:
            c.close()
    threads = [threading.Thread(target=click) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(calls) == 1 and sum(o.startswith("ART-") for o in outcomes) == 1
    assert W.get_run(conn, run_id)["workspace"]["report"] == "ART-2026-9301"


def test_a_failed_or_abandoned_report_frees_the_run(conn, plan, report_module, monkeypatch):
    from research_lab.replication import report
    run_id = a_run(conn, plan)

    def broken(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(report, "generate", broken)
    with pytest.raises(RuntimeError):
        W.report_run(conn, plan, run_id)
    assert "report_pending" not in W.get_run(conn, run_id)["workspace"]
    W.update_workspace(conn, run_id, lambda ws: ws.__setitem__("report_pending", {"token": "x", "since": 0}))   # a dead process
    monkeypatch.setattr(report, "generate", lambda *a, **k: "ART-2026-9400")
    assert W.report_run(conn, plan, run_id) == "ART-2026-9400"


def test_every_run_derived_text_in_the_report_shows_home_as_tilde():
    from research_lab.replication import report
    home = str(Path.home())
    view = {"id": "RUN-2026-0001", "label": f"复现工作区 {home}", "extension": False, "figures": [f"{home}/f.png"],
            "needs_r": [f"需要 R：见 {home}/lab/run.log"], "reading": "我在 /Users/leo/notes 里记了",
            "code": [{"name": "scripts/01.py", "text": f"read('{home}/x.csv')"}],
            "tables": [{"label": "Table 2", "model": "(1)", "estimator": "OLS", "needs_r": False,
                        "notes": [f"数据来自 {home}/Downloads/raw.csv", "Traceback: File \"/Users/leo/lab/s.py\""],
                        "rows": [{"term": "a", "estimate": 0.1, "se": 0.05, "p": 0.04, "ci": None, "n": 10}]}]}
    cmp = {"run_id": "RUN-2026-0001", "items": [{"id": "T1", "table": "Table 2", "model": "(1)", "term": "a", "page": "3",
           "verdict": "missing", "via": None, "matched": None, "paper": {"value": 0.1, "se": 0.05, "n": 10},
           "ours": None, "diff": None, "diff_se": None, "n_match": None, "reasons": [f"见 {home}/lab/log"]}],
           "summary": {"total": 1, "counts": {"missing": 1}, "n_match": None, "n_diffs": [], "text": f"日志在 {home}/log"}}
    plan = {"id": "RP-2026-0001", "paper_title": "T", "plan": {}}
    md = report.build(plan, [], [view], cmp)
    assert "/Users/" not in md and home not in md and "~/Downloads/raw.csv" in md
    assert view["needs_r"][0].startswith("需要 R：见 /") or home in view["needs_r"][0]          # the input is untouched
