from copy import deepcopy

import httpx
import pytest

from research_lab import config
from research_lab.feeds import presets, sources
from research_lab.files.yamlio import StaleFileError, save_yaml


@pytest.fixture
def empty_feeds(repo):
    path = repo / '_data/feeds.yml'
    path.write_text('# User sources stay here.\njournals: []\nnews: []\nradar_terms: []\n')
    return path


def save(profiles=('ir',), channels=('papers', 'news'), keywords=''):
    presets.save(list(profiles), list(channels), keywords, presets.read()[1])


def test_opt_in_skip_and_reopen(client, empty_feeds):
    before = empty_feeds.read_bytes()
    response = client.get('/settings/interests?return_to=/guide')
    assert response.status_code == 200
    assert 'value="ir"' in response.text and '国际关系预设' in response.text
    assert 'href="/guide">Skip' in response.text
    assert empty_feeds.read_bytes() == before
    assert sources.load().journals == []
    save(('ir', 'security', 'asia'), keywords='ASEAN, ASEAN, bargaining')
    first = sources.load()
    assert len(first.journals) == 6 and len(first.news) == 3
    assert {'asean', 'alliance', 'bargaining'} <= set(first.radar_terms)
    config._paths = None
    assert sources.load() == first
    assert presets.view()['selected'] == ['ir', 'security', 'asia']
    assert presets.view()['keywords'] == 'asean, bargaining'
    assert '# User sources stay here.' in empty_feeds.read_text()


@pytest.mark.parametrize('channels,counts', [(['papers'], (4, 0)), (['news'], (0, 2)), ([], (0, 0))])
def test_channels_and_cancel(empty_feeds, channels, counts):
    save(channels=channels)
    src = sources.load()
    assert (len(src.journals), len(src.news)) == counts
    save((), ())
    assert sources.load() == sources.Sources()
    assert presets.view()['selected'] == []
    assert 'ir' not in presets.view()['selected']  # Saved opt-out must not restore defaults.


def test_presets_dedupe_and_manual_disabled_entries_win(empty_feeds):
    data = {'journals': [
        {'abbr': 'Different-name', 'name': 'Manual IO', 'issn': ['0020-8183'], 'enabled': False},
        {'abbr': 'isq', 'name': 'Custom ISQ', 'issn': ['1111-1111'], 'weight': 17},
    ], 'news': [
        {'name': 'Renamed BBC', 'url': 'https://feeds.bbci.co.uk/news/world/rss.xml', 'enabled': False},
        {'name': 'UN News', 'url': 'https://example.org/manual', 'lane': 'research', 'weight': 3},
    ], 'radar_terms': ['existing'], 'gp_terms': ['unchanged'], 'private_extension': {'keep': True}}
    save_yaml(empty_feeds, data)
    save(('ir', 'governance', 'security'))
    expanded = sources.load()
    assert [j.name for j in expanded.journals] == ['Custom ISQ', 'Review of International Studies',
                                                  'European Journal of International Relations', 'International Security']
    assert len(expanded.news) == 1 and expanded.news[0].url == 'https://example.org/manual'
    stored, _ = presets.read()
    assert {k: v for k, v in stored.items() if k != 'subscriptions'} == data
    before = deepcopy(stored)
    presets.expand(stored)
    assert stored == before
    save((), ())
    assert sources.load() == sources.parse(data)


def test_all_presets_are_bounded_unique_and_have_real_identifiers(empty_feeds):
    save(tuple(presets.IDS))
    src = sources.load()
    assert len(src.journals) == len(presets.JOURNALS) and len(src.news) == 3
    assert len({i for j in src.journals for i in j.issns}) == len(presets.JOURNALS)
    for _, issn, publisher in presets.JOURNALS.values():
        digits = issn.replace('-', '')
        total = sum(int(n) * (8 - i) for i, n in enumerate(digits[:7])) + (10 if digits[-1] == 'X' else int(digits[-1]))
        assert total % 11 == 0
        assert publisher.startswith('https://')


def test_stale_missing_and_malformed_files_are_safe(empty_feeds):
    stale = presets.read()[1]
    empty_feeds.write_text(empty_feeds.read_text() + 'custom: preserved\n')
    with pytest.raises(StaleFileError):
        presets.save(['ir'], ['papers'], '', stale)
    assert 'custom: preserved' in empty_feeds.read_text() and 'subscriptions' not in empty_feeds.read_text()
    empty_feeds.unlink()
    assert presets.read() == ({}, '')
    save()
    assert len(sources.load().journals) == 4
    empty_feeds.write_text('news: [broken:')
    with pytest.raises(presets.PreferenceError):
        save()
    assert empty_feeds.read_text() == 'news: [broken:'


@pytest.mark.parametrize('value', [None, {'version': 99}, {'version': 1, 'profiles': ['no-such-field']},
                                  {'version': 1, 'keywords': 'oops'}])
def test_bad_subscription_metadata_keeps_manual_sources(empty_feeds, value):
    save_yaml(empty_feeds, {'news': [{'name': 'Manual', 'url': 'https://example.org/feed'}], 'subscriptions': value})
    assert len(sources.load().news) == 1


