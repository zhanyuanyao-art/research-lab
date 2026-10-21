"""Owner labels stay local to a workspace without changing assignment authority."""
import pytest

from research_lab.files import issues as I


def test_old_workspaces_keep_legacy_names(repo):
    assert I.assignees()[''] == 'Zhanyuan Yao'
    assert [I.display('assignee', value) for value in I.OWNER_VALUES] == ['Zhanyuan Yao'] * 3


@pytest.mark.parametrize('profile, label', [
    ('owner_name: 我\n', '我'),
    ('owner_name: Alex Chen\n', 'Alex Chen'),
    ('owner_name: [\n', '我'),
    (b'owner_name: \xff\xfe', '我'),
    ('owner_name: "<script>"\n', '我'),
    ('owner_name: "' + 'A' * 81 + '"\n', '我'),
    ('- invalid schema\n', '我'),
])
def test_profile_labels_are_plain_and_preserve_stored_aliases(repo, profile, label):
    (repo / '_data/profile.yml').write_bytes(profile if isinstance(profile, bytes) else profile.encode())
    assert I.assignees()[''] == label
    assert all(I.display('assignee', value) == label for value in I.OWNER_VALUES)
    assert I._check_assignee('Leo') == 'Leo'
    assert I.display('assignee', 'theo') == 'Theo'


def test_owner_label_in_menu_and_assistant_handover(client, repo, conn, monkeypatch):
    from research_lab.agent import tasks
    (repo / '_data/profile.yml').write_text('owner_name: 我\n')
    monkeypatch.setattr(tasks.RUNNER, 'submit', lambda task_id: None)
    ref = I.create('核对结论', 'us-rok-reassurance', '', I.revision())
    page = client.get(f'/issues/{ref}').text
    assert '分配人：我' in page and '改回 我 就停下' in page
    assert 'Zhanyuan Yao' not in page
    assert 'class="issue-avatar" aria-hidden="true">我</span>' in page
    I.update(ref, 'assignee', 'theo', I.revision(ref), actor='user')
    tasks.sync_handovers(conn)
    plan = tasks._plan_of(conn, ref)
    tasks._finish(conn, plan['id'], 'done', report='先核对原文。')
    assert I.get(ref)['assignee'] == 'theo'
    assert tasks.handover_state(conn, ref) == 'draft'
    assert any('改回 我' in c['text'] for c in I.comments(ref))
    _, run = tasks.start_handover(conn, plan['id'])
    tasks._finish(conn, run, 'done', report='已核对，等待你的判断。')
    assert I.get(ref)['assignee'] == ''
    assert any('分配人回到 我' in row['text'] for row in conn.execute(
        "SELECT text FROM agent_task_events WHERE task_id=?", (plan['id'],)))


def test_unreadable_profile_falls_back_to_self(repo):
    (repo / '_data/profile.yml').mkdir()
    assert I.owner_name() == '我'


@pytest.mark.parametrize("name, initial", [("我", "我"), ("Alex Chen", "A")])
def test_activity_and_subissue_composer_use_workspace_owner(client, repo, name, initial):
    import re
    (repo / "_data/profile.yml").write_text(f"owner_name: {name}\n")
    ref = I.create("核验一个问题", "us-rok-reassurance", "", I.revision())
    I.comment(ref, "自己的判断", I.revision(ref))
    I.comment(ref, "助手的建议", I.revision(ref), by="assistant")
    I.update(ref, "status", "doing", I.revision(ref), actor="user")
    page = client.get(f"/issues/{ref}").text
    thread = page.split('class="activity" data-thread', 1)[1].split('</ol>', 1)[0]
    own, assistant, event = thread.split('</li>')[:3]
    assert f"<strong>{name}</strong>" in own and f"<strong>{name}</strong>" in event
    assert "<strong>Leo</strong>" not in thread
    assert "data-comment-edit" in own and "data-comment-edit" not in assistant
    avatars = re.findall(r'<span class="issue-avatar" aria-hidden="true">(.*?)</span>', page)
    assert len(avatars) >= 3 and set(avatars) == {initial}
    # A template cache must not retain a previous workspace's owner.
    (repo / "_data/profile.yml").write_text("owner_name: Another\n")
    changed = client.get(f"/issues/{ref}").text
    thread = changed.split('class="activity" data-thread', 1)[1].split('</ol>', 1)[0]
    assert "<strong>Another</strong>" in thread and f"<strong>{name}</strong>" not in thread
    assert all(c["by"] in ("user", "assistant") for c in I.comments(ref))
