"""The 待办 page as issues (docs/design/TASKS.md §C–D): blocks per 课题, nested sub-tasks, views,
inline add-sub-task form, folded closed items, no ids in copy, source labels."""
from __future__ import annotations

import re

from research_lab import i18n
from research_lab.files import tasks as T
from research_lab.files.weeks import week_of


def seed():
    parent, _ = T.add({"title": "第二章 周边环境的变化", "project": "asean-centrality", "due": "2026-12-31", "words": 20000, "file": "第二章.docx",
                       "origin": "assistant", "source": "AT-2026-0001"})
    a, _ = T.add({"title": "地区秩序变化", "project": "asean-centrality", "parent": parent, "week": week_of()})
    b, _ = T.add({"title": "周边环境演变态势", "project": "asean-centrality", "parent": parent, "due": "2026-12-31"})
    plain, _ = T.add({"title": "读完 Goh 2013", "project": "us-rok-reassurance", "week": week_of(), "source": "M3"})
    old, _ = T.add({"title": "已完成的事", "project": "us-rok-reassurance", "week": week_of()})
    T.set_status(old, "done")
    return parent, a, b, plain, old


def test_issue_view_structure(client, repo):
    parent, a, b, plain, old = seed()
    html = client.get("/tasks").text                                  # default view: this week
    assert 'data-view="week"' in html and 'class="task-block" data-project="asean-centrality"' in html
    assert f'data-id="{parent}" data-level="0"' in html and 'class="list-item task-row is-parent"' in html
    assert f'<ol class="list task-subs task-list" data-parent="{parent}">' in html and f'data-id="{a}" data-level="1"' in html
    assert f'hx-post="/tasks/{parent}/sub"' in html and 'placeholder="添加子任务…"' in html
    assert "0/2 节" in html                                             # parent progress with a Word file → 节
    assert "来自检查：" in html and "助手建立" in html                   # source labels, never the raw value
    assert "AT-2026-0001" not in re.sub(r"<[^>]+>", "", html)           # ids stay out of the page copy
    assert "已完成的事" not in html                                      # closed items are not in the week view
    allv = client.get("/tasks?view=all").text
    assert 'class="task-closed"' in allv and "已完成的事" in allv and 'aria-current="page"' in allv
    overdue = client.get("/tasks?view=overdue").text
    assert "没有逾期的待办" in overdue


def test_parent_row_has_no_carry_and_sub_add_posts(client, repo):
    parent, a, b, plain, old = seed()
    html = client.get("/tasks").text
    parent_row = html.split(f'data-id="{parent}"')[1].split("</li>")[0]
    assert "移到下周" not in parent_row and 'data-toggle' in parent_row
    r = client.post(f"/tasks/{parent}/sub", data={"title": "第三节", "view": "all"}, follow_redirects=False)
    assert r.status_code in (204, 303)
    assert [t["title"] for t in T.children(parent)][-1] == "第三节"
    page = client.get("/tasks?view=all").text
    assert "第三节" in page


def test_filter_by_project_and_empty_states(client, repo):
    assert "还没有待办" in client.get("/tasks?view=week&project=us-rok-reassurance").text   # nothing anywhere yet
    T.add({"title": "x", "project": "asean-centrality", "week": week_of()})
    assert "这个课题还没有待办" in client.get("/tasks?view=all&project=us-rok-reassurance").text
    assert "本周没有到期的待办" in client.get("/tasks?view=week&project=us-rok-reassurance").text
    assert "没有逾期的待办" in client.get("/tasks?view=overdue").text


def test_task_source_labels():
    assert i18n.task_source("M3").startswith("来自检查：")
    assert i18n.task_source("ART-2026-4001") == "来自产出"
    assert i18n.task_source("AT-2026-0003") == "助手建立"
    assert i18n.task_source("2026-W40") == "来自2026年 第40周的回顾"
    assert i18n.task_source("") == "" and i18n.task_source("whatever") == ""
