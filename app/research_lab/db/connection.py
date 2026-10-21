"""SQLite connection, migrations and the settings table."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import get_paths


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    paths = get_paths()
    path = db_path or paths.db
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def migrate(conn: sqlite3.Connection, migrations_dir: Path | None = None) -> list[int]:
    """Apply numbered .sql files not yet recorded in schema_migrations."""
    mdir = migrations_dir or get_paths().migrations
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    applied: list[int] = []
    for f in sorted(mdir.glob("*.sql")):
        m = re.match(r"(\d+)_", f.name)
        if not m:
            continue
        version = int(m.group(1))
        if version in done:
            continue
        # executescript commits any open transaction first, so it cannot sit inside BEGIN/COMMIT.
        conn.executescript(f.read_text())
        conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (version, now_iso()))
        applied.append(version)
    return applied


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, json.dumps(value), now_iso()),
    )


def next_sequence_id(conn: sqlite3.Connection, table: str, prefix: str) -> str:
    """EXC-2026-0001 style ids: prefix-YEAR-NNNN, max existing + 1 within the year."""
    year = datetime.now().year
    like = f"{prefix}-{year}-%"
    row = conn.execute(f"SELECT MAX(id) FROM {table} WHERE id LIKE ?", (like,)).fetchone()
    last = int(row[0].rsplit("-", 1)[1]) if row and row[0] else 0
    return f"{prefix}-{year}-{last + 1:04d}"


def backup(conn: sqlite3.Connection, keep: int = 14) -> Path:
    paths = get_paths()
    paths.backups.mkdir(parents=True, exist_ok=True)
    target = paths.backups / f"lab-{datetime.now():%Y%m%d}.sqlite"
    dest = sqlite3.connect(target)
    with dest:
        conn.backup(dest)
    dest.close()
    olds = sorted(paths.backups.glob("lab-*.sqlite"))
    for old in olds[:-keep]:
        old.unlink()
    return target
