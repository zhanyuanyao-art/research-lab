"""Token use per skill and engine, from structured job runs and agent tasks: the numbers to compare
before moving work from Claude Code to DeepSeek."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import skills


def by_skill(conn: sqlite3.Connection, days: int = 7) -> dict[str, dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    out: dict[str, dict[str, Any]] = {s: {"runs": 0, "input": 0, "cached": 0, "output": 0, "engines": set()}
                                      for s in skills.load_all()}
    rows = conn.execute("SELECT j.kind, r.provider, r.prompt_tokens, r.cache_hit_tokens, r.completion_tokens "
                        "FROM ai_runs r JOIN ai_jobs j ON j.id = r.job_id WHERE substr(r.started_at, 1, 10) >= ?", (since,))
    for kind, provider, inp, cached, outp in rows:
        sk = skills.for_job(kind)
        d = out.setdefault(sk.name if sk else "other", {"runs": 0, "input": 0, "cached": 0, "output": 0, "engines": set()})
        d["runs"] += 1
        d["input"] += inp or 0
        d["cached"] += cached or 0
        d["output"] += outp or 0
        d["engines"].add("Claude Code" if provider == "claude-code" else "DeepSeek")
    try:
        trows = conn.execute("SELECT skill, engine, input_tokens, cache_tokens, output_tokens FROM agent_tasks "
                             "WHERE started_at IS NOT NULL AND substr(started_at, 1, 10) >= ?", (since,)).fetchall()
    except sqlite3.Error:
        trows = []
    for skill, engine, inp, cached, outp in trows:
        d = out.setdefault(skill, {"runs": 0, "input": 0, "cached": 0, "output": 0, "engines": set()})
        d["runs"] += 1
        d["input"] += (inp or 0) + (cached or 0)
        d["cached"] += cached or 0
        d["output"] += outp or 0
        d["engines"].add("Claude Code" if engine == "claude" else "DeepSeek")
    return out
