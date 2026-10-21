"""The issue workspace must not migrate research content on read or split task state."""
import pytest
import re
from datetime import date

from research_lab.files import issues as I, artifacts as A, tasks as T
from research_lab.files.weeks import week_of, week_add
from research_lab.files.yamlio import StaleFileError


def new(title='测试议题', project='us-rok-reassurance', parent=''):
    return I.create(title, project, parent, I.revision(parent))


def change(ref, field, value):
    I.update(ref, field, value, I.revision(ref))


def flatten(groups):
    def walk(nodes):
        for node in nodes:
            yield node['issue']['id']
            yield from walk(node['children'])
    return list(walk([n for g in groups for n in g['items']]))


def test_reads_preserve_original_files(client, repo):
    before = {p: p.read_bytes() for p in (repo / '_artifacts').glob('*.md')}
    tasks = (repo / '_data/tasks.yml').read_bytes()
    response = client.get('/issues?direction=strategy-signaling')
    assert response.status_code == 200
    ref = next(r['id'] for r in I.read() if r['artifact'])
    assert client.get('/issues/' + ref).status_code == 200
    assert not I.path().exists()
    assert all(p.read_bytes() == data for p, data in before.items())
    assert (repo / '_data/tasks.yml').read_bytes() == tasks


def test_parent_child_share_task_identity_and_due(client, repo):
    parent = new()
    child = new('本周核对事件', parent=parent)
    change(child, 'due', T.week_due(week_add(week_of(), 1)))                 # an older caller's week: next Friday
    assert T.get(child)['due'] == T.friday(week_add(week_of(), 1)) and T.get(child)['postponed'] == 0
    change(child, 'due', T.sunday(week_add(week_of(), 1)))
    assert T.get(child)['postponed'] == 1
    assert T.get(parent)['due'] is None                            # a parent is not derived from its sub-issues
    assert len(T.read()) == 2
    groups, count = I.board(direction='alliance-us', view='next')
    assert child in flatten(groups) and parent in flatten(groups)
    assert client.get('/issues/' + child).status_code == 200


def test_completed_parent_keeps_open_child_visible(repo):
    parent = new()
    child = new('仍要做的子议题', parent=parent)
    change(child, 'due', date.today().isoformat())
    change(parent, 'status', 'done')
    assert T.get(child)['status'] == 'todo'
    groups, _ = I.board(view='week')
    assert parent in flatten(groups) and child in flatten(groups)
    old_tree = T.tree(T.read(), 'week', week_of())
    assert old_tree[0]['items'][0]['task']['id'] == parent
    assert old_tree[0]['items'][0]['subs'][0]['task']['id'] == child


def test_old_document_current_child_and_independent_due(repo):
    path = A.create('evidence', 'old-issue', 'us-iran-talks', week=week_add(week_of(), -1))
    ref = A.load(path).ref
    before = path.read_bytes()
    child = new('本周推进旧资料', parent=ref)
    change(child, 'due', T.week_due(week_of()))
    change(ref, 'due', '2026-12-01')
    change(child, 'status', 'done')
    assert I.get(ref)['due'] == '2026-12-01'
    change(child, 'status', 'todo')
    groups, _ = I.board(view='week')
    assert ref in flatten(groups) and child in flatten(groups)
    assert path.read_bytes() == before  # no AI notes, earned sections or maturity were changed


def test_all_properties_persist_without_changing_research_maturity(repo):
    ref = next(r['id'] for r in I.read() if r['artifact'])
    doc = I._documents()[ref]
    before = doc.path.read_bytes()
    for field, value in [('status', 'doing'), ('priority', 'high'), ('assignee', 'Leo'),
                         ('labels', ['文献地图', '案例时间轴']), ('description', '先核对三个关键事件。'),
                         ('due', '2027-01-15')]:
        change(ref, field, value)
        assert I.get(ref)[field] == value
    assert doc.path.read_bytes() == before


def test_stale_edits_and_invalid_values_do_not_write(repo):
    ref = new()
    stale = I.revision(ref)
    change(ref, 'assignee', 'Leo')
    with pytest.raises(StaleFileError):
        I.update(ref, 'status', 'done', stale)
    assert T.get(ref)['status'] == 'todo'
    before = T.path().read_bytes()
    for field, value in [('due', '2026-02-30'), ('week', '2026-W42'), ('status', 'paper-ready'),
                         ('labels', ['伪造标签']), ('related', [ref]), ('Claim', '不能写')]:
        with pytest.raises(I.IssueError):
            change(ref, field, value)
    assert T.path().read_bytes() == before
    assert not I.path().exists()


def test_related_and_comments_survive_reopen(repo, client):
    first, second = new('甲'), new('乙')
    change(first, 'related', [second])
    assert first in I.get(second)['related']
    change(second, 'related', [])
    assert not I.get(first)['related']
    I.comment(first, '继续核对来源。', I.revision(first))
    assert I.comments(first)[0]['text'] == '继续核对来源。'
    assert '继续核对来源。' in client.get('/issues/' + first).text


