"""Synthetic research fixtures with isolated SQLite and an allowed-host TestClient."""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "content"


def pytest_configure(config):
    # Collection-time imports must not discover a developer workspace or credentials.
    sandbox = tempfile.TemporaryDirectory(prefix="rl-test-session-")
    config._rl_sandbox = sandbox
    shutil.copyfile(Path(__file__).resolve().parents[2] / "pyproject.toml",
                    Path(sandbox.name) / "pyproject.toml")
    variables = {"RL_REPO_ROOT": sandbox.name, "RL_DATA_DIR": str(Path(sandbox.name) / "app/data"),
                 "PYTHON_DOTENV_DISABLED": "1", "RL_CLAUDE_BIN": "none", "RL_AI_WORKER": "0",
                 "RL_FEEDS_AUTO": "0", "RL_PM_AUTO": "0", "RL_AUTOFIX": "0",
                 "DEEPSEEK_API_KEY": "", "BRAVE_SEARCH_API_KEY": ""}
    config._rl_previous_env = {key: os.environ.get(key) for key in variables}
    os.environ.update(variables)


def pytest_unconfigure(config):
    for key, value in getattr(config, "_rl_previous_env", {}).items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    sandbox = getattr(config, "_rl_sandbox", None)
    if sandbox is not None:
        sandbox.cleanup()


CONTENT = ("_artifacts", "_data", "templates", "assets", "projects")


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    from research_lab.feeds import net
    real_resolver = net.resolve_addresses
    # MockTransport does not resolve DNS; keep metadata tests offline too.
    monkeypatch.setattr(net, "resolve_addresses", lambda host, port: ["93.184.216.34"])
    return real_resolver


@pytest.fixture(scope="session")
def content_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Keep tests independent of both committed and uncommitted personal research."""
    snap = tmp_path_factory.mktemp("content")
    for name in CONTENT:
        src = FIXTURE_ROOT / name
        if src.exists():
            shutil.copytree(src, snap / name)
    return snap


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content_snapshot: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for name in CONTENT:
        src = content_snapshot / name
        if src.exists():
            shutil.copytree(src, root / name)
    (root / "_harvests").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / "app").mkdir()
    (root / "_config.yml").write_text("exclude:\n  - app\n")
    (root / ".gitignore").write_text("app/data/\n")
    monkeypatch.setenv("RL_REPO_ROOT", str(root))
    monkeypatch.delenv("RL_DATA_DIR", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("RL_CLAUDE_BIN", "none")  # never start the real Claude Code CLI from tests
    monkeypatch.setenv("RL_FEEDS_AUTO", "0")  # never start the six-hourly feed refresh (network) from tests
    monkeypatch.setenv("RL_AI_WORKER", "0")  # no background AI worker from the app: tests start their own
    monkeypatch.setenv("RL_PM_AUTO", "0")  # no Friday progress timer, even with RL_PM_AUTO=1 exported in the shell
    monkeypatch.setenv("RL_AUTOFIX", "0")  # nothing a test does may start the small-bug autofix pipeline
    import research_lab.config as cfg
    cfg._paths = None
    cfg.get_paths().ensure_dirs()
    yield root
    cfg._paths = None


@pytest.fixture()
def conn(repo: Path) -> sqlite3.Connection:
    from research_lab.db.connection import connect, migrate
    c = connect()
    migrate(c)
    yield c
    c.close()


@pytest.fixture()
def client(repo: Path):
    from fastapi.testclient import TestClient
    from research_lab.web.app import create_app
    with TestClient(create_app(), base_url="http://127.0.0.1:4100") as c:
        c.headers["HX-Request"] = "true"   # every mutation in tests passes the guard
        yield c


@pytest.fixture()
def sample_pdf(tmp_path: Path) -> Path:
    """A small real PDF with a text layer, a DOI line and page labels, built with PyMuPDF."""
    import pymupdf
    doc = pymupdf.open()
    texts = [
        "Who Convenes Asia?\nForum attendance and the durability of ASEAN-led institutions\nL. Tan\n"
        "Abstract\nGreat powers keep attending ASEAN-led forums. doi:10.1234/rl.2023.001\n",
        "Introduction\nBetween 2005 and 2022 US leader-level attendance at the EAS lapsed in 5 of 17 summits, "
        "yet no participant proposed moving the chair outside ASEAN.\n",
        "Conclusion\nConvening power is a residual of mutual veto.\nReferences\nAcharya, A. 2001.\n",
    ]
    for t in texts:
        page = doc.new_page()
        page.insert_text((72, 72), t, fontsize=11)
    doc.set_page_labels([{"startpage": 0, "prefix": "", "style": "D", "firstpagenum": 244}])
    p = tmp_path / "tan2023.pdf"
    doc.save(p)
    doc.close()
    return p
