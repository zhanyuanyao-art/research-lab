"""`rl agent …`: the queue as Claude Code sees it. Results pass the same pydantic schema and the
method-layer guardrails before anything is written; nothing here edits the DB or artifacts by hand."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError

from ..ai import queue, registry
from ..config import get_paths
from . import open_db

app = typer.Typer(no_args_is_help=True, help="Work the AI job queue from a Claude Code session")

STATUSES = ("queued", "claimed", "failed", "done", "cancelled")
# Record ids a result may cite; each must already appear in the job's input (guardrail: cite only given ids).
ID_PATTERN = re.compile(r"\b(?:EXC|ART)-\d{4}-\d{4}\b")


def _err(msg: str, code: int = 1) -> typer.Exit:
    typer.echo(msg, err=True)
    return typer.Exit(code)


def _job_or_exit(conn, job_id: str):
    job = queue.get(conn, job_id)
    if job is None:
        raise _err(f"no job {job_id}")
    return job


def find_quote_keys(value: Any, path: str = "$") -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            p = f"{path}.{k}"
            if str(k).lower() == "quote":
                hits.append(p)
            hits += find_quote_keys(v, p)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            hits += find_quote_keys(v, f"{path}[{i}]")
    return hits


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _input_text(job) -> str:
    # Decode the request first: in raw JSON a newline is the two characters "\n", so an id that
    # starts a line has no word boundary before it and the regex misses it.
    parts = list(_strings(json.loads(job["request_json"]))) + [job["target_id"] or ""]
    root = get_paths().root
    for f in json.loads(job["input_files_json"] or "[]"):
        p = Path(f) if Path(f).is_absolute() else root / f
        try:
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return "\n".join(parts)


def unknown_ids(job, data: Any) -> list[str]:
    cited = {m for s in _strings(data) for m in ID_PATTERN.findall(s)}
    if not cited:
        return []
    given = set(ID_PATTERN.findall(_input_text(job)))
    return sorted(cited - given)


def _row_dict(r) -> dict[str, Any]:
    return {k: r[k] for k in ("id", "kind", "status", "target_type", "target_id", "attempts", "created_at",
                              "claimed_at", "completed_at", "error")}


@app.command("list")
def list_cmd(status: str | None = typer.Option(None, "--status", help="|".join(STATUSES)),
             kind: str | None = typer.Option(None, "--kind"),
             as_json: bool = typer.Option(False, "--json"),
             limit: int = typer.Option(100, "--limit")) -> None:
    """List jobs, newest first."""
    if status and status not in STATUSES:
        raise _err(f"--status must be one of {', '.join(STATUSES)}", 2)
    conn = open_db()
    rows = queue.list_jobs(conn, status=status, kind=kind, limit=limit)
    if as_json:
        typer.echo(json.dumps([_row_dict(r) for r in rows], indent=2))
        return
    if not rows:
        typer.echo("No jobs." if not status else f"No {status} jobs.")
        return
    for r in rows:
        target = f"{r['target_type'] or ''} {r['target_id'] or ''}".strip()
        typer.echo(f"{r['id']}  {r['status']:<9}  {r['kind']:<18}  {target:<32}  {r['created_at']}")


@app.command()
def show(job_id: str, fmt: str = typer.Option("md", "--format", help="md|json"),
         out: Path | None = typer.Option(None, "--out")) -> None:
    """Print the prompt, schema and input file paths (file contents are not printed)."""
    conn = open_db()
    job = _job_or_exit(conn, job_id)
    if fmt == "md":
        text = queue.render_markdown(job)
    elif fmt == "json":
        spec = registry.get(job["kind"])
        text = json.dumps({**_row_dict(job), "request": json.loads(job["request_json"]),
                           "input_files": json.loads(job["input_files_json"] or "[]"),
                           "schema": json.loads(job["schema_json"]), "example": spec.example(),
                           "result": json.loads(job["result_json"]) if job["result_json"] else None},
                          indent=2, ensure_ascii=False)
    else:
        raise _err("--format must be md or json", 2)
    if out:
        out.write_text(text + "\n", encoding="utf-8")
        typer.echo(f"wrote {out}")
    else:
        typer.echo(text)


@app.command()
def claim(job_id: str | None = typer.Argument(None), next_: bool = typer.Option(False, "--next"),
          kind: str | None = typer.Option(None, "--kind")) -> None:
    """queued -> claimed; prints the md view. Prints "No queued jobs." when --next finds none."""
    if bool(job_id) == next_:
        raise _err("give a job ID or --next", 2)
    conn = open_db()
    if next_:
        job = queue.claim_next(conn, kind)
        if job is None:
            typer.echo("No queued jobs." if not kind else f"No queued {kind} jobs.")
            return
    else:
        _job_or_exit(conn, job_id)
        try:
            job = queue.claim(conn, job_id)
        except LookupError as e:
            raise _err(str(e))
    typer.echo(queue.render_markdown(job))


@app.command()
def schema(kind: str) -> None:
    """JSON schema of a job kind's result, plus a filled example."""
    kinds = registry.load_all()
    if kind not in kinds:
        raise _err(f"unknown kind {kind!r}; known: {', '.join(sorted(kinds)) or 'none'}", 2)
    spec = kinds[kind]
    typer.echo(json.dumps({"kind": kind, "schema": spec.Result.model_json_schema(), "example": spec.example()},
                          indent=2, ensure_ascii=False))


