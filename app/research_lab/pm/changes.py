"""assistant_changes: what the assistant proposed or changed, and the one path its writes take.

docs/design/ISSUE-AGENT.md §4, §5. One row per field, created issue or comment; a batch is one
request's direct changes or one confirmation card. Row status: proposed → applied | dismissed, and
applied → undone. Which changes go straight through and which wait on a card is the caller's call
(the issue tools, card 0077); this module only records, applies and undoes, the same way for both.

    change_item(target, field, old, new, label=)      a field change; old = the value the assistant saw
    create_item(title, project=, parent=, **props)    a new issue or sub-issue (origin: assistant)
    comment_item(target, text, by=, via=)             a comment; recorded for the audit, never undone

    propose(conn, items, task_id=, thread_id=, message_id=, reason=, kind='issue') -> batch_id
    digest_batch_id(kind) · digest_kind(batch_id)              the Friday digest's batches (pm/scheduler.py)
    apply(conn, batch_id, ids=None, edits=None) -> outcome   the card's 执行; idempotent
    apply_now(conn, items, ...) -> outcome                     a direct change: propose + apply
    dismiss(conn, batch_id, ids=None) -> int                   the card's 不用了
    revise(conn, change_id, new)                               edit a proposed row (a title on the card)
    undo(conn, batch_id) -> outcome                            field-checked; leaves what Leo changed since
    attach(conn, batch_id, message_id)                         hang a batch under a chat message
    last_batch(conn, thread_id) -> batch_id | None             for 「撤销刚才的修改」
    rows(conn, batch_id) · batches(conn, thread_id=|task_id=)  for cards, undo buttons and the audit
    summary(outcome) -> str                                    Chinese, for the reply and the toast

An outcome is {batch_id, applied, skipped, same, failed, dismissed, undone, kept, already}, each a list
of row dicts (see _row) except `already`, true when there was nothing left to do (a second click).
Row `note` carries the Chinese reason a row was skipped or left alone (「这条你刚改成了 X，我没动」).

Kind 'issue' takes actor='assistant' only: the assistant's field whitelist always applies to issues.
actor='user' is for kind 'setting' (a change made on the settings page, card 0080). An applied set
row's detail holds the values as really stored {old, new, postponed?}; undo compares against those
(rows recorded before 0093 hold `carried` instead).

No plan week (docs/design/ISSUES-LINEAR.md §3): change_item and create_item turn a `week` from an older
caller into a due date on that week's Friday (files.issues.week_due), so cards, apply and undo only
ever see `due`. The assistant's tools move to due dates in card 3.

Applying and undoing never call a model and do not check the daily spend cap. Kind 'issue' is built
in; card 0080 adds 'setting' with register(kind, apply_fn, undo_fn), each fn(conn, rows, source) ->
[{id, status, result?, note?, detail?, target?}] for the rows it handled. A handler may call
finish_row(conn, result) as it goes, so a crash mid-batch never re-applies a row already written.
A row a handler carried out but cannot put back (a run it started, card 0212) is applied with result
ONCE: it counts as applied and is never undone.
"""
from __future__ import annotations

import json
import secrets
import threading
from typing import Any, Callable

from .. import i18n
from ..db.connection import now_iso
from ..files import issues
from ..files.yamlio import StaleFileError

STATUSES = ("proposed", "applied", "undone", "dismissed")
ONCE = "once"
ACTIONS = ("set", "create", "comment")
# One process serves the app: two clicks on 执行 queue here, and the second finds nothing proposed.
_LOCK = threading.RLock()
Handler = Callable[[Any, list[dict], dict], list[dict]]
_HANDLERS: dict[str, tuple[Handler, Handler]] = {}


def register(kind: str, apply_fn: Handler, undo_fn: Handler) -> None:
    _HANDLERS[kind] = (apply_fn, undo_fn)


# The Friday digest (pm/scheduler.py) has no request behind its batches, so their id carries the origin:
# DIGEST_BATCH + move | park | week + hex. chat/cards.py names that origin on the card and starts the rows
# of an opt-in batch (搁置, and this week's unfinished items on Friday) unticked.
DIGEST_BATCH = "AC-friday-"
DIGEST_KINDS = ("move", "park", "week")
DIGEST_OPT_IN = ("park", "week")


