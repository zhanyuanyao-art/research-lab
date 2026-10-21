"""Claude Code as the assistant's engine: the signed-in `claude` CLI on this Mac, run headless.

Structured jobs (summaries, translations, associations, …) go through `ClaudeCodeProvider`, which
plays the same role as the DeepSeek provider: one prompt in, one JSON reply out, no tools. A single
background worker drains the queue so a long translation never starts a dozen CLI processes at once.

The CLI runs with `--safe-mode`: the user's own hooks, MCP servers, memory and CLAUDE.md stay out of
these runs, while their Claude login keeps working. No API key is involved.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Empty, Queue

from ..db.connection import connect, get_setting, now_iso
from ..tunables import tunable
from .provider import ChatRequest, ChatResult

log = logging.getLogger("research_lab.ai.claude_code")

NAME = "claude-code"
DEFAULT_MODEL = "sonnet"
DEFAULT_DAILY_RUNS = 200
TIMEOUT_S = 900
# A job that does not ask for thinking (every feed batch, translation chunk and summary today): with the CLI's
# default thinking, haiku feed batches spent 85–92 % of their output on it and ran 2–3 minutes (10-01 runs);
# without it the same work takes seconds, so a run still going after this long is stuck.
LIGHT_TIMEOUT_S = 300
# No output line at all for this long ends the run. With --include-partial-messages tokens stream as they
# come, so silence means the CLI is waiting on something (on 10-01 it sat out a usage limit for the full 900 s).
IDLE_S = 120
STALL_PAUSE_MIN = 15      # how long the engine rests after a stuck run
LIMIT_FALLBACK_MIN = 30   # a usage limit without a reset time
MAX_STALLS = 3            # a job deferred this many times (counting attempts) is marked failed instead
EXIT_GRACE_S = 5          # after the result line, how long the CLI gets to exit before it is killed
STALE_MINUTES = 30
BLOCK_KEY = "engine.claude.blocked"
# Only the subscription's own wording: "Context limit reached", "rate limit of the tool" and the like are ordinary
# errors that would otherwise requeue forever.
_LIMIT_TEXT = re.compile(r"usage limit|hit your (?:usage |session |weekly )?limit|out of (?:extra )?usage|"
                         r"(?:5|five)[- ]hour limit|weekly limit", re.I)
_CANDIDATES = ("~/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude", "~/.claude/local/claude")


def find_cli(conn=None) -> str | None:
    override = os.environ.get("RL_CLAUDE_BIN")
    if override is not None:  # "none" disables the engine (tests); a path pins the binary
        return None if override in ("", "none") else (override if os.access(override, os.X_OK) else None)
    configured = get_setting(conn, "ai.claude_bin", None) if conn is not None else None
    for c in ([configured] if configured else []) + [shutil.which("claude") or ""] + list(_CANDIDATES):
        if c and os.access(os.path.expanduser(c), os.X_OK):
            return os.path.expanduser(c)
    return None


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    # A run started from inside a Claude Code session must not look like a nested session.
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
        env.pop(k, None)
    extra = [os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    env["PATH"] = os.pathsep.join([p for p in extra if p not in env.get("PATH", "")] + [env.get("PATH", "")])
    return env


class Deferred(RuntimeError):
    """The run could not finish now; the job goes back to the queue (queue.defer). When `blocks_engine`, the
    engine rests until `until` (UTC) and the worker's sweep picks the queue up after; otherwise only this
    job waits for the next sweep. Not a failure of the job, up to MAX_STALLS counted attempts."""

    def __init__(self, until: datetime, why: str, *, counts_attempt: bool = True, blocks_engine: bool = True):
        super().__init__(why)
        self.until, self.why = until, why
        self.counts_attempt, self.blocks_engine = counts_attempt, blocks_engine


class RateLimited(Deferred):
    """The subscription refused the run. Only the structured refusal (a rejected rate_limit_event) is free; a
    limit read from error text counts as an attempt, so a misread error cannot requeue forever."""

    def __init__(self, until: datetime, detail: str = "", *, structured: bool = True):
        local = until.astimezone().strftime("%H:%M")
        super().__init__(until, f"Claude Code 额度用完，{local} 后自动继续" + (f"（{detail[:120]}）" if detail else ""),
                         counts_attempt=not structured)


class Stalled(Deferred):
    """The CLI went silent for IDLE_S (the engine rests: it is waiting on something outside this job), or kept
    producing output past its timeout (only this job waits: the job is too big, not the engine stuck)."""

    def __init__(self, seconds: int, *, silent: bool = True):
        until = datetime.now(timezone.utc) + timedelta(minutes=STALL_PAUSE_MIN)
        what = f"{seconds} 秒没有任何输出，已中止；{until.astimezone().strftime('%H:%M')} 后再试" if silent \
            else f"跑了 {seconds} 秒还没完成，已中止，下一轮再试"
        super().__init__(until, "Claude Code " + what, blocks_engine=silent)


def _overage_allowed(info: dict) -> bool:
    """A refusal of the plan's own window while extra usage is on: the CLI keeps going on overage."""
    return bool(info.get("isUsingOverage")) or str(info.get("overageStatus") or "") in ("allowed", "allowed_warning")