def test_new_form_direction_and_user_post(client, repo):
    response = client.get('/issues/new?direction=strategy-signaling')
    assert response.status_code == 200
    assert 'name="project" value="institutional-reassurance"' in response.text
    assert 'data-choice="us-iran-talks"' in response.text
    assert 'data-choice="us-rok-reassurance"' not in response.text
    form = {'title': '用户明确创建的议题', 'project': 'us-iran-talks', 'parent': '', 'revision': I.revision()}
    response = client.post('/issues', data=form)
    assert response.status_code == 200
    ref = T.read()[0]['id']
    assert response.url.path == '/issues/' + ref
    child_form = dict(form, title='用户创建的子议题', parent=ref, revision=I.revision(ref))
    response = client.post('/issues', data=child_form)
    assert response.url.path == '/issues/' + ref
    assert '用户创建的子议题' in response.text
    # Untrusted markup is shown as text in descriptions and comments.
    change(ref, 'description', '<img src=x onerror=alert(1)>')
    html = client.get('/issues/' + ref).text
    assert '<img src=x onerror=alert(1)>' not in html
    assert '&lt;img' in html


def test_filters_and_missing_ref(client, repo):
    ref = new('检索联盟政治文献')
    change(ref, 'labels', ['文献地图'])
    groups, count = I.board(direction='alliance-us', label='文献地图', query='联盟')
    assert count == 1 and flatten(groups) == [ref]
    assert client.get('/issues/not-an-issue').status_code == 404
    assert client.get('/issues?direction=not-a-direction').status_code == 404


def test_change_between_validation_and_write_is_not_overwritten(repo, monkeypatch):
    ref = new()
    original = T.update
    def racing_update(task_id, values, expected, **kwargs):
        original(task_id, {'note': '另一个窗口刚保存的内容'}, T.file_sha())
        return original(task_id, values, expected, **kwargs)
    monkeypatch.setattr(T, 'update', racing_update)
    with pytest.raises(StaleFileError):
        change(ref, 'description', '不能覆盖别人的新内容')
    assert T.get(ref)['note'] == '另一个窗口刚保存的内容'


def test_issue_status_does_not_cascade_to_children(repo):
    parent = new()
    child = new('保留独立状态', parent=parent)
    parked = new('暂时搁置的子议题', parent=parent)
    change(parked, 'status', 'parked')
    change(parent, 'status', 'parked')
    assert I.get(child)['status'] == 'todo'
    change(parent, 'status', 'doing')
    assert I.get(parked)['status'] == 'parked'
    change(parent, 'status', 'parked')
    change(parent, 'due', T.week_due(week_add(week_of(), 1)))
    assert I.get(parked)['status'] == 'parked'


def test_global_new_issue_keeps_legacy_material_context(client, repo):
    html = client.get('/projects/us-iran-talks').text
    assert 'href="/issues/new?direction=strategy-signaling&project=us-iran-talks"' in html
    path = A.create('evidence', 'context', 'disaster-management')
    html = client.get('/artifacts/' + A.load(path).ref).text
    assert 'href="/issues/new?direction=periphery-nts&project=disaster-management"' in html


def test_form_never_pairs_old_description_with_new_revision(client, repo, monkeypatch):
    ref = new()
    change(ref, 'description', '旧描述')
    original = I.get
    def racing_get(target):
        row = original(target)
        if target == ref:
            T.update(ref, {'note': '另一个窗口的新描述'}, T.file_sha())
        return row
    with monkeypatch.context() as m:
        m.setattr(I, 'get', racing_get)
        html = client.get('/issues/' + ref).text
    version = re.search(r'name="revision" value="([^"]+)"', html).group(1)
    response = client.post('/issues/' + ref + '/property', data={
        'field': 'description', 'value': '旧描述加一句', 'revision': version})
    assert response.status_code == 409
    assert T.get(ref)['note'] == '另一个窗口的新描述'


def test_legacy_incoming_related_link_is_selected_and_preserved(client, repo):
    first = A.load(A.create('memo', 'related-first', 'us-iran-talks'))
    second = A.load(A.create('memo', 'related-second', 'us-iran-talks'))
    first.front['related'] = [second.ref]
    A.save(first)
    third = new('新关联')
    html = client.get('/issues/' + second.ref).text
    assert f'value="{first.ref}" checked' in html
    change(second.ref, 'related', [first.ref, third])
    assert first.ref in I.get(second.ref)['related']
    assert second.ref in I.get(first.ref)['related']


