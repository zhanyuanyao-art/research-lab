"""Card 0006: 加为待办 from a failing milestone check (with a safe `next`), and the assistant's
/tasks and /plan commands with their cards — all answered from _data/tasks.yml, no model."""
from __future__ import annotations

import re
from datetime import date

import pytest

from research_lab.files import tasks as T
from research_lab.files.weeks import week_add, week_of


@pytest.fixture()
def db(client):
    from research_lab.db.connection import connect
    c = connect()
    yield c
    c.close()


def visible(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def send(client, text: str, page: str = "/chat", thread_id: str = ""):
    return client.post("/chat/send", data={"text": text, "page": page, "thread_id": thread_id}, follow_redirects=False)


def thread_of(resp) -> str:
    tid = resp.headers.get("X-Chat-Thread") or resp.headers["HX-Redirect"].rsplit("/", 1)[-1]
    assert re.fullmatch(r"T-\d{4}-\d{4}", tid)
    return tid


def msgs(db, tid):
    from research_lab.chat import service
    return service.messages(db, tid)


def _redirect(r) -> str:
    assert r.status_code in (204, 303), r.status_code
    return r.headers.get("location") or r.headers.get("HX-Redirect")


# ---------------------------------------------------------------- 课题 page: 加为待办

def test_ladder_add_task_creates_once_and_returns_safely(client, repo):
    page = client.get("/projects/asean-centrality").text
    assert "Add task" in page and 'name="source" value="M1"' in page
    assert 'name="next" value="/projects/asean-centrality"' in page
    form = re.search(r'<form class="inline add-task"[^>]*>(.*?)</form>', page, re.S).group(1)
    data = dict(re.findall(r'name="([a-z]+)" value="([^"]*)"', form))
    assert set(data) == {"title", "project", "source", "note", "next"} and data["note"] == "摸清研究版图"

    inline = {"HX-Target": "add-task-M1-1"}                    # htmx sends it: the form has an id
    r = client.post("/tasks", data=data, headers=inline)
    assert r.status_code == 200 and "已加入待办" in r.text and "T-2026" not in visible(r.text)
    rows = T.read()
    assert len(rows) == 1 and rows[0]["source"] == "M1" and rows[0]["title"] == data["title"]
    assert rows[0]["project"] == "asean-centrality" and rows[0]["due"] is None and rows[0]["note"] == "摸清研究版图"

    again = client.post("/tasks", data=data, headers=inline)   # one open task per check, however often clicked
    assert again.status_code == 200 and "已在待办中" in again.text and len(T.read()) == 1

    # without scripts the plain post comes back to the 课题 page; a done task no longer blocks a new one
    T.set_status(rows[0]["id"], "done")
    assert _redirect(client.post("/tasks", data=data)) == "/projects/asean-centrality"
    assert len(T.read()) == 2
    for bad in ("https://evil.example/x", "//evil.example/x", "/\\evil.example", "javascript:alert(1)"):
        T.set_status(T.read()[-1]["id"], "done")
        assert _redirect(client.post("/tasks", data={**data, "next": bad})) == "/tasks?saved=created"


# ---------------------------------------------------------------- 助手: /tasks

def _seed_tasks():
    T.add({"title": "读完 Ba 第三章", "project": "asean-centrality", "week": week_add(week_of(), -1)})
    T.add({"title": "整理主席规则证据", "project": "asean-centrality", "source": "M2", "due": date.today().isoformat()})
    T.add({"title": "US-ROK 修改稿回复审稿人", "project": "us-rok-reassurance", "due": date.today().isoformat()})
    T.add({"title": "下周再看的书", "project": "asean-centrality", "week": week_add(week_of(), 1)})


def test_tasks_command_and_card(client, db):
    tid = thread_of(send(client, "/tasks"))
    assert "还没有待办" in msgs(db, tid)[1].content and msgs(db, tid)[1].card_kind is None

    _seed_tasks()
    tid = thread_of(send(client, "/tasks"))
    m = msgs(db, tid)[1]
    assert m.card_kind == "tasks" and m.card_ref == "all"
    assert "本周到期 2 条" in m.content and "逾期 1 条" in m.content and "以后 1 条" in m.content
    page = client.get(f"/chat/{tid}").text
    assert "读完 Ba 第三章" in page and "整理主席规则证据" in page and "US-ROK 修改稿回复审稿人" in page
    assert "改到本周五" in page and "搁置" in page and "来自「提出谜题」" in page and "打开待办" in page
    assert "T-2026" not in visible(page) and "M2" not in visible(page)
    assert 'hx-post="/tasks/' in page and 'hx-swap="none ignoreTitle:true"' in page

    tid = thread_of(send(client, "/tasks us-rok"))
    m = msgs(db, tid)[1]
    assert m.card_ref == "us-rok-reassurance" and "本周到期 1 条" in m.content
    page = client.get(f"/chat/{tid}").text
    assert "US-ROK 修改稿回复审稿人" in page and "读完 Ba 第三章" not in page and "/tasks?project=us-rok-reassurance" in page

    tid = thread_of(send(client, "/tasks nope"))
    assert "找不到课题" in msgs(db, tid)[1].content


# ---------------------------------------------------------------- 助手: /plan

def test_plan_command_and_card(client, db, repo):
    _seed_tasks()
    tid = thread_of(send(client, "/plan"))
    m = msgs(db, tid)[1]
    assert m.card_kind == "plan" and m.card_ref == week_of()
    assert "已逾期 1 条" in m.content and "本周到期 2 条" in m.content and "Produce the W40" in m.content
    assert "上周剩下" not in m.content and "本周已排" not in m.content            # no plan week (card 0107)
    page = client.get(f"/chat/{tid}").text
    assert "已过截止" in page and "读完 Ba 第三章" in page and "改到本周五" in page and "搁置" in page
    assert 'name="this_week"' in page and 'hx-post="/week/now"' in page and "定下本周计划" in page
    from research_lab.files import datafiles
    assert f'value="{datafiles.file_sha("now")}"' in page           # the plan card carries now.yml's sha
    assert "T-2026" not in visible(page)
    # the card's buttons hit the same routes the 待办 page uses
    tid_task = T.read()[0]["id"]
    r = client.post(f"/tasks/{tid_task}/carry", data={"week": week_of()}, headers={"HX-Target": f"card-{tid_task}-carry"}, follow_redirects=False)
    assert r.status_code == 303 and T.get(tid_task)["week"] == week_of()
    assert "已过截止" not in client.get(f"/chat/{tid}").text  # re-rendered from the file: nothing overdue now


def test_natural_phrasings_route_to_tasks_and_plan():
    from research_lab.chat.router import cmd_plan, cmd_tasks, match
    assert match("待办")[0] is cmd_tasks and match("本周待办？")[0] is cmd_tasks and match("tasks")[0] is cmd_tasks
    assert match("/todo asean") == (cmd_tasks, "asean")
    assert match("定本周计划")[0] is cmd_plan and match("/plan")[0] is cmd_plan
    assert match("帮我把待办整理成表格") is None


def test_tasks_card_and_tool_show_parents_as_issues(client, db):
    """TASKS.md §C/§E: the assistant shows a parent once, as its progress sentence, with this week's
    sub-tasks under it; list_tasks gives the model the same tree."""
    from datetime import date, timedelta
    from research_lab.agent import tools
    p, wk = "asean-centrality", week_of()
    due = (date.today() + timedelta(days=10)).isoformat()
    ch, _ = T.add({"title": "第二章", "project": p, "due": due})
    T.add({"title": "一节", "project": p, "parent": ch, "week": wk})
    T.add({"title": "读 Goh", "project": p, "week": wk})
    tid = thread_of(send(client, "待办"))
    assert "1 个父任务在进行" in msgs(db, tid)[-1].content and "本周到期 1 条" in msgs(db, tid)[-1].content
    page = visible(client.get(f"/chat/{tid}").text)
    assert "第二章 · 距截止 10 天 · 0/1 项" in page and "一节" in page and "读 Goh" in page
    text, err = tools.call(db, "list_tasks", {}, 6000)
    assert not err and "第二章 · 距截止 10 天 · 0/1 项" in text and "\n  - " in text and "读 Goh" in text
