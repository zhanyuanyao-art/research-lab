"""Card 0143 (Leo 10-12: 「我没有删除 sub issue 的权限吗？」): Leo deletes a 待办-type issue with its sub-issues after a
confirmation, and Undo puts back exactly what was there. 课题 and outputs cannot be deleted here, an old page gets 409,
ids and keys are never reused, attachment files stay, and the assistant gains no delete."""
import pytest

from research_lab.files import issues as I, tasks as T, yamlio as Y
from research_lab.files.yamlio import StaleFileError

SLUG = "us-rok-reassurance"
JSON = {"Accept": "application/json", "HX-Request": "true"}


def new(title, parent=""):
    return I.create(title, SLUG, parent, I.revision(parent))


def delete(client, ref, revision=None, **extra):
    return client.post(f"/issues/{ref}/delete", data={"revision": revision or I.revision(ref), **extra}, headers=JSON)


def test_a_leaf_is_deleted_and_its_id_and_key_never_come_back(client, repo):
    I.sync_keys(create=True)
    ref = new("要删掉的待办")
    key = I.key_of(ref)
    r = delete(client, ref)
    assert r.status_code == 200 and r.json()["count"] == 0 and r.json()["undo"] == f"/issues/{ref}/restore"
    assert r.json()["url"] == "/issues?project=us-rok-reassurance"                      # back to the 课题
    assert I.get(ref) is None and T.get(ref) is None
    assert I._entry(I._metadata(), ref)["deleted"]                                      # the tombstone holds the undo
    later = new("之后新建的")
    assert later != ref and I.key_of(later) != key                                      # no id or key is reused
    assert I.resolve(key) == ref                                                        # nothing else was renumbered


def test_deleting_a_parent_takes_its_sub_issues_and_undo_restores_everything(client, repo, tmp_path):
    I.sync_keys(create=True)
    parent = new("第二章")
    kids = [new("第一节", parent), new("第二节", parent)]
    other = new("别的议题")
    I.update(kids[0], "labels", [next(iter(I.LABELS))], I.revision(kids[0]))
    I.update(other, "related", [kids[1]], I.revision(other))
    I.comment(parent, "导师说要压缩", I.revision(parent))
    I.attach_upload(parent, "笔记.pdf", b"%PDF-1.4", I.revision(parent))
    tasks_before = T.path().read_text()
    meta_before = {i: I._entry(I._metadata(), i) for i in [parent, *kids, other]}
    keys_before = {i: I.key_of(i) for i in [parent, *kids]}
    page = client.get(f"/issues/{parent}").text
    assert f'data-issue-context="{parent}"' in page and 'data-count="2"' in page            # the confirmation's count
    r = delete(client, parent)
    assert r.status_code == 200 and r.json()["count"] == 2 and set(r.json()["ids"]) == {parent, *kids}
    assert not any(T.get(i) for i in [parent, *kids]) and kids[1] not in I.get(other)["related"]
    assert (repo / "private" / SLUG / "笔记.pdf").exists()                              # attachment files stay
    back = client.post(f"/issues/{parent}/restore", data={"revision": I.revision(parent)}, headers=JSON)
    assert back.status_code == 200 and back.json()["url"] == f"/issues/{parent}"
    assert T.path().read_text() == tasks_before                                          # the entries, exactly
    assert {i: I._entry(I._metadata(), i) for i in [parent, *kids, other]} == meta_before  # comments, labels, links
    assert {i: I.key_of(i) for i in [parent, *kids]} == keys_before
    assert client.post(f"/issues/{parent}/restore", data={"revision": I.revision(parent)}, headers=JSON).status_code == 400   # nothing left to restore


def test_a_sub_issue_row_deletes_against_the_page_it_sits_on(client, repo):
    parent = new("父议题")
    child = new("子议题", parent)
    page = client.get(f"/issues/{parent}").text
    assert f'data-issue-context="{child}"' in page and 'data-title="子议题"' in page
    stale = delete(client, child, revision="old", page=parent)
    assert stale.status_code == 409 and T.get(child)
    r = delete(client, child, revision=I.revision(parent), page=parent)
    assert r.status_code == 200 and T.get(child) is None and T.get(parent)


def test_a_project_root_and_an_output_cannot_be_deleted_and_a_stale_page_writes_nothing(client, repo):
    root = I.project_ref(SLUG)
    assert delete(client, root).status_code == 400 and I.get(root)
    assert 'data-issue-context="PRJ-' not in client.get(I.url(root)).text
    output = next(r["id"] for r in I.read() if r["artifact"])
    assert delete(client, output).status_code == 400 and I.get(output)
    ref = new("别处改过的")
    old = I.revision(ref)
    I.update(ref, "status", "doing", old)
    meta = lambda: I.path().read_text() if I.path().exists() else ""   # noqa: E731
    before = (T.path().read_text(), meta())
    assert delete(client, ref, revision=old).status_code == 409
    assert (T.path().read_text(), meta()) == before


def test_the_assistant_gains_no_delete():
    from research_lab.agent import tools
    assert I.ASSISTANT_FIELDS == ('status', 'priority', 'due', 'labels', 'related', 'project', 'title', 'description', 'direction')
    assert I.ASSISTANT_CREATE == ('description', 'status', 'due', 'priority', 'labels')
    assert not [name for name in tools.TOOLS if "delete" in name or "remove" in name]