def digest_batch_id(kind: str) -> str:
    if kind not in DIGEST_KINDS:
        raise ValueError(f"unknown digest batch {kind!r}")
    return f"{DIGEST_BATCH}{kind}-{secrets.token_hex(6)}"


def digest_kind(batch_id: str | None) -> str:
    """move | park | week for a batch the Friday digest proposed, '' for any other."""
    rest = str(batch_id or "")
    if not rest.startswith(DIGEST_BATCH):
        return ""
    kind = rest[len(DIGEST_BATCH):].split("-", 1)[0]
    return kind if kind in DIGEST_KINDS else ""


# ---------------------------------------------------------------- items

def change_item(target: str, field: str, old: Any, new: Any, *, label: str = "") -> dict:
    if field == "week":
        row = issues.get(target)
        field, old, new = "due", (row or {}).get("due", ""), issues.week_due(new)
    return {"action": "set", "target": target, "field": field, "old": old, "new": new, "label": label}


def create_item(title: str, *, project: str = "", parent: str = "", **props: Any) -> dict:
    week = props.pop("week", "")
    if week and not props.get("due"):
        props["due"] = issues.week_due(week)
    return {"action": "create", "target": "", "field": "", "old": None, "label": title,
            "new": {"title": title, "project": project, "parent": parent, **props}}


def comment_item(target: str, text: str, *, by: str = "assistant", via: str = "", label: str = "") -> dict:
    """by='user' is Leo's own words typed in through the assistant: always marked 经助手录入."""
    return {"action": "comment", "target": target, "field": "", "old": None, "label": label,
            "new": {"text": text, "by": by, "via": "assistant" if by == "user" else via}}


# ---------------------------------------------------------------- records

def _dump(v: Any) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False)


def _load(v: str | None) -> Any:
    return None if v is None else json.loads(v)


def _row(r) -> dict:
    d = dict(r)
    d["old"], d["new"] = _load(d.pop("old_json")), _load(d.pop("new_json"))
    d["detail"] = _load(d.pop("detail_json")) or {}
    return d


def source(conn, *, task_id: str | None = None, thread_id: str | None = None) -> dict:
    """Where a change came from, for the activity line: {task_id, thread_id, title, url}."""
    if not thread_id and task_id:
        r = conn.execute("SELECT thread_id FROM agent_tasks WHERE id = ?", (task_id,)).fetchone()
        thread_id = r[0] if r else None
    r = conn.execute("SELECT title FROM chat_threads WHERE id = ?", (thread_id,)).fetchone() if thread_id else None
    return {"task_id": task_id, "thread_id": thread_id, "title": r[0] if r else "",
            "url": f"/chat/{thread_id}" if thread_id else ""}


def _labels(items: list[dict]) -> dict[str, str]:
    if all(i.get("label") or not i.get("target") for i in items):
        return {}
    return {r["id"]: r["title"] for r in issues.read() + issues.project_roots()}


def propose(conn, items: list[dict], *, task_id: str | None = None, thread_id: str | None = None,
            message_id: int | None = None, reason: str = "", kind: str = "issue", actor: str = "assistant",
            batch_id: str | None = None) -> str:
    """Record items as proposed rows of one batch and return its id. Nothing is written to the files."""
    if kind != "issue" and kind not in _HANDLERS:
        raise ValueError(f"unknown change kind {kind!r}")
    if kind == "issue" and actor != "assistant":
        raise ValueError("issue changes are the assistant's; actor='user' is for settings only")
    if not items:
        raise ValueError("a batch needs at least one change")
    for i in items:
        if i.get("action", "set") not in ACTIONS:
            raise ValueError(f"unknown action {i.get('action')!r}")
        if i.get("action", "set") == "set" and not (i.get("target") and i.get("field")):
            raise ValueError("a field change needs a target and a field")
        if i.get("action") == "comment" and (i.get("new") or {}).get("by") not in ("user", "assistant"):
            raise ValueError("a comment is by user or assistant")
    if not thread_id and task_id:
        r = conn.execute("SELECT thread_id FROM agent_tasks WHERE id = ?", (task_id,)).fetchone()
        thread_id = r[0] if r else None
    batch_id = batch_id or "AC-" + secrets.token_hex(6)
    titles = _labels(items) if kind == "issue" else {}
    now = now_iso()
    for i in items:
        conn.execute(
            "INSERT INTO assistant_changes(batch_id, task_id, thread_id, message_id, kind, action, target, label, "
            "field, old_json, new_json, status, reason, actor, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'proposed', ?,?,?)",
            (batch_id, task_id, thread_id, message_id, kind, i.get("action", "set"), i.get("target") or "",
             str(i.get("label") or titles.get(i.get("target") or "", "")), i.get("field") or "",
             _dump(i.get("old")), _dump(i.get("new")), reason, actor, now))
    return batch_id


