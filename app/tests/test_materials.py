"""交付任务 from Leo's own materials: upload → plan (agent tool) → Leo edits and confirms → records."""
from __future__ import annotations

import re
import shutil
import zipfile

import pytest

NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
SLUG = "chapter2-order"


def _docx(path, paras: list[tuple[str | None, str]]) -> None:
    body = "".join((f'<w:p><w:pPr><w:pStyle w:val="{s}"/></w:pPr>' if s else "<w:p>")
                   + f'<w:r><w:t xml:space="preserve">{t}</w:t></w:r></w:p>' for s, t in paras)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{NS}"><w:body>{body}</w:body></w:document>')
        z.writestr("word/styles.xml", f'<w:styles xmlns:w="{NS}"><w:style w:styleId="1"><w:name w:val="heading 1"/></w:style></w:styles>')


@pytest.fixture()
def db(client):
    from research_lab.db.connection import connect
    c = connect()
    yield c
    c.close()


def _upload(client, path, name, text=""):
    with open(path, "rb") as fh:
        r = client.post("/chat/send", data={"text": text, "page": "/chat", "thread_id": ""},
                        files=[("files", (name, fh, "application/octet-stream"))], follow_redirects=False)
    return r.headers.get("X-Chat-Thread") or r.headers["HX-Redirect"].rsplit("/", 1)[-1]


def test_sections_split_at_headings():
    from research_lab.files.materials import sections
    secs = sections("前言一段\n\n# 一、地区秩序\n\n甲段\n\n乙段\n\n## 小节\n\n丙段\n")
    assert [(s.index, s.heading, s.level, s.paragraphs) for s in secs] == [
        (1, "开头", 0, ("前言一段",)), (2, "一、地区秩序", 1, ("甲段", "乙段")), (3, "小节", 2, ("丙段",))]
    assert sections("没有标题\n\n第二段")[0].heading == "全文"


def test_upload_plan_confirm_creates_deliverable_and_tasks(client, db, repo, tmp_path):
    from research_lab.agent import tasks as agent_tasks, tools
    from research_lab.chat import service
    from research_lab.files import datafiles, materials, tasks
    from research_lab.files.weeks import week_of
    from datetime import date

    f = tmp_path / "ch2.docx"
    _docx(f, [("1", "一、地区秩序变化"), (None, "地区秩序正在从单极走向多极。"), ("1", "二、周边环境"), (None, "周边环境呈现三种态势。")])
    tid = _upload(client, f, "第二章材料.docx")
    reply = [m.content for m in service.messages(db, tid)][-1]
    assert "已收到材料 **第二章材料.docx**：2 节" in reply and "交付任务" in reply
    [mat] = materials.for_thread(tid)
    assert db.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 0     # a draft is not a paper

    task = agent_tasks.create(db, "第二章报告，提纲如下", thread_id=tid, skill="project-setup")
    plan = {"project": {"slug": SLUG, "name": "第二章报告", "direction": "sea-asean", "question": "模型编的问题"},
            "deliverable": {"title": "第二章 国际秩序转型背景下周边环境的变化", "due": "2030-12-20", "words": 20000,
                            "file": mat["id"], "body": "模型写的正文"},             # not part of the plan: dropped
            "sections": [{"title": "1. 国际秩序转型背景下的地区秩序变化", "due": "2030-11-30"},
                         {"title": "2. 周边环境的演变态势"}]}
    text, err = tools.call(db, "propose_project_setup", plan, 9000, task_id=task)
    assert not err and f"(/chat/setup/{task})" in text and "Nothing is written" in text
    assert datafiles.project(SLUG) is None and not tasks.read()               # nothing before Leo confirms
    stored = materials.load_plan(task)["plan"]
    assert "body" not in stored["deliverable"] and stored["sections"][1]["due"] == "2030-12-20"   # spread to the end

    page = client.get(f"/chat/setup/{task}").text
    assert ">Create<" in page and 'value="2030-12-20"' in page and 'value="20000"' in page and "第二章材料.docx" in page

    r = client.post(f"/chat/setup/{task}/apply", data={"title": "第二章 国际秩序转型背景下周边环境的变化", "due": "2030-12-20",
                                                        "words": "25000", "question": "",
                                                        "section_0_title": "1. 国际秩序转型背景下的地区秩序变化",
                                                        "section_0_due": "2030-11-30", "section_1_title": "2. 周边环境的演变态势",
                                                        "section_1_due": "2030-12-15"}, follow_redirects=False)
    assert r.status_code == 303
    assert datafiles.project(SLUG)["question"] == materials.DEFAULT_QUESTION   # Leo cleared the model's question
    [parent] = [t for t in tasks.for_project(SLUG) if not t["parent"]]
    assert (parent["title"], parent["due"], parent["words"], parent["file"]) == (
        "第二章 国际秩序转型背景下周边环境的变化", "2030-12-20", 25000, "第二章材料.docx")
    subs = tasks.children(parent["id"])
    assert [(t["title"], t["due"]) for t in subs] == [("1. 国际秩序转型背景下的地区秩序变化", "2030-11-30"),
                                                     ("2. 周边环境的演变态势", "2030-12-15")]
    assert "week" not in tasks.path().read_text()                     # sections are scheduled by their due dates
    assert {t["source"] for t in [parent, *subs]} == {task} and {t["origin"] for t in subs} == {"assistant"}
    assert (repo / "private" / SLUG / "第二章材料.docx").exists()
    assert db.execute("SELECT COUNT(*) FROM artifacts WHERE project = ?", (SLUG,)).fetchone()[0] == 0   # written in Word
    assert any("已建立交付任务" in m.content and f"/projects/{SLUG}" in m.content for m in service.messages(db, tid))

    client.post(f"/chat/setup/{task}/apply", follow_redirects=False)           # confirming twice adds nothing
    assert len(tasks.for_project(SLUG)) == 3
    assert "已建立" in client.get(f"/chat/setup/{task}").text
    text, err = tools.call(db, "propose_project_setup", plan, 9000, task_id=task)
    assert err and "already carried out" in text