def test_archive_overview_precedes_project_workflow(client, repo):
    ref = new('只有课题内才出现的议题')
    other = new('其他课题的议题', project='us-iran-talks')
    html = client.get('/issues').text
    assert 'class="project-row"' in html and 'issue-filter-bar' not in html
    assert 'href="/issues?project=us-rok-reassurance"' in html
    assert 'href="/issues?project=us-iran-talks"' in html
    assert '只有课题内才出现的议题' not in html
    filtered = client.get('/issues?direction=alliance-us').text
    assert 'href="/issues?project=us-rok-reassurance"' in filtered
    assert 'href="/issues?project=us-iran-talks"' not in filtered
    page = client.get('/issues?project=us-rok-reassurance')
    assert page.status_code == 200 and '只有课题内才出现的议题' in page.text
    assert f'href="/issues/{other}"' not in page.text
    assert 'name="project" value="us-rok-reassurance"' in page.text
    assert 'href="/issues?project=us-rok-reassurance"' in client.get('/issues/' + ref).text
    assert client.get('/issues?project=missing').status_code == 404
    assert client.get('/issues?direction=strategy-signaling&project=us-rok-reassurance').status_code == 404


def test_project_keeps_all_children_when_old_filter_links_are_opened(client, repo):
    parent = new('研究父议题')
    child = new('本周子议题', parent=parent)
    future = new('下周子议题', parent=parent)
    change(parent, 'status', 'done')
    change(child, 'status', 'doing')
    change(future, 'due', T.week_due(week_add(week_of(), 1)))
    html = client.get('/issues?project=us-rok-reassurance&view=week&state=doing').text
    assert f'href="/issues/{parent}"' in html and f'href="/issues/{child}"' in html
    assert f'href="/issues/{future}"' in html
    assert 'data-issue-filters' not in html
    assert 'project=us-rok-reassurance' in html


def test_unclassified_projects_and_legacy_directions_remain_reachable(client, repo):
    from research_lab.files import datafiles
    datafiles.update_project('us-rok-reassurance', {'direction': ''})
    ref = new('尚未归类的研究')
    root = client.get('/issues').text
    assert '未分类' in root and 'href="/issues?project=us-rok-reassurance"' in root
    assert '尚未归类的研究' in client.get('/issues?project=us-rok-reassurance').text
    assert 'href="/issues?project=us-rok-reassurance"' in client.get('/issues?direction=_none').text
    assert 'name="project" value="us-rok-reassurance"' in client.get('/issues/new?direction=_none&project=us-rok-reassurance').text
    legacy_form = client.get('/projects/new').text
    assert 'href="/issues?direction=_none"' in legacy_form
    assert '<option value="_none"' not in legacy_form
    path = A.create('evidence', '跨方向历史材料', 'us-rok-reassurance')
    doc = A.load(path)
    doc.front['direction'] = 'strategy-signaling'
    A.save(doc)
    before = path.read_bytes()
    assert f'href="/issues/{doc.ref}"' in client.get('/issues?project=us-rok-reassurance').text
    detail = client.get('/issues/' + doc.ref).text
    assert '/issues/new?direction=_none&project=us-rok-reassurance' in detail
    sub = client.get('/issues/new?parent=' + doc.ref).text
    assert 'name="project" value="us-rok-reassurance"' in sub
    assert path.read_bytes() == before


def test_composer_creates_properties_atomically(client, repo, monkeypatch):
    writes = []
    save = T.save_yaml
    def counted(*args, **kwargs):
        writes.append(args[0])
        return save(*args, **kwargs)
    monkeypatch.setattr(T, 'save_yaml', counted)
    response = client.post('/issues', headers={'X-Issue-Composer': '1'}, data={
        'title': '完整新建议题', 'description': '研究描述', 'project': 'us-rok-reassurance',
        'revision': I.revision(), 'status': 'doing', 'priority': 'high', 'assignee': 'Leo',
        'due': '2027-01-15', 'labels': ['文献地图', '写作']})
    assert response.status_code == 200
    row = I.get(response.json()['url'].split('/')[-1])
    assert (row['description'], row['priority'], row['assignee'], row['status']) == ('研究描述', 'high', 'Leo', 'doing')
    assert row['labels'] == ['文献地图', '写作'] and row['due'] == '2027-01-15'
    assert 'week:' not in T.path().read_text()
    assert writes == [T.path()] and not I.path().exists()
    assert response.json()['revision'] == I.revision()


@pytest.mark.parametrize('field,value', [('title',''), ('priority','bogus'), ('labels',['bogus']), ('due','2026-02-30'), ('week','2026-W42')])
def test_invalid_composer_does_not_leave_partial_task(client, repo, field, value):
    before = T.path().read_bytes()
    form = {'title':'新议题', 'project':'us-rok-reassurance', 'revision':I.revision(), field:value}
    assert client.post('/issues', data=form, headers={'X-Issue-Composer':'1'}).status_code == 400
    assert T.path().read_bytes() == before and not I.path().exists()