def rows(conn, batch_id: str) -> list[dict]:
    q = "SELECT * FROM assistant_changes WHERE batch_id = ? ORDER BY id"
    return [_row(r) for r in conn.execute(q, (batch_id,))]


def batch_status(batch_rows: list[dict]) -> str:
    """proposed while a row waits on the card; applied while one can be undone; else undone or dismissed."""
    states = {r["status"] for r in batch_rows}
    for s in ("proposed", "applied", "undone"):
        if s in states:
            return s
    return "dismissed"


def batches(conn, *, thread_id: str | None = None, task_id: str | None = None) -> list[dict]:
    """[{batch_id, status, undoable, rows}] oldest first, for one conversation or one task."""
    col, val = ("thread_id", thread_id) if thread_id else ("task_id", task_id)
    out: dict[str, list[dict]] = {}
    for r in conn.execute(f"SELECT * FROM assistant_changes WHERE {col} = ? ORDER BY id", (val,)):
        out.setdefault(r["batch_id"], []).append(_row(r))
    return [{"batch_id": b, "status": batch_status(rs), "undoable": _undoable(rs), "rows": rs}
            for b, rs in out.items()]


def _undoable(batch_rows: list[dict]) -> bool:
    return any(_undo_pending(r) for r in batch_rows)


def _undo_pending(r: dict) -> bool:
    """Applied, not a comment, not a one-off action (ONCE), and not one undo already left alone because Leo
    changed it (final)."""
    return r["status"] == "applied" and r["action"] != "comment" and r["result"] not in ("kept", ONCE)


def last_batch(conn, thread_id: str) -> str | None:
    """The newest batch in a conversation that still has something to undo."""
    for b in reversed(batches(conn, thread_id=thread_id)):
        if b["undoable"]:
            return b["batch_id"]
    return None


def attach(conn, batch_id: str, message_id: int) -> None:
    conn.execute("UPDATE assistant_changes SET message_id = ? WHERE batch_id = ?", (message_id, batch_id))


def revise(conn, change_id: int, new: Any) -> None:
    """Replace a proposed row's new value (a sub-issue title edited on the card). A create row takes
    a title string or a dict merged into its fields."""
    r = conn.execute("SELECT * FROM assistant_changes WHERE id = ?", (change_id,)).fetchone()
    if r is None or r["status"] != "proposed":
        raise ValueError("only a proposed change can be edited")
    value = _load(r["new_json"])
    if r["action"] == "create":
        value = {**value, **(new if isinstance(new, dict) else {"title": str(new)})}
        conn.execute("UPDATE assistant_changes SET new_json = ?, label = ? WHERE id = ?",
                     (_dump(value), str(value.get("title") or ""), change_id))
    else:
        conn.execute("UPDATE assistant_changes SET new_json = ? WHERE id = ?", (_dump(new), change_id))


def _finish(conn, rid: int, status: str, *, result: str | None = None, note: str | None = None,
            detail: dict | None = None, target: str | None = None) -> None:
    stamp = {"applied": "applied_at", "undone": "undone_at", "dismissed": "dismissed_at"}.get(status)
    sets = ["status = ?", "result = ?", "note = ?"]
    args: list[Any] = [status, result, note]
    if stamp:
        sets.append(f"{stamp} = ?")
        args.append(now_iso())
    if detail is not None:
        sets.append("detail_json = ?")
        args.append(_dump(detail))
    if target is not None:
        sets.append("target = ?")
        args.append(target)
    conn.execute(f"UPDATE assistant_changes SET {', '.join(sets)} WHERE id = ?", (*args, rid))


