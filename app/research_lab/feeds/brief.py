"""每日世界政治简报 (card 0062, docs/design/NEWS-v2.md §4): when to queue it, and what 首页 shows.

The job itself is ai/jobs/news_brief.py. It is queued at the end of every refresh (enrich.enqueue_pending) once the
morning has come and today has no brief yet, so the day's first refresh after the brief hour produces it. Its events
choose the day's 世界要闻 (feeds/digest.py). The daily 8 o'clock refresh and the manual 刷新今天 (feeds/scheduler.py
refresh_today) rewrite it even when one exists (rewrite()).

Days and hours are Beijing time (Asia/Shanghai, card 0104), whatever the Mac's own time zone says.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..db.connection import get_setting
from ..tunables import tunable

log = logging.getLogger("research_lab.feeds.brief")

TZ = ZoneInfo("Asia/Shanghai")
# The day's edition: the brief, the picks and the automatic refresh all turn over at this Beijing hour (Leo 10-11).
# Stands in for the default of the tunable feeds.brief_hour (still 7 in tunables.py, which card 0093 holds); a value
# Leo stored there wins. Move the default to tunables.py once 0093 is merged.
DAILY_HOUR = 8
BRIEF_HOUR = DAILY_HOUR
MIN_ITEMS = 10     # fewer world items than this in a day is not worth a brief
MAX_TRIES = 3      # failed brief jobs a day before giving up until tomorrow
# 今日精选 holds its 世界要闻 slots open while the brief is on its way (feeds/digest.py); a brief job unanswered
# this long after the day's first try is no longer waited for.
WAIT_AFTER_FIRST_TRY = timedelta(hours=2)


def now_bj() -> datetime:
    return datetime.now(TZ)


def today_bj() -> date:
    return now_bj().date()


def daily_hour(conn: sqlite3.Connection) -> int:
    """The hour the day's edition turns over: Leo's stored feeds.brief_hour, else DAILY_HOUR."""
    return tunable(conn, "feeds.brief_hour") if get_setting(conn, "feeds.brief_hour", None) is not None else DAILY_HOUR


def get(conn: sqlite3.Connection, day: str) -> dict[str, Any] | None:
    from ..ai.jobs.news_brief import KEY
    b = get_setting(conn, KEY + day, None)
    return b if b and b.get("events") else None


def latest(conn: sqlite3.Connection, day: date | None = None) -> dict[str, Any] | None:
    """Today's brief, or yesterday's until today's is written."""
    day = day or today_bj()
    b = get(conn, day.isoformat())
    if b:
        return {**b, "today_iso": day.isoformat(), "yesterday": False}
    b = get(conn, (day - timedelta(days=1)).isoformat())
    return {**b, "today_iso": day.isoformat(), "yesterday": True} if b else None


def _utc(ts: str) -> datetime:
    t = datetime.fromisoformat(ts)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def may_still_come(conn: sqlite3.Connection, day: str, now: datetime) -> bool:
    """Whether `day`'s brief is not written yet but on its way, judged by its job rather than the clock: pages fill
    今日精选 before the refresh that queues the brief has run, so an afternoon's first visit must wait for it too.
    On its way: no job yet while one could be queued (enough world news, push budget left), or tried, nothing done,
    fewer than MAX_TRIES failures and the first try less than WAIT_AFTER_FIRST_TRY ago (a stuck worker)."""
    from ..ai.jobs.news_brief import KIND, items_for
    if day != now.date().isoformat() or get(conn, day):
        return False
    jobs = conn.execute("SELECT status, created_at FROM ai_jobs WHERE kind = ? AND target_id = ?", (KIND, day)).fetchall()
    if not jobs:
        from .enrich import over_budget
        if over_budget(conn):
            return False
        if len(items_for(conn, day)) >= MIN_ITEMS:
            return True
        # After a long break the page asks before the refresh that brings today's news has run: wait for it.
        from . import scheduler, store
        age = scheduler._age_seconds((store.last_refresh(conn) or {}).get("at"))
        return scheduler.running() or (age is not None and age > scheduler.news_fresh(conn))
    if any(j["status"] == "done" for j in jobs) or sum(j["status"] == "failed" for j in jobs) >= MAX_TRIES:
        return False   # done without events, or given up
    first = min(_utc(j["created_at"]) for j in jobs)
    return now.astimezone(timezone.utc) < first + WAIT_AFTER_FIRST_TRY


def maybe_enqueue(conn: sqlite3.Connection, *, dispatch_jobs: bool = True, now: datetime | None = None) -> str | None:
    """Queue today's brief when it is due: past the brief hour, none written, none queued or done today, and enough
    world news to say something. Returns the job id. A failed job is retried on the next refresh."""
    from ..ai import dispatch, queue
    from ..ai.jobs.news_brief import KIND, items_for
    now = now or now_bj()
    day = now.date().isoformat()
    if now.hour < daily_hour(conn) or get(conn, day):
        return None
    from .enrich import over_budget
    if over_budget(conn):
        return None
    if conn.execute("SELECT 1 FROM ai_jobs WHERE kind = ? AND target_id = ? AND status IN ('queued', 'claimed', 'done')",
                    (KIND, day)).fetchone():
        return None
    if conn.execute("SELECT COUNT(*) FROM ai_jobs WHERE kind = ? AND target_id = ? AND status = 'failed'",
                    (KIND, day)).fetchone()[0] >= MAX_TRIES:
        return None
    if len(items_for(conn, day)) < MIN_ITEMS:
        return None
    job_id = queue.enqueue(conn, KIND, day)
    if job_id and dispatch_jobs:
        try:
            dispatch.dispatch(job_id)
        except Exception:  # noqa: BLE001 - stays queued for the worker
            log.exception("dispatch of %s failed", job_id)
    return job_id


REWRITE_WAIT = 240   # seconds the refresh waits for the rewritten brief before picking without it


def rewrite(conn: sqlite3.Connection, day: str, *, dispatch_jobs: bool = True, wait: float = REWRITE_WAIT,
            poll: float = 2.0) -> str:
    """Write `day`'s brief again even when one exists (card 0104), and wait for it up to `wait` seconds.
    Returns 'done', 'failed', 'pending' (still queued or running), 'budget' (push budget spent) or 'few' (not
    enough world news). The old brief stays until the new one lands (news_brief keeps it if the new one is empty)."""
    from ..ai import dispatch, queue
    from ..ai.jobs.news_brief import KIND, items_for
    from .enrich import over_budget
    if over_budget(conn):
        return "budget"
    if len(items_for(conn, day)) < MIN_ITEMS:
        return "few"
    open_job = conn.execute("SELECT id FROM ai_jobs WHERE kind = ? AND target_id = ? AND status IN ('queued', 'claimed') "
                            "ORDER BY created_at DESC LIMIT 1", (KIND, day)).fetchone()
    job_id = open_job["id"] if open_job else queue.enqueue(conn, KIND, day)
    if not job_id:
        return "pending"
    if dispatch_jobs:
        try:
            dispatch.dispatch(job_id)
        except Exception:  # noqa: BLE001 - stays queued for the worker
            log.exception("dispatch of %s failed", job_id)
    deadline = time.monotonic() + wait
    while True:
        row = conn.execute("SELECT status FROM ai_jobs WHERE id = ?", (job_id,)).fetchone()
        status = row["status"] if row else "failed"
        if status == "done":
            return "done"
        if status not in ("queued", "claimed"):
            return "failed"
        if time.monotonic() >= deadline:
            return "pending"
        time.sleep(poll)