def test_composer_parent_scope_and_stale_revision(client, repo):
    parent = new()
    form = {'title':'子议题', 'parent':parent, 'project':'us-iran-talks', 'revision':I.revision(parent)}
    change(parent, 'assignee', 'Leo')
    before = T.path().read_bytes()
    assert client.post('/issues', data=form, headers={'X-Issue-Composer':'1'}).status_code == 409
    assert T.path().read_bytes() == before
    form['revision'] = I.revision(parent)
    assert client.post('/issues', data=form, headers={'X-Issue-Composer':'1'}).status_code == 200
    assert next(t for t in T.read() if t['title']=='子议题')['project'] == 'us-rok-reassurance'


def test_legacy_task_attributes_can_be_cleared(repo):
    ref = new()
    I.path().write_text(f'{ref}:\n  assignee: 旧负责人\n  labels: [文献地图]\n  priority: high\n')
    assert I.get(ref)['assignee'] == '旧负责人'
    change(ref, 'assignee', '')
    change(ref, 'labels', [])
    change(ref, 'priority', 'none')
    assert I.get(ref)['assignee'] == '' and I.get(ref)['labels'] == [] and I.get(ref)['priority'] == 'none'


def test_week_and_status_are_independent_and_grouping_optional(client, repo):
    ref = new('本周已完成工作')
    change(ref, 'status', 'done')
    groups, count = I.board(view='week', state='done', group='none')
    assert count == 1 and ref in flatten(groups)
    html = client.get('/issues?project=us-rok-reassurance&view=week&state=done&group=status').text
    assert '本周已完成工作' in html and 'issue-group-heading' not in html
    assert 'issue-tabs' not in html


def test_recursive_historical_subissues_remain_visible(client, repo):
    doc = A.load(A.create('evidence','历史父议题','us-rok-reassurance'))
    parent = new('已完成中间议题', parent=doc.ref)
    child = new('仍在推进的子议题', parent=parent)
    change(parent, 'status', 'done')
    html = client.get('/issues/' + doc.ref).text
    assert f'href="/issues/{parent}"' in html and f'href="/issues/{child}"' in html


def test_project_is_the_parent_detail_without_creating_a_task(client, repo):
    from research_lab.files import datafiles
    ref = I.project_ref('us-rok-reassurance')
    first = new('先厘清研究问题')
    child = new('核对原始文件', parent=first)
    original = {p: p.read_bytes() for p in (repo / '_data').glob('*.yml')}
    page = client.get('/issues?project=us-rok-reassurance')
    assert page.status_code == 200
    assert 'data-project-parent="us-rok-reassurance"' in page.text
    assert 'class="issue-detail' in page.text and 'class="issue-index"' not in page.text
    assert f'/issues/{ref}/property' in page.text
    assert '议题属性' in page.text and 'Back to direction' in page.text
    assert f'href="/issues/{first}"' in page.text and f'href="/issues/{child}"' in page.text
    assert I.get(ref)['title'] == datafiles.project('us-rok-reassurance')['name']
    assert len(T.read()) == 2 and len([r for r in I.read() if not r['artifact']]) == 2
    assert all(p.read_bytes() == data for p, data in original.items()) and not I.path().exists()
    assert client.get('/issues/' + ref).url.query == b'project=us-rok-reassurance'
    assert client.get('/issues/PRJ-missing').status_code == 404


def test_project_parent_properties_are_independent_and_preserve_materials(client, repo):
    from research_lab.files import datafiles
    ref = I.project_ref('us-rok-reassurance')
    child = new('仍需完成的子议题')
    project_before = datafiles.project('us-rok-reassurance')
    before = {p: p.read_bytes() for p in (repo / '_artifacts').glob('*.md')}
    change(ref, 'title', '美韩同盟：保证与调整')
    for field, value in [('description', '重构研究框架。'), ('status', 'done'), ('priority', 'high'),
                         ('assignee', 'Leo'), ('due', '2027-01-15'), ('labels', ['文献地图'])]:
        change(ref, field, value)
        assert I.get(ref)[field] == value
    project_after = datafiles.project('us-rok-reassurance')
    assert project_after['name'] == '美韩同盟：保证与调整'
    assert project_after['question'] == project_before['question']
    assert project_after['stage'] == project_before['stage']
    assert I.get(child)['status'] == 'todo'
    assert all(p.read_bytes() == data for p, data in before.items())
    with pytest.raises(I.IssueError):
        change(ref, 'project', 'us-iran-talks')
    I.comment(ref, '本周先完成资料核对。', I.revision(ref))
    assert '本周先完成资料核对。' in client.get(I.url(ref)).text
    assert '美韩同盟：保证与调整' in client.get('/issues').text


