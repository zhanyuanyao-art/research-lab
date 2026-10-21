"""Theo steers a replication from a sentence (card 0212), through the functions the replication page's buttons call
(web/routes/data.py, replicate.py, run_all.py); no logic of theirs is copied here.

Read   list_replications, get_replication
Write  set_replication_targets   复现清单 ticks and 汇报人 (coverage.save_marks)
       add_replication_data      a data link, a data source or an imported dataset (plans.save_links / save_sources /
                                 attach_dataset)
       run_replication           Run all, the R route, the Python translation route, a rerun, one step again, the deck
       stop_replication          the running R step and the Python session (rrun.stop, workspace.stop)

Tiers, as for issues and settings (docs/design/ISSUE-AGENT.md §5, SETTINGS-AGENT.md §3): an edit the user's own words
name goes straight through and can be undone; one the assistant picked, or a link the user did not type, waits on a
confirmation card; anything that runs long or may spend tokens always waits on a card. A question writes nothing and
makes no card: the tool hands back the lines to ask with, and a bare yes to exactly those lines counts as the order.
Every write is a row of assistant_changes kind 'replication' applied by the handlers below, so cards and 撤销 work as
for issues. A run that was started cannot be put back: its row is applied with changes.ONCE.

Nothing here runs author code: R runs only inside rrun's sandbox and the Python route only inside the workspace's,
each started by its own function after the user's click on the card.
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("research_lab.agent.replication_tools")

KIND = "replication"
F_TICK, F_OWNER = "复现清单", "汇报人"
F_LINK, F_SOURCE, F_DATASET = "数据链接", "数据来源", "数据集"
F_STOP = "停止"
YES, NO = "复现", "不复现"
ATTACHED, DETACHED = "挂上", "没挂"
SEP = " · "
THIS = {"", "这个", "这篇", "这份", "当前", "这个方案", "这个复现", "这篇论文", "这份方案"}
# action → (what the card says, what it costs or how long it takes)
OPS = {
    "run_all": ("一键跑完", "取包、装包、跑 R 都不花 token；报告这一步调用 AI，约 1 到 2 万 token；整轮可能要几十分钟"),
    "r_run": ("运行作者 R 代码", "没有报错就不调用 AI；报错时 AI 做最小修补，按次花 token"),
    "r_install": ("装 R 包", "要联网，可能要几十分钟，不花 token"),
    "python": ("Python 翻译路线", "AI 把作者脚本翻成 Python 再跑，估计 {low}–{high} 万 token"),
    "rerun": ("重跑", "用这次运行存下的代码再跑一遍，不调用 AI"),
    "scan": ("扫描表图", "按规则重新列出论文的表和图，不花 token"),
    "compare": ("对照", "按规则和论文报告值对照，不花 token"),
    "deck": ("重新生成展示稿", "不花 token，一两分钟；你在展示稿页改过的字会保留"),
    "export": ("导出数据文件夹", "不花 token；放进课题的草稿文件夹"),
}
OP_BY_LABEL = {label: key for key, (label, _) in OPS.items()}
STEP_WORDS = {"waiting": "等待", "running": "进行中", "done": "完成", "blocked": "卡住", "failed": "出错", "skipped": "跳过"}
MODE_WORDS = {"ai": "Python 翻译", "rerun": "重跑", "rrun": "作者 R 代码"}
RUN_STATE_WORDS = {"done": "完成", "error": "出错", "stopped": "中途停了", "running": "在跑"}
GROUP_REFS = {"正文结果": ("main",), "正文": ("main",), "附录稳健性": ("robust",), "稳健性": ("robust",),
              "附录其他": ("other",), "附录": ("robust", "other"), "全部": ("main", "robust", "other"),
              "所有": ("main", "robust", "other"), "all": ("main", "robust", "other")}
# the words in the user's message that name a whole group
GROUP_SAID = {"main": re.compile(r"正文|全部|所有|全都"), "robust": re.compile(r"稳健|附录|全部|所有|全都"),
              "other": re.compile(r"附录|全部|所有|全都")}
ONLY = re.compile(r"只|仅|就复现|其余|其他的?都|别的都")
URL_RE = re.compile(r"https?://\S+", re.I)
MAX_LIST = 60              # 复现清单 items listed by get_replication; the rest are counted
MAX_PLANS = 30
CARD_NOTE = ("Nothing has started or changed yet: this waits on a confirmation card under your reply. Tell the user in "
             "one short Chinese sentence what will happen, what it costs or how long it takes (the card's note), and "
             "that the card waits for their click. Do not call a write tool again for it.")
DONE_NOTE = ("Start your report with this, keep the plan's link, and add that the user can undo it (撤销 under the "
             "reply, or by saying 撤销).")


def _tools():
    from . import tools
    return tools


def _clip(text: Any, n: int = 120) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------- which plan

PAGE = re.compile(r"^/(?:data/plans|replicate)/([A-Za-z0-9_-]+)")


def _page_plan(conn, task_id: str | None) -> str | None:
    """The replication page the request was sent from (/data/plans/<id>…, /replicate/<id>), or the one the request it
    answers was sent from; any other page means 「这个」 is not a plan."""
    T = _tools()
    seen: set[str] = set()
    t = T._task_row(conn, task_id)
    while t is not None and t["id"] not in seen and len(seen) < 6:
        seen.add(t["id"])
        m = conn.execute("SELECT card_kind, card_ref FROM chat_messages WHERE thread_id = ? AND role = 'user' AND id < ? "
                         "ORDER BY id DESC LIMIT 1", (t["thread_id"], t["message_id"])).fetchone() if t["message_id"] else None
        page = str(m["card_ref"] or "") if m is not None and m["card_kind"] == "page" else ""
        if page:
            hit = PAGE.match(page)
            if hit:
                return hit.group(1)
            if not page.startswith("/chat"):
                return None
        t = T._task_row(conn, t["parent_id"])
        if t is None or t["status"] != "waiting":
            return None
    return None


def _name(conn, p: dict) -> str:
    """「Jost 2024」: the paper's first author and year, else a short title."""
    from ..excerpts import cite_zh, short_title
    if p.get("paper_id"):
        row = conn.execute("SELECT authors, year, title FROM papers WHERE id = ?", (p["paper_id"],)).fetchone()
        if row is not None:
            return cite_zh(row["authors"], row["year"], row["title"]) or short_title(row["title"], 36)
    return short_title(p["plan"].get("title") or "", 36) or "未关联论文的复现方案"