def test_a_row_delete_must_come_from_the_page_that_lists_it(client, repo):
    parent, other = new("父议题"), new("另一个")
    stranger = new("别处的子议题", other)
    r = delete(client, stranger, revision=I.revision(parent), page=parent)
    assert r.status_code == 400 and T.get(stranger)                                     # not on that page: nothing goes


def test_cmd_backspace_has_the_page_delete_to_open():
    """⌘⌫ (card 0147) clicks the page's own Delete, which opens the same confirmation."""
    from pathlib import Path
    import research_lab
    js = (Path(research_lab.__file__).parent / "web/static/issues.js").read_text()
    assert "e.key !== 'Backspace'" in js and "[data-issue-context][data-page-issue]" in js


def test_undo_requires_the_version_of_the_delete_offer(client, repo):
    ref = new("要撤销的")
    deleted = delete(client, ref).json()
    assert deleted["revision"] == I.revision(ref)
    before = (T.path().read_bytes(), I.path().read_bytes())
    assert client.post(deleted["undo"], headers=JSON).status_code == 409
    assert (T.path().read_bytes(), I.path().read_bytes()) == before
    new("之后新增的工作")
    before = (T.path().read_bytes(), I.path().read_bytes())
    response = client.post(deleted["undo"], data={"revision": deleted["revision"]}, headers=JSON)
    assert response.status_code == 409
    assert (T.path().read_bytes(), I.path().read_bytes()) == before


@pytest.mark.parametrize("stage", ["metadata", "task", "task_stale"])
def test_failed_undo_leaves_both_files_deleted_and_retryable(client, repo, monkeypatch, stage):
    ref = new("待恢复")
    child = new("一起恢复", ref)
    I.comment(child, "保留评论", I.revision(child))
    deleted = delete(client, ref).json()
    before = (T.path().read_bytes(), I.path().read_bytes())
    original = Y.os.replace
    def fail(src, dst):
        if dst == (I.path() if stage == "metadata" else T.path()):
            if stage == "task_stale":
                raise StaleFileError("synthetic concurrent edit")
            raise OSError("synthetic write failure")
        return original(src, dst)
    with monkeypatch.context() as patch:
        patch.setattr(Y.os, "replace", fail)
        result = client.post(deleted["undo"], data={"revision": deleted["revision"]}, headers=JSON)
    assert result.status_code == (409 if stage == "task_stale" else 400)
    assert (T.path().read_bytes(), I.path().read_bytes()) == before
    assert I.get(ref) is None and I.get(child) is None
    assert client.post(deleted["undo"], data={"revision": deleted["revision"]}, headers=JSON).status_code == 200
    assert I.get(ref) and I.get(child)


def test_context_actions_exist_in_all_todos_without_overflow_delete(client, repo):
    parent = new("行操作")
    new("子项", parent)
    page = client.get('/issues?view=todo').text
    assert f'data-issue-context="{parent}"' in page
    assert 'data-issue-context-menu' in page and 'data-issue-delete-dialog' in page
    assert 'data-revision="' + I.revision() in page
    detail = client.get(f'/issues/{parent}').text
    assert 'data-context-delete' in detail
    assert 'data-issue-delete=' not in detail and 'issue-row-menu' not in detail


def test_row_delete_keeps_the_page_version_during_membership_lookup(client, repo, monkeypatch):
    from research_lab.web.routes import issues as routes
    parent, ref = new("父议题"), None
    ref = new("范围内的子议题", parent)
    original = routes.listed_under
    def edited(page):
        I.comment(ref, "别处刚更新", I.revision(ref))
        return original(page)
    monkeypatch.setattr(routes, "listed_under", edited)
    response = delete(client, ref, page=parent, revision=I.revision(parent))
    assert response.status_code == 409 and I.get(ref)


def test_undo_offer_does_not_adopt_a_later_revision(client, repo, monkeypatch):
    ref = new("删除目标")
    other = new("稍后更新")
    original = I.delete
    own = []
    def edited(ref, expected):
        result = original(ref, expected)
        own.append(result["revision"])
        I.comment(other, "同时更新", I.revision(other))
        return result
    monkeypatch.setattr(I, "delete", edited)
    response = delete(client, ref)
    assert response.json()["revision"] == own[0] != I.revision(ref)
    assert client.post(response.json()["undo"], data={"revision": own[0]}, headers=JSON).status_code == 409


def test_undo_rollback_finishes_before_another_metadata_writer(client, repo, monkeypatch):
    import threading
    ref = new("并发撤销")
    deleted = delete(client, ref).json()
    before = (T.path().read_text(), I.path().read_text())
    started, finished = threading.Event(), threading.Event()
    errors = []
    def writer():
        started.set()
        try:
            Y.atomic_write(I.path(), before[1] + "# concurrent writer\n", Y.sha256_text(before[1]))
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()
    thread = threading.Thread(target=writer)
    original = Y.os.replace
    def fail(src, dst):
        if dst == T.path():
            thread.start()
            assert started.wait(2)
            assert not finished.wait(.05)
            raise OSError("synthetic second-file failure")
        return original(src, dst)
    with monkeypatch.context() as patch:
        patch.setattr(Y.os, 'replace', fail)
        response = client.post(deleted["undo"], data={"revision": deleted["revision"]}, headers=JSON)
    thread.join(3)
    assert not thread.is_alive() and not errors and response.status_code == 400
    assert T.path().read_text() == before[0]
    assert I.path().read_text() == before[1] + "# concurrent writer\n"
    assert I.get(ref) is None and I._entry(I._metadata(), ref)["deleted"]
