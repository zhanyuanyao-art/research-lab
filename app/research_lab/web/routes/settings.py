"""/settings: the consumer page (docs/design/SETTINGS-AGENT.md §2, §4). Seven blocks: 外观, AI, 推送, 连接, 复现环境
(card 0204: R, its build tools, the shared library and Preinstall; loaded by its own request, since finding R and the tools
starts programs), 当前微调, 关于. Each control posts one small form and redirects back, so there is no global save; values go through
tunables.set_value and take the registry's ranges. Run mode is back on the page as a segmented
switch (card 0173); models, tiers and the usage/price tables stay with the assistant and /dev. Secrets stay in app/.env and are never shown."""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from ... import tunables
from ...ai import claude_code, cost, dispatch, queue
from ...config import env, get_paths, has_deepseek_key
from ...db.connection import get_setting, set_setting
from ...pm import changes
from ..context import get_conn, render

router = APIRouter()

# The registry (tunables.py) is the source of truth; test_tunables checks these still agree with it.
MODES = ("auto", "claude", "deepseek", "agent")
THEMES = ("system", "light", "dark")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$")
WEEK_RE = re.compile(r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$")

AUTOIMPORT_KEY = "feeds.autoimport_oa"
FILE_STORES = (tunables.FEEDS, tunables.DIRECTIONS)
FILE_CHANGES_SHOWN = 10
NOTES = {"reset": "已恢复默认。", "undone": "已撤销。", "kept": "你后来改过的地方没有撤销。",
         "already": "这次修改已经撤销过了。", "failed": "没改成，请再试一次。",
         "preinstall": "已开始预装常用的 R 包，进度在复现环境里。", "preinstall-busy": "预装已经在进行了。",
         "preinstall-failed": "预装没能开始：这台电脑没有 R，或者缺命令行开发工具。", "preinstall-stopped": "已停止预装。"}
BAD_NOTES = ("failed", "preinstall-failed")


def why_zh(why: str) -> str:
    """dispatch.route's reason, in the page's words."""
    fixed = {"agent mode": "当前是手动队列", "no DEEPSEEK_API_KEY": "没有配置 DeepSeek 密钥",
             "Claude Code CLI not found": "没有找到 Claude Code"}
    if why in fixed:
        return fixed[why]
    m = re.fullmatch(r"daily cap \$([\d.]+) reached", why or "")
    if m:
        return f"已达每日上限 ${m.group(1)}"
    m = re.fullmatch(r"daily limit of (\d+) Claude Code runs reached", why or "")
    if m:
        return f"Claude Code 今天已用满 {m.group(1)} 次"
    return why or ""


def _feeds_line(conn: sqlite3.Connection) -> dict:
    try:
        from ...feeds import sources
        src = sources.load()
        n_sources = len(src.journals) + len(src.news)
    except Exception:  # noqa: BLE001 - a broken feeds.yml must not break Settings
        n_sources = None
    return {"news_n": tunables.tunable(conn, "feeds.digest.news_n"), "papers_n": tunables.tunable(conn, "feeds.digest.papers_n"),
            "sources": n_sources, "autoimport": tunables.tunable(conn, AUTOIMPORT_KEY)}


def _when(stamp: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _by_assistant(conn: sqlite3.Connection, d: dict) -> str | None:
    """The applied_at of the assistant's latest applied change of this key, when that change is the value in force:
    same value, and the settings row not written after it (a later write from this page or the top-bar toggle, even
    of the same value, is Leo's)."""
    r = conn.execute("SELECT new_json, applied_at FROM assistant_changes WHERE kind = 'setting' AND target = ? "
                     "AND status = 'applied' AND actor = 'assistant' ORDER BY id DESC LIMIT 1", (d["key"],)).fetchone()
    if r is None:
        return None
    try:
        same = tunables.validate(d["key"], json.loads(r["new_json"])) == d["value"]
    except (KeyError, TypeError, ValueError):   # TunableError is a ValueError
        same = False
    applied, written = _when(r["applied_at"]), _when(d["updated_at"])
    # apply writes the row, then stamps applied_at: the stamp is never earlier for the assistant's own write
    if not same or applied is None or (written is not None and written > applied):
        return None
    return r["applied_at"]


def tweaks(conn: sqlite3.Connection) -> list[dict]:
    """当前微调, part one: settings that differ from their default, with who changed them and when."""
    out = []
    for d in tunables.changed(conn):
        at = _by_assistant(conn, d)
        out.append({**d, "by_assistant": at is not None, "at": at or d["updated_at"]})
    return out


def _change_text(t: tunables.Tunable, old, new) -> str:
    """「开 → 关」, 「80 → 90」; a list says what was added and removed."""
    if t.type == tunables.LIST and isinstance(old, list) and isinstance(new, list):
        added = [str(x).strip() for x in new if x not in old]
        removed = [str(x).strip() for x in old if x not in new]
        parts = ([f"加了 {'、'.join(added)}"] if added else []) + ([f"去掉 {'、'.join(removed)}"] if removed else [])
        return "；".join(parts) or "调了顺序"
    return f"{tunables.label(t, old)} → {tunables.label(t, new)}"


def file_changes(conn: sqlite3.Connection, limit: int = FILE_CHANGES_SHOWN) -> list[dict]:
    """当前微调, part two: the assistant's latest applied feeds.yml / directions.yml changes, newest first, grouped
    by batch since 撤销 (pm.changes.undo) takes a whole batch."""
    batches: dict[str, dict] = {}
    shown = 0
    for r in conn.execute("SELECT batch_id, target, label, old_json, new_json, detail_json, result, note, applied_at "
                          "FROM assistant_changes WHERE kind = 'setting' AND action = 'set' AND status = 'applied' "
                          "AND actor = 'assistant' ORDER BY id DESC LIMIT 200"):
        try:
            t = tunables.entry(r["target"])
        except KeyError:
            continue
        if t.store not in FILE_STORES:
            continue
        # detail holds the values as really stored (pm/changes.py); old/new are what the assistant saw and asked for
        detail = json.loads(r["detail_json"]) if r["detail_json"] else {}
        old = detail.get("old", json.loads(r["old_json"]) if r["old_json"] else None)
        new = detail.get("new", json.loads(r["new_json"]) if r["new_json"] else None)
        b = batches.setdefault(r["batch_id"], {"batch_id": r["batch_id"], "at": r["applied_at"], "rows": [], "undoable": False})
        kept = r["result"] == "kept"   # an earlier undo left it: Leo changed the file since
        b["rows"].append({"name": t.name or r["label"], "change": _change_text(t, old, new), "note": r["note"] if kept else ""})
        b["undoable"] = b["undoable"] or not kept
        shown += 1
        if shown >= limit:
            break
    return list(batches.values())


def _page(request: Request, conn: sqlite3.Connection, note: str = ""):
    where, why = dispatch.route(conn)
    return render(request, conn, "pages/settings.html", title="设置", note=NOTES.get(note, ""), note_bad=note in BAD_NOTES,
                  theme_choices=tunables.entry("ui.theme").choices, route=where, route_why=why_zh(why),
                  ai_paused=dispatch.paused(conn), claude_cli=bool(claude_code.find_cli(conn)),
                  mode_choices=tunables.entry("ai.mode").choices, mode_setting=get_setting(conn, "ai.mode", "auto"),
                  mode_effective=queue.mode(conn),
                  claude_runs_today=claude_code.runs_today(conn), has_key=has_deepseek_key(), spent=cost.spent_today(conn),
                  has_contact=bool(env("CONTACT_EMAIL")), feeds=_feeds_line(conn), tweaks=tweaks(conn),
                  file_changes=file_changes(conn))


def _back(anchor: str, note: str = "") -> RedirectResponse:
    return RedirectResponse(f"/settings{'?note=' + note if note else ''}#{anchor}", status_code=303)


@router.get("/settings")
def settings_page(request: Request, note: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    return _page(request, conn, note)


@router.post("/settings/theme")
async def set_theme(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    try:
        tunables.set_value(conn, "ui.theme", str(form.get("theme") or ""))
    except tunables.TunableError:
        return _back("appearance", "failed")
    return _back("appearance")


@router.post("/settings/autoimport")
async def set_autoimport(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    try:
        tunables.set_value(conn, AUTOIMPORT_KEY, str(form.get("on") or ""))
    except tunables.TunableError:
        return _back("feeds", "failed")
    return _back("feeds")


@router.post("/settings/reset")
async def reset(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """恢复默认: delete the stored value so the code's default applies again."""
    form = await request.form()
    try:
        tunables.reset(conn, str(form.get("key") or ""))
    except (KeyError, LookupError, tunables.TunableError):
        return _back("tweaks", "failed")
    return _back("tweaks", "reset")


@router.post("/settings/undo")
async def undo(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """撤销 one of the assistant's setting changes; pm.changes checks each field and leaves what Leo changed since."""
    form = await request.form()
    batch = str(form.get("batch") or "")
    rows = changes.rows(conn, batch)
    if not rows or any(r["kind"] != "setting" for r in rows):
        return _back("tweaks", "failed")
    try:
        outcome = changes.undo(conn, batch)
    except Exception:  # noqa: BLE001 - e.g. no undo handler registered for kind 'setting' yet (card 0080)
        return _back("tweaks", "failed")
    if outcome["already"]:
        return _back("tweaks", "already")
    if outcome["failed"]:
        return _back("tweaks", "failed")
    return _back("tweaks", "kept" if outcome["kept"] or outcome["skipped"] else "undone")


@router.post("/settings/ai-pause")
async def ai_pause(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """暂停全部 AI / 恢复: nothing new starts while paused; a run in flight finishes. Not a tunable: the assistant
    stops with it."""
    form = await request.form()
    paused = str(form.get("paused") or "") == "1"
    set_setting(conn, dispatch.PAUSE_KEY, paused)
    if not paused:
        claude_code.WORKER.kick()
    return _back("ai")


@router.post("/settings/ai-mode")
async def set_ai_mode(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """运行方式 (card 0173): the switch is on the page so an exhausted Claude Code quota can be worked around without
    asking the assistant. Jobs already waiting go to the engine the new mode routes to: the Claude worker sweeps on a
    kick; DeepSeek has no sweep, so each queued job is dispatched here (a missing key or a cap leaves it queued)."""
    form = await request.form()
    try:
        tunables.set_value(conn, "ai.mode", str(form.get("mode") or ""))
    except tunables.TunableError:
        return _back("ai", "failed")
    where, _why = dispatch.route(conn)
    if where == "claude":
        claude_code.WORKER.kick()
    elif where == "deepseek":
        for job in queue.list_jobs(conn, "queued"):
            dispatch.dispatch(job["id"])
    return _back("ai")


@router.get("/settings/r-env")
def r_env_section(request: Request, fresh: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    """复现环境, alone (the page loads it after itself; while a preinstall runs it polls itself every 3 s). A sync route:
    finding R and the tools starts programs, so it runs off the event loop."""
    from ...replication import renv
    return render(request, conn, "partials/_r_env.html", env=renv.view(fresh=bool(fresh)))


@router.post("/settings/r-env/preinstall")
def r_env_preinstall(request: Request):
    """Preinstall: the common packages into the lab's shared library, in the background (replication/renv.py)."""
    from ...replication import renv, rrun
    try:
        started = renv.start()
    except rrun.RRunError:
        return _back("r-env", "preinstall-failed")
    return _back("r-env", "preinstall" if started else "preinstall-busy")


@router.post("/settings/r-env/stop")
def r_env_stop(request: Request):
    from ...replication import renv
    renv.stop()
    return _back("r-env", "preinstall-stopped")


@router.post("/settings/reveal")
async def reveal(request: Request):
    """Open the research repository or the log folder in Finder (the app only ever runs on this Mac)."""
    form = await request.form()
    what = str(form.get("what") or "repo")
    from ...desktop import LOG_DIR
    target = get_paths().root if what == "repo" else LOG_DIR
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run(["open", str(target)], check=False)
    return _back("about")


def _interests_page(request, conn, *, error="", status=200, return_to=""):
    from ...feeds import presets
    try:
        prefs = presets.view()
    except (OSError, presets.PreferenceError):
        prefs = None
        error = "推送配置无法读取，未更改订阅。"
        status = 400
    dest = return_to or request.query_params.get("return_to", "")
    response = render(request, conn, "pages/feed_interests.html", title="学科与兴趣", prefs=prefs,
                      error=error, saved=request.query_params.get("saved") == "1",
                      return_to=dest if dest in ("/guide", "/feed") else "/settings#feeds")
    response.status_code = status
    return response


@router.get("/settings/interests")
def feed_interests(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _interests_page(request, conn)


@router.post("/settings/interests")
async def save_feed_interests(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    from ...feeds import presets
    from ...files.yamlio import StaleFileError
    async with request.form(max_fields=30, max_files=0) as form:
        dest = str(form.get("return_to", ""))
        try:
            if "sha" not in form:
                raise presets.PreferenceError("请重新打开页面后保存。")
            presets.save(form.getlist("profiles"), form.getlist("channels"),
                         str(form.get("keywords", "")), str(form["sha"]))
        except StaleFileError:
            return _interests_page(request, conn, error="订阅已在别处更新，请核对后再保存。", status=409, return_to=dest)
        except (presets.PreferenceError, OSError) as exc:
            error = str(exc) if isinstance(exc, presets.PreferenceError) else "订阅未保存，请重试。"
            return _interests_page(request, conn, error=error, status=400, return_to=dest)
    from urllib.parse import urlencode
    dest = dest if dest in ("/guide", "/feed") else "/settings#feeds"
    return RedirectResponse("/settings/interests?" + urlencode({"saved": "1", "return_to": dest}), status_code=303)
