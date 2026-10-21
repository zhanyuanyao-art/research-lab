"""Which process runs AI work: a lease in the database, renewed by the holder's worker sweep.

The desktop app, `rl serve` and worktree servers can all point at one database. Only one of them
should spend Claude Code runs or DeepSeek credit on the queue, or the daily cap is split and nobody can
tell who ran what (card 0001). A process that can run an engine takes the lease when it is free or
stale and keeps it by renewing it every sweep; the others leave jobs and tasks queued for it. A holder
that dies stops renewing, and after TTL seconds the next process to sweep takes over.
"""
from __future__ import annotations

import os
import socket
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

NAME = "ai"
TTL = 150                       # seconds; the worker sweeps every 60
PORT: int | None = None         # set by create_app, shown on the 开发 page


def me() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def acquire(conn: sqlite3.Connection, name: str = NAME, now: datetime | None = None) -> bool:
    """Take or renew the lease. True when this process holds it afterwards."""
    t = _now(now)
    stamp = t.isoformat(timespec="seconds")
    cutoff = (t - timedelta(seconds=TTL)).isoformat(timespec="seconds")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT holder, renewed_at FROM worker_lease WHERE name = ?", (name,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO worker_lease(name, holder, pid, port, acquired_at, renewed_at) VALUES (?,?,?,?,?,?)",
                         (name, me(), os.getpid(), PORT, stamp, stamp))
            ok = True
        elif row["holder"] == me():
            conn.execute("UPDATE worker_lease SET renewed_at = ?, port = ? WHERE name = ?", (stamp, PORT, name))
            ok = True
        elif row["renewed_at"] < cutoff:
            conn.execute("UPDATE worker_lease SET holder = ?, pid = ?, port = ?, acquired_at = ?, renewed_at = ? WHERE name = ?",
                         (me(), os.getpid(), PORT, stamp, stamp, name))
            ok = True
        else:
            ok = False
        conn.execute("COMMIT")
        return ok
    except Exception:
        conn.execute("ROLLBACK")
        raise


def holds(conn: sqlite3.Connection, name: str = NAME, now: datetime | None = None) -> bool:
    """This process holds a live lease (without renewing it)."""
    row = conn.execute("SELECT holder, renewed_at FROM worker_lease WHERE name = ?", (name,)).fetchone()
    if row is None or row["holder"] != me():
        return False
    cutoff = (_now(now) - timedelta(seconds=TTL)).isoformat(timespec="seconds")
    return row["renewed_at"] >= cutoff


def release(conn: sqlite3.Connection, name: str = NAME) -> None:
    conn.execute("DELETE FROM worker_lease WHERE name = ? AND holder = ?", (name, me()))


def current(conn: sqlite3.Connection, name: str = NAME, now: datetime | None = None) -> dict[str, Any] | None:
    """The lease as the 开发 page shows it: who, since when, last renewal, and whether it is live."""
    row = conn.execute("SELECT * FROM worker_lease WHERE name = ?", (name,)).fetchone()
    if row is None:
        return None
    cutoff = (_now(now) - timedelta(seconds=TTL)).isoformat(timespec="seconds")
    return {**dict(row), "live": row["renewed_at"] >= cutoff, "mine": row["holder"] == me()}


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
