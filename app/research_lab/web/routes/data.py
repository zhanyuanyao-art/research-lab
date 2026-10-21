"""数据复现: datasets (upload, profile, preview, rename/cast, delete), replication plans (from a paper
via methods.replicate, editable checklist and variables, review, export) and analysis runs (method
forms, results, figure, code, the user's reading, the analysis.read suggestion, check link, export)."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.datastructures import UploadFile

from ...ai import queue
from ...config import get_paths
from ...replication import coverage, datasets, deck, export, fetch, plans, runs
from ...replication.datasets import DatasetError
from ...replication import workspace as W
from ...replication.export import ExportError
from ...replication.plans import PlanError, StalePlan, StaleTargets
from ...replication.runs import RunError
from ...replication.stats import StatsError
from ..context import chosen_direction, get_conn, render

router = APIRouter()
log = logging.getLogger("research_lab.web.data")
CHUNK = 1 << 20
USER_ERRORS = (DatasetError, PlanError, RunError, StatsError, ExportError)


def _go(request: Request, url: str, **flash: str) -> Response:
    if flash:
        url += ("&" if "?" in url else "?") + urlencode({k: v for k, v in flash.items() if v})
    if request.headers.get("hx-request") == "true" and request.headers.get("hx-current-url"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def _flash(request: Request) -> dict[str, str]:
    return {"msg": request.query_params.get("msg", ""), "err": request.query_params.get("err", "")}


def _projects() -> list[dict[str, Any]]:
    from ...files import datafiles
    return [dict(p) for p in (datafiles.read("projects") or []) if isinstance(p, dict)]


def _papers_brief(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, title, year FROM papers ORDER BY updated_at DESC LIMIT 300").fetchall()


# ---------------------------------------------------------------- overview

@router.get("/data")
def data_home(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """One list: the datasets, each with its replication plans and analyses (docs/design/DATA-v2.md, Leo 10-02);
    plans without a dataset wait in 待配数据. The direction bar filters by the one tag system, the research
    direction, remembered across pages (context.chosen_direction)."""
    from ...intake import directions as directions_mod
    direction = chosen_direction(request)
    all_ds, all_plans = datasets.list_all(conn), plans.list_all(conn)
    last_runs = dict(conn.execute("SELECT dataset_id, MAX(created_at) FROM analysis_runs WHERE dataset_id IS NOT NULL GROUP BY dataset_id").fetchall())
    for d in all_ds:
        d["n_plans"] = sum(1 for p in all_plans if d["id"] in (p["plan"].get("datasets") or []))
        d["last_run"] = (last_runs.get(d["id"]) or "")[:10]
    live = {d["id"] for d in all_ds}
    dir_counts = {str(x["slug"]): sum(1 for d in all_ds if d.get("direction") == x["slug"]) for x in directions_mod.load()}
    shown = [d for d in all_ds if not direction or d.get("direction") == direction]
    # 待配数据: no dataset that still exists (a deleted dataset leaves its id in the plan)
    unattached = [p for p in all_plans if not live & set(p["plan"].get("datasets") or [])
                  and (not direction or p.get("direction") == direction)]
    # analyses whose dataset was deleted keep their results (datasets.delete) and stay reachable here
    orphan_runs = [] if direction else [r for r in runs.list_all(conn, limit=100000) if not r.get("dataset_id")]
    replications, groups = _replications(conn, all_plans, directions_mod.load(), direction)
    return render(request, conn, "pages/data.html", title="数据", wide=True, datasets=shown, total=len(all_ds),
                  unattached=unattached, orphan_runs=orphan_runs, dir_counts=dir_counts, dir_total=len(all_ds), dir_filter=direction,
                  direction=direction, status_labels=plans.STATUS_LABELS, tab=request.query_params.get("tab", ""),
                  replications=replications, replication_groups=groups, papers=_papers_brief(conn), **_flash(request))


def _replications(conn: sqlite3.Connection, all_plans: list[dict[str, Any]], dirs: list[dict[str, Any]],
                  direction: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Card 0196: every replication as one row (its paper, how it came out, how many things wait for Leo), grouped
    by research direction in the directions file's order."""
    from ...replication import report_content
    names = {str(d["slug"]): str(d.get("name") or d["slug"]) for d in dirs}
    rows = []
    for p in all_plans:
        if direction and p.get("direction") != direction:
            continue
        rows.append({"id": p["id"], "title": p.get("paper_title") or p["id"], "direction": p.get("direction") or "",
                     "status": report_content.status_line(conn, p["id"]), "needs": len(report_content.needs_of(p["id"]))})
    order = list(names) + [""]
    groups = []
    for slug in order + sorted({r["direction"] for r in rows} - set(order)):
        mine = [r for r in rows if r["direction"] == slug]
        if mine:
            groups.append({"name": names.get(slug, slug or "未分方向"), "rows": mine})
    return rows, groups


# ---------------------------------------------------------------- datasets

