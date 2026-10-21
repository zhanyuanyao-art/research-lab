"""Create an empty workspace without copying a developer's research or credentials."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from .db.connection import migrate, set_setting
from .files.yamlio import dump_yaml
from .i18n import LEVEL, STAGE, TYPE


def initialize(target: Path) -> Path:
    """Require a new directory so a repeated setup can never overwrite research."""
    target = target.expanduser().absolute()
    target.mkdir(parents=True, exist_ok=False)
    for name in ("_data", "_artifacts", "_harvests", "projects", "templates/project", "app/data"):
        (target / name).mkdir(parents=True)
    defaults = {
        "directions": [], "projects": [], "tasks": [], "archive": [],
        "profile": {"owner_name": "我"},
        "issue_keys": {"last": {}, "refs": {}, "aliases": {}},
        "now": {"project": "", "question": "", "this_week": "", "next_week": ""},
        "year": {"goals": []}, "feeds": {"journals": [], "news": []},
        "types": [{"key": k, "label": v} for k, v in TYPE.items()],
        "levels": [{"key": k, "label": v} for k, v in LEVEL.items()],
        "stages": [{"key": k, "label": v} for k, v in STAGE.items()],
    }
    for name, value in defaults.items():
        (target / "_data" / f"{name}.yml").write_text(dump_yaml(value), encoding="utf-8")
    (target / "templates/artifact.md").write_text(
        "---\nstatus: draft\nversion: v0.1\nrelated: []\n---\n"
        "\n## What did I figure out\n\n## Claim\n\n## Evidence\n\n## Next question\n\n## AI notes\n",
        encoding="utf-8")
    for name, body in {
        "index.md": "# <Project name>\n\n## Question\n\n...\n",
        "literature.md": "# 文献\n", "evidence.md": "# 证据\n",
    }.items():
        (target / "templates/project" / name).write_text(body, encoding="utf-8")
    (target / ".gitignore").write_text("app/data/\napp/.env\nprivate/\n.DS_Store\n", encoding="utf-8")
    (target / "pyproject.toml").write_text('[project]\nname = "research-workspace"\n', encoding="utf-8")
    (target / "_config.yml").write_text("exclude:\n  - app\n  - private\n", encoding="utf-8")
    # Use the new database explicitly; RL_DATA_DIR may still name an existing workspace.
    with closing(sqlite3.connect(target / "app/data/lab.sqlite", isolation_level=None)) as conn:
        migrate(conn, Path(__file__).parent / "db/migrations")
        set_setting(conn, "coach.active", True)
        set_setting(conn, "ai.paused", True)
        # A research workspace does not opt in to maintainer releases or installs.
        set_setting(conn, "dev.autofix", False)
        set_setting(conn, "feeds.autoimport_oa", False)
    return target.resolve()
