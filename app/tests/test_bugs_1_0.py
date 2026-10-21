"""v1.0.0 regressions left over from the /issues workspace (card 0054 review, card 0072).

Each test reproduces one reported bug: a broken file must degrade instead of 500, outputs are not
planned work, a deleted output's ref is never handed out again, the plan week does not touch the
status, a relation has one home, a folder made from a direction can be removed, and a stale save
shows the output that was being edited."""
from __future__ import annotations

import logging

import pytest

from research_lab import dashboard, paper_collections as C
from research_lab.files import artifacts as A, issues as I, tasks as T, updates
from research_lab.files.weeks import week_add, week_of
from research_lab.files.yamlio import StaleFileError

LANDSCAPE = "_artifacts/2026-W40-research-landscape.md"


def new_task(title="核对来源", project="us-iran-talks", parent=""):
    return I.create(title, project, parent, I.revision(parent))


def change(ref, field, value):
    I.update(ref, field, value, I.revision(ref))


def landscape_ref(repo):
    return A.load(repo / LANDSCAPE).ref


# ---------------------------------------------------------------- (a) a broken file degrades

def test_broken_output_file_is_skipped_and_logged(client, repo, caplog):
    (repo / "_artifacts" / "2026-W41-notes.md").write_text("just a note dropped into the folder\n", encoding="utf-8")
    (repo / "_artifacts" / "2026-W41-bad-yaml.md").write_text("---\ntitle: [unclosed\n---\nbody\n", encoding="utf-8")
    # a task hung under an output makes the home page look up output titles as well
    T.add({"title": "补一条证据", "project": "us-iran-talks", "source": landscape_ref(repo)})
    with caplog.at_level(logging.WARNING):
        assert client.get("/").status_code == 200
        assert client.get("/issues").status_code == 200
        assert client.get("/issues?direction=strategy-signaling").status_code == 200
        assert client.get("/artifacts").status_code == 200
    assert "2026-W41-notes.md" in caplog.text and "2026-W41-bad-yaml.md" in caplog.text
    refs = {r["id"] for r in I.read()}
    assert landscape_ref(repo) in refs and len([r for r in I.read() if r["artifact"]]) == len(list((repo / "_artifacts").glob("2026-W40-*.md")))


def test_broken_issues_yml_reads_empty_and_is_never_overwritten(client, repo):
    ref = new_task()
    p = repo / "_data" / "issues.yml"
    p.write_text("T-2026-0001: {labels: [\n", encoding="utf-8")
    before = p.read_bytes()
    assert client.get("/").status_code == 200
    assert client.get("/issues").status_code == 200
    assert client.get("/issues/" + ref).status_code == 200
    with pytest.raises(I.IssueError):
        I.comment(ref, "继续核对。", I.revision(ref))
    with pytest.raises(I.IssueError):
        change(landscape_ref(repo), "priority", "high")
    assert p.read_bytes() == before
    change(ref, "status", "doing")            # task fields live in tasks.yml and still save
    assert T.get(ref)["status"] == "doing"


def test_broken_tasks_yml_reads_empty_and_is_never_overwritten(client, repo):
    p = repo / "_data" / "tasks.yml"
    p.write_text("- id: T-2026-0001\n  title: [unclosed\n", encoding="utf-8")
    before = p.read_bytes()
    assert client.get("/").status_code == 200
    assert client.get("/tasks").status_code == 200
    assert client.get("/issues").status_code == 200
    with pytest.raises(T.TaskError):
        T.add({"title": "新待办", "project": "us-iran-talks"})
    assert p.read_bytes() == before
    p.write_text("not_a_list: true\n", encoding="utf-8")       # parses, but is not a task list
    with pytest.raises(T.TaskError):
        T.add({"title": "新待办", "project": "us-iran-talks"})
    assert p.read_text(encoding="utf-8") == "not_a_list: true\n"
    page = client.get("/tasks").text
    assert "_data/tasks.yml 读不出来" in page and "还没有待办" not in page
    p.write_text("- id: T-2026-0001\n  title: 旧待办\n  project: us-iran-talks\n  status: todo\n  week: 2026-W40\n"
                 "  carried: twice\n", encoding="utf-8")
    assert T.read()[0]["carried"] == 0 and client.get("/").status_code == 200
    assert "读不出来" not in client.get("/tasks").text
    T.carry("T-2026-0001", "2026-W45")                 # a hand-written count does not break moving it
    assert T.get("T-2026-0001")["carried"] == 1


