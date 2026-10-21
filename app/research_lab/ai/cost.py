"""Cost of one call from DeepSeek usage fields, today's spend, and the daily cap."""
from __future__ import annotations

import sqlite3
from datetime import datetime, time, timezone

from ..db.connection import get_setting
from .pricing import rate_for

DEFAULT_DAILY_CAP_USD = 0.50


def cost_usd(model: str, usage: dict | None, at: datetime | None = None) -> float:
    """completion_tokens already include reasoning tokens, so reasoning is not added again."""
    u = usage or {}
    prompt = int(u.get("prompt_tokens") or 0)
    hit = min(int(u.get("cache_hit_tokens") or 0), prompt)
    out = int(u.get("completion_tokens") or 0)
    r = rate_for(model, at)
    return round(((prompt - hit) * r.cache_miss + hit * r.cache_hit + out * r.output) / 1_000_000, 8)


def local_day_start_utc(now: datetime | None = None) -> str:
    local = (now or datetime.now()).astimezone()
    start = datetime.combine(local.date(), time.min, tzinfo=local.tzinfo)
    return start.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def spent_today(conn: sqlite3.Connection, now: datetime | None = None) -> float:
    """Sum of ai_runs.cost_usd since local midnight (started_at is stored as UTC ISO)."""
    row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM ai_runs WHERE started_at >= ?",
                       (local_day_start_utc(now),)).fetchone()
    return float(row[0] or 0.0)


def daily_cap(conn: sqlite3.Connection) -> float:
    try:
        return float(get_setting(conn, "ai.daily_cap_usd", DEFAULT_DAILY_CAP_USD))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_CAP_USD


def under_cap(conn: sqlite3.Connection) -> bool:
    return spent_today(conn) < daily_cap(conn)
