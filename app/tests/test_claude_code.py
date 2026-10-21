"""Claude Code engine: provider parsing, routing, and the background worker (with a fake CLI)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from pydantic import BaseModel


@pytest.fixture()
def worker_factory(conn, monkeypatch):
    from research_lab.ai import claude_code
    workers = []

    def create(**kwargs):
        worker = claude_code.Worker(**kwargs)
        workers.append(worker)
        return worker

    yield create
    # Cleanup precedes database and environment teardown, even on assertion failure.
    for worker in workers:
        worker.stop()
    for worker in workers:
        assert all(thread is None or not thread.is_alive() for thread in (worker._thread, worker._keeper))


def test_worker_stop_releases_both_idle_threads(worker_factory):
    worker = worker_factory(interval=60, hosted=True)
    worker.start()
    worker.stop()
    assert not worker._thread.is_alive() and not worker._keeper.is_alive()
    worker.stop()  # Repeated cleanup is harmless.


def fake_cli(tmp_path: Path, result: dict | str, *, is_error: bool = False, exit_code: int = 0) -> Path:
    """A stand-in `claude` that ignores its prompt and prints one JSON result line."""
    body = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    out = {"type": "result", "is_error": is_error, "result": body,
           "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 2, "output_tokens": 7},
           "modelUsage": {"claude-sonnet-5": {}}}
    script = tmp_path / "claude"
    script.write_text("#!/bin/sh\ncat > /dev/null\nprintf '%s\\n' " + "'" + json.dumps(out).replace("'", "'\\''") + "'\n"
                      + f"exit {exit_code}\n")
    script.chmod(0o755)
    return script


class EchoResult(BaseModel):
    text: str


@pytest.fixture()
def echo_job():
    from research_lab.ai import registry
    from research_lab.ai.provider import ChatRequest, JobSpec

    class Echo(JobSpec):
        kind = "test.echo"
        Result = EchoResult
        target_type = "test"
        applied: list[str] = []

        def build(self, target_id, conn):
            return ChatRequest(messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": target_id}]), []

        def apply(self, result, run_id, conn, target_id):
            Echo.applied.append(result.text)

    spec = Echo()
    registry.load_all()
    registry.register(spec)
    yield spec
    registry._JOBS.pop("test.echo", None)


def test_provider_parses_result_and_usage(tmp_path):
    from research_lab.ai.claude_code import ClaudeCodeProvider
    from research_lab.ai.provider import ChatRequest

    p = ClaudeCodeProvider(str(fake_cli(tmp_path, {"text": "东盟"})), "sonnet")
    res = p.complete(ChatRequest(messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]))
    assert json.loads(res.content) == {"text": "东盟"}
    assert res.usage == {"prompt_tokens": 17, "cache_hit_tokens": 2, "completion_tokens": 7}
    assert res.model == "claude-sonnet-5"
    cmd = p.command("sys", "sonnet")
    assert "--safe-mode" in cmd and cmd[cmd.index("--tools") + 1] == "" and "--no-session-persistence" in cmd


def test_provider_raises_on_cli_error(tmp_path):
    from research_lab.ai.claude_code import ClaudeCodeProvider
    from research_lab.ai.provider import ChatRequest

    p = ClaudeCodeProvider(str(fake_cli(tmp_path, "Not logged in", is_error=True, exit_code=1)))
    with pytest.raises(RuntimeError, match="Not logged in"):
        p.complete(ChatRequest(messages=[{"role": "user", "content": "u"}]))


def test_auto_mode_prefers_claude_code_when_installed(conn, tmp_path, monkeypatch):
    from research_lab.ai import dispatch, queue

    assert queue.mode(conn) == "agent" and dispatch.route(conn)[0] == "queue"
    monkeypatch.setenv("RL_CLAUDE_BIN", str(fake_cli(tmp_path, {"text": "x"})))
    assert queue.mode(conn) == "claude" and queue.online(conn)
    assert dispatch.route(conn) == ("claude", "")
    from research_lab.db.connection import set_setting
    set_setting(conn, "ai.claude_daily_runs", 0)
    assert dispatch.route(conn)[0] == "queue"


def test_worker_runs_queued_job(conn, tmp_path, monkeypatch, echo_job, worker_factory):
    from research_lab.ai import claude_code, dispatch, queue

    monkeypatch.setenv("RL_CLAUDE_BIN", str(fake_cli(tmp_path, {"text": "译文"})))
    job_id = queue.enqueue(conn, "test.echo", "hello")
    worker = worker_factory(interval=0.2, hosted=True)
    monkeypatch.setattr(claude_code, "WORKER", worker)
    assert dispatch.dispatch(job_id) == "ran"
    # the fake CLI is a fresh Python process: under a loaded full run it can take several seconds to start.
    # runner.run commits status='done' first and writes the log file and ai_runs.valid after it, so the test
    # waits for both within the same 30 s (card 0164).
    finished = "SELECT 1 FROM ai_runs WHERE job_id = ? AND valid IS NOT NULL"
    for _ in range(300):
        if queue.get(conn, job_id)["status"] == "done" and conn.execute(finished, (job_id,)).fetchone():
            break
        time.sleep(0.1)
    job = queue.get(conn, job_id)
    assert job["status"] == "done" and json.loads(job["result_json"]) == {"text": "译文"}
    assert echo_job.applied[-1] == "译文"
    run = conn.execute("SELECT provider, model, valid FROM ai_runs WHERE job_id = ?", (job_id,)).fetchone()
    assert tuple(run) == ("claude-code", "sonnet", 1)
    assert claude_code.runs_today(conn) == 1
    # The app's worker above still sweeps every 0.2 s; left running, it can take the next job before the check
    # below and the job reads as claimed (dispatch then says "skipped"). That race made this test fail now and
    # then under load (card 0108).
    worker.stop()
    cli_worker = worker_factory()  # a CLI process never runs jobs itself
    monkeypatch.setattr(claude_code, "WORKER", cli_worker)
    second = queue.enqueue(conn, "test.echo", "again")
    assert dispatch.dispatch(second) == "queued" and queue.get(conn, second)["status"] == "queued"


def test_recover_stale_requeues_abandoned_claims(conn, echo_job):
    from research_lab.ai import claude_code, queue
    old = queue.enqueue(conn, "test.echo", "old")
    fresh = queue.enqueue(conn, "test.echo", "fresh")
    conn.execute("UPDATE ai_jobs SET status = 'claimed', claimed_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (old,))
    queue.claim(conn, fresh)
    assert claude_code.recover_stale(conn) == 1
    assert queue.get(conn, old)["status"] == "queued" and queue.get(conn, fresh)["status"] == "claimed"


def script_cli(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "claude"
    script.write_text("#!/bin/sh\ncat > /dev/null\n" + body)
    script.chmod(0o755)
    return script


def _line(ev: dict) -> str:
    return "printf '%s\\n' '" + json.dumps(ev, ensure_ascii=False).replace("'", "'\\''") + "'\n"


def test_usage_limit_refusal_ends_the_run_at_once(tmp_path):
    """Card 0049: a rejected rate_limit_event stops the run right away (the CLI would otherwise sit it out)."""
    from research_lab.ai.claude_code import ClaudeCodeProvider, RateLimited
    from research_lab.ai.provider import ChatRequest

    reject = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 4102444800,
                                                               "rateLimitType": "five_hour", "unifiedWindows": {}}}
    p = ClaudeCodeProvider(str(script_cli(tmp_path, _line({"type": "system", "subtype": "init"}) + _line(reject) + "sleep 30\n")))
    t0 = time.monotonic()
    with pytest.raises(RateLimited, match="额度用完") as e:
        p.complete(ChatRequest(messages=[{"role": "user", "content": "u"}]))
    assert time.monotonic() - t0 < 10 and e.value.until.year == 2100 and not e.value.counts_attempt


def test_usage_limit_in_the_result_text_is_a_limit_not_a_failure(tmp_path):
    from research_lab.ai.claude_code import ClaudeCodeProvider, RateLimited
    from research_lab.ai.provider import ChatRequest

    p = ClaudeCodeProvider(str(fake_cli(tmp_path, "Claude AI usage limit reached|4102444800", is_error=True, exit_code=1)))
    with pytest.raises(RateLimited) as e:
        p.complete(ChatRequest(messages=[{"role": "user", "content": "u"}]))
    assert e.value.until.year == 2100 and e.value.counts_attempt          # read from text: capped by MAX_STALLS
    other = ClaudeCodeProvider(str(fake_cli(tmp_path, "Context limit reached for this conversation", is_error=True, exit_code=1)))
    with pytest.raises(RuntimeError, match="Context limit") as e2:
        other.complete(ChatRequest(messages=[{"role": "user", "content": "u"}]))
    assert not isinstance(e2.value, RateLimited)


def test_a_cli_that_lingers_after_its_result_still_succeeds(tmp_path):
    from research_lab.ai.claude_code import ClaudeCodeProvider
    from research_lab.ai.provider import ChatRequest

    result = {"type": "result", "is_error": False, "result": "{\"text\": \"ok\"}", "usage": {}, "modelUsage": {}}
    p = ClaudeCodeProvider(str(script_cli(tmp_path, _line(result) + "sleep 30\n")), idle=2)
    t0 = time.monotonic()
    assert json.loads(p.complete(ChatRequest(messages=[{"role": "user", "content": "u"}])).content) == {"text": "ok"}
    assert time.monotonic() - t0 < 12


def test_a_refusal_while_extra_usage_is_on_keeps_going(tmp_path):
    from research_lab.ai.claude_code import ClaudeCodeProvider
    from research_lab.ai.provider import ChatRequest

    reject = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 4102444800, "isUsingOverage": True}}
    result = {"type": "result", "is_error": False, "result": "{\"text\": \"ok\"}", "usage": {}, "modelUsage": {}}
    p = ClaudeCodeProvider(str(script_cli(tmp_path, _line(reject) + _line(result))))
    assert json.loads(p.complete(ChatRequest(messages=[{"role": "user", "content": "u"}])).content) == {"text": "ok"}


def test_a_run_past_its_timeout_waits_alone(conn, tmp_path, monkeypatch, echo_job):
    """Output kept flowing, so the job is too big rather than the engine stuck: only the job is deferred."""
    from research_lab.ai import claude_code, dispatch, queue, runner

    tick = _line({"type": "stream_event", "event": {"type": "content_block_delta"}})
    monkeypatch.setenv("RL_CLAUDE_BIN", str(script_cli(tmp_path, "i=0\nwhile [ $i -lt 40 ]; do " + tick.strip() + "; sleep 0.2; i=$((i+1)); done\n")))
    job_id = queue.enqueue(conn, "test.echo", "hello")
    provider = claude_code.make_provider(conn)
    provider.timeout = 2
    with pytest.raises(claude_code.Stalled, match="还没完成"):
        runner.run_job(conn, job_id, provider)
    assert queue.get(conn, job_id)["status"] == "queued" and claude_code.blocked(conn) is None
    assert dispatch.route(conn) == ("claude", "")


def test_a_silent_cli_is_stopped(tmp_path):
    from research_lab.ai.claude_code import ClaudeCodeProvider, Stalled
    from research_lab.ai.provider import ChatRequest

    p = ClaudeCodeProvider(str(script_cli(tmp_path, _line({"type": "system", "subtype": "init"}) + "sleep 30\n")), idle=1)
    t0 = time.monotonic()
    with pytest.raises(Stalled, match="没有任何输出"):
        p.complete(ChatRequest(messages=[{"role": "user", "content": "u"}]))
    assert time.monotonic() - t0 < 10


def test_thinking_is_off_unless_the_request_asks(tmp_path):
    """Feed batches spent ~90 % of their output on thinking under the CLI's default (10-01 runs)."""
    from research_lab.ai.claude_code import ClaudeCodeProvider
    from research_lab.ai.provider import ChatRequest

    result = {"type": "result", "is_error": False, "usage": {}, "modelUsage": {}, "result": "THINK=${MAX_THINKING_TOKENS-unset}"}
    body = "printf '%s\\n' \"" + json.dumps(result).replace('"', '\\"').replace("\\\\\"", "\\\"") + "\"\n"
    p = ClaudeCodeProvider(str(script_cli(tmp_path, body)))
    assert p.complete(ChatRequest(messages=[{"role": "user", "content": "u"}])).content == "THINK=0"
    assert p.complete(ChatRequest(messages=[{"role": "user", "content": "u"}], thinking="high")).content == "THINK=unset"
    assert "--include-partial-messages" in p.command("", "haiku")