@pytest.mark.parametrize('form', [
    {'profiles': ['unknown']}, {'channels': ['invalid']}, {'keywords': 'x' * 501},
    {'keywords': ','.join(str(i) for i in range(21))}, {'keywords': 'x' * 61},
])
def test_invalid_preferences_do_not_write(client, empty_feeds, form):
    before = empty_feeds.read_bytes()
    response = client.post('/settings/interests', data={'sha': presets.read()[1], **form})
    assert response.status_code == 400
    assert empty_feeds.read_bytes() == before


def test_post_round_trip_and_csrf_and_conflict(client, empty_feeds):
    sha = presets.read()[1]
    response = client.post('/settings/interests', data={'sha': sha, 'profiles': ['ir', 'disasters'],
                           'channels': ['papers', 'news'], 'return_to': '/guide', 'keywords': 'flood'}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'].startswith('/settings/interests?saved=1')
    page = client.get(response.headers['location']).text
    assert '订阅已保存' in page and 'href="/guide">Back' in page
    before = empty_feeds.read_bytes()
    conflict = client.post('/settings/interests', data={'sha': sha, 'profiles': ['ir']})
    assert conflict.status_code == 409 and '别处更新' in conflict.text
    assert empty_feeds.read_bytes() == before
    response = client.post('/settings/interests', data={'profiles': ['ir']})
    assert response.status_code == 400
    client.headers.pop('HX-Request', None)
    response = client.post('/settings/interests', data={'sha': presets.read()[1]}, headers={'Origin': 'https://evil.example'})
    assert response.status_code == 403
    assert empty_feeds.read_bytes() == before


def test_return_path_is_local_and_entry_points_exist(client, empty_feeds):
    page = client.get('/settings/interests?return_to=https://evil.example').text
    assert 'name="return_to" value="/settings#feeds"' in page
    assert 'href="https://evil.example"' not in page
    assert '/settings/interests' in client.get('/guide').text
    assert '/settings/interests' in client.get('/settings').text
    # Avoid today's refresh machinery while checking the feed template entry point.
    from research_lab.web.context import templates
    assert '/settings/interests' in templates().env.loader.get_source(templates().env, 'pages/feed.html')[0]


def test_corrupt_yaml_shows_error_without_form(client, empty_feeds):
    empty_feeds.write_text('news: [broken:')
    response = client.get('/settings/interests')
    assert response.status_code == 400 and '推送配置无法读取' in response.text
    assert 'action="/settings/interests"' not in response.text


def test_existing_refresh_uses_selected_journals_and_interest_terms(conn, empty_feeds, monkeypatch):
    from research_lab.feeds import papers
    save(('security',), ('papers',), 'bargaining')
    seen = []
    def items(http, journal, since, **kwargs):
        seen.append(journal.abbr)
        return [{'DOI': f'10.1000/{journal.abbr}', 'title': ['Alliance bargaining'], 'created': {'date-parts': [[2026, 10, 12]]}}]
    monkeypatch.setattr(papers, 'crossref_items', items)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'results': []}))) as http:
        papers.refresh_papers(conn, http)
    assert seen == ['IS', 'ISQ']
    rows = conn.execute('SELECT radar FROM feed_papers').fetchall()
    assert len(rows) == 2 and all(r['radar'] for r in rows)
    count = conn.execute('SELECT COUNT(*) FROM ai_runs').fetchone()[0]
    save((), ())
    assert conn.execute('SELECT COUNT(*) FROM feed_papers').fetchone()[0] == 2
    assert conn.execute('SELECT COUNT(*) FROM ai_runs').fetchone()[0] == count


def test_padded_manual_disabled_sources_stay_disabled(empty_feeds):
    data = {'journals': [{'abbr': ' IO ', 'issn': [' 0020-8183 '], 'enabled': False},
                         {'abbr': 'Renamed', 'issn': (' 0020-8833 ',), 'enabled': False}],
            'news': [{'name': 'Renamed BBC', 'url': ' https://feeds.bbci.co.uk/news/world/rss.xml ', 'enabled': False},
                     {'name': ' UN News ', 'url': 'https://example.org/un', 'enabled': False}],
            'subscriptions': presets.validate(['ir'], ['papers', 'news'], '')}
    src = sources.parse(presets.expand(data))
    assert [j.abbr for j in src.journals] == ['RIS', 'EJIR']
    assert not src.news


def test_disabled_malformed_url_does_not_break_other_sources(client, empty_feeds):
    save_yaml(empty_feeds, {'journals': [], 'news': [
        {'name': 'Old broken source', 'url': 'https://[broken', 'enabled': False},
        {'name': 'Manual', 'url': 'https://example.org/feed'},
    ]})
    save()
    src = sources.load()
    assert len(src.journals) == 4
    assert [n.name for n in src.news] == ['Manual', 'BBC World', 'UN News']
    assert client.get('/settings/interests').status_code == 200
    assert presets.read()[0]['news'][0]['url'] == 'https://[broken'
