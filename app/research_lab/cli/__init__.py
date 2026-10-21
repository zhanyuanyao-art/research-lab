"""`rl` command line. Sub-apps live in research_lab/cli/<name>.py and expose `app`;
missing modules are skipped so modules can be added without editing this file."""
from __future__ import annotations

import os

import importlib
import subprocess
import sys
import webbrowser
from pathlib import Path

import typer

from ..config import HOST, PORT, APP_VERSION, get_paths, has_deepseek_key
from ..db.connection import backup as db_backup, connect, migrate

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Research Lab — local research workbench")
db_app = typer.Typer(help="Database maintenance")
app.add_typer(db_app, name="db")

SUB_APPS = ("artifact", "intake", "paper", "excerpt", "related", "check", "hunt", "agent", "ai", "launchd", "chat", "data", "feeds", "dev",
            "tasks", "issues")


def open_db():
    conn = connect()
    migrate(conn)
    return conn


@app.callback()
def _main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand != "init":
        get_paths().ensure_dirs()


@app.command("init")
def init_workspace(path: Path = typer.Argument(..., help="New workspace folder")) -> None:
    """Create a blank workspace; existing folders are never overwritten."""
    from ..workspace import initialize
    import shlex
    try:
        root = initialize(path)
    except FileExistsError:
        raise typer.BadParameter("文件夹已存在，请选择一个新路径。") from None
    except OSError:
        raise typer.BadParameter("无法创建工作区，请检查位置和写入权限。") from None
    typer.echo(f"工作区已创建：{root}")
    typer.echo(f"RL_REPO_ROOT={shlex.quote(str(root))} RL_DATA_DIR={shlex.quote(str(root / 'app/data'))} uv run rl serve")


@app.command()
def serve(port: int = PORT, reload: bool = False, no_open: bool = typer.Option(False, "--no-open")) -> None:
    """Start the web app on 127.0.0.1 and open it in the browser."""
    import uvicorn
    conn = open_db()
    paths = get_paths()
    last = sorted(paths.backups.glob("lab-*.sqlite"))
    import time
    if not last or time.time() - last[-1].stat().st_mtime > 86400:
        db_backup(conn)
    conn.close()
    url = f"http://{HOST}:{port}/"
    typer.echo(f"Research Lab {APP_VERSION} → {url}")
    if not no_open:
        webbrowser.open(url)
    os.environ["RL_PORT"] = str(port)  # the factory below cannot take arguments
    uvicorn.run("research_lab.web.app:create_app", factory=True, host=HOST, port=port, reload=reload,
                reload_dirs=[str(paths.app)] if reload else None, log_level="warning")


@app.command()
def desktop() -> None:
    """Open Research Lab in a native Mac window (the same app the .app bundle runs)."""
    from ..desktop import main as desktop_main
    desktop_main()


@app.command()
def doctor() -> None:
    """Check the environment: python, db, .env, Jekyll exclude, write access."""
    paths = get_paths()
    ok = True

    def line(good: bool, msg: str) -> None:
        nonlocal ok
        ok = ok and good
        typer.echo(("✓ " if good else "✗ ") + msg)

    line(sys.version_info >= (3, 12), f"python {sys.version.split()[0]}")
    line(paths.root.joinpath("_artifacts").is_dir(), f"repo root {paths.root}")
    try:
        conn = open_db()
        n = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        line(True, f"database {paths.db} ({n} tables)")
    except Exception as e:  # noqa: BLE001
        line(False, f"database: {e}")
    line(paths.env_file.exists(), f"app/.env {'present' if paths.env_file.exists() else 'missing (copy app/.env.example)'}")
    typer.echo(("✓ " if has_deepseek_key() else "· ") + ("DEEPSEEK_API_KEY set → DeepSeek mode" if has_deepseek_key() else "no DeepSeek key → Claude Code agent mode"))
    cfg = (paths.root / "_config.yml").read_text()
    line("- app" in cfg, "_config.yml excludes app/ from the Jekyll build")
    line(paths.root.joinpath(".gitignore").read_text().find("app/data/") >= 0, ".gitignore covers app/data/")
    for d in (paths.data, paths.artifacts, paths.data_yml):
        line(d.exists() and __import__("os").access(d, 2), f"writable {d.relative_to(paths.root) if d.is_relative_to(paths.root) else d}")
    vendor = paths.web_static / "vendor"
    line((vendor / "htmx.min.js").exists() and (vendor / "cytoscape.min.js").exists(), "vendored htmx + cytoscape")
    raise typer.Exit(0 if ok else 1)


@db_app.command("migrate")
def db_migrate() -> None:
    conn = connect()
    applied = migrate(conn)
    typer.echo(f"applied {applied or 'nothing'}")


@db_app.command("backup")
def db_backup_cmd(to: Path | None = typer.Option(None, "--to")) -> None:
    conn = open_db()
    target = db_backup(conn)
    duck = get_paths().data / "lab.duckdb"
    if duck.exists():
        import shutil
        shutil.copy2(duck, target.with_name(target.stem + ".duckdb"))
    if to:
        import shutil
        to.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, to / target.name)
        for sub in ("papers", "datasets", "figures"):
            src = get_paths().data / sub
            if src.exists():
                shutil.copytree(src, to / sub, dirs_exist_ok=True)
        if duck.exists():
            shutil.copy2(duck, to / duck.name)
    typer.echo(f"backup → {target}")


@db_app.command("export")
def db_export(out: Path | None = typer.Option(None, "--out")) -> None:
    """Deterministic JSONL export of non-private records for git history."""
    import json
    conn = open_db()
    out = out or get_paths().records
    out.mkdir(parents=True, exist_ok=True)
    tables = {"papers": "private = 0", "excerpts": "private = 0", "claims": "1=1", "cases": "1=1", "camps": "1=1",
              "concepts": "1=1", "evidence_cells": "1=1", "checks": "1=1", "associations": "status != 'rejected'"}
    for t, where in tables.items():
        rows = conn.execute(f"SELECT * FROM {t} WHERE {where} ORDER BY 1").fetchall()
        with (out / f"{t}.jsonl").open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(dict(r), ensure_ascii=False, sort_keys=True) + "\n")
        typer.echo(f"{t}: {len(rows)}")


@db_app.command("reindex")
def db_reindex() -> None:
    from ..files import artifacts
    conn = open_db()
    n = artifacts.index(conn)
    for t in ("excerpts_fts", "artifacts_fts", "papers_fts", "pages_fts"):
        conn.execute(f"INSERT INTO {t}({t}) VALUES ('rebuild')")
    typer.echo(f"artifacts indexed: {n}; FTS rebuilt")


from ..distribution import is_trial

for _name in SUB_APPS:
    if is_trial() and _name in {"dev", "launchd"}:
        continue
    try:
        _mod = importlib.import_module(f"{__name__}.{_name}")
    except ModuleNotFoundError as e:
        if e.name != f"{__name__}.{_name}":
            raise
        continue
    app.add_typer(_mod.app, name=_name)
