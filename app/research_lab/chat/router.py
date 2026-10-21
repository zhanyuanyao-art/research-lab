"""Deterministic command router for the chat composer. Files and commands are answered without a
model; anything else becomes a `chat.reply` job whose placeholder message fills in when it completes."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx

from .. import i18n
from ..db.connection import get_setting
from ..files import datafiles
from ..files.weeks import week_of
from . import service
from .service import Message

log = logging.getLogger("research_lab.chat")

REPLY_KIND = "chat.reply"
# Strings shaped like credentials become 已隐藏 before a message is stored, routed or handed to a model
# (docs/design/SETTINGS-AGENT.md §3): a key pasted into the chat must not land in its history or a request.
_PREFIXED = (r"(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}"
             r"|(?:ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{20,}|xox[abposr]-[A-Za-z0-9\-]{10,}|Bearer\s+[A-Za-z0-9._\-]{20,}")
SECRET_RE = re.compile(
    _PREFIXED + r"|(?<![A-Za-z0-9])[0-9a-fA-F]{32,}(?![A-Za-z0-9])"
    # long base64 / random tokens: letters and digits mixed, no separators a path or a word would have
    r"|(?<![A-Za-z0-9+=_])(?=[A-Za-z0-9+=_]*\d)(?=[A-Za-z0-9+=_]*[A-Za-z])[A-Za-z0-9+=_]{32,}(?![A-Za-z0-9+=_])")
MASKED = "已隐藏"
# An explicit bug report goes to the assistant's default skill, which files it (agent/tools.report_bug, card 0090).
BUG_WORDS = re.compile(r"bug|报错|闪退", re.I)
# Inside a link only prefixed keys count: a 32-hex article id or a long query value is part of the address, and
# masking it would break the link Leo pasted.
PREFIXED_SECRET_RE = re.compile(_PREFIXED)
URL_RE = re.compile(r"https?://\S+")


def mask_secrets(text: str) -> str:
    text, out, last = text or "", [], 0
    for m in URL_RE.finditer(text):
        out += [SECRET_RE.sub(MASKED, text[last:m.start()]), PREFIXED_SECRET_RE.sub(MASKED, m.group())]
        last = m.end()
    return "".join(out) + SECRET_RE.sub(MASKED, text[last:])


def mask_page(page: str) -> str:
    """The page as stored and shown to a model, with credential-shaped strings hidden in what chat.js carries in
    its #ctx fragment (a selection can hold a pasted key)."""
    from urllib.parse import quote, unquote, urlsplit
    url = urlsplit(page or "")
    if not url.fragment.startswith("ctx="):
        return page or ""
    try:
        ctx = json.loads(unquote(url.fragment[4:]))
    except ValueError:
        ctx = None
    if not isinstance(ctx, dict):
        return url._replace(fragment="").geturl()
    clean = {str(k): mask_secrets(str(v)) for k, v in ctx.items()}
    return url._replace(fragment="ctx=" + quote(json.dumps(clean, ensure_ascii=False))).geturl()


DOI_RE = re.compile(r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/\S+)$", re.I)
EXC_RE = re.compile(r"^EXC-\d{4}-\d{4,}$", re.I)

# (usage, what it does) — shown by /help, in the composer hints, and to the chat.reply model.
COMMANDS: list[tuple[str, str]] = [
    ("/status", "今天该做的事"),
    ("/week", "本周产出、待整理的划线和提醒"),
    ("/now", "本周计划：问题、谜题、本周、下周"),
    ("/hunt [project]", "接下来最值得找的证据"),
    ("/milestones [project]", "课题进度和下一步"),
    ("/search QUERY", "全文搜索文献、划线、产出和页面文本"),
    ("/related TEXT", "与一段文字相关的划线和产出"),
    ("/papers", "最近导入的文献"),
    ("/excerpt [EXC-id]", "从本对话最近的文献划线，或查看某条划线"),
    ("/associate EXC-id", "某条划线的关联建议"),
    ("/artifacts [project|all]", "产出列表"),
    ("/seed TYPE SLUG [--project P]", "为本周新建一份初稿"),
    ("/tasks [project]", "本周到期和已逾期的待办"),
    ("/plan", "看已逾期和本周到期的待办，写下本周要做的一件事"),
    ("/议题 要做的事", "直接交给议题助手：查议题、改状态和安排、建子议题"),
    ("/help", "这份命令列表"),
]
ISSUE_COMMAND = "/议题"
CARD_KINDS = ("paper_summary", "association", "hunt", "week", "search", "excerpt", "artifacts", "milestones", "job_wait", "task",
              "tasks", "plan")


@dataclass
class Reply:
    content: str
    card_kind: str | None = None
    card_ref: str | None = None
    label: str | None = None           # intent label, used as a thread title for commands


@dataclass
class Upload:
    path: Path
    name: str


# ---------------------------------------------------------------- helpers

def _now() -> dict:
    try:
        return datafiles.read("now") or {}
    except Exception:  # noqa: BLE001 - a broken now.yml must not break the chat
        return {}