def test_project_parent_child_creation_inherits_project_without_consuming_task_depth(client, repo):
    root = I.project_ref('us-rok-reassurance')
    change(root, 'due', T.week_due(week_add(week_of(), 1)))
    r = client.post('/issues', headers={'X-Issue-Composer': '1'}, data={
        'parent': root, 'project': 'us-iran-talks', 'title': '从课题拆分研究工作', 'revision': I.revision(root)})
    assert r.status_code == 200 and r.json()['url'] == '/issues?project=us-rok-reassurance'
    task = next(t for t in T.read() if t['title'] == '从课题拆分研究工作')
    assert task['project'] == 'us-rok-reassurance' and not task['parent'] and not task['source']
    assert task['due'] is None and I.get(root)['due'] == T.friday(week_add(week_of(), 1))   # nothing inherited
    nested = new('仍可继续细分', parent=task['id'])
    assert T.get(nested)['parent'] == task['id']
    html = client.get('/issues/' + task['id']).text
    assert 'Back to parent' in html and 'href="/issues?project=us-rok-reassurance"' in html


def test_project_parent_concurrent_rename_and_stale_create_do_not_write(repo, monkeypatch):
    from research_lab.files import datafiles
    ref = I.project_ref('us-rok-reassurance')
    old = I.revision(ref)
    datafiles.update_project('us-rok-reassurance', {'name': '其他窗口的新标题'})
    with pytest.raises(StaleFileError):
        I.create('不能用旧表单创建', '', ref, old)
    assert not T.read()
    original = datafiles.update_project
    def racing(slug, values, expected):
        original(slug, {'name': '保留并发写入'})
        return original(slug, values, expected)
    monkeypatch.setattr(datafiles, 'update_project', racing)
    with pytest.raises(StaleFileError):
        change(ref, 'title', '不应覆盖')
    assert datafiles.project('us-rok-reassurance')['name'] == '保留并发写入'


def test_related_project_roots_and_tasks_remain_bidirectional(repo, client):
    root, other = map(I.project_ref, ['us-rok-reassurance', 'us-iran-talks'])
    task = new('关联核对')
    change(root, 'related', [other, task])
    assert root in I.get(other)['related'] and root in I.get(task)['related']
    assert 'href="/issues?project=us-iran-talks"' in client.get(I.url(root)).text
    change(other, 'related', [])
    assert other not in I.get(root)['related'] and task in I.get(root)['related']
    change(task, 'related', [])
    assert not I.get(root)['related']


def test_empty_project_and_no_matching_children_keep_parent_visible(client, repo):
    from research_lab.files import datafiles
    datafiles.add_project({'slug': 'empty-parent', 'name': '空课题', 'question': '研究问题', 'stage': 'idea',
                           'direction': 'alliance-us'}, create_folder=False)
    page = client.get('/issues?project=empty-parent')
    assert page.status_code == 200 and '子议题标题' in page.text
    assert '还没有子议题' not in page.text and 'class="issue-subtree"' not in page.text
    ref = I.project_ref('us-rok-reassurance')
    change(ref, 'description', '父议题描述始终可见')
    parent = new('保留已完成父项')
    child = new('筛选目标子项', parent=parent)
    change(parent, 'status', 'done')
    change(child, 'status', 'doing')
    page = client.get('/issues?project=us-rok-reassurance&state=doing&view=week')
    assert '父议题描述始终可见' in page.text
    assert f'href="/issues/{parent}"' in page.text and f'href="/issues/{child}"' in page.text
    page = client.get('/issues?project=us-rok-reassurance&query=没有这个标题')
    assert '父议题描述始终可见' in page.text and f'href="/issues/{child}"' in page.text
    assert '添加子议题' in page.text and '议题属性' in page.text


def test_description_is_directly_editable_and_quick_add_has_one_submit(client, repo):
    ref = I.create('交互验收','us-rok-reassurance','',I.revision())
    page = client.get(f'/issues/{ref}').text
    assert '<form class="issue-description-form"' in page and 'data-autogrow' in page
    assert 'issue-description-edit' not in page and '编辑描述' not in page
    quick = page.split('class="issue-add-sub"')[1].split('</form>',1)[0]
    assert quick.count('type="submit"') == 1 and 'data-add-sub' in quick
    props = page.split('aria-label="议题属性"')[1].split('</aside>',1)[0]
    assert '负责人' not in props and 'issue-row-trail .issue-avatar' not in page
    composer = client.get('/issues/new?project=us-rok-reassurance',headers={'X-Issue-Composer':'1'}).text
    assert 'name="assignee"' not in composer


