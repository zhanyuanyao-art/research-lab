"""Analysis runs: the methods menu, form parsing, in-process execution with a 60 s guard, figures,
reproducible code, the user's reading, the analysis.read suggestion, and linking a run to a check.

A run's numbers come only from executed code. `reading` is written by the user; `ai_reading`
holds the analysis.read suggestion and is never copied into a check, an artifact's Claim or a verdict."""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..config import get_paths
from ..db.connection import next_sequence_id, now_iso
from . import datasets, network, stats, store
from .stats import Outcome, StatsError

TIMEOUT_S = 60


@dataclass
class Field:
    name: str
    kind: str                 # col | cols | choice | bool | text
    label: str
    required: bool = False
    numeric: bool = False     # restrict column choices to numeric columns
    choices: list[tuple[str, str]] = field(default_factory=list)
    default: Any = None
    help: str = ""


@dataclass
class Method:
    key: str
    label: str
    fn: Callable[..., Outcome]
    fields: list[Field]
    blurb: str = ""


SE_CHOICES = [("classical", "常规"), ("robust", "稳健 (HC1)"), ("cluster", "聚类")]

METHODS: dict[str, Method] = {m.key: m for m in [
    Method("describe", "描述统计", stats.describe,
           [Field("vars", "cols", "变量", help="留空 = 全部列（最多 40 个）")],
           "均值、标准差、分位数、缺失；分类变量的频数；直方图。"),
    Method("crosstab", "交叉表与χ²", stats.crosstab,
           [Field("row", "col", "行变量", required=True), Field("col", "col", "列变量", required=True)],
           "频数、行百分比、χ² 独立性检验、Cramér's V。"),
    Method("correlation", "相关矩阵", stats.correlation,
           [Field("vars", "cols", "数值变量", required=True, numeric=True),
            Field("corr_method", "choice", "方法", choices=[("pearson", "Pearson"), ("spearman", "Spearman")], default="pearson")],
           "两两相关与 p 值（列表删除）；热图。"),
    Method("ttest", "均值差异 t 检验", stats.ttest,
           [Field("y", "col", "结果变量", required=True, numeric=True), Field("group", "col", "分组变量", required=True),
            Field("group_a", "text", "组 A 取值", help="分组变量多于两个取值时必填"), Field("group_b", "text", "组 B 取值"),
            Field("equal_var", "bool", "假定方差相等（Student）", default=False)],
           "两组均值差、Welch/Student t 检验、差值的 95% 置信区间、Cohen's d。"),
    Method("ols", "OLS", stats.ols,
           [Field("y", "col", "因变量", required=True, numeric=True), Field("x", "cols", "自变量", required=True),
            Field("se", "choice", "标准误", choices=SE_CHOICES, default="classical"), Field("cluster", "col", "聚类变量")],
           "系数、标准误、p、95% 置信区间、R²；系数图。文本型自变量自动作为分类变量。"),
    Method("logit", "Logit", stats.logit,
           [Field("y", "col", "二分因变量", required=True), Field("x", "cols", "自变量", required=True),
            Field("positive", "text", "编码为 1 的取值", help="因变量不是 0/1 时指定"),
            Field("se", "choice", "标准误", choices=SE_CHOICES, default="classical"), Field("cluster", "col", "聚类变量")],
           "对数几率系数、几率比、McFadden 伪 R²；系数图。"),
    Method("fe", "固定效应", stats.fixed_effects,
           [Field("y", "col", "因变量", required=True, numeric=True), Field("x", "cols", "自变量", required=True),
            Field("entity", "col", "个体变量 (entity)", required=True), Field("time", "col", "时间变量（双向固定效应，可选）"),
            Field("se", "choice", "标准误", choices=SE_CHOICES, default="cluster"), Field("cluster", "col", "聚类变量", help="留空 = 按个体聚类")],
           "个体虚拟变量（statsmodels 公式 C(entity)），可选时间虚拟变量；默认按个体聚类标准误。"),
    Method("network", "网络指标", network.run,
           [Field("source", "col", "起点 source", required=True), Field("target", "col", "终点 target", required=True),
            Field("weight", "col", "权重（可选）", numeric=True), Field("year", "col", "年份/时期（可选，按期分别计算）"),
            Field("group", "col", "起点所属组（可选）", help="给出组才计算 Gould–Fernandez 中介角色"),
            Field("target_group", "col", "终点所属组（可选）"),
            Field("directed", "bool", "有向网络", default=False)],
           "度数、中介、接近中心度；Freeman 中心势；可按年；给出组时计算 Gould–Fernandez 中介角色。"),
]}