def test_broken_updates_yml_reads_empty_and_is_never_overwritten(client, repo):
    ref = landscape_ref(repo)
    p = repo / "_data" / "updates.yml"
    for text in ("- id: U-2026-0001\n  text: [unclosed\n", "U-2026-0001: {target: ART-2026-4001, text: 旧记录}\n"):
        p.write_text(text, encoding="utf-8")
        assert client.get("/artifacts").status_code == 200
        assert client.get(f"/artifacts/{ref}").status_code == 200
        assert client.get(f"/issues/{ref}").status_code == 200
        with pytest.raises(updates.UpdateError):
            updates.add(ref, "user", "新记录")
        assert updates.record(ref, "系统记录") is None        # never fails the change it describes
        r = client.post(f"/artifacts/{ref}/updates", data={"text": "新记录"})
        assert r.status_code == 400 and "updates.yml" in r.text
        assert p.read_text(encoding="utf-8") == text


# ---------------------------------------------------------------- (b) outputs are not planned work

def test_new_output_is_not_this_weeks_todo(client, repo):
    current = week_of()
    ref = A.load(A.create("memo", "this-week-output", "us-iran-talks", title="本周写的备忘")).ref
    row = I.get(ref)
    assert row["due"] == "" and row["status"] == "todo"
    agenda = dashboard.weekly_agenda(current)
    assert not agenda["parents"] and not agenda["completed_parents"]
    groups, count = I.board(view="week")
    assert count == 0
    assert "本周写的备忘" not in client.get("/").text.split('id="home-tasks"', 1)[-1].split("</section>", 1)[0]
    change(ref, "due", T.this_friday())          # scheduling it explicitly still works (by due date, card 0107)
    assert I.get(ref)["due"] == T.this_friday() and I.board(view="week")[1] == 1


# ---------------------------------------------------------------- (c) refs are never reused

def _decorated_output(week):
    ref = A.load(A.create("memo", "to-delete", "us-iran-talks", week=week)).ref
    change(ref, "status", "done")
    change(ref, "labels", ["文献地图"])
    I.comment(ref, "旧产出的评论", I.revision(ref))
    updates.record(ref, "旧产出的记录")
    T.add({"title": "旧产出的待办", "project": "us-iran-talks", "source": ref})
    return ref


def _fresh(week, slug):
    ref = A.load(A.create("memo", slug, "us-iran-talks", week=week)).ref
    row = I.get(ref)
    assert row["status"] == "todo" and row["labels"] != ["文献地图"]
    assert not I.comments(ref) and not T.for_source(ref)
    return ref


def test_deleted_output_ref_is_not_handed_out_again(client, repo):
    week = "2026-W45"
    old = _decorated_output(week)
    r = client.post(f"/artifacts/{old}/delete", data={"sha": A.load(repo / "_artifacts" / f"{week}-to-delete.md").sha},
                    follow_redirects=False)
    assert r.status_code in (204, 303)
    assert not (repo / "_artifacts" / f"{week}-to-delete.md").exists()
    assert _fresh(week, "replacement") != old


def test_ref_stays_retired_after_the_trash_is_emptied(repo):
    week = "2026-W46"
    old = _decorated_output(week)
    path = repo / "_artifacts" / f"{week}-to-delete.md"
    path.unlink()                                   # removed by hand, not through the trash
    assert _fresh(week, "replacement") != old


# ---------------------------------------------------------------- (e) week and status are independent

