"""Card 0180 (Leo 10-16): a direction's issue prefix can change, APUS → US, through `rl tasks rekey`. Every key keeps
its number under the new prefix, the old key stays an alias that finds the same issue, new issues continue after the
old count, directions.yml takes the new key in the same write, and running it again changes nothing."""
import pytest
from typer.testing import CliRunner

from research_lab.files import datafiles, issues as I, tasks as T
from research_lab.files.yamlio import load_yaml, save_yaml, sha256_text

KEYS = {'alliance-us': 'APUS', 'sea-asean': 'SEAS', 'strategy-signaling': 'SSIG', 'periphery-nts': 'NTSP'}
SLUG = 'alliance-us'


@pytest.fixture
def numbered(repo):
    """Leo's four prefixes and a key file, as after `rl tasks migrate-keys`."""
    p = repo / '_data/directions.yml'
    data = load_yaml(p)
    for d in data:
        if not d.get('key'):
            d.insert(1, 'key', KEYS[str(d['slug'])])
    save_yaml(p, data)
    I.keys_path().unlink(missing_ok=True)
    I.sync_keys(create=True)
    return repo


def task(title, project='us-rok-reassurance'):
    return T.add({'title': title, 'project': project})[0]


def keyed(pfx: str) -> dict[str, str]:
    return {r: str(k) for r, k in I._keys()['refs'].items() if str(k).startswith(pfx + '-')}


def shas() -> dict[str, str]:
    return {p.name: sha256_text(p.read_text(encoding='utf-8'))
            for p in (I.keys_path(), datafiles._path('directions'), I.path()) if p.exists()}


def rl(*args):
    from research_lab.cli import app
    return CliRunner().invoke(app, ['tasks', *args])


def test_every_key_keeps_its_number_and_the_old_key_finds_the_same_issue(numbered, client):
    for title in ('第一条', '第二条', '第三条'):
        task(title)
    before, top = keyed('APUS'), I._keys()['last']['APUS']
    assert len(before) >= 4                                              # the 课题 and its three issues
    plan = I.rekey_direction(SLUG, 'US')
    assert plan['written'] and (plan['old'], plan['new']) == ('APUS', 'US')
    assert {c['ref']: (c['old'], c['new']) for c in plan['changes']} == {
        r: (k, 'US-' + k.split('-')[1]) for r, k in before.items()}
    stored = I._keys()
    for ref, old in before.items():
        new = 'US-' + old.split('-')[1]
        assert stored['refs'][ref] == new and stored['aliases'][old] == ref
        assert I.resolve(old) == ref and I.resolve(new) == ref and I.resolve(old.lower()) == ref
    assert 'APUS' not in stored['last'] and stored['last']['US'] == top
    assert list(stored['last'])[0] == 'US'                               # in the old prefix's place
    assert datafiles.direction(SLUG)['key'] == 'US'
    assert I.sync_keys() == []                                           # the two files agree: nothing reads as a move
    some = next(r for r in before if not r.startswith('PRJ-'))
    old, new = before[some], 'US-' + before[some].split('-')[1]
    r = client.get(f'/issues/{old}', follow_redirects=False)            # an old link goes to the new key's address
    assert r.status_code == 303 and r.headers['location'] == f'/issues/{new}'
    assert client.get(f'/issues/{new}').status_code == 200
    assert f'编号 {old} → {new}' in [c['text'] for c in I.comments(some)]


def test_new_issues_continue_after_the_old_count(numbered):
    doc = load_yaml(I.keys_path())
    doc['last']['APUS'] = int(doc['last']['APUS']) + 5                  # as if the newest five had been deleted
    save_yaml(I.keys_path(), doc)
    top = int(doc['last']['APUS'])
    plan = I.rekey_direction(SLUG, 'US')
    assert plan['next'] == f'US-{top + 1:04d}'
    assert I.key_of(task('改前缀之后建的')) == f'US-{top + 1:04d}'      # no deleted number comes back
    assert I.key_of(task('再建一个')) == f'US-{top + 2:04d}'


@pytest.mark.parametrize('bad, word', [('', '前缀'), ('us', '大写'), ('U', '大写'), ('USAAAA', '大写'), ('U1', '大写'),
                                       ('LAB', '留作他用'), ('DS', '留作他用'), ('SEAS', '已被')])