class RunError(ValueError):
    pass


# ---------------------------------------------------------------- params

def parse_params(method: str, form: dict[str, Any]) -> dict[str, Any]:
    """Form values (str or list) → params dict for the method, dropping empty optional fields."""
    m = METHODS.get(method)
    if not m:
        raise RunError(f"未知方法：{method}")
    out: dict[str, Any] = {}
    for f in m.fields:
        raw = form.get(f.name)
        if f.kind == "cols":
            vals = raw if isinstance(raw, list) else ([raw] if raw else [])
            vals = [v for v in (str(x).strip() for x in vals) if v]
            if vals:
                out[f.name] = list(dict.fromkeys(vals))
        elif f.kind == "bool":
            if isinstance(raw, list):
                raw = raw[-1] if raw else None
            out[f.name] = str(raw).lower() in ("1", "true", "on", "yes") if raw is not None else bool(f.default)
        else:
            if isinstance(raw, list):
                raw = raw[-1] if raw else None
            v = str(raw).strip() if raw is not None else ""
            if v:
                out[f.name] = v
            elif f.default is not None and f.kind == "choice":
                out[f.name] = f.default
    for f in m.fields:
        if f.required and not out.get(f.name):
            raise StatsError(f"请填写：{f.label}")
    return out


def params_label(method: str, params: dict[str, Any]) -> str:
    m = METHODS.get(method)
    if not m:
        return json.dumps(params, ensure_ascii=False)
    parts = []
    for f in m.fields:
        v = params.get(f.name)
        if v in (None, "", [], False):
            continue
        if f.kind == "choice":
            v = dict(f.choices).get(v, v)
        elif f.kind == "bool":
            v = "是"
        elif isinstance(v, list):
            v = "、".join(v)
        parts.append(f"{f.label}：{v}")
    return "；".join(parts)


# ---------------------------------------------------------------- execution

def execute(method: str, dataset_id: str, params: dict[str, Any], timeout: float = TIMEOUT_S) -> Outcome:
    m = METHODS.get(method)
    if not m:
        raise RunError(f"未知方法：{method}")
    parquet = str(store.parquet_path(dataset_id))

    def work() -> Outcome:
        df = datasets.load(dataset_id)
        return m.fn(df, params, parquet=parquet)

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rl-run")
    fut = pool.submit(work)
    try:
        return fut.result(timeout=timeout)
    except FutureTimeout:
        raise RunError(f"运行超过 {int(timeout)} 秒，已放弃（请缩小数据或减少变量）") from None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _code_header(run_id: str, method: str, ds: dict[str, Any], params: dict[str, Any]) -> str:
    return (f"# {run_id} · {METHODS[method].label} · {ds['id']} {ds['name']}\n"
            f"# params: {json.dumps(params, ensure_ascii=False)}\n"
            f"# generated {datetime.now():%Y-%m-%d %H:%M} by research-lab; run with `uv run python this_file.py`\n")


def _save_figure(run_id: str, fig) -> str | None:
    path = store.figures_dir() / f"{run_id}.png"
    if fig is None:
        path.unlink(missing_ok=True)
        return None
    fig.savefig(path, dpi=144, bbox_inches="tight")
    return str(path.relative_to(get_paths().data))


def figure_file(run: dict[str, Any]) -> Path | None:
    if not run.get("figure_path"):
        return None
    p = get_paths().data / run["figure_path"]
    return p if p.exists() else None


