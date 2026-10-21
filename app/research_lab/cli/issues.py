"""`rl issues`: the issue layer for agents (card 0176, docs/design/ISSUE-AGENT.md). Everything the page can do,
as commands whose output is Markdown an agent reads whole and whose writes go through the same checked paths as the
assistant (activity lines say 助手). Keys (APUS-0012) name issues; a 课题 slug names its parent issue."""
from __future__ import annotations

from pathlib import Path

import typer

from ..files import issues as I

app = typer.Typer(no_args_is_help=True, help="议题：agent 可读可写的入口（Markdown 输出）")


def _ref(key: str) -> str:
    ref = I.resolve(key) or (I.project_ref(key) if I.get(I.project_ref(key)) else None) or (key if I.get(key) else None)
    if ref is None:
        typer.echo(f"找不到议题 {key}", err=True)
        raise typer.Exit(1)
    return ref


def _fail(e: Exception) -> None:
    typer.echo(I.error_text(e) if isinstance(e, I.ERRORS) else str(e), err=True)
    raise typer.Exit(1)


@app.command("list")
def list_cmd(project: str | None = typer.Option(None, "--project", help="只看一个课题"),
             all_: bool = typer.Option(False, "--all", help="含已完成和搁置")) -> None:
    """One line per issue: key, status, due, title."""
    rows = I.read() + I.project_roots()
    for r in sorted(rows, key=lambda r: (r.get('project') or '', 0 if r.get('project_root') else 1, str(r.get('due') or '9999'), r['id'])):
        if project and r.get('project') != project:
            continue
        if not all_ and r.get('status') in ('done', 'parked'):
            continue
        due = f" 截止 {r['due']}" if r.get('due') else ''
        typer.echo(f"{r.get('key') or r['id']}  {I.display('status', r.get('status') or '')}{due}  {'[课题] ' if r.get('project_root') else ''}{r['title']}")


@app.command()
def show(key: str, project: bool = typer.Option(False, "--project", help="把整个课题及其子议题全部输出")) -> None:
    """The issue as Markdown: properties, description, sub-issues, attachments, thread."""
    try:
        if project:
            slug = key if I.get(I.project_ref(key)) else I.get(_ref(key))['project']
            typer.echo(I.project_markdown(slug))
        else:
            typer.echo(I.markdown(_ref(key)))
    except I.ERRORS as e:
        _fail(e)


@app.command()
def new(title: str, project: str = typer.Option(..., "--project", help="课题 slug"),
        parent: str = typer.Option("", "--parent", help="父子议题的 key"),
        note: str = typer.Option("", "--note", help="描述"), note_file: Path | None = typer.Option(None, "--note-file"),
        due: str = typer.Option("", "--due", help="YYYY-MM-DD"), priority: str = typer.Option("", "--priority"),
        labels: str = typer.Option("", "--labels", help="逗号分隔：问题,文献地图,数据结果,结论,案例时间轴,写作")) -> None:
    """Create a sub-issue (origin assistant); prints its key."""
    props = {}
    text = note_file.read_text(encoding="utf-8") if note_file else note
    if text:
        props["description"] = text
    if due:
        props["due"] = due
    if priority:
        props["priority"] = priority
    if labels:
        props["labels"] = [x.strip() for x in labels.split(",") if x.strip()]
    try:
        ref, _ = I.create_checked(title, project=project, parent=_ref(parent) if parent else "", actor="assistant", **props)
    except I.ERRORS as e:
        _fail(e)
    typer.echo(f"{I.key_of(ref) or ref} {title}")


@app.command("set")
def set_cmd(key: str, field: str = typer.Argument(..., help="status | priority | due | labels | title | description | related"),
            value: str = typer.Argument(..., help="labels/related 用逗号分隔；description 可用 @文件 读取")) -> None:
    """Change one property the assistant may change; the activity shows 助手."""
    ref = _ref(key)
    row = I.get(ref)
    if field == "status" and value in ("done", "parked", "已完成", "搁置"):
        _fail(RuntimeError("agent 不把议题改成已完成或搁置，由 Leo 验收（ISSUE-AGENT.md）"))
    new = value[1:] and Path(value[1:]).read_text(encoding="utf-8") if value.startswith("@") and field == "description" else value
    if field in ("labels", "related"):
        new = [x.strip() for x in str(new).split(",") if x.strip()]
    try:
        out = I.change_fields(ref, [(field, I.value_of(row, field), new)], actor="assistant")
    except I.ERRORS as e:
        _fail(e)
    if out.get("failed"):
        _fail(RuntimeError(out["failed"][0]["error"]))
    typer.echo(out.get("line") or f"{I.key_of(ref)} {field} 已改")


@app.command()
def start(key: str) -> None:
    """待办 → 进行中, which is as far as an agent may move a status (ISSUE-AGENT.md)."""
    set_cmd(key, "status", "doing")


@app.command()
def comment(key: str, text: str = typer.Option("", "--text"), file: Path | None = typer.Option(None, "--file", help="从文件读评论正文"),
            attach: list[str] = typer.Option([], "--attach", help="课题里已有的文件：drafts/x.md 或 private/x.pdf，可重复")) -> None:
    """Post a 助手 comment, optionally carrying existing 课题 files as attachments."""
    body = file.read_text(encoding="utf-8") if file else text
    if not body.strip() and attach:
        body = "附件：" + "、".join(a.split("/", 1)[-1] for a in attach)
    try:
        I.comment_checked(_ref(key), body, by="assistant", attachments=attach)
    except I.ERRORS as e:
        _fail(e)
    typer.echo("已评论")


@app.command("attach")
def attach_cmd(key: str, rel: str = typer.Argument(..., help="drafts/<文件> 或 private/<文件>，须已在课题文件夹里")) -> None:
    """Link an existing 课题 file to the issue without uploading it."""
    ref = _ref(key)
    try:
        I.attach_existing(ref, rel, I.revision(ref))
    except I.ERRORS as e:
        _fail(e)
    typer.echo(f"{I.key_of(ref) or ref} 已附上 {rel}")