def _link(conn, p: dict) -> str:
    return f"[{_name(conn, p)}](/data/plans/{p['id']})"


def _haystack(conn, p: dict) -> str:
    authors = ""
    if p.get("paper_id"):
        row = conn.execute("SELECT authors FROM papers WHERE id = ?", (p["paper_id"],)).fetchone()
        authors = str(row[0] or "") if row else ""
    return " ".join([p["id"], str(p.get("paper_title") or ""), str(p.get("paper_year") or ""), authors,
                     str(p["plan"].get("title") or ""), _name(conn, p)]).casefold()


def _choices(conn, rows: list[dict], n: int = 8) -> str:
    return "; ".join(f"{p['id']} {_name(conn, p)}" for p in rows[:n]) + (f"; and {len(rows) - n} more" if len(rows) > n else "")


def _plan(conn, task_id: str | None, ref: Any) -> dict:
    from ..replication import plans
    T = _tools()
    ref = " ".join(str(ref or "").split())
    every = plans.list_all(conn)
    if not every:
        raise T.ToolError("there is no replication plan yet: the user makes one on the 数据 page from a paper "
                          "(or asks for 「给这篇做一个复现方案」)")
    if ref in THIS:
        page = _page_plan(conn, task_id)
        hit = plans.get(conn, page) if page else None
        if hit:
            return hit
        if len(every) == 1:
            return every[0]
        raise T.ToolError("which replication plan? ask the user (ask_user) with these titles: " + _choices(conn, every))
    exact = [p for p in every if p["id"].casefold() == ref.casefold()]
    if exact:
        return exact[0]
    words = [w for w in re.split(r"[\s,，、·]+", ref.casefold()) if w]
    hits = [p for p in every if all(w in _haystack(conn, p) for w in words)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise T.ToolError(f"no replication plan matches {ref!r}; plans: " + _choices(conn, every))
    raise T.ToolError("several plans match; ask the user which one (ask_user) with these titles: " + _choices(conn, hits))


# ---------------------------------------------------------------- reading

def _run_all_words(st: dict) -> str:
    if st.get("running"):
        cur = next((s for s in st.get("steps") or [] if s.get("key") == st.get("current")), None)
        return "正在跑" + (f"：{cur['label']}" if cur else "")
    if not st.get("steps"):
        return "没跑过"
    n = len(st.get("needs") or []) + int(st.get("needs_more") or 0)
    return {"done": "全部跑完了", "needs": f"能跑的都跑完了，{n} 件事需要你", "stopped": "已停止",
            "interrupted": "上一轮被中断了"}.get(str(st.get("state")), "跑完了，有的步骤没成功")


def _pipeline_state(plan_id: str) -> dict:
    from ..replication import pipeline
    try:
        return pipeline.state(plan_id)
    except Exception:  # noqa: BLE001 - a broken state file reads as never run
        return {"steps": [], "needs": [], "needs_more": 0, "running": False}


def list_replications(conn, a, task_id=None):
    from ..replication import coverage, plans
    q = " ".join(str(a.get("query") or "").split()).casefold()
    rows = [p for p in plans.list_all(conn) if not q or all(w in _haystack(conn, p) for w in q.split())]
    if not rows:
        return "no replication plans" + (f" match {q!r}" if q else " yet")
    out = []
    for p in rows[:MAX_PLANS]:
        cov = coverage.overview(conn, p["id"])
        st = _pipeline_state(p["id"])
        out.append(f"{p['id']} · {_link(conn, p)} · {plans.STATUS_LABELS.get(p['status'], p['status'])} · "
                   f"一键跑完：{_run_all_words(st)} · 复现清单 勾选 {cov['ticked']}/{cov['total']}")
    if len(rows) > MAX_PLANS:
        out.append(f"另有 {len(rows) - MAX_PLANS} 个方案未列出")
    out.append("(ids are for the tools; show the user titles and links only)")
    return "\n".join(out)


def _item_key(r: dict) -> str:
    from ..replication import coverage
    found = coverage.mentions(str(r.get("label") or ""))
    return found[0][2][0] if found and found[0][2] else str(r.get("label") or "").casefold()


def _coverage_lines(rows: list[dict]) -> list[str]:
    from ..replication import coverage
    out = []
    shown = 0
    for g in coverage.GROUPS:
        mine = [r for r in rows if r["group"] == g]
        if not mine:
            continue
        bits = []
        for r in mine:
            if shown >= MAX_LIST:
                break
            shown += 1
            bit = ("✓ " if r["ticked"] else "✗ ") + str(r["label"])
            if r.get("owner"):
                bit += f" 汇报人 {r['owner']}"
            if not r.get("scripts"):
                bit += " 没对上脚本"
            bits.append(bit)
        out.append(f"- {coverage.GROUP_LABELS[g]} {sum(r['ticked'] for r in mine)}/{len(mine)}：" + "；".join(bits))
    if len(rows) > shown:
        out.append(f"- 另有 {len(rows) - shown} 项未列出")
    return out


def _comparison_words(run: dict) -> str:
    cmp_ = (run.get("workspace") or {}).get("comparison") or {}
    counts = (cmp_.get("summary") or {}).get("counts") or {}
    if not counts:
        return ""
    from ..replication.compare import LABELS
    return "对照：" + "、".join(f"{LABELS.get(k, k)} {n} 项" for k, n in counts.items() if n)


def get_replication(conn, a, task_id=None):
    from ..replication import coverage, datasets, deck, plans, rrun, runs
    from ..replication import workspace as W
    p = _plan(conn, task_id, a.get("plan"))
    pid = p["id"]
    plan = p["plan"]
    lines = [f"## {_link(conn, p)} · {pid}",
             f"论文：{_clip(p.get('paper_title') or '未关联论文', 100)} · 状态：{plans.STATUS_LABELS.get(p['status'], p['status'])}"]
    st = _pipeline_state(pid)
    lines.append(f"一键跑完：{_run_all_words(st)}" + (f" · 开始于 {str(st.get('started'))[:16]}" if st.get("started") else ""))
    for s in st.get("steps") or []:
        lines.append(f"- {s.get('label')} {STEP_WORDS.get(s.get('state'), s.get('state'))}"
                     + (f"：{_clip(s.get('detail'), 100)}" if s.get("detail") else ""))
    needs = list(st.get("needs") or [])
    asks: list[str] = []
    for n in needs[:8]:
        if n.get("kind") == "dependency":
            asks.append(f"- 缺依赖 {n.get('title')}：{_clip(n.get('why'), 100)}"
                        + (f"；在终端运行 `{n['command']}`，装好后再跑一次" if n.get("command") else ""))
        else:
            asks.append(f"- 等数据 {n.get('title')}：{_clip(n.get('why'), 80)}" + (f"；{_clip(n.get('where'), 60)}" if n.get("where") else "")
                        + "；在复现页「需要你」里上传")
        if n.get("affects"):
            asks[-1] += "；影响 " + "、".join(n["affects"][:6])
    more = len(needs) - 8 + int(st.get("needs_more") or 0)
    if more > 0:
        asks.append(f"- 另有 {more} 件未列出")
    if not plans.targets_reviewed(p):
        asks.append("- 论文报告值还没审阅：对照和 Python 翻译路线要等用户在方案详情页核对后标为已审阅")
    if not plan.get("datasets"):
        asks.append("- 还没有挂数据集：Python 翻译路线要先挂上")
    lines.append("需要用户：" + ("\n" + "\n".join(asks) if asks else "没有"))
    rows = coverage.items(conn, pid)
    if rows:
        lines.append("复现清单，✓ 要复现，展示稿和数据文件夹按它来：")
        lines += _coverage_lines(rows)
    else:
        lines.append("复现清单：还没有，扫描表图或一键跑完会列出来")
    lines.append(f"论文报告值：{len(plans.targets(conn, pid))} 条，{'已审阅' if plans.targets_reviewed(p) else '未审阅'}")
    links = plan.get("links") or []
    lines.append("数据链接：" + ("；".join(f"{l.get('url')}" + (f" {_clip(l.get('note'), 40)}" if l.get("note") else "")
                                        for l in links[:6]) if links else "没有"))
    sources = plan.get("data_sources") or []
    if sources:
        lines.append("数据来源：" + "；".join(f"{s.get('name')}{SEP + s['url_or_where'] if s.get('url_or_where') else ''}"
                                          for s in sources[:6]))
    sets = [d for d in (datasets.get(conn, i) for i in plan.get("datasets") or []) if d]
    lines.append("数据集：" + ("；".join(f"{d['id']} {d['name']}" for d in sets) if sets else "没有挂"))
    wruns = W.runs(conn, pid)[:5]
    if wruns:
        lines.append("运行记录，新的在前：")
        for r in wruns:
            w = r.get("workspace") or {}
            bits = [r["id"], str(r.get("created_at") or "")[:16].replace("T", " "), MODE_WORDS.get(w.get("mode"), w.get("mode") or ""),
                    RUN_STATE_WORDS.get(w.get("state"), w.get("state") or r.get("status") or "")]
            if w.get("tables"):
                bits.append(f"{len(w['tables'])} 张表")
            if _comparison_words(r):
                bits.append(_comparison_words(r))
            lines.append("- " + " · ".join(b for b in bits if b))
    other = [r for r in runs.list_all(conn, plan_id=pid, limit=20) if r["method"] != "workspace"][:3]
    for r in other:
        lines.append(f"- {r['id']} · {r.get('method_label')} · {r.get('status')}")
    try:
        live = rrun.live(pid)
    except Exception:  # noqa: BLE001 - the R card is a convenience
        live = {"running": False, "state": {}}
    rs = live.get("state") or {}
    if live.get("running"):
        lines.append("R：正在" + ("装包" if rs.get("kind") == "install" else "运行作者代码"))
    elif rs.get("state"):
        lines.append(f"R：上次{'装包' if rs.get('kind') == 'install' else '运行'} {RUN_STATE_WORDS.get(rs['state'], rs['state'])}"
                     + (f"：{_clip(rs.get('note'), 80)}" if rs.get("note") and rs.get("state") != "done" else ""))
    if W.current_job(conn, pid) is not None:
        lines.append("Python 翻译路线：正在跑")
    d = deck.info(pid)
    lines.append(f"展示稿：{'已生成 ' + d['when'] if d['exists'] else '还没有'}")
    lines.append("路线：一键跑完走作者的 R 代码；Python 翻译路线要单独开始，会花 token。")
    lines.append("(ids are for the tools; show the user titles and links only)")
    return "\n".join(lines)


# ---------------------------------------------------------------- consent

def _words(conn, task_id: str | None) -> str:
    return "\n".join(_tools()._requests(conn, task_id)) if task_id else ""


def _blocked(conn, task_id: str | None, lines: list[str]) -> str | None:
    """Why this request may not write at all (先问再改, card 0103): a question never does, nor a bare yes to a question
    that did not spell out these very lines. A yes to exactly these lines is the order."""
    from . import skills
    T = _tools()
    if not task_id:
        return None
    if lines and T._agreed(conn, task_id, tuple(lines)):
        return None
    said = T._requests(conn, task_id)
    if said and skills.asks(URL_RE.sub(" ", said[0])):         # the ? of a query string asks nothing
        return "the user's message is a question"
    if T._yes_to_question(conn, task_id):
        return "the user agreed only to the changes their question spelled out"
    return None


def _ask_first(why: str, lines: list[str]) -> str:
    return "\n".join(
        [f"not done ({why}). Nothing was changed and no card was made. Answer what the user said. If they may want "
         "this, call ask_user with one short question followed by these lines exactly as written, one per line, then "
         "stop:"] + lines + ["If they answer with a short yes, call this tool again with the same arguments."])


def _batch(conn, task_id: str | None, *, card: bool) -> str | None:
    """This request's open card of this kind, or its batch of direct changes: one card and one 撤销 per reply."""
    from ..pm import changes
    if not task_id:
        return None
    for b in reversed(changes.batches(conn, task_id=task_id)):
        if any(r["kind"] != KIND for r in b["rows"]):
            continue
        states = {r["status"] for r in b["rows"]}
        if card and "proposed" in states:
            return b["batch_id"]
        if not card and "proposed" not in states and "applied" in states:
            return b["batch_id"]
    return None


def _write(conn, task_id: str | None, items: list[dict], *, direct: bool, reason: str = "") -> str:
    from ..pm import changes
    if direct:
        out = changes.apply_now(conn, items, task_id=task_id, reason=reason, kind=KIND,
                                batch_id=_batch(conn, task_id, card=False))
        lines = [changes.summary(out)]
        for r in out["applied"]:
            if r["result"] != changes.ONCE:
                lines.append(f"- {r['label']}：{r['field']} {r['old'] or '无'} → {r['new']}")
            elif r["note"]:
                lines.append(f"- {r['label']}：{r['note']}")
        undoable = any(r["result"] != changes.ONCE for r in out["applied"])
        return "\n".join(lines + ([DONE_NOTE] if undoable else []))
    batch = _batch(conn, task_id, card=True)
    pending = {(r["target"], r["field"], str(r["new"])) for r in changes.rows(conn, batch)} if batch else set()
    fresh = [i for i in items if (i["target"], i["field"], str(i["new"])) not in pending]
    if fresh:
        changes.propose(conn, fresh, task_id=task_id, reason=reason, kind=KIND, batch_id=batch)
    shown = "\n".join(f"- {i['label']}：{i['field']} {i['old'] or '无'} → {i['new']}" for i in items)
    return "\n".join(["card waiting for the user's click:", shown] + ([f"note on the card: {reason}"] if reason else [])
                     + [CARD_NOTE])


# ---------------------------------------------------------------- 复现清单

def _list(v: Any) -> list[str]:
    if v is None or v == "":
        return []
    vals = v if isinstance(v, list) else re.split(r"[,，、;；]", str(v))
    return [" ".join(str(x).split()) for x in vals if str(x).strip()]


def _keys_in(text: str) -> set[str]:
    from ..replication import coverage
    out: set[str] = set()
    for t in (text, text.upper()):        # 「附表 a7」 as well as 「附表 A7」
        for _, _, keys in coverage.mentions(t[:4000]):
            out.update(keys)
    return out


def _matches(r: dict, keys: set[str]) -> bool:
    from ..replication import coverage
    k = _item_key(r)
    return k in keys or (coverage.parent_key(k) or "") in keys


def _resolve_items(rows: list[dict], refs: list[str]) -> tuple[set[int], set[int], list[str]]:
    """(ids named by a label, ids named by a group, refs that match nothing)."""
    by_label: set[int] = set()
    by_group: set[int] = set()
    missing = []
    for ref in refs:
        groups = GROUP_REFS.get(ref.casefold()) or GROUP_REFS.get(ref)
        if groups:
            by_group |= {r["id"] for r in rows if r["group"] in groups}
            continue
        keys = _keys_in(ref)
        hit = {r["id"] for r in rows if (keys and _matches(r, keys)) or str(r["label"]).casefold() == ref.casefold()}
        if hit:
            by_label |= hit
        else:
            missing.append(ref)
    return by_label, by_group, missing


def _named_items(rows: list[dict], by_label: set[int], by_group: set[int], said: str) -> bool:
    """Every item the call names by label is one the user's words name; a whole group only when they name the group."""
    keys = _keys_in(said)
    return all(_matches(r, keys) for r in rows if r["id"] in by_label) \
        and all(GROUP_SAID[r["group"]].search(said) for r in rows if r["id"] in by_group - by_label)


def _labels(rows: list[dict], n: int = 8) -> str:
    names = [str(r["label"]) for r in rows]
    return "、".join(names[:n]) + (f" 等 {len(names)} 项" if len(names) > n else "")


def set_replication_targets(conn, a, task_id=None):
    """Which tables and figures the replication covers (复现清单 ticks), and who reports them (汇报人)."""
    from ..pm import changes
    from ..replication import coverage
    T = _tools()
    p = _plan(conn, task_id, a.get("plan"))
    pid, name = p["id"], _name(conn, p)
    rows = coverage.items(conn, pid)
    if not rows:
        return ("this plan has no 复现清单 yet: run_replication with action scan (a card) or Run all lists the paper's "
                "tables and figures first; nothing changed")
    tick, untick, only = _list(a.get("tick")), _list(a.get("untick")), bool(a.get("only"))
    owner = None if a.get("owner") is None else coverage.owner_of(a.get("owner"))
    if not tick and not untick and owner is None:
        raise T.ToolError("give tick and/or untick (labels as get_replication lists them, or 正文结果 / 附录稳健性 / "
                          "附录其他 / 全部), or owner with tick")
    on_label, on_group, miss_on = _resolve_items(rows, tick)
    off_label, off_group, miss_off = _resolve_items(rows, untick)
    on, off = on_label | on_group, off_label | off_group
    if miss_on or miss_off:
        raise T.ToolError("not in this plan's 复现清单: " + "、".join(miss_on + miss_off) + "; it lists: "
                          + _labels(rows, 40) + ". Ask the user if they meant another table or figure.")
    if on & off:
        raise T.ToolError("the same item is both in tick and untick")
    want = {r["id"]: bool(r["ticked"]) for r in rows}
    for i in on:
        want[i] = True
    for i in off:
        want[i] = False
    if only:
        for i in want:
            if i not in on:
                want[i] = False
    items, now_on, now_off, owned = [], [], [], []
    for r in rows:
        label = f"{r['label']}{SEP}{name}"
        if want[r["id"]] != bool(r["ticked"]):
            items.append(changes.change_item(f"{pid}#{r['id']}", F_TICK, YES if r["ticked"] else NO,
                                             YES if want[r["id"]] else NO, label=label))
            (now_on if want[r["id"]] else now_off).append(r)
        if owner is not None and r["id"] in on and (r.get("owner") or "") != owner:
            items.append(changes.change_item(f"{pid}#{r['id']}", F_OWNER, r.get("owner") or "", owner, label=label))
            owned.append(r)
    if not items:
        return f"「{name}」的复现清单本来就是这样，没有改。"
    lines = [f"「{name}」复现清单：{YES} {_labels(now_on)}" if now_on else "",
             f"「{name}」复现清单：{NO} {_labels(now_off)}" if now_off else "",
             f"「{name}」汇报人 {owner}：{_labels(owned)}" if owned else ""]
    lines = [x for x in lines if x]
    why = _blocked(conn, task_id, lines)
    if why:
        return _ask_first(why, lines)
    said = _words(conn, task_id)
    direct = bool(said) and _named_items(rows, on_label | off_label, on_group | off_group, said) \
        and (not only or bool(ONLY.search(said))) \
        and (owner is None or owner in said)
    return _write(conn, task_id, items, direct=direct, reason=_clip(a.get("reason"), 200))


# ---------------------------------------------------------------- data: links, sources, datasets

def add_replication_data(conn, a, task_id=None):
    """A data link (URL) or a data source the user gives, or an imported dataset attached to the plan."""
    from ..pm import changes
    from ..replication import plans
    T = _tools()
    p = _plan(conn, task_id, a.get("plan"))
    pid, name, plan = p["id"], _name(conn, p), p["plan"]
    said = _words(conn, task_id)
    flat = re.sub(r"\s+", "", said)
    items, lines, named = [], [], []
    url = str(a.get("url") or "").strip()
    if url:
        try:
            plans.Link.model_validate({"url": url, "kind": plans.link_kind(url), "access": "public"})
        except Exception:  # noqa: BLE001 - pydantic says why in English; the user gets the plain reason
            raise T.ToolError(f"{url[:80]} is not a public http(s) link; nothing changed") from None
        if any(l.get("url") == url for l in plan.get("links") or []):
            lines.append("")
            named.append(True)
        else:
            note = _clip(a.get("note"), 80).replace(SEP, " ")
            new = url + (SEP + note if note else "")
            items.append(changes.change_item(pid, F_LINK, "", new, label=name))
            lines.append(f"「{name}」{F_LINK}：加上 {url}")
            named.append(url in said or re.sub(r"\s+", "", url) in flat)
    src = " ".join(str(a.get("source_name") or "").split()).replace(SEP, " ")[:120]
    if src:
        where = " ".join(str(a.get("source_where") or "").split())[:300]
        if any(s.get("name") == src and (s.get("url_or_where") or "") == where for s in plan.get("data_sources") or []):
            lines.append("")
            named.append(True)
        else:
            items.append(changes.change_item(pid, F_SOURCE, "", src + (SEP + where if where else ""), label=name))
            lines.append(f"「{name}」{F_SOURCE}：加上 {src}")
            named.append(src.casefold() in said.casefold() and (not where or where in said))
    ds_ref = str(a.get("dataset") or "").strip()
    if ds_ref:
        ds = T._dataset(conn, ds_ref)
        if ds["id"] in (plan.get("datasets") or []):
            lines.append("")
            named.append(True)
        else:
            items.append(changes.change_item(f"{pid}#{ds['id']}", F_DATASET, DETACHED, ATTACHED,
                                             label=f"{ds['name']}{SEP}{name}"))
            lines.append(f"「{name}」{F_DATASET}：{ATTACHED} {ds['name']}")
            named.append(str(ds["name"]).casefold() in said.casefold() or ds["id"] in said)
    if not (url or src or ds_ref):
        raise T.ToolError("give url (with an optional note), source_name (with source_where), or dataset")
    if not items:
        return f"「{name}」已经有这些了，没有改。"
    lines = [x for x in lines if x]
    why = _blocked(conn, task_id, lines)
    if why:
        return _ask_first(why, lines)
    return _write(conn, task_id, items, direct=bool(said) and all(named), reason=_clip(a.get("reason"), 200))


# ---------------------------------------------------------------- runs, steps, the deck

def _run_of(conn, pid: str, ref: str, *, valid: bool = False) -> dict | None:
    """The run named (its id), else the latest workspace run of the plan (the latest that can be compared)."""
    from ..replication import runs
    from ..replication import workspace as W
    if ref:
        r = W.get_run(conn, ref) or runs.get(conn, ref)
        return r if r is not None and r.get("plan_id") == pid else None
    if valid:
        return W.latest_valid(conn, pid)
    every = W.runs(conn, pid)
    if every:
        return every[0]
    other = [r for r in runs.list_all(conn, plan_id=pid, limit=5) if r["method"] != "workspace"]
    return other[0] if other else None


def _run_words(run: dict) -> str:
    w = run.get("workspace") or {}
    when = str(run.get("created_at") or "")[5:16].replace("T", " ")
    what = MODE_WORDS.get(w.get("mode"), "") if run.get("method") == "workspace" else str(run.get("method_label") or "分析")
    return f"{when} 的{what}运行".strip()


def _precheck(conn, pid: str, op: str, run: dict | None) -> str:
    """Why the action cannot start now, in Chinese, with nothing proposed; '' when it can. The same refusals the
    buttons give (pipeline._check, workspace.check_start, rrun.spawn), asked before a card is made."""
    from ..replication import pipeline, rrun
    from ..replication import workspace as W
    if op == "run_all":
        if pipeline.running(pid):
            return "一键跑完已经在跑了，等这一轮结束。"
        if W.current_job(conn, pid) is not None:
            return "这份方案的 Python 翻译路线正在跑，等它结束或先停掉，再一键跑完。"
    if op in ("r_run", "r_install") or (op == "rerun" and run and (run.get("workspace") or {}).get("mode") == "rrun"):
        if rrun.busy(pid):
            return "这份方案正在运行 R，等它结束或先停掉。"
        if W.current_job(conn, pid) is not None:
            return "这份方案的 Python 翻译路线正在跑，等它结束或先停掉。"
        if not rrun.find_rscript():
            return f"这台电脑没有找到 R：在终端运行 `{pipeline.R_INSTALL}` 装好后再来。"
    elif op == "python" or (op == "rerun" and run and run.get("method") == "workspace"):
        try:
            W.check_start(conn, pid, "ai" if op == "python" else "rerun", run["id"] if op == "rerun" else None)
        except W.WorkspaceError as e:
            return str(e)
    if op in ("rerun", "compare") and run is None:
        return "这份方案还没有可以对照的运行。" if op == "compare" else "这份方案还没有可以重跑的运行。"
    if op == "compare" and run is not None:
        if run.get("method") != "workspace":
            return "只有复现工作区里的运行能和论文报告值对照。"
        why = W.not_comparable(run)
        if why:
            return why
        if not W.targets_reviewed(conn, pid):
            return "论文报告值还没审阅，不能对照：先在方案详情页核对后标为已审阅。"
    return ""


def run_replication(conn, a, task_id=None):
    """Start Run all, a route, a rerun, one step again or the deck: always on a card the user clicks."""
    from ..pm import changes
    from ..replication import deck
    from ..replication import workspace as W
    T = _tools()
    op = str(a.get("action") or "")
    if op not in OPS:
        raise T.ToolError(f"action must be one of {', '.join(OPS)}")
    p = _plan(conn, task_id, a.get("plan"))
    pid, name = p["id"], _name(conn, p)
    run = None
    if op in ("rerun", "compare"):
        run = _run_of(conn, pid, str(a.get("run") or "").strip(), valid=op == "compare")
        if run is None and a.get("run"):
            raise T.ToolError(f"no run {a.get('run')!r} in this plan; get_replication lists its runs")
    why = _precheck(conn, pid, op, run)
    if why:
        return f"not proposed: {why} Tell the user this; nothing changed and no card was made."
    label, cost = OPS[op]
    if op == "python":
        low, high = W.estimate(conn, pid, W.author_code(conn, pid))
        cost = cost.format(low=round(low / 10000, 1), high=round(high / 10000, 1))
    target, what = pid, name
    if run is not None:
        target, what = f"{pid}#{run['id']}", f"{name}{SEP}{_run_words(run)}"
    if op == "run_all":
        old = _run_all_words(_pipeline_state(pid))
    elif op == "deck":
        d = deck.info(pid)
        old = f"{d['when']} 生成的" if d["exists"] else "还没有"
    else:
        old = ""
    new = "重新生成" if op == "deck" else "开始"
    lines = [f"「{what}」{label}：{new}"]
    blocked = _blocked(conn, task_id, lines)
    if blocked:
        return _ask_first(blocked, lines)
    item = changes.change_item(target, label, old, new, label=what)
    return _write(conn, task_id, [item], direct=False, reason=cost)


def stop_replication(conn, a, task_id=None):
    """The user's Stop, at once: the running R step and/or the Python translation session."""
    from ..pm import changes
    from ..replication import pipeline, rrun
    from ..replication import workspace as W
    T = _tools()
    what = str(a.get("what") or "all")
    if what not in ("r", "python", "all"):
        raise T.ToolError("what must be r, python or all")
    p = _plan(conn, task_id, a.get("plan"))
    pid, name = p["id"], _name(conn, p)
    r_busy = what in ("r", "all") and rrun.busy(pid)
    py_busy = what in ("python", "all") and W.current_job(conn, pid) is not None
    if not (r_busy or py_busy):
        if pipeline.running(pid):
            return ("nothing to stop: Run all is between its R steps, and only its R steps can be stopped. Tell the "
                    "user; they can stop it once it reaches 准备环境 or 运行作者代码.")
        return "nothing is running for this plan; nothing changed."
    new = "全部" if r_busy and py_busy else ("R" if r_busy else "Python")
    lines = [f"「{name}」{F_STOP}：{new}"]
    why = _blocked(conn, task_id, lines)
    if why:
        return _ask_first(why, lines)
    item = changes.change_item(pid, F_STOP, "运行中", new, label=name)
    return _write(conn, task_id, [item], direct=True)


# ---------------------------------------------------------------- pm.changes handlers for kind 'replication'

def _cov_row(conn, pid: str, sub: str) -> dict | None:
    from ..replication import coverage
    return next((r for r in coverage.items(conn, pid) if str(r["id"]) == sub), None)


def _split(new: Any) -> tuple[str, str]:
    head, _, tail = str(new or "").partition(SEP)
    return head, tail


def _do_op(conn, op: str, pid: str, sub: str) -> str:
    """One button's function; returns what the card says under the row."""
    from ..replication import coverage, deck, export, pipeline, rrun, runs
    from ..replication import workspace as W
    if op == "run_all":
        return "已开始一键跑完，进度在复现页。" if pipeline.start(conn, pid) else "已经在跑了，等这一轮结束。"
    if op in ("r_run", "r_install"):
        rrun.spawn(conn, pid, "run" if op == "r_run" else "install")
        return "开始运行作者的 R 代码，没有报错就不会调用 AI。" if op == "r_run" else "开始装 R 包了，要联网，可能要几十分钟。"
    if op == "python":
        W.start(conn, pid, mode="ai")
        return "已开始 Python 翻译路线，进度在复现工作区，可以随时停下。"
    if op == "rerun":
        run = W.get_run(conn, sub)
        if run is not None and run["plan_id"] == pid:
            if (run.get("workspace") or {}).get("mode") == "rrun":
                rrun.spawn(conn, pid, "run")
                return "开始重跑作者的 R 代码，上次的修补会沿用。"
            W.start(conn, pid, mode="rerun", source_run=sub)
            return "正在用这次运行存下的代码重跑，不调用 AI。"
        stat = runs.get(conn, sub)
        if stat is None or stat.get("plan_id") != pid:
            raise runs.RunError("没有这次运行。")
        return "已重跑，结果在分析页。" if runs.rerun(conn, sub) == "done" else "重跑出错了，原因在分析页。"
    if op == "scan":
        done = coverage.rescan(conn, pid)
        return f"清单已更新：共 {done['total']} 项，对上脚本 {done['mapped']} 项" + (f"；另有 {done['omitted']} 项未列出" if done["omitted"] else "")
    if op == "compare":
        out = W.compare_run(conn, pid, sub)
        counts = (out.get("summary") or {}).get("counts") or {}
        from ..replication.compare import LABELS
        return "已对照：" + ("、".join(f"{LABELS.get(k, k)} {n} 项" for k, n in counts.items() if n) or "没有可对照的数")
    if op == "deck":
        done = deck.make(conn, pid)
        return f"展示稿已生成，共 {done.slides} 页" + ("。" + "；".join(done.notes[:2]) if done.notes else "")
    if op == "export":
        return export.export_folder(conn, pid).message
    raise ValueError(op)


def _stop(conn, pid: str, what: str) -> str:
    from ..replication import rrun
    from ..replication import workspace as W
    done = []
    if what in ("R", "全部") and rrun.stop(pid):
        done.append("R 这一步")
    if what in ("Python", "全部") and W.stop(conn, pid):
        done.append("Python 翻译路线")
    return ("已停止 " + "和".join(done) + "，已经写出的结果留着。") if done else "已经停了。"


def _errors() -> tuple[type[BaseException], ...]:
    from ..replication import coverage, deck, export, pipeline, plans, runs
    from ..replication import workspace as W
    return (pipeline.PipelineError, W.WorkspaceError, coverage.CoverageError, deck.DeckError, export.ExportError,
            runs.RunError, plans.PlanError, OSError)


def _apply_one(conn, r: dict) -> dict:
    from ..pm import changes
    from ..replication import coverage, plans
    pid, _, sub = str(r["target"]).partition("#")
    f, rid = r["field"], r["id"]
    noop = {"id": rid, "status": "dismissed", "result": "noop", "note": "本来就是这样，不用改"}
    if f in (F_TICK, F_OWNER):
        row = _cov_row(conn, pid, sub)
        if row is None:
            return {"id": rid, "status": "dismissed", "result": "error", "note": "这一项已经不在复现清单里了"}
        cur = (YES if row["ticked"] else NO) if f == F_TICK else (row.get("owner") or "")
        if cur == r["new"]:
            return noop
        if cur != (r["old"] or ""):
            return {"id": rid, "status": "dismissed", "result": "conflict", "detail": {"current": cur},
                    "note": f"这一项你刚改成了 {cur or '无'}，我没动"}
        _mark(conn, pid, row, r["new"] if f == F_TICK else None, r["new"] if f == F_OWNER else None)
        return {"id": rid, "status": "applied", "detail": {"old": r["old"], "new": r["new"]}}
    if f in (F_LINK, F_SOURCE):
        p = plans.get(conn, pid)
        if p is None:
            return {"id": rid, "status": "dismissed", "result": "error", "note": "这个复现方案已经不在了"}
        head, tail = _split(r["new"])
        if f == F_LINK:
            have = [dict(x) for x in p["plan"].get("links") or []]
            if any(x.get("url") == head for x in have):
                return noop
            have.append({"url": head, "kind": plans.link_kind(head), "access": "public", "note": tail})
            plans.save_links(conn, pid, have, version=plans.section_version(p, "links"))
        else:
            have = [dict(x) for x in p["plan"].get("data_sources") or []]
            if any(x.get("name") == head and (x.get("url_or_where") or "") == tail for x in have):
                return noop
            have.append({"name": head, "access": "", "url_or_where": tail})
            plans.save_sources(conn, pid, have, version=plans.section_version(p, "sources"))
        return {"id": rid, "status": "applied", "detail": {"new": r["new"]}}
    if f == F_DATASET:
        p = plans.get(conn, pid)
        if p is not None and sub in (p["plan"].get("datasets") or []):
            return noop
        plans.attach_dataset(conn, pid, sub)
        return {"id": rid, "status": "applied", "detail": {"new": sub}}
    if f == F_STOP:
        return {"id": rid, "status": "applied", "result": changes.ONCE, "note": _stop(conn, pid, str(r["new"]))}
    if f in OP_BY_LABEL:
        return {"id": rid, "status": "applied", "result": changes.ONCE, "note": _clip(_do_op(conn, OP_BY_LABEL[f], pid, sub), 200)}
    return {"id": rid, "status": "dismissed", "result": "error", "note": "不认识的改动"}


def _mark(conn, pid: str, row: dict, tick: str | None, owner: str | None) -> None:
    from ..replication import coverage
    ticked = (tick == YES) if tick is not None else bool(row["ticked"])
    coverage.save_marks(conn, pid, [row["id"]], [row["id"]] if ticked else [],
                        {row["id"]: owner if owner is not None else (row.get("owner") or "")})


def _apply_rows(conn, rows: list[dict], src: dict) -> list[dict]:
    out = []
    for r in rows:
        try:
            out.append(_apply_one(conn, r))
        except _errors() as e:
            out.append({"id": r["id"], "status": "dismissed", "result": "error", "note": _clip(e, 200) or "没能执行"})
        except Exception:  # noqa: BLE001 - one broken row must not lose the others; the card says so
            log.exception("replication change %s failed", r["id"])
            out.append({"id": r["id"], "status": "dismissed", "result": "error", "note": "没能执行，稍后再试"})
    return out


def _undo_one(conn, r: dict) -> dict:
    from ..replication import plans
    pid, _, sub = str(r["target"]).partition("#")
    f, rid = r["field"], r["id"]
    written = (r["detail"] or {}).get("new", r["new"])
    if f in (F_TICK, F_OWNER):
        row = _cov_row(conn, pid, sub)
        if row is None:
            return {"id": rid, "status": "undone", "result": "noop"}
        cur = (YES if row["ticked"] else NO) if f == F_TICK else (row.get("owner") or "")
        old = (r["detail"] or {}).get("old", r["old"]) or ""
        if cur == old:
            return {"id": rid, "status": "undone", "result": "noop"}
        if cur != written:
            return {"id": rid, "status": "applied", "result": "kept", "detail": {**(r["detail"] or {}), "current": cur},
                    "note": f"这一项你后来改成了 {cur or '无'}，我没动"}
        _mark(conn, pid, row, old if f == F_TICK else None, old if f == F_OWNER else None)
        return {"id": rid, "status": "undone"}
    p = plans.get(conn, pid)
    if p is None:
        return {"id": rid, "status": "undone", "result": "noop"}
    if f == F_LINK:
        url = _split(written)[0]
        have = [dict(x) for x in p["plan"].get("links") or []]
        if not any(x.get("url") == url for x in have):
            return {"id": rid, "status": "undone", "result": "noop"}
        plans.save_links(conn, pid, [x for x in have if x.get("url") != url], version=plans.section_version(p, "links"))
        return {"id": rid, "status": "undone"}
    if f == F_SOURCE:
        head, tail = _split(written)
        have = [dict(x) for x in p["plan"].get("data_sources") or []]
        keep = [x for x in have if not (x.get("name") == head and (x.get("url_or_where") or "") == tail)]
        if len(keep) == len(have):
            return {"id": rid, "status": "undone", "result": "noop"}
        plans.save_sources(conn, pid, keep, version=plans.section_version(p, "sources"))
        return {"id": rid, "status": "undone"}
    if f == F_DATASET:
        if sub not in (p["plan"].get("datasets") or []):
            return {"id": rid, "status": "undone", "result": "noop"}
        plans.detach_dataset(conn, pid, sub)
        return {"id": rid, "status": "undone"}
    return {"id": rid, "status": "applied", "result": "error", "note": "这一处撤销不了"}


def _undo_rows(conn, rows: list[dict], src: dict) -> list[dict]:
    out = []
    for r in rows:
        try:
            out.append(_undo_one(conn, r))
        except _errors() as e:
            out.append({"id": r["id"], "status": "applied", "result": "error", "note": _clip(e, 200) or "没撤销成"})
    return out


def _register_kind() -> None:
    from ..pm import changes
    changes.register(KIND, _apply_rows, _undo_rows)


_register_kind()


# ---------------------------------------------------------------- the tool list

def _label(verb: str):
    return lambda conn, a: verb


def register(Tool, obj):
    plan = {"type": "string", "description": "the plan: its id from list_replications, words of the paper's title or "
                                             "author, or 这个 for the replication page the user is on"}
    labels = {"type": "array", "items": {"type": "string"}}
    return [
        Tool("list_replications", "The user's replication plans (复现), one line each: id, paper, status, Run all's state "
             "and how many tables and figures are ticked. query filters by title, author or year.",
             obj({"query": {"type": "string"}}), list_replications, _label("查看复现方案"), True),
        Tool("get_replication", "One replication in full: Run all's steps and what needs the user (a data file to upload, "
             "a dependency to install), the 复现清单 of tables and figures (ticked = replicated), the paper's reported "
             "values, data links, sources and datasets, the last runs with their comparison, the R step and the deck.",
             obj({"plan": plan}), get_replication, _label("查看复现进度"), True),
        Tool("set_replication_targets", "Choose which tables and figures are replicated (the 复现清单; the deck and the "
             "data folder follow it). tick / untick: labels as get_replication lists them (Table 2, Figure A7, 表 3) or "
             "a group (正文结果, 附录稳健性, 附录其他, 全部). only=true unticks everything not in tick (「只复现表 2 和图 3」). "
             "owner sets 汇报人 of the ticked items. Applies at once and undoably when the user named the items; "
             "otherwise it waits on a card.",
             obj({"plan": plan, "tick": labels, "untick": labels, "only": {"type": "boolean"},
                  "owner": {"type": "string"}, "reason": {"type": "string"}}),
             set_replication_targets, _label("改复现清单"), True),
        Tool("add_replication_data", "Add data to a replication plan: a data link the user gives (url, public http(s), "
             "with an optional note), a data source (source_name and source_where), or an imported dataset (dataset: "
             "its id or name from list_datasets). Applies at once and undoably when the user typed it; otherwise it "
             "waits on a card. Nothing is downloaded here: Run all fetches from the links.",
             obj({"plan": plan, "url": {"type": "string"}, "note": {"type": "string"},
                  "source_name": {"type": "string"}, "source_where": {"type": "string"},
                  "dataset": {"type": "string"}, "reason": {"type": "string"}}),
             add_replication_data, _label("加复现数据"), True),
        Tool("run_replication", "Put one action on a confirmation card; it starts only when the user clicks. action: "
             "run_all (every step, the authors' R code), r_run (the authors' R code only), r_install (install the R "
             "packages, networked), python (the Python translation route: AI translates the authors' scripts, costs "
             "tokens), rerun (one run again: run is its id, default the latest), scan (list the paper's tables and "
             "figures again), compare (a run against the paper's reported values), deck (regenerate the 展示稿), "
             "export (the data folder). Refuses with the reason when the action cannot start now.",
             obj({"plan": plan, "action": {"type": "string", "enum": list(OPS)}, "run": {"type": "string"}},
                 ("action",)),
             run_replication, lambda conn, a: "准备" + OPS.get(str(a.get("action")), ("复现操作",))[0], True),
        Tool("stop_replication", "Stop, at once, the running R step (install or the authors' code; this also ends Run "
             "all) and/or the Python translation session. what: r, python or all (default). Only when the user asks.",
             obj({"plan": plan, "what": {"type": "string", "enum": ["r", "python", "all"]}}),
             stop_replication, _label("停止复现运行"), True),
    ]