def finish_row(conn, res: dict) -> dict:
    """Record one row's result now ({id, status, result?, note?, detail?, target?}); returns it marked
    so apply/undo do not record it twice."""
    if not res.get("_recorded"):
        _finish(conn, res["id"], res["status"], result=res.get("result"), note=res.get("note"),
                detail=res.get("detail"), target=res.get("target"))
        res["_recorded"] = True
    return res


def _dismiss_aimed_at(conn, ref: str) -> None:
    """A deleted issue's proposed changes (on any card) and proposed sub-issues under it go too:
    cards never expire, and its id must not draw an old card onto something else."""
    for r in conn.execute("SELECT * FROM assistant_changes WHERE kind = 'issue' AND status = 'proposed'").fetchall():
        row = _row(r)
        if row["target"] == ref or (row["action"] == "create" and (row["new"] or {}).get("parent") == ref):
            _finish(conn, row["id"], "dismissed", result="gone", note="这条议题已经撤销删除")


def _empty(batch_id: str) -> dict:
    return {"batch_id": batch_id, "applied": [], "skipped": [], "same": [], "failed": [], "dismissed": [],
            "undone": [], "kept": [], "already": False}


def _collect(conn, batch_id: str, ids: list[int], out: dict) -> dict:
    by_id = {r["id"]: r for r in rows(conn, batch_id)}
    for rid in ids:
        r = by_id[rid]
        key = {"applied": "applied", "undone": "undone"}.get(r["status"])
        if r["result"] == "conflict":
            key = "skipped"
        elif r["result"] == "noop":
            key = "same"
        elif r["result"] == "error":
            key = "failed"
        elif r["result"] == "kept":
            key = "kept"
        out[key or "dismissed"].append(r)
    return out


# ---------------------------------------------------------------- apply · dismiss · undo

def apply(conn, batch_id: str, *, ids: list[int] | None = None, edits: dict[int, Any] | None = None) -> dict:
    """Write the batch's proposed rows (only `ids` when given; the rest are dismissed, they were unticked).
    Each row is field-checked again now: one Leo changed since the proposal is skipped and noted.
    A second call finds nothing proposed and returns already=True."""
    with _LOCK:
        out = _empty(batch_id)
        pending = [r for r in rows(conn, batch_id) if r["status"] == "proposed"]
        if not pending:
            out["already"] = True
            return out
        waiting = {r["id"] for r in pending}
        for rid, value in (edits or {}).items():
            if int(rid) in waiting:
                revise(conn, int(rid), value)
        pending = [r for r in rows(conn, batch_id) if r["status"] == "proposed"]
        unticked: list[int] = []
        if ids is not None:
            keep = {int(i) for i in ids}
            unticked = [r["id"] for r in pending if r["id"] not in keep]
            for rid in unticked:
                _finish(conn, rid, "dismissed")
            pending = [r for r in pending if r["id"] in keep]
        src = source(conn, task_id=pending[0]["task_id"] if pending else None,
                     thread_id=pending[0]["thread_id"] if pending else None)
        done = []
        for kind in dict.fromkeys(r["kind"] for r in pending):
            mine = [r for r in pending if r["kind"] == kind]
            fn = _apply_issue if kind == "issue" else _HANDLERS[kind][0]
            for res in fn(conn, mine, src):
                done.append(finish_row(conn, res)["id"])
        return _collect(conn, batch_id, unticked + done, out)


def apply_now(conn, items: list[dict], **meta: Any) -> dict:
    """A direct change (Leo named 1–3 issues): record and write in one step, undoable like a card."""
    return apply(conn, propose(conn, items, **meta))


