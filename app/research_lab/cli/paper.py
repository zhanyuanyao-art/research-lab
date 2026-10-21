from __future__ import annotations

import json

import typer

app = typer.Typer(help="查看和处理文献库中的论文")

FILTERS = {
    "all": "1=1",
    "unsummarized": "ai_summary_json IS NULL",
    "unverified": "metadata_status != 'verified'",
    "unread": "read_depth IS NULL",
}


@app.command("list")
def list_(filter_: str = typer.Option("all", "--filter", help="all | unsummarized | unverified | unread")) -> None:
    from . import open_db
    if filter_ not in FILTERS:
        raise typer.BadParameter(f"--filter 只能是 {', '.join(FILTERS)}")
    conn = open_db()
    rows = conn.execute(f"SELECT id, title, year, metadata_status, ai_summary_json IS NOT NULL AS summarized, read_depth, "
                        f"language, direction FROM papers WHERE {FILTERS[filter_]} ORDER BY created_at DESC").fetchall()
    for r in rows:
        flags = ("S" if r["summarized"] else "·") + ("V" if r["metadata_status"] == "verified" else "?")
        typer.echo(f"{flags}  {r['id']:<32} {r['year'] or '    '}  {(r['language'] or '--'):<5} {(r['direction'] or '-'):<18} {r['title'][:60]}")
    typer.echo(f"（共 {len(rows)} 篇 · S = 已摘要，V = 元数据已核验）")


@app.command("show")
def show(paper_id: str) -> None:
    from ..intake import pipeline
    from . import open_db
    conn = open_db()
    p = pipeline.get_paper(conn, paper_id)
    if p is None:
        raise typer.BadParameter(f"没有论文 {paper_id}")
    authors = json.loads(p["authors_json"] or "[]")
    typer.echo(f"{p['id']} · {p['metadata_status']}")
    typer.echo(f"{p['title']}")
    typer.echo(f"{'; '.join(authors) or 'unknown authors'} · {p['year'] or 'n.d.'} · {p['venue'] or '—'} · {p['kind'] or '—'}")
    if p["doi"]:
        typer.echo(f"doi:{p['doi']}")
    f = pipeline.primary_file(conn, paper_id)
    if f:
        typer.echo(f"file: {f['rel_path']} · {f['page_count']} pages · text layer {'yes' if f['has_text_layer'] else 'no'}")
    projects = [r[0] for r in conn.execute("SELECT project_slug FROM paper_projects WHERE paper_id = ?", (paper_id,))]
    typer.echo(f"projects: {', '.join(projects) or '—'} · read: {p['read_depth'] or '—'} · camp: {p['camp'] or '—'}")
    typer.echo(f"language: {p['language'] or '—'} · direction: {p['direction'] or '—'}")
    for col in ("claim_text", "mechanism", "cannot_explain", "concept_usage", "method"):
        if p[col]:
            typer.echo(f"{col}: {p[col]}")
    s = pipeline.summary_of(p)
    if s:
        typer.echo("\nAI 摘要（阅读辅助，请对照页码核实）：")
        for key in ("research_question", "argument", "mechanism", "method", "relevance_to_project"):
            v = s.get(key) or {}
            if v.get("text"):
                pages = f" [p. {', '.join(v.get('pages') or [])}]" if v.get("pages") else ""
                typer.echo(f"  {key}: {v['text']}{pages}")
        cg = s.get("camp_guess") or {}
        if cg.get("camp"):
            typer.echo(f"  camp_guess: {cg['camp']} ({cg.get('confidence')})")
        if s.get("read_first"):
            typer.echo("  read first: " + "; ".join(f"p. {r['page']} {r['why']}" for r in s["read_first"]))
    else:
        job = pipeline.latest_summary_job(conn, paper_id)
        typer.echo(f"\nsummary: {job['status'] + ' · ' + job['id'] if job else 'none (rl paper summarize ' + paper_id + ')'}")
    n = conn.execute("SELECT COUNT(*) FROM excerpts WHERE paper_id = ?", (paper_id,)).fetchone()[0]
    typer.echo(f"excerpts: {n}")


