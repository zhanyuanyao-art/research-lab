"""A resumable first research loop, driven by saved work rather than visited screens."""
from __future__ import annotations

import secrets
import sqlite3

from .db.connection import get_setting, set_setting
from .files import datafiles, issues, tasks
from .files.yamlio import StaleFileError

STEPS = ("建立课题", "放入文献", "对照原文", "留下下一步")


class CoachError(ValueError):
    pass


def _text(form, key, limit=200):
    value = str(form.get(key) or "").strip()
    if len(value) > limit:
        raise CoachError(f"内容过长，请控制在 {limit} 字以内。")
    return value


def state(conn: sqlite3.Connection) -> dict:
    project = datafiles.project(get_setting(conn, "coach.project", ""))
    papers = []
    if project:
        papers = [dict(r) for r in conn.execute(
            "SELECT p.id, p.title, p.read_depth FROM papers p JOIN paper_projects pp ON pp.paper_id=p.id "
            "WHERE pp.project_slug=? AND EXISTS (SELECT 1 FROM paper_files f WHERE f.paper_id=p.id) "
            "ORDER BY p.created_at DESC, p.id", (project["slug"],))]
    chosen = get_setting(conn, "coach.paper", "")
    paper = next((p for p in papers if p["id"] == chosen), papers[0] if papers else None)
    reviewed = bool(paper and get_setting(conn, "coach.reviewed", "") == paper["id"])
    task = tasks.get(get_setting(conn, "coach.task", ""))
    finished = bool(reviewed and task and project and task["project"] == project["slug"]
                    and get_setting(conn, "coach.task_paper", "") == paper["id"])
    done = [bool(project), bool(paper), reviewed, finished]
    step = next((i + 1 for i, value in enumerate(done) if not value), 5)
    return dict(project=project, paper=paper, papers=papers, task=task if finished else None,
                done=done, step=step, total=sum(done), finished=finished,
                active=get_setting(conn, "coach.active", False),
                hidden=get_setting(conn, "coach.hidden", False), steps=STEPS)


def start(conn):
    set_setting(conn, "coach.active", True)
    set_setting(conn, "coach.hidden", False)


def choose_project(conn, form):
    selected = _text(form, "project")
    if selected:
        if not datafiles.project(selected):
            raise CoachError("这个课题已不存在，请重新选择。")
    else:
        name = _text(form, "name")
        question = _text(form, "question", 1000)
        direction = _text(form, "direction")
        direction_name = _text(form, "direction_name", 80)
        if not name:
            raise CoachError("先给课题起一个名字。")
        if direction and not datafiles.direction(direction):
            raise CoachError("这个研究方向已不存在，请重新选择。")
        if not direction and not direction_name:
            raise CoachError("写一个研究方向，或选择已有方向。")
        # Check both revisions before either write; a stale form must not leave a new direction.
        for key in ("directions", "projects"):
            if _text(form, key + "_sha") != datafiles.file_sha(key):
                raise StaleFileError("工作区已在别处更新，请确认后重试。")
        if not direction:
            same = next((d for d in datafiles.read("directions") or [] if d["name"] == direction_name), None)
            direction = same["slug"] if same else "direction-" + secrets.token_hex(4)
            if not same:
                datafiles.add_direction({"slug": direction, "name": direction_name,
                                         "key": _text(form, "direction_key", 5)}, form["directions_sha"])
        selected = "project-" + secrets.token_hex(4)
        datafiles.add_project({"slug": selected, "name": name, "question": question or name,
                               "direction": direction, "stage": "idea", "type": "课题"}, form["projects_sha"])
    if get_setting(conn, "coach.project", "") != selected:
        for key in ("paper", "reviewed", "task", "task_paper"):
            set_setting(conn, "coach." + key, "")
    set_setting(conn, "coach.project", selected)
    start(conn)


def require_project(conn, form):
    current = state(conn)
    if not current["project"] or _text(form, "project") != current["project"]["slug"]:
        raise CoachError("课题已切换，请刷新后继续。")
    return current


def choose_paper(conn, form):
    require_project(conn, form)
    paper_id = _text(form, "paper")
    if not conn.execute("SELECT 1 FROM papers p WHERE p.id=? AND EXISTS "
                        "(SELECT 1 FROM paper_files f WHERE f.paper_id=p.id)", (paper_id,)).fetchone():
        raise CoachError("请选择一篇已导入 PDF 的文献。")
    conn.execute("INSERT OR IGNORE INTO paper_projects(paper_id, project_slug) VALUES (?,?)",
                 (paper_id, form["project"]))
    if get_setting(conn, "coach.paper", "") != paper_id:
        set_setting(conn, "coach.reviewed", "")
        set_setting(conn, "coach.task", "")
        set_setting(conn, "coach.task_paper", "")
    set_setting(conn, "coach.paper", paper_id)


def review(conn, form):
    current = require_project(conn, form)
    if not current["paper"] or _text(form, "paper") != current["paper"]["id"]:
        raise CoachError("文献已变动，请刷新后继续。")
    # This is a user's acknowledgement, never evidence verification or AI adoption.
    set_setting(conn, "coach.reviewed", current["paper"]["id"])


def next_step(conn, form):
    current = require_project(conn, form)
    if current["finished"]:
        return  # Browser retries must not create a second task.
    if not current["done"][2] or _text(form, "paper") != current["paper"]["id"]:
        raise CoachError("先对照原文，再记录下一步。")
    title, note, due = _text(form, "title"), _text(form, "note", 4000), _text(form, "due", 10)
    if not title:
        raise CoachError("写下一件准备做的事。")
    project = current["project"]["slug"]
    ref = issues.create(title, project, "", _text(form, "revision"), due=due,
                        description=(note + "\n\n" if note else "") + f"[阅读文献](/papers/{current['paper']['id']})")
    set_setting(conn, "coach.task", ref)
    set_setting(conn, "coach.task_paper", current["paper"]["id"])
