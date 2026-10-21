from __future__ import annotations

import typer

app = typer.Typer(help="Related excerpts and artifacts (FTS + tags + links + recency)",
                  context_settings={"allow_interspersed_args": True})


@app.callback(invoke_without_command=True)
def related(src: str | None = typer.Argument(None, metavar="[EXC-…|ART-…]"),
            text: str | None = typer.Option(None, "--text"), limit: int = typer.Option(20, "--limit"),
            explain: bool = typer.Option(False, "--explain")) -> None:
    """Rank related items for an excerpt, an artifact or free text."""
    from . import open_db
    from ..search.related import related_with_query
    if not src and not text:
        raise typer.BadParameter("pass EXC-…, ART-… or --text")
    kwargs = {"excerpt_id": src} if src and src.startswith("EXC-") else {"artifact_ref": src} if src else {"text": text}
    conn = open_db()
    try:
        items, query = related_with_query(conn, limit=limit, explain=explain, **kwargs)
    except LookupError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    if explain:
        terms = " ".join(f"{t}:{w:.2f}" for t, w in query.terms)
        typer.echo(f"query: {' '.join(repr(p) for p in query.phrases)} {terms}".strip())
    for c in items:
        typer.echo(f"{c.score:.3f}  {c.type:<8} {c.id:<16} {c.title}")
        if explain and c.explain:
            e = c.explain
            typer.echo(f"         T {e['T']}  G {e['G']}  L {e['L']}  R {e['R']}  bm25 {e['bm25']}  shared {', '.join(e['shared']) or '-'}")
    if not items:
        typer.echo("nothing related yet")
