"""AI layer: DeepSeek provider (MockTransport), runner accounting, dispatch routing, agent CLI
guardrails, /agent and /settings pages, pricing."""
from __future__ import annotations

import json
import plistlib
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from research_lab.ai import cost, dispatch, pricing, queue, registry, runner
from research_lab.ai.deepseek import OpenAICompatProvider, ProviderError
from research_lab.ai.provider import SYSTEM_RULES, ChatRequest, JobSpec
from research_lab.db.connection import get_setting, set_setting

cli = CliRunner()


class EchoResult(BaseModel):
    echo: str
    ids: list[str] = []


class EchoSpec(JobSpec):
    kind = "test.echo"
    title = "Echo (test only)"
    Result = EchoResult
    target_type = "excerpt"

    def build(self, target_id, conn):
        return ChatRequest(messages=[
            {"role": "system", "content": SYSTEM_RULES},
            {"role": "user", "content": f"Echo the target {target_id}. Candidate ids: EXC-2026-0001, ART-2026-4001."},
        ], max_tokens=200), ["app/data/cache/echo-input.txt"]

    def apply(self, result, run_id, conn, target_id):
        conn.execute("INSERT INTO settings(key, value, updated_at) VALUES (?, ?, 'now') "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (f"test.echo.{target_id}", json.dumps(result.echo)))

    def example(self):
        return {"echo": "EXC-2026-0001", "ids": ["EXC-2026-0001"]}


@pytest.fixture()
def echo(conn, monkeypatch):
    # Only the test spec: other modules' job files are not needed to exercise the AI layer.
    monkeypatch.setattr(registry, "_loaded", True)
    monkeypatch.setitem(registry._JOBS, "test.echo", EchoSpec())
    return lambda target="EXC-2026-0001": queue.enqueue(conn, "test.echo", target, force=True)


def reply(content: str, prompt=1200, hit=200, completion=80, reasoning=0, status=200):
    return httpx.Response(status, json={
        "id": "x", "object": "chat.completion", "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt, "prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": prompt - hit,
                  "completion_tokens": completion, "total_tokens": prompt + completion,
                  "completion_tokens_details": {"reasoning_tokens": reasoning}}})