def _project_slugs() -> list[str]:
    try:
        return [str(p["slug"]) for p in datafiles.read("projects") or []]
    except Exception:  # noqa: BLE001
        return []


def _project_name(slug: str) -> str:
    try:
        for p in datafiles.read("projects") or []:
            if str(p.get("slug")) == slug:
                return str(p.get("name") or slug)
    except Exception:  # noqa: BLE001
        pass
    return slug


def _kind_label(type_: str | None) -> str:
    """Artifact type key → the visible kind (问题 / 文献地图 / 数据结果 / 结论)."""
    kind = i18n.TYPE_KIND.get(str(type_))
    return i18n.KIND[kind] if kind else i18n.TYPE.get(str(type_), str(type_ or ""))


def _paper_title(conn: sqlite3.Connection, paper_id: str | None) -> str:
    row = conn.execute("SELECT title FROM papers WHERE id = ?", (paper_id,)).fetchone() if paper_id else None
    return (row["title"] if row else "") or "这篇文献"


def _project(arg: str) -> tuple[str | None, str | None]:
    """(slug, error). Empty arg → now.yml project; a unique prefix is accepted."""
    arg = arg.strip().split()[0] if arg.strip() else ""
    slugs = _project_slugs()
    if not arg:
        slug = str(_now().get("project") or "")
        return (slug, None) if slug else (None, "还没有设定当前课题。请到[首页](/)调整计划，或在命令里写明课题，例如 `/hunt asean-centrality`。")
    if arg in slugs:
        return arg, None
    hits = [s for s in slugs if s.startswith(arg.lower())]
    if len(hits) == 1:
        return hits[0], None
    if not slugs:
        return None, f"找不到课题“{arg}”，目前还没有任何课题。"
    names = "、".join(_project_name(s) for s in slugs)
    return None, f"找不到课题“{arg}”。现有课题：{names}。命令里写课题英文标识的开头几个字母即可，例如 `/hunt {slugs[0]}`。"


def help_text() -> str:
    lines = ["可用命令如下，也可以直接用自然语言提问：", ""]
    lines += [f"- `{u}` — {d}" for u, d in COMMANDS]
    lines += ["", "把 PDF 拖到输入框，就能导入一篇文献。其他内容会交给助手，文献地图和案例时间轴会保留来源，审阅采纳后进入研究档案。"]
    return "\n".join(lines)


# ---------------------------------------------------------------- commands

def cmd_help(conn, arg, ctx) -> Reply:
    return Reply(help_text(), label="帮助")


def cmd_hunt(conn, arg, ctx) -> Reply:
    slug, err = _project(arg)
    if err:
        return Reply(err, label="证据线索")
    name = _project_name(slug)
    return Reply(f"**{name}** 接下来最值得找的证据：",
                 "hunt", slug, label=f"证据线索 · {name}")


def cmd_week(conn, arg, ctx) -> Reply:
    week = week_of()
    return Reply(f"**{i18n.week_label(week)}** 的进展：", "week", week, label="本周进展")


def cmd_now(conn, arg, ctx) -> Reply:
    now = _now()
    if not now:
        return Reply("本周计划还是空的。到[首页](/)调整计划。", label="本周计划")
    fields = [("question", "研究问题"), ("puzzle", "谜题"), ("project", "课题"), ("this_week", "本周"),
              ("next_week", "下周"), ("period", "学期")]
    lines = [f"- **{label}：** {_project_name(str(now[k])) if k == 'project' else now[k]}"
             for k, label in fields if now.get(k)]
    for key, label in (("reading", "在读"), ("need_to_find_out", "待查清")):
        items = now.get(key) or []
        if items:
            lines.append(f"- **{label}：**")
            lines += [f"    - {i}" for i in items]
    lines.append("\n要修改，到[首页](/)调整计划。")
    return Reply("\n".join(lines), label="本周计划")


def cmd_status(conn, arg, ctx) -> Reply:
    from .. import dashboard
    d = dashboard.build(conn)
    t = d["tiles"]
    # No "N weeks without output" nudge here or on the home page: Leo turned it down on 10-12.
    lines = [f"**{i18n.week_label(d['current_week'])}**"]
    if d["week_artifact"]:
        a = d["week_artifact"]
        lines.append(f"本周产出：[{a['title']}](/artifacts/{a['ref']}) · {_kind_label(a['type'])} · {i18n.LEVEL.get(a['level'], a['level'])}"
                     + (" · 还没写完" if not a["counts"] else ""))
    lines += ["", "**今天该做的事**"]
    lines += [f"{i}. [{x['text']}]({x['href']})" for i, x in enumerate(d["actions"], 1)] or ["眼下没有要紧的事。"]
    since = t["weeks_since_last"]
    lines += ["", "**概览**",
              f"- 本周新增划线：{t['excerpts_week']}",
              "- 距上次有结论的产出：" + (f"{since} 周" if since is not None else "还没有"),
              f"- 已放入文献地图的文献：{t['works_placed']} / {t['works_target']}",
              f"- 证据矩阵已查清：{t['cells_resolved']} / {t['cells_total']}"
              + (f" · 下一个关键案例：{t['decisive']}" if t.get("decisive") else "")]
    q = d["ai_counts"].get("queued", 0)
    if q:
        lines.append(f"- 助手待处理的任务：{q} · [去设置查看](/settings)")
    return Reply("\n".join(lines), label="今日概览")


