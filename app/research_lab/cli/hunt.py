"""`rl hunt`: this week's hunt list (method-layer §3.2, guardrail 8)."""
from __future__ import annotations

import typer

from . import open_db

app = typer.Typer(help="Weekly evidence hunt list", invoke_without_command=True)


@app.callback()
def hunt(project: str | None = typer.Option(None, "--project"), n: int = typer.Option(5, "-n")) -> None:
    """Top evidence cells to search this week: what to check and where to look."""
    from ..files import datafiles
    from ..method import hunt as hunt_mod
    conn = open_db()
    slug = project or str((datafiles.read("now") or {}).get("project") or "asean-centrality")
    items = hunt_mod.hunt_list(conn, slug, n=n)
    if not items:
        typer.echo(f"{slug}: nothing to hunt (no missing or weak cells).")
        return
    typer.echo(f"Hunt list · {slug}")
    for i, it in enumerate(items, 1):
        test = f" · {it['test_label']}" if it["test_label"] else ""
        typer.echo(f"{i}. {it['case_label']} × {it['code']}{test} · priority {it['priority']:g}{' · forced' if it['forced'] else ''}")
        typer.echo(f"   check: {it['implication'] or '(write the implication before looking)'}")
        typer.echo(f"   where: {it['where_to_look'] or '(add a where-to-look note to the cell)'}")
        if it["reason"]:
            typer.echo(f"   {it['reason']}")
