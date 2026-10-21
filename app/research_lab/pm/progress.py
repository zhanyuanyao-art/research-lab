"""One sentence of progress for a parent task, shared by 首页, /tasks, the 课题 page and the assistant:

    第二章 · 距截止 12 天 · 1/2 节 · 8,200/20,000 字

Segments join with " · ". The due segment appears only with `due`; the sub-task segment only when
there are non-parked sub-tasks (unit 节 when the task has a Word `file`, otherwise 项); the word
segment only with `words` and a `file`, reading the docx's actual count (deliverables.word_count).
Everything here is a pure function over task dicts so views and tests never touch the file system
except through word_count_for().
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from ..config import Paths
from ..files import deliverables


def due_label(due: str | None, today: date | None = None) -> str | None:
    """'距截止 12 天' / '今天截止' / '已过期 3 天'; None without a due date or for an unreadable one."""
    if not due:
        return None
    try:
        d = date.fromisoformat(str(due)[:10])
    except ValueError:
        return None
    n = (d - (today or date.today())).days
    if n > 0:
        return f"距截止 {n} 天"
    if n == 0:
        return "今天截止"
    return f"已过期 {-n} 天"


def is_overdue(task: dict[str, Any], today: date | None = None) -> bool:
    """Hard deadline passed and the task is still open."""
    due = task.get("due")
    if not due or task.get("status") in ("done", "parked"):
        return False
    try:
        return date.fromisoformat(str(due)[:10]) < (today or date.today())
    except ValueError:
        return False


def counted(subs: list[dict[str, Any]] | None) -> dict[str, int]:
    """Sub-tasks done out of those not parked (parked ones are not expected, so they do not count)."""
    live = [t for t in (subs or []) if t.get("status") != "parked"]
    return {"done": sum(1 for t in live if t.get("status") == "done"), "total": len(live)}


def word_count_for(task: dict[str, Any], paths: Paths | None = None) -> int | None:
    """The actual word count of the task's Word file under private/<课题>/, or None when there is no
    file name, the file is missing, or it cannot be read."""
    name, project = task.get("file"), task.get("project")
    if not name or not project:
        return None
    try:
        path = deliverables.resolve(str(project), f"private/{name}", paths)
    except deliverables.DeliverableError:
        return None
    return deliverables.word_count(path)


def sentence(task: dict[str, Any], subs: list[dict[str, Any]] | None = None, word_count: int | None = None,
             today: date | None = None, *, with_title: bool = True) -> str:
    parts: list[str] = [str(task.get("title") or "")] if with_title else []
    due = due_label(task.get("due"), today)
    if due:
        parts.append(due)
    p = counted(subs)
    if p["total"]:
        parts.append(f"{p['done']}/{p['total']} {'节' if task.get('file') else '项'}")
    words = task.get("words")
    if words and task.get("file"):
        parts.append("找不到 Word 文件" if word_count is None else f"{word_count:,}/{int(words):,} 字")
    return " · ".join(x for x in parts if x)


def describe(task: dict[str, Any], subs: list[dict[str, Any]] | None = None, paths: Paths | None = None,
             today: date | None = None) -> dict[str, Any]:
    """Everything a view needs for one parent task: the sentence plus the numbers behind it."""
    wc = word_count_for(task, paths) if task.get("file") else None
    return {"sentence": sentence(task, subs, wc, today), "due_label": due_label(task.get("due"), today),
            "overdue": is_overdue(task, today), "progress": counted(subs), "word_count": wc, "words": task.get("words")}
