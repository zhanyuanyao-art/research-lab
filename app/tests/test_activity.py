"""The activity thread (card 0097, ISSUES-LINEAR §2.3, §2.6): comments in Markdown with the shared editor, Leo's
own comments editable and deletable, his property changes as grey events folded within a minute, an output's
记录 in the same thread, relative times worked out in the browser."""
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import research_lab
from research_lab.files import issues as I

STATIC = Path(research_lab.__file__).parent / "web" / "static"


def _issue():
    return I.create("活动流验收", "us-rok-reassurance", "", I.revision())


def _entries(ref):
    return list(I._entry(I._metadata(), ref).get("comments") or [])


def test_comments_are_markdown_with_an_id_and_the_shared_limit(repo):
    ref = _issue()
    I.comment(ref, "第一行\n**第二行**", I.revision(ref))
    c = _entries(ref)[-1]
    assert c["text"] == "第一行\n**第二行**" and str(c["id"]).startswith("c-")
    with pytest.raises(I.IssueError, match="4000"):
        I.comment(ref, "长" * 4001, I.revision(ref))
    item = I.comments(ref)[-1]
    assert item["kind"] == "comment" and item["own"] and item["source"] == "issue" and item["key"] == c["id"]


def test_leo_edits_and_deletes_only_his_own_comments(repo):
    ref = _issue()
    I.comment(ref, "原话", I.revision(ref))
    I.comment(ref, "助手的话", I.revision(ref), by="assistant")
    mine, theirs = (c["id"] for c in _entries(ref))
    I.edit_comment(ref, mine, "改过的话", I.revision(ref))
    assert _entries(ref)[0]["text"] == "改过的话" and _entries(ref)[0]["edited"]
    with pytest.raises(I.IssueError, match="自己的"):
        I.edit_comment(ref, theirs, "篡改", I.revision(ref))
    with pytest.raises(I.IssueError, match="自己的"):
        I.delete_comment(ref, theirs, I.revision(ref))
    I.delete_comment(ref, mine, I.revision(ref))
    assert [c["id"] for c in _entries(ref)] == [theirs]


def test_old_comments_without_an_id_are_named_by_their_time(repo):
    ref = _issue()
    I._save_meta(ref, {"comments": [{"by": "user", "text": "老评论", "at": "2026-10-01 09:00:00"}]}, I._meta_sha())
    assert I.comments(ref)[0]["key"] == "2026-10-01 09:00:00"
    I.edit_comment(ref, "2026-10-01 09:00:00", "老评论改过", I.revision(ref))
    assert _entries(ref)[0]["text"] == "老评论改过"


def test_a_stale_revision_refuses_the_edit(repo):
    ref = _issue()
    I.comment(ref, "原话", I.revision(ref))
    old = I.revision(ref)
    I.comment(ref, "又一条", old)
    from research_lab.files.yamlio import StaleFileError
    with pytest.raises(StaleFileError):
        I.edit_comment(ref, _entries(ref)[0]["id"], "改", old)


def test_leos_property_changes_become_one_grey_event_a_minute(repo):
    ref = _issue()
    first_priority = I.get(ref)["priority"]
    I.update(ref, "status", "doing", I.revision(ref), actor="user")
    events = [c for c in _entries(ref) if c.get("kind") == "event"]
    assert len(events) == 1 and "状态" in events[0]["text"] and "进行中" in events[0]["text"]
    # fold: a priority change within the minute joins the same line
    I.update(ref, "priority", "high", I.revision(ref), actor="user")
    events = [c for c in _entries(ref) if c.get("kind") == "event"]
    assert len(events) == 1 and "优先级" in events[0]["text"] and "状态" in events[0]["text"]
    # a field changed back drops out of the line
    I.update(ref, "priority", first_priority, I.revision(ref), actor="user")
    events = [c for c in _entries(ref) if c.get("kind") == "event"]
    assert len(events) == 1 and "优先级" not in events[0]["text"]
    item = I.comments(ref)[-1]
    assert item["kind"] == "event" and not item["own"]


