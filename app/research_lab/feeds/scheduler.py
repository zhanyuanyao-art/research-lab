"""Background refresh of the push streams.

When the app starts it waits a few seconds, then refreshes if the last full refresh is older than a day,
and keeps doing so daily while it runs; opening 首页 or 推送 also refreshes (refresh_news_on_view). The
manual 刷新 button starts the same refresh in a thread and returns at once; a request never waits on a
network fetch. The last run's time and counts
live in the settings row `feeds.last_refresh`. A lease row (`feeds.lease`) keeps two app processes
(browser server and Mac app) from refreshing at the same time.

The day's edition (card 0104, Leo 10-11): at 8 o'clock Beijing time (brief.daily_hour) refresh_today fetches both
streams, rewrites today's brief and recomputes today's picks. The timer runs it on the hour while the app is open; the
first visit to 首页 or 推送 after 8 runs it when the app was closed then. Before 8 the pages keep yesterday's brief.
首页's refresh button runs the same thing by hand, MANUAL_LIMIT times a day (it calls the AI).

Set RL_FEEDS_AUTO=0 to keep the timer off (the tests do); the manual button still works.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from ..db.connection import connect, get_setting, now_iso, set_setting
from ..tunables import default, tunable
from . import brief, enrich, net, news as news_mod, papers as papers_mod, store

log = logging.getLogger("research_lab.feeds.scheduler")

# Defaults of the tunables feeds.refresh_hours / feeds.news_fresh_minutes; interval() and news_fresh() read them live.
INTERVAL = default("feeds.refresh_hours") * 3600        # papers and news together once a day (Leo, 10-02)
NEWS_FRESH = default("feeds.news_fresh_minutes") * 60   # opening 首页 or 推送 refetches news older than this (Leo: 「给我看的时候要实时更新」)
STARTUP_DELAY = 20
RETRY_AFTER = 600
LEASE_KEY = "feeds.lease"
LEASE_TTL = 1800
CHECK_EVERY = 600        # the timer re-reads the clock this often: a sleeping Mac does not advance its waits
TODAY_KEY = "feeds.today_refresh"     # {"at": Beijing time of the last refresh_today, "kind": daily | manual}
MANUAL_KEY = "feeds.manual_refresh:"  # + Beijing date → how many manual refreshes that day
# Manual refreshes a day (Leo 10-11: once). Stands in for a tunable that goes into tunables.py once card 0093 is merged.
MANUAL_LIMIT = 1
LOCK_WAIT = 120          # refresh_today waits this long for a refresh already running to finish

_run_lock = threading.Lock()
_running = threading.Event()
_stop = threading.Event()
_loop_thread: threading.Thread | None = None
_last_thread: threading.Thread | None = None


def enabled() -> bool:
    return os.environ.get("RL_FEEDS_AUTO", "1").strip().lower() not in ("0", "false", "no", "off")


def running() -> bool:
    return _running.is_set()


def interval(conn) -> int:
    return tunable(conn, "feeds.refresh_hours") * 3600


def news_fresh(conn) -> int:
    return tunable(conn, "feeds.news_fresh_minutes") * 60


def _age_seconds(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return time.time() - datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def seconds_until_due(conn) -> float:
    """Until the next full refresh: a news-only refresh for a page being looked at does not postpone it."""
    last = store.last_refresh(conn) or {}
    age = _age_seconds(last.get("full_at") or last.get("at"))
    return 0.0 if age is None else max(0.0, interval(conn) - age)


def edition_start(conn, now: datetime | None = None) -> datetime:
    """When the current day's edition began: today at the daily hour, or yesterday's before it (Beijing time)."""
    now = (now or brief.now_bj()).astimezone(brief.TZ)
    start = now.replace(hour=brief.daily_hour(conn), minute=0, second=0, microsecond=0)
    return start if now >= start else start - timedelta(days=1)


def last_today_refresh(conn) -> datetime | None:
    v = get_setting(conn, TODAY_KEY, None) or {}
    try:
        t = datetime.fromisoformat(v.get("at") or "")
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=brief.TZ)


def daily_due(conn, now: datetime | None = None) -> bool:
    """Past today's daily hour and refresh_today has not run since."""
    now = (now or brief.now_bj()).astimezone(brief.TZ)
    start = now.replace(hour=brief.daily_hour(conn), minute=0, second=0, microsecond=0)
    if now < start:
        return False
    last = last_today_refresh(conn)
    if last is None and get_setting(conn, TODAY_KEY, None) is None:
        # Until the first refresh_today has ever run (the day this ships), a refresh since 8 counts as the edition.
        try:
            prev = datetime.fromisoformat((store.last_refresh(conn) or {}).get("at") or "")
        except (TypeError, ValueError):
            prev = None
        if prev is not None:
            prev = prev if prev.tzinfo else prev.replace(tzinfo=timezone.utc)
            return prev < start
    return last is None or last < start


