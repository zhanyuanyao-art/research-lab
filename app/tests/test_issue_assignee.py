"""Card 0101 (ISSUES-LINEAR.md §7, Leo's 10-10 decision): 分配人 is Zhanyuan Yao by default, or an assistant
from agent/skills.py ASSISTANTS; it is shown and chosen, and it starts nothing."""
from research_lab.agent.skills import ASSISTANTS
from research_lab.files import issues as I, tasks as T

JSON = {'Accept': 'application/json'}
SLUG = 'us-rok-reassurance'


def test_the_menu_lists_leo_by_name_and_the_assistants(client, repo):
    ref = I.create('分配验收', SLUG, '', I.revision())
    assert I.assignees() == {'': 'Zhanyuan Yao', **{k: a.name for k, a in ASSISTANTS.items()}}
    page = client.get(f'/issues/{ref}').text
    menu = page.split('<aside class="issue-properties"', 1)[1].split('data-prop="due"', 1)[0]
    assert 'data-prop="assignee"' in menu and '<kbd>A</kbd>' in menu and '分配人：Zhanyuan Yao' in menu
    assert all(f'value="{k}"' in menu and a.name in menu for k, a in ASSISTANTS.items())
    assert '你点「开始」才动手' in menu


def test_assigning_to_theo_shows_on_the_row_and_only_opens_a_plan(client, repo, conn, monkeypatch):
    from research_lab.agent import tasks
    monkeypatch.setattr(tasks.RUNNER, "submit", lambda task_id: None)
    parent = I.create('父议题', SLUG, '', I.revision())
    child = I.create('交给 Theo 的', SLUG, parent, I.revision(parent))
    r = client.post(f'/issues/{child}/property', headers=JSON, data={'field': 'assignee', 'value': 'theo', 'revision': I.revision(child)})
    assert r.status_code == 200 and T.get(child)['assignee'] == 'theo'
    rows = client.get(f'/issues/{parent}').text.split('class="issue-subtree"', 1)[1].split('data-sub-composer', 1)[0]
    assert 'class="issue-avatar is-assistant" title="分配给 Theo"' in rows and rows.count('issue-avatar') == 1   # Leo shows no avatar
    (plan,) = conn.execute("SELECT * FROM agent_tasks").fetchall()      # card 0116: a plan step, nothing runs before 开始
    assert plan["status"] == "queued" and tasks.handover_state(conn, child) == "planning"
    assert I.display('assignee', 'theo') == 'Theo' and I.display('assignee', '') == I.display('assignee', 'Leo') == 'Zhanyuan Yao'
    bad = client.post(f'/issues/{child}/property', headers=JSON, data={'field': 'assignee', 'value': '随便谁', 'revision': I.revision(child)})
    assert bad.status_code == 400
    out = I.change_fields(child, [('assignee', 'theo', '')])                            # the assistant cannot change it
    assert [f['field'] for f in out['failed']] == ['assignee'] and T.get(child)['assignee'] == 'theo'


def test_the_composer_offers_the_assignee_and_creates_with_it(client, repo, conn, monkeypatch):
    from research_lab.agent import tasks
    monkeypatch.setattr(tasks.RUNNER, "submit", lambda task_id: None)
    parent = I.create('父议题', SLUG, '', I.revision())
    form = client.get(f'/issues/{parent}').text.split('data-sub-composer', 1)[1].split('</form>', 1)[0]
    assert 'name="assignee" value=""' in form and 'data-choice="theo"' in form and 'Zhanyuan Yao' in form
    r = client.post('/issues', headers=JSON, data={'title': '交给 Theo 准备', 'parent': parent, 'project': SLUG,
                                                    'revision': I.revision(parent), 'assignee': 'theo'})
    assert r.status_code == 200 and T.get(r.json()['id'])['assignee'] == 'theo'
    assert tasks.handover_state(conn, r.json()['id']) == 'planning'          # card 0116: created for Theo, planned at once
