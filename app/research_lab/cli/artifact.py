from __future__ import annotations

import typer

from ..config import get_paths
from ..files import artifacts, datafiles

app = typer.Typer(help="Weekly artifacts (Markdown in _artifacts/)")


@app.command("new")
def new(type_: str = typer.Argument(..., metavar="TYPE"), slug: str = typer.Argument(...),
        project: str | None = typer.Option(None, "--project"), title: str | None = typer.Option(None, "--title"),
        level: str = typer.Option("seed", "--level")) -> None:
    """Create this week's artifact from templates/artifact.md."""
    types = [t["key"] for t in datafiles.read("types") or []]
    if types and type_ not in types:
        raise typer.BadParameter(f"type must be one of {', '.join(types)}")
    project = project or str(datafiles.read("now")["project"])
    path = artifacts.create(type_, slug, project, title=title, level=level)
    typer.echo(f"{path.relative_to(get_paths().root)}  ({artifacts.load(path).ref})")


@app.command("index")
def index() -> None:
    from . import open_db
    conn = open_db()
    n = artifacts.index(conn)
    rows = conn.execute("SELECT ref, week, type, level, counts FROM artifacts ORDER BY ref").fetchall()
    for r in rows:
        typer.echo(f"{r['ref']}  {r['week']}  {r['type']:<16} {r['level']:<10} {'counts' if r['counts'] else 'draft'}")
    typer.echo(f"({n} re-indexed)")


@app.command("link")
def link(src: str, dst: str) -> None:
    """Add DST to SRC's related list."""
    from . import open_db
    conn = open_db()
    artifacts.index(conn)
    row = conn.execute("SELECT path FROM artifacts WHERE ref = ?", (src,)).fetchone()
    if not row:
        raise typer.BadParameter(f"unknown artifact {src}")
    if not conn.execute("SELECT 1 FROM artifacts WHERE ref = ?", (dst,)).fetchone():
        raise typer.BadParameter(f"unknown artifact {dst}")
    a = artifacts.load(get_paths().root / row["path"])
    changed = artifacts.add_related(a, dst)
    if changed:
        artifacts.save(a)
    typer.echo("linked" if changed else "already linked")