@app.command()
def complete(job_id: str, from_file: Path = typer.Option(..., "--from-file", exists=True, dir_okay=False),
             model: str = typer.Option("claude-code", "--model")) -> None:
    """Validate result.json against the job schema and guardrails, then apply it. Invalid -> exit 2, job unchanged."""
    conn = open_db()
    job = _job_or_exit(conn, job_id)
    if job["status"] not in ("queued", "claimed"):
        raise _err(f"{job_id} is {job['status']}; nothing to complete")
    try:
        data = json.loads(from_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise _err(f"{from_file}: not valid JSON: {e}", 2)
    quotes = find_quote_keys(data)
    if quotes:
        raise _err("rejected (guardrail 2, no AI quotes): remove every `quote` field: " + ", ".join(quotes), 2)
    strangers = unknown_ids(job, data)
    if strangers:
        raise _err("rejected (cite only ids from the input): not in this job's input: " + ", ".join(strangers), 2)
    try:
        run_id = queue.complete(conn, job_id, data, provider="claude-code", model=model)
    except ValidationError as e:
        typer.echo(f"{job_id}: result does not match the {job['kind']} schema ({e.error_count()} errors):", err=True)
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "$"
            typer.echo(f"  {loc}: {err['msg']}", err=True)
        raise typer.Exit(2)
    except ValueError as e:
        raise _err(f"{job_id}: rejected by the job's apply(): {e}", 2)
    typer.echo(f"{job_id} done (run {run_id}, provider claude-code, model {model})")


@app.command()
def fail(job_id: str, reason: str = typer.Option(..., "--reason")) -> None:
    """Mark a job failed with a reason (e.g. the input cannot support any answer)."""
    conn = open_db()
    job = _job_or_exit(conn, job_id)
    if job["status"] not in ("queued", "claimed"):
        raise _err(f"{job_id} is {job['status']}")
    queue.fail(conn, job_id, reason)
    typer.echo(f"{job_id} failed: {reason}")


@app.command()
def requeue(job_id: str) -> None:
    """failed / claimed / cancelled -> queued."""
    conn = open_db()
    job = _job_or_exit(conn, job_id)
    queue.requeue(conn, job_id)
    typer.echo(f"{job_id}: {job['status']} -> {queue.get(conn, job_id)['status']}")


@app.command()
def cancel(job_id: str) -> None:
    """queued / claimed -> cancelled."""
    conn = open_db()
    job = _job_or_exit(conn, job_id)
    queue.cancel(conn, job_id)
    typer.echo(f"{job_id}: {job['status']} -> {queue.get(conn, job_id)['status']}")
