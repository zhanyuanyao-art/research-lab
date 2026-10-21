"""`rl chat`: drive the chat router from the terminal (same commands, same threads as the web UI)."""
from __future__ import annotations

from pathlib import Path

import typer

from . import open_db

app = typer.Typer(help="Chat with the lab from the terminal", no_args_is_help=True)


def _print(conn, m) -> None:
    head = "you" if m.role == "user" else m.role
    card = f"  [card {m.card_kind}: {m.card_ref}]" if m.card_kind else ""
    typer.echo(f"{head} · #{m.id}{card}")
    if m.content:
        typer.echo(m.content)
    if m.job_id:
        from ..ai import queue
        job = queue.get(conn, m.job_id)
        if job and job["status"] in ("queued", "claimed"):
            typer.echo(f"(reply job {m.job_id} {job['status']} — `uv run rl agent claim {m.job_id}` if no DeepSeek key)")
    typer.echo("")


@app.command("send")
def send(text: str = typer.Argument("", help="Message or command, e.g. \"/hunt\""),
         file: list[Path] = typer.Option([], "--file", "-f", help="PDF to add (repeatable)"),
         thread: str | None = typer.Option(None, "--thread", "-t", help="Thread id; default continues a recent one")) -> None:
    """Send a message; prints the thread id and the replies."""
    from ..chat import router
    conn = open_db()
    uploads = [router.Upload(p, p.name) for p in file]
    for p in file:
        if not p.exists():
            raise typer.BadParameter(f"{p} does not exist")
    msgs = router.route(conn, text, uploads, thread_id=thread, page="cli")
    typer.echo(f"thread {msgs[0].thread_id}\n")
    for m in msgs[1:]:
        _print(conn, m)


@app.command("log")
def log(thread: str | None = typer.Argument(None, help="Thread id; default the most recent"),
        n: int = typer.Option(20, "-n", help="Last N messages")) -> None:
    """Show a thread, or list threads when there is none yet."""
    from ..chat import service
    conn = open_db()
    if thread is None:
        rows = service.list_threads(conn, limit=1)
        if not rows:
            typer.echo("no conversations yet")
            return
        thread = rows[0]["id"]
    t = service.get_thread(conn, thread)
    if t is None:
        raise typer.BadParameter(f"no thread {thread}")
    typer.echo(f"{t['id']} · {t['title']}\n")
    for m in service.messages(conn, thread, limit=n):
        _print(conn, m)


@app.command("threads")
def threads(n: int = typer.Option(20, "-n")) -> None:
    """List conversations, newest first."""
    from ..chat import service
    conn = open_db()
    for t in service.list_threads(conn, limit=n):
        typer.echo(f"{t['id']}  {t['updated_at'][:16]}  {t['n_messages']:>3}  {t['title']}")
