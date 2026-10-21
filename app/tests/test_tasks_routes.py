"""/tasks routes for sub-tasks: add under a parent, reorder, edit the due date, and the carry rule."""
from __future__ import annotations

from datetime import date

from research_lab.files import tasks as T
from research_lab.files.weeks import week_of

P = "asean-centrality"


def _ok(r):
    assert r.status_code in (204, 303), r.text[:300]


def test_sub_task_move_and_due_through_the_page(client, repo):
    ch, _ = T.add({"title": "第二章", "project": P, "due": "2030-12-20"})
    _ok(client.post(f"/tasks/{ch}/sub", data={"title": "一、地区秩序"}, follow_redirects=False))
    _ok(client.post(f"/tasks/{ch}/sub", data={"title": "二、周边环境", "due": "2030-12-05"}, follow_redirects=False))
    a, b = (t["id"] for t in T.children(ch))
    assert T.get(a)["parent"] == ch and T.get(a)["project"] == P
    assert T.get(a)["due"] is None and T.get(b)["due"] == "2030-12-05"   # a section has its own due date, or none
    assert T.get(ch)["due"] == "2030-12-20"                          # the parent keeps its own

    _ok(client.post(f"/tasks/{b}/move", data={"before": a}, follow_redirects=False))
    assert [t["id"] for t in T.children(ch)] == [b, a]
    _ok(client.post(f"/tasks/{a}/edit", data={"due": "2030-11-30"}, follow_redirects=False))
    assert T.get(a)["due"] == "2030-11-30"
    _ok(client.post(f"/tasks/{a}/edit", data={"due": ""}, follow_redirects=False))
    assert T.get(a)["due"] is None

    _ok(client.post(f"/tasks/{ch}/carry", data={"week": "2030-W52"}, follow_redirects=False))   # an old board's week
    assert T.get(ch)["due"] == "2030-12-27" and T.get(b)["due"] == "2030-12-05"   # a parent moves on its own now
    r = client.post(f"/tasks/{a}/sub", data={"title": "孙任务"}, follow_redirects=False)
    assert r.status_code == 400 and "不能再建子任务" in r.text
    assert client.post("/tasks/T-2030-9999/sub", data={"title": "x"}, follow_redirects=False).status_code == 404

    for view in ("week", "overdue", "all", "bogus"):
        assert client.get(f"/tasks?view={view}").status_code == 200


def test_artifact_page_adds_a_task_under_the_output(client, repo):
    import re
    from research_lab.db.connection import connect
    from research_lab.files import artifacts
    c = connect()
    artifacts.index(c)
    row = c.execute("SELECT ref, project FROM artifacts WHERE COALESCE(project, '') != '' ORDER BY ref LIMIT 1").fetchone()
    c.close()
    page = client.get(f"/artifacts/{row['ref']}").text
    assert f'name="source" value="{row["ref"]}"' in page and "推到可入文" not in page
    r = client.post("/tasks", data={"title": "补一个反例", "project": row["project"], "source": row["ref"],
                                    "next": f"/artifacts/{row['ref']}?saved=task"}, follow_redirects=False)
    _ok(r)
    [t] = T.for_source(row["ref"])
    assert t["title"] == "补一个反例" and t["parent"] is None
    assert "挂在这份产出下面" in client.get(f"/artifacts/{row['ref']}?saved=task").text
    assert f'href="/artifacts/{row["ref"]}">来自产出《' in client.get("/tasks?view=all").text