def dismiss(conn, batch_id: str, ids: list[int | str] | None = None) -> int:
    """不用了: proposed rows become dismissed and stay in the audit. Returns how many."""
    wanted = None if ids is None else {int(i) for i in ids}
    with _LOCK:
        n = 0
        for r in rows(conn, batch_id):
            if r["status"] == "proposed" and (wanted is None or r["id"] in wanted):
                _finish(conn, r["id"], "dismissed")
                n += 1
        return n


def undo(conn, batch_id: str) -> dict:
    """Put back the old value of every applied row whose field still holds what the assistant wrote;
    one Leo changed afterwards is left alone for good and noted (status stays applied, result kept).
    Created issues are deleted if untouched, else parked. Comments stay. A second call finds nothing
    left and returns already=True."""
    with _LOCK:
        out = _empty(batch_id)
        pending = [r for r in rows(conn, batch_id) if _undo_pending(r)]
        if not pending:
            out["already"] = True
            return out
        src = source(conn, task_id=pending[0]["task_id"], thread_id=pending[0]["thread_id"])
        done = []
        for kind in dict.fromkeys(r["kind"] for r in pending):
            mine = [r for r in reversed(pending) if r["kind"] == kind]
            fn = _undo_issue if kind == "issue" else _HANDLERS[kind][1]
            for res in fn(conn, mine, src):
                done.append(finish_row(conn, res)["id"])
        return _collect(conn, batch_id, done, out)


# ---------------------------------------------------------------- kind: issue

def _groups(set_rows: list[dict]) -> list[list[dict]]:
    """Consecutive-by-target groups with each field once: one change_fields call, one activity line."""
    groups: dict[str, list[list[dict]]] = {}
    order: list[list[dict]] = []
    for r in set_rows:
        lst = groups.setdefault(r["target"], [])
        if not lst or any(x["field"] == r["field"] for x in lst[-1]):
            lst.append([])
            order.append(lst[-1])
        lst[-1].append(r)
    return order


def _by_field(result: dict) -> dict[str, tuple[str, dict]]:
    out = {}
    for key in ("applied", "skipped", "same", "failed"):
        for x in result[key]:
            out[x["field"]] = (key, x)
    return out


def _error(r: dict, e: Exception, status: str) -> dict:
    return {"id": r["id"], "status": status, "result": "error", "note": issues.error_text(e)}


def _apply_issue(conn, pending: list[dict], src: dict) -> list[dict]:
    res: list[dict] = []
    for r in pending:
        if r["action"] == "create":
            values = dict(r["new"] or {})
            title = values.pop("title", "")
            project, parent = values.pop("project", ""), values.pop("parent", "")
            try:
                ref, state = issues.create_checked(title, project=project, parent=parent, source=src, **values)
            except (*issues.ERRORS, TypeError) as e:
                res.append(finish_row(conn, _error(r, e, "dismissed")))
                continue
            done = {"id": r["id"], "status": "applied", "target": ref, "detail": {"state": state}}
            res.append(finish_row(conn, done))
        elif r["action"] == "comment":
            v = r["new"] or {}
            by = v.get("by", "assistant")
            try:
                issues.comment_checked(r["target"], v.get("text", ""), by=by,
                                       via="assistant" if by == "user" else v.get("via", ""), link=src["url"])
            except issues.ERRORS as e:
                res.append(finish_row(conn, _error(r, e, "dismissed")))
                continue
            res.append(finish_row(conn, {"id": r["id"], "status": "applied"}))
    for group in _groups([r for r in pending if r["action"] == "set"]):
        try:
            result = issues.change_fields(group[0]["target"], [(r["field"], r["old"], r["new"]) for r in group],
                                          source=src)
        except issues.ERRORS as e:
            res += [finish_row(conn, _error(r, e, "dismissed")) for r in group]
            continue
        fields = _by_field(result)
        for r in group:
            key, x = fields.get(r["field"], ("failed", {"error": "没有执行。"}))
            if key == "applied":
                detail = {"old": x["old"], "new": x["new"], **({"postponed": x["postponed"]} if "postponed" in x else {})}
                out = {"id": r["id"], "status": "applied", "detail": detail}
            elif key == "skipped":
                out = {"id": r["id"], "status": "dismissed", "result": "conflict", "detail": {"current": x["current"]},
                       "note": f"这条你刚改成了 {issues.display(r['field'], x['current'])}，我没动"}
            elif key == "same":
                out = {"id": r["id"], "status": "dismissed", "result": "noop", "note": "本来就是这样，不用改"}
            else:
                out = {"id": r["id"], "status": "dismissed", "result": "error", "note": x["error"]}
            res.append(finish_row(conn, out))
    return res


