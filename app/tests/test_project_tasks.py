"""课题页的待办标签与概览一行；产出页加为待办 (docs/design/TASKS.md §C, card 0021)."""
from __future__ import annotations

from research_lab.files import tasks as T
from research_lab.files.weeks import week_of

SLUG = "asean-centrality"
REF = "ART-2026-4001"


def test_project_page_tasks_tab_and_overview_line(client, repo):
    ch, _ = T.add({"title": "第二章", "project": SLUG, "due": "2099-01-01", "words": 20000, "file": "第二章.docx"})
    T.add({"title": "一节", "project": SLUG, "parent": ch, "week": week_of()})
    T.add({"title": "读 Goh", "project": SLUG, "week": week_of()})
    T.add({"title": "别的课题的", "project": "us-rok-reassurance", "week": week_of()})
    html = client.get(f"/projects/{SLUG}").text
    assert 'data-panel="tasks">待办 <span class="count">3</span>' in html
    assert "待办 3 项，本周到期 2 项" in html and "0/1 节 · 找不到 Word 文件" in html    # overview line + parent sentence
    assert 'id="panel-tasks"' in html and 'id="task-board"' in html and f'data-id="{ch}" data-level="0"' in html
    assert "一节" in html and "读 Goh" in html and "别的课题的" not in html
    assert f'hx-post="/tasks/{ch}/sub"' in html and "tasks.js" in html and "pm.css" in html


def test_artifact_add_as_task(client, repo):
    page = client.get(f"/artifacts/{REF}").text
    assert "添加子任务" in page and f'name="source" value="{REF}"' in page
    r = client.post("/tasks", data={"title": "把《X》推到可入文", "project": SLUG, "source": REF, "note": "来自产出", "next": f"/artifacts/{REF}?saved=task"}, follow_redirects=False)
    assert r.status_code in (204, 303) and "saved=task" in r.headers.get("location", r.headers.get("hx-redirect", ""))
    rows = [t for t in T.read() if t["source"] == REF]
    assert len(rows) == 1 and rows[0]["project"] == SLUG and rows[0]["due"] is None    # no default schedule
    client.post("/tasks", data={"title": "把《X》推到可入文", "project": SLUG, "source": REF, "next": f"/artifacts/{REF}"}, follow_redirects=False)
    assert len([t for t in T.read() if t["source"] == REF]) == 1                      # an open duplicate is not added twice
    assert "已加为待办" in client.get(f"/artifacts/{REF}?saved=task").text
    assert "来自产出" in client.get(f"/projects/{SLUG}").text                            # source shown in words on the 课题 page
