"""周五进度 (docs/design/ISSUE-AGENT.md §6, §10b): once per ISO week, from Friday 17:00 local until
Sunday night, one assistant message in the fixed conversation 「本周进度」: the progress card and, for
the preset rules that are on, suggestions on confirmation cards. No model call, no spend, and nothing is
applied: pm.changes.propose only records them until Leo presses 执行.

    due(now) -> bool                         Friday from 17:00, Saturday, Sunday (catch-up after a closed app)
    suggestions(report, rows, rules) -> {move, week, park}   change items, pure
    send_if_due(conn, now=None) -> dict | None   what was posted; None when not due, off, sent or locked
    tick(now=None) · start() · stop() · install(app)   a light timer, every CHECK_EVERY seconds

Up to three cards, each its own decision; 挪到下周 sets the due date to next Friday (no plan week since
0093, docs/design/ISSUES-LINEAR.md §3):
    move  截止改到下周五: overdue (pm.rule.overdue); 0078's ticks
    week  截止改到下周五: due by this Sunday and still open; Leo's week ends on Friday (pm.rule.overdue)
    park  搁置: due date postponed twice or more (pm.rule.overdue) or 进行中 without a change for
          pm.stalled_days (pm.rule.stalled)
An item can be on a move and a park card. The batch ids come from pm.changes.digest_batch_id: chat/cards.py
starts week and park rows unticked and says 来自「每周五的进度」 instead of quoting Leo from the thread.

Settings: pm.friday_digest and pm.rule.overdue / stalled (tunables); pm.friday_sent:<ISO week>,
the week's marker, claimed in the same transaction as the post; pm.friday_thread, the conversation's id
(a missing or archived one is replaced, never a thread found by title: Leo's own 「本周进度」 question
names a thread the same way). Browser server and Mac app share the database: only the holder of the
'pm.digest' lease (ai/lease.py) tries, and the marker insert under BEGIN IMMEDIATE decides the rest.
RL_PM_AUTO=0 turns the timer off; unset, it follows RL_FEEDS_AUTO (the tests and `rl dev check` set 0).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime
from typing import Any

from .. import tunables
from ..ai import lease
from ..chat import service
from ..db.connection import connect, get_setting, now_iso, set_setting
from ..files import issues
from ..files import tasks as T
from ..files.weeks import week_of
from . import changes, checkup

log = logging.getLogger("research_lab.pm.scheduler")

FRIDAY, HOUR = 4, 17
CHECK_EVERY = 600
STARTUP_DELAY = 30
LEASE = "pm.digest"
SENT_KEY = "pm.friday_sent:{week}"
THREAD_KEY = "pm.friday_thread"
THREAD_TITLE = "本周进度"
MAX_ROWS = 10              # per card; a longer list belongs on the issue pages

_stop = threading.Event()
_loop_thread: threading.Thread | None = None


def enabled() -> bool:
    flag = os.environ.get("RL_PM_AUTO")
    if flag is None:
        flag = os.environ.get("RL_FEEDS_AUTO", "1")
    return flag.strip().lower() not in ("0", "false", "no", "off")


def due(now: datetime) -> bool:
    """Friday ≥ 17:00 or the weekend after it; Monday starts a new ISO week and a new marker."""
    wd = now.weekday()
    return wd > FRIDAY or (wd == FRIDAY and now.hour >= HOUR)


def suggestions(report: dict, rows: list[dict], rules: dict[str, bool], next_week: str = "") -> dict[str, list[dict]]:
    """Change items per card for the rules that are on (rules: checkup.rules). A 课题 is left alone; a
    parent with open sub-issues is not parked by itself (parking cascades to them). `next_week` is
    accepted for older callers and not used: the new due date is the Friday after report['today']."""
    today = date.fromisoformat(report["today"]) if report.get("today") else date.today()
    target = T.next_friday(today)
    by_id = {r["id"]: r for r in rows}
    open_parents = {r["parent"] for r in rows if r.get("parent") and r["status"] in T.OPEN and not r["archived"]}
    out: dict[str, list[dict]] = {"move": [], "week": [], "park": []}
    for a in report["attention"]:
        row = by_id.get(a["id"])
        if row is None or row.get("project_root"):
            continue
        kinds = a["kinds"]
        if rules.get("overdue") and "overdue" in kinds and (row["due"] or "") < target:
            out["move"].append(changes.change_item(row["id"], "due", row["due"], target, label=row["title"]))
        parkable = (rules.get("stalled") and "stalled" in kinds) or (rules.get("overdue") and kinds.get("postponed", 0) >= checkup.POSTPONED_MIN)
        if parkable and row["id"] not in open_parents:
            out["park"].append(changes.change_item(row["id"], "status", row["status"], "parked", label=row["title"]))
    if rules.get("overdue"):
        moved = {i["target"] for i in out["move"]}
        for row in rows:
            if (T.flags(row, today=today)["this_week"] and not row["archived"] and not row.get("project_root")
                    and row["id"] not in moved and row["id"] not in open_parents):
                out["week"].append(changes.change_item(row["id"], "due", row["due"], target, label=row["title"]))
    return {k: v[:MAX_ROWS] for k, v in out.items()}


def _text(report: dict, picks: dict[str, list]) -> str:
    n = len(report["attention"])
    lines = [f"**{report['title']}** · {report['subtitle']}：" + (f"要注意的有 {n} 条。" if n else "没有要注意的。")]
    if picks["move"]:
        lines.append("已经逾期的，建议把截止改到下周五。")
    if picks["week"]:
        lines.append("本周到期还没做完的，也可以改到下周五，默认不勾。")
    if picks["park"]:
        lines.append("截止推迟多次或很久没动的，可以考虑搁置，默认不勾。")
    if any(picks.values()):
        lines.append("这些只是建议：勾选后点「执行」才会改，不需要就点「不用了」。")
    return "\n\n".join(lines)


REASONS = {"move": "已经逾期：截止改到下周五",
           "week": "本周到期还没做完：要不要改到下周五",
           "park": "截止推迟两次以上，或进行中 {days} 天没动：可以先搁置"}


def _thread(conn) -> str:
    tid = get_setting(conn, THREAD_KEY)
    row = service.get_thread(conn, tid) if isinstance(tid, str) else None
    if row is None or row["archived"]:
        tid = service.create_thread(conn, THREAD_TITLE)
        set_setting(conn, THREAD_KEY, tid)
    return tid


def _post(conn, today, key: str) -> dict | None:
    report = checkup.run(conn, "week", today=today)          # files only; read before the write lock
    picks = suggestions(report, issues.read(), checkup.rules(conn))
    conn.execute("BEGIN IMMEDIATE")
    try:
        claimed = conn.execute("INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO NOTHING",
                               (key, json.dumps({"at": now_iso()}), now_iso())).rowcount
        if not claimed:
            conn.execute("ROLLBACK")
            return None
        tid = _thread(conn)
        msg = service.add_message(conn, tid, "assistant", _text(report, picks),
                                  card_kind="progress", card_ref=f"week@{today.isoformat()}")
        batches = {kind: changes.propose(conn, items, thread_id=tid, message_id=msg.id, batch_id=changes.digest_batch_id(kind),
                                         reason=REASONS[kind].format(days=report["stalled_days"]))
                   for kind, items in picks.items() if items}       # cards in this order: move, week, park
        out = {"at": now_iso(), "week": report["week"], "thread_id": tid, "message_id": msg.id, "batches": batches,
               "counts": {kind: len(items) for kind, items in picks.items()}}
        set_setting(conn, key, out)
        conn.execute("COMMIT")
        return out
    except Exception:
        conn.execute("ROLLBACK")
        raise


def send_if_due(conn, now: datetime | None = None) -> dict[str, Any] | None:
    now = now or datetime.now()
    if not due(now) or not tunables.tunable(conn, "pm.friday_digest"):
        return None
    today = now.date()
    key = SENT_KEY.format(week=week_of(today))
    if get_setting(conn, key) is not None or not lease.acquire(conn, LEASE):
        return None
    try:
        return _post(conn, today, key)
    finally:
        lease.release(conn, LEASE)


def tick(now: datetime | None = None) -> dict[str, Any] | None:
    try:
        conn = connect()
        try:
            return send_if_due(conn, now)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - background work must never take the app down; retried next tick
        log.exception("Friday progress failed")
        return None


def _loop() -> None:
    if _stop.wait(STARTUP_DELAY):
        return
    while not _stop.is_set():
        tick()
        _stop.wait(CHECK_EVERY)


def start() -> bool:
    global _loop_thread
    if not enabled():
        return False
    if _loop_thread is not None and _loop_thread.is_alive():
        return True
    _stop.clear()
    _loop_thread = threading.Thread(target=_loop, daemon=True, name="pm-friday")
    _loop_thread.start()
    return True


def stop() -> None:
    _stop.set()


def install(app) -> None:
    app.router.on_startup.append(start)
    app.router.on_shutdown.append(stop)
