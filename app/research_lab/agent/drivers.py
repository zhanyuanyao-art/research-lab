"""Engine drivers for the task agent. Same skills, same tools, same task records; only transport differs.

- `ClaudeCodeDriver`: the signed-in `claude` CLI, headless. The lab tools arrive over the local MCP
  endpoint with a per-task bearer token; built-in tools, the user's settings, hooks and plugins are
  off and the default Claude Code system prompt is replaced by the skill's (a bare run costs ~500
  input tokens instead of several thousand). Follow-ups resume the session.
- `DeepSeekDriver`: OpenAI-compatible function calling against DeepSeek, executing the same tools
  in-process. Follow-ups replay the stored message history.

`active_engine` follows the Settings choice (queue.mode), so switching engines needs no other change.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any

from ..ai import claude_code, queue
from ..ai.provider import ChatRequest
from . import skills, tools
from .tasks import Context, add_event, execute_tool, system_prompt

log = logging.getLogger("research_lab.agent.drivers")

MCP_NAME = "lab"
TASK_TIMEOUT_S = 900
BASE_URL = {"value": "http://127.0.0.1:4100"}   # set by create_app to the port actually served


@dataclass
class Outcome:
    report: str = ""
    error: str | None = None
    session_id: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


def active_engine(conn) -> str | None:
    m = queue.mode(conn)
    if m == "claude" and claude_code.find_cli(conn) and claude_code.under_cap(conn):
        return "claude"
    if m == "deepseek":
        from ..ai.dispatch import make_provider
        return "deepseek" if make_provider(conn) else None
    return None


def _fresh_prompt(task, parent) -> str:
    """The user's request, prefixed with the previous exchange when the session cannot be resumed."""
    if parent is None:
        return task["prompt"]
    prev = (parent["report"] or "")[:3000]
    q = f"\nYou asked: {parent['question']}" if parent["question"] else ""
    return f"Earlier in this conversation the user asked: {parent['prompt']}\nYour report was:\n{prev}{q}\n\nThe user now says: {task['prompt']}"


