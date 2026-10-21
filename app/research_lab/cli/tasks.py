"""`rl tasks …`: maintenance of _data/tasks.yml, the issues.yml sidecar and the issue keys."""
from __future__ import annotations

import typer

app = typer.Typer(help="待办 maintenance (the move from plan weeks to due dates)", no_args_is_help=True)


@app.command("migrate-week")
def migrate_week(dry_run: bool = typer.Option(False, "--dry-run", help="Print every change and write nothing.")) -> None:
    """Replace plan weeks with due dates, once (docs/design/ISSUES-LINEAR.md §3.3, Leo 10-10).

    An open item with a plan week and no due date gets that week's Sunday as its due date; an open item
    with a due date keeps it; a done or parked one just loses the week; `carried` becomes `postponed`.
    Covers tasks.yml and the outputs' and 课题' entries in issues.yml. Run with --dry-run first."""
    from ..files import issues, tasks
    from ..files.yamlio import StaleFileError
    try:
        task_changes = tasks.migrate_week(write=not dry_run)
        issue_changes = issues.migrate_week(write=not dry_run)
    except (tasks.TaskError, issues.IssueError, StaleFileError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    for c in task_changes:
        typer.echo(f"{c['id']} 「{c['title']}」 {c['status']}: " + "；".join(c["changes"]))
    for c in issue_changes:
        typer.echo(f"{c['ref']} 「{c['title']}」 {c['status']}: " + "；".join(c["changes"]))
    n = len(task_changes) + len(issue_changes)
    if not n:
        typer.echo("没有计划周要迁移。")
    elif dry_run:
        typer.echo(f"预演：{len(task_changes)} 条待办、{len(issue_changes)} 条议题属性会改，没有写入。去掉 --dry-run 再运行一次就会写入。")
    else:
        typer.echo(f"已写入：{len(task_changes)} 条待办、{len(issue_changes)} 条议题属性。旧值留在 git 历史里。")


@app.command("migrate-keys")
def migrate_keys(dry_run: bool = typer.Option(False, "--dry-run", help="Print every key and write nothing.")) -> None:
    """Give every issue its key, APUS-0001 … (docs/design/ISSUES-LINEAR.md 附：议题编号, Leo 10-11).

    Per research direction, by creation: each 课题, then its sub-issues and outputs, then the next 课题; issues
    without a direction count under LAB. Writes _data/issue_keys.yml; from then on new and moved issues are
    numbered as they happen. Running it again only numbers what is missing. Run with --dry-run first."""
    from ..files import datafiles, issues
    from ..files.yamlio import StaleFileError
    problems = []
    seen: dict[str, str] = {}
    for d in datafiles.read("directions") or []:
        slug, key = str(d.get("slug")), str(d.get("key") or "")
        if not datafiles.DIRECTION_KEY_RE.match(key) or key in datafiles.RESERVED_KEYS:
            problems.append(f"方向 {slug} 的前缀「{key or '空'}」不合规：要 2 到 5 个大写字母，且不能是 {'、'.join(datafiles.RESERVED_KEYS)}")
        elif key in seen:
            problems.append(f"方向 {slug} 和 {seen[key]} 用了同一个前缀 {key}")
        seen.setdefault(key, slug)
    if problems:
        typer.echo("先在 _data/directions.yml 里修好前缀：\n" + "\n".join(problems), err=True)
        raise typer.Exit(1)
    try:
        changes = issues.sync_keys(write=not dry_run, create=True)
        if not dry_run:
            issues.note_key_moves(changes)        # an issue that changed direction says so in its activity
    except (issues.IssueError, StaleFileError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    for c in changes:
        moved = f"（原 {c['old']}）" if c["old"] else ""
        typer.echo(f"{c['new']}  {c['ref']} 「{c['title']}」{moved}")
    counts: dict[str, int] = {}
    for c in changes:
        counts[c["new"].split("-")[0]] = counts.get(c["new"].split("-")[0], 0) + 1
    summary = "，".join(f"{k} {n} 个" for k, n in counts.items())
    if not changes:
        typer.echo("每个议题都已有编号，没有要改的。")
    elif dry_run:
        typer.echo(f"预演：{len(changes)} 个议题会编号（{summary}），没有写入。去掉 --dry-run 再运行一次就会写入。")
    else:
        typer.echo(f"已写入 _data/issue_keys.yml：{len(changes)} 个议题（{summary}）。")


@app.command("rekey")
def rekey(slug: str = typer.Argument(..., help="The direction's slug, e.g. alliance-us."),
          key: str = typer.Argument(..., help="The new prefix: 2 to 5 capital letters, e.g. US."),
          dry_run: bool = typer.Option(False, "--dry-run", help="Print the mapping and write nothing.")) -> None:
    """Change a direction's issue prefix and keep every number: APUS-0008 → US-0008 (Leo 10-16).

    The old keys stay as aliases, so search, /issues/APUS-0008 and text that names them still find their issue; new
    issues continue after the old count. Writes _data/issue_keys.yml and the direction's key in _data/directions.yml
    together. Running it again changes nothing. Run with --dry-run first."""
    from ..files import datafiles, issues
    from ..files.yamlio import StaleFileError
    try:
        plan = issues.rekey_direction(slug, key, write=not dry_run)
    except (issues.IssueError, datafiles.DataFileError, StaleFileError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    old, new, changes = plan["old"], plan["new"], plan["changes"]
    if old == new:
        typer.echo(f"方向 {slug} 的前缀已经是 {new}，没有要改的。")
        return
    for c in changes:
        typer.echo(f"{c['old']} → {c['new']}  {c['ref']} 「{c['title'] or '已删除'}」")
    nxt = f"，下一个新议题是 {plan['next']}" if plan["next"] else ""
    what = f"方向 {slug} 的前缀 {old} → {new}：{len(changes)} 个编号换前缀，号码不变，旧编号留作别名{nxt}"
    if dry_run:
        typer.echo(f"预演：{what}。没有写入。去掉 --dry-run 再运行一次就会写入。")
    else:
        typer.echo(f"已写入 _data/issue_keys.yml 和 _data/directions.yml：{what}。")