def test_a_refused_job_waits_in_the_queue_and_the_engine_rests(conn, tmp_path, monkeypatch, echo_job):
    from research_lab.ai import claude_code, dispatch, queue, runner
    from research_lab.db.connection import set_setting

    reject = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 4102444800, "unifiedWindows": {}}}
    monkeypatch.setenv("RL_CLAUDE_BIN", str(script_cli(tmp_path, _line(reject) + "sleep 30\n")))
    job_id = queue.enqueue(conn, "test.echo", "hello")
    with pytest.raises(claude_code.RateLimited):
        runner.run_job(conn, job_id, claude_code.make_provider(conn))
    job = queue.get(conn, job_id)
    assert job["status"] == "queued" and job["attempts"] == 0 and "额度用完" in job["error"]
    where, why = dispatch.route(conn)
    assert where == "queue" and "额度用完" in why and claude_code.blocked(conn)
    set_setting(conn, claude_code.BLOCK_KEY, {"until": "2000-01-01T00:00:00+00:00", "why": "old"})
    assert dispatch.route(conn) == ("claude", "") and claude_code.blocked(conn) is None


def test_a_job_that_keeps_sticking_is_failed(conn, tmp_path, monkeypatch, echo_job):
    from research_lab.ai import claude_code, queue, runner

    monkeypatch.setenv("RL_CLAUDE_BIN", str(script_cli(tmp_path, "sleep 30\n")))
    job_id = queue.enqueue(conn, "test.echo", "hello")
    provider = claude_code.make_provider(conn)
    provider.idle = 0.5
    for n in range(1, claude_code.MAX_STALLS + 1):
        with pytest.raises(claude_code.Stalled):
            runner.run_job(conn, job_id, provider)
        job = queue.get(conn, job_id)
        assert job["attempts"] == n
        assert job["status"] == ("failed" if n == claude_code.MAX_STALLS else "queued")
    assert "不再自动重试" in job["error"]