class Script:
    """MockTransport handler that returns queued responses in order and records requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


def provider(script, **kw):
    return OpenAICompatProvider("https://api.deepseek.com", "sk-test", "deepseek-flash",
                                transport=httpx.MockTransport(script), sleep=lambda s: None, **kw)


VALID = json.dumps({"echo": "EXC-2026-0001", "ids": ["EXC-2026-0001"]})


# ---------------------------------------------------------------- provider + runner

def test_run_job_completes_with_tokens_cost_and_log(conn, echo, repo):
    jid = echo()
    script = Script(reply(VALID))
    run_id = runner.run_job(conn, jid, provider(script), cost.cost_usd)
    job = queue.get(conn, jid)
    assert job["status"] == "done" and json.loads(job["result_json"])["echo"] == "EXC-2026-0001"
    assert get_setting(conn, "test.echo.EXC-2026-0001") == "EXC-2026-0001"
    run = conn.execute("SELECT * FROM ai_runs WHERE id = ?", (run_id,)).fetchone()
    assert (run["provider"], run["model"]) == ("deepseek", "deepseek-flash")
    assert (run["prompt_tokens"], run["cache_hit_tokens"], run["completion_tokens"]) == (1200, 200, 80)
    assert run["cost_usd"] > 0 and run["valid"] == 1 and run["repaired"] == 0
    assert Path(run["log_path"]).exists() and "exchanges" in Path(run["log_path"]).read_text()

    body = script.bodies()[0]
    assert script.requests[0].url.path == "/chat/completions"
    assert script.requests[0].headers["authorization"] == "Bearer sk-test"
    assert body["model"] == "deepseek-flash" and body["thinking"] == {"type": "disabled"}
    assert body["response_format"] == {"type": "json_object"} and "reasoning_effort" not in body
    assert any("json" in m["content"].lower() for m in body["messages"])
    assert "sk-test" not in Path(run["log_path"]).read_text()


def test_invalid_then_valid_is_repaired(conn, echo):
    jid = echo()
    script = Script(reply("Sure! Here you go: not json"), reply(VALID))
    run_id = runner.run_job(conn, jid, provider(script), cost.cost_usd)
    run = conn.execute("SELECT * FROM ai_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["repaired"] == 1 and run["valid"] == 1
    assert run["prompt_tokens"] == 2400    # usage summed over both calls
    assert queue.get(conn, jid)["status"] == "done"
    repair_msgs = script.bodies()[1]["messages"]
    assert repair_msgs[-2]["role"] == "assistant" and "not valid" in repair_msgs[-1]["content"]


def test_two_invalid_replies_fail_the_job(conn, echo):
    jid = echo()
    with pytest.raises(Exception):
        runner.run_job(conn, jid, provider(Script(reply(""), reply('{"wrong": 1}'))), cost.cost_usd)
    job = queue.get(conn, jid)
    assert job["status"] == "failed" and job["error"]


def test_429_then_200_is_retried():
    script = Script(httpx.Response(429, text="rate limited"), reply(VALID))
    res = provider(script).complete(ChatRequest(messages=[{"role": "user", "content": "echo as json"}]))
    assert len(script.requests) == 2 and json.loads(res.content)["echo"] == "EXC-2026-0001"
    assert res.usage == {"prompt_tokens": 1200, "cache_hit_tokens": 200, "completion_tokens": 80, "reasoning_tokens": 0}


def test_timeouts_retry_then_raise_and_4xx_does_not_retry():
    script = Script(*[httpx.ReadTimeout("slow")] * 3)
    with pytest.raises(ProviderError, match="ReadTimeout"):
        provider(script).complete(ChatRequest(messages=[{"role": "user", "content": "json"}]))
    assert len(script.requests) == 3
    script = Script(httpx.Response(401, text='{"error": "bad key"}'))
    with pytest.raises(ProviderError, match="401.*bad key"):
        provider(script).complete(ChatRequest(messages=[{"role": "user", "content": "json"}]))
    assert len(script.requests) == 1


def test_body_json_hint_and_thinking():
    p = provider(Script())
    body = p.build_body(ChatRequest(messages=[{"role": "user", "content": "Summarize."}], thinking="high"))
    assert "json" in body["messages"][-1]["content"].lower()
    assert body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "high"
    assert "temperature" not in body
    plain = p.build_body(ChatRequest(messages=[{"role": "user", "content": "Hi"}], json_mode=False, model="deepseek-v4-pro"))
    assert plain["messages"][-1]["content"] == "Hi" and "response_format" not in plain and plain["model"] == "deepseek-v4-pro"


def test_body_ignores_job_params():
    """Card 0174: feeds.enrich keeps `stream: "news"` in params; it must not overwrite the API's boolean stream."""
    p = provider(Script())
    body = p.build_body(ChatRequest(messages=[{"role": "user", "content": "json"}],
                                    params={"stream": "news", "schema_name": "FeedEnrichment", "ids": ["a"]}))
    assert body["stream"] is False
    assert "schema_name" not in body and "ids" not in body
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    with_tools = p.build_body(ChatRequest(messages=[{"role": "user", "content": "json"}],
                                          params={"tools": tools, "tool_choice": "auto", "stream": "news"}))
    assert with_tools["tools"] == tools and with_tools["tool_choice"] == "auto" and with_tools["stream"] is False


# ---------------------------------------------------------------- dispatch

class InlineThread:
    def __init__(self, target, args=(), **kw):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def test_dispatch_queues_without_key_and_runs_with_key(conn, echo, monkeypatch):
    jid = echo()
    assert dispatch.dispatch(jid) == "queued"
    assert queue.get(conn, jid)["status"] == "queued"
    assert dispatch.dispatch("J-19990101-0001") == "skipped"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    script = Script(reply(VALID))
    monkeypatch.setattr(dispatch, "_TRANSPORT", httpx.MockTransport(script))
    monkeypatch.setattr(dispatch, "Thread", InlineThread)
    assert dispatch.dispatch(jid) == "ran"
    assert queue.get(conn, jid)["status"] == "done"
    assert cost.spent_today(conn) > 0
    assert dispatch.dispatch(jid) == "skipped"

    set_setting(conn, "ai.daily_cap_usd", 0.0)
    jid2 = echo("EXC-2026-0002")
    assert dispatch.dispatch(jid2) == "queued"
    set_setting(conn, "ai.daily_cap_usd", 5.0)
    set_setting(conn, "ai.mode", "agent")
    assert dispatch.dispatch(jid2) == "queued"


def test_run_now_needs_key(conn, echo, monkeypatch):
    jid = echo()
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        dispatch.run_now(jid)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    run_id = dispatch.run_now(jid, transport=httpx.MockTransport(Script(reply(VALID))))
    assert run_id and queue.get(conn, jid)["status"] == "done"


# ---------------------------------------------------------------- agent CLI

def _rl(*args):
    from research_lab.cli import app
    return cli.invoke(app, list(args))