def test_a_bad_prefix_is_refused_and_nothing_is_written(numbered, bad, word):
    before = shas()
    with pytest.raises(datafiles.DataFileError, match=word):
        I.rekey_direction(SLUG, bad)
    assert shas() == before


def test_a_prefix_the_key_file_ever_used_is_refused(numbered):
    I.rekey_direction(SLUG, 'US')
    before = shas()
    with pytest.raises(I.IssueError, match='用过'):
        I.rekey_direction('sea-asean', 'APUS')                          # still the prefix of every alias
    assert shas() == before
    doc = load_yaml(I.keys_path())
    doc['aliases']['OLDX-0003'] = 'PRJ-asean-centrality'                 # a prefix only an alias remembers
    save_yaml(I.keys_path(), doc)
    before = shas()
    with pytest.raises(I.IssueError, match='用过'):
        I.rekey_direction('sea-asean', 'OLDX')
    with pytest.raises(I.IssueError, match='没有方向'):
        I.rekey_direction('no-such-direction', 'NEW')
    assert shas() == before
    out = rl('rekey', 'sea-asean', 'OLDX')
    assert out.exit_code == 1 and '用过' in out.output


def test_running_it_again_changes_nothing(numbered):
    first = I.rekey_direction(SLUG, 'US')
    before = shas()
    again = I.rekey_direction(SLUG, 'US')
    assert (again['old'], again['new'], again['changes'], again['written']) == ('US', 'US', [], False)
    assert shas() == before
    out = rl('rekey', SLUG, 'US')
    assert out.exit_code == 0 and '没有要改的' in out.output and shas() == before
    assert first['changes'] and all(I.resolve(c['old']) == c['ref'] for c in first['changes'])


def test_a_dry_run_prints_the_mapping_and_writes_nothing(numbered):
    before, keys = shas(), keyed('APUS')
    plan = I.rekey_direction(SLUG, 'US', write=False)
    assert not plan['written'] and len(plan['changes']) == len(keys) and shas() == before
    out = rl('rekey', SLUG, 'US', '--dry-run')
    assert out.exit_code == 0 and '预演' in out.output and f'{len(keys)} 个编号' in out.output
    assert all(f"{k} → US-{k.split('-')[1]}" in out.output for k in keys.values())
    assert shas() == before and datafiles.direction(SLUG)['key'] == 'APUS' and keyed('APUS') == keys
    out = rl('rekey', SLUG, 'US')
    assert out.exit_code == 0 and '已写入' in out.output and keyed('APUS') == {} and len(keyed('US')) == len(keys)


def test_other_directions_and_their_aliases_are_untouched(numbered, client):
    moved = task('换到东盟的议题')
    old = I.key_of(moved)
    r = client.post(f'/issues/{moved}/property', headers={'Accept': 'application/json'},
                    data={'field': 'direction', 'value': 'sea-asean', 'revision': I.revision(moved)})
    assert r.status_code == 200 and I.key_of(moved).startswith('SEAS-')
    others = {p: keyed(p) for p in ('SEAS', 'SSIG', 'NTSP')}
    last = {p: n for p, n in I._keys()['last'].items() if p != 'APUS'}
    I.rekey_direction(SLUG, 'US')
    stored = I._keys()
    assert {p: keyed(p) for p in others} == others
    assert {p: n for p, n in stored['last'].items() if p != 'US'} == last
    assert stored['aliases'][old] == moved and I.resolve(old) == moved   # an alias from a move keeps pointing home
    assert f"US-{old.split('-')[1]}" not in stored['aliases'] and I.resolve(f"US-{old.split('-')[1]}") is None
    assert {p: datafiles.direction(s)['key'] for s, p in KEYS.items() if s != SLUG} == {p: p for p in others}
    assert I.sync_keys() == []
    assert int(I.key_of(task('新的一条')).split('-')[1]) > int(old.split('-')[1])   # the moved one's number stays used


def test_before_the_migration_only_the_direction_changes(numbered):
    I.keys_path().unlink()
    plan = I.rekey_direction(SLUG, 'US')
    assert plan['written'] and plan['changes'] == [] and not I.keys_path().exists()
    assert datafiles.direction(SLUG)['key'] == 'US'