def cmd_search(conn, arg, ctx) -> Reply:
    q = re.sub(r"^(for|me|papers? (on|about)|anything (on|about)|about)\s+", "", arg.strip(), flags=re.I).strip()
    if not q:
        return Reply("要搜什么？例如 `/search 东盟中心地位`。", label="搜索")
    return Reply(f"“{q}”的搜索结果。", "search", q, label=f"搜索 · {q}"[:60])


def cmd_related(conn, arg, ctx) -> Reply:
    from ..search.related import related
    text = arg.strip()
    if not text:
        return Reply("和什么相关？例如 `/related 出席率下降，但主席规则依然有效`。", label="相关")
    cands = related(conn, text=text, limit=8)
    if not cands:
        return Reply("划线和产出里还没有与这段文字相关的内容。", label="相关")
    lines = [f"与“{text[:80]}”相关：", ""]
    for c in cands:
        href = f"/excerpts/{c.id}" if c.type == "excerpt" else f"/artifacts/{c.id}"
        snip = " ".join(c.snippet.split())[:160]
        kind = "划线" if c.type == "excerpt" else "产出"
        lines.append(f"- [{c.title}]({href}) · {kind} · 相关度 {c.score:.2f}  \n  {snip}")
    return Reply("\n".join(lines), label=f"相关 · {text}"[:60])


def cmd_papers(conn, arg, ctx) -> Reply:
    rows = conn.execute("SELECT id, title, year, metadata_status, ai_summary_json IS NOT NULL AS summarized "
                        "FROM papers ORDER BY created_at DESC, id LIMIT 10").fetchall()
    if not rows:
        return Reply("文献库还是空的。把 PDF 拖到输入框就能导入。", label="文献")
    lines = ["最近导入的文献：", ""]
    for r in rows:
        tail = " · ".join(x for x in (str(r["year"]) if r["year"] else "",
                                      "引用信息已核验" if r["metadata_status"] == "verified" else "引用信息待核验",
                                      "已有概要" if r["summarized"] else "暂无概要") if x)
        lines.append(f"- [{r['title']}](/papers/{r['id']}) · {tail}")
    lines.append("\n全部文献见[文献库](/papers)。")
    return Reply("\n".join(lines), label="文献")


def cmd_excerpt(conn, arg, ctx) -> Reply:
    arg = arg.strip()
    if EXC_RE.match(arg):
        eid = arg.upper()
        if not conn.execute("SELECT 1 FROM excerpts WHERE id = ?", (eid,)).fetchone():
            return Reply("找不到这条划线。", label="划线")
        return Reply("这条划线：", "excerpt", eid, label="划线")
    paper_id = service.latest_paper_in_thread(conn, ctx["thread_id"]) if ctx.get("thread_id") else None
    if not paper_id:
        return Reply("这个对话里还没有文献。把 PDF 拖进来，或打开一篇文献，在阅读页按 `e` 划线。", label="划线")
    title = _paper_title(conn, paper_id)
    return Reply(f"要从《{title}》划线：打开[阅读页](/papers/{paper_id}/read)，按 `e`，或选中文字后在弹出的面板里保存。"
                 f"这篇文献已有的划线在[划线页签](/papers/{paper_id}#marks)。",
                 label=f"划线 · {title}"[:60])


def cmd_associate(conn, arg, ctx) -> Reply:
    eid = arg.strip().upper()
    if not EXC_RE.match(eid):
        return Reply("请指明某条划线，例如 `/associate EXC-2026-0014`。", label="关联建议")
    if not conn.execute("SELECT 1 FROM excerpts WHERE id = ?", (eid,)).fetchone():
        return Reply("找不到这条划线。", label="关联建议")
    return Reply("这条划线的关联建议。你确认之前，不会应用任何一条。", "association", eid, label="关联建议")


def cmd_artifacts(conn, arg, ctx) -> Reply:
    arg = arg.strip()
    if not arg or arg.lower() == "all":
        return Reply("全部产出，最新的在前。", "artifacts", "all", label="产出")
    slug, err = _project(arg)
    if err:
        return Reply(err, label="产出")
    name = _project_name(slug)
    return Reply(f"课题 **{name}** 的产出。", "artifacts", slug, label=f"产出 · {name}")


def cmd_milestones(conn, arg, ctx) -> Reply:
    slug, err = _project(arg)
    if err:
        return Reply(err, label="课题进度")
    name = _project_name(slug)
    return Reply(f"课题 **{name}** 的进度。", "milestones", slug, label=f"课题进度 · {name}")


