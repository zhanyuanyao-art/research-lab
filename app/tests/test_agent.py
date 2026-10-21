"""Task agent: skills, tools and budgets, the MCP endpoint, and both engine drivers."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest


# ---------------------------------------------------------------- helpers

def offline_paper(conn, sample_pdf):
    """Ingest the sample PDF without network or AI jobs."""
    from research_lab.intake import pipeline
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404, json={}))) as c:
        return pipeline.ingest(conn, sample_pdf, http=c, ai=False).paper_id


def small_dataset(conn, tmp_path: Path) -> str:
    from research_lab.replication import datasets
    rows = ["国家,年份,出席率,联盟强度"] + [f"{c},{2005 + i},{0.3 + 0.02 * i + (0.1 if c == '印尼' else 0)},{0.1 * (i % 5)}"
                                         for c in ("印尼", "泰国", "越南") for i in range(12)]
    p = tmp_path / "attendance.csv"
    p.write_text("\n".join(rows), encoding="utf-8")
    return datasets.import_file(conn, p, name="asean-attendance")


def fake_claude(tmp_path: Path, report: str = "## 结论\n完成。") -> tuple[Path, Path]:
    """A stand-in `claude` that records its argv and stdin, then streams init / rate limit / result events."""
    argv_log = tmp_path / "argv.jsonl"
    events = [
        {"type": "system", "subtype": "init", "session_id": "sess-1", "mcp_servers": [{"name": "lab", "status": "connected"}]},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 0.2}, "seven_day": {"utilization": 0.5}}}},
        {"type": "result", "subtype": "success", "is_error": False, "result": report, "session_id": "sess-1",
         "usage": {"input_tokens": 100, "cache_creation_input_tokens": 50, "cache_read_input_tokens": 400, "output_tokens": 80}},
    ]
    script = tmp_path / "claude"
    script.write_text(
        "#!/usr/bin/env python3\nimport json, sys\n"
        f"open({str(argv_log)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'stdin': sys.stdin.read()}}) + '\\n')\n"
        f"for e in {events!r}:\n    print(json.dumps(e, ensure_ascii=False), flush=True)\n", encoding="utf-8")
    script.chmod(0o755)
    return script, argv_log


# ---------------------------------------------------------------- skills

def test_skills_load_and_route():
    from research_lab.agent import skills
    allskills = skills.load_all()
    assert {"task", "translate", "summarize", "methods", "analysis", "associate", "writing", "feeds", "chat", "issues"} <= set(allskills)
    assert skills.pick("把这篇翻译成中文").name == "translate"
    assert skills.pick("帮我跑个回归看看联盟强度是否显著").name == "analysis"
    assert skills.pick("这篇文章的研究设计和变量是怎么操作化的").name == "methods"
    assert skills.pick("下周要做什么").name == "task"
    assert skills.pick("把数据清洗那条改成已完成").name == "issues"      # not analysis: an action on an issue
    assert skills.for_job("paper.translate").name == "translate"
    assert skills.for_job("unknown.kind") is None
    for s in allskills.values():
        if s.runs_tasks:
            assert s.instructions and s.tools and all(t in __import__("research_lab.agent.tools", fromlist=["TOOLS"]).TOOLS for t in s.tools)


def test_skill_parse_rejects_bad_tier():
    from research_lab.agent import skills
    with pytest.raises(ValueError, match="tier"):
        skills.parse("---\nname: x\ntier: huge\n---\nbody")


def test_tier_overrides_and_engine_models(conn):
    from research_lab.agent import skills
    from research_lab.db.connection import set_setting
    sk = skills.get("translate")
    assert skills.tier_of(conn, sk) == "standard" and skills.tier_of(conn, sk, task=True) == "light"
    set_setting(conn, "skill.translate.tier", "light")
    assert skills.tier_of(conn, sk) == "light"
    assert skills.engine_model(conn, "claude", "light") == "haiku"
    assert skills.engine_model(conn, "deepseek", "deep") == "deepseek-v4-pro"
    set_setting(conn, "engine.claude.standard", "opus")
    assert skills.engine_model(conn, "claude", "standard") == "opus"


# ---------------------------------------------------------------- tools and budgets

def test_tools_read_and_analyse(conn, sample_pdf, tmp_path):
    from research_lab.agent import tools
    pid = offline_paper(conn, sample_pdf)
    text, err = tools.call(conn, "list_papers", {}, 4000)
    assert not err and pid in text
    text, err = tools.call(conn, "get_paper", {"paper_id": pid}, 4000)
    assert not err and "pages:" in text and "no summary yet" in text
    text, err = tools.call(conn, "read_pages", {"paper_id": pid, "start": 1, "end": 2}, 4000)
    assert not err and "=== position 1" in text
    text, err = tools.call(conn, "get_paper", {"paper_id": "nope"}, 4000)
    assert err and "list_papers" in text
    small_dataset(conn, tmp_path)
    text, err = tools.call(conn, "describe_dataset", {"dataset_id": "asean-attendance"}, 4000)  # by name
    assert not err and "出席率" in text
    text, err = tools.call(conn, "run_analysis", {"dataset_id": "asean-attendance", "method": "ols",
                                                  "params": {"y": "出席率", "x": ["联盟强度"]}}, 4000)
    assert not err and "OLS on 《asean-attendance》" in text and "link: [在数据页查看](/data/runs/RUN-" in text
    text, err = tools.call(conn, "run_analysis", {"dataset_id": "asean-attendance", "method": "ols", "params": {"y": "不存在"}}, 4000)
    assert err and "method keys and params" in text
    text, _ = tools.call(conn, "read_pages", {"paper_id": pid, "start": 1, "end": 6}, 200)
    assert len(text) <= 200 and text.endswith("characters]")


def test_execute_tool_enforces_skill_budget(conn, sample_pdf):
    from research_lab.agent import skills, tasks
    offline_paper(conn, sample_pdf)
    task_id = tasks.create(conn, "翻译这篇", skill="translate")
    sk = skills.get("translate")
    ctx = tasks.Context(task_id, sk)
    assert tasks.execute_tool(conn, ctx, "run_analysis", {})[1] is True        # not in the skill's tools
    for _ in range(sk.max_tool_calls):
        assert tasks.execute_tool(conn, ctx, "list_papers", {})[1] is False
    text, err = tasks.execute_tool(conn, ctx, "list_papers", {})
    assert err and "budget" in text
    ctx2 = tasks.Context(task_id, sk)
    tasks.execute_tool(conn, ctx2, "ask_user", {"question": "是哪一篇？"})
    assert ctx2.question == "是哪一篇？"
    assert [e["text"] for e in tasks.events(conn, task_id)][:2] == ["浏览文献库", "浏览文献库"]


# ---------------------------------------------------------------- MCP endpoint

def test_mcp_endpoint(client):
    from research_lab.agent import skills, tasks
    from research_lab.db.connection import connect
    conn = connect()
    task_id = tasks.create(conn, "跑个回归", skill="analysis")
    conn.close()
    ctx = tasks.Context(task_id, skills.get("analysis"))
    tasks.ACTIVE[ctx.token] = ctx
    try:
        no_hx = {"HX-Request": "false"}  # Claude Code sends no HTMX header and no Origin
        assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}, headers=no_hx).status_code == 401
        auth = {**no_hx, "Authorization": f"Bearer {ctx.token}"}
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                        headers=auth)
        assert r.status_code == 200 and r.json()["result"]["capabilities"] == {"tools": {"listChanged": False}}
        assert client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=auth).status_code == 202
        names = {t["name"] for t in client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                                                 headers=auth).json()["result"]["tools"]}
        assert names == set(skills.get("analysis").tools)
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                      "params": {"name": "list_datasets", "arguments": {}}}, headers=auth).json()["result"]
        assert r["isError"] is False and "no datasets" in r["content"][0]["text"]
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                      "params": {"name": "read_pages", "arguments": {}}}, headers=auth).json()["result"]
        assert r["isError"] is True  # not an analysis tool
    finally:
        tasks.ACTIVE.pop(ctx.token, None)


# ---------------------------------------------------------------- drivers

def deepseek_transport(seen: list[dict]):
    replies = iter([
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "list_papers", "arguments": "{}"}}]}}],
         "usage": {"prompt_tokens": 300, "prompt_cache_hit_tokens": 100, "completion_tokens": 20}},
        {"choices": [{"message": {"content": "## 结论\n文献库是空的。"}}],
         "usage": {"prompt_tokens": 400, "prompt_cache_hit_tokens": 300, "completion_tokens": 30}},
        {"choices": [{"message": {"content": "好的，继续。"}}], "usage": {"prompt_tokens": 500, "completion_tokens": 10}},
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=next(replies))
    return httpx.MockTransport(handler)


def test_deepseek_driver_runs_tools_and_follow_up(conn, monkeypatch):
    from research_lab.agent import tasks
    from research_lab.ai import dispatch
    from research_lab.db.connection import set_setting
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    seen: list[dict] = []
    monkeypatch.setattr(dispatch, "_TRANSPORT", deepseek_transport(seen))
    task_id = tasks.create(conn, "我的文献库里有什么？")
    tasks.run(task_id)
    t = tasks.get(conn, task_id)
    assert t["status"] == "done" and t["engine"] == "deepseek" and "文献库是空的" in t["report"]
    assert (t["input_tokens"], t["cache_tokens"], t["output_tokens"]) == (300, 400, 50)
    assert [e["text"] for e in tasks.events(conn, task_id)] == ["浏览文献库"]
    assert seen[0]["tools"][0]["type"] == "function" and seen[0]["model"] == "deepseek-flash"
    assert seen[1]["messages"][-1]["role"] == "tool"
    follow = tasks.create(conn, "那帮我导入一篇", parent_id=task_id)
    tasks.run(follow)
    assert tasks.get(conn, follow)["status"] == "done"
    replayed = seen[2]["messages"]
    assert any(m.get("role") == "tool" for m in replayed) and replayed[-1] == {"role": "user", "content": "那帮我导入一篇"}
    set_setting(conn, "ai.mode", "agent")
    t3 = tasks.create(conn, "随便问问")
    tasks.run(t3)
    assert tasks.get(conn, t3)["status"] == "failed"


def test_claude_driver_command_session_and_resume(conn, tmp_path, monkeypatch):
    from research_lab.agent import drivers, tasks
    from research_lab.db.connection import get_setting
    cli, argv_log = fake_claude(tmp_path)
    monkeypatch.setenv("RL_CLAUDE_BIN", str(cli))
    task_id = tasks.create(conn, "帮我跑个回归")
    tasks.run(task_id)
    t = tasks.get(conn, task_id)
    assert t["status"] == "done" and t["engine"] == "claude" and t["model"] == "sonnet" and t["session_id"] == "sess-1"
    assert (t["input_tokens"], t["cache_tokens"], t["output_tokens"]) == (150, 400, 80)
    call = json.loads(argv_log.read_text().splitlines()[0])
    argv = call["argv"]
    assert call["stdin"] == "帮我跑个回归"
    assert argv[argv.index("--setting-sources") + 1] == "project" and argv[argv.index("--tools") + 1] == ""
    assert "--safe-mode" not in argv  # it would switch off MCP
    mcp = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["lab"]
    assert mcp["url"] == drivers.BASE_URL["value"] + "/mcp" and mcp["headers"]["Authorization"].startswith("Bearer ")
    assert argv[argv.index("--allowedTools") + 1].split(",")[0] == "mcp__lab__list_datasets"
    assert get_setting(conn, "engine.claude.rate_limits", {})["seven_day"] == 0.5
    follow = tasks.create(conn, "再加上贸易依存", parent_id=task_id)
    tasks.run(follow)
    second = json.loads(argv_log.read_text().splitlines()[1])
    assert second["argv"][second["argv"].index("--resume") + 1] == "sess-1" and second["stdin"] == "再加上贸易依存"


def test_chat_free_text_becomes_task(client, tmp_path, monkeypatch):
    from research_lab.agent import tasks
    cli, _ = fake_claude(tmp_path, report="## 结论\n这是任务报告。")
    monkeypatch.setenv("RL_CLAUDE_BIN", str(cli))
    monkeypatch.setattr(tasks.RUNNER, "submit", tasks.run)  # run inline instead of on the runner thread
    from research_lab.ai import claude_code
    monkeypatch.setattr(claude_code.WORKER, "hosted", True)  # as in the app (tests set RL_AI_WORKER=0)
    r = client.post("/chat/send", data={"text": "看看这周该读什么"})
    assert r.status_code in (200, 204)
    from research_lab.db.connection import connect
    conn = connect()
    t = conn.execute("SELECT * FROM agent_tasks").fetchone()
    conn.close()
    assert t["status"] == "done" and t["skill"] == "task"
    page = client.get(f"/chat/{t['thread_id']}").text
    assert "这是任务报告" in page and "已完成" in page and "任务管理" in page


def test_follow_up_reroutes_when_text_matches_another_skill(conn):
    from research_lab.agent import tasks
    first = tasks.create(conn, "跑个回归看看")
    assert tasks.get(conn, first)["skill"] == "analysis"
    conn.execute("UPDATE agent_tasks SET status = 'done' WHERE id = ?", (first,))
    assert tasks.get(conn, tasks.create(conn, "好的，谢谢", parent_id=first))["skill"] == "analysis"
    assert tasks.get(conn, tasks.create(conn, "这篇的研究设计和变量呢", parent_id=first))["skill"] == "methods"
