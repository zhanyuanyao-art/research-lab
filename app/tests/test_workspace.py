from pathlib import Path
from types import SimpleNamespace
import sqlite3

import pytest
from typer.testing import CliRunner

from research_lab import config, desktop
from research_lab.db.connection import get_setting
from research_lab.workspace import initialize


def test_initialize_is_empty_and_does_not_use_existing_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "get_paths", lambda: pytest.fail("read existing workspace"))
    other = tmp_path / "other"
    monkeypatch.setenv("RL_DATA_DIR", str(other))
    root = initialize(tmp_path / "new")
    assert desktop.looks_like_repo(root)
    assert not other.exists()
    assert not list((root / "_artifacts").iterdir())
    assert not list((root / "projects").iterdir())
    assert not (root / "app/.env").exists()
    from research_lab.files.yamlio import load_yaml
    assert load_yaml(root / "_data/profile.yml") == {"owner_name": "我"}
    with sqlite3.connect(root / "app/data/lab.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        assert get_setting(conn, "coach.active") is True
        assert get_setting(conn, "ai.paused") is True
        assert get_setting(conn, "dev.autofix") is False
        assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 0


def test_existing_directory_and_symlink_are_never_overwritten(tmp_path):
    root = tmp_path / "existing"
    root.mkdir()
    (root / "keep.txt").write_text("keep this")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    for target in (root, alias):
        with pytest.raises(FileExistsError):
            initialize(target)
    assert list(root.iterdir()) == [root / "keep.txt"]
    assert (root / "keep.txt").read_text() == "keep this"


def test_init_cli_never_opens_current_workspace(tmp_path, monkeypatch):
    from research_lab import cli
    monkeypatch.setattr(cli, "get_paths", lambda: pytest.fail("opened current workspace"))
    target = tmp_path / "a new workspace"
    result = CliRunner().invoke(cli.app, ["init", str(target)])
    assert result.exit_code == 0, result.output
    assert "RL_REPO_ROOT='" in result.output
    assert desktop.looks_like_repo(target)
    result = CliRunner().invoke(cli.app, ["init", str(target)])
    assert result.exit_code != 0
    assert "文件夹已存在" in result.output


def test_native_first_run_create_cancel_and_existing(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(FileDialog=SimpleNamespace(FOLDER="folder")))
    monkeypatch.setattr(desktop, "SUPPORT_DIR", tmp_path / "support")
    monkeypatch.setattr(desktop, "CONFIG_FILE", tmp_path / "support/config.json")
    opened = []
    monkeypatch.setenv("RL_DATA_DIR", str(tmp_path / "old-data"))
    monkeypatch.setattr(desktop.threading, "Thread", lambda target, args, **kw: SimpleNamespace(start=lambda: target(*args)))
    api = desktop.FirstRunApi(opened.append)
    api.window = SimpleNamespace(create_file_dialog=lambda *a: None)
    assert api.create() == "" and opened == []
    api.window = SimpleNamespace(create_file_dialog=lambda *a: [str(tmp_path)])
    assert api.create() == ""
    assert opened == [tmp_path / "Research Lab"]
    assert desktop.load_config()["repo_root"] == str(opened[0])
    import os
    assert os.environ["RL_DATA_DIR"] == str(opened[0] / "app/data")
    assert "已有" in api.create()
    assert len(opened) == 1


def test_new_workspace_confirmed_bug_does_not_launch_maintainer_pipeline(tmp_path, monkeypatch):
    from contextlib import closing
    from research_lab.agent import tasks, tools
    from research_lab.db.connection import connect
    from research_lab.devflow import cards

    root = initialize(tmp_path / "new")
    monkeypatch.setenv("RL_REPO_ROOT", str(root))
    monkeypatch.setenv("RL_DATA_DIR", str(root / "app/data"))
    monkeypatch.setenv("RL_AUTOFIX", "1")
    monkeypatch.setattr(config, "_paths", None)
    launches = []
    monkeypatch.setattr(tools, "AUTOFIX_LAUNCHER", lambda *args: launches.append(args))
    args = {"kind": "bug", "description": "首页的按钮点击后没有反应，预期打开课题。"}
    with closing(connect()) as conn:
        first = tasks.create(conn, "首页按钮点了没反应", skill="task")
        reply = tools.report_bug(conn, args, first)
        question = reply.split("then stop:\n", 1)[1].split("\nWhen they answer", 1)[0]
        conn.execute("UPDATE agent_tasks SET status='waiting', question=? WHERE id=?", (question, first))
        confirmed = tasks.create(conn, "好", skill="task", parent_id=first)
        reply = tools.report_bug(conn, args, confirmed)
    assert "pipeline did not start" in reply
    assert len(cards.all_cards()) == 1
    assert cards.all_cards()[0].status == "planned"
    assert launches == []


def test_new_workspace_keeps_existing_maintainer_preferences(tmp_path):
    from research_lab.db.connection import set_setting
    old = initialize(tmp_path / "existing")
    with sqlite3.connect(old / "app/data/lab.sqlite") as conn:
        set_setting(conn, "dev.autofix", True)
    initialize(tmp_path / "another")
    with sqlite3.connect(old / "app/data/lab.sqlite") as conn:
        assert get_setting(conn, "dev.autofix") is True


def test_first_project_and_issue_receive_keys_without_migration(tmp_path, monkeypatch):
    from research_lab.files import datafiles, issues
    from research_lab.files.yamlio import load_yaml
    root = initialize(tmp_path / "new")
    monkeypatch.setenv("RL_REPO_ROOT", str(root))
    monkeypatch.setenv("RL_DATA_DIR", str(root / "app/data"))
    monkeypatch.setattr(config, "_paths", None)
    assert load_yaml(root / "_data/issue_keys.yml") == {"last": {}, "refs": {}, "aliases": {}}
    datafiles.add_direction({"slug": "demo", "name": "示例研究", "key": "DEMO"})
    stage = datafiles.read("stages")[0]["key"]
    datafiles.add_project({"slug": "first", "name": "第一个课题", "question": "如何核验？",
                           "direction": "demo", "stage": stage})
    parent = "PRJ-first"
    ref = issues.create("读一篇文献", "first", parent, issues.revision(parent))
    assert issues.key_of(parent) == "DEMO-0001"
    assert issues.key_of(ref) == "DEMO-0002"
    assert issues.resolve("DEMO-0002") == ref
    stored = (root / "_data/issue_keys.yml").read_bytes()
    initialize(tmp_path / "another")
    assert (root / "_data/issue_keys.yml").read_bytes() == stored
