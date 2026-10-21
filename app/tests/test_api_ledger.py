"""Card 0001: one process runs AI work (the lease), every call is in one ledger, and a switch pauses it all."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def test_lease_is_held_renewed_and_taken_over_when_stale(conn, monkeypatch):
    from research_lab.ai import lease
    assert lease.acquire(conn, now=T0) and lease.holds(conn, now=T0)
    assert lease.acquire(conn, now=T0 + timedelta(seconds=60))                 # renewal by the holder
    monkeypatch.setattr(lease, "me", lambda: "other-host:4242")              # a second process
    assert not lease.acquire(conn, now=T0 + timedelta(seconds=120))          # live lease: it only queues
    assert not lease.holds(conn, now=T0 + timedelta(seconds=120))
    assert lease.acquire(conn, now=T0 + timedelta(seconds=60 + lease.TTL + 1))   # the holder stopped renewing
    cur = lease.current(conn, now=T0 + timedelta(seconds=60 + lease.TTL + 2))
    assert cur["holder"] == "other-host:4242" and cur["live"] and cur["mine"]
    lease.release(conn)
    assert lease.current(conn) is None


def test_ledger_shows_job_runs_and_task_runs(conn):
    from research_lab.ai import queue
    from research_lab.db.connection import now_iso
    conn.execute("INSERT INTO ai_jobs(id, kind, status, request_json, schema_json, created_at) VALUES ('J1', 'paper.summarize', 'done', '{}', '{}', ?)",
                 (now_iso(),))
    run = queue.start_run(conn, "J1", "claude-code", "sonnet")
    queue.finish_run(conn, run, valid=True, usage={"prompt_tokens": 1200, "cache_hit_tokens": 800, "completion_tokens": 300})
    conn.execute("INSERT INTO agent_tasks(id, skill, engine, model, status, prompt, input_tokens, output_tokens, created_at, started_at, finished_at, pid) "
                 "VALUES ('AT-1', 'task', 'claude', 'sonnet', 'failed', 'x', 50, 5, '2026-10-01T10:00:00+00:00', '2026-10-01T10:00:00+00:00', "
                 "'2026-10-01T10:00:02+00:00', 99)")
    rows = {r["ref"]: dict(r) for r in conn.execute("SELECT * FROM api_calls")}
    job, task = rows["J1"], rows["AT-1"]
    assert (job["engine"], job["kind"], job["what"], job["result"], job["pid"]) == ("claude", "job", "paper.summarize", "ok", os.getpid())
    assert (job["input_tokens"], job["cache_tokens"], job["output_tokens"]) == (1200, 800, 300)
    assert (task["engine"], task["kind"], task["what"], task["result"], task["duration_ms"], task["pid"]) == ("claude", "task", "task", "error", 2000, 99)


def test_pause_stops_new_work(conn):
    from research_lab.ai import dispatch
    from research_lab.db.connection import set_setting
    set_setting(conn, dispatch.PAUSE_KEY, True)
    assert dispatch.route(conn) == ("queue", "AI 已暂停")
    set_setting(conn, dispatch.PAUSE_KEY, False)
    assert dispatch.route(conn)[1] != "AI 已暂停"


def test_pause_switch_and_dev_page(client):
    r = client.post("/settings/ai-pause", data={"paused": "1"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/settings").text
    assert "全部 AI 已暂停" in page and "恢复 AI" in page and "AI 已暂停" in page
    assert "已暂停" in client.get("/dev").text
    client.post("/settings/ai-pause", data={"paused": "0"}, follow_redirects=False)
    assert "暂停全部 AI" in client.get("/settings").text


def test_tasks_are_claimed_once_and_recovery_spares_live_processes(conn, monkeypatch):
    from research_lab.agent import tasks
    from research_lab.db.connection import now_iso
    for tid, pid in (("AT-live", os.getpid()), ("AT-dead", 999999)):
        conn.execute("INSERT INTO agent_tasks(id, skill, status, prompt, created_at, started_at, pid) VALUES (?, 'task', 'running', 'x', ?, ?, ?)",
                     (tid, now_iso(), now_iso(), pid))
    tasks.recover(conn)
    status = dict(conn.execute("SELECT id, status FROM agent_tasks").fetchall())
    assert status == {"AT-live": "running", "AT-dead": "failed"}                 # another process's task is left alone
    tid = tasks.create(conn, "hello", skill="task")
    conn.execute("UPDATE agent_tasks SET status = 'running' WHERE id = ?", (tid,))    # someone else started it
    from research_lab.agent import drivers
    monkeypatch.setattr(drivers, "active_engine", lambda c: "claude")
    calls = []
    monkeypatch.setattr(drivers, "DRIVERS", {"claude": lambda c: calls.append(1)})
    tasks.run(tid)
    assert calls == []                                                            # not claimed, not run twice


class _Provider:
    name = "fake"


def _two_jobs(conn):
    from research_lab.ai import queue
    from research_lab.db.connection import now_iso
    for jid in ("J1", "J2"):
        conn.execute("INSERT INTO ai_jobs(id, kind, status, request_json, schema_json, created_at) VALUES (?, 'test.echo', 'queued', '{}', '{}', ?)",
                     (jid, now_iso()))
    return queue


def test_holder_stops_draining_once_the_lease_is_taken_over(conn, monkeypatch):
    """Review A: the lease is renewed before every job, so a drain stops when another process holds it."""
    from research_lab.ai import claude_code, dispatch, lease, runner
    from research_lab.db.connection import now_iso
    _two_jobs(conn)
    monkeypatch.setattr(dispatch, "route", lambda c: ("claude", ""))
    monkeypatch.setattr(claude_code, "make_provider", lambda c: _Provider())
    ran = []

    def fake_run(c, jid, provider, cost_fn=None):
        ran.append(jid)
        c.execute("UPDATE ai_jobs SET status = 'done' WHERE id = ?", (jid,))
        # meanwhile this process stalled long enough for another one to take the lease
        c.execute("UPDATE worker_lease SET holder = 'other-host:1', pid = 1, renewed_at = ?", (now_iso(),))
    monkeypatch.setattr(runner, "run_job", fake_run)
    claude_code.Worker(hosted=True)._sweep(conn)
    assert ran == ["J1"]
    assert conn.execute("SELECT status FROM ai_jobs WHERE id = 'J2'").fetchone()[0] == "queued"
    assert not lease.holds(conn)


def test_holder_drains_deepseek_jobs_and_keeps_the_lease(conn, monkeypatch):
    """Review B: jobs another process queued for DeepSeek are drained by the holder's sweep."""
    from research_lab.ai import claude_code, dispatch, lease, runner
    _two_jobs(conn)
    monkeypatch.setattr(dispatch, "route", lambda c: ("deepseek", ""))
    monkeypatch.setattr(dispatch, "make_provider", lambda c: _Provider())
    ran = []

    def fake_run(c, jid, provider, cost_fn=None):
        ran.append((jid, cost_fn is not None))
        c.execute("UPDATE ai_jobs SET status = 'done' WHERE id = ?", (jid,))
    monkeypatch.setattr(runner, "run_job", fake_run)
    worker = claude_code.Worker(hosted=True)
    worker._sweep(conn)
    assert ran == [("J1", True), ("J2", True)] and lease.holds(conn)
    assert worker.keep_once(conn)                                              # renews what it holds
    monkeypatch.setattr(dispatch, "route", lambda c: ("queue", "AI 已暂停"))
    assert not worker.keep_once(conn)                                          # paused: lets it lapse
    lease.release(conn)
    monkeypatch.setattr(dispatch, "route", lambda c: ("claude", ""))
    assert not worker.keep_once(conn) and lease.current(conn) is None          # never takes a free lease


def test_deepseek_thread_leaves_a_job_the_sweep_already_took(conn, monkeypatch):
    """Review (fork, 0001 re-review): dispatch's DeepSeek thread claims atomically like the sweep, so the
    two never run one job twice."""
    from research_lab.ai import dispatch, runner
    _two_jobs(conn)
    conn.execute("UPDATE ai_jobs SET status = 'claimed' WHERE id = 'J1'")       # the sweep got there first
    monkeypatch.setattr(dispatch, "make_provider", lambda c: _Provider())
    ran = []
    monkeypatch.setattr(runner, "run_job", lambda c, jid, p, cost_fn=None: ran.append(jid))
    dispatch._worker("J1")
    dispatch._worker("J2")
    assert ran == ["J2"]