def _result_payload(outcome: Outcome, method: str, ds: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    return {**outcome.result(), "method": method, "method_label": METHODS[method].label,
            "dataset": {"id": ds["id"], "name": ds["name"], "rows": ds["rows"]},
            "params_label": params_label(method, params)}


def create(conn: sqlite3.Connection, method: str, dataset_id: str, params: dict[str, Any], *,
           plan_id: str | None = None, check_id: int | None = None) -> str:
    """Execute and store. Raises StatsError/RunError without creating a row when the run cannot execute."""
    ds = datasets.get(conn, dataset_id)
    if not ds:
        raise RunError(f"没有这个数据集：{dataset_id}")
    if method not in METHODS:
        raise RunError(f"未知方法：{method}")
    if plan_id and not conn.execute("SELECT 1 FROM replication_plans WHERE id = ?", (plan_id,)).fetchone():
        raise RunError(f"没有这个复现方案：{plan_id}")
    outcome = execute(method, dataset_id, params)
    run_id = next_sequence_id(conn, "analysis_runs", "RUN")
    fig_path = _save_figure(run_id, outcome.figure)
    now = now_iso()
    conn.execute(
        "INSERT INTO analysis_runs(id, dataset_id, plan_id, check_id, method, params_json, result_json, figure_path, code, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, dataset_id, plan_id or None, None, method, json.dumps(params, ensure_ascii=False),
         json.dumps(_result_payload(outcome, method, ds, params), ensure_ascii=False), fig_path,
         _code_header(run_id, method, ds, params) + outcome.code, "done", now, now))
    if check_id:
        conn.execute("UPDATE analysis_runs SET check_id = ? WHERE id = ?", (int(check_id), run_id))
    return run_id


def rerun(conn: sqlite3.Connection, run_id: str) -> str:
    """Re-execute with the stored params on the dataset as it is now. Returns 'done' or 'error'."""
    run = get(conn, run_id)
    if not run:
        raise RunError(f"没有这个运行：{run_id}")
    if run["method"] == "workspace":          # card 0158: its code lives in the replication workspace, not in params
        raise RunError("复现工作区的运行请在复现工作区里点 Re-run。")
    ds = datasets.get(conn, run["dataset_id"]) if run["dataset_id"] else None
    if not ds:
        conn.execute("UPDATE analysis_runs SET status='error', result_json=?, updated_at=? WHERE id=?",
                     (json.dumps({**(run["result"] or {}), "error": "数据集已删除，无法重新运行"}, ensure_ascii=False), now_iso(), run_id))
        return "error"
    try:
        outcome = execute(run["method"], ds["id"], run["params"])
    except (StatsError, RunError) as e:
        conn.execute("UPDATE analysis_runs SET status='error', result_json=?, updated_at=? WHERE id=?",
                     (json.dumps({**(run["result"] or {}), "error": str(e)}, ensure_ascii=False), now_iso(), run_id))
        return "error"
    fig_path = _save_figure(run_id, outcome.figure)
    conn.execute("UPDATE analysis_runs SET result_json=?, figure_path=?, code=?, status='done', updated_at=? WHERE id=?",
                 (json.dumps(_result_payload(outcome, run["method"], ds, run["params"]), ensure_ascii=False), fig_path,
                  _code_header(run_id, run["method"], ds, run["params"]) + outcome.code, now_iso(), run_id))
    return "done"


# ---------------------------------------------------------------- records

