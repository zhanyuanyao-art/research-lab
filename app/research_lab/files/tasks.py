"""_data/tasks.yml: the user's 待办 — one entry per task under a 课题, scheduled by its due date.

File order is priority. The file is content like `now.yml` and `projects.yml`: hand-editable, diffed
and committed with the week, so writes go through the same ruamel round-trip and sha guard as
`datafiles.update_project`. A missing file reads as no tasks and is created on the first add.

Statuses: todo | doing | done | parked. `postponed` counts how many times `due` was moved later; the
progress check treats two or more as a signal to park or split the task (docs/design/PM-AGENT.md).

No plan week (Leo 10-10, docs/design/ISSUES-LINEAR.md §3): 本周 means due by this Sunday, plus anything
overdue. Files written before that may still carry `week` and `carried` until `rl tasks migrate-week`
rewrites them; until then an open task with a week and no due date reads as due on that week's Sunday,
and `carried` reads as `postponed`. Callers not yet moved to due dates (the assistant's tools, 0093 →
card 3) still get two read-only keys: `week`, the ISO week of the due date, and `carried`, equal to
`postponed`. A week written to add()/update() becomes a due date on that week's Friday.
`updated` is the date of the last write that changed an entry (or reordered it): the progress check
calls an in-progress task stalled when it and its comments have not moved for pm.stalled_days.
`remove` exists only for undoing an untouched task the assistant created (docs/design/ISSUE-AGENT.md §5).
Ids (`T-YYYY-NNNN`) are for URLs and links only and never appear in page copy.

Sub-tasks, as issues and sub-issues in Linear: a task may name a `parent` in the same 课题, one level
deep. A piece of writing handed in as a Word file is a parent with `due`, `words` (target, 中文字 +
英文词) and `file` (its name under private/<课题>/); each outline section is a sub-task with its own
`due`. Parking a parent parks its open sub-tasks, and bringing it back (postpone or a status change)
brings them back. A parent can be marked done at any time (Leo, 10-01): its open sub-tasks keep their
state and are shown folded under it (docs/design/TASKS.md).
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import SingleQuotedScalarString

from ..config import Paths, get_paths
from .weeks import week_of, week_start
from .yamlio import StaleFileError, load_yaml, save_yaml, sha256_text, dump_yaml, atomic_write

STATUSES = ("todo", "doing", "done", "parked")
OPEN = ("todo", "doing")
ORIGINS = ("user", "assistant")
FIELDS = ("id", "title", "project", "status", "parent", "due", "words", "file", "origin", "source", "created",
          "done", "updated", "postponed", "note", "priority", "assignee", "labels")
OPTIONAL = ("parent", "due", "words", "file")       # written only when set
EDITABLE = ("title", "project", "note", "source", "status", "priority", "assignee", "labels", *OPTIONAL)
LEGACY = ("week", "carried")                        # read until `rl tasks migrate-week`; never written
ID_RE = re.compile(r"^T-(\d{4})-(\d{4})$")
WEEK_RE = re.compile(r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$")
TITLE_MAX = 120
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WORDS_MAX = 2_000_000
_BAD_FILE = re.compile(r"[\\/\x00-\x1f]")
HEADER = ("One entry per 待办. Order is priority. `status` is todo | doing | done | parked; `due` is the\n"
          "deadline; `postponed` counts moves of `due` to a later date. Written by the app, safe to hand-edit.")


log = logging.getLogger("research_lab.tasks")
BROKEN = "待办文件 _data/tasks.yml 读不出来，格式有误：页面上暂时看不到其中的待办，文件本身没有改动。请先修好这个文件再保存。"


class TaskError(ValueError):
    """A user-facing validation failure (the page shows it, status 400)."""


# ---------------------------------------------------------------- file

def path(paths: Paths | None = None) -> Path:
    return (paths or get_paths()).data_yml / "tasks.yml"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def file_sha(paths: Paths | None = None) -> str:
    """sha of the file as it is now; the sha of an empty text when the file does not exist yet."""
    return sha256_text(_text(path(paths)))


def _check_sha(p: Path, expected_sha: str | None) -> None:
    if expected_sha is not None and sha256_text(_text(p)) != expected_sha:
        raise StaleFileError(f"{p.name} changed on disk since it was loaded")


def _load(p: Path) -> CommentedSeq:
    """The list to write back. A file that does not parse, or parses as something other than a list,
    is refused rather than replaced: it is the user's hand-editable content."""
    try:
        data = load_yaml(p) if p.exists() and p.read_text(encoding="utf-8").strip() else None
    except Exception:  # noqa: BLE001 - YAML errors come in many classes
        raise TaskError(BROKEN) from None
    if data is not None and not isinstance(data, CommentedSeq):
        raise TaskError(BROKEN)
    if data is None or not len(data):
        data = CommentedSeq()
        data.yaml_set_start_comment(HEADER)
    return data


