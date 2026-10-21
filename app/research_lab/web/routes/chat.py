"""Chat: threads, the message stream, the composer endpoint and the per-message poll."""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from markdown_it import MarkdownIt
from markupsafe import Markup
from starlette.datastructures import UploadFile

from ...agent import tasks
from ...ai import dispatch, queue
from ...chat import cards, service
from ...chat import router as chat_router
from ...config import get_paths
from ...files import convert, materials
from ...intake import extract
from ...pm import changes
from ..context import get_conn, render, templates

router = APIRouter()

MAX_FILES = 10
CHUNK = 1 << 20
OPEN = ("queued", "claimed")
DOCK_MESSAGES = 30   # the floating assistant shows the newest ones; the rest are in /chat/<thread>
# Assistant text comes from a model: render markdown without raw HTML.
_safe_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")


def _ensure_filters() -> None:
    env = templates().env
    if "chat_md" not in env.filters:
        env.filters["chat_md"] = lambda s: Markup(_safe_md.render(s or ""))


def is_hx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def message_view(conn: sqlite3.Connection, m: service.Message, request: Request) -> dict[str, Any]:
    """What _message.html renders. A finished job's result is used when the message was never filled."""
    content, kind, ref = m.content, m.card_kind, m.card_ref
    if kind == "task" and ref:
        task = tasks.get(conn, ref)
        pending = bool(task and task["status"] in tasks.OPEN)
        card = cards.render_card(conn, kind, ref, request) + cards.render_plans(conn, m.id, ref, request)
        return {"m": m, "content": content, "card_html": Markup(card),
                "pending": pending, "poll": "every 2s" if pending else None, "failed": False, "job": None, "suggestions": []}
    job = queue.get(conn, m.job_id) if m.job_id else None
    suggestions: list[str] = []
    pending = bool(job and job["status"] in OPEN)
    if job and job["status"] == "done" and job["kind"] == chat_router.REPLY_KIND and job["result_json"]:
        result = json.loads(job["result_json"])
        suggestions = [c for c in result.get("suggested_commands") or [] if isinstance(c, str)][:3]
        if not content:
            content = result.get("reply") or ""
            card = result.get("card") or {}
            kind, ref = card.get("kind"), card.get("ref")
        if kind == "job_wait":
            kind, ref = None, None
    failed = bool(job and job["status"] in ("failed", "cancelled") and not content)
    poll = None
    if pending:
        poll = "every 2s" if (job["status"] == "claimed" or queue.online(conn)) else "every 10s"
    card = cards.render_card(conn, kind, ref, request)
    if m.role == "assistant":
        card += cards.render_plans(conn, m.id, None, request)
    return {"m": m, "content": content, "card_html": Markup(card),
            "pending": pending, "poll": poll, "failed": failed, "job": job, "suggestions": suggestions}


def render_messages(conn: sqlite3.Connection, request: Request, msgs: list[service.Message]) -> str:
    _ensure_filters()
    t = templates().env.get_template("partials/_message.html")
    base = cards._base(request, conn)
    return "".join(t.render({**base, **message_view(conn, m, request)}) for m in msgs)


def _page(request: Request, conn: sqlite3.Connection, thread: sqlite3.Row | None):
    _ensure_filters()
    msgs = service.messages(conn, thread["id"]) if thread else []
    return render(request, conn, "pages/chat.html", title=thread["title"] if thread else "助手", chat_page=True, wide=True,
                  thread=thread, **_thread_ctx(conn), messages_html=Markup(render_messages(conn, request, msgs)),
                  commands=chat_router.COMMANDS)