def _undo_issue(conn, pending: list[dict], src: dict) -> list[dict]:
    res: list[dict] = []
    for r in pending:
        if r["action"] != "create":
            continue
        try:
            outcome = issues.remove_created(r["target"], (r["detail"] or {}).get("state"), source=src)
        except (*issues.ERRORS, StaleFileError, OSError) as e:
            res.append(finish_row(conn, _error(r, e, "applied")))
            continue
        if outcome == "deleted":
            _dismiss_aimed_at(conn, r["target"])
        if outcome == "kept":
            res.append(finish_row(conn, {"id": r["id"], "status": "applied", "result": "kept",
                                         "note": "这条议题你已经处理过，留着没动"}))
        else:
            res.append(finish_row(conn, {"id": r["id"], "status": "undone", "result": outcome}))
    for group in _groups([r for r in pending if r["action"] == "set"]):
        # Undo compares against what the write really stored (detail), then the proposal for old rows.
        written = [(r, (r["detail"] or {}).get("new", r["new"]), (r["detail"] or {}).get("old", r["old"]))
                   for r in group]
        moved = [r for r in group if r["field"] in ("due", "week")]
        # Exact, 0 included: undoing a move to an earlier date must not count as a postponement.
        carried = -sum(int((r["detail"] or {}).get("postponed", (r["detail"] or {}).get("carried")) or 0)
                       for r in moved) if moved else None
        try:
            result = issues.change_fields(group[0]["target"], [(r["field"], new, old) for r, new, old in written],
                                          source=src, undo=True, carried_delta=carried)
        except issues.ERRORS as e:
            res += [finish_row(conn, _error(r, e, "applied")) for r in group]
            continue
        fields = _by_field(result)
        for r in group:
            key, x = fields.get(r["field"], ("failed", {"error": "没有撤销。"}))
            if key == "applied":
                out = {"id": r["id"], "status": "undone"}
            elif key == "same":
                out = {"id": r["id"], "status": "undone", "result": "noop"}
            elif key == "skipped":
                out = {"id": r["id"], "status": "applied", "result": "kept",
                       "detail": {**(r["detail"] or {}), "current": x["current"]},
                       "note": f"这条你后来改成了 {issues.display(r['field'], x['current'])}，我没动"}
            else:
                out = {"id": r["id"], "status": "applied", "result": "error", "note": x["error"]}
            res.append(finish_row(conn, out))
    return res


# ---------------------------------------------------------------- words

def _what(r: dict) -> str:
    label = r.get("label") or ""
    if r["action"] == "create":
        return f"「{label}」"
    if r["kind"] != "issue":
        return label or r["target"]
    return f"「{label}」的{i18n.ISSUE_FIELD.get(r['field'], r['field'])}"


def summary(outcome: dict) -> str:
    """「已改 3 处；「读 Acharya 第三章」的计划周你刚改成了 第 43 周，我没动」 and the like."""
    if outcome.get("already"):
        return "这批修改已经处理过了。"
    parts: list[str] = []
    applied = outcome["applied"]
    created = [r for r in applied if r["action"] == "create"]
    changed = [r for r in applied if r["action"] == "set"]
    if changed:
        parts.append(f"已改 {len(changed)} 处")
    if created:
        parts.append(f"新建 {len(created)} 个议题")
    if outcome["undone"]:
        parts.append(f"已撤销 {len(outcome['undone'])} 处")
    if outcome["same"]:
        parts.append(f"{len(outcome['same'])} 处本来就是这样")
    for r in outcome["skipped"] + outcome["kept"]:
        parts.append(f"{_what(r)}：{r['note']}")
    for r in outcome["failed"]:
        parts.append(f"{_what(r)}没改成：{r['note']}")
    return "；".join(parts) + "。" if parts else "没有改动。"
