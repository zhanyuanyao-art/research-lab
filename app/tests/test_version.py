"""Version is declared twice (pyproject for packaging, config for the running app); keep them equal."""
import json
import re

from research_lab import config


def test_version_single_source():
    text = (config.find_repo_root() / "pyproject.toml").read_text()
    assert re.search(r'^version = "([^"]+)"', text, re.M).group(1) == config.APP_VERSION


def test_build_label_from_checkout_and_release(tmp_path, monkeypatch):
    label = config.build_label()
    assert label.startswith(config.APP_VERSION)
    stamp = tmp_path / "build.json"
    stamp.write_text(json.dumps({"commit": "abc1234", "date": "2026-10-01", "branch": "main", "dirty": False}))
    monkeypatch.setattr(config, "_BUILD_FILE", stamp)
    assert config.build_label() == f"{config.APP_VERSION} · abc1234 · 2026-10-01"
    assert config.build_info()["source"] == "release"


def test_worktree_marker_redirects_content_root(tmp_path, monkeypatch):
    """A worktree with .rl-content-root uses the main checkout for content, its own package for code."""
    monkeypatch.delenv("RL_REPO_ROOT", raising=False)
    main = tmp_path / "main"; (main / "app").mkdir(parents=True); (main / "pyproject.toml").write_text("x")
    wt = tmp_path / "wt"; (wt / "app" / "research_lab").mkdir(parents=True); (wt / "pyproject.toml").write_text("x")
    (wt / config.CONTENT_ROOT_MARKER).write_text(str(main) + "\n")
    assert config.find_repo_root(wt / "app" / "research_lab" / "config.py") == main.resolve()
    (wt / config.CONTENT_ROOT_MARKER).write_text(str(tmp_path / "gone"))
    assert config.find_repo_root(wt / "app" / "research_lab" / "config.py") == wt.resolve()