@router.post("/data/datasets")
async def dataset_upload(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    up = form.get("file")
    if not isinstance(up, UploadFile) or not up.filename:
        return _go(request, "/data", err="请选择要导入的文件")
    incoming = get_paths().cache / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    tmp = incoming / f"{uuid.uuid4().hex}{Path(up.filename).suffix.lower()}"
    try:
        with tmp.open("wb") as fh:
            while chunk := await up.read(CHUNK):
                fh.write(chunk)
                if fh.tell() > datasets.MAX_BYTES:
                    raise DatasetError("文件超过 500 MB")
        ds_id = await run_in_threadpool(
            datasets.import_file, conn, tmp, original_name=Path(up.filename).name, name=str(form.get("name") or "") or None,
            paper_id=str(form.get("paper_id") or "") or None, direction=str(form.get("direction") or "") or None,
            note=str(form.get("note") or "") or None, sheet=str(form.get("sheet") or "") or None)
    except DatasetError as e:
        return _go(request, "/data", err=str(e))
    finally:
        tmp.unlink(missing_ok=True)
    return _go(request, f"/data/datasets/{ds_id}", msg="已导入")


def _ds_or_404(conn: sqlite3.Connection, ds_id: str) -> dict[str, Any]:
    ds = datasets.get(conn, ds_id)
    if ds is None:
        raise HTTPException(404, f"没有数据集 {ds_id}")
    return ds


@router.get("/data/datasets/{ds_id}")
def dataset_page(ds_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    ds = _ds_or_404(conn, ds_id)
    try:
        cols, rows = datasets.preview(ds_id)
    except Exception as e:  # noqa: BLE001 - a missing DuckDB table should not hide the registry row
        cols, rows = [], []
        ds["preview_error"] = f"{type(e).__name__}: {e}"
    paper = conn.execute("SELECT id, title FROM papers WHERE id = ?", (ds["paper_id"],)).fetchone() if ds["paper_id"] else None
    return render(request, conn, "pages/data_dataset.html", title=ds["name"], wide=True, ds=ds, preview_cols=cols,
                  preview_rows=rows, cast_types=datasets.CAST_TYPES, paper=paper, papers=_papers_brief(conn),
                  runs=runs.list_all(conn, dataset_id=ds_id), methods=runs.METHODS,
                  plans=[p for p in plans.list_all(conn) if ds_id in p["plan"].get("datasets", [])],
                  status_labels=plans.STATUS_LABELS, **_flash(request))


@router.post("/data/datasets/{ds_id}/columns")
async def dataset_columns(ds_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    ds = _ds_or_404(conn, ds_id)
    form = await request.form()
    renames, casts = {}, {}
    for i, col in enumerate(ds["columns"]):
        new = str(form.get(f"name_{i}") or "").strip()
        if new and new != col:
            renames[col] = new
        t = str(form.get(f"type_{i}") or "")
        if t and t != datasets.column_kinds(ds).get(col):
            casts[col] = t
    try:
        lost = await run_in_threadpool(datasets.rename_cast, conn, ds_id, renames, casts)
    except DatasetError as e:
        return _go(request, f"/data/datasets/{ds_id}", err=str(e))
    msg = "已更新列" if (renames or casts) else "没有改动"
    lost_bits = [f"{k}：{v} 个值无法转换，已设为缺失" for k, v in lost.items() if v]
    return _go(request, f"/data/datasets/{ds_id}", msg=msg + ("；" + "；".join(lost_bits) if lost_bits else ""))


@router.post("/data/datasets/{ds_id}/sheet")
async def dataset_sheet(ds_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _ds_or_404(conn, ds_id)
    form = await request.form()
    try:
        await run_in_threadpool(datasets.reimport_sheet, conn, ds_id, str(form.get("sheet") or ""))
    except DatasetError as e:
        return _go(request, f"/data/datasets/{ds_id}", err=str(e))
    return _go(request, f"/data/datasets/{ds_id}", msg="已切换工作表（列的重命名与类型转换需重新做）")


@router.post("/data/datasets/{ds_id}/meta")
async def dataset_meta(ds_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _ds_or_404(conn, ds_id)
    form = await request.form()
    try:
        datasets.update_meta(conn, ds_id, name=str(form.get("name") or ""), note=str(form.get("note") or ""),
                             paper_id=str(form.get("paper_id") or "") or None, direction=str(form.get("direction") or "") or None)
    except DatasetError as e:
        return _go(request, f"/data/datasets/{ds_id}", err=str(e))
    return _go(request, f"/data/datasets/{ds_id}", msg="已保存")


@router.post("/data/datasets/{ds_id}/delete")
def dataset_delete(ds_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _ds_or_404(conn, ds_id)
    datasets.delete(conn, ds_id)
    return _go(request, "/data", msg=f"已删除数据集 {ds_id}（相关运行保留结果，但不能再重新运行）")


@router.get("/data/datasets/{ds_id}/original")
def dataset_original(ds_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    ds = _ds_or_404(conn, ds_id)
    p = datasets.original_path(ds)
    if not p.exists():
        raise HTTPException(404, "原始文件不存在")
    return FileResponse(p, filename=ds["original_name"])


# ---------------------------------------------------------------- plans

@router.get("/data/plans/new")
def plan_new(request: Request, paper: str | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    chosen = None
    if paper:
        chosen = conn.execute("SELECT id, title, year FROM papers WHERE id = ?", (paper,)).fetchone()
        if chosen is None:
            raise HTTPException(404, f"没有论文 {paper}")
    material = plans.methods_material(conn, paper) if chosen else None
    job = queue.latest_for(conn, "methods.replicate", paper) if chosen else None
    return render(request, conn, "pages/data_plan.html", title="新建复现方案", wide=True, mode="new",
                  candidates=plans.candidate_papers(conn), chosen=chosen, material=material, job=_job_view(conn, job),
                  existing=plans.list_all(conn, paper_id=paper) if chosen else [], status_labels=plans.STATUS_LABELS, **_flash(request))


@router.post("/data/plans/new")
async def plan_enqueue(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    paper_id = str(form.get("paper_id") or "")
    if not conn.execute("SELECT 1 FROM papers WHERE id = ?", (paper_id,)).fetchone():
        return _go(request, "/data/plans/new", err="请选择一篇论文")
    try:
        job_id = plans.enqueue(conn, paper_id, force=bool(form.get("force")))
    except (PlanError, LookupError) as e:
        return _go(request, f"/data/plans/new?paper={paper_id}", err=str(e))
    if job_id is None:
        job = queue.latest_for(conn, "methods.replicate", paper_id)
        job_id = job["id"] if job else None
    if request.headers.get("hx-request") == "true" and request.headers.get("hx-target") and job_id:
        return render(request, conn, "partials/_data_job.html", job=_job_view(conn, queue.get(conn, job_id)))
    return _go(request, f"/data/plans/new?paper={paper_id}", msg="已提交生成复现方案" if job_id else "")


@router.post("/data/plans/blank")
async def plan_blank(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    try:
        plan_id = plans.create_blank(conn, str(form.get("paper_id") or "") or None, str(form.get("direction") or "") or None)
    except PlanError as e:
        return _go(request, "/data/plans/new", err=str(e))
    return _go(request, f"/data/plans/{plan_id}/detail", msg="已新建空白方案")


def _job_view(conn: sqlite3.Connection, job: sqlite3.Row | None) -> dict[str, Any] | None:
    if job is None:
        return None
    d = dict(job)
    d["plan_id"] = plans.plan_for_job(conn, job["id"]) if job["kind"] == "methods.replicate" and job["status"] == "done" else None
    return d


@router.get("/data/jobs/{job_id}")
def data_job(job_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    job = queue.get(conn, job_id)
    if job is None or job["kind"] not in ("methods.replicate", "analysis.read"):
        raise HTTPException(404, f"没有任务 {job_id}")
    if job["kind"] == "analysis.read" and job["status"] == "done":
        run = runs.get(conn, job["target_id"])
        return render(request, conn, "partials/_data_ai_reading.html", run=run, job=None)
    return render(request, conn, "partials/_data_job.html", job=_job_view(conn, job))


def _plan_or_404(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    p = plans.get(conn, plan_id)
    if p is None:
        raise HTTPException(404, f"没有复现方案 {plan_id}")
    return p


def _attached(conn: sqlite3.Connection, p: dict[str, Any]) -> list[dict[str, Any]]:
    return [d for d in (datasets.get(conn, i) for i in p["plan"].get("datasets", [])) if d]


def _plan_ctx(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    p = _plan_or_404(conn, plan_id)
    attached = _attached(conn, p)
    return {"p": p, "attached": attached, "all_datasets": datasets.list_all(conn), "roles": plans.ROLES,
            "role_labels": plans.ROLE_LABELS, "families": plans.FAMILIES, "levels": plans.LEVELS,
            "level_labels": plans.LEVEL_LABELS, "status_labels": plans.STATUS_LABELS, "statuses": plans.STATUSES,
            "plan_runs": runs.list_all(conn, plan_id=plan_id), "methods": runs.METHODS, "projects": _projects(),
            "default_project": export.default_project(),
            # card 0157: 复现目标 (论文报告值), data links, identifying assumptions; the workspace opens once targets are reviewed
            "targets": plans.targets(conn, plan_id), "targets_reviewed": plans.targets_reviewed(p),
            "targets_version": plans.targets_version(conn, plan_id), "sources_version": _sources_version(p),
            "versions": plans.section_versions(p), "ledger": plans.ledger(conn, plan_id),
            "link_kinds": plans.LINK_KINDS, "link_kind_labels": plans.LINK_KIND_LABELS, "link_access": plans.LINK_ACCESS,
            "link_access_labels": plans.LINK_ACCESS_LABELS, "assumption_statuses": plans.ASSUMPTION_STATUSES,
            "coverage": coverage.overview(conn, plan_id), "deck": deck.info(plan_id)}


def _stale(request: Request, conn: sqlite3.Connection, plan_id: str, e: PlanError) -> Response:
    """409 with the plan as it is now and the reason on top: nothing from the stale form was written (card 0168)."""
    ctx = _plan_ctx(conn, plan_id)
    response = render(request, conn, "pages/data_plan.html", title="方案详情", wide=True, mode="plan",
                      short_title=_short(str(ctx["p"].get("paper_title") or plan_id)), **ctx, msg="", err=str(e))
    response.status_code = 409
    return response


def _version(form: Any) -> str | None:
    return str(form.get("version") or "") or None


@router.get("/data/plans/{plan_id}", response_class=HTMLResponse)
def plan_page(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Card 0196: one replication on one page — how it came out, what waits for Leo, and 研究, 数据, 方法, 结果
    opening in place (no third level, Leo 10-19); only the deck has a page of its own. The plan's fields are on /detail."""
    from ...replication import report_content
    plan = _plan_or_404(conn, plan_id)
    view = report_content.page_view(conn, plan)
    return render(request, conn, "pages/replication.html", title=view.title, v=view, plan=plan,
                  short_title=_short(view.title), deck_info=deck.info(plan_id), **_flash(request))


@router.get("/data/plans/{plan_id}/detail", response_class=HTMLResponse)
def plan_detail_page(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    ctx = _plan_ctx(conn, plan_id)
    title = str(ctx["p"].get("paper_title") or plan_id)
    return render(request, conn, "pages/data_plan.html", title="方案详情", wide=True, mode="plan", short_title=_short(title),
                  **ctx, **_flash(request))


@router.post("/data/plans/{plan_id}/steps/{index}")
async def plan_step(plan_id: str, index: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _plan_or_404(conn, plan_id)
    form = await request.form()
    done = str(form.get("done") or "") in ("1", "on", "true") if form.get("toggle") else None
    note = str(form["note"]) if "note" in form else None
    try:
        step = plans.set_step(conn, plan_id, index, done=done, note=note)
    except PlanError as e:
        raise HTTPException(400, str(e))
    if request.headers.get("hx-request") == "true":
        return render(request, conn, "partials/_data_step.html", p=_plan_or_404(conn, plan_id), step=step, i=index)
    return _go(request, f"/data/plans/{plan_id}/detail")


@router.post("/data/plans/{plan_id}/steps")
async def plan_step_add(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    try:
        plans.add_step(conn, plan_id, str(form.get("text") or ""))
    except PlanError as e:
        return _go(request, f"/data/plans/{plan_id}/detail", err=str(e))
    return _go(request, f"/data/plans/{plan_id}/detail#steps")


@router.post("/data/plans/{plan_id}/steps/{index}/delete")
def plan_step_delete(plan_id: str, index: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _plan_or_404(conn, plan_id)
    plans.remove_step(conn, plan_id, index)
    return _go(request, f"/data/plans/{plan_id}/detail#steps")


def _rows(form: Any, prefix: str, keys: tuple[str, ...]) -> list[dict[str, str]]:
    cols = {k: form.getlist(f"{prefix}_{k}") for k in keys}
    n = max((len(v) for v in cols.values()), default=0)
    return [{k: str(cols[k][i]) if i < len(cols[k]) else "" for k in keys} for i in range(n)]


@router.post("/data/plans/{plan_id}/variables")
async def plan_variables(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _plan_or_404(conn, plan_id)
    form = await request.form()
    try:
        plans.save_variables(conn, plan_id, _rows(form, "var", ("role", "name", "measure", "source")), version=_version(form))
    except StalePlan as e:
        return _stale(request, conn, plan_id, e)
    return _go(request, f"/data/plans/{plan_id}/detail#variables", msg="变量表已保存")


@router.post("/data/plans/{plan_id}/sources")
async def plan_sources(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _plan_or_404(conn, plan_id)
    form = await request.form()
    try:
        plans.save_sources(conn, plan_id, _rows(form, "src", ("name", "access", "url_or_where")), version=_version(form))
    except StalePlan as e:
        return _stale(request, conn, plan_id, e)
    return _go(request, f"/data/plans/{plan_id}/detail#sources", msg="数据来源已保存")


@router.post("/data/plans/{plan_id}/fields")
async def plan_fields(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _plan_or_404(conn, plan_id)
    form = await request.form()
    keys = ("unit_of_analysis", "period", "sample_rule", "model_family", "model_specification", "feasibility_level",
            "feasibility_why", "robustness", "to_obtain", "caveats", "direction")
    try:
        plans.save_fields(conn, plan_id, {k: str(form[k]) for k in keys if k in form}, version=_version(form))
    except StalePlan as e:
        return _stale(request, conn, plan_id, e)
    return _go(request, f"/data/plans/{plan_id}/detail", msg="已保存")


@router.post("/data/plans/{plan_id}/targets")
async def plan_targets(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _plan_or_404(conn, plan_id)
    form = await request.form()
    version = str(form.get("targets_version") or "") or None
    try:
        n = plans.save_targets(conn, plan_id, _rows(form, "tgt", ("tid", *plans.TARGET_FIELDS)), version=version)
    except StaleTargets as e:
        return _stale(request, conn, plan_id, e)
    except PlanError as e:
        raise HTTPException(400, str(e))
    return _go(request, f"/data/plans/{plan_id}/detail#targets", msg=f"复现目标已保存（{n} 条）")


@router.post("/data/plans/{plan_id}/targets/review")
def plan_targets_review(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        plans.review_targets(conn, plan_id)
    except PlanError as e:
        return _go(request, f"/data/plans/{plan_id}/detail#targets", err=str(e))
    return _go(request, f"/data/plans/{plan_id}/detail#targets", msg="复现目标已审阅：现在可以进入工作区")


@router.post("/data/plans/{plan_id}/coverage/scan")
async def plan_coverage_scan(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Card 0187: list every table and figure of the paper and its appendix, tie them to the author's scripts. Off the
    event loop: reading a long appendix and a big codebook takes a moment (and stops, bounded, past coverage.SCAN_SECONDS)."""
    _plan_or_404(conn, plan_id)
    try:
        done = await run_in_threadpool(coverage.rescan, conn, plan_id)
    except coverage.CoverageError as e:
        return _go(request, f"/data/plans/{plan_id}/detail?{urlencode({'err': str(e)})}#coverage")
    text = (f"清单已更新：共 {done['total']} 项，对上脚本 {done['mapped']} 项"
            + (f"；另有 {done['omitted']} 项未列出" if done["omitted"] else ""))
    return _go(request, f"/data/plans/{plan_id}/detail?{urlencode({'msg': text})}#coverage")


_ID = re.compile(r"[0-9]{1,12}")                    # str.isdigit() accepts "²", and int("²") raises


@router.post("/data/plans/{plan_id}/coverage")
async def plan_coverage_marks(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _plan_or_404(conn, plan_id)
    form = await request.form()
    ids = [int(x) for x in form.getlist("cov_id") if _ID.fullmatch(str(x))]
    ticked = [int(x) for x in form.getlist("cov_tick") if _ID.fullmatch(str(x))]
    owners = {i: str(form.get(f"cov_owner_{i}") or "") for i in ids}
    n = coverage.save_marks(conn, plan_id, ids, ticked, owners)
    return _go(request, f"/data/plans/{plan_id}/detail?{urlencode({'msg': '清单已保存' if n else '清单没有变化'})}#coverage")


@router.post("/data/plans/{plan_id}/links")
async def plan_links(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _plan_or_404(conn, plan_id)
    form = await request.form()
    try:
        plans.save_links(conn, plan_id, _rows(form, "lnk", ("url", "kind", "access", "note")), version=_version(form))
    except StalePlan as e:
        return _stale(request, conn, plan_id, e)
    except PlanError as e:
        return _go(request, f"/data/plans/{plan_id}/detail#links", err=str(e))
    return _go(request, f"/data/plans/{plan_id}/detail#links", msg="数据链接已保存")


@router.post("/data/plans/{plan_id}/assumptions")
async def plan_assumptions(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _plan_or_404(conn, plan_id)
    form = await request.form()
    try:
        plans.save_assumptions(conn, plan_id, _rows(form, "asm", ("text", "status")), version=_version(form))
    except StalePlan as e:
        return _stale(request, conn, plan_id, e)
    return _go(request, f"/data/plans/{plan_id}/detail#assumptions", msg="识别假设已保存")


@router.post("/data/plans/{plan_id}/datasets")
async def plan_attach(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    try:
        plans.attach_dataset(conn, plan_id, str(form.get("dataset_id") or ""))
    except PlanError as e:
        return _go(request, f"/data/plans/{plan_id}/detail", err=str(e))
    return _go(request, f"/data/plans/{plan_id}/detail#datasets")


@router.post("/data/plans/{plan_id}/datasets/{ds_id}/detach")
def plan_detach(plan_id: str, ds_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        plans.detach_dataset(conn, plan_id, ds_id)
    except PlanError as e:
        return _go(request, f"/data/plans/{plan_id}/detail", err=str(e))
    return _go(request, f"/data/plans/{plan_id}/detail#datasets")


@router.post("/data/plans/{plan_id}/review")
def plan_review(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        plans.mark_reviewed(conn, plan_id)
    except PlanError as e:
        return _go(request, f"/data/plans/{plan_id}/detail", err=str(e))
    return _go(request, f"/data/plans/{plan_id}/detail", msg="已标记为已审阅：方案现在算作你的设计")


@router.post("/data/plans/{plan_id}/status")
async def plan_status(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    try:
        plans.set_status(conn, plan_id, str(form.get("status") or ""))
    except PlanError as e:
        return _go(request, f"/data/plans/{plan_id}/detail", err=str(e))
    return _go(request, f"/data/plans/{plan_id}/detail")


@router.post("/data/plans/{plan_id}/export")
async def plan_export(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    try:
        ref = export.export_plan(conn, plan_id, str(form.get("project") or "") or None)
    except (ExportError, FileExistsError) as e:
        return _go(request, f"/data/plans/{plan_id}/detail", err=str(e))
    return _go(request, f"/data/plans/{plan_id}/detail", msg=f"已导出为 research-design 成果 {ref}")


@router.post("/data/plans/{plan_id}/deck")
async def plan_deck_make(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Card 0190: the 展示稿 (.pptx) of the plan. Off the event loop: it crops pages out of the paper's PDF and draws every
    slide, and stops itself past deck.DECK_SECONDS."""
    _plan_or_404(conn, plan_id)
    form = await request.form()
    try:
        done = await run_in_threadpool(deck.make, conn, plan_id, seminar_date=str(form.get("seminar_date") or ""))
    except (deck.DeckError, W.WorkspaceError, OSError) as e:
        return _go(request, f"/data/plans/{plan_id}/deck?{urlencode({'err': str(e)})}")
    text = f"展示稿已生成，共 {done.slides} 页" + ("。" + "；".join(done.notes[:2]) if done.notes else "")
    return _go(request, f"/data/plans/{plan_id}/deck?{urlencode({'msg': text})}")


@router.get("/data/plans/{plan_id}/deck", response_class=HTMLResponse)
def plan_deck_page(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Card 0196: the deck as slides on a page, drawn from what the last make saved beside the pptx."""
    from ...replication import report_content, slides
    from ..context import templates
    plan = _plan_or_404(conn, plan_id)
    info = deck.info(plan_id)
    saved = report_content.load_saved(plan_id) if info["exists"] else None
    html = ""
    if saved is not None:
        html = slides.render_html(saved, lambda name: f"/data/plans/{plan_id}/deck/asset/{name}", templates().env)
    title = str(plan.get("paper_title") or plan_id)
    return render(request, conn, "pages/data_deck.html", title=f"展示稿 · {title}", plan=plan, info=info,
                  deck_html=html, slides_n=len(saved.slides) if saved else 0, short_title=_short(title),
                  at=report_content.saved_at(plan_id) if saved else "",
                  msg=request.query_params.get("msg"), err=request.query_params.get("err"))


def _short(title: str, n: int = 28) -> str:
    return title if len(title) <= n else title[:n].rstrip() + "…"


EDITS_BYTES = 256 * 1024


@router.post("/data/plans/{plan_id}/deck/edits")
async def plan_deck_edits(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Leo's edits to the deck's words, from the deck page: {"changes": {"<slide>|<field>": text}}. Kept beside the
    slides and written into the pptx at once; a remake keeps them on the same slides."""
    from ...replication import report_content
    _plan_or_404(conn, plan_id)
    size = request.headers.get("content-length") or ""
    if not size.isdigit() or int(size) > EDITS_BYTES:
        raise HTTPException(413, "改动太多，分几次保存")
    raw = await request.body()
    if len(raw) > EDITS_BYTES:
        raise HTTPException(413, "改动太多，分几次保存")
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        raise HTTPException(400, "不是 JSON")
    changes = body.get("changes") if isinstance(body, dict) else None
    at = body.get("at") if isinstance(body, dict) else None
    if not isinstance(changes, dict) or (at is not None and not isinstance(at, str)):
        raise HTTPException(400, "没有改动")
    try:
        kept = await run_in_threadpool(report_content.save_edits, plan_id, changes, at)
        await run_in_threadpool(report_content.rewrite_pptx, plan_id)
    except (deck.DeckError, W.WorkspaceError, OSError) as e:
        raise HTTPException(409, str(e))
    return {"saved": kept}


@router.get("/data/plans/{plan_id}/deck/asset/{name}")
def plan_deck_asset(plan_id: str, name: str, conn: sqlite3.Connection = Depends(get_conn)):
    """One picture of the deck preview, by its plain name, from the deck's own folder only."""
    from ...replication import report_content, slides
    _plan_or_404(conn, plan_id)
    path = slides.asset_file(report_content.assets_dir(plan_id), name)
    if path is None:
        raise HTTPException(404, "没有这张图")
    return FileResponse(path, media_type="image/png" if name.endswith(".png") else "image/svg+xml",
                        headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'"})


@router.get("/data/plans/{plan_id}/deck.pptx")
def plan_deck_download(plan_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    """The one file the deck button wrote, as an attachment; nothing else in the workspace is reachable from here."""
    _plan_or_404(conn, plan_id)
    path = deck.download_path(plan_id)
    if path is None:
        raise HTTPException(404, "这份方案还没有生成展示稿")
    return FileResponse(path, media_type=deck.MIME, filename=f"{plan_id}-deck.pptx", content_disposition_type="attachment",
                        headers={"Cache-Control": "no-store"})


@router.post("/data/plans/{plan_id}/delete")
def plan_delete(plan_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        plans.delete(conn, plan_id)
    except PlanError as e:
        return _go(request, "/data", err=str(e))
    return _go(request, "/data", msg=f"已删除方案 {plan_id}")


# ---------------------------------------------------------------- runs

def _run_form_ctx(conn: sqlite3.Connection, method: str | None, dataset: str | None, plan: str | None,
                  values: dict[str, Any] | None = None, error: str = "") -> dict[str, Any]:
    ds = datasets.get(conn, dataset) if dataset else None
    all_ds = datasets.list_all(conn)
    if ds is None and len(all_ds) == 1:
        ds = datasets.get(conn, all_ds[0]["id"])
    m = runs.METHODS.get(method or "")
    kinds = datasets.column_kinds(ds) if ds else {}
    return {"method": m, "method_key": method or "", "methods": runs.METHODS, "ds": ds, "all_datasets": all_ds,
            "kinds": kinds, "numeric_cols": [c for c, k in kinds.items() if k in ("integer", "number")],
            "plan_id": plan or "", "all_plans": plans.list_all(conn), "values": values or {}, "error": error}


@router.get("/data/runs/new")
def run_new(request: Request, method: str | None = None, dataset: str | None = None, plan: str | None = None,
            conn: sqlite3.Connection = Depends(get_conn)):
    if method and method not in runs.METHODS:
        raise HTTPException(404, f"未知方法 {method}")
    return render(request, conn, "pages/data_run.html", title="新建分析", wide=True, mode="new",
                  **_run_form_ctx(conn, method, dataset, plan))


@router.post("/data/runs")
async def run_create(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    method = str(form.get("method") or "")
    dataset = str(form.get("dataset_id") or "")
    plan = str(form.get("plan_id") or "") or None
    values = {k: (form.getlist(k) if len(form.getlist(k)) > 1 or k in ("x", "vars") else form.get(k)) for k in form.keys()}
    try:
        params = runs.parse_params(method, values)
        run_id = await run_in_threadpool(runs.create, conn, method, dataset, params, plan_id=plan)
    except USER_ERRORS as e:
        ctx = _run_form_ctx(conn, method, dataset, plan, values, str(e))
        return render(request, conn, "pages/data_run.html", title="新建分析", wide=True, mode="new", **ctx)
    return _go(request, f"/data/runs/{run_id}")


def _run_or_404(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    r = runs.get(conn, run_id)
    if r is None:
        raise HTTPException(404, f"没有运行 {run_id}")
    return r


@router.get("/data/runs/{run_id}")
def run_page(run_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    run = _run_or_404(conn, run_id)
    job = queue.latest_for(conn, "analysis.read", run_id)
    check = None
    if run.get("check_id"):
        check = conn.execute("SELECT id, title, status, project_slug FROM checks WHERE id = ?", (run["check_id"],)).fetchone()
    return render(request, conn, "pages/data_run.html", title=run["method_label"], wide=True, mode="run",
                  run=run, has_figure=runs.figure_file(run) is not None, job=_job_view(conn, job) if job and job["status"] != "done" else None,
                  checks=runs.open_checks(conn), check=check, projects=_projects(), default_project=export.default_project(),
                  plan=plans.get(conn, run["plan_id"]) if run.get("plan_id") else None, **_flash(request))


@router.get("/data/runs/{run_id}/figure.png")
def run_figure(run_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    run = _run_or_404(conn, run_id)
    f = runs.figure_file(run)
    if f is None:
        raise HTTPException(404, "这个运行没有图")
    return FileResponse(f, media_type="image/png", headers={"Cache-Control": "no-cache"})


@router.get("/data/runs/{run_id}/code.py")
def run_code(run_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    run = _run_or_404(conn, run_id)
    return Response(run.get("code") or "", media_type="text/x-python; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{run_id}.py"'})


@router.post("/data/runs/{run_id}/rerun")
async def run_rerun(run_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _run_or_404(conn, run_id)
    try:
        status = await run_in_threadpool(runs.rerun, conn, run_id)
    except RunError as e:
        return _go(request, f"/data/runs/{run_id}", err=str(e))
    return _go(request, f"/data/runs/{run_id}", **({"msg": "已重新运行"} if status == "done" else {"err": "重新运行失败，见下方错误"}))


@router.post("/data/runs/{run_id}/reading")
async def run_reading(run_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _run_or_404(conn, run_id)
    form = await request.form()
    runs.set_reading(conn, run_id, str(form.get("reading") or ""))
    if request.headers.get("hx-request") == "true" and request.headers.get("hx-target"):
        return HTMLResponse('<span class="saved-note" role="status">已保存</span>')
    return _go(request, f"/data/runs/{run_id}", msg="解读已保存")


@router.post("/data/runs/{run_id}/ai-read")
def run_ai_read(run_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _run_or_404(conn, run_id)
    try:
        job_id = runs.request_reading(conn, run_id, force=True)
    except (RunError, LookupError) as e:
        return _go(request, f"/data/runs/{run_id}", err=str(e))
    if request.headers.get("hx-request") == "true" and request.headers.get("hx-target") and job_id:
        return render(request, conn, "partials/_data_job.html", job=_job_view(conn, queue.get(conn, job_id)))
    return _go(request, f"/data/runs/{run_id}", msg="已请求解读建议")


@router.post("/data/runs/{run_id}/check")
async def run_check(run_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    try:
        runs.attach_check(conn, run_id, int(str(form.get("check_id") or "0") or 0))
    except (RunError, ValueError) as e:
        return _go(request, f"/data/runs/{run_id}", err=str(e))
    return _go(request, f"/data/runs/{run_id}", msg="已记为检验结果")


@router.post("/data/runs/{run_id}/export")
async def run_export(run_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    try:
        ref = export.export_run(conn, run_id, str(form.get("project") or "") or None)
    except (ExportError, FileExistsError) as e:
        return _go(request, f"/data/runs/{run_id}", err=str(e))
    return _go(request, f"/data/runs/{run_id}", msg=f"已导出为 analysis 成果 {ref}")


@router.post("/data/runs/{run_id}/delete")
def run_delete(run_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _run_or_404(conn, run_id)
    runs.delete(conn, run_id)
    return _go(request, "/data", msg=f"已删除运行 {run_id}")


# ---------------------------------------------------------------- 复现数据链接 → List files → Import (card 0165)

def _sources_version(p: dict[str, Any]) -> str:
    """The plan's 复现数据链接 as the page drew them. List files and Import name a link by its row, so a page drawn
    before the links changed is refused (409) instead of listing or downloading from another link."""
    urls = [str(link.get("url") or "") for link in p["plan"].get("links") or []]
    return hashlib.sha256(json.dumps(urls, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _fetch_panel(request: Request, conn: sqlite3.Connection, p: dict[str, Any], status: int = 200, **ctx: Any) -> Response:
    base = {"index": None, "url": "", "version": _sources_version(p), "listing": None, "auto": None, "results": None,
            "error": "", "stale": ""}
    response = render(request, conn, "partials/_plan_fetch.html", p=p, **{**base, **ctx})
    response.status_code = status
    return response


def _plan_link(request: Request, conn: sqlite3.Connection, plan_id: str, index: int,
               version: str) -> tuple[dict[str, Any], str, Response | None]:
    """The link the page's row names, or the 409 panel when the page is older than the links."""
    p = _plan_or_404(conn, plan_id)
    if version != _sources_version(p):
        return p, "", _fetch_panel(request, conn, p, 409,
                                   stale="复现数据链接在别处改过，这一页已经过期，什么都没有下载。刷新页面后再试。")
    links = p["plan"].get("links") or []
    if not 0 <= index < len(links):
        raise HTTPException(404, "没有这条复现数据链接")
    return p, str(links[index]["url"]), None


@router.get("/data/plans/{plan_id}/links/{index}/files")
async def plan_link_files(plan_id: str, index: int, request: Request, v: str = "",
                          conn: sqlite3.Connection = Depends(get_conn)):
    """What the link offers, in a worker thread so the page stays usable. The scripts and documents are read as text to find
    the main analysis; no data file is downloaded until Import."""
    p, url, stale = _plan_link(request, conn, plan_id, index, v)
    if stale:
        return stale
    try:
        listing = await run_in_threadpool(fetch.list_source, url)
    except fetch.FetchError as e:
        return _fetch_panel(request, conn, p, index=index, url=url, error=str(e))
    except Exception:  # noqa: BLE001 - the panel says so instead of a silent 500
        log.exception("listing %s failed", url)
        return _fetch_panel(request, conn, p, index=index, url=url, error="列出文件时出错了，稍后再试。")
    try:        # the author's code (read as text, never run) says which files the main analysis needs
        auto = await run_in_threadpool(fetch.plan_auto, listing)
    except Exception:  # noqa: BLE001 - the list still shows; only the pre-ticking is lost
        log.exception("reading the code of %s failed", url)
        auto = fetch.AutoPlan(listing, note="没能读取作者代码，没有自动勾选；请手动选。")
    return _fetch_panel(request, conn, p, index=index, url=url, listing=listing, auto=auto)


@router.post("/data/plans/{plan_id}/links/{index}/import")
async def plan_link_import(plan_id: str, index: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Download only the files Leo ticked, import them and attach each dataset to the plan; one line per file."""
    form = await request.form()
    p, url, stale = _plan_link(request, conn, plan_id, index, str(form.get("sources_version") or ""))
    if stale:
        return stale
    picks = [str(x) for x in form.getlist("pick") if str(x).strip()]
    if not picks:
        return _fetch_panel(request, conn, p, index=index, url=url, error="先勾选要导入的文件，再点 Import。")
    try:
        results = await run_in_threadpool(lambda: fetch.fetch(conn, url, picks, plan_id=plan_id))
    except fetch.FetchError as e:
        return _fetch_panel(request, conn, p, index=index, url=url, error=str(e))
    except Exception:  # noqa: BLE001
        log.exception("import from %s failed", url)
        return _fetch_panel(request, conn, p, index=index, url=url, error="导入中途出错了；已经导入的数据集保留在方案里。")
    p = _plan_or_404(conn, plan_id)
    return _fetch_panel(request, conn, p, index=index, url=url, results=results, attached=_attached(conn, p),
                        all_datasets=datasets.list_all(conn))