def cmd_seed(conn, arg, ctx) -> Reply:
    from .. import weekly
    from ..files import artifacts
    usage = ("用法：`/seed TYPE SLUG [--project P]`，例如 `/seed puzzle chair-rule`。"
             "SLUG 是这份产出的英文短名，只能用小写字母、数字和连字符。")
    project = None
    m = re.search(r"--project[= ]\s*(\S+)", arg)
    if m:
        project = m.group(1)
        arg = (arg[:m.start()] + arg[m.end():]).strip()
    words = [w for w in re.split(r"\s+", arg.strip()) if w and w.lower() not in ("a", "an", "of", "type", "called", "named", "seed", "artifact")]
    types = [str(t["key"]) for t in datafiles.read("types") or []]
    type_ = next((w.lower() for w in words if w.lower() in types), None)
    if type_ is None:
        choices = "、".join(f"{i18n.TYPE.get(t, t)} `{t}`" for t in types)
        return Reply(f"{usage}可选类型，写在命令里：{choices}。", label="新建初稿")
    rest = [w for w in words if w.lower() != type_]
    slug = weekly.slugify(" ".join(rest))
    if not slug:
        return Reply(usage, label="新建初稿")
    if project:
        project, err = _project(project)
        if err:
            return Reply(err, label="新建初稿")
    else:
        project = str(_now().get("project") or "")
    try:
        path = weekly.create_artifact(type_, slug, "", "seed", project)
    except weekly.WeeklyError as e:
        return Reply(f"无法新建初稿：{e}", label="新建初稿")
    artifacts.index(conn)
    made = artifacts.load(path)
    ref = made.ref
    where = f"，归入课题 {_project_name(project)}" if project else ""
    return Reply(f"已新建一份初稿：[{made.get('title') or slug}](/artifacts/{ref})，类型为{_kind_label(type_)}{where}。"
                 f"结论要你自己写，助手不会代填。", "artifacts", ref, label=f"新建初稿 · {slug}")


def cmd_tasks(conn, arg, ctx) -> Reply:
    """Open tasks for this week and anything overdue, all 课题 or one; answered from _data/tasks.yml."""
    from ..files import tasks as T
    slug = None
    if arg.strip():
        slug, err = _project(arg)
        if err:
            return Reply(err, label="待办")
    rows = T.read()
    if slug:
        rows = T.for_project(slug, rows)
    if not rows:
        where = f"课题 **{_project_name(slug)}** " if slug else ""
        return Reply(f"{where}还没有待办。到[待办页](/tasks/new)添加，或在课题页的未过检查旁点「加为待办」。", label="待办")
    from .cards import issue_split
    g, parents = issue_split(rows, week_of())
    name = f" · {_project_name(slug)}" if slug else ""
    bits = ([f"{len(parents)} 个父任务在进行"] if parents else []) + [f"本周到期 {len(g['week'])} 条"]
    if g["overdue"]:
        bits.append(f"逾期 {len(g['overdue'])} 条")
    if g["later"]:
        bits.append(f"以后 {len(g['later'])} 条")
    return Reply("**待办** · " + "，".join(bits) + "。", "tasks", slug or "all", label=f"待办{name}"[:60])


def cmd_plan(conn, arg, ctx) -> Reply:
    """Monday's view by due date (no plan week since card 0093): what is overdue, what is due this week, what is in
    progress with no due date, and the one line for 本周."""
    from ..files import tasks as T
    week = week_of()
    rows = T.read()
    g = T.groups(data=rows)
    undated = [t for t in rows if t.get("status") == "doing" and not t.get("due")]
    now = _now()
    lines = [f"**{i18n.week_label(week)} 的安排**"]
    if now.get("this_week"):
        lines.append(f"- 本周：{now['this_week']}")
    if g["overdue"]:
        lines.append(f"- 已逾期 {len(g['overdue'])} 条，逐条决定：改到周五、改到下周五、搁置，或已经做完。")
    lines.append(f"- 本周到期 {len(g['week'])} 条。" if g["week"] else "- 本周没有到期的待办。")
    if undated:
        lines.append(f"- 进行中但没有截止 {len(undated)} 条，可以给它们定个截止。")
    return Reply("\n".join(lines), "plan", week, label="本周安排")