def _count(v: Any) -> int:
    """`postponed` as written by hand: anything but a whole number counts as 0."""
    return int(v) if str(v or "").isdigit() else 0


# ---------------------------------------------------------------- dates

def friday(week: str) -> str:
    """ISO date of an ISO week's Friday."""
    return (week_start(week) + timedelta(days=4)).isoformat()


def week_due(week: str, today: date | None = None) -> str:
    """Where a week written by an older caller puts the due date: its Friday, but this week never
    before today (「这周做完」 on a Saturday is today, not an overdue Friday)."""
    today = today or date.today()
    return this_friday(today) if week == week_of(today) else friday(week)


def sunday(week: str) -> str:
    """ISO date of an ISO week's Sunday: an unmigrated open task's due date (Leo 10-10)."""
    return (week_start(week) + timedelta(days=6)).isoformat()


def week_end(today: date | None = None) -> date:
    """This week's Sunday: 本周 runs until then."""
    today = today or date.today()
    return today + timedelta(days=6 - today.weekday())


def this_friday(today: date | None = None) -> str:
    """「本周五」; once Friday has passed it is today (docs/design/ISSUES-LINEAR.md §3.2)."""
    today = today or date.today()
    return max(today, today + timedelta(days=4 - today.weekday())).isoformat()


def next_friday(today: date | None = None) -> str:
    """「下周五」, where the progress check and 挪到下周 put a due date."""
    today = today or date.today()
    return (today + timedelta(days=11 - today.weekday())).isoformat()


def _due_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _legacy_due(entry: Any) -> str:
    """The due date an entry has, or the Sunday of its old plan week if it is open and has none."""
    due = entry.get("due")
    if isinstance(due, date):
        return due.isoformat()
    if due:
        return str(due)
    week = str(entry.get("week") or "")
    return sunday(week) if str(entry.get("status")) in OPEN and WEEK_RE.match(week) else ""


