"""Card 0121 (docs/design/ISSUES-LINEAR.md 附：议题编号, Leo 10-11): each research direction has a prefix, issues are
numbered per prefix (APUS-0001, LAB without a direction), numbers never come back, a change of direction gives the
next number of the new prefix and keeps the old key as an alias, and pages, search and the assistant all use keys."""
from datetime import date

import pytest
from typer.testing import CliRunner

from research_lab.files import artifacts as A, datafiles, issues as I, tasks as T

KEYS = {'alliance-us': 'APUS', 'sea-asean': 'SEAS', 'strategy-signaling': 'SSIG', 'periphery-nts': 'NTSP'}
JSON = {'Accept': 'application/json'}


@pytest.fixture
def prefixed(repo):
    """directions.yml with Leo's four prefixes (committed with this card; set here so the tests stand on their own)."""
    from research_lab.files.yamlio import load_yaml, save_yaml
    p = repo / '_data/directions.yml'
    data = load_yaml(p)
    for d in data:
        if not d.get('key'):
            d.insert(1, 'key', KEYS[str(d['slug'])])
    save_yaml(p, data)
    return repo


@pytest.fixture
def unnumbered(prefixed):
    """Before the migration: no key file, whatever the content snapshot carries."""
    I.keys_path().unlink(missing_ok=True)
    return prefixed


@pytest.fixture
def numbered(prefixed):
    I.sync_keys(create=True)
    return prefixed


def num(key: str) -> int:
    return int(key.rsplit('-', 1)[1])


def last(pfx: str) -> int:
    return int(I._keys()['last'].get(pfx) or 0)


def task(title, project='us-rok-reassurance', day=None):
    return T.add({'title': title, 'project': project}, today=day)[0]


def rl(*args):
    from research_lab.cli import app
    return CliRunner().invoke(app, ['tasks', *args])


def test_migration_numbers_each_direction_by_creation_projects_first(unnumbered):
    late, early = task('后建的核对', day=date(2026, 9, 20)), task('先建的核对', day=date(2026, 9, 10))
    asean = task('东盟的待办', project='asean-centrality', day=date(2026, 12, 1))
    datafiles.add_project({'slug': 'misc', 'name': '杂项', 'question': '还没定方向的事', 'stage': 'idea'}, create_folder=False)
    assert I.key_of(late) == '' and not I.keys_path().exists()          # nothing is numbered before the migration
    dry = rl('migrate-keys', '--dry-run')
    assert dry.exit_code == 0 and '预演' in dry.output and 'APUS-0001' in dry.output and not I.keys_path().exists()
    out = rl('migrate-keys')
    assert out.exit_code == 0 and '已写入' in out.output
    keys = {r['id']: r['key'] for r in I.read() + I.project_roots()}
    assert all(keys.values())                                          # every issue, outputs included
    root, first, second = (keys[r] for r in ('PRJ-us-rok-reassurance', early, late))
    assert all(k.startswith('APUS-') for k in (root, first, second)) and num(root) < num(first) < num(second)
    assert keys['PRJ-asean-centrality'].startswith('SEAS-') and keys[asean].startswith('SEAS-')
    assert num(keys['PRJ-asean-centrality']) < num(keys[asean])        # the 课题 first, then its issues
    assert keys['PRJ-misc'].startswith('LAB-')
    assert len(set(keys.values())) == len(keys)
    again = rl('migrate-keys')
    assert again.exit_code == 0 and '没有要改的' in again.output and I.key_of(early) == first


def test_migration_refuses_a_direction_without_a_valid_prefix(unnumbered):
    from research_lab.files.yamlio import load_yaml, save_yaml
    p = unnumbered / '_data/directions.yml'
    for broken in ('', 'SEAS'):                                          # missing, then the same as another's
        data = load_yaml(p)
        data[0]['key'] = broken
        save_yaml(p, data)
        out = rl('migrate-keys', '--dry-run')
        assert out.exit_code == 1 and '前缀' in out.output and not I.keys_path().exists()


