"""复现工作区 (card 0158, docs/design/REPLICATION-v2.md 第三步): the workspace layout, the sandbox around every script,
results.json in the shared format, a run kept as an analysis record, Re-run without the AI, Stop, and no Start
before the targets are reviewed. The Claude call is mocked; the sandbox is real (macOS sandbox-exec)."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from research_lab.replication import datasets, plans, workspace as W

HAS_SANDBOX = os.access(W.SANDBOX_EXEC, os.X_OK)
needs_sandbox = pytest.mark.skipif(not HAS_SANDBOX, reason="macOS sandbox-exec only")

GOOD = {"tables": [{"label": "Table 2", "model": "(3)", "estimator": "OLS",
                    "rows": [{"term": "treated_post", "estimate": 0.139, "se": 0.05, "p": 0.006, "ci": [0.041, 0.237], "n": 40}],
                    "notes": ["按 unit 聚类"], "needs_r": False}]}

MAIN = '''import json, pathlib
import pandas as pd
import statsmodels.formula.api as smf
import matplotlib.pyplot as plt
df = pd.read_csv("data/{ds}.csv")
fit = smf.ols("y ~ treated_post", data=df).fit(cov_type="cluster", cov_kwds={{"groups": df["unit"]}})
lo, hi = fit.conf_int().loc["treated_post"]
out = {{"tables": [{{"label": "Table 2", "model": "(3)", "estimator": "OLS",
         "rows": [{{"term": "treated_post", "estimate": float(fit.params["treated_post"]), "se": float(fit.bse["treated_post"]),
                   "p": float(fit.pvalues["treated_post"]), "ci": [float(lo), float(hi)], "n": int(fit.nobs)}}],
         "notes": ["按 unit 聚类"], "needs_r": False}},
        {{"label": "Table 3", "model": "Double DID", "estimator": "double DID", "rows": [],
         "notes": ["需要 R：DIDdesign 的 Double DID"], "needs_r": True}}]}}
pathlib.Path("outputs/results.json").write_text(json.dumps(out))
plt.plot(df["year"], df["y"], ".")
plt.savefig("outputs/figures/trend.png")
print("ok", round(fit.params["treated_post"], 3))
'''


@pytest.fixture()
def plan(conn, tmp_path):
    rows = ["unit,year,treated_post,y"] + [f"{u},{y},{int(u < 2 and y >= 2)},{u + y * 0.5 + (1.0 if u < 2 and y >= 2 else 0)}"
                                            for u in range(4) for y in range(5)]
    src = tmp_path / "panel.csv"
    src.write_text("\n".join(rows) + "\n")
    ds = datasets.import_file(conn, src, name="面板数据")
    plan_id = plans.create_blank(conn)
    plans.attach_dataset(conn, plan_id, ds)
    return {"id": plan_id, "ds": ds}


def reviewed(conn, plan_id, value="2026-10-13T10:00:00"):
    """Card 0157's flag, set (or cleared) directly."""
    conn.execute("UPDATE replication_plans SET targets_reviewed_at = ? WHERE id = ?", (value, plan_id))


def add_target(conn, plan_id, label="Table 2", model="(3)", term="treated_post", value="0.142"):
    plans.save_targets(conn, plan_id, [{"table": label, "model": model, "term": term, "value": value, "page": "17"}])


# ---------------------------------------------------------------- the workspace

def test_workspace_layout_and_read_only_data(conn, plan):
    add_target(conn, plan["id"])
    ws = W.prepare(conn, plan["id"])
    from research_lab.config import get_paths
    assert ws == get_paths().data / "replication" / plan["id"] / "workspace"
    for d in W.SUBDIRS:
        assert (ws / d).is_dir(), d
    ds = plan["ds"]
    for suffix in (".parquet", ".csv", ".columns.json"):
        f = ws / "data" / f"{ds}{suffix}"
        assert f.exists() and not os.access(f, os.W_OK)
    assert (ws / "data" / f"{ds}.csv").read_text().startswith("unit,year,treated_post,y")
    md = (ws / "PLAN.md").read_text()
    assert f"data/{ds}.parquet" in md and "面板数据" in md and "20 行" in md
    assert "T1：Table 2 · (3) · treated_post（p. 17）" in md and "0.142" not in md          # no paper value for the AI
    W.prepare(conn, plan["id"])                                                              # fresh copies again
    assert (ws / "data" / f"{ds}.csv").exists()