def cmd_undo(conn, arg, ctx) -> Reply:
    """「撤销刚才的修改」 without a model: what the newest request in this conversation changed directly
    (pm/changes.py checks each field; what Leo changed since stays). Only that request: if it only left
    a card, nothing older is undone. 「把刚才对第二章的修改撤销」 names the issue it expects."""
    from ..files import issues
    from ..pm import changes
    tid = ctx.get("thread_id")
    found = changes.batches(conn, thread_id=tid) if tid else []
    if not found:
        return Reply("这个对话里没有可以撤销的修改。", label="撤销")
    last = max(found, key=lambda b: max(r["id"] for r in b["rows"]))
    task_id = last["rows"][0]["task_id"]
    mine = [b for b in found if b["rows"][0]["task_id"] == task_id] if task_id else [last]
    undoable = [b for b in mine if b["undoable"]]
    if not undoable:
        if any(b["status"] == "proposed" for b in mine):
            return Reply("刚才的修改还在确认卡片上等你点执行，没有已经生效、可以撤销的修改。", label="撤销")
        return Reply("刚才那次请求没有可以撤销的修改。", label="撤销")
    target = re.sub(r"\s+", "", arg or "").casefold()
    if target and not any(target in re.sub(r"\s+", "", r["label"] or "").casefold()
                          for b in undoable for r in b["rows"]):
        return Reply(f"刚才的修改没有动到「{arg.strip()}」，所以没有撤销。", label="撤销")
    merged: dict = {}
    for b in undoable:
        for key, value in changes.undo(conn, b["batch_id"]).items():
            if isinstance(value, list):
                merged.setdefault(key, []).extend(value)
    merged.update(batch_id=undoable[-1]["batch_id"], already=False)
    links = []
    for r in merged.get("undone", []) + merged.get("kept", []):
        row = issues.get(r["target"]) if r["target"] and r["result"] != "deleted" else None
        link = f"[{row['title']}]({row['url']})" if row else f"「{r['label']}」"
        if link not in links:
            links.append(link)
    text = changes.summary(merged) + (f"\n\n涉及：{'、'.join(links)}。" if links else "")
    return Reply(text, label="撤销")


def cmd_tweaks(conn, arg, ctx) -> Reply:
    """「现在都调了哪些」 without a model: settings that differ from their default, and the assistant's latest
    changes to push sources and directions (feeds.yml / directions.yml have no default)."""
    from .. import tunables
    from ..agent.tools import _who
    lines = []
    for d in tunables.changed(conn):
        who = _who(conn, d["key"], d)
        state = "存的值无效，按默认" if d["broken"] else d["value_label"]
        lines.append(f"- {d['name']}：{state}（默认 {d['default_label']}）" + (f" · {who}" if who else ""))
    files = []
    for r in conn.execute("SELECT target, label, detail_json FROM assistant_changes WHERE kind = 'setting' AND status = 'applied' "
                          "AND actor = 'assistant' ORDER BY id DESC LIMIT 40"):
        try:
            t = tunables.entry(r["target"])
        except KeyError:
            continue
        if t.store == tunables.SETTINGS or len(files) >= 5:
            continue
        detail = json.loads(r["detail_json"]) if r["detail_json"] else {}
        files.append(f"- {t.name}：{tunables.label(t, detail.get('old'))} → {tunables.label(t, detail.get('new'))}")
    if not lines and not files:
        return Reply("都是默认设置。想调什么，直接跟助手说。", label="当前微调")
    out = (["**和默认不同的设置**"] + lines if lines else []) + (["", "**助手最近改过的推送来源和方向**"] + files if files else [])
    out += ["", "想改回去，跟助手说「恢复默认」或「撤销」，也可以在[设置](/settings#tweaks)里点。"]
    return Reply("\n".join(out).strip(), label="当前微调")


def cmd_progress(conn, arg, ctx) -> Reply:
    """「本周进度」: the §6 checks without a model, as the progress card (chat/cards.py, card 0078) with a
    one-line summary. 「进度怎么样」 on an issue or 课题 page checks that one."""
    from datetime import date
    from ..agent.tools import issue_for_page
    from ..pm import checkup
    scope, target = "week", ""
    ref = None if arg else issue_for_page(ctx.get("page"))
    if ref:
        scope, target = ("project", ref[4:]) if ref.startswith("PRJ-") else ("issue", ref)
    report = checkup.run(conn, scope, target)
    n = len(report["attention"])
    text = f"**{report['title']}** · {report['subtitle']}：" + (f"要注意的有 {n} 条。" if n else "没有要注意的。")
    card = (f"{scope}:{target}" if target else scope) + f"@{date.today().isoformat()}"
    return Reply(text, "progress", card, label=report["title"][:60])


def cmd_doi(conn, doi, ctx) -> Reply:
    from ..intake import dedupe, pipeline
    d = pipeline.normalize_doi(doi)
    have = dedupe.by_doi(conn, d)
    if have:
        return Reply(f"这个 DOI 对应的文献已在文献库里：[{_paper_title(conn, have)}](/papers/{have})。",
                     "paper_summary", have, label=f"DOI · {d}"[:60])
    return Reply(f"`{d}` 需要 PDF 才能完整导入。把 PDF 拖进来，上传时会自动查询引用信息，导入后也可以在文献页重新核验。"
                 f"只凭 DOI 不会生成任何引用信息。", label=f"DOI · {d}"[:60])


Handler = Callable[[sqlite3.Connection, str, dict], Reply]

SLASH: dict[str, Handler] = {
    "help": cmd_help, "commands": cmd_help, "hunt": cmd_hunt, "week": cmd_week, "now": cmd_now, "status": cmd_status,
    "search": cmd_search, "find": cmd_search, "related": cmd_related, "papers": cmd_papers, "excerpt": cmd_excerpt,
    "associate": cmd_associate, "association": cmd_associate, "artifacts": cmd_artifacts, "milestones": cmd_milestones,
    "milestone": cmd_milestones, "seed": cmd_seed, "today": cmd_status,
    "tasks": cmd_tasks, "todo": cmd_tasks, "plan": cmd_plan,
}

