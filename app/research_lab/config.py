"""Paths, environment and settings shared by CLI and web.

The repo root is found by walking up from this file to the directory holding
pyproject.toml; tests override it with RL_REPO_ROOT.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

HOST = "127.0.0.1"
PORT = 4100
# Single source of truth is pyproject.toml; tests assert this constant matches it (the packaged app has
# no pyproject to read). Bump both together, then tag v<version>.
APP_VERSION = "1.3.46"

_BUILD_FILE = Path(__file__).resolve().parent / "web" / "static" / "build.json"


def build_info() -> dict:
    """What this running copy was built from. packaging/macos/build.sh writes build.json into the bundle
    (commit, date, branch, dirty, source=release|worktree); from a checkout it is derived from git."""
    import json
    import subprocess
    if _BUILD_FILE.exists():
        try:
            return {"source": "release", **json.loads(_BUILD_FILE.read_text())}
        except (OSError, ValueError):
            pass
    try:
        code = Path(__file__).resolve().parent  # the checkout the code runs from, not the content root
        run = lambda *a: subprocess.run(["git", "-C", str(code), *a], capture_output=True, text=True, timeout=3).stdout.strip()
        commit = run("rev-parse", "--short", "HEAD")
        if not commit:
            return {"source": "unknown"}
        return {"source": "checkout", "commit": commit, "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
                "dirty": bool(run("status", "--porcelain", "--untracked-files=no")),
                "date": run("log", "-1", "--format=%cs")}
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return {"source": "unknown"}


def build_label() -> str:
    """'0.4.0 · 1568bb0 · 2026-10-01' for a release, '0.4.0 · 源码 1568bb0+' from a checkout with local edits."""
    info = build_info()
    commit = info.get("commit", "")
    if info.get("source") == "release":
        return " · ".join(x for x in (APP_VERSION, commit + ("+" if info.get("dirty") else ""), info.get("date", "")) if x)
    if commit:
        return f"{APP_VERSION} · 源码 {commit}{'+' if info.get('dirty') else ''}"
    return APP_VERSION


CONTENT_ROOT_MARKER = ".rl-content-root"


def find_repo_root(start: Path | None = None) -> Path:
    """The content root: research files (_artifacts, _data, projects/), app/data and app/.env live here.
    Code, templates, static files and migrations always come from this package, wherever it is.

    A git worktree made by scripts/agent-worktree.sh carries a `.rl-content-root` file naming the main
    checkout, so a session developing on a branch runs its own code against the one real library instead
    of an empty copy. RL_REPO_ROOT (tests) overrides both."""
    override = os.environ.get("RL_REPO_ROOT")
    if override:
        return Path(override).resolve()
    here = (start or Path(__file__)).resolve()
    for parent in [here, *here.parents]:
        if (parent / "pyproject.toml").exists() and (parent / "app").is_dir():
            marker = parent / CONTENT_ROOT_MARKER
            if marker.exists():
                target = Path(marker.read_text().strip()).expanduser()
                if (target / "pyproject.toml").exists():
                    return target.resolve()
            return parent
    raise RuntimeError("research-lab repo root not found (no pyproject.toml above %s)" % here)


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def app(self) -> Path: return self.root / "app"
    @property
    def data(self) -> Path: return Path(os.environ.get("RL_DATA_DIR") or (self.app / "data"))
    @property
    def db(self) -> Path: return self.data / "lab.sqlite"
    @property
    def papers(self) -> Path: return self.data / "papers"
    @property
    def ai_log(self) -> Path: return self.data / "ai-log"
    @property
    def backups(self) -> Path: return self.data / "backups"
    @property
    def cache(self) -> Path: return self.data / "cache"
    @property
    def logs(self) -> Path: return self.data / "logs"
    @property
    def records(self) -> Path: return self.app / "records"
    @property
    def env_file(self) -> Path: return self.app / ".env"
    # research content (source of truth)
    @property
    def artifacts(self) -> Path: return self.root / "_artifacts"
    @property
    def harvests(self) -> Path: return self.root / "_harvests"
    @property
    def data_yml(self) -> Path: return self.root / "_data"
    @property
    def projects(self) -> Path: return self.root / "projects"
    @property
    def templates(self) -> Path: return self.root / "templates"
    @property
    def site_assets(self) -> Path: return self.root / "assets"
    @property
    def migrations(self) -> Path: return Path(__file__).parent / "db" / "migrations"
    @property
    def web_templates(self) -> Path: return Path(__file__).parent / "web" / "templates"
    @property
    def web_static(self) -> Path: return Path(__file__).parent / "web" / "static"

    def ensure_dirs(self) -> None:
        for d in (self.data, self.papers, self.ai_log, self.backups, self.cache, self.logs,
                  self.cache / "incoming", self.harvests, self.artifacts):
            d.mkdir(parents=True, exist_ok=True)


_paths: Paths | None = None


def get_paths() -> Paths:
    global _paths
    if _paths is None or os.environ.get("RL_REPO_ROOT"):
        _paths = Paths(find_repo_root())
        load_dotenv(_paths.env_file, override=False)
    return _paths


def env(name: str, default: str | None = None) -> str | None:
    get_paths()
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def has_deepseek_key() -> bool:
    return bool(env("DEEPSEEK_API_KEY"))