def seconds_until_daily(conn, now: datetime | None = None) -> float:
    now = (now or brief.now_bj()).astimezone(brief.TZ)
    if daily_due(conn, now):
        return 0.0
    nxt = edition_start(conn, now) + timedelta(days=1)
    return max(0.0, (nxt - now).total_seconds())


def manual_used(conn, day: str | None = None) -> int:
    v = get_setting(conn, MANUAL_KEY + (day or brief.today_bj().isoformat()), 0)
    return int(v) if isinstance(v, (int, float)) else 0


def manual_left(conn, day: str | None = None) -> int:
    return max(0, MANUAL_LIMIT - manual_used(conn, day))


def _claim_manual(conn, day: str) -> bool:
    """Take one of today's manual refreshes; read and increment are one statement, so two windows clicking at
    once cannot both get the last one."""
    key = MANUAL_KEY + day
    conn.execute("INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES (?, '0', ?)", (key, now_iso()))
    cur = conn.execute("UPDATE settings SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT), updated_at = ? "
                       "WHERE key = ? AND CAST(value AS INTEGER) < ?", (now_iso(), key, MANUAL_LIMIT))
    return cur.rowcount == 1


def news_days(last_at: str | None, today: date | None = None) -> int:
    today = today or date.today()
    if not last_at:
        return 4
    try:
        gap = (today - date.fromisoformat(last_at[:10])).days
    except ValueError:
        return 4
    return max(2, min(7, gap + 1))


def link_library(conn) -> int:
    """Feed papers whose DOI is already in the library point at that paper."""
    cur = conn.execute("UPDATE feed_papers SET paper_id = (SELECT p.id FROM papers p WHERE p.doi = feed_papers.doi) "
                       "WHERE paper_id IS NULL AND doi IS NOT NULL AND doi IN (SELECT doi FROM papers WHERE doi IS NOT NULL)")
    return cur.rowcount


def refresh_all(conn, http: httpx.Client | None = None, *, papers_days: int | None = None, news_days_: int | None = None,
                ai: bool = True, dispatch_jobs: bool = True) -> dict[str, Any]:
    """Both streams, then the assistant batches. Each stream's failure is recorded, never raised."""
    last = store.last_refresh(conn) or {}
    # the papers' own last fetch: news-only refreshes move `at` but never fetch papers (card 0067 review)
    pdays = papers_days or papers_mod.default_days(last.get("full_at") or last.get("at"))
    ndays = news_days_ or news_days(last.get("at"))
    summary: dict[str, Any] = {"started": now_iso(), "papers_days": pdays, "news_days": ndays}
    own = http is None
    http = http if http is not None else net.new_client()
    try:
        for key, fn, days in (("papers", papers_mod.refresh_papers, pdays), ("news", news_mod.refresh_news, ndays)):
            try:
                summary[key] = fn(conn, http, days=days)
            except Exception as e:  # noqa: BLE001 - one stream failing still records the other
                log.exception("%s refresh failed", key)
                summary[key] = {"new": 0, "failed": [], "error": type(e).__name__}
    finally:
        if own:
            http.close()
    try:
        summary["linked"] = link_library(conn)
        summary["ai_jobs"] = enrich.enqueue_pending(conn, dispatch_jobs=dispatch_jobs) if ai else []
    except Exception as e:  # noqa: BLE001
        log.exception("feed enrichment could not be queued")
        summary["ai_jobs"], summary["error"] = [], type(e).__name__
    summary["at"] = summary["full_at"] = now_iso()
    set_setting(conn, store.LAST_KEY, summary)
    return summary


def refresh_news(conn, http: httpx.Client | None = None, *, ai: bool = True, dispatch_jobs: bool = True) -> dict[str, Any]:
    """News only, for a page being looked at; papers keep their daily turn (full_at carries over)."""
    last = store.last_refresh(conn) or {}
    summary: dict[str, Any] = {"kind": "news", "started": now_iso(), "news_days": news_days(last.get("at")),
                               "full_at": last.get("full_at") or last.get("at"), "papers": {"new": 0, "failed": []}}
    own = http is None
    http = http if http is not None else net.new_client()
    try:
        summary["news"] = news_mod.refresh_news(conn, http, days=summary["news_days"])
    except Exception as e:  # noqa: BLE001
        log.exception("news refresh failed")
        summary["news"] = {"new": 0, "failed": [], "error": type(e).__name__}
    finally:
        if own:
            http.close()
    try:
        summary["ai_jobs"] = enrich.enqueue_pending(conn, dispatch_jobs=dispatch_jobs) if ai else []
    except Exception as e:  # noqa: BLE001
        log.exception("feed enrichment could not be queued")
        summary["ai_jobs"], summary["error"] = [], type(e).__name__
    summary["at"] = now_iso()
    set_setting(conn, store.LAST_KEY, summary)
    return summary


