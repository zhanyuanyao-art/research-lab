"""`rl data …`: import datasets, list them, queue a replication plan, run an analysis."""
from __future__ import annotations

import json
from pathlib import Path

import typer

from . import open_db

app = typer.Typer(help="数据复现: datasets (DuckDB), replication plans, analysis runs", no_args_is_help=True)


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


@app.command("import")
def import_cmd(file: Path = typer.Argument(..., exists=True, dir_okay=False, help="CSV/TSV, XLSX, DTA, SAV or Parquet"),
               name: str | None = typer.Option(None, "--name"), paper: str | None = typer.Option(None, "--paper", help="Paper id"),
               sheet: str | None = typer.Option(None, "--sheet", help="XLSX sheet (default: first)"),
               note: str | None = typer.Option(None, "--note")) -> None:
    """Import a data file into DuckDB (with a parquet copy) and print its profile."""
    from ..replication import datasets
    conn = open_db()
    try:
        ds_id = datasets.import_file(conn, file, name=name, paper_id=paper, sheet=sheet, note=note)
    except datasets.DatasetError as e:
        raise typer.BadParameter(str(e))
    ds = datasets.get(conn, ds_id)
    typer.echo(f"{ds_id}  {ds['name']}  {ds['rows']} rows × {ds['cols']} cols  ({ds['source_kind']})")
    for c in ds["profile"]["columns"]:
        extra = f" min {_fmt(c.get('min'))} max {_fmt(c.get('max'))} mean {_fmt(c.get('mean'))}" if "mean" in c else ""
        typer.echo(f"  {c['name']:<24} {c['dtype']:<8} missing {c['missing']} ({c['missing_pct']}%) unique {c['unique']}{extra}")


def _size(f) -> str:
    if not f.size:
        return "大小未知"
    return f"{f.size / 1024 / 1024:.1f} MB" if f.size >= 1024 * 1024 else f"{max(1, round(f.size / 1024))} KB"


def _print_listing(listing) -> None:
    from ..replication import fetch
    groups = [("Code", listing.code, False), ("Docs", listing.docs, False), ("Data", listing.files, True)]
    typer.echo(f"{listing.title}  ({listing.source}: {len(listing.files)} 个数据文件，{len(listing.code)} 个代码，"
               f"{len(listing.docs)} 个说明书，另有 {len(listing.other)} 个其他文件不列出)")
    for label, files, numbered in groups:
        if not files:
            continue
        typer.echo(f"{label} ({len(files)})")
        for i, f in enumerate(files, 1):
            num = f"{i:>3}." if numbered else "    -"
            typer.echo(f"  {num} {f.name}  {_size(f)}" + (f"  [{f.note}]" if f.note else ""))
    if listing.files or listing.code or listing.docs:
        typer.echo("用 --pick 选要下载的文件（数据可以用编号，代码和说明书用文件名），或者 --auto 让程序按作者代码选")


def _print_auto(auto) -> None:
    pkg = auto.pkg
    listing = auto.listing
    typer.echo(f"{listing.title}  ({listing.source}: {len(listing.files)} 个数据文件，{len(listing.code)} 个代码，"
               f"{len(listing.docs)} 个说明书)")
    if pkg is None:
        typer.echo("没有可读的 R / Stata / Python 脚本，不能判断主分析；只选说明书" if listing.docs else "没有可读的脚本，也没有说明书")
    else:
        typer.echo(f"主分析 ({len(pkg.main)})")
        for m in pkg.main:
            from ..replication.package import format_models
            typer.echo(f"  {m}  {format_models(pkg.models.get(m, {})) or '没有模型调用'}")
            for d in pkg.data_of(m):
                typer.echo(f"      读 {d}")
        if not pkg.main:
            typer.echo("  （没有同时读数据又拟合模型的脚本；用 --main 指定，或在方案页手动勾选）")
        if pkg.helpers:
            typer.echo(f"被其他脚本调用的辅助文件 ({len(pkg.helpers)})：" + "、".join(pkg.helpers[:6]) + ("…" if len(pkg.helpers) > 6 else ""))
        for name, why in pkg.not_main.items():
            typer.echo(f"  不当主分析  {name}  {why}")
        for name, why in pkg.left_out.items():
            typer.echo(f"  主脚本要用但没选  {name}  {why}")
    for label, files in (("数据", auto.data), ("代码", auto.code), ("说明书", auto.docs)):
        typer.echo(f"默认选择·{label} ({len(files)})")
        for f in files:
            readers = auto.readers(f.name) if label == "数据" else []
            typer.echo(f"  {f.name}  {_size(f)}" + (f"  ← {', '.join(readers)}" if readers else ""))


