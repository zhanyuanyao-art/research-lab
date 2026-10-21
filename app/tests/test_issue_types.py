"""Card 0100 (ISSUES-LINEAR.md §5, Leo's 10-10 decision): 论文 / 课题 / 手稿 is the 课题's output type,
one of three, set on the 课题 (projects.yml) and shown on its sub-issues as 「类型：课题」."""
import pytest

from research_lab.files import datafiles, issues as I

JSON = {'Accept': 'application/json'}
SLUG = 'us-rok-reassurance'


def set_type(client, ref, value, revision=None):
    return client.post(f'/issues/{ref}/property', headers=JSON,
                       data={'field': 'type', 'value': value, 'revision': revision or I.revision(ref)})


def test_the_type_is_set_on_the_project_and_inherited(client, repo):
    root = I.project_ref(SLUG)
    child = I.create('子议题', SLUG, '', I.revision())
    page = client.get(I.url(root)).text
    assert '类型：未设置' in page and 'name="field" value="type"' in page and '这个课题的成果类型' in page
    r = set_type(client, root, '论文')
    assert r.status_code == 200 and r.json()['value'] == '论文' and r.json()['revision'] == I.revision(root)
    assert datafiles.project(SLUG)['type'] == '论文'
    sub = client.get(f'/issues/{child}').text
    assert '类型：论文' in sub and 'name="field" value="type"' not in sub     # read-only on a sub-issue
    assert set_type(client, child, '手稿').status_code == 400                 # set on the 课题 only
    assert set_type(client, root, '专著').status_code == 400                  # one of the three
    assert set_type(client, root, '课题', revision='stale').status_code == 409
    assert set_type(client, root, '').status_code == 200 and 'type' not in datafiles.project(SLUG)
    with pytest.raises(datafiles.DataFileError):
        datafiles.update_project(SLUG, {'type': '专著'})


def test_project_cards_show_the_type(client, repo):
    datafiles.update_project(SLUG, {'type': '课题'})
    direction = datafiles.project(SLUG)['direction']
    assert '类型：课题' in client.get(f'/issues?direction={direction}').text   # the 课题 progress card
    assert '类型：课题' in client.get('/projects').text                         # the project card
    page = client.get(I.url(I.project_ref(SLUG))).text
    assert '内容标签' in page                                                 # the six labels are the 内容 group