@app.command("summarize")
def summarize(paper_id: str) -> None:
    """排队（或重新排队）中文摘要 paper.summarize；替换仍在等待的任务。"""
    from ..intake import pipeline
    from . import open_db
    conn = open_db()
    if pipeline.get_paper(conn, paper_id) is None:
        raise typer.BadParameter(f"没有论文 {paper_id}")
    typer.echo(pipeline.enqueue_summary(conn, paper_id, force=True) or "未排队")


@app.command("text")
def text(paper_id: str, page: str | None = typer.Option(None, "--page", help="印刷页码，例如 245")) -> None:
    from ..intake import pipeline
    from . import open_db
    conn = open_db()
    f = pipeline.primary_file(conn, paper_id)
    if f is None:
        raise typer.BadParameter(f"论文 {paper_id} 没有文件")
    pages = pipeline.load_pages(conn, f["id"])
    if page is not None:
        pages = [p for p in pages if p.label == page]
        if not pages:
            raise typer.BadParameter(f"没有页码为 {page} 的页")
    for p in pages:
        typer.echo(f"=== p. {p.label} (index {p.index}) ===")
        typer.echo(p.text)


@app.command("translate")
def translate(paper_id: str,
              lang: str | None = typer.Option(None, "--lang", help="en | zh；默认：中日韩论文译为英文，其他译为中文"),
              force: bool = typer.Option(False, "--force", help="已有译文的分段也重新翻译")) -> None:
    """按页分段排队 paper.translate（每段一个任务），并显示进度。"""
    from ..intake import pipeline
    from . import open_db
    conn = open_db()
    p = pipeline.get_paper(conn, paper_id)
    if p is None:
        raise typer.BadParameter(f"没有论文 {paper_id}")
    lang_to = lang or pipeline.default_translate_target(pipeline.ensure_language(conn, paper_id))
    if lang_to not in pipeline.TRANSLATE_TARGETS:
        raise typer.BadParameter("--lang 只能是 en 或 zh")
    try:
        ids = pipeline.enqueue_translation(conn, paper_id, lang_to, force=force)
    except (ValueError, LookupError) as e:
        raise typer.BadParameter(str(e))
    st = pipeline.translation_status(conn, paper_id, lang_to)
    typer.echo(f"新排队 {len(ids)} 个任务：{' '.join(ids) or '—'}")
    typer.echo(f"进度：{st['chunks_done']}/{st['chunks_total']} 段 · {st['pages_done']}/{st['pages_total']} 页 · 进行中 {st['chunks_open']} 段")


@app.command("direction")
def direction(paper_id: str, slug: str | None = typer.Argument(None, help="directions.yml 里的 slug；省略则清空")) -> None:
    """设置（或清空）论文的研究方向。"""
    from ..intake import directions, pipeline
    from . import open_db
    conn = open_db()
    try:
        value = pipeline.set_direction(conn, paper_id, slug)
    except LookupError:
        raise typer.BadParameter(f"没有论文 {paper_id}")
    except ValueError:
        raise typer.BadParameter(f"未知方向 {slug}；可选：{', '.join(directions.slugs())}")
    typer.echo(f"{paper_id} → {value or '（未设置）'}")


@app.command("collections-cleanup")
def collections_cleanup(dry_run: bool = typer.Option(False, "--dry-run", help="只列出会删除和跳过的集合，什么都不改")) -> None:
    """删除 10-02 迁移留下的、和研究方向同名的残缺集合（Leo 10-12 同意）：名字和某个方向完全相同、而且每篇成员都属于
    那个方向的才删；其余的列为跳过并说明原因。文献本身、方向和课题都不动。先加 --dry-run 看清单。"""
    from .. import paper_collections as C
    from . import open_db
    conn = open_db()
    plan = C.remove_direction_copies(conn, dry_run=dry_run)
    verb = "会删除" if dry_run else "已删除"
    for row in plan:
        typer.echo(f"[{verb if row['action'] == 'delete' else '跳过'}] {row['name']}（{row['members']} 篇）：{row['reason']}")
    gone = sum(row["action"] == "delete" for row in plan)
    typer.echo(f"（共 {len(plan)} 个集合：{verb} {gone} 个，跳过 {len(plan) - gone} 个"
               + ("；这是演练，没有改动任何东西）" if dry_run else "）"))