@app.command("fetch")
def fetch_cmd(source: str = typer.Argument(..., help="Dataverse DOI or link, OSF project, GitHub repo or tree, or a data file link"),
              plan: str | None = typer.Option(None, "--plan", help="Replication plan to attach the datasets to"),
              pick: list[str] = typer.Option([], "--pick", help="File to download, by number or name; repeat for more"),
              auto: bool = typer.Option(False, "--auto", help="Pick the data the main analysis scripts read, plus those scripts and the codebook"),
              dry_run: bool = typer.Option(False, "--dry-run", help="With --auto: print what would be picked, writing and importing nothing (code and docs are still read as text to find the main analysis)"),
              main: list[str] = typer.Option([], "--main", help="With --auto: name the main scripts yourself (path or file name), replacing the rule"),
              max_mb: int = typer.Option(500, "--max-mb", help="Refuse a file (or an unpacked zip) larger than this")) -> None:
    """List what a public replication source offers (data, code, README); with --pick or --auto, download and import (cards 0159, 0183)."""
    from ..replication import fetch
    conn = open_db()
    auto = auto or dry_run
    try:
        if auto:
            if pick:
                raise typer.BadParameter("--auto 和 --pick 不能一起用")
            if not dry_run and not plan:
                raise typer.BadParameter("--auto 要用 --plan 指定方案（只想看看会选什么，加 --dry-run）")
            if dry_run:
                listing = fetch.list_source(source, cap=max_mb * 1024 * 1024)
                _print_auto(fetch.plan_auto(listing, main=main or None))
                return
            plan_auto, results = fetch.fetch_auto(conn, source, plan, main=main or None, max_bytes=max_mb * 1024 * 1024)
            _print_auto(plan_auto)
        elif not pick:
            _print_listing(fetch.list_source(source))
            return
        else:
            results = fetch.fetch(conn, source, pick, plan_id=plan, max_bytes=max_mb * 1024 * 1024)
    except fetch.FetchError as e:
        raise typer.BadParameter(str(e))
    for r in results:
        if r.dataset:
            typer.echo(f"{r.dataset}  {r.name}")
        elif r.saved:
            typer.echo(f"已存 {r.saved}  {r.name}")
        else:
            typer.echo(f"失败  {r.name}: {r.error}")
    if plan and any(r.dataset for r in results):
        typer.echo(f"已挂到方案 {plan}")


@app.command("list")
def list_cmd() -> None:
    """Datasets, replication plans and recent runs."""
    from ..replication import datasets, plans, runs
    conn = open_db()
    ds = datasets.list_all(conn)
    typer.echo(f"datasets ({len(ds)})")
    for d in ds:
        typer.echo(f"  {d['id']}  {d['name']}  {d['rows']}×{d['cols']}  {d['source_kind']}" + (f"  paper {d['paper_id']}" if d["paper_id"] else ""))
    pl = plans.list_all(conn)
    typer.echo(f"plans ({len(pl)})")
    for p in pl:
        typer.echo(f"  {p['id']}  {p['status']:<11} {p['origin']:<12} {p['paper_id'] or '—'}  steps {p['steps_done']}/{p['steps_total']}")
    rs = runs.list_all(conn, limit=20)
    typer.echo(f"runs (latest {len(rs)})")
    for r in rs:
        n = (r["result"] or {}).get("summary", {}).get("n")
        typer.echo(f"  {r['id']}  {r['method']:<11} {r['dataset_id'] or '—'}  n={n}  {r['status']}")