def test_new_issues_take_the_next_number_and_numbers_never_come_back(numbered, client):
    top = last('APUS')
    ref = I.create('新的核对', 'us-rok-reassurance', '', I.revision())
    assert I.key_of(ref) == f'APUS-{top + 1:04d}'
    snap = I.created_state(ref)
    assert I.remove_created(ref, snap) == 'kept'                        # Leo's own issue is never deleted
    gone = T.add({'title': '助手建的', 'project': 'us-rok-reassurance', 'origin': 'assistant'})[0]
    assert I.key_of(gone) == f'APUS-{top + 2:04d}'
    assert I.remove_created(gone, I.created_state(gone)) == 'deleted'  # the key does not block the assistant's undo
    after = task('删除之后再建')
    assert I.key_of(after) == f'APUS-{top + 3:04d}'                     # the deleted one's number is not issued again
    art = A.load(A.create('evidence', 'keyed-output', 'asean-centrality', title='新产出'))
    assert I.key_of(art.ref).startswith('SEAS-')
    datafiles.add_project({'slug': 'maritime', 'name': '海洋', 'question': '？', 'stage': 'idea',
                           'direction': 'periphery-nts'}, create_folder=False)
    assert I.key_of('PRJ-maritime').startswith('NTSP-')


def test_a_change_of_direction_renumbers_and_the_old_key_still_finds_it(numbered, client):
    ref = task('换方向的议题')
    old = I.key_of(ref)
    r = client.post(f'/issues/{ref}/property', headers=JSON,
                    data={'field': 'direction', 'value': 'sea-asean', 'revision': I.revision(ref)})
    assert r.status_code == 200
    new = I.key_of(ref)
    assert new.startswith('SEAS-') and I.resolve(old) == ref and I.resolve(new) == ref
    # the revision the page got still saves: the renumbering and its activity line are this request's own
    assert client.post(f'/issues/{ref}/property', headers=JSON,
                       data={'field': 'status', 'value': 'doing', 'revision': r.json()['revision']}).status_code == 200
    texts = [c['text'] for c in I.comments(ref)]
    assert f'编号 {old} → {new}' in texts
    assert client.get(f'/issues/{old}', follow_redirects=False).headers['location'] == f'/issues/{new}'
    lower = old.lower().replace('-000', '-')                            # apus-2: any case, no padding
    assert client.get(f'/issues/{lower}', follow_redirects=False).headers['location'] == f'/issues/{new}'
    page = client.get(f'/issues/{new}')
    assert page.status_code == 200 and f'data-link="/issues/{new}"' in page.text and 'issue-key-copy' in page.text
    assert client.get('/issues/APUS-9999').status_code == 404
    assert client.get(f'/search?q={old}', follow_redirects=False).headers['location'] == f'/issues/{new}'
    root_key = I.key_of('PRJ-us-rok-reassurance')
    assert client.get(f'/search?q={root_key}', follow_redirects=False).headers['location'] == '/issues?project=us-rok-reassurance'


def test_a_project_changing_direction_renumbers_its_issues(numbered, client):
    sub = task('跟着课题走的议题')
    root = I.project_ref('us-rok-reassurance')
    before, root_before = I.key_of(sub), I.key_of(root)
    rev = I.update(root, 'direction', 'periphery-nts', I.revision(root), actor='user')
    assert I.key_of(root).startswith('NTSP-') and I.key_of(sub).startswith('NTSP-')
    assert I.resolve(root_before) == root and I.resolve(before) == sub
    assert rev == I.revision(root)                                       # the page's next save is not refused
    datafiles.update_project('us-rok-reassurance', {'direction': 'alliance-us'})   # the 课题's own edit page
    assert I.key_of(sub).startswith('APUS-') and I.key_of(sub) != before  # back, but with a new number


def test_keys_show_on_the_pages_in_grey(numbered, client):
    parent = task('第二章')
    child = I.create('第一节', 'us-rok-reassurance', parent, I.revision(parent))
    T.update(child, {'due': date.today().isoformat()})
    pkey, ckey = I.key_of(parent), I.key_of(child)
    page = client.get(f'/issues/{child}').text
    assert f'<span class="current issue-key">{ckey}</span>' in page                     # breadcrumb
    assert f'<span class="issue-key">{pkey}</span>' in page                             # the parent above the title
    rows = client.get(f'/issues/{parent}').text
    assert f'<span class="issue-row-key">{ckey}</span>' in rows                          # sub-issue rows
    archive = client.get('/issues').text
    assert f'<span class="project-row-key">{I.key_of("PRJ-us-rok-reassurance")}</span>' in archive   # 研究档案
    todo = client.get('/issues?view=todo').text
    assert f'<span class="issue-row-key">{ckey}</span>' in todo                          # 全部待办
    assert f'<span class="issue-key">{ckey}</span>' in client.get('/').text              # 首页待办