def test_changes_more_than_a_minute_apart_are_separate_lines(repo):
    ref = _issue()
    I.update(ref, "status", "doing", I.revision(ref), actor="user")
    entries = _entries(ref)
    entries[-1]["at"] = (datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    I._save_meta(ref, {"comments": entries}, I._meta_sha())
    I.update(ref, "priority", "high", I.revision(ref), actor="user")
    assert len([c for c in _entries(ref) if c.get("kind") == "event"]) == 2


def test_a_change_back_within_the_minute_removes_the_line(repo):
    ref = _issue()
    I.update(ref, "status", "doing", I.revision(ref), actor="user")
    I.update(ref, "status", "todo", I.revision(ref), actor="user")
    assert not [c for c in _entries(ref) if c.get("kind") == "event"]


def test_no_event_without_actor_and_none_for_the_description(repo):
    ref = _issue()
    I.update(ref, "status", "doing", I.revision(ref))                          # the assistant's path writes its own line
    I.update(ref, "description", "新描述", I.revision(ref), actor="user")        # autosaved text is not an event
    assert not [c for c in _entries(ref) if c.get("kind") == "event"]


def test_the_revision_update_returns_covers_the_event(repo):
    ref = _issue()
    rev = I.update(ref, "status", "doing", I.revision(ref), actor="user")
    assert rev == I.revision(ref)                                                   # the page can keep saving


def test_events_do_not_stop_a_deleted_task_from_retiring(repo):
    ref = _issue()
    I.update(ref, "status", "doing", I.revision(ref), actor="user")
    assert I._untouched_meta(dict(I._entry(I._metadata(), ref)))


def test_the_issue_page_shows_the_thread(client, repo):
    ref = _issue()
    I.comment(ref, "看 **Lee 2024** 第三章", I.revision(ref))
    I.comment(ref, "我先整理文献", I.revision(ref), by="assistant")
    I.update(ref, "status", "doing", I.revision(ref), actor="user")
    page = client.get(f"/issues/{ref}").text
    thread = page.split('class="activity" data-thread', 1)[1].split("</ol>", 1)[0]
    assert "<strong>Lee 2024</strong>" in thread                                   # Markdown, shared renderer
    assert 'class="issue-avatar is-assistant"' in thread and '<span class="badge">助手</span>' in thread
    assert 'class="activity-event"' in thread and "<strong>Zhanyuan Yao</strong>" in thread
    assert "data-rel" in thread and 'datetime="' in thread
    form = page.split('class="issue-comment-form"', 1)[1].split("</form>", 1)[0]
    assert "data-rich" in form and "required" not in form


def test_an_output_page_uses_the_same_thread_and_editor(client, repo):
    from research_lab.files import artifacts as A
    ref = next(A.load(p).ref for p in A.list_paths())
    client.post(f"/artifacts/{ref}/updates", data={"text": "第一行\n- 列表"})
    page = client.get(f"/artifacts/{ref}").text
    thread = page.split('class="activity" data-thread', 1)[1].split("</ol>", 1)[0]
    assert "<li>列表</li>" in thread and "data-comment-edit=" in thread
    assert 'data-editor-assets="/static/vendor/tiptap-editor.min.js' in page and "vendor/issue-editor.css" in page
    uid = thread.split('data-comment-edit="/artifacts/' + ref + '/updates/', 1)[1].split("/edit", 1)[0]
    client.post(f"/artifacts/{ref}/updates/{uid}/edit", data={"text": "改过的"})
    assert "改过的" in client.get(f"/artifacts/{ref}").text and "已编辑" in client.get(f"/artifacts/{ref}").text
    client.post(f"/artifacts/{ref}/updates/{uid}/delete")
    assert "改过的" not in client.get(f"/artifacts/{ref}").text


def test_the_browser_side_wires_editor_times_and_actions():
    js = (STATIC / "issues.js").read_text()
    assert "function relativeTime" in js and "'刚刚'" in js and "' 分钟前'" in js and "'昨天 '" in js
    assert "data-comment-edit" in js and "data-comment-delete" in js and "Confirm" in js
    assert ".issue-comment-form textarea[data-rich], .tl-compose textarea[data-rich]" in js
    assert "data-editor-assets" not in js or "editorAssets" in js


def _post(client, url, **data):
    return client.post(url, data=data, headers={"Accept": "application/json", "HX-Request": "true"})


def test_editing_and_deleting_issue_comments_through_the_page(client, repo):
    ref = _issue()
    I.comment(ref, "原话", I.revision(ref))
    key = _entries(ref)[0]["id"]
    page = client.get(f"/issues/{ref}").text
    assert f'data-comment-edit="/issues/{ref}/comments/{key}/edit"' in page
    r = _post(client, f"/issues/{ref}/comments/{key}/edit", text="改过的**话**", revision=I.revision(ref))
    assert r.status_code == 200 and r.json()["revision"] == I.revision(ref)
    assert "改过的<strong>话</strong>" in client.get(f"/issues/{ref}").text
    stale = _post(client, f"/issues/{ref}/comments/{key}/delete", revision="old")
    assert stale.status_code == 409
    r = _post(client, f"/issues/{ref}/comments/{key}/delete", revision=I.revision(ref))
    assert r.status_code == 200 and not _entries(ref)


def test_the_assistants_comments_cannot_be_changed_from_the_page(client, repo):
    ref = _issue()
    I.comment(ref, "助手的话", I.revision(ref), by="assistant")
    key = _entries(ref)[0]["id"]
    assert "data-comment-edit" not in client.get(f"/issues/{ref}").text.split('class="activity"', 1)[1].split("</ol>", 1)[0]
    r = _post(client, f"/issues/{ref}/comments/{key}/edit", text="篡改", revision=I.revision(ref))
    assert r.status_code == 400 and _entries(ref)[0]["text"] == "助手的话"


def test_a_property_saved_from_the_page_leaves_a_line_and_a_usable_revision(client, repo):
    ref = _issue()
    r = _post(client, f"/issues/{ref}/property", field="title", value="改过的标题", revision=I.revision(ref))
    assert r.status_code == 200
    events = [c for c in _entries(ref) if c.get("kind") == "event"]
    assert len(events) == 1 and "改过的标题" in events[0]["text"]
    # the next autosave with the returned revision is accepted (tasks.yml and issues.yml both changed)
    again = _post(client, f"/issues/{ref}/property", field="description", value="接着写", revision=r.json()["revision"])
    assert again.status_code == 200


def test_two_old_comments_of_the_same_second_are_refused_not_mixed_up(repo):
    ref = _issue()
    same = "2026-10-01 09:00:00"
    I._save_meta(ref, {"comments": [{"by": "user", "text": "第一条", "at": same},
                                    {"by": "user", "text": "第二条", "at": same}]}, I._meta_sha())
    for act in (lambda: I.edit_comment(ref, same, "改", I.revision(ref)), lambda: I.delete_comment(ref, same, I.revision(ref))):
        with pytest.raises(I.IssueError, match="同一时间"):
            act()
    assert [c["text"] for c in _entries(ref)] == ["第一条", "第二条"]