@app.command("plan")
def plan_cmd(paper_id: str = typer.Argument(...), force: bool = typer.Option(False, "--force", help="Queue even if one is open"),
             show: bool = typer.Option(False, "--show", help="Print what would be sent, do not queue")) -> None:
    """Queue methods.replicate for a paper (DeepSeek runs it, or `rl agent` picks it up)."""
    from ..replication import plans
    conn = open_db()
    if show:
        req, _ = plans.build(conn, paper_id)
        typer.echo(req.messages[-1]["content"])
        return
    try:
        job_id = plans.enqueue(conn, paper_id, force=force)
    except (plans.PlanError, LookupError) as e:
        raise typer.BadParameter(str(e))
    if job_id is None:
        typer.echo("a methods.replicate job for this paper is already open (use --force to queue another)")
        return
    typer.echo(f"queued {job_id} · methods.replicate · {paper_id}")
    typer.echo(f"  claude code: uv run rl agent show {job_id}   ·   page: /data/plans/new?paper={paper_id}")


@app.command("rrun")
def rrun_cmd(plan: str = typer.Argument(..., help="Replication plan id, RP-YYYY-NNNN"),
             install: bool = typer.Option(False, "--install", help="Fetch missing helper scripts and install the R packages from CRAN (needs the network)"),
             run: bool = typer.Option(False, "--run", help="Run the authors' R scripts in the sandbox (no network)"),
             script: list[str] = typer.Option([], "--script", help="Run only this script (file name or path tail); repeat"),
             package: list[str] = typer.Option([], "--package", help="Also install this CRAN package; repeat"),
             no_ai: bool = typer.Option(False, "--no-ai", help="Never ask the AI to repair a failing script"),
             rounds: int = typer.Option(3, "--rounds", min=0, help="Most repair sessions in one run"),
             fresh: bool = typer.Option(False, "--fresh", help="Forget the repairs of earlier runs"),
             fetch: bool = typer.Option(False, "--fetch", help="With --run: fetch missing helper scripts first (network)"),
             no_fetch: bool = typer.Option(False, "--no-fetch", help="With --install: skip fetching helper scripts"),
             source: str | None = typer.Option(None, "--source", help="Where to fetch helpers from (default: the datasets' source)")) -> None:
    """R route (card 0186): `--install` is the only networked step; `--run` runs the authors' R code as it is and only
    asks the AI when a script fails. Without a flag: what is installed and what is missing."""
    from ..replication import rrun
    from ..replication.workspace import WorkspaceError
    conn = open_db()
    try:
        if not install and not run:
            o = rrun.overview(conn, plan)
            typer.echo(f"R: {('R ' + o['r']['version'] + ' · ' + o['r']['path']) if o['r']['found'] else '没有找到 R（Rscript）'}")
            for s in o["scripts"]:
                typer.echo(f"脚本: {s['path']}" + (f"  缺 {', '.join(s['missing'])}" if s["missing"] else ""))
            have = [p for p in o["to_install"] if p["have"]]
            typer.echo(f"R 包: {len(o['to_install'])} 个，已装 {len(have)} 个" + (f"，没装: {', '.join(o['missing_pkgs'])}" if o["missing_pkgs"] else ""))
            for n in o["notes"]:
                typer.echo(f"注意: {n}")
            st = o["state"]
            typer.echo(f"上一次: {st.get('kind', '—')} {st.get('state', '—')} {st.get('finished_at') or ''} {st.get('note') or ''}".rstrip())
            return
        if install:
            r = rrun.install(conn, plan, extra=package, fetch=not no_fetch, source=source, progress=typer.echo)
            typer.echo(f"装包: {'全部装上了' if r['ok'] else '有包没装上'} · 已装 {len(r['installed'])} 个 · {r['seconds']} 秒")
            for name, why in r["failed"].items():
                typer.echo(f"  没装上 {name}: {why}")
            for p in r["fetched"]:
                typer.echo(f"  取回辅助脚本 {p}")
            for n in r["notes"]:
                typer.echo(f"  注意: {n}")
            typer.echo(f"  日志: logs/rinstall.log 在方案的工作区里")
        if run:
            o = rrun.run(conn, plan, scripts=script or None, repair=not no_ai, rounds=rounds, fresh=fresh, fetch=fetch,
                         source=source, progress=typer.echo)
            typer.echo(f"运行: {o['note']} · 记录 {o['run_id']} · {o['rounds']} 轮 AI 修补 · {o['tokens']} token")
            for s in o["scripts"]:
                typer.echo(f"  {s['script']}: 退出码 {s['exit']} · {s['seconds']} 秒")
            for s in o["skipped"]:
                typer.echo(f"  没请 AI: {s}")
            typer.echo(f"  表: {o['tables']} 张写进 results.json · 输出文件 {len(o['outputs'])} 个 · 改动见 outputs/changes.md")
    except WorkspaceError as e:
        raise typer.BadParameter(str(e))


