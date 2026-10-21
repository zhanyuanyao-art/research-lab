"""The ai_jobs queue: enqueue, claim, complete, fail, requeue, and the Markdown view Claude Code reads."""
from __future__ import annotations

import os

import json
import sqlite3
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from ..config import get_paths, has_deepseek_key
from ..db.connection import get_setting, now_iso
from . import registry
from .provider import ChatRequest, JobSpec


def new_job_id(conn: sqlite3.Connection) -> str:
    day = datetime.now().strftime("%Y%m%d")
    row = conn.execute("SELECT MAX(id) FROM ai_jobs WHERE id LIKE ?", (f"J-{day}-%",)).fetchone()
    n = int(row[0].rsplit("-", 1)[1]) if row and row[0] else 0
    return f"J-{day}-{n + 1:04d}"


def mode(conn: sqlite3.Connection) -> str:
    """deepseek | claude | agent. auto = DeepSeek when a key exists, else the local Claude Code CLI
    when it is installed, else agent (jobs wait for a Claude Code session to claim them)."""
    m = get_setting(conn, "ai.mode", "auto")
    if m == "auto":
        if has_deepseek_key():
            return "deepseek"
        from .claude_code import find_cli
        return "claude" if find_cli(conn) else "agent"
    return m


def online(conn: sqlite3.Connection) -> bool:
    """True when new jobs run by themselves (DeepSeek or the Claude Code worker)."""
    return mode(conn) in ("deepseek", "claude")


def enqueue(conn: sqlite3.Connection, kind: str, target_id: str, *, force: bool = False) -> str | None:
    """Build the request now and store it. Returns the job id, or None if an open duplicate exists."""
    spec = registry.get(kind)
    key = None if force else spec.dedupe_key(target_id)
    if key:
        dup = conn.execute("SELECT id FROM ai_jobs WHERE dedupe_key = ? AND status IN ('queued','claimed')", (key,)).fetchone()
        if dup:
            return None
    req, files = spec.build(target_id, conn)
    job_id = new_job_id(conn)
    conn.execute(
        "INSERT INTO ai_jobs(id, kind, status, target_type, target_id, request_json, input_files_json, schema_json, dedupe_key, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (job_id, kind, "queued", spec.target_type, target_id, json.dumps(req.to_json(), ensure_ascii=False),
         json.dumps(files), json.dumps(spec.Result.model_json_schema()), key, now_iso()))
    return job_id


