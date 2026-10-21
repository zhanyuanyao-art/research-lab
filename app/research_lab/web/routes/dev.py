"""开发 page: a read-only view of dev/ (the board), versions and builds, worktrees and running servers,
and what the assistant engines used. Actions stay in `rl dev` (the integrator's tool). It also carries the
read-only tables that left 设置 (docs/design/SETTINGS-AGENT.md §2): skill tiers and usage, tier → model,
DeepSeek prices and the push sources in detail; the assistant changes those settings."""
from __future__ import annotations

import os
import sqlite3
import threading

from fastapi import APIRouter, Depends, Request

from ...agent import skills as skill_mod, usage
from ...ai import claude_code, cost, dispatch, lease, queue
from ...ai.pricing import CHECKED_ON, PRICES
from ...db.connection import get_setting
from ...devflow import cards, status
from ...tunables import tunable
from ..context import get_conn, render

router = APIRouter()
ENGINES = (("claude", "Claude Code"), ("deepseek", "DeepSeek"))
COPIES_WAIT = 0.3      # seconds the page waits for the app-copies lookup before showing 正在查找
_board_memo: dict[str, tuple] = {}
_board_lock = threading.Lock()


def board() -> dict:
    """cards.board(), reparsed only when a card file is added, removed or rewritten (about 0.5 s for 160 cards)."""
    d = cards.cards_dir()
    try:
        key = (str(d), tuple(sorted((e.name, e.stat().st_mtime_ns, e.stat().st_size)
                                    for e in os.scandir(d) if e.name.endswith(".md"))))
    except OSError:
        return cards.board()
    with _board_lock:
        if (hit := _board_memo.get("board")) and hit[0] == key:
            return hit[1]
    value = cards.board()
    with _board_lock:
        _board_memo["board"] = (key, value)
    return value


def skills_view(conn: sqlite3.Connection) -> list[dict]:
    """Every skill with its tier(s) and 7-day usage, busiest first; usage under a name no skill file has stays."""
    use = usage.by_skill(conn, 7)
    out = []
    for sk in skill_mod.load_all().values():
        u = use.pop(sk.name, None) or {}
        out.append({"name": sk.name, "title": sk.title, "mode": sk.mode, "tier": skill_mod.tier_of(conn, sk),
                    "task_tier": skill_mod.tier_of(conn, sk, task=True), **_use(u)})
    out += [{"name": name, "title": name, "mode": "", "tier": "", "task_tier": "", **_use(u)}
            for name, u in use.items() if u.get("runs")]
    return sorted(out, key=lambda s: -s["runs"])


def _use(u: dict) -> dict:
    return {"runs": u.get("runs", 0), "input": u.get("input", 0), "cached": u.get("cached", 0), "output": u.get("output", 0),
            "engines": "、".join(sorted(u.get("engines") or []))}


def feeds_view(conn: sqlite3.Connection) -> dict | None:
    try:
        from ...feeds import sources, store
        src = sources.load()
        st = store.status(conn)
    except Exception:  # noqa: BLE001 - a broken feeds.yml must not break the page
        return None
    return {"journals": len(src.journals), "news": len(src.news), "news_off": src.disabled_news,
            "refresh_hours": tunable(conn, "feeds.refresh_hours"), **st}


@router.get("/dev")
def dev_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        overview = status.overview(include_copies=True, copies_wait=COPIES_WAIT)
    except Exception as e:  # noqa: BLE001 - git or lsof missing must not break the page
        overview = {"error": str(e), "versions": {}, "worktrees": [], "servers": [], "copies": [], "lock": None}
    # every AI call, job runs and assistant tasks alike (the api_calls view, migration 0016)
    runs = conn.execute("SELECT * FROM api_calls ORDER BY at DESC LIMIT 20").fetchall()
    what = {name: s.title for name, s in skill_mod.load_all().items()}
    inbox = cards.dev_dir() / "INBOX.md"
    inbox_items = 0
    if inbox.exists():
        inbox_items = sum(1 for block in inbox.read_text(encoding="utf-8").split("\n\n")[1:] if block.strip() and not block.startswith("#"))
    return render(request, conn, "pages/dev.html", title="开发", board=board(), labels=cards.LABEL, o=overview,
                  skills=skills_view(conn), runs=runs, mode=queue.mode(conn), model=dispatch.configured_model(conn),
                  claude_runs=claude_code.runs_today(conn), claude_cap=claude_code.daily_runs(conn),
                  rate=get_setting(conn, "engine.claude.rate_limits", None), spent=cost.spent_today(conn),
                  inbox_items=inbox_items, installed=str(status.INSTALLED), lease=lease.current(conn),
                  paused=dispatch.paused(conn), claude_blocked=claude_code.blocked(conn), skill_titles=what,
                  tiers=skill_mod.TIERS, tier_label=skill_mod.TIER_LABEL, engines=ENGINES,
                  engine_tiers={e: {t: skill_mod.engine_model(conn, e, t) for t in skill_mod.TIERS} for e, _ in ENGINES},
                  prices=PRICES, prices_checked=CHECKED_ON, daily_cap=cost.daily_cap(conn), feeds=feeds_view(conn))