def _reset_time(info: dict) -> datetime:
    try:
        return datetime.fromtimestamp(int(info["resetsAt"]), timezone.utc)
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        return datetime.now(timezone.utc) + timedelta(minutes=LIMIT_FALLBACK_MIN)


def _limit_from_text(text: str) -> RateLimited | None:
    """A usage-limit refusal reported in the result text (older CLIs append `|<epoch>`)."""
    if not _LIMIT_TEXT.search(text or ""):
        return None
    m = re.search(r"\|(\d{10})\b", text)
    return RateLimited(_reset_time({"resetsAt": m.group(1)} if m else {}), text.strip().splitlines()[0] if text.strip() else "",
                       structured=False)


def _prompt(req: ChatRequest) -> tuple[str, str]:
    """(system prompt, user prompt). Multi-turn requests (the runner's repair retry) become a transcript."""
    system = "\n\n".join(m["content"] for m in req.messages if m["role"] == "system")
    turns = [m for m in req.messages if m["role"] != "system"]
    if len(turns) == 1:
        return system, turns[0]["content"]
    lines = []
    for m in turns:
        lines.append(f"### {'Your earlier reply' if m['role'] == 'assistant' else 'Request'}\n{m['content']}")
    return system, "\n\n".join(lines)


class ClaudeCodeProvider:
    name = NAME

    def __init__(self, cli: str, model: str = DEFAULT_MODEL, cwd: Path | None = None, timeout: int = TIMEOUT_S,
                 idle: float = IDLE_S):
        self.cli, self.default_model, self.cwd, self.timeout, self.idle = cli, model, cwd, timeout, idle

    def command(self, system: str, model: str) -> list[str]:
        cmd = [self.cli, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages", "--safe-mode",
               "--tools", "", "--no-session-persistence"]
        if system:
            cmd += ["--system-prompt", system]
        if model:
            cmd += ["--model", model]
        return cmd

    def complete(self, req: ChatRequest) -> ChatResult:
        """Run the CLI and read its stream as it comes, so a usage-limit refusal or a silent CLI ends the run
        at once (Deferred) instead of holding the worker until the timeout."""
        system, prompt = _prompt(req)
        model = req.model if req.model and not req.model.startswith("deepseek") else self.default_model
        env = child_env()
        if not req.thinking:
            env["MAX_THINKING_TOKENS"] = "0"
        timeout = self.timeout if req.thinking else min(self.timeout, LIGHT_TIMEOUT_S)
        proc = subprocess.Popen(self.command(system, model), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, cwd=self.cwd, env=env, start_new_session=True)
        lines: Queue[str | None] = Queue()
        err: list[str] = []

        def pump() -> None:
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        def feed() -> None:
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

        threads = [threading.Thread(target=f, daemon=True) for f in (pump, feed, lambda: err.append(proc.stderr.read()))]
        for t in threads:
            t.start()
        start, out, plain = time.monotonic(), {}, []
        try:
            while True:
                left = timeout - (time.monotonic() - start)
                if left <= 0:
                    raise Stalled(timeout, silent=False)
                try:
                    line = lines.get(timeout=min(self.idle, left))
                except Empty:
                    if time.monotonic() - start >= timeout:
                        raise Stalled(timeout, silent=False) from None
                    raise Stalled(int(self.idle)) from None
                if line is None:
                    break
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    plain.append(line)
                    continue
                if ev.get("type") == "rate_limit_event":
                    info = ev.get("rate_limit_info") or {}
                    record_rate_limits(info)
                    if info.get("status") == "rejected" and not _overage_allowed(info):
                        raise RateLimited(_reset_time(info), str(info.get("rateLimitType") or ""))
                elif ev.get("type") == "result" or "result" in ev:
                    out = ev
                    break   # the answer is in; a CLI that lingers after it must not turn this into a stall
        except BaseException:
            _kill(proc)
            raise
        try:
            code = proc.wait(timeout=EXIT_GRACE_S)
        except subprocess.TimeoutExpired:
            _kill(proc)
            code = 0 if out and not out.get("is_error") else proc.returncode
        threads[2].join(timeout=2)
        if code != 0 or out.get("is_error") or "result" not in out:
            detail = str(out.get("result") or "").strip() or "".join(err).strip() or "".join(plain).strip() or f"exit {code}"
            limited = _limit_from_text(detail)
            if limited:
                raise limited
            raise RuntimeError(f"Claude Code: {detail[:500]}")
        u = out.get("usage") or {}
        # Same meaning as DeepSeek's usage: prompt_tokens is all input, cache_hit_tokens the cached part.
        cached = u.get("cache_read_input_tokens") or 0
        usage = {"prompt_tokens": (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + cached,
                 "cache_hit_tokens": cached, "completion_tokens": u.get("output_tokens") or 0}
        used = next(iter(out.get("modelUsage") or {}), model)
        return ChatResult(content=out["result"], model=used, usage=usage, raw=out)


def _kill(proc: subprocess.Popen) -> None:
    """The CLI and anything it started (it runs in its own session)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()
    proc.wait()


def configured_model(conn) -> str:
    from ..agent import skills
    return skills.engine_model(conn, "claude", "standard") or DEFAULT_MODEL


def record_rate_limits(info: dict) -> None:
    """Keep the subscription's usage windows the CLI reports, for Settings."""
    windows = info.get("unifiedWindows") or {}
    if not windows:
        return
    from ..db.connection import set_setting
    conn = connect()
    try:
        set_setting(conn, "engine.claude.rate_limits", {
            "five_hour": (windows.get("five_hour") or {}).get("utilization"),
            "seven_day": (windows.get("seven_day") or {}).get("utilization"),
            "status": info.get("status"), "at": now_iso()})
    finally:
        conn.close()


def block(conn, until: datetime, why: str) -> None:
    """Rest the engine until `until`: dispatch.route queues new work meanwhile and the sweep resumes after."""
    from ..db.connection import set_setting
    set_setting(conn, BLOCK_KEY, {"until": until.astimezone(timezone.utc).isoformat(timespec="seconds"), "why": why})


def blocked(conn) -> dict | None:
    """{'until', 'why'} while the engine rests after a usage limit or a stuck run, else None."""
    b = get_setting(conn, BLOCK_KEY, None)
    try:
        if b and datetime.fromisoformat(b["until"]) > datetime.now(timezone.utc):
            return b
    except (KeyError, TypeError, ValueError):
        pass
    return None


def daily_runs(conn) -> int:
    return tunable(conn, "ai.claude_daily_runs")   # range-checked, so the settings page and the cap agree


def runs_today(conn) -> int:
    """Structured job runs plus agent tasks on Claude Code since 00:00 UTC."""
    day = now_iso()[:10]
    jobs = conn.execute("SELECT COUNT(*) FROM ai_runs WHERE provider = ? AND substr(started_at, 1, 10) = ?",
                        (NAME, day)).fetchone()[0]
    try:
        tasks = conn.execute("SELECT COUNT(*) FROM agent_tasks WHERE engine = 'claude' AND substr(started_at, 1, 10) = ?",
                             (day,)).fetchone()[0]
    except Exception:  # noqa: BLE001 - databases migrated before 0012
        tasks = 0
    return jobs + tasks


def under_cap(conn) -> bool:
    return runs_today(conn) < daily_runs(conn)


def make_provider(conn) -> ClaudeCodeProvider | None:
    from ..config import get_paths
    cli = find_cli(conn)
    if not cli:
        return None
    return ClaudeCodeProvider(cli, configured_model(conn), cwd=get_paths().data)


# ---------------------------------------------------------------- the worker

KEEP_EVERY = 30   # seconds between lease renewals while this process holds it (lease.TTL is 150)


class Worker:
    """One background thread that runs queued jobs on Claude Code, oldest first.

    `kick()` wakes it after an enqueue; it also sweeps the queue every `interval` seconds, so jobs
    queued while the app was closed (or by the CLI) are picked up too.
    """

    def __init__(self, interval: float = 60.0, hosted: bool = False):
        self.interval = interval
        # Only a long-running app process hosts the worker. A CLI command that queues a job exits
        # within seconds and would orphan the run; the app's next sweep picks the job up instead.
        self.hosted = hosted
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._pending: deque[str] = deque()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._keeper: threading.Thread | None = None
        self.current: str | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="claude-code-worker")
            self._thread.start()
            if not (self._keeper and self._keeper.is_alive()):
                self._keeper = threading.Thread(target=self._keep, daemon=True, name="ai-lease-keeper")
                self._keeper.start()

    def stop(self, timeout: float = 5.0) -> None:
        # Finish the current job, then release both threads before their test DB disappears.
        self._stop.set()
        self._wake.set()
        deadline = time.monotonic() + timeout
        for thread in (self._thread, self._keeper):
            if thread is not None and thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))

    def kick(self, job_id: str | None = None) -> None:
        if not self.hosted:
            return
        if job_id:
            with self._lock:
                if job_id not in self._pending:
                    self._pending.append(job_id)
        self.start()
        self._wake.set()

    def _next(self, conn, skip: set[str] = frozenset()) -> str | None:
        with self._lock:
            while self._pending:
                jid = self._pending.popleft()
                row = conn.execute("SELECT status FROM ai_jobs WHERE id = ?", (jid,)).fetchone()
                if row and row[0] == "queued" and jid not in skip:
                    return jid
        marks = ",".join("?" * len(skip))
        row = conn.execute("SELECT id FROM ai_jobs WHERE status = 'queued'" + (f" AND id NOT IN ({marks})" if skip else "")
                           + " ORDER BY created_at, id LIMIT 1", tuple(skip)).fetchone()
        return row[0] if row else None

    def _loop(self) -> None:
        while not self._stop.is_set():
            conn = connect()
            try:
                self._sweep(conn)
            except Exception:  # noqa: BLE001 - never let the worker thread die
                log.exception("Claude Code worker loop error")
            finally:
                conn.close()
            self._wake.wait(self.interval)
            self._wake.clear()

    def _sweep(self, conn) -> None:
        from . import cost, dispatch, lease, runner
        # Only the lease holder runs anything (ai/lease.py); a process that cannot run an engine right now
        # (paused, no CLI or key, cap reached) neither takes nor renews the lease.
        where = dispatch.route(conn)[0]
        if where == "queue" or not lease.acquire(conn):
            return
        from ..agent import tasks as agent_tasks
        agent_tasks.sweep(conn)
        if where == "claude":
            recover_stale(conn)
        waiting: set[str] = set()   # deferred in this drain without resting the engine: next sweep
        while not self._stop.is_set():
            # Renew before every job and stop as soon as the lease is gone, so a long drain never runs
            # beside the process that took over. Jobs queued for DeepSeek by other processes drain here too.
            where = dispatch.route(conn)[0]
            if where not in ("claude", "deepseek") or not lease.acquire(conn):
                break
            jid = self._next(conn, waiting)
            if not jid:
                break
            provider = make_provider(conn) if where == "claude" else dispatch.make_provider(conn)
            if provider is None:
                break
            # Take the job only if no one else has, so a job never runs twice.
            if conn.execute("UPDATE ai_jobs SET status = 'claimed', claimed_at = ? WHERE id = ? AND status = 'queued'",
                            (now_iso(), jid)).rowcount == 0:
                continue
            self.current = jid
            try:
                runner.run_job(conn, jid, provider, cost.cost_usd if where == "deepseek" else None)
            except LookupError:
                pass
            except Deferred as e:   # back in the queue (runner); a resting engine ends the drain, else the rest go on
                log.warning("AI job %s deferred: %s", jid, e)
                if e.blocks_engine:
                    break
                waiting.add(jid)
            except Exception:  # noqa: BLE001 - runner marked the job failed and logged the exchange
                log.exception("AI job %s failed", jid)
                time.sleep(5)
            finally:
                self.current = None

    def keep_once(self, conn) -> bool:
        """Renew the lease this process already holds, unless it can no longer run an engine. Never takes
        a free lease: that is the sweep's job. True when renewed."""
        from . import dispatch, lease
        return lease.holds(conn) and dispatch.route(conn)[0] != "queue" and lease.acquire(conn)

    def _keep(self) -> None:
        # A job can run for many minutes and an assistant task runs on its own thread: renewing from here
        # keeps the lease while either is busy (the sweep itself only renews between jobs).
        while not self._stop.wait(KEEP_EVERY):
            conn = connect()
            try:
                self.keep_once(conn)
            except Exception:  # noqa: BLE001 - a missed renewal is retried in KEEP_EVERY seconds
                log.exception("lease renewal failed")
            finally:
                conn.close()


def recover_stale(conn, minutes: int = STALE_MINUTES) -> int:
    """Re-queue jobs whose claim outlived any plausible run: the process that claimed them is gone
    (an app restart kills its worker thread mid-job). Runs only while this engine is active, so a
    human `rl agent claim` in agent mode is never taken away."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    return conn.execute("UPDATE ai_jobs SET status = 'queued', claimed_at = NULL WHERE status = 'claimed' AND claimed_at < ?",
                        (cutoff,)).rowcount


WORKER = Worker()