# Natural forms: (pattern on the whole message, handler, group holding the argument or None)
NATURAL: list[tuple[re.Pattern, Handler, int | None]] = [
    (re.compile(r"^(?:help|commands|what can you do)\??$", re.I), cmd_help, None),
    (re.compile(r"^(?:how(?:'s| is| are)\s+(?:this|the)\s+week\b.*|week status|this week\??)$", re.I), cmd_week, None),
    (re.compile(r"^(?:status|what should i do(?: today| now)?\??|today)$", re.I), cmd_status, None),
    (re.compile(r"^(?:what am i working on\??|now)$", re.I), cmd_now, None),
    (re.compile(r"^(?:hunt|hunt list|what should i hunt(?: next)?)\b\s*(?:for\s+)?(.*?)\??$", re.I), cmd_hunt, 1),
    (re.compile(r"^(?:milestones?|next milestone)\b\s*(?:for\s+)?(.*?)\??$", re.I), cmd_milestones, 1),
    (re.compile(r"^(?:recent|latest)\s+papers\b.*$|^papers$", re.I), cmd_papers, None),
    (re.compile(r"^create\s+(?:a\s+|an\s+)?(?:seed|artifact)\b\s*(.*)$", re.I), cmd_seed, 1),
    (re.compile(r"^(?:find|search)\s+(.+)$", re.I), cmd_search, 1),
    (re.compile(r"^related\s+(?:to\s+)?(.+)$", re.I), cmd_related, 1),
    # Chinese phrasings; anything else in Chinese goes to the assistant as before.
    (re.compile(r"^(?:撤销|撤回)(?:一下)?(?:刚才|刚刚|上一次|上次|最近一次)?的?(?:修改|改动|操作|更改)?[。.!！]?$"), cmd_undo, None),
    (re.compile(r"^把?(?:刚才|刚刚|上一次|上次|最近一次)(?:对|给)?(.{0,30}?)的?(?:修改|改动|操作|更改)(?:都)?(?:撤销|撤回)(?:掉|吧)?[。.!！]?$"),
     cmd_undo, 1),
    (re.compile(r"^(?:给我)?(?:一个|一份)?(?:看看)?(本周|这周)的?进度(?:怎么样|如何)?[？?。.]?$"), cmd_progress, 1),
    (re.compile(r"^进度(?:怎么样|如何)[？?。.]?$"), cmd_progress, None),
    (re.compile(r"^(?:现在|目前)?(?:都)?(?:调|改)了?哪些(?:设置)?[？?]?$|^(?:当前|现在)的?微调[？?]?$|^改过哪些设置[？?]?$"),
     cmd_tweaks, None),
    (re.compile(r"^(?:帮助|你能做什么|有哪些命令)[？?]?$"), cmd_help, None),
    (re.compile(r"^(?:这周|本周)(?:进展|怎么样|如何|进展如何)[？?]?$"), cmd_week, None),
    (re.compile(r"^(?:今天(?:该做什么|做什么)?|今日概览|现在该做什么)[？?]?$"), cmd_status, None),
    (re.compile(r"^(?:本周计划|我在做什么|我现在在做什么)[？?]?$"), cmd_now, None),
    (re.compile(r"^(?:下一步)?该找什么证据[？?]?$|^证据线索$"), cmd_hunt, None),
    (re.compile(r"^(?:课题进度|课题走到哪一步了|进度)[？?]?$"), cmd_milestones, None),
    (re.compile(r"^(?:最近(?:导入|添加)?的?文献|文献列表)[？?]?$"), cmd_papers, None),
    (re.compile(r"^(?:搜索|查找|搜)\s*(.+)$"), cmd_search, 1),
    (re.compile(r"^(?:和|与)(.+?)(?:相关的|有关的)(?:划线|内容|材料)?[？?]?$"), cmd_related, 1),
    (re.compile(r"^(?:待办|我的待办|本周待办|有什么待办|待办事项)[？?]?$"), cmd_tasks, None),
    (re.compile(r"^(?:tasks?|todos?)$", re.I), cmd_tasks, None),
    (re.compile(r"^(?:定本周计划|排本周|周一计划|做计划|本周怎么安排)[？?]?$"), cmd_plan, None),
    (re.compile(r"^plan$", re.I), cmd_plan, None),
]


def match(text: str, ctx: dict | None = None) -> tuple[Handler, str] | None:
    """The deterministic handler and its argument for a message, or None for the model."""
    t = text.strip()
    if issue_request(t) is not None:
        return None
    if t.startswith("/"):
        head, _, arg = t[1:].partition(" ")
        h = SLASH.get(head.lower())
        if h is None:
            return (lambda conn, a, c: Reply(f"不认识的命令 `/{head}`。\n\n" + help_text(), label="帮助")), arg
        return h, arg
    from ..agent import skills
    if skills.pick(t).name in ("literature-map", "case-timeline"):
        return None
    m = DOI_RE.match(t)
    if m:
        return cmd_doi, m.group(1)
    for pat, h, group in NATURAL:
        m = pat.match(t)
        if m:
            return h, (m.group(group) or "") if group else ""
    return None