# ---------------------------------------------------------------- the sandbox

PROBE = '''import json, os, pathlib, socket, sys
out = {{}}
def tryit(name, fn):
    try:
        fn(); out[name] = "allowed"
    except PermissionError:
        out[name] = "denied"
    except OSError as e:
        out[name] = "denied" if e.errno in (1, 13) else "error:" + str(e)
tryit("read_data", lambda: pathlib.Path("data/{ds}.csv").read_text())
tryit("write_workspace", lambda: pathlib.Path("outputs/ok.txt").write_text("x"))
tryit("write_data", lambda: pathlib.Path("data/new.csv").write_text("x"))
tryit("write_outside", lambda: pathlib.Path({outside!r}).write_text("x"))
tryit("read_outside", lambda: pathlib.Path({secret!r}).read_text())
tryit("read_home", lambda: pathlib.Path({home_file!r}).read_text())
tryit("read_db", lambda: pathlib.Path({db!r}).read_bytes())
tryit("read_env_in_workspace", lambda: pathlib.Path("tmp/.env").read_text())
tryit("network", lambda: socket.create_connection(("127.0.0.1", {port}), timeout=3).close())
import pandas, statsmodels
out["imports"] = "ok"
out["env_secret"] = "DEEPSEEK_API_KEY" in os.environ or "RL_TEST_SECRET" in os.environ
out["home"] = os.environ.get("HOME", "")
print(json.dumps(out))
'''


@needs_sandbox
def test_the_sandbox_denies_network_and_paths_outside_the_workspace(conn, plan, tmp_path, monkeypatch):
    from research_lab.config import get_paths
    monkeypatch.setenv("RL_TEST_SECRET", "synthetic-secret-not-a-key")
    ws = W.prepare(conn, plan["id"])
    (ws / "tmp").mkdir(exist_ok=True)
    (ws / "tmp" / ".env").write_text("KEY=1")
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("secret")
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    home_file = Path(W.venv_python()).parents[2] / "pyproject.toml"           # the repository, under $HOME
    (ws / "scripts" / "probe.py").write_text(PROBE.format(ds=plan["ds"], outside=str(tmp_path / "escape.txt"),
                                                          secret=str(secret), home_file=str(home_file),
                                                          db=str(get_paths().db), port=port))
    try:
        r = W.run_script(ws, "scripts/probe.py", timeout=180)
    finally:
        server.close()
    assert r["exit"] == 0, r["output"]
    got = json.loads(r["output"].strip().splitlines()[-1])
    assert got["read_data"] == "allowed" and got["write_workspace"] == "allowed" and got["imports"] == "ok"
    for k in ("write_data", "write_outside", "read_outside", "read_db", "read_env_in_workspace", "network"):
        assert got[k] == "denied", (k, got)
    if home_file.exists() and str(home_file).startswith(str(Path.home())):
        assert got["read_home"] == "denied", got
    assert not got["env_secret"] and got["home"].startswith(str(ws.resolve()))
    assert not (tmp_path / "escape.txt").exists()
    calls = [json.loads(x) for x in (ws / "logs" / "calls.jsonl").read_text().splitlines()]
    assert calls[-1]["script"] == "scripts/probe.py" and calls[-1]["exit"] == 0


@pytest.mark.parametrize("script", ["../outside.py", "data/x.py", "scripts/missing.py", "scripts/notes.txt", "/etc/hosts"])
def test_only_scripts_of_the_workspace_run(conn, plan, script):
    ws = W.prepare(conn, plan["id"])
    (ws / "scripts" / "notes.txt").write_text("x")
    with pytest.raises(W.WorkspaceError):
        W.run_script(ws, script)


def test_a_database_without_0157s_column_counts_as_not_reviewed(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "old.sqlite")
    db.execute("CREATE TABLE replication_plans (id TEXT)")
    db.execute("INSERT INTO replication_plans VALUES ('RP-2026-0001')")
    assert W.targets_reviewed(db, "RP-2026-0001") is False