def test_subissues_sort_by_due_without_breaking_tree(client, repo):
    from datetime import timedelta
    day = lambda n: (date.today() + timedelta(days=n)).isoformat()
    later = A.load(A.create('evidence','较晚父项','us-rok-reassurance')).ref
    early_child = new('更早子项', parent=later)
    early = new('较早父项')
    unscheduled = new('未排期父项')
    finished = new('已完成的')
    change(later, 'due', day(14))
    change(early_child, 'due', day(-7))                            # pulls its parent forward: earliest open sub-issue
    change(early, 'due', day(0))
    change(finished, 'due', day(-30))
    change(finished, 'status', 'done')                             # done sinks to the bottom whatever its date
    html = client.get('/issues?project=us-rok-reassurance').text
    pos = [html.index(f'href="/issues/{x}"') for x in (later, early_child, early, unscheduled, finished)]
    assert pos == sorted(pos)
    assert 'data-issue-filters' not in html and '按截止' in html and '按计划周' not in html
    assert 'View archived' in html


JSON = {'Accept': 'application/json'}


def test_title_saves_inline_and_returns_the_stored_value_and_revision(client, repo):
    from research_lab.files import datafiles
    ref = new('旧标题')
    page = client.get('/issues/' + ref).text
    title = page.split('class="issue-title-form"')[1].split('</form>', 1)[0]
    assert 'data-inline-field' in title and '<textarea name="value"' in title and '<h1>' in title
    assert '保存标题' not in page and 'issue-inline-edit' not in page
    r = client.post(f'/issues/{ref}/property', headers=JSON, data={'field': 'title', 'value': '  核对  原始\n电报 ', 'revision': I.revision(ref)})
    assert r.status_code == 200
    assert r.json() == {'revision': I.revision(ref), 'value': '核对 原始 电报'}
    assert I.get(ref)['title'] == '核对 原始 电报'
    root = I.project_ref('us-rok-reassurance')
    r = client.post(f'/issues/{root}/property', headers=JSON, data={'field': 'title', 'value': '美韩同盟新名', 'revision': I.revision(root)})
    assert r.status_code == 200 and r.json()['revision'] == I.revision(root)
    assert datafiles.project('us-rok-reassurance')['name'] == '美韩同盟新名'


def test_description_autosave_chains_revisions_from_each_reply(client, repo):
    ref = new()
    page = client.get('/issues/' + ref).text
    assert '保存描述' not in page and 'data-autosave' in page and 'data-save-status' in page
    first = re.search(r'name="revision" value="([^"]+)"', page).group(1)
    r = client.post(f'/issues/{ref}/property', headers=JSON, data={'field': 'description', 'value': '第一段\r\n第二段  ', 'revision': first})
    assert r.status_code == 200 and r.json()['value'] == '第一段\n第二段'
    second = r.json()['revision']
    assert second != first and second == I.revision(ref)
    # The next autosave carries the revision the previous reply returned, never the page's original one.
    r = client.post(f'/issues/{ref}/property', headers=JSON, data={'field': 'description', 'value': '第一段\n第二段\n第三段', 'revision': second})
    assert r.status_code == 200 and T.get(ref)['note'] == '第一段\n第二段\n第三段'
    stale = client.post(f'/issues/{ref}/property', headers=JSON, data={'field': 'description', 'value': '旧草稿', 'revision': first})
    assert stale.status_code == 409 and stale.json()['error'] == '议题已在别处更新，请刷新后再修改。'
    assert T.get(ref)['note'] == '第一段\n第二段\n第三段'
    # Without the JSON accept header the no-JS form still redirects back to the issue.
    plain = client.post(f'/issues/{ref}/property', data={'field': 'description', 'value': '表单提交', 'revision': I.revision(ref)}, follow_redirects=False)
    assert plain.status_code == 303 and plain.headers['location'] == '/issues/' + ref


def test_quick_sub_issue_returns_its_row_progress_and_revision(client, repo):
    parent = new('拆分研究工作')
    page = client.get('/issues/' + parent).text
    assert 'data-quick-sub' in page and 'data-sub-progress hidden' in page
    r = client.post('/issues', headers=JSON, data={'title': '第一项子议题', 'parent': parent, 'project': 'us-rok-reassurance', 'revision': I.revision(parent)})
    assert r.status_code == 200
    body = r.json()
    child = body['id']
    assert T.get(child)['parent'] == parent and body['url'] == '/issues/' + child
    assert body['revision'] == I.revision(parent) and (body['done'], body['total']) == (0, 1)
    assert f'href="/issues/{child}"' in body['row'] and '第一项子议题' in body['row']
    assert 'data-sort="09999-99-99"' in body['row'] and 'class="issue-tree-item"' in body['row']
    change(child, 'status', 'done')
    r = client.post('/issues', headers=JSON, data={'title': '第二项子议题', 'parent': parent, 'project': 'us-rok-reassurance', 'revision': body['revision']})
    assert r.status_code == 409 and r.json()['error'] == '议题已在别处更新，请刷新后再修改。'
    r = client.post('/issues', headers=JSON, data={'title': '第二项子议题', 'parent': parent, 'project': 'us-rok-reassurance', 'revision': I.revision(parent)})
    assert (r.json()['done'], r.json()['total']) == (1, 2)
    # On a project page the new row is a top-level member and the count covers the project's members.
    root = I.project_ref('us-rok-reassurance')
    r = client.post('/issues', headers=JSON, data={'title': '课题下的新议题', 'parent': root, 'project': 'us-rok-reassurance', 'revision': I.revision(root)})
    assert r.status_code == 200 and (r.json()['done'], r.json()['total']) == (0, 2)
    assert 'issue-project-tag' not in r.json()['row']
    assert '课题下的新议题' in client.get('/issues?project=us-rok-reassurance').text