@pytest.mark.skipif(shutil.which("textutil") is None, reason="macOS textutil")
def test_without_a_word_draft_the_app_starts_one_from_the_outline(client, db, repo, tmp_path):
    from research_lab.agent import tasks as agent_tasks, tools
    from research_lab.files import convert, tasks
    note = tmp_path / "n.md"
    note.write_text("# 想法\n\n一段笔记\n", encoding="utf-8")
    tid = _upload(client, note, "笔记.md")
    task = agent_tasks.create(db, "交付任务", thread_id=tid, skill="project-setup")
    _, err = tools.call(db, "propose_project_setup", {
        "project": {"slug": "report-x", "name": "报告", "direction": "sea-asean"},
        "deliverable": {"title": "课题报告"}, "sections": [{"title": "一、背景"}, {"title": "二、态势"}]}, 9000, task_id=task)
    assert not err
    assert client.post(f"/chat/setup/{task}/apply", follow_redirects=False).status_code == 303
    [parent] = [t for t in tasks.for_project("report-x") if not t["parent"]]
    assert parent["file"] == "课题报告.docx" and parent["due"] is None
    assert [t["due"] for t in tasks.children(parent["id"])] == [None, None]
    text = convert.to_markdown(repo / "private" / "report-x" / "课题报告.docx")
    assert "课题报告" in text and "一、背景" in text and "二、态势" in text
    assert (repo / "private" / "report-x" / "笔记.md").exists()


def test_plan_guardrails(client, db, repo, tmp_path):
    from research_lab.agent import tasks as agent_tasks, tools
    from research_lab.files import datafiles, materials, tasks
    note = tmp_path / "n.md"
    note.write_text("# 甲\n\n一段\n", encoding="utf-8")
    t1, t2 = _upload(client, note, "a.md", "这是材料"), _upload(client, note, "b.md")   # page /chat: a new thread each
    mine, other = materials.for_thread(t1)[0]["id"], materials.for_thread(t2)[0]["id"]
    task = agent_tasks.create(db, "交付任务", thread_id=t1, skill="project-setup")

    def propose(project=None, **dl):
        plan = {"project": project or {"slug": "x-new", "name": "X", "direction": "sea-asean"},
                "deliverable": {"title": "报告", **{k: v for k, v in dl.items() if k != "materials"}},
                "sections": [{"title": "一"}]}
        if "materials" in dl:
            plan["materials"] = dl["materials"]
        return tools.call(db, "propose_project_setup", plan, 9000, task_id=task)

    assert "direction must be one of" in propose({"slug": "x-new", "name": "X", "direction": "nope"})[0]
    assert "lowercase" in propose({"slug": "第二章", "name": "X", "direction": "sea-asean"})[0]
    assert "YYYY-MM-DD" in propose(due="12月20日")[0]
    assert "whole number" in propose(words=5)[0]
    assert "unknown material" in propose(materials=[other])[0]                     # another conversation's file
    assert ".docx" in propose(file=mine)[0]                                         # a Markdown note is not the Word draft
    # an existing project is reused as it is: no name or direction needed, projects.yml does not change
    existing = (datafiles.read("projects") or [])[0]
    before = (repo / "_data/projects.yml").read_text(encoding="utf-8")
    text, err = propose({"slug": existing["slug"]}, due="2030-01-31")
    assert not err and "existing project" in text
    r = client.post(f"/chat/setup/{task}/apply", data={"due": "2030-02-30"}, follow_redirects=False)
    assert r.status_code == 400 and "YYYY-MM-DD" in r.text                         # Leo's bad edit is refused, not written
    assert not tasks.read()
    assert client.post(f"/chat/setup/{task}/apply", data={"due": "2030-02-28"}, follow_redirects=False).status_code == 303
    assert (repo / "_data/projects.yml").read_text(encoding="utf-8") == before
    assert [t["due"] for t in tasks.for_project(existing["slug"]) if not t["parent"]] == ["2030-02-28"]