def _week_due(values: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Compatibility for writers that still speak in plan weeks: `week` becomes `due` on its Friday
    unless a due date is given too; an empty week means no due date."""
    if "week" not in values:
        return values
    out = dict(values)
    week = str(out.pop("week") or "").strip()
    if week and not WEEK_RE.match(week):
        raise TaskError("周请写成 ISO 周，例如 2026-W41。")
    if not out.get("due"):
        out["due"] = week_due(week, today) if week else ""
    return out


def _plain(entry: Any) -> dict[str, Any]:
    """A task as a plain dict with string dates (ruamel would otherwise hand back datetime.date)."""
    out: dict[str, Any] = {}
    for k in FIELDS:
        if k in ('priority', 'assignee', 'labels') and k not in entry:
            continue
        v = entry.get(k) if hasattr(entry, "get") else None
        if isinstance(v, date):
            v = v.isoformat()
        elif k == "labels":
            v = list(v) if isinstance(v, list) else None
        elif k == "words":
            v = int(v) if str(v or "").isdigit() else None
        elif k == "postponed":
            v = _count(v if "postponed" in entry else entry.get("carried"))
        elif v is not None:
            v = str(v)
        out[k] = v
    out["due"] = _legacy_due(entry) or None
    out["note"] = out.get("note") or ""
    out["source"] = out.get("source") or ""
    due = _due_date(out["due"])
    out["week"] = week_of(due) if due else ""        # read-only, for callers not yet on due dates
    out["carried"] = out["postponed"]
    return out


def read(paths: Paths | None = None) -> list[dict[str, Any]]:
    """A file that does not parse reads as no tasks (logged) so the pages still open; writes refuse it."""
    p = path(paths)
    if not p.exists():
        return []
    try:
        data = load_yaml(p)
    except Exception as e:  # noqa: BLE001
        log.warning("tasks.yml does not parse, reading it as empty: %s", str(e).splitlines()[0] if str(e) else type(e).__name__)
        return []
    return [_plain(e) for e in (data if isinstance(data, list) else []) if hasattr(e, "get") and e.get("id")]


def problem(paths: Paths | None = None) -> str | None:
    """Why tasks.yml cannot be used, for a page to say so instead of showing an empty list."""
    p = path(paths)
    if not _text(p).strip():
        return None
    try:
        data = load_yaml(p)
    except Exception:  # noqa: BLE001
        return BROKEN
    return None if data is None or isinstance(data, list) else BROKEN


def get(task_id: str, paths: Paths | None = None) -> dict[str, Any] | None:
    return next((t for t in read(paths) if t["id"] == task_id), None)


# ---------------------------------------------------------------- validation

def _project_slugs(paths: Paths | None) -> list[str]:
    from . import datafiles
    return [str(p["slug"]) for p in (datafiles.read("projects", paths) or [])]


def _clean(values: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in values.items():
        if v is None:
            continue
        if k == 'labels':
            if not isinstance(v, (list, tuple)):
                raise TaskError('标签需要从已有标签中选择。')
            out[k] = list(dict.fromkeys(str(x).strip() for x in v if str(x).strip()))
        else:
            out[k] = str(v).strip() if k == "note" else " ".join(str(v).split())
    return out


def _validate(values: dict[str, Any], paths: Paths | None, *, creating: bool) -> None:
    from ..i18n import ISSUE_LABELS, ISSUE_PRIORITY
    if 'priority' in values and values['priority'] not in ISSUE_PRIORITY:
        raise TaskError('请选择有效的优先级。')
    if len(values.get('assignee', '')) > 60:
        raise TaskError('负责人名称不要超过 60 个字。')
    if any(v not in ISSUE_LABELS for v in values.get('labels', [])):
        raise TaskError('请选择已有标签。')
    if len(values.get('note', '')) > 30000:
        raise TaskError('描述请控制在三万个字以内。')
    if "title" in values or creating:
        title = values.get("title", "")
        if not title:
            raise TaskError("待办要有一句话标题。")
        if len(title) > TITLE_MAX:
            raise TaskError(f"标题不要超过 {TITLE_MAX} 个字。")
    if "project" in values or creating:
        slugs = _project_slugs(paths)
        if not values.get("project"):
            raise TaskError("请选择待办属于哪个课题。")
        if slugs and values["project"] not in slugs:
            raise TaskError("找不到这个课题。")
    if values.get("status") and values["status"] not in STATUSES:
        raise TaskError("未知的待办状态。")
    if values.get("origin") and values["origin"] not in ORIGINS:
        raise TaskError("未知的来源。")
    if values.get("due"):
        try:
            if not DATE_RE.match(values["due"]):
                raise ValueError
            date.fromisoformat(values["due"])
        except ValueError:
            raise TaskError("截止日期请写成 YYYY-MM-DD，例如 2026-11-15。") from None
    if values.get("words") and (not values["words"].isdigit() or not 1 <= int(values["words"]) <= WORDS_MAX):
        raise TaskError("目标字数请写一个正整数。")
    if values.get("file") and (_BAD_FILE.search(values["file"]) or values["file"].startswith(".") or len(values["file"]) > 200):
        raise TaskError("文件只写 private/<课题>/ 下的文件名，不带路径。")


def _check_parent(data: CommentedSeq, task_id: str | None, parent_id: str, project: str) -> None:
    """One level deep, same 课题, never itself."""
    if parent_id == task_id:
        raise TaskError("待办不能是自己的子任务。")
    parent = next((e for e in data if hasattr(e, "get") and str(e.get("id")) == parent_id), None)
    if parent is None:
        raise TaskError("找不到父任务。")
    if parent.get("parent"):
        raise TaskError("子任务下面不能再建子任务。")
    if task_id and any(hasattr(e, "get") and str(e.get("parent") or "") == task_id for e in data):
        raise TaskError("有子任务的待办不能再挂到别的待办下面。")
    if str(parent.get("project") or "") != project:
        raise TaskError("子任务要和父任务在同一个课题。")


def _set_postponed(entry: CommentedMap, n: int) -> None:
    """Write the count as `postponed`, taking the place of an unmigrated `carried`."""
    if "postponed" not in entry and "carried" in entry:
        keys = list(entry.keys())
        at = keys.index("carried")
        entry.pop("carried")
        entry.insert(at, "postponed", n)
    else:
        entry["postponed"] = n


def _set_due(entry: CommentedMap, value: str, adjust: int | None = None) -> None:
    """Set or clear `due`. A move to a later date than the one in force counts as a postponement;
    `adjust` replaces that rule with a fixed change of the count (undo passes minus what it added)."""
    before = _due_date(_legacy_due(entry))
    _set_optional(entry, "due", value)
    entry.pop("week", None)          # an explicit due date (or none) replaces an unmigrated plan week
    after = _due_date(value)
    if adjust is not None:
        _set_postponed(entry, max(0, _count(entry.get("postponed", entry.get("carried"))) + adjust))
    elif before and after and after > before:
        _set_postponed(entry, _count(entry.get("postponed", entry.get("carried"))) + 1)


def _set_optional(entry: CommentedMap, key: str, value: str) -> None:
    """Optional fields sit after `status` (or an unmigrated `week`) when set and disappear when cleared."""
    if not value:
        entry.pop(key, None)
        return
    v: Any = int(value) if key == "words" else (_q(value) if key == "due" else value)
    if key in entry:
        entry[key] = v
        return
    keys = list(entry.keys())
    anchor = max(keys.index(k) for k in ("status", "week", *OPTIONAL[:OPTIONAL.index(key)]) if k in keys)
    entry.insert(anchor + 1, key, v)


def next_id(data: CommentedSeq | list, today: date | None = None, retired: Any = ()) -> str:
    """max + 1 within the year, counting `retired` ids too: a deleted task's id is never issued again."""
    year = (today or date.today()).year
    n = 0
    for tid in [str(e.get("id", "") if hasattr(e, "get") else "") for e in data] + [str(r) for r in retired]:
        m = ID_RE.match(tid)
        if m and int(m.group(1)) == year:
            n = max(n, int(m.group(2)))
    return f"T-{year}-{n + 1:04d}"


def retired_ids(paths: Paths | None = None) -> set[str]:
    """Task ids issues.yml still names: the tombstone undo leaves for a deleted task, or properties a
    hand-deleted one left behind. A new task must not inherit them, nor an old card aimed at them."""
    f = (paths or get_paths()).data_yml / "issues.yml"
    try:
        data = load_yaml(f) if f.exists() else None
    except Exception:  # noqa: BLE001 - a broken sidecar is reported where it is read
        return set()
    return {str(k) for k in data if ID_RE.match(str(k))} if isinstance(data, dict) else set()


def _find(data: CommentedSeq, task_id: str) -> CommentedMap:
    entry = next((e for e in data if hasattr(e, "get") and str(e.get("id")) == task_id), None)
    if entry is None:
        raise KeyError(task_id)
    return entry


def _q(day: str) -> SingleQuotedScalarString:
    return SingleQuotedScalarString(day)   # keeps `'2026-10-01'` a string on the way back in


def _fingerprint(entry: Any) -> tuple:
    return tuple((str(k), str(v)) for k, v in entry.items() if k != "updated")


def _snapshot(data: CommentedSeq) -> dict[str, tuple]:
    return {str(e.get("id")): _fingerprint(e) for e in data if hasattr(e, "get")}


def _stamp(entry: CommentedMap, today: date | None) -> None:
    day = _q((today or date.today()).isoformat())
    if "updated" in entry:
        entry["updated"] = day
        return
    keys = list(entry.keys())
    anchor = next((k for k in ("done", "created") if k in keys), None)
    entry.insert(keys.index(anchor) + 1 if anchor else len(keys), "updated", day)


def _save(p: Path, data: CommentedSeq, expected_sha: str | None, before: dict[str, tuple],
          today: date | None, also: tuple[str, ...] = ()) -> str:
    """save_yaml, stamping `updated` on every entry this write changed, cascades and re-derived parent
    weeks included (they moved too), plus `also` (a reordered entry, whose fields are unchanged)."""
    for e in data:
        if hasattr(e, "get") and (before.get(str(e.get("id"))) != _fingerprint(e) or str(e.get("id")) in also):
            _stamp(e, today)
    return save_yaml(p, data, expected_sha)


# ---------------------------------------------------------------- writes

def add(values: dict[str, Any], expected_sha: str | None = None, paths: Paths | None = None,
        today: date | None = None) -> tuple[str, str]:
    """Append a task. Returns (id, new sha). Raises TaskError, StaleFileError."""
    p = path(paths)
    _check_sha(p, expected_sha)
    today = today or date.today()
    vals = _clean(_week_due(values, today))
    vals.setdefault("status", "todo")
    vals.setdefault("origin", "user")
    _validate(vals, paths, creating=True)
    data = _load(p)
    before = _snapshot(data)
    if vals.get("parent"):
        _check_parent(data, None, vals["parent"], vals["project"])
        parent_source = str(_find(data, vals["parent"]).get("source") or "")
        if vals.get("source") and vals["source"] != parent_source:
            raise TaskError("子任务的来源要和父任务一致。")
    task_id = next_id(data, today, retired_ids(paths))
    entry = CommentedMap()
    entry["id"] = task_id
    entry["title"] = vals["title"]
    entry["project"] = vals["project"]
    entry["status"] = vals["status"]
    for k in OPTIONAL:
        if vals.get(k):
            _set_optional(entry, k, vals[k])
    entry["origin"] = vals["origin"]
    if vals.get("source"):
        entry["source"] = vals["source"]
    entry["created"] = _q(today.isoformat())
    entry["done"] = _q(today.isoformat()) if vals["status"] == "done" else None
    entry["postponed"] = 0
    entry["note"] = vals.get("note", "")
    for k in ('priority', 'assignee', 'labels'):
        if k in vals:
            entry[k] = vals[k]
    data.append(entry)
    sha = _save(p, data, expected_sha, before, today)
    from . import issues       # the key (APUS-0002) lives beside issues.yml, not in tasks.yml
    issues.assign_key(task_id, project=vals["project"], paths=paths)
    return task_id, sha


def update(task_id: str, changes: dict[str, Any], expected_sha: str | None = None, paths: Paths | None = None,
           today: date | None = None, *, cascade_status: bool = True, adjust_postponed: int | None = None,
           renumber: bool = True) -> str:
    """Change title / project / note / source / status / parent / due / words / file of one task.
    Unchanged fields are left alone; an emptied optional field is removed; a later due date counts as
    a postponement (adjust_postponed: undo's fixed change instead). A move to a 课题 in another direction
    renumbers the task (issues.sync_keys); files/issues.py passes renumber=False and does it itself.
    Returns the new sha. Raises KeyError, TaskError, StaleFileError."""
    p = path(paths)
    _check_sha(p, expected_sha)
    changes = _week_due(changes, today)
    unknown = set(changes) - set(EDITABLE)
    if unknown:
        raise TaskError(f"不能修改字段：{', '.join(sorted(unknown))}")
    vals = _clean(changes)
    _validate(vals, paths, creating=False)
    data = _load(p)
    entry = _find(data, task_id)
    old_project = str(entry.get("project") or "")
    project = vals.get("project") or str(entry.get("project") or "")
    parent = vals["parent"] if "parent" in vals else str(entry.get("parent") or "")
    if parent:
        _check_parent(data, task_id, parent, project)
    if project != str(entry.get("project") or "") and any(
            hasattr(e, "get") and str(e.get("parent") or "") == task_id for e in data):
        raise TaskError("有子任务的待办不能单独换课题。")
    before = _statuses(data)
    snap = _snapshot(data)
    changed = False
    for k, v in vals.items():
        current = _legacy_due(entry) if k == "due" else entry.get(k)
        if (current == v if k in ('priority', 'assignee', 'labels') else str(current or "") == v):
            continue
        if k == "due":
            _set_due(entry, v, adjust_postponed)
        elif k in OPTIONAL:
            _set_optional(entry, k, v)
        elif k == "status":
            if cascade_status:
                _change_status(data, entry, v, today)
            else:
                _apply_status(entry, v, today)
        elif k in ("note", "source") and not v:
            if k in entry:
                entry[k] = "" if k == "note" else entry.pop(k)
        else:
            entry[k] = v
        changed = True
    if not changed:
        return sha256_text(_text(p))
    sha = _save(p, data, expected_sha, snap, today)
    _note_done(before, data, paths)
    if renumber and paths is None and project != old_project:
        from . import issues
        issues.renumber_quietly()
    return sha


def _apply_status(entry: CommentedMap, status: str, today: date | None = None) -> None:
    entry["status"] = status
    entry["done"] = _q((today or date.today()).isoformat()) if status == "done" else None


def _subs(data: CommentedSeq, task_id: str) -> list[CommentedMap]:
    return [e for e in data if hasattr(e, "get") and str(e.get("parent") or "") == task_id]


def _change_status(data: CommentedSeq, entry: CommentedMap, status: str, today: date | None = None) -> None:
    """Parking a parent parks its open sub-tasks; un-parking it brings the parked ones back as todo."""
    before = str(entry.get("status"))
    _apply_status(entry, status, today)
    tid = str(entry.get("id"))
    if status == "parked":
        for e in _subs(data, tid):
            if str(e.get("status")) in OPEN:
                _apply_status(e, "parked", today)
    elif before == "parked" and status in OPEN:
        for e in _subs(data, tid):
            if str(e.get("status")) == "parked":
                _apply_status(e, "todo", today)


def _note_done(before: dict[str, str], data: CommentedSeq, paths: Paths | None) -> None:
    """A task hung under an output (source ART-…) that has just been finished leaves one system line in
    that output's 记录 (docs/design/ARCHIVE-ISSUES.md §B, §H.4). Never fails the write it follows."""
    from . import updates
    for e in data:
        if not hasattr(e, "get"):
            continue
        tid, source = str(e.get("id")), str(e.get("source") or "")
        if source.startswith("ART-") and str(e.get("status")) == "done" and before.get(tid) not in (None, "done"):
            updates.record(source, f"完成待办「{e.get('title')}」", paths=paths)


def _statuses(data: CommentedSeq) -> dict[str, str]:
    return {str(e.get("id")): str(e.get("status")) for e in data if hasattr(e, "get")}


def set_status(task_id: str, status: str, expected_sha: str | None = None, paths: Paths | None = None,
               today: date | None = None) -> str:
    """todo | doing | done | parked. `done` stamps the date; leaving done clears it."""
    if status not in STATUSES:
        raise TaskError("未知的待办状态。")
    p = path(paths)
    _check_sha(p, expected_sha)
    data = _load(p)
    entry = _find(data, task_id)
    if str(entry.get("status")) == status:
        return sha256_text(_text(p))
    before = _statuses(data)
    snap = _snapshot(data)
    _change_status(data, entry, status, today)
    sha = _save(p, data, expected_sha, snap, today)
    _note_done(before, data, paths)
    return sha


def postpone(task_id: str, due: str, expected_sha: str | None = None, paths: Paths | None = None,
             *, today: date | None = None) -> str:
    """Give an open or parked task a new due date (下周五 on the 待办 page and the home plan form). A
    later date than the one in force counts as a postponement; a parked task comes back as todo, a
    parked parent with its parked sub-tasks. A done task is not rescheduled."""
    if not DATE_RE.match(due or "") or _due_date(due) is None:
        raise TaskError("截止日期请写成 YYYY-MM-DD，例如 2026-11-15。")
    p = path(paths)
    _check_sha(p, expected_sha)
    data = _load(p)
    snap = _snapshot(data)
    entry = _find(data, task_id)
    if str(entry.get("status")) == "done":
        raise TaskError("已完成的待办不用再改截止。")
    unpark = str(entry.get("status")) == "parked"
    if _legacy_due(entry) == due and not unpark:
        return sha256_text(_text(p))
    _set_due(entry, due)
    if unpark:
        entry["status"] = "todo"
        for e in _subs(data, task_id):
            if str(e.get("status")) == "parked":
                e["status"] = "todo"
    return _save(p, data, expected_sha, snap, today)


def carry(task_id: str, to_week: str, expected_sha: str | None = None, paths: Paths | None = None,
          *, today: date | None = None, **_: Any) -> str:
    """Compatibility for callers that still move tasks between weeks (removed with card 3): the task
    becomes due on that week's Friday (postpone)."""
    if not WEEK_RE.match(to_week or ""):
        raise TaskError("周请写成 ISO 周，例如 2026-W41。")
    return postpone(task_id, week_due(to_week, today), expected_sha, paths, today=today)


def move(task_id: str, before_id: str | None, expected_sha: str | None = None, paths: Paths | None = None,
         today: date | None = None) -> str:
    """Reorder: put the task just before `before_id`, or last when None. File order is priority, and
    among a parent's sub-tasks it is their order."""
    p = path(paths)
    _check_sha(p, expected_sha)
    data = _load(p)
    snap = _snapshot(data)
    entry = _find(data, task_id)
    if before_id == task_id:
        return sha256_text(_text(p))
    if before_id:
        _find(data, before_id)
    data.remove(entry)
    if before_id:
        data.insert(next(i for i, e in enumerate(data) if hasattr(e, "get") and str(e.get("id")) == before_id), entry)
    else:
        data.append(entry)
    return _save(p, data, expected_sha, snap, today, also=(task_id,))


def remove(task_id: str, expected_sha: str | None = None, paths: Paths | None = None) -> str:
    """Delete one task. Only undo of an assistant-created task calls this (files.issues.remove_created
    decides whether it is untouched and leaves the id's tombstone in issues.yml, so retired_ids keeps
    it from being issued again); nothing on a page deletes. Refuses a task the assistant did not
    create or one with sub-tasks, so a slip in the caller cannot take Leo's work with it."""
    p = path(paths)
    _check_sha(p, expected_sha)
    data = _load(p)
    entry = _find(data, task_id)
    if str(entry.get("origin") or "") != "assistant":
        raise TaskError("只能删除助手新建的待办。")
    if _subs(data, task_id):
        raise TaskError("有子任务的待办不能删除。")
    snap = _snapshot(data)
    data.remove(entry)
    return _save(p, data, expected_sha, snap, None)


def entries(task_ids: list[str], paths: Paths | None = None) -> list[tuple[int, CommentedMap]]:
    """Copies of these tasks' entries with their places in tasks.yml, for a delete's undo (files.issues.delete)."""
    import copy
    data = _load(path(paths))
    wanted = set(task_ids)
    return [(i, copy.deepcopy(e)) for i, e in enumerate(data) if hasattr(e, "get") and str(e.get("id")) in wanted]


def delete_text(task_ids: list[str], expected_sha: str | None = None, paths: Paths | None = None) -> str:
    """Validate a task deletion and prepare its YAML without writing."""
    p = path(paths)
    _check_sha(p, expected_sha)
    data = _load(p)
    wanted = set(task_ids)
    for tid in task_ids:
        _find(data, tid)
        if any(str(e.get("id")) not in wanted for e in _subs(data, tid)):
            raise TaskError("子任务要和它一起删除。")
    for e in [e for e in data if hasattr(e, "get") and str(e.get("id")) in wanted]:
        data.remove(e)
    return dump_yaml(data)


def delete(task_ids: list[str], expected_sha: str | None = None, paths: Paths | None = None) -> str:
    return atomic_write(path(paths), delete_text(task_ids, expected_sha, paths), expected_sha)


def restore_text(removed: list[tuple[int, Any]], expected_sha: str | None = None, paths: Paths | None = None) -> str:
    """Prepare restored entries at their old positions without changing timestamps."""
    p = path(paths)
    _check_sha(p, expected_sha)
    data = _load(p)
    present = {str(e.get("id")) for e in data if hasattr(e, "get")}
    if any(str(entry.get("id")) in present for _, entry in removed):
        raise TaskError("这条待办已经在了。")
    for at, entry in sorted(removed, key=lambda x: int(x[0])):
        data.insert(min(int(at), len(data)), entry)
    return dump_yaml(data)


def restore(removed: list[tuple[int, Any]], expected_sha: str | None = None, paths: Paths | None = None) -> str:
    return atomic_write(path(paths), restore_text(removed, expected_sha, paths), expected_sha)


# ---------------------------------------------------------------- queries (over plain dicts)

def is_open(t: dict[str, Any]) -> bool:
    return t.get("status") in OPEN


def due_this_week(t: dict[str, Any], today: date | None = None) -> bool:
    """本周待办: open and due by this Sunday, overdue included (Leo 10-10)."""
    due = _due_date(t.get("due"))
    return is_open(t) and due is not None and due <= week_end(today)


def done_this_week(t: dict[str, Any], today: date | None = None) -> bool:
    """Finished since this Monday: the home list keeps these, ticked."""
    today = today or date.today()
    done = _due_date(t.get("done"))
    return t.get("status") == "done" and done is not None and today - timedelta(days=today.weekday()) <= done <= today


def overdue(data: list[dict[str, Any]] | None = None, paths: Paths | None = None,
            today: date | None = None) -> list[dict[str, Any]]:
    """Open tasks whose due date has passed."""
    today = today or date.today()
    return [t for t in (data if data is not None else read(paths))
            if is_open(t) and (d := _due_date(t.get("due"))) is not None and d < today]


def for_project(slug: str, data: list[dict[str, Any]] | None = None, paths: Paths | None = None) -> list[dict[str, Any]]:
    return [t for t in (data if data is not None else read(paths)) if t.get("project") == slug]


def for_source(source: str, data: list[dict[str, Any]] | None = None, paths: Paths | None = None) -> list[dict[str, Any]]:
    """The top-level tasks hung under an output (source ART-…) or a milestone check, in file order: an
    output's 待办 on its page. A task belongs to one thing only: with a `parent` it is that parent's
    sub-task and shows there, whatever its source (docs/design/ARCHIVE-ISSUES.md §H.3)."""
    return [t for t in (data if data is not None else read(paths)) if t.get("source") == source and not t.get("parent")]


def children(parent_id: str, data: list[dict[str, Any]] | None = None, paths: Paths | None = None) -> list[dict[str, Any]]:
    """A task's sub-tasks, in file order."""
    return [t for t in (data if data is not None else read(paths)) if t.get("parent") == parent_id]


def flags(t: dict[str, Any], week: str = "", today: date | None = None) -> dict[str, Any]:
    """What a row shows besides its title, all from the due date: `week_due` due by this Sunday or
    overdue (本周), `this_week` due by Sunday and not yet overdue, `past_due` (also `late`, the name
    older callers read), days to the due date (negative once passed), `due_soon` within 7 days. Only
    open tasks get any of them. `week` is accepted for older callers and not used."""
    today = today or date.today()
    open_ = is_open(t)
    due = _due_date(t.get("due"))
    days = (due - today).days if due else None
    past = open_ and days is not None and days < 0
    week_due = due_this_week(t, today)
    return {"week_due": week_due, "this_week": week_due and not past, "late": past,
            "due_days": days, "past_due": past, "due_soon": open_ and days is not None and 0 <= days <= 7}


VIEWS = ("week", "overdue", "all")


def tree(rows: list[dict[str, Any]], view: str, week: str, *, project_order: list[str] | None = None,
         today: date | None = None) -> list[dict[str, Any]]:
    """The 待办 page as issues: one block per 课题 (in `project_order`, then any others), each with
    `items` (open top-level tasks) and `closed` (done and parked, shown only in the all view, folded).
    A node is {task, flags, subs: [{task, flags}], progress}; sub-tasks keep file order, a done
    parent keeps its open sub-tasks under it. A sub-task whose parent is missing stands on its own.

    week: open tasks due by this Sunday, overdue included; a parent shows when one of its sub-tasks does.
    overdue: open tasks past due, and parents with such a sub-task.
    all: everything. `week` is accepted for older callers and not used."""
    if view not in VIEWS:
        view = "week"
    ids = {t["id"] for t in rows}
    kids: dict[str, list[dict[str, Any]]] = {}
    for t in rows:
        if t.get("parent") in ids:
            kids.setdefault(t["parent"], []).append(t)

    def node(t: dict[str, Any]) -> dict[str, Any]:
        subs = kids.get(t["id"], [])
        live = [s for s in subs if s.get("status") != "parked"]
        return {"task": t, "flags": flags(t, week, today), "subs": [{"task": s, "flags": flags(s, week, today)} for s in subs],
                "progress": {"done": sum(1 for s in live if s.get("status") == "done"), "total": len(live)}}

    def shown(n: dict[str, Any]) -> bool:
        f, t = n["flags"], n["task"]
        if view == "all":
            return True
        key = "week_due" if view == "week" else "past_due"
        return f[key] or any(s["flags"][key] for s in n["subs"] if is_open(s["task"]))

    blocks: dict[str, dict[str, Any]] = {}
    for t in rows:
        if t.get("parent") in ids:
            continue
        n = node(t)
        if not shown(n):
            continue
        b = blocks.setdefault(t.get("project") or "", {"project": t.get("project") or "", "items": [], "closed": []})
        (b["items"] if is_open(t) or view != "all" else b["closed"]).append(n)
    order = {slug: i for i, slug in enumerate(project_order or [])}
    return sorted(blocks.values(), key=lambda b: (order.get(b["project"], len(order)), b["project"]))


def progress(task: dict[str, Any], data: list[dict[str, Any]] | None = None, paths: Paths | None = None) -> dict[str, int]:
    """Sub-tasks done out of those not parked. Word count against `words` is deliverables.word_count's job."""
    subs = [t for t in children(task["id"], data, paths) if t.get("status") != "parked"]
    return {"done": sum(1 for t in subs if t.get("status") == "done"), "total": len(subs)}


def groups(week: str = "", data: list[dict[str, Any]] | None = None, paths: Paths | None = None,
           recent_done: int = 20, today: date | None = None) -> dict[str, list[dict[str, Any]]]:
    """The 待办 sections by due date: 本周 (due by Sunday, not yet overdue) · 逾期 · 以后 (later or no
    due date) · 搁置 · 已完成 (newest first, capped). `week` is accepted for older callers and not used."""
    rows = data if data is not None else read(paths)
    today = today or date.today()
    out: dict[str, list[dict[str, Any]]] = {"week": [], "overdue": [], "later": [], "parked": [], "done": []}
    for t in rows:
        st = t.get("status")
        f = flags(t, today=today)
        if st == "done":
            out["done"].append(t)
        elif st == "parked":
            out["parked"].append(t)
        elif f["past_due"]:
            out["overdue"].append(t)
        elif f["this_week"]:
            out["week"].append(t)
        else:
            out["later"].append(t)
    out["done"] = sorted(out["done"], key=lambda t: (t.get("done") or "", t["id"]), reverse=True)[:recent_done]
    return out


# ---------------------------------------------------------------- one-time migration

def migrate_week(paths: Paths | None = None, *, write: bool = False) -> list[dict[str, Any]]:
    """`rl tasks migrate-week` for tasks.yml (Leo 10-10, ISSUES-LINEAR.md §3.3): an open task with a
    plan week and no due date gets that week's Sunday as its due date, so nothing turns overdue on the
    day of the migration; an open task with a due date keeps it; a done or parked one just loses the
    week; `carried` becomes `postponed` with the same count. Returns one entry per changed task
    {id, title, status, changes: [Chinese lines]}; writes only with write=True, without stamping
    `updated` (rewriting a key is not an edit of the task). The old values stay in git history."""
    p = path(paths)
    text = _text(p)
    if not text.strip():
        return []
    data = _load(p)
    out = []
    for e in data:
        if not hasattr(e, "get"):
            continue
        lines = []
        if "week" in e:
            week = str(e.get("week") or "")
            if not e.get("due") and str(e.get("status")) in OPEN and WEEK_RE.match(week):
                _set_optional(e, "due", sunday(week))
                lines.append(f"截止设为 {sunday(week)}，即原计划周 {week} 的周日")
            elif e.get("due") and str(e.get("status")) in OPEN:
                lines.append(f"保留截止 {_legacy_due(e)}，去掉计划周 {week or '空'}")
            else:
                lines.append(f"去掉计划周 {week or '空'}")
            e.pop("week")
        if "carried" in e:
            n = _count(e.get("carried"))
            _set_postponed(e, n)
            lines.append(f"carried 改名 postponed，次数 {n}")
        if lines:
            out.append({"id": str(e.get("id")), "title": str(e.get("title") or ""), "status": str(e.get("status") or ""),
                        "changes": lines})
    if write and out:
        start = data.ca.comment[1] if data.ca.comment and len(data.ca.comment) > 1 and data.ca.comment[1] else []
        if any("`week` is the ISO week" in getattr(c, "value", "") for c in start):
            data.ca.comment[1] = []
            data.yaml_set_start_comment(HEADER)
        save_yaml(p, data, sha256_text(text))
    return out
