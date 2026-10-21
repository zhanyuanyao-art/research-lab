"""`rl ai …`: run queued jobs on DeepSeek now, spend report, model check, routing mode."""
from __future__ import annotations

from datetime import date

import typer

from ..ai import cost, dispatch, queue
from ..ai.pricing import CHECKED_ON, KNOWN_MODELS
from ..config import has_deepseek_key
from ..db.connection import get_setting, set_setting
from . import open_db

app = typer.Typer(no_args_is_help=True, help="DeepSeek runs, cost and routing")

MODES = ("auto", "deepseek", "agent")


def _run_line(conn, job_id: str, run_id: int) -> str:
    r = conn.execute("SELECT * FROM ai_runs WHERE id = ?", (run_id,)).fetchone()
    extra = " · repaired" if r["repaired"] else ""
    return (f"{job_id} done · {r['model']} · prompt {r['prompt_tokens'] or 0} (cache hit {r['cache_hit_tokens'] or 0}) · "
            f"completion {r['completion_tokens'] or 0} (reasoning {r['reasoning_tokens'] or 0}) · "
            f"${(r['cost_usd'] or 0):.5f}{extra}")


@app.command()
def run(job_id: str | None = typer.Argument(None), all_queued: bool = typer.Option(False, "--all-queued"),
        force: bool = typer.Option(False, "--force", help="ignore the daily cap")) -> None:
    """Execute job(s) on DeepSeek synchronously and print tokens + cost."""
    if bool(job_id) == all_queued:
        typer.echo("give a job ID or --all-queued", err=True)
        raise typer.Exit(2)
    if not has_deepseek_key():
        typer.echo("no DEEPSEEK_API_KEY in app/.env; use `uv run rl agent claim --next` instead", err=True)
        raise typer.Exit(1)
    conn = open_db()
    ids = [job_id] if job_id else [r["id"] for r in reversed(queue.list_jobs(conn, status="queued", limit=1000))]
    if not ids:
        typer.echo("No queued jobs.")
        return
    failures = 0
    for jid in ids:
        try:
            run_id = dispatch.run_now(jid, force=force)
        except (RuntimeError, LookupError) as e:
            typer.echo(f"{jid}: {e}", err=True)
            failures += 1
            if "daily cap" in str(e):
                break
            continue
        except Exception as e:  # noqa: BLE001 - runner already recorded the failure
            typer.echo(f"{jid} failed: {e}", err=True)
            failures += 1
            continue
        typer.echo(_run_line(conn, jid, run_id))
    typer.echo(f"spent today ${cost.spent_today(conn):.4f} of ${cost.daily_cap(conn):.2f}")
    if failures:
        raise typer.Exit(1)


@app.command("cost")
def cost_cmd(since: str | None = typer.Option(None, "--since", help="YYYY-MM-DD")) -> None:
    """Spend per day and job kind from ai_runs."""
    if since:
        try:
            date.fromisoformat(since)
        except ValueError:
            typer.echo("--since must be YYYY-MM-DD", err=True)
            raise typer.Exit(2)
    conn = open_db()
    rows = conn.execute(
        "SELECT substr(r.started_at, 1, 10) AS day, j.kind, r.provider, COUNT(*) AS runs, "
        "COALESCE(SUM(r.prompt_tokens),0) AS p, COALESCE(SUM(r.cache_hit_tokens),0) AS h, "
        "COALESCE(SUM(r.completion_tokens),0) AS c, COALESCE(SUM(r.cost_usd),0) AS usd "
        "FROM ai_runs r JOIN ai_jobs j ON j.id = r.job_id WHERE r.started_at >= ? "
        "GROUP BY day, j.kind, r.provider ORDER BY day, j.kind", (since or "0000",)).fetchall()
    if not rows:
        typer.echo("No AI runs" + (f" since {since}." if since else "."))
    else:
        typer.echo(f"{'day (UTC)':<11} {'kind':<20} {'provider':<12} {'runs':>4} {'prompt':>9} {'cache':>8} {'output':>8} {'usd':>10}")
        for r in rows:
            typer.echo(f"{r['day']:<11} {r['kind']:<20} {r['provider']:<12} {r['runs']:>4} {r['p']:>9} {r['h']:>8} {r['c']:>8} {r['usd']:>10.5f}")
        typer.echo(f"{'total':<11} {'':<20} {'':<12} {sum(r['runs'] for r in rows):>4} {sum(r['p'] for r in rows):>9} "
                   f"{sum(r['h'] for r in rows):>8} {sum(r['c'] for r in rows):>8} {sum(r['usd'] for r in rows):>10.5f}")
    typer.echo(f"today ${cost.spent_today(conn):.4f} of cap ${cost.daily_cap(conn):.2f} · prices checked {CHECKED_ON}")


@app.command()
def models() -> None:
    """List model ids from DeepSeek and check the configured one exists."""
    conn = open_db()
    provider = dispatch.make_provider(conn)
    configured = dispatch.configured_model(conn)
    if provider is None:
        typer.echo(f"no DEEPSEEK_API_KEY; configured model {configured}; known ids: {', '.join(KNOWN_MODELS)}", err=True)
        raise typer.Exit(1)
    try:
        ids = provider.list_models()
    except Exception as e:  # noqa: BLE001
        typer.echo(f"could not list models: {e}", err=True)
        raise typer.Exit(1)
    for i in ids:
        typer.echo(("* " if i == configured else "  ") + i)
    if configured in ids:
        typer.echo(f"configured model {configured}: available")
    else:
        typer.echo(f"configured model {configured}: NOT offered by the API; ask the assistant to change ai.model (current value on /dev)", err=True)
        raise typer.Exit(1)


@app.command()
def mode(value: str | None = typer.Argument(None, help="auto|deepseek|agent")) -> None:
    """Show or set ai.mode."""
    conn = open_db()
    if value is not None:
        if value not in MODES:
            typer.echo(f"mode must be one of {', '.join(MODES)}", err=True)
            raise typer.Exit(2)
        set_setting(conn, "ai.mode", value)
    where, why = dispatch.route(conn)
    typer.echo(f"ai.mode = {get_setting(conn, 'ai.mode', 'auto')} (effective: {queue.mode(conn)}; "
               f"new jobs → {where}{' — ' + why if why else ''})")