def test_spread_puts_the_last_section_on_the_due_date():
    from datetime import date
    from research_lab.files.materials import spread
    assert spread("2030-01-31", 3, today=date(2030, 1, 1)) == ["2030-01-11", "2030-01-21", "2030-01-31"]
    assert spread(None, 2) == [None, None] and spread("2029-12-01", 1, today=date(2030, 1, 1)) == ["2030-01-01"]


def test_tools_are_scoped_to_the_conversation(client, db, tmp_path):
    from research_lab.agent import tasks, tools
    note = tmp_path / "n.md"
    note.write_text("# 甲\n\n" + "长段落。" * 10 + "\n\n第二段\n", encoding="utf-8")
    tid = _upload(client, note, "笔记.md")
    task = tasks.create(db, "按提纲建课题", thread_id=tid, skill="project-setup")
    listing, _ = tools.call(db, "list_materials", {}, 9000, task_id=task)
    mid = re.search(r"material (MAT-[0-9a-f]{8})", listing).group(1)
    outline, _ = tools.call(db, "read_material", {"material_id": mid}, 9000, task_id=task)
    assert "§1 # 甲 · 2 paragraphs" in outline
    sec, _ = tools.call(db, "read_material", {"material_id": mid, "section": 1}, 9000, task_id=task)
    assert "¶1 长段落。" in sec and "¶2 第二段" in sec
    stranger = tasks.create(db, "别的对话", thread_id=None, skill="project-setup")
    text, err = tools.call(db, "read_material", {"material_id": mid}, 9000, task_id=stranger)
    assert err and "no material" in text


def test_routing_to_the_setup_skill():
    from research_lab.agent import skills, tools
    assert skills.pick("我要写第二章，提纲：1. 文献综述 2. 案例时间线").name == "project-setup"
    assert skills.pick("帮我把这些材料整理成课题").name == "project-setup"
    assert skills.pick("导师布置了一个课题报告，下个月交").name == "project-setup"
    assert skills.pick("做一份联盟管理的文献综述").name == "literature-map"
    s = skills.get("project-setup")
    assert set(s.tools) <= set(tools.TOOLS) and "propose_project_setup" in s.tools
    assert not {"save_research_draft", "run_analysis", "queue_background"} & set(s.tools)


def test_a_retry_after_a_failed_step_does_not_duplicate(client, db, repo, tmp_path, monkeypatch):
    from research_lab.agent import tasks as agent_tasks, tools
    from research_lab.files import materials, tasks
    f = tmp_path / "d.docx"
    _docx(f, [("1", "一"), (None, "段落")])
    tid = _upload(client, f, "旧稿.docx")
    [mat] = materials.for_thread(tid)
    task = agent_tasks.create(db, "交付任务", thread_id=tid, skill="project-setup")
    tools.call(db, "propose_project_setup", {"project": {"slug": "retry-x", "name": "报告", "direction": "sea-asean"},
               "deliverable": {"title": "报告", "due": "2030-06-30", "file": mat["id"]},
               "sections": [{"title": "一"}, {"title": "二"}]}, 9000, task_id=task)
    real_add, calls = tasks.add, []

    def flaky(values, *a, **k):
        calls.append(values["title"])
        if len(calls) == 2:
            raise OSError("disk full")
        return real_add(values, *a, **k)
    monkeypatch.setattr(tasks, "add", flaky)
    assert client.post(f"/chat/setup/{task}/apply", follow_redirects=False).status_code == 400
    assert [t["title"] for t in tasks.for_project("retry-x")] == ["报告"]      # the parent made it, the 2nd add failed
    monkeypatch.setattr(tasks, "add", real_add)
    assert client.post(f"/chat/setup/{task}/apply", follow_redirects=False).status_code == 303
    assert [(t["title"], bool(t["parent"])) for t in tasks.for_project("retry-x")] == [("报告", False), ("一", True), ("二", True)]
    assert sorted(p.name for p in (repo / "private" / "retry-x").iterdir()) == ["旧稿.docx"]