def test_the_profile_carries_the_rules():
    sb = W.PROFILE.read_text()
    assert "(deny network*)" in sb and "(deny file-write*)" in sb
    assert '(allow file-write* (subpath (param "WORKSPACE"))' in sb and '"/data")' in sb
    assert '(subpath (param "HOME_DIR"))' in sb and '(subpath (param "APP_DATA"))' in sb and "\\.env" in sb
    assert sb.index("(deny file-read* (subpath (param \"HOME_DIR\"))") < sb.index('(allow file-read* (subpath (param "WORKSPACE"))')


# ---------------------------------------------------------------- the results format

def test_results_validator():
    assert W.validate_results(GOOD) == []
    bad = json.loads(json.dumps(GOOD))
    bad["tables"][0]["rows"][0].update(se="0.05", p=1.5, ci=[0.3, 0.1], n=4.5)
    bad["tables"][0]["needs_r"] = "no"
    errs = W.validate_results(bad)
    assert any(".se:" in e for e in errs) and any(".p:" in e for e in errs) and any(".ci:" in e for e in errs)
    assert any(".n:" in e for e in errs) and any("needs_r" in e for e in errs)
    assert W.validate_results({"tables": [], "extra": 1}) and W.validate_results([])
    nan = json.loads(json.dumps(GOOD))
    nan["tables"][0]["rows"][0]["estimate"] = float("nan")
    assert W.validate_results(nan)
    empty = json.loads(json.dumps(GOOD))
    empty["tables"][0]["rows"] = []
    assert W.validate_results(empty)                                   # rows may be empty only when it needs R
    empty["tables"][0]["needs_r"] = True
    assert W.validate_results(empty) == []
    assert W.validate_mapping({"T1": ["Table 2", "(3)", "treated_post"]}, GOOD, ["T1"]) == []
    assert W.validate_mapping({"T9": ["Table 2", "(3)", "treated_post"]}, GOOD, ["T1"])
    assert W.validate_mapping({"T1": ["Table 9", "(3)", "treated_post"]}, GOOD, ["T1"])


def mcp(ws, *messages):
    """The run_python / check_results server as Claude Code starts it: this file as a script, the app's python."""
    lines = "".join(json.dumps(m) + "\n" for m in messages)
    out = subprocess.run([sys.executable, "-E", "-s", str(Path(W.__file__).resolve()), "mcp", str(ws)], input=lines,
                         capture_output=True, text=True, timeout=240)
    return [json.loads(x) for x in out.stdout.splitlines() if x.strip()]


@needs_sandbox
def test_the_mcp_server_runs_scripts_and_checks_results(conn, plan):
    ws = W.prepare(conn, plan["id"])
    (ws / "outputs").mkdir(exist_ok=True)
    (ws / "outputs" / "results.json").write_text(json.dumps({"tables": [{"label": "Table 2"}]}))
    (ws / "scripts" / "hello.py").write_text("print('hello from the sandbox')")
    replies = mcp(ws, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                  {"jsonrpc": "2.0", "method": "notifications/initialized"},
                  {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                  {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "check_results", "arguments": {}}},
                  {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "run_python", "arguments": {"script": "scripts/hello.py"}}},
                  {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "run_python", "arguments": {"script": "../x.py"}}})
    by = {r["id"]: r for r in replies}
    assert by[1]["result"]["protocolVersion"] == "2025-06-18" and len(replies) == 5          # no reply to the notification
    assert {t["name"] for t in by[2]["result"]["tools"]} == {"run_python", "check_results"}
    assert by[3]["result"]["isError"] and "keys must be exactly" in by[3]["result"]["content"][0]["text"]
    assert not by[4]["result"]["isError"] and "hello from the sandbox" in by[4]["result"]["content"][0]["text"]
    assert by[5]["result"]["isError"] and "只能运行工作区里的" in by[5]["result"]["content"][0]["text"]
    (ws / "outputs" / "results.json").write_text(json.dumps(GOOD))
    ok = mcp(ws, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "check_results", "arguments": {}}})
    assert not ok[0]["result"]["isError"] and ok[0]["result"]["content"][0]["text"].startswith("ok")


# ---------------------------------------------------------------- Start, refused before review

