"""Card 0098 (ISSUES-LINEAR.md §2.4): the 添加子议题 row is the sub-issue composer, folded; open, it takes a
description, keeps the parent's 课题 and 类型, starts with the parent's priority and sends the due date as a
preset worked out on the server."""
from research_lab.files import dates as D, datafiles, issues as I, tasks as T

JSON = {'Accept': 'application/json'}
SLUG = 'us-rok-reassurance'


def test_the_add_row_is_the_composer_with_inherited_properties(client, repo):
    datafiles.update_project(SLUG, {'type': '论文'})
    parent = I.create('父议题', SLUG, '', I.revision())
    I.update(parent, 'priority', 'high', I.revision(parent))
    page = client.get(f'/issues/{parent}').text
    form = page.split('data-sub-composer', 1)[1].split('</form>', 1)[0]
    assert form.count('type="submit"') == 1 and 'data-sub-more hidden' in form and 'name="description"' in form
    assert 'name="priority" value="high"' in form                       # starts as the parent's
    assert '类型：论文' in form and datafiles.project(SLUG)['name'] in form  # inherited, shown, not chosen
    assert all(f'data-choice="{p.key}"' in form for p in D.presets())    # due presets go up as keys
    assert 'data-open-sub-composer' in page                              # the header + opens it in place


def test_creating_from_the_open_composer_sends_description_priority_and_due(client, repo):
    parent = I.create('父议题', SLUG, '', I.revision())
    r = client.post('/issues', headers=JSON, data={'title': '核对案例编码', 'parent': parent, 'project': SLUG,
                                                    'revision': I.revision(parent), 'description': '先看 **Lee 2024**',
                                                    'priority': 'medium', 'due': 'friday'})
    assert r.status_code == 200 and 'row' in r.json()
    child = T.get(r.json()['id'])
    assert (child['parent'], child['note'], child['due']) == (parent, '先看 **Lee 2024**', D.upcoming_friday().isoformat())
    assert I.get(child['id'])['priority'] == 'medium'