def refresh_today(conn, http: httpx.Client | None = None, *, kind: str = "daily", dispatch_jobs: bool = True,
                  brief_wait: float | None = None) -> dict[str, Any]:
    """The day's edition: both streams, today's brief written again, today's picks recomputed."""
    day = brief.today_bj().isoformat()
    summary = refresh_all(conn, http, dispatch_jobs=dispatch_jobs)
    try:
        summary["brief"] = brief.rewrite(conn, day, dispatch_jobs=dispatch_jobs,
                                         wait=brief.REWRITE_WAIT if brief_wait is None else brief_wait)
    except Exception as e:  # noqa: BLE001 - the picks are still worth recomputing
        log.exception("today's brief could not be rewritten")
        summary["brief"] = type(e).__name__
    from . import digest
    try:
        digest.recompute(conn, day)
    except Exception:  # noqa: BLE001
        log.exception("today's picks could not be recomputed")
    set_setting(conn, TODAY_KEY, {"at": brief.now_bj().isoformat(timespec="seconds"), "kind": kind, "day": day,
                                  "brief": summary["brief"]})
    summary["kind"] = kind
    return summary


def _acquire_lease(conn) -> bool:
    now = time.time()
    cur = conn.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
        "value = excluded.value, updated_at = excluded.updated_at "
        "WHERE CAST(json_extract(settings.value, '$.until') AS REAL) < ?",
        (LEASE_KEY, json.dumps({"pid": os.getpid(), "until": now + LEASE_TTL}), now_iso(), now))
    return cur.rowcount == 1


def _release_lease(conn) -> None:
    conn.execute("DELETE FROM settings WHERE key = ? AND CAST(json_extract(value, '$.pid') AS INTEGER) = ?",
                 (LEASE_KEY, os.getpid()))


def run_refresh(http: httpx.Client | None = None, news_only: bool = False, **kw: Any) -> dict[str, Any] | None:
    """One refresh in the calling thread with its own connection. None when another refresh is running
    (in this process or another) or the run could not start."""
    if not _run_lock.acquire(blocking=False):
        return None
    _running.set()
    conn = None
    try:
        conn = connect()
        if not _acquire_lease(conn):
            return None
        try:
            return (refresh_news if news_only else refresh_all)(conn, http, **kw)
        finally:
            _release_lease(conn)
    except Exception:  # noqa: BLE001 - background work must never take the app down
        log.exception("feed refresh failed")
        return None
    finally:
        if conn is not None:
            conn.close()
        _running.clear()
        _run_lock.release()


_today_thread: threading.Thread | None = None
_today_active = threading.Event()
_start_lock = threading.Lock()


def run_today(http: httpx.Client | None = None, kind: str = "daily", lock_wait: float = LOCK_WAIT, **kw: Any) -> dict[str, Any] | None:
    """refresh_today in the calling thread; waits up to lock_wait for a refresh already running (a page's news
    refresh) rather than giving up, so a click is never lost. None when it could not run."""
    if not _run_lock.acquire(timeout=lock_wait):
        return None
    _running.set()
    _today_active.set()
    conn = None
    try:
        conn = connect()
        if kind == "daily" and not daily_due(conn):
            return None   # the timer and a page visit both saw it due; the first one ran it
        deadline = time.monotonic() + lock_wait
        while not _acquire_lease(conn):        # another process (browser server or Mac app) is refreshing
            if time.monotonic() >= deadline:
                return None
            time.sleep(2)
        try:
            if kind == "daily" and not daily_due(conn):
                return None   # the other process held the lease for this very run
            return refresh_today(conn, http, kind=kind, **kw)
        finally:
            _release_lease(conn)
    except Exception:  # noqa: BLE001
        log.exception("today's refresh failed")
        return None
    finally:
        if conn is not None:
            conn.close()
        _today_active.clear()
        _running.clear()
        _run_lock.release()


def _then_import(result: dict[str, Any] | None, http: httpx.Client | None) -> None:
    """After a refresh Leo started himself, today's downloadable articles go into the library (card 0156). A refresh
    that did not run (None) imports nothing; the daily run and the page-view refreshes never come here."""
    if result is None:
        return
    from . import importer
    try:
        importer.autoimport_after_refresh(http)
    except Exception:  # noqa: BLE001 - a refresh that worked is not undone by its import
        log.exception("auto-import after the refresh could not start")


