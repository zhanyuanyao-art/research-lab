from __future__ import annotations

import json
from pathlib import Path

import typer

from .. import excerpts as ex

app = typer.Typer(help="Excerpts: capture, list, show, add to this week's artifact", no_args_is_help=True)


@app.command("add")
def add(paper: str = typer.Option(..., "--paper", help="paper id"),
        locator: str = typer.Option(..., "--locator", help="page, paragraph or timestamp"),
        quote: str | None = typer.Option(None, "--quote", help="verbatim text pasted from the source"),
        quote_file: Path | None = typer.Option(None, "--quote-file", exists=True, dir_okay=False),
        note: str = typer.Option(..., "--note", help="your words: why this matters"),
        kind: str = typer.Option("quote", "--kind"),
        use: list[str] = typer.Option([], "--use"), case: list[str] = typer.Option([], "--case"),
        concept: list[str] = typer.Option([], "--concept"), camp: str | None = typer.Option(None, "--camp"),
        strength: str | None = typer.Option(None, "--strength"),
        source_type: str = typer.Option("secondary-scholarly", "--source-type"),
        project: str | None = typer.Option(None, "--project"), page_index: int | None = typer.Option(None, "--page-index"),
        associate: bool = typer.Option(True, "--associate/--no-associate")) -> None:
    """Add an excerpt. The quote must come from you (pasted or from a file), never from an AI job."""
    from . import open_db
    if (quote is None) == (quote_file is None):
        raise typer.BadParameter("pass exactly one of --quote or --quote-file")
    text = quote if quote is not None else quote_file.read_text(encoding="utf-8")
    conn = open_db()
    try:
        eid = ex.create(conn, paper_id=paper, quote=text, my_note=note, locator=locator, kind=kind, uses=use, camp=camp,
                        cases=case, concepts=concept, strength=strength, source_type=source_type, project=project,
                        page_index=page_index)
    except ex.ExcerptError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    row = ex.get(conn, eid)
    typer.echo(f"{eid}  quote {'verified' if row['quote_verified'] else 'unverified'}  project {row['project_slug'] or '-'}")
    if associate:
        job = ex.enqueue_associate(conn, eid)
        if job:
            typer.echo(f"excerpt.associate queued: {job}")


@app.command("list")
def list_(status: str | None = typer.Option(None, "--status"), paper: str | None = typer.Option(None, "--paper"),
          limit: int = typer.Option(50, "--limit")) -> None:
    from . import open_db
    conn = open_db()
    for r in ex.list_excerpts(conn, status=status, paper=paper, limit=limit):
        typer.echo(f"{r['id']}  {r['status']:<9} {r['paper_id']}  {ex.locator_label(r['locator']):<10} {ex.one_line(r['quote'], 70)}")


@app.command("show")
def show(excerpt_id: str) -> None:
    from . import open_db
    from ..ai.jobs import excerpt_associate as job_mod
    conn = open_db()
    row = ex.get(conn, excerpt_id)
    if row is None:
        typer.echo(f"no excerpt {excerpt_id}", err=True)
        raise typer.Exit(1)
    t = ex.tags(conn, excerpt_id)
    typer.echo(f"{row['id']} · {ex.author_year(row['paper_authors'], row['paper_year'], row['paper_title'])}, "
               f"{ex.locator_label(row['locator'])} · {row['kind']} · {row['status']} · "
               f"{'verified' if row['quote_verified'] else 'unverified'}")
    typer.echo(f"\n  “{row['quote']}”\n\nNote: {row['my_note']}")
    typer.echo(f"uses {', '.join(t['uses']) or '-'} · camp {t['camp'] or '-'} · cases {', '.join(t['cases']) or '-'} · "
               f"concepts {', '.join(t['concepts']) or '-'}")
    for w in ex.check(conn, excerpt_id):
        typer.echo(f"▲ {w}")
    p = job_mod.panel(conn, excerpt_id)
    if not p["job"]:
        return
    typer.echo(f"\nassociation job {p['job']['id']}: {p['job']['status']}")
    if not p["done"]:
        return
    for item in p["links"] + p["tags"]:
        a = item["assoc"]
        typer.echo(f"  [{a['id']}] {a['status']:<8} {a['rel']:<12} {item['title']}" + (f" · {a['reason']}" if a["reason"] else ""))
    if p["claim"]:
        typer.echo(f"  claim [{p['claim']['id']}] {p['claim']['status']}: {p['claim']['statement']}")
    if p["next_question"]:
        typer.echo(f"  next question: {p['next_question']}")
    for tg in p["targets"]:
        typer.echo(f"  can go into ({tg['kind']}): {tg['ref']} · {tg['note']}")


@app.command("add-to-artifact")
def add_to_artifact(excerpt_id: str, section: str = typer.Option("Evidence", "--section"),
                    artifact: str | None = typer.Option(None, "--artifact")) -> None:
    """Append the excerpt's marker line to this week's artifact (creates a Seed if the week has none)."""
    from . import open_db
    from ..config import get_paths
    conn = open_db()
    try:
        path, ref = ex.add_to_artifact(conn, excerpt_id, section=section, artifact_ref=artifact)
    except ex.ExcerptError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"{ref} § {section} ← {excerpt_id}  ({path.relative_to(get_paths().root)})")


@app.command("reassociate")
def reassociate(excerpt_id: str) -> None:
    from . import open_db
    conn = open_db()
    if ex.get(conn, excerpt_id) is None:
        typer.echo(f"no excerpt {excerpt_id}", err=True)
        raise typer.Exit(1)
    typer.echo(ex.enqueue_associate(conn, excerpt_id) or "already queued")