def issue_request(text: str) -> str | None:
    """The request after an explicit `/议题`, which skips trigger routing; None without the prefix."""
    t = text.strip()
    if t == ISSUE_COMMAND or t.startswith(ISSUE_COMMAND + " ") or t.startswith(ISSUE_COMMAND + "\u3000"):
        return t[len(ISSUE_COMMAND):].strip()
    return None


# ---------------------------------------------------------------- files

def _ingest(conn: sqlite3.Connection, up: Upload, http: httpx.Client | None) -> Reply:
    from ..intake import extract, pipeline
    try:
        r = pipeline.ingest(conn, up.path, http=http, original_name=up.name)
    except extract.RejectedFile as e:
        return Reply(f"无法导入 **{up.name}**：{str(e).rstrip('.。')}。", label=f"导入 · {up.name}")
    if r.created and r.paper_id:
        p = pipeline.get_paper(conn, r.paper_id)
        verified = "引用信息已核验。" if r.metadata_status == "verified" else "引用信息待核验，请到文献页核对。"
        tail = "助手正在写概要。" if r.job_id else "暂未生成概要。"
        notes = "".join(f"\n- {n}" for n in r.notes)
        return Reply(f"已导入[{p['title']}](/papers/{r.paper_id})。{verified}{tail}{notes}", "paper_summary", r.paper_id,
                     label=f"导入 · {p['title']}"[:60])
    if r.pending:
        why = {"same file": "文件完全相同", "same DOI": "DOI 相同", "similar title": "标题相近"}.get(r.reason or "", "内容相近")
        return Reply(f"**{up.name}** 看起来和文献库里的[{_paper_title(conn, r.duplicate_of)}](/papers/{r.duplicate_of})重复，"
                     f"依据是{why}。请到[文献库](/papers?pending={r.pending})确认：是同一篇，还是另一篇。",
                     label=f"导入 · {up.name}"[:60])
    dup = r.duplicate_of or ""
    title = _paper_title(conn, dup)
    return Reply(f"这篇文献已在文献库里：[{title}](/papers/{dup})。", "paper_summary", dup, label=f"导入 · {title}"[:60])


def _material(up: Upload, thread_id: str, hint: bool) -> tuple[Reply, bool]:
    """Leo's own draft or notes (Word, Markdown, text): kept for a 交付任务, not imported as a paper."""
    from ..files import convert, materials
    try:
        m = materials.save(up.path, up.name, thread_id)
    except convert.ConvertError as e:
        return Reply(f"读不了 **{up.name}**：{str(e).rstrip('。.')}。", label=f"材料 · {up.name}"[:60]), False
    tail = "告诉我提纲、截止日期和目标字数，我把它建成交付任务（Word 文件 + 每节一条待办），你确认后才写入。" if hint else ""
    return Reply(f"已收到材料 **{m['name']}**：{m['sections']} 节，约 {m['chars']} 字。{tail}", label=f"材料 · {m['name']}"[:60]), True


# ---------------------------------------------------------------- entry point

def _pick_thread(conn: sqlite3.Connection, thread_id: str | None, page: str) -> str | None:
    if thread_id and service.get_thread(conn, thread_id):
        return thread_id
    if page.startswith("/chat"):
        return None
    minutes = int(get_setting(conn, "chat.reuse_minutes", 30) or 0)
    if minutes <= 0:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).replace(microsecond=0).isoformat()
    row = conn.execute("SELECT id FROM chat_threads WHERE archived = 0 AND updated_at >= ? "
                       "ORDER BY updated_at DESC LIMIT 1", (cutoff,)).fetchone()
    return row["id"] if row else None


def queue_reply(conn: sqlite3.Connection, thread_id: str) -> Message:
    """Placeholder assistant message + a `chat.reply` job that fills it."""
    from ..ai import dispatch, queue
    ph = service.add_message(conn, thread_id, "assistant", "", card_kind="job_wait")
    try:
        job_id = queue.enqueue(conn, REPLY_KIND, str(ph.id))
    except Exception as e:  # noqa: BLE001 - building the context failed; say so instead of hanging
        log.exception("chat.reply enqueue failed")
        service.update_message(conn, ph.id, content=f"没能把这条消息交给助手：{e}", card_kind=None)
        return service.get_message(conn, ph.id)  # type: ignore[return-value]
    service.update_message(conn, ph.id, job_id=job_id, card_ref=job_id)
    if job_id:
        try:
            dispatch.dispatch(job_id)
        except Exception:  # noqa: BLE001 - the job stays queued for `rl agent`
            log.exception("dispatch of %s failed", job_id)
    return service.get_message(conn, ph.id)  # type: ignore[return-value]