def test_start_is_refused_before_the_targets_are_reviewed(conn, plan, client, monkeypatch):
    from research_lab.ai import claude_code
    monkeypatch.setattr(claude_code, "find_cli", lambda conn=None: "/usr/bin/true")
    with pytest.raises(W.WorkspaceError, match="复现目标还没审阅"):
        W.start(conn, plan["id"])                                        # never reviewed
    add_target(conn, plan["id"])
    plans.review_targets(conn, plan["id"])
    reviewed(conn, plan["id"], None)                                     # reviewed, then cleared
    with pytest.raises(W.WorkspaceError, match="复现目标还没审阅"):
        W.start(conn, plan["id"])
    page = client.get(f"/replicate/{plan['id']}").text
    assert "data-replicate-unreviewed" in page and "data-replicate-start disabled" in page
    r = client.post(f"/replicate/{plan['id']}/start", follow_redirects=False)
    assert "err=" in r.headers.get("location", r.headers.get("hx-redirect", ""))
    assert not conn.execute("SELECT COUNT(*) FROM replication_jobs").fetchone()[0]
    plans.review_targets(conn, plan["id"])                              # card 0157's own click
    assert W.check_start(conn, plan["id"], "ai")["id"] == plan["id"]
    empty = plans.create_blank(conn)
    reviewed(conn, empty)
    with pytest.raises(W.WorkspaceError, match="数据集"):
        W.check_start(conn, empty, "ai")


def test_the_page_shows_the_plan_datasets_and_an_estimate(conn, plan, client):
    page = client.get(f"/replicate/{plan['id']}").text
    assert "复现工作区" in page and "面板数据" in page and "data-replicate-estimate" in page and "万 token" in page
    assert ">Start</button>" in page
    assert client.get("/replicate/RP-2099-9999").status_code == 404


# ---------------------------------------------------------------- a run, mocked AI

class Sync:
    """threading.Thread, run on the spot."""
    def __init__(self, target, args=(), **kw):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def fake_session(plan, calls):
    def run_cli(cmd, prompt, cwd, env, on_start, timeout=0):
        calls.append({"cmd": cmd, "prompt": prompt, "env": env})
        on_start(os.getpid())
        (cwd / "scripts" / "01_main.py").write_text(MAIN.format(ds=plan["ds"]))
        r = W.run_script(cwd, "scripts/01_main.py", timeout=240)
        assert r["exit"] == 0, r["output"]
        return {"ok": True, "text": "做了 Table 2；Table 3 需要 R。", "tokens": 12345, "turns": 7, "seconds": 3, "stopped": False}
    return run_cli