@router.get("/chat")
def chat_index(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _page(request, conn, None)


@router.get("/chat/message/{message_id}")
def chat_message(message_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    m = service.get_message(conn, message_id)
    if m is None:
        raise HTTPException(404, f"no message {message_id}")
    return HTMLResponse(render_messages(conn, request, [m]))


@router.post("/chat/message/{message_id}/retry")
def chat_retry(message_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    m = service.get_message(conn, message_id)
    if m is None or not m.job_id:
        raise HTTPException(404, f"no job on message {message_id}")
    queue.requeue(conn, m.job_id)
    dispatch.dispatch(m.job_id)
    return HTMLResponse(render_messages(conn, request, [service.get_message(conn, message_id)]))  # type: ignore[list-item]


@router.post("/chat/task/{task_id}/cancel")
def chat_task_cancel(task_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    task = tasks.get(conn, task_id)
    if task is None:
        raise HTTPException(404, "这个任务已经不存在")
    tasks.cancel(conn, task_id)
    m = service.get_message(conn, task["message_id"]) if task["message_id"] else None
    return HTMLResponse(render_messages(conn, request, [m]) if m else "")


def _thread_ctx(conn: sqlite3.Connection) -> dict[str, Any]:
    threads = service.list_threads(conn)
    open_jobs = conn.execute("SELECT COUNT(*) FROM ai_jobs WHERE status IN ('queued', 'claimed')").fetchone()[0]
    return {"threads": threads, "thread_status": tasks.thread_status(conn, [t["id"] for t in threads]), "open_jobs": open_jobs}


JOB_OUTCOME = {"paper.summarize": "中文概要已生成", "paper.translate": "这一段已译完", "methods.replicate": "复现方案已生成",
               "analysis.read": "结果解读已生成", "excerpt.associate": "关联建议已生成", "news.arrange": "推送已整理",
               "feeds.enrich": "中文标题和摘要已补上",
               "artifact.draft": "草稿笔记已写入产出", "checks.derive": "检验已推导", "harvest.review": "复盘已生成"}


# First match wins; the raw error stays in the row's tooltip. Patterns are lower-cased substrings of what
# subprocess, the Claude Code CLI, DeepSeek and the runner put into ai_jobs.error.
FAILURE_REASONS = (
    (("timed out", "timeout"), "运行超时，助手没在限定时间内给出结果"),
    (("daily cap", "daily run", "上限"), "今天的用量上限到了，明天自动恢复"),
    (("usage limit", "rate limit", "rate_limit", "overloaded"), "助手的订阅用量到了上限，稍后重试"),
    (("not logged in", "login", "unauthorized", "api key", "invalid_api_key"), "助手没有登录或密钥无效，去设置里检查"),
    (("no such file", "not found", "filenotfound"), "找不到需要的程序或文件"),
    (("returned non-zero exit status", "exit "), "助手进程意外退出"),
    (("json", "schema", "validation"), "助手返回的内容格式不对，没能保存"),
    (("connection", "network", "resolve host"), "网络连接失败"),
)


def failure_reason(error: str | None) -> str:
    low = (error or "").lower()
    if low.startswith("command '"):
        # subprocess errors repr the whole argv (system prompt included) before the reason; match the tail only.
        low = low.rpartition("' ")[2]
    for needles, reason in FAILURE_REASONS:
        if any(n in low for n in needles):
            return reason
    return "运行出错，没有结果"


def _job_rows(conn: sqlite3.Connection, limit: int = 60) -> list[dict[str, Any]]:
    rows = []
    for j in conn.execute("SELECT id, kind, status, target_type, target_id, error, created_at, completed_at, "
                          "COALESCE(completed_at, created_at) AS shown_at FROM ai_jobs "
                          "ORDER BY shown_at DESC, id DESC LIMIT ?", (limit,)):
        d = dict(j)
        title, href = None, None
        tid = (d["target_id"] or "").split(":")[0]
        if d["target_type"] in ("paper", "paper_translation"):
            r = conn.execute("SELECT title FROM papers WHERE id = ?", (tid,)).fetchone()
            title, href = (r[0] if r else None), f"/papers/{tid}"
        elif d["target_type"] == "excerpt":
            r = conn.execute("SELECT p.title FROM excerpts e JOIN papers p ON p.id = e.paper_id WHERE e.id = ?", (tid,)).fetchone()
            title, href = (f"《{r[0]}》的一条划线" if r else None), f"/excerpts/{tid}"
        elif d["target_type"] in ("run", "analysis_run"):
            title, href = "一次数据分析", f"/data/runs/{tid}"
        elif d["target_type"] == "artifact":
            r = conn.execute("SELECT title FROM artifacts WHERE ref = ?", (tid,)).fetchone()
            title, href = (r[0] if r else None), f"/artifacts/{tid}"
        d.update(title=title, href=href, outcome=JOB_OUTCOME.get(d["kind"], "已完成") if d["status"] == "done" else None,
                 reason=failure_reason(d["error"]) if d["status"] == "failed" else None)
        rows.append(d)
    return rows


@router.get("/chat/jobs")
def chat_jobs(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _ensure_filters()
    recent_tasks = [dict(r) for r in conn.execute(
        "SELECT t.id, t.status, t.skill, t.prompt, t.engine, t.created_at, t.thread_id, t.tool_calls, t.output_tokens "
        "FROM agent_tasks t ORDER BY t.created_at DESC LIMIT 20")]
    from ...agent import skills as skill_mod
    titles = {k: s.title for k, s in skill_mod.load_all().items()}
    return render(request, conn, "pages/chat_jobs.html", title="后台任务", chat_page=True, wide=True, thread=None, jobs_page=True,
                  jobs=_job_rows(conn), recent_tasks=recent_tasks, skill_titles=titles, **_thread_ctx(conn))


@router.get("/chat/threads")
def chat_threads(request: Request, current: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    return render(request, conn, "partials/_thread_list.html", thread={"id": current} if current else None, **_thread_ctx(conn))


@router.get("/chat/dock")
def chat_dock(request: Request, thread: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    """The floating assistant's content, fetched when it opens: recent threads and one conversation. An unknown
    or archived thread gives the empty new-conversation view."""
    _ensure_filters()
    row = service.get_thread(conn, thread) if thread else None
    if row is not None and row["archived"]:
        row = None
    total = conn.execute("SELECT COUNT(*) FROM chat_messages WHERE thread_id = ?", (row["id"],)).fetchone()[0] if row else 0
    msgs = service.messages(conn, row["id"], limit=DOCK_MESSAGES) if row else []
    return render(request, conn, "partials/_assistant_dock.html", dock_fragment=True, thread=row, older=max(0, total - len(msgs)),
                  messages_html=Markup(render_messages(conn, request, msgs)), **_thread_ctx(conn))


@router.post("/chat/{thread_id}/archive")
def chat_archive(thread_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if service.get_thread(conn, thread_id) is None:
        raise HTTPException(404, f"no thread {thread_id}")
    service.archive(conn, thread_id)
    if is_hx(request):
        return Response(status_code=204, headers={"HX-Redirect": "/chat"})
    return RedirectResponse("/chat", status_code=303)


@router.get("/chat/{thread_id}")
def chat_thread(thread_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    thread = service.get_thread(conn, thread_id)
    if thread is None:
        raise HTTPException(404, f"no thread {thread_id}")
    return _page(request, conn, thread)


async def _save_upload(upload: UploadFile) -> Path:
    incoming = get_paths().cache / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    name = upload.filename or ""
    dest = incoming / f"chat-{uuid.uuid4().hex}{Path(name).suffix.lower() if convert.supported(name) else '.pdf'}"
    size = 0
    with open(dest, "wb") as out:
        while chunk := await upload.read(CHUNK):
            size += len(chunk)
            if size > extract.MAX_BYTES:
                out.close()
                dest.unlink(missing_ok=True)
                raise extract.RejectedFile(f"文件超过 {extract.MAX_BYTES // (1024 * 1024)} MB")
            out.write(chunk)
    return dest


def _error(request: Request, message: str, status: int = 400) -> Response:
    if is_hx(request):
        return HTMLResponse(f'<p class="composer-error" role="alert">{Markup.escape(message)}</p>', status_code=status)
    return RedirectResponse("/chat", status_code=303)


@router.post("/chat/send")
async def chat_send(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_FILES * extract.MAX_BYTES + CHUNK:
        return _error(request, "文件太大。", 413)
    temps: list[Path] = []
    try:
        async with request.form(max_files=MAX_FILES, max_fields=20) as form:
            text = str(form.get("text") or "").strip()
            thread_id = str(form.get("thread_id") or "").strip() or None
            page = str(form.get("page") or "")
            fresh = form.get("fresh") == "1"
            files: list[chat_router.Upload] = []
            rejected: list[str] = []
            for up in form.getlist("files"):
                if not isinstance(up, UploadFile) or not up.filename:
                    continue
                name = Path(up.filename).name
                try:
                    tmp = await _save_upload(up)
                except extract.RejectedFile as e:
                    rejected.append(f"{name}: {e}")
                    continue
                temps.append(tmp)
                files.append(chat_router.Upload(tmp, name))
        if rejected and not files and not text:
            return _error(request, "; ".join(rejected))
        if not text and not files:
            return _error(request, "请输入内容，或附上 PDF、Word 或 Markdown 文件。")
        msgs = await run_in_threadpool(chat_router.route, conn, text, files, thread_id=thread_id, page=page,
                                       http=getattr(request.app.state, "intake_http", None), fresh=fresh)
    finally:
        for t in temps:
            t.unlink(missing_ok=True)
    tid = msgs[0].thread_id
    url = f"/chat/{tid}"
    if not is_hx(request):
        return RedirectResponse(url, status_code=303)
    # A composer that named this thread shows it (the /chat page or the floating assistant): append in place.
    if thread_id == tid:
        return HTMLResponse(render_messages(conn, request, msgs), headers={"X-Chat-Thread": tid})
    return Response(status_code=204, headers={"HX-Redirect": url, "X-Chat-Thread": tid})


# ---------------------------------------------------------------- confirmation cards (docs/design/ISSUE-AGENT.md §5)
# 执行 / 不用了 / 撤销 go through pm.changes only: no model call and no spend check, so they work while AI is paused
# or the day's cap is spent. pm.changes serialises them and re-reads row status, so a double click applies once.
# Each answers with the re-rendered card (htmx swaps #plan-<batch>); a plain post goes back to the conversation.

def _batch(conn: sqlite3.Connection, batch_id: str) -> list[dict[str, Any]]:
    rows = changes.rows(conn, batch_id)
    if not rows:
        raise HTTPException(404, "这批修改已经不存在")
    return rows


def _plan_reply(request: Request, conn: sqlite3.Connection, batch_id: str, rows: list[dict[str, Any]], flash: str = "",
                posted: dict[str, Any] | None = None):
    if is_hx(request):
        return HTMLResponse(cards.render_plan(conn, batch_id, request, flash=flash, posted=posted))
    tid = rows[0]["thread_id"]
    return RedirectResponse(f"/chat/{tid}#plan-{batch_id}" if tid else "/chat", status_code=303)


def _ids(values: list[Any]) -> set[int]:
    # ASCII digits only: int() refuses '²' although isdigit() accepts it; anything else is not a row id
    return {int(v) for v in (str(x).strip() for x in values) if v.isascii() and v.isdecimal()}


def _outcome_flash(outcome: dict[str, Any]) -> str:
    # the card itself shows what happened row by row; only a click that found nothing to do needs words
    return changes.summary(outcome) if outcome.get("already") else ""


LOCKED = "助手还在处理，结束后再执行。"


@router.post("/chat/changes/{batch_id}/apply")
async def chat_changes_apply(batch_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    rows = _batch(conn, batch_id)
    form = await request.form()
    if "shown" not in form:              # only the card's own form says which rows Leo saw; never apply blind
        raise HTTPException(400, "请从卡片上执行")
    if cards.plan_locked(conn, rows):
        return _plan_reply(request, conn, batch_id, rows, LOCKED)
    pending = {r["id"] for r in rows if r["status"] == "proposed"}
    if pending - _ids(str(form.get("shown") or "").split(",")):
        return _plan_reply(request, conn, batch_id, rows, "助手又加了几处修改，请再看一眼再执行。")
    ticked = _ids(form.getlist("ids"))
    titles = {r["id"]: " ".join(str(form.get(f"title-{r['id']}")).split()) for r in rows
              if r["id"] in pending and r["action"] == "create" and f"title-{r['id']}" in form}
    # A cleared or over-long title keeps the whole card waiting, drawn as Leo left it: an empty box is
    # never read as "use the draft" (he may have meant to retype it), and the write layer would refuse it.
    if any(cards.title_problem(t) for rid, t in titles.items() if rid in ticked):
        return _plan_reply(request, conn, batch_id, rows, "有新议题的标题需要改一下，改好再执行。",
                           posted={"ticked": ticked, "titles": titles})
    edits = {rid: t for rid, t in titles.items()
             if t != (next(r for r in rows if r["id"] == rid)["new"] or {}).get("title")}
    try:
        outcome = await run_in_threadpool(changes.apply, conn, batch_id, ids=sorted(ticked), edits=edits)
    except (ValueError, KeyError) as e:
        return _plan_reply(request, conn, batch_id, rows, f"没能执行：{e}")
    return _plan_reply(request, conn, batch_id, rows, _outcome_flash(outcome))


@router.post("/chat/changes/{batch_id}/dismiss")
def chat_changes_dismiss(batch_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    rows = _batch(conn, batch_id)
    if cards.plan_locked(conn, rows):
        return _plan_reply(request, conn, batch_id, rows, LOCKED)
    n = changes.dismiss(conn, batch_id)
    return _plan_reply(request, conn, batch_id, rows, "" if n else "这批修改已经处理过了。")


@router.post("/chat/changes/{batch_id}/undo")
def chat_changes_undo(batch_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    rows = _batch(conn, batch_id)
    try:
        outcome = changes.undo(conn, batch_id)
    except (ValueError, KeyError) as e:
        return _plan_reply(request, conn, batch_id, rows, f"没能撤销：{e}")
    return _plan_reply(request, conn, batch_id, rows, _outcome_flash(outcome))


# ---------------------------------------------------------------- 交付任务 plans (agent/skills/project-setup.md)

def _setup_page(request: Request, conn: sqlite3.Connection, task_id: str, status: int = 200, error: str | None = None,
                form: dict[str, str] | None = None):
    record = materials.load_plan(task_id)
    if record is None:
        raise HTTPException(404, "没有这份方案")
    plan = record["plan"]
    from ...files import datafiles
    directions = {str(d.get("slug")): str(d.get("name") or d.get("slug")) for d in datafiles.read("directions") or []}
    papers = [dict(r) for r in conn.execute(
        f"SELECT id, title, year FROM papers WHERE id IN ({','.join('?' * len(plan['papers']))})", plan["papers"])] if plan["papers"] else []
    files = [m for m in (materials.get(mid) for mid in plan["materials"]) if m]
    # the 交付稿 Leo already set up under this title, if any: the page offers to merge into it (default)
    same = materials.same_title_parent(plan["project"]["slug"], (form or {}).get("title") or plan["deliverable"]["title"]) \
        if record.get("status") != "applied" else None
    from ...files import tasks as tasks_file
    same_subs = tasks_file.children(same["id"]) if same else []
    resp = render(request, conn, "pages/chat_setup.html", title="交付任务方案", record=record, plan=plan, files=files,
                  docx_files=[m for m in files if m.get("suffix") == ".docx"], same=same, same_subs=same_subs,
                  direction_name=directions.get(plan["project"]["direction"], ""), papers=papers, form=form or {},
                  default_question=materials.DEFAULT_QUESTION, error=error, new_file=materials.NEW_FILE, merge_new=materials.MERGE_NEW,
                  running=bool(conn.execute("SELECT 1 FROM agent_tasks WHERE id = ? AND status IN ('queued','running')",
                                            (task_id,)).fetchone()))
    resp.status_code = status
    return resp


@router.get("/chat/setup/{task_id}")
def chat_setup(task_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _setup_page(request, conn, task_id)


@router.post("/chat/setup/{task_id}/apply")
async def chat_setup_apply(task_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    record = materials.load_plan(task_id)
    if record is None:
        raise HTTPException(404, "没有这份方案")
    form = {k: str(v) for k, v in (await request.form()).items() if isinstance(v, str)}
    first = record.get("status") != "applied"
    try:
        result = await run_in_threadpool(materials.apply, conn, task_id, form)
    except (materials.PlanError, convert.ConvertError, ValueError, OSError) as e:
        return _setup_page(request, conn, task_id, 400, error=f"没能建立：{e}", form=form)
    if first and record.get("thread_id") and service.get_thread(conn, record["thread_id"]):
        facts = "，".join(x for x in (f"截止 {result['due']}" if result["due"] else "",
                                      f"目标 {result['words']} 字" if result["words"] else "") if x)
        if result.get("merged"):
            text = (f"已把这份提纲合并进已有的交付稿「{result['title']}」（课题[{result['name']}](/projects/{result['project']})）"
                    f"，补上了 {result['added']} 个子任务，原有的 {len(result['tasks']) - result['added']} 个保持不变。到[待办](/tasks)里看。")
        else:
            text = (f"已建立交付任务「{result['title']}」（课题[{result['name']}](/projects/{result['project']})）"
                    + (f"：{facts}" if facts else "") + f"。它在[待办](/tasks)里是一个父任务，按节分了 {len(result['tasks'])} 个子任务"
                    + ("，Word 文件在课题页「文件」里，可以直接打开写。" if result["file"] else "。"))
        service.add_message(conn, record["thread_id"], "assistant", text)
    return RedirectResponse(f"/chat/setup/{task_id}", status_code=303)