def start_task(conn: sqlite3.Connection, thread_id: str, text: str, skill: str | None = None) -> Message | None:
    """Hand a free-text request to the task agent (skills + tools, any engine). None when no engine
    runs by itself, so the request falls back to the queued chat reply."""
    from ..agent import drivers, tasks
    from ..agent import skills
    from ..ai import claude_code, dispatch, lease
    engine_missing = drivers.active_engine(conn) is None or not claude_code.WORKER.hosted
    prev = tasks.latest_in_thread(conn, thread_id)
    # Named explicitly so a research parent cannot pull an issue action back into its workflow
    # (tasks.create keeps a follow-up in the parent's research skill otherwise). An answer to the issue
    # agent's question stays with it even when it names a 文献地图.
    if skill is None and BUG_WORDS.search(text):
        skill = skills.assistant().default_skill     # it holds report_bug; 「设置页有个 bug」 is not a settings change
    if skill is None:
        answering = prev is not None and prev["skill"] == "issues" and prev["status"] == "waiting"
        if answering or skills.is_issue_request(text):
            skill = "issues"
    is_research = skill != "issues" and (skills.pick(text).name in ("literature-map", "case-timeline")
                                         or bool(prev is not None and prev["research_session_id"]))
    if engine_missing and not is_research:
        return None
    parent = prev["id"] if prev is not None and (prev["status"] in ("done", "waiting", "failed") or (is_research and prev["status"] in tasks.OPEN)) else None
    if skill == "issues" and prev is not None and prev["research_session_id"]:
        parent = None     # a research conversation's history is long and says nothing about issues
    ph = service.add_message(conn, thread_id, "assistant", "", card_kind="task")
    task_id = tasks.create(conn, text, thread_id=thread_id, message_id=ph.id, parent_id=parent, skill=skill)
    service.update_message(conn, ph.id, card_ref=task_id)
    if engine_missing:
        tasks._finish(conn, task_id, "waiting", report="研究请求已保留。请先在设置中连接助手，然后回到这里回复“继续”。")
    elif dispatch.paused(conn):
        tasks._finish(conn, task_id, "waiting", report="AI 已暂停。在设置里恢复后，回到这里回复“继续”。")
    elif lease.acquire(conn):
        tasks.RUNNER.submit(task_id)
    # otherwise another process holds the lease: the task stays queued and its worker takes it within a minute
    return service.get_message(conn, ph.id)


def route(conn: sqlite3.Connection, text: str, files: list[Upload] | None = None, *, thread_id: str | None = None,
          page: str = "", http: httpx.Client | None = None, fresh: bool = False) -> list[Message]:
    """Save the user's message, answer it, and return [user message, assistant message(s)]. `fresh` starts a new
    thread when none is named, where a message from an ordinary page would continue a recently active one (the
    floating assistant shows an empty conversation then)."""
    text = mask_secrets((text or "").strip())
    page = mask_page(page)
    files = files or []
    if not text and not files:
        raise ValueError("empty message")
    forced = issue_request(text) if text else None
    handler = match(text) if text and forced is None else None
    tid = None if fresh and not thread_id else _pick_thread(conn, thread_id, page)
    new_thread = tid is None
    if new_thread:
        tid = service.create_thread(conn, project_slug=str(_now().get("project") or "") or None)
    assert tid is not None
    content = text
    if files:
        names = ", ".join(f.name for f in files)
        content = (text + "\n\n" if text else "") + f"附件：{names}"
    # The page Leo sends from rides along on his message (card 'page', never rendered): the issue tools
    # read 「这条」 from it, also when the request runs in another process, and a page that is not an
    # issue tells them 这条 means something else.
    out = [service.add_message(conn, tid, "user", content, card_kind="page" if page else None, card_ref=page or None)]

    from ..files import convert
    labels: list[str] = []
    got_material = False
    for up in files:
        if convert.supported(up.name):
            rep, saved = _material(up, tid, hint=not text)
            got_material = got_material or saved
        else:
            rep = _ingest(conn, up, http)
        labels.append(rep.label or "")
        out.append(service.add_message(conn, tid, "assistant", rep.content, card_kind=rep.card_kind, card_ref=rep.card_ref))

    if text:
        if forced is not None:
            if forced:
                out.append(start_task(conn, tid, forced, "issues") or queue_reply(conn, tid))
            else:
                rep = Reply("在 `/议题` 后面写要做的事，例如 `/议题 把第二章改成进行中`。", label="议题")
                labels.append(rep.label or "")
                out.append(service.add_message(conn, tid, "assistant", rep.content))
        elif handler:
            fn, arg = handler
            ctx = {"thread_id": tid, "page": page}
            try:
                rep = fn(conn, arg, ctx)
            except Exception as e:  # noqa: BLE001 - a failing command answers with its error
                log.exception("chat command failed")
                rep = Reply(f"这条命令执行失败：{e}")
            labels.append(rep.label or "")
            out.append(service.add_message(conn, tid, "assistant", rep.content, card_kind=rep.card_kind, card_ref=rep.card_ref))
        else:
            # only the 交付任务 skill can read materials, so a message that brings them goes there
            out.append(start_task(conn, tid, text, "project-setup" if got_material else None) or queue_reply(conn, tid))

    if new_thread:
        service.set_title(conn, tid, service.infer_title(text, next((l for l in labels if l), None)))
    return out