@needs_sandbox
def test_an_ai_run_is_kept_as_a_record_and_re_runs_without_the_ai(conn, plan, client, monkeypatch):
    from research_lab.ai import claude_code
    reviewed(conn, plan["id"])
    calls: list = []
    monkeypatch.setattr(claude_code, "find_cli", lambda conn=None: "/usr/bin/true")
    monkeypatch.setattr(W.threading, "Thread", Sync)
    monkeypatch.setattr(W, "run_cli", fake_session(plan, calls))
    W.start(conn, plan["id"])
    (cmd,) = [c["cmd"] for c in calls]
    assert "--restricted" in cmd and "dontAsk" in cmd and "Bash" in cmd[cmd.index("--disallowedTools") + 1:]
    assert "Bash" not in cmd[cmd.index("--tools") + 1] and "mcp__workspace__run_python" in cmd
    assert calls[0]["env"].get("MAX_THINKING_TOKENS") == "0" and "RL_TEST_SECRET" not in calls[0]["env"]
    assert "results.json" in calls[0]["prompt"] and "check_results" in calls[0]["prompt"]
    (run,) = W.runs(conn, plan["id"])
    w = run["workspace"]
    assert run["status"] == "done" and run["tokens"] == 12345 and w["mode"] == "ai" and w["state"] == "done"
    assert w["code"] == ["scripts/01_main.py"] and w["figures"] == ["outputs/figures/trend.png"] and not w["errors"]
    assert w["needs_r"] and "需要 R" in w["needs_r"][0] and "需要 R" in w["log"]
    est = run["result"]["results"]["tables"][0]["rows"][0]["estimate"]
    import pandas as pd
    import statsmodels.formula.api as smf
    df = pd.read_csv(W.workspace_dir(plan["id"]) / "data" / f"{plan['ds']}.csv")
    assert est == pytest.approx(float(smf.ols("y ~ treated_post", data=df).fit().params["treated_post"]))   # executed code
    frozen = W.run_dir(plan["id"], run["id"])
    assert (frozen / "scripts" / "01_main.py").exists() and (frozen / "outputs" / "results.json").exists()
    job = conn.execute("SELECT * FROM replication_jobs").fetchone()
    assert job["state"] == "done" and job["run_id"] == run["id"] and job["tokens"] == 12345
    page = client.get(f"/replicate/{plan['id']}").text
    assert "Table 2" in page and "需要 R" in page and f"/replicate/{plan['id']}/runs/{run['id']}/files/outputs/figures/trend.png" in page
    png = client.get(f"/replicate/{plan['id']}/runs/{run['id']}/files/outputs/figures/trend.png")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert client.get(f"/replicate/{plan['id']}/runs/{run['id']}/files/../../workspace/PLAN.md").status_code == 404
    assert client.get(f"/replicate/{plan['id']}/runs/{run['id']}/files/data/x.csv").status_code == 404

    # Re-run: the saved code in the sandbox, no AI call, no tokens
    def no_ai(*a, **k):
        raise AssertionError("a re-run must not call the AI")
    monkeypatch.setattr(W, "run_cli", no_ai)
    monkeypatch.setattr(claude_code, "find_cli", lambda conn=None: None)
    (W.workspace_dir(plan["id"]) / "scripts" / "01_main.py").write_text("raise SystemExit('changed since')")
    r = client.post(f"/replicate/{plan['id']}/runs/{run['id']}/rerun", follow_redirects=False)
    assert r.status_code in (204, 303)
    again = W.runs(conn, plan["id"])[0]
    assert again["id"] != run["id"] and again["workspace"]["mode"] == "rerun" and again["tokens"] == 0
    assert again["status"] == "done" and again["workspace"]["source_run"] == run["id"]
    assert again["result"]["results"]["tables"][0]["rows"][0]["estimate"] == est


@needs_sandbox
def test_a_run_whose_results_do_not_match_the_format_is_an_error(conn, plan, monkeypatch):
    from research_lab.ai import claude_code
    reviewed(conn, plan["id"])
    monkeypatch.setattr(claude_code, "find_cli", lambda conn=None: "/usr/bin/true")
    monkeypatch.setattr(W.threading, "Thread", Sync)

    def run_cli(cmd, prompt, cwd, env, on_start, timeout=0):
        (cwd / "outputs" / "results.json").write_text(json.dumps({"tables": [{"label": "Table 2", "rows": "0.14"}]}))
        return {"ok": True, "text": "done", "tokens": 100, "turns": 2, "seconds": 1, "stopped": False}
    monkeypatch.setattr(W, "run_cli", run_cli)
    W.start(conn, plan["id"])
    (run,) = W.runs(conn, plan["id"])
    assert run["status"] == "error" and run["workspace"]["errors"] and "不符合格式" in run["workspace"]["log"]
    assert run["result"]["valid"] is False                               # step 4 skips it
    assert conn.execute("SELECT state FROM replication_jobs").fetchone()[0] == "error"


# ---------------------------------------------------------------- Stop, and a job whose app is gone

def test_stop_kills_the_running_process_group(conn, plan):
    proc = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    conn.execute("INSERT INTO replication_jobs(plan_id, mode, state, pid, host_pid, started_at) VALUES (?,?,?,?,?,?)",
                 (plan["id"], "ai", "running", proc.pid, os.getpid(), "2026-10-13 10:00:00"))
    assert W.stop(conn, plan["id"])
    assert proc.wait(timeout=10) == -signal.SIGKILL
    assert conn.execute("SELECT state FROM replication_jobs").fetchone()[0] == "stopped"
    assert not W.stop(conn, plan["id"])