# ---------------------------------------------------------------- card 0022: the confirm page's choices

def _upload_into(client, tid, path, name, text=""):
    """A second file into the same conversation (the fragment response carries the thread id)."""
    with open(path, "rb") as fh:
        r = client.post("/chat/send", data={"text": text, "page": f"/chat/{tid}", "thread_id": tid},
                        files=[("files", (name, fh, "application/octet-stream"))], follow_redirects=False)
    assert r.headers.get("X-Chat-Thread") == tid


def _plan_for(db, tid, slug, title, sections, **dl):
    from research_lab.agent import tasks as agent_tasks, tools
    task = agent_tasks.create(db, "交付任务", thread_id=tid, skill="project-setup")
    text, err = tools.call(db, "propose_project_setup", {
        "project": {"slug": slug, "name": "第二章报告", "direction": "sea-asean"},
        "deliverable": {"title": title, **dl}, "sections": [{"title": t} for t in sections]}, 9000, task_id=task)
    assert not err, text
    return task


def test_reorder_keeps_edits_with_their_sections():
    from research_lab.files.materials import reorder, with_edits
    secs = [{"title": "一", "due": None}, {"title": "二", "due": None}, {"title": "三", "due": None}]
    assert [s["title"] for s in reorder(secs, "2,0,1")] == ["三", "一", "二"]
    assert [s["title"] for s in reorder(secs, "1")] == ["二", "一", "三"]            # the rest keep their order
    assert [s["title"] for s in reorder(secs, "9,x,1,1")] == ["二", "一", "三"]      # junk and repeats ignored
    plan = {"project": {"existing": True}, "deliverable": {"title": "t", "due": None, "words": None, "file": None}, "sections": secs}
    out = with_edits(plan, {"section_1_title": "二改", "section_order": "1,0,2", "file": "new", "merge": "new"})
    assert [s["title"] for s in out["sections"]] == ["二改", "一", "三"]            # fields are by rendered index, then reordered
    assert out["deliverable"]["file"] == "" and out["merge"] == "new"


def test_confirm_page_reorders_sections_and_picks_the_word_source(client, db, repo, tmp_path):
    from research_lab.files import materials, tasks
    a, b = tmp_path / "a.docx", tmp_path / "b.docx"
    _docx(a, [("1", "旧稿"), (None, "一段")])
    _docx(b, [("1", "新稿"), (None, "另一段")])
    tid = _upload(client, a, "旧稿.docx")
    _upload_into(client, tid, b, "新稿.docx")
    by_name = {m["name"]: m for m in materials.for_thread(tid)}      # same-second uploads sort arbitrarily
    ma, mb = by_name["旧稿.docx"], by_name["新稿.docx"]
    task = _plan_for(db, tid, SLUG, "第二章", ["一", "二", "三"], file=ma["id"])

    page = client.get(f"/chat/setup/{task}").text
    assert 'name="section_order"' in page and page.count('data-move="up"') == 3 and "上移" in page
    assert page.count('name="file"') == 3 and f'value="{ma["id"]}" checked' in page and "空白 docx" in page
    assert "已有同名交付稿" not in page and "chat.js" in page

    bad = client.post(f"/chat/setup/{task}/apply", data={"file": "MAT-00000000"}, follow_redirects=False)
    assert bad.status_code == 400 and "deliverable.file" in bad.text and not tasks.read()

    r = client.post(f"/chat/setup/{task}/apply", data={"section_order": "2,0,1", "section_1_title": "二改", "file": mb["id"]},
                    follow_redirects=False)
    assert r.status_code == 303
    [parent] = [t for t in tasks.for_project(SLUG) if not t["parent"]]
    assert parent["file"] == "新稿.docx"
    assert [t["title"] for t in tasks.children(parent["id"])] == ["三", "一", "二改"]
    assert sorted(p.name for p in (repo / "private" / SLUG).iterdir()) == ["新稿.docx", "旧稿.docx"]


