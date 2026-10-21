from __future__ import annotations

from pathlib import Path

import typer

from ..files import datafiles

app = typer.Typer(help="导入 PDF：提取页面、解析元数据、排队生成中文摘要（中日韩论文自动排队英文译文）")


def _known_project(slug: str) -> bool:
    return any(str(p.get("slug")) == slug for p in datafiles.read("projects") or [])


@app.command("add")
def add(files: list[Path] = typer.Argument(..., exists=True, dir_okay=False, readable=True, metavar="FILE..."),
        project: str | None = typer.Option(None, "--project", help="项目 slug；默认用 now.yml 里的项目"),
        doi: str | None = typer.Option(None, "--doi", help="为该文件指定 DOI（仅限单个文件）"),
        no_ai: bool = typer.Option(False, "--no-ai", help="不排队 AI 任务（摘要、翻译）")) -> None:
    """导入一个或多个 PDF。"""
    from ..ai import queue
    from ..intake import pipeline
    from . import open_db
    if doi and len(files) > 1:
        raise typer.BadParameter("--doi 只能用于单个文件")
    if project and not _known_project(project):
        raise typer.BadParameter(f"未知项目 '{project}'（见 _data/projects.yml）")
    conn = open_db()
    failed = 0
    for f in files:
        try:
            r = pipeline.ingest(conn, f, project=project, doi=doi, ai=not no_ai)
        except pipeline.RejectedFile as e:
            failed += 1
            typer.echo(f"✗ {f.name}：未导入 — {e}", err=True)
            continue
        if r.created:
            line = f"✓ {f.name} → {r.paper_id}  [{r.metadata_status}]" + (f" [{r.language}]" if r.language else "")
            if r.job_id:
                line += f"  摘要 {r.job_id}" + ("（已排入 Claude Code 队列）" if queue.mode(conn) == "agent" else "")
            if r.translate_jobs:
                line += f"  译文 {len(r.translate_jobs)} 段"
            typer.echo(line)
        elif r.pending:
            typer.echo(f"? {f.name}：与 {r.duplicate_of} {r.reason}，暂存为 {r.pending}。"
                       f"请在 /papers 页面确认，或换一个文件重试。")
        else:
            typer.echo(f"= {f.name}：已在文献库中，编号 {r.duplicate_of}（{r.reason}）")
        for note in r.notes:
            typer.echo(f"    · {note}")
    raise typer.Exit(1 if failed else 0)


@app.command("lookup")
def lookup(paper_id: str) -> None:
    """重新向 Crossref/OpenAlex 查询一篇论文的元数据。"""
    from ..intake import pipeline
    from . import open_db
    conn = open_db()
    try:
        res = pipeline.lookup(conn, paper_id)
    except LookupError:
        raise typer.BadParameter(f"没有论文 {paper_id}")
    if res.status == "verified" and res.chosen:
        typer.echo(f"已核验：{res.chosen.title} ({res.chosen.year}) doi:{res.chosen.doi}")
    else:
        typer.echo("未核验" + (f"（检索词：{res.query}）" if res.query else ""))
        for i, c in enumerate(res.candidates):
            typer.echo(f"  [{i}] {c.match:5.1f}  {c.title} ({c.year or '?'}) {'; '.join(c.authors[:2])} doi:{c.doi}")
    for e in res.errors:
        typer.echo(f"  · {e}", err=True)