def test_a_job_whose_app_process_is_gone_failed(conn, plan):
    dead = subprocess.Popen(["/usr/bin/true"])
    dead.wait()
    conn.execute("INSERT INTO replication_jobs(plan_id, mode, state, host_pid, started_at) VALUES (?,?,?,?,?)",
                 (plan["id"], "ai", "running", dead.pid, "2026-10-13 10:00:00"))
    assert W.current_job(conn, plan["id"]) is None
    row = conn.execute("SELECT state, note FROM replication_jobs").fetchone()
    assert row["state"] == "error" and "中断" in row["note"]


# ---------------------------------------------------------------- the prompt

def test_the_skill_states_the_rules_and_the_format():
    from research_lab.agent import skills
    sk = skills.get("replicate")
    assert sk.mode == "job" and not sk.runs_tasks
    text = sk.instructions
    for needle in ("outputs/results.json", "outputs/mapping.json", '"needs_r"', "「需要 R」", "check_results",
                   "Never invent", "exact specification", "deviation", "run_python"):
        assert needle in text, needle
    assert '"estimate"' in text and '"se"' in text and '"ci"' in text and '"n"' in text


def test_a_workspace_run_does_not_break_the_data_pages(conn, plan, client):
    ws = W.prepare(conn, plan["id"])
    (ws / "outputs").mkdir(exist_ok=True)
    (ws / "outputs" / "results.json").write_text(json.dumps(GOOD))
    job = {"id": 0, "mode": "ai", "source_run": None}
    run_id = W.record(conn, plan["id"], job, ws, {"ok": True, "text": "", "tokens": 5, "code": []})
    for url in ("/data", f"/data/runs/{run_id}", f"/data/plans/{plan['id']}", f"/data/datasets/{plan['ds']}"):
        assert client.get(url).status_code == 200, url
    from research_lab.replication import runs
    with pytest.raises(runs.RunError, match="复现工作区"):
        runs.rerun(conn, run_id)                     # the data page's rerun leaves the record alone
    assert W.get_run(conn, run_id)["status"] == "done"


# ---------------------------------------------------------------- 0158 review: links, odd files, child processes

ESCAPES = '''import json, os, socket, subprocess, sys
out = {{}}
def tryit(name, fn):
    try:
        fn(); out[name] = "allowed"
    except OSError as e:
        out[name] = "denied" if e.errno in (1, 13) else "error:" + str(e)
tryit("symlink_out", lambda: os.symlink({decoy!r}, "logs/probe.log"))
tryit("symlink_in", lambda: os.symlink("scripts/escapes.py", "outputs/link.py"))
tryit("fifo", lambda: os.mkfifo("outputs/pipe"))
tryit("fork", lambda: os.waitpid(os.fork(), 0) if True else None)
tryit("subprocess", lambda: subprocess.run(["/bin/sleep", "60"]))
print(json.dumps(out))
'''


@needs_sandbox
def test_the_sandbox_refuses_links_fifos_and_child_processes(conn, plan, tmp_path):
    decoy = tmp_path / "decoy.txt"
    decoy.write_text("Leo's file")
    ws = W.prepare(conn, plan["id"])
    (ws / "outputs").mkdir(exist_ok=True)
    (ws / "scripts" / "escapes.py").write_text(ESCAPES.format(decoy=str(decoy)))
    r = W.run_script(ws, "scripts/escapes.py", timeout=60)
    got = json.loads(r["output"].strip().splitlines()[-1])
    assert got == {"symlink_out": "denied", "symlink_in": "denied", "fifo": "denied", "fork": "denied",
                   "subprocess": "denied"}, got
    assert decoy.read_text() == "Leo's file" and r["seconds"] < 30


@needs_sandbox
def test_pyfixest_differences_and_matplotlib_run_without_child_processes(conn, plan):
    ws = W.prepare(conn, plan["id"])
    (ws / "outputs" / "figures").mkdir(parents=True, exist_ok=True)
    (ws / "scripts" / "did.py").write_text('''import numpy as np, pandas as pd, pyfixest as pf, differences, matplotlib.pyplot as plt
rng = np.random.default_rng(0)
df = pd.DataFrame({"unit": np.repeat(np.arange(40), 10), "year": np.tile(np.arange(10), 40)})
df["treat"] = ((df.unit < 20) & (df.year >= 5)).astype(int)
df["y"] = 0.5 * df.treat + rng.normal(size=len(df))
fit = pf.feols("y ~ treat | unit + year", data=df, vcov={"CRV1": "unit"})
df["cohort"] = np.where(df.unit < 20, 5, np.nan)
att = differences.ATTgt(data=df.set_index(["unit", "year"]), cohort_column="cohort")
att.fit(formula="y", n_jobs=1, progress_bar=False)
plt.plot([1, 2]); plt.savefig("outputs/figures/x.png")
print("feols", round(float(fit.coef()["treat"]), 2), "attgt", len(att.aggregate("simple")))
''')
    r = W.run_script(ws, "scripts/did.py", timeout=600)
    assert r["exit"] == 0, r["output"]
    assert "feols 0.51 attgt 1" in r["output"] and (ws / "outputs" / "figures" / "x.png").is_file()


