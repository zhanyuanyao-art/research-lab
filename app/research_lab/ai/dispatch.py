"""Route a queued job to DeepSeek (a thread per job), the local Claude Code worker (one serial
thread), or leave it in the queue for a Claude Code session to claim.

Contract for other modules: after `queue.enqueue(...)` returns a job id, call `dispatch(job_id)`.
It never blocks on the model call and never raises for routing reasons.
"""
from __future__ import annotations

import logging
import sqlite3
from threading import Thread

import httpx

from ..config import env, has_deepseek_key
from ..db.connection import connect, get_setting, now_iso
from ..tunables import tunable
from . import claude_code, cost, lease, queue, runner
from .deepseek import OpenAICompatProvider
from .pricing import DEFAULT_BASE_URL

log = logging.getLogger("research_lab.ai.dispatch")

# Tests inject an httpx.MockTransport here; production leaves it None.
_TRANSPORT: httpx.BaseTransport | None = None


def configured_model(conn: sqlite3.Connection) -> str:
    return tunable(conn, "ai.model")   # pricing.DEFAULT_MODEL unless a valid id is stored


def make_provider(conn: sqlite3.Connection, transport: httpx.BaseTransport | None = None) -> OpenAICompatProvider | None:
    key = env("DEEPSEEK_API_KEY")
    if not key:
        return None
    return OpenAICompatProvider(env("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL), key, configured_model(conn),
                                transport=transport or _TRANSPORT)


def route(conn: sqlite3.Connection) -> tuple[str, str]:
    """('deepseek' | 'claude', '') when a new job would run now, else ('queue', why)."""
    if paused(conn):
        return "queue", "AI 已暂停"
    m = queue.mode(conn)
    if m == "agent":
        return "queue", "agent mode"
    if m == "claude":
        if not claude_code.find_cli(conn):
            return "queue", "Claude Code CLI not found"
        if not claude_code.under_cap(conn):
            return "queue", f"daily limit of {claude_code.daily_runs(conn)} Claude Code runs reached"
        rest = claude_code.blocked(conn)
        if rest:
            return "queue", rest["why"]
        return "claude", ""
    if not has_deepseek_key():
        return "queue", "no DEEPSEEK_API_KEY"
    if not cost.under_cap(conn):
        return "queue", f"daily cap ${cost.daily_cap(conn):.2f} reached"
    return "deepseek", ""


def _worker(job_id: str) -> None:
    conn = connect()
    try:
        provider = make_provider(conn)
        if provider is None:
            return
        # The holder's worker sweep may already have taken this job: claim it the same way, or leave it
        # (runner's own claim also accepts 'claimed', so without this both would run it).
        if conn.execute("UPDATE ai_jobs SET status = 'claimed', claimed_at = ? WHERE id = ? AND status = 'queued'",
                        (now_iso(), job_id)).rowcount == 0:
            return
        runner.run_job(conn, job_id, provider, cost.cost_usd)
    except LookupError:
        log.info("job %s was no longer queued when the worker started", job_id)
    except Exception:  # noqa: BLE001 - runner has already marked the job failed and logged the exchange
        log.exception("AI job %s failed", job_id)
    finally:
        conn.close()


PAUSE_KEY = "ai.paused"


def paused(conn: sqlite3.Connection) -> bool:
    """设置 → 暂停全部 AI: nothing new starts (jobs stay queued, tasks wait); a run in flight finishes."""
    return bool(get_setting(conn, PAUSE_KEY, False))


def dispatch(job_id: str) -> str:
    """'ran' = started on DeepSeek in a daemon thread; 'queued' = left for Claude Code / later;
    'skipped' = unknown id or not queued; 'failed' = the worker thread could not be started."""
    conn = connect()
    try:
        job = queue.get(conn, job_id)
        if job is None or job["status"] != "queued":
            return "skipped"
        where, _why = route(conn)
    finally:
        conn.close()
    if where == "claude":
        if not claude_code.WORKER.hosted:
            return "queued"  # a CLI process: the running app's worker picks it up on its next sweep
        claude_code.WORKER.kick(job_id)
        return "ran"
    if where != "deepseek":
        return "queued"
    conn = connect()
    try:
        if not lease.acquire(conn):
            return "queued"    # another process holds the lease and runs the queue
    finally:
        conn.close()
    try:
        Thread(target=_worker, args=(job_id,), daemon=True, name=f"ai-{job_id}").start()
    except RuntimeError:
        log.exception("could not start worker for %s", job_id)
        return "failed"
    return "ran"


def run_now(job_id: str, *, force: bool = False, transport: httpx.BaseTransport | None = None) -> int:
    """Synchronous run for the CLI. Returns the ai_runs id; raises on failure (job marked failed).
    `force` ignores the daily cap but never the missing key."""
    conn = connect()
    try:
        provider = make_provider(conn, transport)
        if provider is None:
            raise RuntimeError("no DEEPSEEK_API_KEY in app/.env; process the job with `rl agent` instead")
        if not force and not cost.under_cap(conn):
            raise RuntimeError(f"daily cap ${cost.daily_cap(conn):.2f} reached (spent ${cost.spent_today(conn):.4f}); "
                               "raise ai.daily_cap_usd or pass --force")
        job = queue.get(conn, job_id)
        if job is None:
            raise LookupError(f"no job {job_id}")
        if job["status"] not in ("queued", "claimed"):
            raise LookupError(f"{job_id} is {job['status']}; requeue it first")
        return runner.run_job(conn, job_id, provider, cost.cost_usd)
    finally:
        conn.close()