def _run_today_manual(http: httpx.Client | None = None, **kw: Any) -> dict[str, Any] | None:
    result = run_today(http, **kw)
    _then_import(result, http)
    return result


def _run_refresh_manual(http: httpx.Client | None = None, **kw: Any) -> dict[str, Any] | None:
    result = run_refresh(http, **kw)
    _then_import(result, http)
    return result


def today_running() -> bool:
    return _today_active.is_set() or (_today_thread is not None and _today_thread.is_alive())


def start_today(conn, http: httpx.Client | None = None, *, manual: bool, **kw: Any) -> str:
    """Start refresh_today in a thread. 'started', 'running' (one is on its way already) or 'limit' (manual
    refreshes used up today). A manual start counts against MANUAL_LIMIT."""
    global _today_thread, _last_thread
    with _start_lock:
        if today_running():
            return "running"
        if manual and not _claim_manual(conn, brief.today_bj().isoformat()):
            return "limit"
        t = threading.Thread(target=_run_today_manual if manual else run_today, args=(http,),
                             kwargs={"kind": "manual" if manual else "daily", **kw}, daemon=True, name="feeds-today")
        _today_thread = _last_thread = t
        t.start()
    return "started"


def start_background(http: httpx.Client | None = None, *, manual: bool = False, **kw: Any) -> bool:
    """The 刷新 button: start a refresh thread unless one is running. Returns whether it started. `manual` is the
    推送 page's button, which then imports today's downloadable articles (card 0156); the page-view refreshes
    (refresh_news_on_view) leave it off."""
    global _last_thread
    if running() or _run_lock.locked():
        return False
    _running.set()   # visible to the very next status poll, before the thread gets scheduled
    t = threading.Thread(target=_run_refresh_manual if manual else run_refresh, args=(http,), kwargs=kw, daemon=True,
                         name="feeds-refresh")
    _last_thread = t
    t.start()
    return True


def refresh_news_on_view(conn, http: httpx.Client | None = None) -> bool:
    """Start a background news refresh when a page shows news older than news_fresh() (首页, 推送). When the
    daily full refresh is due it runs instead: the app is usually opened for a few minutes, so the timer's
    run (20 s after start, then 10 minutes after losing the lock to this one) would otherwise never fetch
    papers (card 0067). Off with RL_FEEDS_AUTO=0 like the timer. Returns whether one started."""
    if not enabled():
        return False
    if daily_due(conn):
        return start_today(conn, http, manual=False) == "started"
    if today_running():
        return False
    if seconds_until_due(conn) <= 0:
        return start_background(http)
    age = _age_seconds((store.last_refresh(conn) or {}).get("at"))
    if age is not None and age < news_fresh(conn):
        return False
    return start_background(http, news_only=True)


def wait(timeout: float = 30.0) -> None:
    """Block until the last manual refresh finishes (tests)."""
    if _last_thread is not None:
        _last_thread.join(timeout)


def _loop() -> None:
    if _stop.wait(STARTUP_DELAY):
        return
    while not _stop.is_set():
        daily = False
        try:
            conn = connect()
            try:
                daily = daily_due(conn)
                due_in, every = min(seconds_until_due(conn), seconds_until_daily(conn)), interval(conn)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            log.exception("could not read the last feed refresh")
            due_in, every = RETRY_AFTER, INTERVAL
        if daily:
            due_in = CHECK_EVERY if run_today() is not None else RETRY_AFTER
        elif due_in <= 0:
            due_in = every if run_refresh() is not None else RETRY_AFTER
        _stop.wait(max(1.0, min(due_in, every, CHECK_EVERY)))


def start() -> bool:
    """Start the daily timer once per process (no-op when RL_FEEDS_AUTO=0)."""
    global _loop_thread
    if not enabled():
        return False
    if _loop_thread is not None and _loop_thread.is_alive():
        return True
    conn = connect()
    try:   # a rule change (news.NOT_NEWS_RE) reaches stored items now, not at the next refresh up to 6 hours away
        news_mod.flag_not_news(conn)
        conn.commit()
    except Exception:  # noqa: BLE001 - never block app start on this
        log.exception("not-news pass at startup failed")
    finally:
        conn.close()
    _stop.clear()
    _loop_thread = threading.Thread(target=_loop, daemon=True, name="feeds-scheduler")
    _loop_thread.start()
    return True


def stop() -> None:
    _stop.set()


def install(app) -> None:
    """Hook the timer into the app's start-up and shut-down."""
    app.router.on_startup.append(start)
    app.router.on_shutdown.append(stop)