def plant(ws, tmp_path):
    """What a script could leave if the sandbox let it: links out of the workspace, a linked folder, a FIFO, a hard
    link. Returns the decoys that must never change."""
    decoy, folder = tmp_path / "decoy.txt", tmp_path / "decoy-dir"
    decoy.write_text("Leo's file")
    folder.mkdir()
    (folder / "keep.txt").write_text("keep")
    for d in ("logs", "outputs/figures", "scripts"):
        (ws / d).mkdir(parents=True, exist_ok=True)
    os.symlink(decoy, ws / "logs" / "probe.log")
    os.symlink(decoy, ws / "logs" / "calls.jsonl")
    os.symlink(decoy, ws / "outputs" / "results.json")
    os.symlink(decoy, ws / "outputs" / "figures" / "leak.png")
    os.symlink(folder, ws / "outputs" / "linked")
    os.mkfifo(ws / "outputs" / "pipe")
    (ws / "scripts" / "a.py").write_text("print(1)")
    os.link(ws / "scripts" / "a.py", ws / "scripts" / "hard.py")
    return decoy, folder


def no_links(ws):
    return [str(p) for p in Path(ws).rglob("*") if p.is_symlink() or not (p.is_file() or p.is_dir())]


def test_sweep_removes_links_and_odd_files_and_never_their_targets(conn, plan, tmp_path):
    ws = W.prepare(conn, plan["id"])
    decoy, folder = plant(ws, tmp_path)
    gone = W.sweep(ws)
    assert {"logs/probe.log", "logs/calls.jsonl", "outputs/results.json", "outputs/figures/leak.png", "outputs/linked",
            "outputs/pipe"} <= set(gone)
    assert any(g.startswith("scripts/") for g in gone)                    # one of the hard-linked pair; the other is single again
    assert all(f.stat().st_nlink == 1 for f in (ws / "scripts").iterdir())
    assert not no_links(ws) and decoy.read_text() == "Leo's file" and (folder / "keep.txt").read_text() == "keep"
    assert (ws / "data" / f"{plan['ds']}.csv").is_file() and (ws / "PLAN.md").is_file()


@needs_sandbox
def test_a_script_run_never_writes_through_a_planted_link(conn, plan, tmp_path):
    """The review's reproduction: logs/<script>.log linked to a file outside was overwritten after one run_python."""
    ws = W.prepare(conn, plan["id"])
    decoy, _ = plant(ws, tmp_path)
    (ws / "scripts" / "probe.py").write_text("print('PWNED')")
    r = W.run_script(ws, "scripts/probe.py", timeout=60)
    assert r["exit"] == 0 and "PWNED" in r["output"]
    assert decoy.read_text() == "Leo's file" and not no_links(ws)
    assert (ws / "logs" / "probe.log").read_text().strip() == "PWNED" and not (ws / "logs" / "probe.log").is_symlink()
    assert "删除了工作区里的链接或特殊文件" in (ws / "logs" / "run.log").read_text()