def test_dev_and_jobs_pages_say_why_jobs_wait(client, conn):
    from datetime import datetime, timedelta, timezone
    from research_lab.ai import claude_code
    claude_code.block(conn, datetime.now(timezone.utc) + timedelta(hours=1), "Claude Code 额度用完，18:00 后自动继续")
    conn.execute("INSERT INTO ai_jobs(id, kind, status, target_type, target_id, request_json, schema_json, error, created_at) "
                 "VALUES ('J-20990101-0001', 'feeds.enrich', 'queued', 'feed', 'news:b1', '{}', '{}', 'Claude Code 额度用完，18:00 后自动继续', ?)",
                 (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
    conn.commit()
    assert "额度用完，18:00 后自动继续" in client.get("/dev").text
    assert "额度用完，18:00 后自动继续" in client.get("/chat/jobs").text


def test_a_job_past_its_timeout_does_not_hold_up_the_rest(conn, tmp_path, monkeypatch, echo_job, worker_factory):
    from research_lab.ai import claude_code, queue
    from research_lab.db.connection import set_setting

    slow, quick = queue.enqueue(conn, "test.echo", "slow"), queue.enqueue(conn, "test.echo", "quick")
    worker = worker_factory(hosted=False)
    assert worker._next(conn) == slow and worker._next(conn, {slow}) == quick
    tick = _line({"type": "stream_event", "event": {"type": "content_block_delta"}})
    result = {"type": "result", "is_error": False, "result": "{\"text\": \"done\"}", "usage": {}, "modelUsage": {}}
    # the fake CLI streams forever for "slow" and answers at once for "quick"
    body = ("p=$(cat)\ncase \"$p\" in *slow*) while true; do " + tick.strip() + "; sleep 0.2; done;; esac\n" + _line(result))
    script = tmp_path / "claude"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    monkeypatch.setenv("RL_CLAUDE_BIN", str(script))
    monkeypatch.setattr(claude_code, "LIGHT_TIMEOUT_S", 2)
    set_setting(conn, "ai.mode", "claude")
    worker._sweep(conn)
    assert queue.get(conn, quick)["status"] == "done" and queue.get(conn, slow)["status"] == "queued"