def test_moving_a_parked_issue_keeps_it_parked(repo):
    ref = new_task("先放着的事")
    change(ref, "status", "parked")
    later = week_add(week_of(), 2)
    change(ref, "due", T.week_due(later))
    assert T.get(ref)["status"] == "parked" and T.get(ref)["due"] == T.week_due(later)
    parent = new_task("搁置的父议题")
    child = new_task("搁置的子议题", parent=parent)
    T.set_status(parent, "parked")                  # the 待办 page parks the whole group
    change(parent, "due", T.week_due(week_add(week_of(), 3)))
    assert T.get(parent)["status"] == "parked" and T.get(child)["status"] == "parked"


# ---------------------------------------------------------------- (d) one home per relation

def test_output_relations_live_in_front_matter_only(client, conn, repo):
    first = A.load(A.create("memo", "rel-first", "us-iran-talks")).ref
    second = A.load(A.create("memo", "rel-second", "us-iran-talks")).ref
    third = A.load(A.create("memo", "rel-third", "us-iran-talks")).ref
    task = new_task("关联的待办")
    path = {ref: next(p for p in (repo / "_artifacts").glob("*.md") if A.load(p).ref == ref) for ref in (first, second, third)}

    change(first, "related", [second, task])
    assert [str(x) for x in A.load(path[first]).front["related"]] == [second]
    assert I._metadata().get(first, {}).get("related", []) == [task]
    assert set(I.get(first)["related"]) == {second, task}
    assert first in I.get(second)["related"] and first in I.get(task)["related"]

    # the output page edits the same list: what it adds and removes shows in the issue view
    r = client.post(f"/artifacts/{first}/meta", data={"related": third, "sha": A.load(path[first]).sha},
                    follow_redirects=False)
    assert r.status_code in (200, 204, 303)
    assert set(I.get(first)["related"]) == {third, task}
    assert first not in I.get(second)["related"]

    # unticking an incoming link in the issue view removes it from the file that holds it
    change(third, "related", [])
    assert not A.load(path[first]).get("related") and I.get(first)["related"] == [task]


def test_moving_an_output_keeps_a_direction_the_user_chose(repo):
    # us-iran-talks sits under strategy-signaling, us-rok-reassurance under alliance-us
    inherited = A.create("memo", "follows-project", "us-iran-talks")
    chosen = A.create("memo", "own-direction", "us-iran-talks", direction="sea-asean")
    for path in (inherited, chosen):
        change(A.load(path).ref, "project", "us-rok-reassurance")
    assert A.load(inherited).get("direction") == "alliance-us"
    assert A.load(chosen).get("direction") == "sea-asean"
    assert A.load(chosen).get("project") == "us-rok-reassurance"


# ---------------------------------------------------------------- (f) folders made from directions

def _paper(conn, ref, direction=None):
    conn.execute("INSERT INTO papers(id,title,title_hash,kind,direction,created_at,updated_at) "
                 "VALUES (?,?,?,'article',?,'2026-10-02','2026-10-02')", (ref, ref, ref, direction))


def test_folder_can_be_deleted_without_touching_papers(client, conn):
    import re
    # startup copied every research direction into a folder of the same name
    ref = next(c["id"] for c in C.listing(conn) if c["name"] == "联盟政治与美国研究")
    _paper(conn, "paper-one", direction="alliance-us")
    C.assign(conn, ref, "paper-one")
    page = client.get("/papers?view=folders").text
    assert f'action="/paper-collections/{ref}/delete"' in page and f'form="delete-{ref}"' in page
    row = page.split(f'data-folder-id="{ref}"', 1)[1].split("</li>", 1)[0]
    assert re.findall(r'<form action="([^"]+)"', row)[0].endswith("/rename")   # papers.js renames via the first form
    stale = client.post(f"/paper-collections/{ref}/delete", data={"version": "0"}, follow_redirects=False)
    assert stale.headers["location"] == "/papers?view=folders&folder=stale"
    assert "集合在别处改过" in client.get(stale.headers["location"]).text   # 文件夹 is 集合 since card 0132
    assert client.post(f"/paper-collections/{ref}/delete", data={"version": "0"},
                       headers={"X-Library-Form": "1"}).json()["error"]
    r = client.post(f"/paper-collections/{ref}/delete", data={"version": "1"}, follow_redirects=False)
    assert r.status_code == 303 and "已删除集合" in client.get(r.headers["location"]).text
    assert C.get(conn, ref) is None
    assert conn.execute("SELECT direction FROM papers WHERE id='paper-one'").fetchone()[0] == "alliance-us"
    assert not conn.execute("SELECT 1 FROM paper_collection_items").fetchone()
    C.migrate_legacy(conn)                          # a restart does not bring it back
    assert C.get(conn, ref) is None and "联盟政治与美国研究" not in {c["name"] for c in C.listing(conn)}
    gone = client.post(f"/paper-collections/{ref}/delete", data={"version": "1"}, follow_redirects=False)
    assert gone.headers["location"] == "/papers?view=folders&folder=missing"