def test_reads_and_writes_outside_the_sandbox_refuse_links(conn, plan, tmp_path):
    ws = W.prepare(conn, plan["id"])
    decoy, folder = plant(ws, tmp_path)
    with pytest.raises(W.WorkspaceError):
        W.safe_write(ws, "logs/probe.log", "overwrite")
    with pytest.raises(W.WorkspaceError):
        W.safe_write(ws, "outputs/linked/keep.txt", "overwrite")            # a linked folder on the way
    with pytest.raises((W.WorkspaceError, OSError)):
        W.safe_read(ws, "logs/calls.jsonl")
    with pytest.raises(W.WorkspaceError):
        W.safe_read(ws, "outputs/pipe")                                     # no hang on a FIFO
    data, why = W._load(ws, W.RESULTS)
    assert data is None and why
    assert W.check_results(ws) and not W.has_results(ws)
    assert decoy.read_text() == "Leo's file" and (folder / "keep.txt").read_text() == "keep"
    W._log(ws, "a line")                                                   # a log line never follows the link either
    assert decoy.read_text() == "Leo's file"


def test_freezing_and_re_running_copy_only_regular_files(conn, plan, tmp_path):
    ws = W.prepare(conn, plan["id"])
    decoy, folder = plant(ws, tmp_path)
    (ws / "outputs" / "real.txt").write_text("ok")
    job = {"id": 0, "mode": "ai", "source_run": None}
    run_id = W.record(conn, plan["id"], job, ws, {"ok": False, "text": "", "tokens": 0, "code": ["scripts/a.py"]})
    frozen = W.run_dir(plan["id"], run_id)
    assert (frozen / "outputs" / "real.txt").read_text() == "ok" and not no_links(frozen)
    assert not (frozen / "outputs" / "results.json").exists() and not (frozen / "outputs" / "linked").exists()
    assert not (frozen / "scripts" / "hard.py").exists() and not (frozen / "scripts" / "a.py").exists()   # hard-linked pair
    assert "Leo's file" not in json.dumps(W.get_run(conn, run_id), ensure_ascii=False)
    (frozen / "scripts").mkdir(exist_ok=True)
    os.symlink(decoy, frozen / "scripts" / "evil.py")                      # even a frozen run is copied plain
    (frozen / "scripts" / "b.py").write_text("print(2)")
    target = tmp_path / "restore"
    assert W.copy_regular(frozen / "scripts", target) == ["b.py"] and not no_links(target)
    with pytest.raises(W.WorkspaceError):
        W.run_file(plan["id"], run_id, "scripts/evil.py")


@needs_sandbox
def test_a_planted_link_is_gone_before_the_ai_session_continues(conn, plan, tmp_path, monkeypatch):
    from research_lab.ai import claude_code
    reviewed(conn, plan["id"])
    ws = W.prepare(conn, plan["id"])
    decoy, _ = plant(ws, tmp_path)
    seen = {}
    monkeypatch.setattr(claude_code, "find_cli", lambda conn=None: "/usr/bin/true")
    monkeypatch.setattr(W.threading, "Thread", Sync)

    def run_cli(cmd, prompt, cwd, env, on_start, timeout=0):
        seen["links"] = no_links(cwd)
        os.symlink(decoy, cwd / "logs" / "claude.txt")          # as if the session left one behind
        return {"ok": False, "text": "overwrite?", "tokens": 1, "turns": 1, "seconds": 1, "stopped": False}
    monkeypatch.setattr(W, "run_cli", run_cli)
    W.start(conn, plan["id"])
    assert seen["links"] == [] and decoy.read_text() == "Leo's file"
    (run,) = W.runs(conn, plan["id"])
    assert "删除了工作区里的链接或特殊文件" in run["workspace"]["log"] and not no_links(W.workspace_dir(plan["id"]))


@needs_sandbox
def test_the_script_timeout_ends_the_run(conn, plan):
    ws = W.prepare(conn, plan["id"])
    (ws / "scripts" / "hang.py").write_text("import time\ntime.sleep(120)")
    start = time.monotonic()
    r = W.run_script(ws, "scripts/hang.py", timeout=3)
    assert r["exit"] == -9 and "已中止" in r["output"] and time.monotonic() - start < 30


def test_the_session_keeps_project_memory_out(conn, plan):
    ws = W.prepare(conn, plan["id"])
    cmd = W.claude_command("/usr/bin/true", "sonnet", ws)
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    assert "**/CLAUDE.md" in settings["claudeMdExcludes"] and settings["autoMemoryEnabled"] is False
    env = W._cli_env(ws, "standard")
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1" and env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["MAX_THINKING_TOKENS"] == "0" and "MAX_THINKING_TOKENS" not in W._cli_env(ws, "deep")
