from contextlib import closing
import sqlite3

from research_lab import config, desktop
from research_lab.db.connection import connect, migrate, get_setting, set_setting


def test_upgrade_backup_precedes_migration_and_is_not_replaced(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, 'build_identity', lambda: 'trial:next')
    paths = config.get_paths()
    with closing(connect()) as conn:
        conn.execute('CREATE TABLE precious (body TEXT)')
        conn.execute("INSERT INTO precious VALUES ('synthetic research')")
        snapshot = desktop.prepare_upgrade(conn, paths)
        migrations = tmp_path / 'migrations'
        migrations.mkdir()
        (migrations / '001_new.sql').write_text('CREATE TABLE new_table (id INTEGER);')
        migrate(conn, migrations)
        with closing(sqlite3.connect(snapshot)) as old:
            assert old.execute('SELECT body FROM precious').fetchone()[0] == 'synthetic research'
            assert not old.execute("SELECT 1 FROM sqlite_master WHERE name='new_table'").fetchone()
        original = snapshot.read_bytes()
        # A failed start has not advanced the marker; retry keeps the first backup.
        assert desktop.prepare_upgrade(conn, paths) != snapshot
        assert snapshot.read_bytes() == original
        desktop.record_build(paths)
        assert desktop.prepare_upgrade(conn, paths) is None
        conn.execute('UPDATE precious SET body=?', ('still here',))
        monkeypatch.setattr(desktop, 'build_identity', lambda: 'trial:later')
        assert desktop.prepare_upgrade(conn, paths) != snapshot
        assert snapshot.read_bytes() == original


def test_clean_start_and_preferences_survive_upgrade(repo, monkeypatch):
    monkeypatch.setattr(desktop, 'build_identity', lambda: 'trial:new')
    paths = config.get_paths()
    with closing(connect()) as conn:
        assert desktop.prepare_upgrade(conn, paths) is None
        migrate(conn)
        set_setting(conn, 'ui.theme', 'dark')
        set_setting(conn, 'coach.active', False)
        snapshot = desktop.prepare_upgrade(conn, paths)
        migrate(conn)
        assert get_setting(conn, 'ui.theme') == 'dark'
        assert get_setting(conn, 'coach.active') is False
        assert snapshot.is_file()


def test_backup_failure_does_not_mark_upgrade_complete(repo, monkeypatch):
    import pytest
    paths = config.get_paths()
    with closing(connect()) as conn:
        migrate(conn)
        paths.backups.parent.mkdir(parents=True, exist_ok=True)
        paths.backups.rmdir()
        paths.backups.write_text('not a folder')
        with pytest.raises(FileExistsError):
            desktop.prepare_upgrade(conn, paths)
        assert not (paths.data / 'desktop-build.json').exists()


def test_trial_restart_ignores_an_inherited_database_override(repo, tmp_path, monkeypatch):
    import pytest
    from research_lab.db import connection
    monkeypatch.setenv('RL_DISTRIBUTION', 'trial')
    monkeypatch.setenv('RL_DATA_DIR', str(tmp_path / 'unrelated'))
    seen = []
    def connect():
        seen.append(config.get_paths().data)
        raise RuntimeError('stop before server')
    monkeypatch.setattr(connection, 'connect', connect)
    with pytest.raises(RuntimeError, match='stop before server'):
        desktop.Server(repo, 0).start()
    assert seen == [repo / 'app/data']
    assert not (tmp_path / 'unrelated').exists()