@pytest.mark.skipif(shutil.which("textutil") is None, reason="macOS textutil")
def test_choosing_a_blank_docx_over_the_uploaded_draft(client, db, repo, tmp_path):
    from research_lab.files import materials, tasks
    a = tmp_path / "a.docx"
    _docx(a, [("1", "旧稿"), (None, "一段")])
    tid = _upload(client, a, "旧稿.docx")
    [ma] = materials.for_thread(tid)
    task = _plan_for(db, tid, SLUG, "第二章", ["一"], file=ma["id"])
    assert client.post(f"/chat/setup/{task}/apply", data={"file": materials.NEW_FILE}, follow_redirects=False).status_code == 303
    [parent] = [t for t in tasks.for_project(SLUG) if not t["parent"]]
    assert parent["file"] == "第二章.docx" and (repo / "private" / SLUG / "第二章.docx").exists()
    assert (repo / "private" / SLUG / "旧稿.docx").exists()                      # still copied as material


def test_same_title_deliverable_merges_by_default(client, db, repo, tmp_path):
    from research_lab.chat import service
    from research_lab.files import materials, tasks
    a = tmp_path / "a.docx"
    _docx(a, [("1", "稿"), (None, "一段")])
    tid1 = _upload(client, a, "稿.docx")
    [m1] = materials.for_thread(tid1)
    first = _plan_for(db, tid1, SLUG, "第二章", ["一", "二"], file=m1["id"], due="2030-12-20", words=20000)
    assert client.post(f"/chat/setup/{first}/apply", follow_redirects=False).status_code == 303
    [parent] = [t for t in tasks.for_project(SLUG) if not t["parent"]]
    assert materials.same_title_parent(SLUG, "第二章")["id"] == parent["id"]
    assert materials.same_title_parent(SLUG, "第三章") is None

    # another day, another conversation: the same chapter again, with one more section
    tid2 = _upload(client, a, "稿.docx")
    second = _plan_for(db, tid2, SLUG, "第二章", ["一", "三"])
    page = client.get(f"/chat/setup/{second}").text
    assert "已有同名交付稿" in page and 'name="merge"' in page and 'value="merge" checked' in page and "已有 2 节" in page
    assert client.post(f"/chat/setup/{second}/apply", follow_redirects=False).status_code == 303
    parents = [t for t in tasks.for_project(SLUG) if not t["parent"]]
    assert [t["id"] for t in parents] == [parent["id"]]                                  # no second parent
    assert [t["title"] for t in tasks.children(parent["id"])] == ["一", "二", "三"]       # only the missing section
    assert tasks.get(parent["id"])["due"] == "2030-12-20" and tasks.get(parent["id"])["words"] == 20000
    result = materials.load_plan(second)["result"]
    assert result["merged"] and result["added"] == 1 and result["parent"] == parent["id"]
    page = client.get(f"/chat/setup/{second}").text
    assert "已合并进已有的交付稿" in page and "原有" in page
    assert any("合并进已有的交付稿" in m.content and "补上了 1 个子任务" in m.content for m in service.messages(db, tid2))
    assert len(list((repo / "private" / SLUG).iterdir())) == 1                           # identical bytes: no second copy

    # Leo can still ask for a separate one
    tid3 = _upload(client, a, "稿.docx")
    third = _plan_for(db, tid3, SLUG, "第二章", ["一"])
    assert client.post(f"/chat/setup/{third}/apply", data={"merge": materials.MERGE_NEW}, follow_redirects=False).status_code == 303
    assert len([t for t in tasks.for_project(SLUG) if not t["parent"]]) == 2


def test_merging_into_a_hand_made_deliverable_keeps_sources_consistent(client, db, repo, tmp_path):
    """A 交付稿 Leo set up by hand has no source; sections merged into it carry none either (a sub-task's
    source always matches its parent's)."""
    from research_lab.agent import tasks as agent_tasks, tools
    from research_lab.files import tasks
    p = "asean-centrality"
    ch, _ = tasks.add({"title": "第二章", "project": p, "file": "第二章.docx", "due": "2030-12-20"})
    tasks.add({"title": "一节", "project": p, "parent": ch})
    note = tmp_path / "n.md"
    note.write_text("# 甲\n\n一段\n", encoding="utf-8")
    tid = _upload(client, note, "n.md")
    task = agent_tasks.create(db, "交付任务", thread_id=tid, skill="project-setup")
    _, err = tools.call(db, "propose_project_setup", {"project": {"slug": p}, "deliverable": {"title": "第二章"},
                        "sections": [{"title": "一节"}, {"title": "二节"}]}, 9000, task_id=task)
    assert not err
    assert client.post(f"/chat/setup/{task}/apply", follow_redirects=False).status_code == 303
    subs = tasks.children(ch)
    assert [t["title"] for t in subs] == ["一节", "二节"] and {t["source"] for t in subs} == {""}

