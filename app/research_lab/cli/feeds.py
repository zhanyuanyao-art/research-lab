"""`rl feeds …`: refresh the push streams now, import the old newsletter's history, show the last run."""
from __future__ import annotations

import json
from pathlib import Path

import typer

from .. import feeds  # noqa: F401 - registers feeds.enrich so `rl agent` can complete those jobs
from ..feeds import enrich, history, papers, scheduler, store
from . import open_db

app = typer.Typer(no_args_is_help=True, help="Push streams: refresh, history import, status")

DEFAULT_HISTORY = Path.home() / "Projects" / "IR&MORE"


def _counts_line(label: str, part: dict) -> str:
    if not part:
        return f"{label}: not run"
    if part.get("error"):
        return f"{label}: failed ({part['error']})"
    failed = ", ".join(f"{f['source']} ({f['error']})" for f in part.get("failed") or []) or "none"
    per = ", ".join(f"{k} {v}" for k, v in sorted((part.get("per_source") or {}).items(), key=lambda kv: -kv[1])) or "-"
    extra = f", skipped {part['skipped']}, filtered {part['filtered']}" if "skipped" in part else f", outside window {part.get('old', 0)}"
    return (f"{label}: {part.get('sources', 0)} sources, fetched {part.get('fetched', 0)}, new {part.get('new', 0)}{extra}\n"
            f"  new per source: {per}\n  failed: {failed}")


@app.command()
def refresh(papers_days: int = typer.Option(0, "--papers-days", help="look-back for journals (default: since last run)"),
            news_days: int = typer.Option(0, "--news-days", help="look-back for news feeds (default: since last run)"),
            ai: bool = typer.Option(True, "--ai/--no-ai", help="queue Chinese titles and summaries for new items"),
            as_json: bool = typer.Option(False, "--json")) -> None:
    """Fetch every journal and news feed now (the app does this every six hours by itself).
    Assistant jobs are queued, not run here: the app's worker or `rl agent` picks them up."""
    conn = open_db()
    conn.close()
    summary = scheduler.run_refresh(papers_days=papers_days or None, news_days_=news_days or None, ai=ai, dispatch_jobs=False)
    if summary is None:
        typer.echo("another refresh is running", err=True)
        raise typer.Exit(1)
    if as_json:
        typer.echo(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    typer.echo(_counts_line("papers", summary.get("papers") or {}))
    typer.echo(_counts_line("news", summary.get("news") or {}))
    typer.echo(f"assistant jobs queued: {len(summary.get('ai_jobs') or [])}; linked to library: {summary.get('linked', 0)}")


@app.command("import-history")
def import_history(repo: Path = typer.Option(DEFAULT_HISTORY, "--repo", help="local clone of the old newsletter repo")) -> None:
    """Seed the streams from the old newsletter's digests and state files (safe to re-run)."""
    if not (repo / "digests").is_dir():
        typer.echo(f"no digests/ folder under {repo}", err=True)
        raise typer.Exit(1)
    conn = open_db()
    r = history.import_all(conn, repo)
    typer.echo(f"papers: found {r['papers']['found']}, new {r['papers']['new']}, filled {r['papers']['filled']}, "
               f"skipped {r['papers']['skipped']}")
    typer.echo(f"news: found {r['news']['found']}, new {r['news']['new']}, filled {r['news']['filled']}")
    typer.echo(f"linked to library: {r['linked']}")


@app.command("reviews-backfill")
def reviews_backfill(days: int = typer.Option(60, "--days", help="how far back to ask the journals again"),
                     dry_run: bool = typer.Option(False, "--dry-run", help="count per journal, write nothing")) -> None:
    """Book reviews the old filter dropped come in as reviews; stored articles that are reviews and front matter that
    slipped in are fixed (card 0154)."""
    conn = open_db()
    out = papers.backfill_reviews(conn, days=days, dry_run=dry_run)
    total = {"new": 0, "reclassified": 0, "junk": 0}
    for abbr, c in sorted(out.items()):
        for k in total:
            total[k] += c[k]
        if c.get("error"):
            typer.echo(f"{abbr}: failed ({c['error']})")
        elif any(c[k] for k in total):
            typer.echo(f"{abbr}: {c['new']} new reviews, {c['reclassified']} articles → reviews, {c['junk']} front matter dismissed")
    typer.echo(f"total: {total['new']} new reviews, {total['reclassified']} articles → reviews, {total['junk']} front matter dismissed"
               + (" (dry run: nothing written)" if dry_run else ""))


@app.command()
def repair(days: int = typer.Option(90, "--days", help="how far back stored rows are mended"),
           dry_run: bool = typer.Option(False, "--dry-run", help="count, write nothing")) -> None:
    """Mend U+FFFD quotes in stored titles and abstracts, find free PDFs on publishers' own pages and fill missing
    keyword directions (card 0155). Writes only those fields; deletes nothing."""
    conn = open_db()
    c = papers.repair(conn, days=days, dry_run=dry_run)
    typer.echo(f"titles mended: {c['titles']}, abstracts mended: {c['abstracts']}")
    typer.echo(f"free PDFs from publishers: {c['publisher_pdfs']}")
    typer.echo(f"directions filled: {c['directions']} papers, {c['news_directions']} news"
               + (" (dry run: nothing written)" if dry_run else ""))


@app.command()
def status() -> None:
    """Last refresh time, counts per stream and status."""
    conn = open_db()
    last = store.last_refresh(conn) or {}
    typer.echo(f"last refresh: {last.get('at') or 'never'}")
    for stream, c in store.counts(conn).items():
        typer.echo(f"{stream}: {c['total']} (new {c['new']}, saved {c['saved']}, dismissed {c['dismissed']})")


@app.command()
def enrich_now() -> None:
    """Queue assistant batches for today's 今日精选 picks that still lack a Chinese title (and the brief when due)."""
    conn = open_db()
    jobs = enrich.enqueue_pending(conn, dispatch_jobs=False)
    typer.echo(f"queued {len(jobs)} job(s): {', '.join(jobs) or '-'}")
