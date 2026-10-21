import json
import sys

from fastapi.testclient import TestClient

from research_lab import desktop, distribution


def test_baked_trial_cannot_be_unlocked_by_environment(tmp_path, monkeypatch):
    stamp = tmp_path / 'build.json'
    stamp.write_text(json.dumps({'distribution': 'trial'}))
    monkeypatch.setattr(distribution, 'BUILD_FILE', stamp)
    monkeypatch.setenv('RL_DISTRIBUTION', 'maintainer')
    assert distribution.is_trial()
    stamp.unlink()
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    assert distribution.is_trial()


def test_trial_requires_explicit_workspace(tmp_path, repo, monkeypatch):
    monkeypatch.setenv('RL_DISTRIBUTION', 'trial')
    monkeypatch.delenv('RL_REPO_ROOT')
    monkeypatch.setattr(desktop, 'CONFIG_FILE', tmp_path / 'config.json')
    monkeypatch.setattr(desktop, 'source_checkout', lambda: (_ for _ in ()).throw(AssertionError('checkout consulted')))
    assert desktop.resolve_repo() is None
    desktop.CONFIG_FILE.write_text(json.dumps({'repo_root': str(repo)}))
    assert desktop.resolve_repo() == repo


def test_trial_routes_and_autofix_are_closed(repo, conn, monkeypatch):
    from research_lab.web.app import create_app
    from research_lab.agent import tools
    from research_lab.devflow import autofix, cards
    from research_lab.db.connection import set_setting
    monkeypatch.setenv('RL_DISTRIBUTION', 'trial')
    set_setting(conn, 'dev.autofix', True)
    monkeypatch.setattr(cards, 'new', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('created a card')))
    assert not autofix.setting_enabled()
    assert not tools._autofix_enabled(conn)
    assert '未发送' in tools.report_bug(conn, {'description': 'test'}, 'confirmed-request')
    with TestClient(create_app(), base_url='http://127.0.0.1:4100') as client:
        for path in ('/dev', '/digest', '/dev/release', '/dev/card/new'):
            assert client.get(path).status_code == 404
            assert client.post(path, headers={'Origin': 'http://127.0.0.1:4100'}).status_code == 404
        assert 'href="/dev"' not in client.get('/settings').text
        assert client.get('/papers').status_code == 200


def test_trial_cli_does_not_register_maintainer_commands(monkeypatch):
    import importlib
    from research_lab import cli
    monkeypatch.setenv('RL_DISTRIBUTION', 'trial')
    try:
        importlib.reload(cli)
        names = {item.name for item in cli.app.registered_groups}
        assert not {'dev', 'launchd'} & names
        assert {'paper', 'data'} <= names
    finally:
        monkeypatch.delenv('RL_DISTRIBUTION')
        importlib.reload(cli)


def test_bibliography_import_survives_without_legacy_digest(conn, monkeypatch):
    from types import SimpleNamespace
    from research_lab.intake import identifiers, metadata
    monkeypatch.setenv('RL_DISTRIBUTION', 'trial')
    monkeypatch.setitem(sys.modules, 'research_lab.digest', None)
    monkeypatch.setattr(identifiers, 'oa_pdf_url', lambda *a: None)
    result = SimpleNamespace(chosen=SimpleNamespace(title='Synthetic bibliography', authors=['Demo'],
                            year=2026, venue='Test', kind='article'), abstract=None, status='verified',
                            crossref=None, openalex=None, openalex_id=None)
    monkeypatch.setattr(metadata, 'fetch_for_doi', lambda *a, **kw: result)
    added = identifiers.add(conn, identifiers.parse_one('10.1234/trial'))
    assert added.state == 'biblio'
    assert conn.execute('SELECT title FROM papers WHERE id=?', (added.paper_id,)).fetchone()[0] == 'Synthetic bibliography'
    assert identifiers.metadata_only(conn, '10.1234/trial', project=None, direction=None, http=None) == added.paper_id