def test_agent_complete_valid(conn, echo, tmp_path):
    jid = echo()
    r = _rl("agent", "claim", "--next")
    assert r.exit_code == 0 and jid in r.output and "Result schema" in r.output
    assert "app/data/cache/echo-input.txt" in r.output
    f = tmp_path / "result.json"
    f.write_text(VALID)
    r = _rl("agent", "complete", jid, "--from-file", str(f), "--model", "claude-opus-5-5")
    assert r.exit_code == 0, r.output
    assert queue.get(conn, jid)["status"] == "done"
    run = conn.execute("SELECT * FROM ai_runs WHERE job_id = ?", (jid,)).fetchone()
    assert (run["provider"], run["model"], run["cost_usd"]) == ("claude-code", "claude-opus-5-5", 0)


def test_agent_complete_rejects_quote_anywhere(conn, echo, tmp_path):
    jid = echo()
    f = tmp_path / "result.json"
    f.write_text(json.dumps({"echo": "x", "ids": [], "extra": [{"Quote": "made up"}]}))
    r = _rl("agent", "complete", jid, "--from-file", str(f))
    assert r.exit_code == 2 and "quote" in r.stderr and "$.extra[0].Quote" in r.stderr
    assert queue.get(conn, jid)["status"] == "queued"


def test_agent_complete_rejects_ids_not_in_input(conn, echo, tmp_path):
    jid = echo()
    f = tmp_path / "result.json"
    f.write_text(json.dumps({"echo": "EXC-2026-0001", "ids": ["EXC-2026-0999"]}))
    r = _rl("agent", "complete", jid, "--from-file", str(f))
    assert r.exit_code == 2 and "EXC-2026-0999" in r.stderr
    assert queue.get(conn, jid)["status"] == "queued"


def test_agent_complete_invalid_schema_keeps_job_claimed(conn, echo, tmp_path):
    jid = echo()
    assert _rl("agent", "claim", jid).exit_code == 0
    f = tmp_path / "result.json"
    f.write_text(json.dumps({"ids": "not-a-list"}))
    r = _rl("agent", "complete", jid, "--from-file", str(f))
    assert r.exit_code == 2 and "echo" in r.stderr
    assert queue.get(conn, jid)["status"] == "claimed"
    assert conn.execute("SELECT COUNT(*) FROM ai_runs WHERE job_id = ?", (jid,)).fetchone()[0] == 0


def test_agent_list_show_schema_fail_requeue_cancel(conn, echo):
    jid = echo()
    r = _rl("agent", "list", "--status", "queued", "--json")
    assert r.exit_code == 0 and json.loads(r.output)[0]["id"] == jid
    r = _rl("agent", "show", jid, "--format", "json")
    assert r.exit_code == 0 and json.loads(r.output)["example"]["echo"] == "EXC-2026-0001"
    r = _rl("agent", "schema", "test.echo")
    assert r.exit_code == 0 and "echo" in json.loads(r.output)["schema"]["properties"]
    assert _rl("agent", "schema", "nope").exit_code == 2
    assert _rl("agent", "fail", jid, "--reason", "input missing").exit_code == 0
    assert queue.get(conn, jid)["status"] == "failed"
    assert _rl("agent", "requeue", jid).exit_code == 0
    assert queue.get(conn, jid)["status"] == "queued"
    assert _rl("agent", "cancel", jid).exit_code == 0
    assert queue.get(conn, jid)["status"] == "cancelled"
    assert "No queued jobs." in _rl("agent", "claim", "--next").output


def test_ai_cli_mode_and_cost(conn, echo, tmp_path):
    r = _rl("ai", "mode", "agent")
    assert r.exit_code == 0 and get_setting(conn, "ai.mode") == "agent"
    assert _rl("ai", "mode", "bogus").exit_code == 2
    jid = echo()
    f = tmp_path / "r.json"
    f.write_text(VALID)
    _rl("agent", "complete", jid, "--from-file", str(f))
    r = _rl("ai", "cost", "--since", "2026-01-01")
    assert r.exit_code == 0 and "test.echo" in r.output and "claude-code" in r.output
    assert _rl("ai", "run", "--all-queued").exit_code == 1      # no key