class ClaudeCodeDriver:
    def __init__(self, conn):
        self.conn = conn
        self.cli = claude_code.find_cli(conn)

    def command(self, ctx: Context, model: str, resume: str | None) -> list[str]:
        mcp = {"mcpServers": {MCP_NAME: {"type": "http", "url": BASE_URL["value"] + "/mcp",
                                         "headers": {"Authorization": f"Bearer {ctx.token}"}}}}
        allowed = ",".join(f"mcp__{MCP_NAME}__{t}" for t in ctx.skill.tools)
        # --safe-mode would also switch off MCP, so user customisations are excluded with
        # --setting-sources project instead (cwd is the data folder, which has no project settings).
        cmd = [self.cli, "-p", "--output-format", "stream-json", "--verbose", "--setting-sources", "project", "--tools", "",
               "--strict-mcp-config", "--mcp-config", json.dumps(mcp), "--allowedTools", allowed,
               "--permission-mode", "dontAsk", "--system-prompt", system_prompt(ctx.skill)]
        if model:
            cmd += ["--model", model]
        if resume:
            cmd += ["--resume", resume]
        return cmd

    def run(self, task, ctx: Context, model: str, parent, procs: dict) -> Outcome:
        resume = parent["session_id"] if parent is not None and parent["engine"] == "claude" else None
        prompt = task["prompt"] if resume else _fresh_prompt(task, parent)
        from ..research.store import prompt_context
        from .tasks import page_context
        prompt += prompt_context(self.conn, ctx.task_id) + page_context(self.conn, ctx.task_id)
        from ..config import get_paths
        proc = subprocess.Popen(self.command(ctx, model, resume), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, cwd=get_paths().data, env=claude_code.child_env())
        procs[ctx.task_id] = proc
        out = Outcome()
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "system" and ev.get("subtype") == "init":
                    out.session_id = ev.get("session_id")
                    lab = next((s for s in ev.get("mcp_servers") or [] if s.get("name") == MCP_NAME), None)
                    if lab and lab.get("status") not in ("connected", None):
                        add_event(self.conn, ctx.task_id, "error", f"工具连接失败：{lab.get('status')}")
                elif ev.get("type") == "rate_limit_event":
                    claude_code.record_rate_limits(ev.get("rate_limit_info") or {})
                elif ev.get("type") == "result":
                    out.session_id = ev.get("session_id") or out.session_id
                    out.report = (ev.get("result") or "").strip()
                    u = ev.get("usage") or {}
                    out.usage = {"input_tokens": (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
                                 "cache_tokens": u.get("cache_read_input_tokens") or 0,
                                 "output_tokens": u.get("output_tokens") or 0}
                    if ev.get("is_error"):
                        out.error = out.report or ev.get("subtype") or "Claude Code error"
            proc.wait(timeout=TASK_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            out.error = "任务超时（15 分钟）"
        if proc.returncode not in (0, None) and not out.report and not out.error:
            err = (proc.stderr.read() if proc.stderr else "") or f"exit {proc.returncode}"
            out.error = f"Claude Code: {err.strip()[:500]}"
        return out


class DeepSeekDriver:
    def __init__(self, conn):
        from ..ai.dispatch import make_provider
        self.conn = conn
        self.provider = make_provider(conn)

    @staticmethod
    def functions(skill: skills.Skill) -> list[dict[str, Any]]:
        return [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.schema}}
                for name in skill.tools if (t := tools.TOOLS.get(name))]

    def run(self, task, ctx: Context, model: str, parent, procs: dict) -> Outcome:
        history: list[dict[str, Any]] = []
        if parent is not None and parent["engine"] == "deepseek" and parent["history_json"]:
            history = json.loads(parent["history_json"])
            user = task["prompt"]
        else:
            user = _fresh_prompt(task, parent)
        from ..research.store import prompt_context
        from .tasks import page_context
        user += prompt_context(self.conn, ctx.task_id) + page_context(self.conn, ctx.task_id)
        messages = [{"role": "system", "content": system_prompt(ctx.skill)}, *history, {"role": "user", "content": user}]
        fns = self.functions(ctx.skill)
        usage = {"input_tokens": 0, "cache_tokens": 0, "output_tokens": 0}
        out = Outcome()
        for _round in range(ctx.skill.max_tool_calls + 2):
            budget_left = ctx.calls < ctx.skill.max_tool_calls and ctx.question is None
            params = {"tools": fns, "tool_choice": "auto"} if budget_left and fns else {}
            req = ChatRequest(messages=messages, model=model, max_tokens=ctx.skill.max_output_tokens, json_mode=False,
                              thinking=None, temperature=0.2, params=params)
            res = self.provider.complete(req)
            usage["input_tokens"] += res.usage.get("prompt_tokens", 0) - res.usage.get("cache_hit_tokens", 0)
            usage["cache_tokens"] += res.usage.get("cache_hit_tokens", 0)
            usage["output_tokens"] += res.usage.get("completion_tokens", 0)
            msg = ((res.raw.get("choices") or [{}])[0].get("message") or {}) if res.raw else {"content": res.content}
            calls = msg.get("tool_calls") or []
            if not calls:
                out.report = (msg.get("content") or "").strip()
                messages.append({"role": "assistant", "content": out.report})
                break
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for call in calls:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                text, _err = execute_tool(self.conn, ctx, fn.get("name") or "", args)
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": text})
            if ctx.question:
                messages.append({"role": "user", "content": "Stop here: give a one-paragraph status in Chinese; your question is shown to the user."})
        out.history = messages[1:]
        out.usage = usage
        return out


DRIVERS = {"claude": ClaudeCodeDriver, "deepseek": DeepSeekDriver}