def get(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM ai_jobs WHERE id = ?", (job_id,)).fetchone()


def list_jobs(conn: sqlite3.Connection, status: str | None = None, kind: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
    sql, args = "SELECT * FROM ai_jobs WHERE 1=1", []
    if status:
        sql += " AND status = ?"; args.append(status)
    if kind:
        sql += " AND kind = ?"; args.append(kind)
    sql += " ORDER BY created_at DESC LIMIT ?"; args.append(limit)
    return conn.execute(sql, args).fetchall()


def latest_for(conn: sqlite3.Connection, kind: str, target_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM ai_jobs WHERE kind = ? AND target_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                        (kind, target_id)).fetchone()


def claim(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
    cur = conn.execute("UPDATE ai_jobs SET status='claimed', claimed_at=?, attempts=attempts+1 WHERE id=? AND status IN ('queued','claimed')",
                       (now_iso(), job_id))
    if cur.rowcount == 0:
        raise LookupError(f"{job_id} is not queued")
    return get(conn, job_id)


def claim_next(conn: sqlite3.Connection, kind: str | None = None) -> sqlite3.Row | None:
    sql, args = "SELECT id FROM ai_jobs WHERE status='queued'", []
    if kind:
        sql += " AND kind=?"; args.append(kind)
    row = conn.execute(sql + " ORDER BY created_at LIMIT 1", args).fetchone()
    return claim(conn, row[0]) if row else None


def start_run(conn: sqlite3.Connection, job_id: str, provider: str, model: str) -> int:
    cur = conn.execute("INSERT INTO ai_runs(job_id, provider, model, started_at, pid) VALUES (?,?,?,?,?)",
                       (job_id, provider, model, now_iso(), os.getpid()))
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, *, valid: bool, usage: dict | None = None,
               cost_usd: float = 0.0, repaired: bool = False, log_path: str | None = None, error: str | None = None) -> None:
    u = usage or {}
    conn.execute(
        "UPDATE ai_runs SET finished_at=?, prompt_tokens=?, cache_hit_tokens=?, completion_tokens=?, reasoning_tokens=?, "
        "cost_usd=?, valid=?, repaired=?, log_path=?, error=? WHERE id=?",
        (now_iso(), u.get("prompt_tokens"), u.get("cache_hit_tokens"), u.get("completion_tokens"), u.get("reasoning_tokens"),
         cost_usd, int(valid), int(repaired), log_path, error, run_id))


def validate(kind: str, data: Any) -> BaseModel:
    spec = registry.get(kind)
    return spec.Result.model_validate(data)


def complete(conn: sqlite3.Connection, job_id: str, result: dict[str, Any] | str, *, provider: str, model: str,
             usage: dict | None = None, cost_usd: float = 0.0, repaired: bool = False, log_path: str | None = None) -> int:
    """Validate against the job's schema, apply through the spec, mark done. Raises ValidationError."""
    job = get(conn, job_id)
    if job is None:
        raise LookupError(job_id)
    if job["status"] not in ("queued", "claimed"):
        raise LookupError(f"{job_id} is {job['status']}")
    spec = registry.get(job["kind"])
    data = json.loads(result) if isinstance(result, str) else result
    parsed = spec.Result.model_validate(data)          # ValidationError propagates
    run_id = start_run(conn, job_id, provider, model)
    conn.execute("BEGIN")
    try:
        spec.apply(parsed, run_id, conn, job["target_id"])
        conn.execute("UPDATE ai_jobs SET status='done', result_json=?, completed_at=?, error=NULL WHERE id=?",
                     (parsed.model_dump_json(), now_iso(), job_id))
        finish_run(conn, run_id, valid=True, usage=usage, cost_usd=cost_usd, repaired=repaired, log_path=log_path)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return run_id


def fail(conn: sqlite3.Connection, job_id: str, error: str) -> None:
    conn.execute("UPDATE ai_jobs SET status='failed', error=?, completed_at=? WHERE id=?", (error[:2000], now_iso(), job_id))


def defer(conn: sqlite3.Connection, job_id: str, note: str, *, count_attempt: bool = True) -> None:
    """Back to the queue with a note (shown on the jobs page): the engine could not run it now and the worker
    takes it again once the engine is back (claude_code.Deferred). A usage-limit refusal is not an attempt."""
    attempts = "attempts" if count_attempt else "MAX(attempts - 1, 0)"
    conn.execute(f"UPDATE ai_jobs SET status='queued', error=?, claimed_at=NULL, completed_at=NULL, attempts={attempts} "
                 "WHERE id=? AND status IN ('queued','claimed')", (note[:2000], job_id))


def requeue(conn: sqlite3.Connection, job_id: str) -> None:
    conn.execute("UPDATE ai_jobs SET status='queued', error=NULL, claimed_at=NULL, completed_at=NULL WHERE id=? AND status IN ('failed','claimed','cancelled')", (job_id,))


def cancel(conn: sqlite3.Connection, job_id: str) -> None:
    conn.execute("UPDATE ai_jobs SET status='cancelled', completed_at=? WHERE id=? AND status IN ('queued','claimed')", (now_iso(), job_id))


def request_of(job: sqlite3.Row) -> ChatRequest:
    return ChatRequest.from_json(json.loads(job["request_json"]))


def render_markdown(job: sqlite3.Row, include_schema: bool = True) -> str:
    """What a Claude Code session reads before producing result.json."""
    req = json.loads(job["request_json"])
    spec = registry.get(job["kind"])
    lines = [f"# AI job {job['id']} · {job['kind']} · {job['status']}",
             f"target: {job['target_type']} {job['target_id']}", ""]
    files = json.loads(job["input_files_json"] or "[]")
    if files:
        lines += ["## Input files (read these)", *[f"- {f}" for f in files], ""]
    for m in req["messages"]:
        lines += [f"## {m['role']}", "", m["content"], ""]
    if include_schema:
        lines += ["## Result schema (JSON)", "", "```json", job["schema_json"], "```", ""]
        ex = spec.example()
        if ex:
            lines += ["## Example result", "", "```json", json.dumps(ex, indent=2, ensure_ascii=False), "```", ""]
    lines += ["Complete with: `uv run rl agent complete %s --from-file result.json`" % job["id"]]
    return "\n".join(lines)


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {"queued": 0, "claimed": 0, "failed": 0, "done": 0}
    for r in conn.execute("SELECT status, COUNT(*) FROM ai_jobs GROUP BY status"):
        out[r[0]] = r[1]
    return out