def _decode(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["params"] = json.loads(r["params_json"] or "{}")
    d["result"] = json.loads(r["result_json"]) if r["result_json"] else None
    d["ai"] = json.loads(r["ai_reading"]) if r["ai_reading"] else None
    d["method_label"] = METHODS[r["method"]].label if r["method"] in METHODS else r["method"]
    return d


def get(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT r.*, d.name AS dataset_name FROM analysis_runs r LEFT JOIN datasets d ON d.id = r.dataset_id "
                     "WHERE r.id = ?", (run_id,)).fetchone()
    return _decode(r) if r else None


def list_all(conn: sqlite3.Connection, *, dataset_id: str | None = None, plan_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    sql, args = ("SELECT r.*, d.name AS dataset_name FROM analysis_runs r LEFT JOIN datasets d ON d.id = r.dataset_id WHERE 1=1"), []
    if dataset_id:
        sql += " AND r.dataset_id = ?"; args.append(dataset_id)
    if plan_id:
        sql += " AND r.plan_id = ?"; args.append(plan_id)
    sql += " ORDER BY r.created_at DESC, r.id DESC LIMIT ?"; args.append(limit)
    return [_decode(r) for r in conn.execute(sql, args)]


def set_reading(conn: sqlite3.Connection, run_id: str, text: str) -> None:
    """The user's reading. Never filled from ai_reading."""
    cur = conn.execute("UPDATE analysis_runs SET reading = ?, updated_at = ? WHERE id = ?", ((text or "").strip() or None, now_iso(), run_id))
    if cur.rowcount == 0:
        raise RunError(f"没有这个运行：{run_id}")


def delete(conn: sqlite3.Connection, run_id: str) -> None:
    run = get(conn, run_id)
    if not run:
        raise RunError(f"没有这个运行：{run_id}")
    f = figure_file(run)
    if f:
        f.unlink(missing_ok=True)
    conn.execute("DELETE FROM analysis_runs WHERE id = ?", (run_id,))


def request_reading(conn: sqlite3.Connection, run_id: str, *, force: bool = False) -> str | None:
    """Queue analysis.read for a run and dispatch it. Returns the job id (None if one is open)."""
    from ..ai import dispatch, queue
    run = get(conn, run_id)
    if not run or run["status"] != "done":
        raise RunError("只有成功完成的运行可以请求解读建议")
    job_id = queue.enqueue(conn, "analysis.read", run_id, force=force)
    if job_id:
        dispatch.dispatch(job_id)
    return job_id


# ---------------------------------------------------------------- checks

def result_note(run: dict[str, Any]) -> str:
    """A mechanical one-line record of the numbers (no interpretation)."""
    res = run.get("result") or {}
    s = res.get("summary") or {}
    head = f"{run['id']} · {run['method_label']} · {res.get('dataset', {}).get('id', run.get('dataset_id'))}: n={s.get('n')}，缺失删除 {s.get('missing_dropped', 0)}"
    bits = []
    m = run["method"]
    if m in ("ols", "logit", "fe"):
        coef = next((t for t in res.get("tables", []) if t.get("kind") == "coef"), None)
        for row in (coef or {}).get("rows", []):
            if row[0] == "截距":
                continue
            bits.append(f"{row[0]} {_f(row[1])} (SE {_f(row[2])}, p={_f(row[4])}, 95% CI [{_f(row[5])}, {_f(row[6])}])")
        if m == "logit":
            bits.append(f"伪 R²={_f(s.get('pseudo_r2'))}")
        else:
            bits.append(f"R²={_f(s.get('r2'))}")
    elif m == "crosstab":
        bits.append(f"χ²={_f(s.get('chi2'))}, df={s.get('df')}, p={_f(s.get('p'))}, V={_f(s.get('cramers_v'))}")
    elif m == "ttest":
        bits.append(f"差值={_f(s.get('diff'))}, t={_f(s.get('t'))}, p={_f(s.get('p'))}, 95% CI [{_f(s.get('ci_low'))}, {_f(s.get('ci_high'))}]")
    elif m == "network":
        bits.append(f"{s.get('periods')} 期；末期 {s.get('nodes')} 节点 {s.get('edges')} 边，度数中心势 {_f(s.get('degree_centralization'))}，"
                    f"中介中心势 {_f(s.get('betweenness_centralization'))}")
    elif m == "correlation":
        bits.append(f"{s.get('method')} 相关矩阵")
    return head + ("；" + "；".join(bits) if bits else "")


def _f(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.3g}" if abs(v) < 1e-3 or abs(v) >= 1e4 else f"{v:.3f}".rstrip("0").rstrip(".")
    return str(v)


def attach_check(conn: sqlite3.Connection, run_id: str, check_id: int) -> None:
    """User action: record this run as a method check's result (status 'recorded', result_note,
    script_ref = run id). The check's reading is the user's run reading, which must exist."""
    from ..method import checks
    run = get(conn, run_id)
    if not run:
        raise RunError(f"没有这个运行：{run_id}")
    if run["status"] != "done":
        raise RunError("运行出错，不能作为检验结果")
    if not (run.get("reading") or "").strip():
        raise RunError("先写下你自己的解读，再把运行记为检验结果")
    k = checks.get(conn, int(check_id))
    if not k:
        raise RunError(f"没有这个检验：{check_id}")
    if k["origin"] == "ai-suggested":
        raise RunError("这个检验还是 AI 建议，请先在检验页接受它")
    if k["status"] in ("planned", "data-ready"):
        checks.set_status(conn, k["id"], "run")
    checks.update(conn, k["id"], result_note=result_note(run), reading=run["reading"], script_ref=run_id)
    checks.set_status(conn, k["id"], "recorded")
    conn.execute("UPDATE analysis_runs SET check_id = ?, updated_at = ? WHERE id = ?", (k["id"], now_iso(), run_id))


def open_checks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT id, project_slug, title, status, origin FROM checks WHERE origin = 'user' ORDER BY project_slug, status = 'recorded', id")]