def test_inner_write_races_answer_409_in_chinese(client, repo, monkeypatch):
    ref = new()
    original = T.update
    def racing_update(task_id, values, expected, **kwargs):
        original(task_id, {'note': '另一个窗口刚保存的内容'}, T.file_sha())
        return original(task_id, values, expected, **kwargs)
    monkeypatch.setattr(T, 'update', racing_update)
    r = client.post(f'/issues/{ref}/property', headers=JSON, data={'field': 'description', 'value': '不能覆盖', 'revision': I.revision(ref)})
    assert r.status_code == 409 and r.json()['error'] == '议题已在别处更新，请刷新后再修改。'
    assert T.get(ref)['note'] == '另一个窗口刚保存的内容'


def test_inline_saves_still_pass_through_the_mutation_guard(client, repo):
    ref = new('受保护的标题')
    before = T.path().read_bytes()
    form = {'field': 'title', 'value': '跨站改名', 'revision': I.revision(ref)}
    r = client.post(f'/issues/{ref}/property', headers={**JSON, 'HX-Request': 'false'}, data=form)
    assert r.status_code == 403
    r = client.post('/issues', headers={**JSON, 'HX-Request': 'false', 'Origin': 'http://evil.example'},
                    data={'title': '跨站子议题', 'parent': ref, 'project': 'us-rok-reassurance', 'revision': I.revision(ref)})
    assert r.status_code == 403
    assert T.path().read_bytes() == before
    r = client.post(f'/issues/{ref}/property', headers={**JSON, 'HX-Request': 'false', 'Origin': 'http://127.0.0.1:4100'}, data=form)
    assert r.status_code == 200 and I.get(ref)['title'] == '跨站改名'


def test_reply_revision_never_absorbs_another_writer(client, repo, monkeypatch):
    ref = new('并发写入')
    original_update, original_add = T.update, T.add
    def then_other_file(original):
        def run(*args, **kwargs):
            result = original(*args, **kwargs)
            I._save_meta(ref, {'assignee': '另一个进程 ' + original.__name__}, I._meta_sha())  # issues.yml changes right after
            return result
        return run
    with monkeypatch.context() as m:
        m.setattr(T, 'update', then_other_file(original_update))
        r = client.post(f'/issues/{ref}/property', headers=JSON, data={'field': 'description', 'value': '我的描述', 'revision': I.revision(ref)})
    assert r.status_code == 200 and T.get(ref)['note'] == '我的描述'
    # The reply's revision must not cover the other writer's change, so the next save is refused.
    nxt = client.post(f'/issues/{ref}/property', headers=JSON, data={'field': 'description', 'value': '下一次', 'revision': r.json()['revision']})
    assert nxt.status_code == 409 and T.get(ref)['note'] == '我的描述'
    with monkeypatch.context() as m:
        m.setattr(T, 'add', then_other_file(original_add))
        r = client.post('/issues', headers=JSON, data={'title': '子项', 'parent': ref, 'project': 'us-rok-reassurance', 'revision': I.revision(ref)})
    assert r.status_code == 200 and r.json()['revision'] != I.revision(ref)
    # Without another writer the reply's revision is current.
    r = client.post(f'/issues/{ref}/property', headers=JSON, data={'field': 'description', 'value': '再写一次', 'revision': I.revision(ref)})
    assert r.json()['revision'] == I.revision(ref)


def test_quick_sub_issue_survives_a_row_that_fails_to_render(client, repo, monkeypatch):
    from research_lab.web.routes import issues as route
    parent = new('父议题')
    def broken():
        raise RuntimeError('template exploded')
    monkeypatch.setattr(route, 'templates', broken)
    r = client.post('/issues', headers=JSON, data={'title': '只建一次', 'parent': parent, 'project': 'us-rok-reassurance', 'revision': I.revision(parent)})
    assert r.status_code == 200
    body = r.json()
    assert 'row' not in body and body['revision'] == I.revision(parent)
    assert [t['title'] for t in T.read()].count('只建一次') == 1 and T.get(body['id'])['parent'] == parent


def test_title_keeps_full_width_spaces_and_collapses_ascii_runs(client, repo):
    doc = A.load(A.create('memo', 'title-spaces', 'us-iran-talks'))
    r = client.post(f'/issues/{doc.ref}/property', headers=JSON, data={'field': 'title', 'value': '东亚　秩序 与  同盟\n政治', 'revision': I.revision(doc.ref)})
    assert r.status_code == 200
    assert r.json()['value'] == '东亚　秩序 与 同盟 政治' == I.get(doc.ref)['title']