@app.command("run")
def run_cmd(method: str = typer.Argument(..., help="describe | crosstab | correlation | ttest | ols | logit | fe | network"),
            dataset: str = typer.Option(..., "--dataset", help="DS-YYYY-NNNN"),
            y: str | None = typer.Option(None, "--y", help="Outcome (ols/logit/fe/ttest)"),
            x: list[str] = typer.Option([], "--x", help="Predictor; repeat for several"),
            vars_: list[str] = typer.Option([], "--var", help="Variable for describe/correlation; repeat"),
            row: str | None = typer.Option(None, "--row"), col: str | None = typer.Option(None, "--col"),
            group: str | None = typer.Option(None, "--group", help="t-test group, or network source-node group"),
            entity: str | None = typer.Option(None, "--entity"), time: str | None = typer.Option(None, "--time"),
            se: str | None = typer.Option(None, "--se", help="classical | robust | cluster"),
            cluster: str | None = typer.Option(None, "--cluster"),
            source: str | None = typer.Option(None, "--source"), target: str | None = typer.Option(None, "--target"),
            weight: str | None = typer.Option(None, "--weight"), year: str | None = typer.Option(None, "--year"),
            directed: bool = typer.Option(False, "--directed"),
            plan: str | None = typer.Option(None, "--plan"),
            as_json: bool = typer.Option(False, "--json")) -> None:
    """Run an analysis on a dataset and store it as a RUN record (with figure and code)."""
    from ..replication import runs
    from ..replication.stats import StatsError
    conn = open_db()
    form = {"y": y, "x": x, "vars": vars_, "row": row, "col": col, "group": group, "entity": entity, "time": time, "se": se,
            "cluster": cluster, "source": source, "target": target, "weight": weight, "year": year,
            "directed": "1" if directed else None}
    try:
        params = runs.parse_params(method, {k: v for k, v in form.items() if v not in (None, [], "")})
        run_id = runs.create(conn, method, dataset, params, plan_id=plan)
    except (StatsError, runs.RunError) as e:
        raise typer.BadParameter(str(e))
    run = runs.get(conn, run_id)
    if as_json:
        typer.echo(json.dumps({"id": run_id, **run["result"]}, ensure_ascii=False, indent=2))
        return
    res = run["result"]
    s = res["summary"]
    typer.echo(f"{run_id} · {run['method_label']} · {dataset} · n={s.get('n')} missing dropped={s.get('missing_dropped')}")
    for t in res["tables"]:
        typer.echo(f"\n{t['title']}  (n={t.get('n')}, missing={t.get('missing')})")
        typer.echo("  " + " | ".join(map(str, t["columns"])))
        for r in t["rows"][:40]:
            typer.echo("  " + " | ".join(_fmt(v) for v in r))
    for n in res.get("notes", []):
        typer.echo(f"note: {n}")
    typer.echo(f"\nfigure: {run['figure_path'] or '—'}   page: /data/runs/{run_id}")