def test_theo_reads_and_says_keys(numbered, conn):
    from research_lab.agent import tools
    ref = task('引言')
    key = I.key_of(ref)
    rows = I.read() + I.project_roots()
    assert tools._resolve(conn, None, key, rows, None)['id'] == ref
    assert tools._resolve(conn, None, key.lower().replace('-000', '-'), rows, None)['id'] == ref
    assert tools.issue_for_page(f'/issues/{key}') == ref
    found, err = tools.call(conn, 'find_issues', {'query': key}, 5000)
    assert not err and f'ref {ref}' in found and f'key {key}' in found
    row = next(r for r in rows if r['id'] == ref)
    assert tools._said(row, 'status', 'todo', 'done', rows) == f'{key}「引言」状态：待办 → 已完成'
    assert tools._named(row, f'把 {key.lower()} 标成完成', None, rows)
    I.update(ref, 'direction', 'sea-asean', I.revision(ref))
    rows = I.read() + I.project_roots()
    assert tools._resolve(conn, None, key, rows, None)['id'] == ref    # the old key still finds it


def test_a_direction_needs_a_prefix_that_never_changes(prefixed, client):
    sha = datafiles.file_sha('directions')
    for bad, word in (('', '前缀'), ('ab1', '大写'), ('LAB', '留作他用'), ('APUS', '已被')):
        with pytest.raises(datafiles.DataFileError, match=word):
            datafiles.add_direction({'slug': 'maritime', 'key': bad, 'name': '海洋'}, sha)
    datafiles.add_direction({'slug': 'maritime', 'key': 'MSEC', 'name': '海洋'}, sha)
    assert datafiles.direction('maritime')['key'] == 'MSEC'
    with pytest.raises(datafiles.DataFileError, match='不能修改'):
        datafiles.update_direction('maritime', {'key': 'MARI'})
    r = client.post('/artifacts/directions', data={'slug': 'climate', 'name': '气候', 'sha': datafiles.file_sha('directions')})
    assert r.status_code == 400 and '前缀' in r.text
    page = client.get('/artifacts/directions').text
    assert 'name="key"' in page and '<span class="issue-key" title="议题编号的前缀，创建后不能修改">MSEC</span>' in page


def test_twenty_issues_numbered_at_once_get_twenty_different_keys_all_written(numbered):
    """0121 review: 30 threads once got 15 keys back, some twice, and only 3 written. Numbering is locked now."""
    import threading
    top = last('APUS')
    refs = [f'T-2099-{n:04d}' for n in range(1, 21)]
    start, got = threading.Barrier(len(refs)), {}

    def take(ref):
        start.wait()
        got[ref] = I.assign_key(ref, 'alliance-us')

    threads = [threading.Thread(target=take, args=(r,)) for r in refs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    keys = list(got.values())
    assert all(keys) and len(set(keys)) == 20
    assert sorted(num(k) for k in keys) == list(range(top + 1, top + 21))
    stored = I._keys()
    assert {r: stored['refs'][r] for r in refs} == got and last('APUS') == top + 20


def test_an_issue_left_without_a_key_gets_one_when_a_page_shows_it(numbered, client):
    ref = task('漏了编号的议题')
    from research_lab.files.yamlio import load_yaml, save_yaml
    doc = load_yaml(I.keys_path())
    lost = doc['refs'].pop(ref)                                          # as if its numbering had failed
    save_yaml(I.keys_path(), doc)
    assert I.key_of(ref) == ''
    page = client.get('/issues?view=todo').text
    key = I.key_of(ref)
    assert key and key != lost and f'<span class="issue-row-key">{key}</span>' in page   # a new number, never the old
    moved = task('换了方向还没改号')
    doc = load_yaml(I.keys_path())
    doc['refs'][moved] = 'SSIG-0999'                                     # a stale prefix is a move, not a gap
    save_yaml(I.keys_path(), doc)
    client.get(f'/issues/{moved}')
    assert I.key_of(moved) == 'SSIG-0999'                                # pages fill gaps only; moves are renumbered on writes
