"""`rl check …`: seed the method records, evaluate milestones, run exports, derive checks."""
from __future__ import annotations

import typer

from . import open_db

app = typer.Typer(help="Method layer: milestones, matrix, exports (method-layer.md)", no_args_is_help=True)


def _default_project() -> str:
    from ..files import datafiles
    return str((datafiles.read("now") or {}).get("project") or "asean-centrality")


@app.command("seed")
def seed_cmd(project: str | None = typer.Option(None, "--project", help="Project slug; default: every seedable project"),
             force: bool = typer.Option(False, "--force", help="Refresh seeded fields (never statuses, excerpts or your text)")) -> None:
    """Seed camps, H1–H4, observations O1–O6, forums, matrix cells and concept ledgers."""
    from ..method import seed
    conn = open_db()
    slugs = [project] if project else list(seed.SEEDABLE)
    for slug in slugs:
        try:
            counts = seed.seed(conn, slug, force=force)
        except KeyError as e:
            raise typer.BadParameter(str(e.args[0]))
        typer.echo(f"{slug}: " + ", ".join(f"{k} +{v}" for k, v in counts.items()) + (" (force)" if force else ""))


@app.command("milestones")
def milestones_cmd(project: str | None = typer.Option(None, "--project"), all_checks: bool = typer.Option(False, "--all")) -> None:
    """Print the milestone ladder with the failing sub-checks; writes _data/lab.yml."""
    from ..files import artifacts
    from ..method import milestones
    conn = open_db()
    slug = project or _default_project()
    artifacts.index(conn)
    ms = milestones.evaluate(conn, slug)
    for m in ms:
        mark = "✓" if m.passed else ("·" if m.behind else "✗")
        typer.echo(f"{mark} {m.id:<4} {m.title:<32} [{m.transition}]")
        for c in m.checks:
            if all_checks or not c.passed:
                typer.echo(f"      {'✓' if c.passed else '○'} {c.label}{' (1-min)' if not c.auto else ''} — {c.detail}")
    nxt = milestones.next_unmet(conn, slug)
    typer.echo(f"\nnext: {nxt['milestone']} {nxt['title']} — {nxt['failing']}" if nxt else "\nnext: nothing unmet at this stage")
    sug = milestones.stage_suggestion(conn, slug, ms)
    if sug:
        typer.echo(f"stage move available: → {sug} (confirm on /projects/{slug})")


@app.command("coverage")
def coverage_cmd(project: str | None = typer.Option(None, "--project")) -> None:
    """Matrix coverage and the process-tracing verdict per explanation."""
    from ..method import matrix
    conn = open_db()
    slug = project or _default_project()
    cov = matrix.coverage(conn, slug)
    typer.echo(f"coverage {cov['resolved']}/{cov['total']} ({cov['pct']}%)")
    for code, v in matrix.verdicts(conn, slug).items():
        typer.echo(f"  {code}: {v}")


@app.command("export")
def export_cmd(what: str = typer.Argument(..., help="literature | evidence | check"),
               project: str | None = typer.Option(None, "--project"), check_id: int | None = typer.Option(None, "--id")) -> None:
    """Rewrite the project tables and create or update the artifact."""
    from ..files import artifacts
    from ..method import exports
    conn = open_db()
    artifacts.index(conn)
    slug = project or _default_project()
    if what == "literature":
        ref = exports.export_literature_map(conn, slug)
    elif what == "evidence":
        ref = exports.export_evidence(conn, slug)
    elif what == "check":
        if check_id is None:
            raise typer.BadParameter("--id is required for a check export")
        ref = exports.export_check(conn, check_id)
    else:
        raise typer.BadParameter("what must be literature, evidence or check")
    typer.echo(ref)


@app.command("derive")
def derive_cmd(target: str = typer.Argument(..., help="claim id, or an ART- artifact ref")) -> None:
    """Queue a checks.derive AI job; results arrive as dashed suggestions on the checks page."""
    from ..files import artifacts
    from ..method import checks
    conn = open_db()
    if target.startswith("ART-"):
        artifacts.index(conn)
    job = checks.derive(conn, target)
    typer.echo(job or "an open checks.derive job for this target already exists")