def test_launchd_install_writes_plist(repo, monkeypatch, tmp_path):
    from research_lab.cli import launchd
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(launchd, "uv_path", lambda: "/Users/x/.local/bin/uv")
    r = _rl("launchd", "install")
    assert r.exit_code == 0 and "launchctl bootstrap" in r.output
    data = plistlib.loads(launchd.plist_path().read_bytes())
    assert data["ProgramArguments"] == ["/Users/x/.local/bin/uv", "run", "rl", "serve", "--no-open"]
    assert data["WorkingDirectory"] == str(repo) and data["RunAtLoad"] and data["KeepAlive"]
    assert data["StandardErrorPath"].endswith("app/data/logs/serve.err.log")
    assert _rl("launchd", "uninstall").exit_code == 0 and not launchd.plist_path().exists()


# ---------------------------------------------------------------- web

def test_agent_pages(client, conn, echo):
    jid = echo()
    r = client.get("/agent")
    assert r.status_code == 200 and jid in r.text and f"uv run rl agent claim {jid}" in r.text
    assert "在等待 Claude Code" in r.text
    r = client.get(f"/agent/{jid}")
    assert r.status_code == 200 and "Result schema" in r.text and "echo-input.txt" in r.text
    assert client.get("/agent/J-19990101-0001").status_code == 404
    r = client.post(f"/agent/{jid}/cancel")
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/agent"
    assert queue.get(conn, jid)["status"] == "cancelled"
    client.post(f"/agent/{jid}/requeue")
    assert queue.get(conn, jid)["status"] == "queued"
    r = client.post(f"/agent/{jid}/run", data={"next": f"/agent/{jid}"})
    assert r.headers["HX-Redirect"] == f"/agent/{jid}?note=queued:{jid}"


def test_agent_run_button_with_key(client, conn, echo, monkeypatch):
    jid = echo()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(dispatch, "_TRANSPORT", httpx.MockTransport(Script(reply(VALID))))
    monkeypatch.setattr(dispatch, "Thread", InlineThread)
    assert "Run now" in client.get("/agent").text
    r = client.post(f"/agent/{jid}/run")
    assert r.headers["HX-Redirect"] == f"/agent?note=ran:{jid}"
    assert queue.get(conn, jid)["status"] == "done"
    r = client.get(f"/agent/{jid}")
    assert "ai-log" in r.text and "&#34;echo&#34;: &#34;EXC-2026-0001&#34;" in r.text


def test_settings_get_and_post(client, conn):
    r = client.get("/settings")
    assert r.status_code == 200 and "未配置" in r.text and "sk-" not in r.text
    assert "deepseek-v4-pro" not in r.text and "deepseek-v4-pro" in client.get("/dev").text   # prices moved to 开发
    r = client.post("/settings/theme", data={"theme": "dark"})
    assert r.status_code == 200 and 'data-theme="dark"' in r.text and get_setting(conn, "ui.theme") == "dark"
    bad = client.post("/settings/theme", data={"theme": "purple"})
    assert bad.status_code == 200 and "没改成" in bad.text and get_setting(conn, "ui.theme") == "dark"
    # run mode, model, cap and year parameters are the assistant's now: the old form is gone
    assert client.post("/settings", data={"ai_mode": "agent", "ai_daily_cap_usd": "1.25"}).status_code == 405
    assert get_setting(conn, "ai.mode") is None and get_setting(conn, "ai.daily_cap_usd") is None


# ---------------------------------------------------------------- pricing

def test_pricing_table_and_peak_clock():
    assert set(pricing.KNOWN_MODELS) <= set(pricing.PRICES)
    for p in pricing.PRICES.values():
        assert p.checked_on == pricing.CHECKED_ON
        assert p.off_peak.output == pytest.approx(p.peak.output / 2)
    tue = lambda h: datetime(2026, 9, 29, h, 30, tzinfo=timezone.utc)
    assert not pricing.is_off_peak(tue(2)) and not pricing.is_off_peak(tue(8))
    assert pricing.is_off_peak(tue(5)) and pricing.is_off_peak(tue(10)) and pricing.is_off_peak(tue(0))
    assert pricing.is_off_peak(datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc))   # Saturday
    usage = {"prompt_tokens": 15_000, "cache_hit_tokens": 0, "completion_tokens": 1_500}
    assert cost.cost_usd("deepseek-flash", usage, at=tue(2)) == pytest.approx(0.0063)
    assert cost.cost_usd("deepseek-flash", usage, at=tue(5)) == pytest.approx(0.00315)
    assert cost.cost_usd("deepseek-v4-pro", usage, at=tue(2)) > cost.cost_usd("deepseek-flash", usage, at=tue(2))
    assert cost.cost_usd("mystery-model", usage, at=tue(2)) == cost.cost_usd("deepseek-v4-pro", usage, at=tue(2))


def test_daily_cap_default(conn):
    assert cost.daily_cap(conn) == 0.50 and cost.spent_today(conn) == 0.0