def test_legacy_folder_copy_skips_blank_direction_names(conn):
    from research_lab.db.connection import set_setting
    set_setting(conn, "library.flat_folders_v1", False)
    _paper(conn, "blank-direction", direction="   ")
    C.migrate_legacy(conn)                          # startup used to fail on a blank name
    assert all(c["name"].strip() for c in C.listing(conn))


def test_direction_used_only_by_an_unreadable_output_is_not_deleted(client, repo):
    from research_lab.files import datafiles as D
    D.add_direction({"slug": "maritime", "key": "MSEC", "name": "海洋安全"}, D.file_sha("directions"))
    (repo / "_artifacts" / "2026-W41-broken.md").write_text("---\ndirection: maritime\ntitle: [\n---\n", encoding="utf-8")
    r = client.post("/artifacts/directions/maritime/delete", data={"sha": D.file_sha("directions")})
    assert r.status_code == 400 and "2026-W41-broken.md" in r.text and D.direction("maritime")
    (repo / "_artifacts" / "2026-W41-broken.md").unlink()
    assert client.post("/artifacts/directions/maritime/delete", data={"sha": D.file_sha("directions")},
                       follow_redirects=False).status_code == 303


# ---------------------------------------------------------------- writes hand back the revision they produced

def test_update_and_create_return_the_revision_they_left(repo):
    parent = new_task("父议题")
    rev = I.update(parent, "status", "doing", I.revision(parent))
    assert rev == I.revision(parent)
    rev = I.update(parent, "labels", ["文献地图"], rev)               # the returned revision is good for the next save
    assert rev == I.revision(parent)
    out = landscape_ref(repo)
    for field, value in (("priority", "high"), ("title", "改过的标题"), ("related", [parent])):
        assert I.update(out, field, value, I.revision(out)) == I.revision(out)
    root = I.project_ref("us-iran-talks")
    assert I.update(root, "title", "美伊核谈判", I.revision(root)) == I.revision(root)
    child, rev = I.create("子议题", "", parent, I.revision(parent), with_revision=True)
    assert T.get(child)["parent"] == parent and rev == I.revision(parent)
    assert I.create("另一个", "us-iran-talks", "", I.revision("")).startswith("T-")
    stale = I.revision(parent)
    T.add({"title": "别处加的", "project": "us-iran-talks"})                # a write elsewhere after ours
    with pytest.raises(StaleFileError):
        I.update(parent, "status", "done", stale)


# ---------------------------------------------------------------- stale save on /week

def test_stale_save_shows_the_output_being_saved_not_this_weeks(client, repo):
    A.create("memo", "this-week", "us-iran-talks")       # this week has its own output
    path = repo / LANDSCAPE
    loaded = A.load(path)
    path.write_text(path.read_text() + "\nEdited elsewhere.\n", encoding="utf-8")
    r = client.post("/week/save", data={"ref": loaded.ref, "sha": loaded.sha, "text": loaded.text() + "\nMine.\n"})
    assert r.status_code == 409 and "磁盘上的当前版本" in r.text and "Mine." in r.text
    assert f'name="ref" value="{loaded.ref}"' in r.text