def test_every_write_stamps_updated_on_what_it_changed(repo):
    from datetime import date
    old, day = date(2026, 9, 1), date(2026, 9, 20)
    parent, _ = T.add({'title': '父', 'project': 'us-rok-reassurance'}, today=old)
    child, _ = T.add({'title': '子', 'project': 'us-rok-reassurance', 'parent': parent}, today=old)
    other, _ = T.add({'title': '旁边的', 'project': 'us-rok-reassurance', 'due': '2026-09-25'}, today=old)
    assert {T.get(x)['updated'] for x in (parent, child, other)} == {'2026-09-01'}
    T.set_status(parent, 'parked', today=day)                     # parking a parent parks its sub-task too
    assert T.get(parent)['updated'] == T.get(child)['updated'] == '2026-09-20' and T.get(other)['updated'] == '2026-09-01'
    T.move(other, parent, today=date(2026, 9, 21))                 # a reorder changes no field but is a touch
    assert T.get(other)['updated'] == '2026-09-21' and T.get(parent)['updated'] == '2026-09-20'
    T.postpone(other, '2026-10-02', today=date(2026, 9, 22))
    assert T.get(other)['updated'] == '2026-09-22' and T.get(other)['postponed'] == 1
    text = T.path().read_text()
    assert "updated: '2026-09-22'" in text and text.index('done:') < text.index('updated:') < text.index('postponed:')
    art = next(r['id'] for r in I.read() if r['artifact'])
    change(art, 'status', 'doing')
    assert str(I._metadata()[art]['updated']) == date.today().isoformat()
    assert I.last_touched()[art] == date.today().isoformat() and I.last_touched()[child] == '2026-09-20'


def test_create_records_origin_and_the_form_still_creates_users_tasks(repo):
    mine = new('我建的')
    ref, state = I.create_checked('助手建的', parent=mine, priority='high')
    assert T.get(mine)['origin'] == 'user' and T.get(ref)['origin'] == 'assistant'
    assert state['title'] == '助手建的' and state['priority'] == 'high' and state['parent'] == mine
    assert I.comments(ref)[0]['by'] == 'assistant' and I.comments(ref)[0]['text'] == '新建了这个议题'
    with pytest.raises(I.IssueError):
        I.create_checked('不能设负责人以外的', parent=mine, Claim='x')


def test_display_and_phrase_words(repo):
    from datetime import date
    today = date(2026, 10, 8)
    assert I.display('week', '2026-W42', today=today) == '第 42 周' and I.display('week', '2027-W02', today=today) == '2027 年第 2 周'
    assert I.display('week', '', today=today) == '未排期' and I.display('due', '2026-10-20', today=today) == '10 月 20 日'
    assert I.display('status', 'parked') == '搁置' and I.display('labels', ['写作', '问题']) == '写作、问题'
    assert I.display('project', 'asean-centrality') == 'ASEAN Centrality'
    assert I.phrase('description', '', '新描述') == '写了描述' and I.phrase('description', '旧', '') == '清空了描述'
    assert I.phrase('title', '旧', '新') == '标题 「旧」 → 「新」'


def test_direction_is_editable_on_projects_outputs_and_sub_issues(client, repo):
    """Leo 10-11: Theo could not move a 课题 to another 研究方向 because direction was not a field."""
    from research_lab.files import datafiles
    slugs = [str(d['slug']) for d in datafiles.read('directions')]
    root = I.project_ref('us-rok-reassurance')
    target = next(d for d in slugs if d != I.get(root)['direction'])
    I.update(root, 'direction', target, I.revision(root))
    assert datafiles.project('us-rok-reassurance')['direction'] == target
    child = new('跟着课题换方向')
    assert I.get(child)['direction'] == target                      # a sub-issue inherits its 课题's
    other = next(d for d in slugs if d != target)
    I.update(child, 'direction', other, I.revision(child))
    assert I.get(child)['direction'] == other and I.get(root)['direction'] == target
    with pytest.raises(I.IssueError):
        I.update(child, 'direction', 'no-such-direction', I.revision(child))
    out = I.change_fields(root, [('direction', target, other)])     # the assistant's path, slug given
    assert [a['field'] for a in out['applied']] == ['direction'] and datafiles.project('us-rok-reassurance')['direction'] == other
    assert '研究方向' in out['line']
    output = next(r for r in I.read() if r['artifact'])
    I.update(output['id'], 'direction', target, I.revision(output['id']))
    assert A.load(next(p for p in A.list_paths() if A.load(p).ref == output['id'])).get('direction') == target
    page = client.get(I.url(root)).text
    assert 'name="field" value="direction"' in page
