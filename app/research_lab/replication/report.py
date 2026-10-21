"""Replication v2 step 5 (card 0162, docs/design/REPLICATION-v2.md 第五步): the 复现报告, Markdown built by rules.

build() is pure: no DB, no AI, inputs are never mutated. Its inputs, in the shapes of the design's appendix:

- plan: plans.get(), {"id", "paper_id", "paper_title", "direction", "status", "targets_reviewed_at", "plan": {...}}.
- targets: plans.target_records(); value_text / se_text, when present, are what the paper printed.
- runs: run views, {"id", "label", "code": [{"name", "text"}], "tables", "figures", "needs_r", "reading",
  "extension"}; run_view() makes one from a stored analysis run (a method run, or a 复现工作区 run of card 0158).
  A run with "extension" true is a 扩展 (a robustness check or other data); the others are the replication.
- comparison: card 0160's compare() output, {"items": [...], "summary": {...}}, or None before step 4 (「尚未对照」).

save() is the one write: the report as a draft 研究档案 (type analysis), direction from the plan. The possible reasons
it lists are rule-built hints read off the numbers, not findings: nothing here says a paper is wrong."""
from __future__ import annotations

import html
import json
import re
import sqlite3
from typing import Any

from . import plans
from .export import md_table

# An absolute home path in a code excerpt (a script's own path, a traceback) says whose Mac it ran on: shown as ~
# (0162 review). The expanded $HOME is replaced first, then any /Users/<name>.
_HOME_PATH = re.compile(r"/Users/[^/\s'\"`)\]]+")


def tilde(text: str) -> str:
    from pathlib import Path
    home = str(Path.home())
    if home and home not in ("/", ""):
        text = text.replace(home, "~")
    return _HOME_PATH.sub("~", text)


def tilde_all(obj: Any) -> Any:
    """tilde() through every string of a run view or a comparison (notes, needs_r, labels, readings, reasons, code),
    without mutating the input (card 0169: run-derived text of every kind, not only code)."""
    if isinstance(obj, str):
        return tilde(obj)
    if isinstance(obj, list):
        return [tilde_all(x) for x in obj]
    if isinstance(obj, dict):
        return {k: tilde_all(v) for k, v in obj.items()}
    return obj


VERDICT_LABELS = {"exact": "完全一致", "within": "在误差范围内", "direction": "方向一致但大小或显著性不同",
                  "mismatch": "对不上", "missing": "没找到对应输出"}
NOT_COMPARED = "尚未对照"
EXCERPT_LINES = 30
STAR = r"\*"            # a star escaped, so *** in a table cell stays three stars


def _esc(v: Any) -> str:
    """Text from a paper, a plan or the AI, kept as text: no HTML, no stray links."""
    text = html.escape(" ".join(str(v if v is not None else "").split()), quote=False)
    return text.replace("[", r"\[").replace("]", r"\]").replace("*", r"\*")


def _num(x: Any) -> str:
    if x is None or x == "":
        return ""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return str(x)
    if isinstance(x, int) or float(x).is_integer():
        return str(int(x))
    return f"{x:.4g}" if abs(x) < 1e-3 and x != 0 else f"{x:.4f}".rstrip("0").rstrip(".")


def _url(u: str) -> str:
    return re.sub(r"[\s<>]", "", str(u or ""))


# ── run views ────────────────────────────────────────────────────────────────────────────────────────────────

def _split_code(text: str, names: list[str]) -> list[dict[str, str]]:
    """A workspace run stores its scripts as one text, each under a `# scripts/x.py` line, in run order."""
    heads = [(text.find(f"# {n}\n"), n) for n in names]
    heads = sorted((i, n) for i, n in heads if i >= 0)
    out = []
    for k, (i, n) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else len(text)
        out.append({"name": n, "text": text[i + len(n) + 3:end].rstrip("\n")})
    return out


def run_view(run: dict[str, Any], *, extension: bool = False) -> dict[str, Any]:
    """A stored analysis run (runs.get() or a workspace run) in the shape build() reads."""
    ws = run.get("workspace") or run.get("workspace_json") or {}
    if isinstance(ws, str):
        ws = json.loads(ws or "{}")
    result = run.get("result") or {}
    rid = str(run.get("id") or "")
    if ws:
        code = _split_code(run.get("code") or "", [str(n) for n in ws.get("code") or []])
        return {"id": rid, "label": "复现工作区" + ("（重跑）" if ws.get("mode") == "rerun" else ""), "code": code,
                "tables": ((result.get("results") or {}).get("tables") or []), "figures": list(ws.get("figures") or []),
                "needs_r": list(ws.get("needs_r") or []), "reading": run.get("reading") or "", "extension": extension}
    label = " · ".join(x for x in (run.get("method_label") or run.get("method"), result.get("params_label")) if x)
    return {"id": rid, "label": label, "code": [{"name": f"{rid}.py", "text": run.get("code") or ""}] if run.get("code") else [],
            "tables": result.get("tables") or [], "figures": [f"/data/runs/{rid}/figure.png"] if run.get("figure_path") else [],
            "needs_r": [], "reading": run.get("reading") or "", "extension": extension}


# ── sections ─────────────────────────────────────────────────────────────────────────────────────────────────

def _summary(plan: dict[str, Any], targets: list[dict[str, Any]]) -> list[str]:
    pl = plan.get("plan") or {}
    model, feas = pl.get("model") or {}, pl.get("feasibility") or {}
    paper = f"，论文 {plan['paper_id']}《{_esc(plan.get('paper_title'))}》" if plan.get("paper_id") else ""
    out = [f"复现方案 {plan.get('id')}（{plans.STATUS_LABELS.get(plan.get('status'), plan.get('status') or '')}）{paper}。", "",
           md_table(["项目", "内容"], [["分析单位", _esc(pl.get("unit_of_analysis"))], ["时期", _esc(pl.get("period"))],
                                      ["样本规则", _esc(pl.get("sample_rule"))],
                                      ["模型", _esc(f"{model.get('family', '')}：{model.get('specification', '')}".strip("："))],
                                      ["可行性", _esc(f"{plans.LEVEL_LABELS.get(feas.get('level'), '')}：{feas.get('why', '')}".strip("："))]])]
    if pl.get("assumptions"):
        out += ["", "**识别假设**", "", *[f"- {_esc(a.get('text'))}（{_esc(a.get('status') or '待检验')}）" for a in pl["assumptions"]]]
    reviewed = "，已审阅" if plan.get("targets_reviewed_at") else "，还没审阅"
    out += ["", f"**复现目标**：{len(targets)} 条（论文报告值，照论文原样抄录{reviewed if targets else ''}）。"]
    return out


def _sources(plan: dict[str, Any], datasets: list[dict[str, Any]] | None) -> list[str]:
    pl = plan.get("plan") or {}
    out: list[str] = []
    if pl.get("data_sources"):
        out += [md_table(["名称", "获取方式", "位置"], [[_esc(s.get("name")), _esc(s.get("access")), _esc(s.get("url_or_where"))]
                                                     for s in pl["data_sources"]]), ""]
    if pl.get("links"):
        out += ["**数据链接**", ""]
        for link in pl["links"]:
            extra = "，".join(x for x in (plans.LINK_ACCESS_LABELS.get(link.get("access"), ""), _esc(link.get("note"))) if x)
            out.append(f"- {plans.LINK_KIND_LABELS.get(link.get('kind'), '其他')}：<{_url(link.get('url'))}>" + (f"（{extra}）" if extra else ""))
        out.append("")
    if datasets:
        out += ["**已关联数据集**", "", *[f"- {d.get('id')}：{_esc(d.get('name'))}" + (f"（{_num(d.get('rows'))} 行）" if d.get("rows") else "")
                                      for d in datasets], ""]
    elif pl.get("datasets"):
        out += ["**已关联数据集**：" + "、".join(str(x) for x in pl["datasets"]), ""]
    return out or ["方案里还没有写数据来源。"]


def _paper_cell(t: dict[str, Any]) -> str:
    value = t.get("value_text") or _num(t.get("value"))
    se = t.get("se_text") or (f"({_num(t.get('se'))})" if t.get("se") is not None else "")
    return _esc(" ".join(x for x in (value, se, t.get("sig") or "") if x)) or "—"


def _ours_cell(o: dict[str, Any] | None) -> str:
    if not o or o.get("estimate") is None:
        return "—"
    se = f" ({_num(o['se'])})" if o.get("se") is not None else ""
    return f"{_num(o['estimate'])}{se}" + STAR * int(o.get("band") or 0)


def _results(targets: list[dict[str, Any]], comparison: dict[str, Any] | None, runs: list[dict[str, Any]]) -> list[str]:
    items = {str(i.get("id")): i for i in (comparison or {}).get("items") or []}
    out = [_esc((comparison.get("summary") or {}).get("text")) if comparison else
           f"{NOT_COMPARED}：第四步对照跑完后重新生成报告，这里才有逐项判定。", ""]
    rows = []
    known = {str(x.get("id")) for x in targets}
    extra = [{**i, **{k: (i.get("paper") or {}).get(k) for k in ("value", "se", "sig", "n")}}   # a verdict for no stored target
             for k, i in items.items() if k not in known]
    for t in [*targets, *extra]:
        i = items.get(str(t.get("id")))
        o = (i or {}).get("ours")
        diff = (i or {}).get("diff")
        gap = (f"{diff:+.4g}" + (f"（{i['diff_se']:.2g} 个标准误）" if i.get("diff_se") is not None else "")) if diff is not None else "—"
        n_paper = t.get("n") if "n" in t else ((i or {}).get("paper") or {}).get("n")
        n = f"{_num(n_paper) or '—'} / {_num((o or {}).get('n')) or '—'}"
        verdict = VERDICT_LABELS.get((i or {}).get("verdict"), "") if i else NOT_COMPARED
        rows.append([_esc(t.get("id")), _esc(t.get("table")), _esc(t.get("model")), _esc(t.get("term")), _paper_cell(t),
                     _ours_cell(o), gap, n, verdict])
    out.append(md_table(["目标", "表", "模型", "项", "论文报告值", "复现值", "差", "样本量（论文 / 复现）", "判定"], rows)
               if rows else "方案里还没有复现目标。")
    for run in [r for r in runs if not r.get("extension")]:
        out += ["", f"### 运行 {run['id']} 的输出" + (f"（{_esc(run.get('label'))}）" if run.get("label") else ""), "", *_run_body(run)]
    return out


def _hints(i: dict[str, Any]) -> list[str]:
    """Possible reasons read off the numbers. The AI's list of reasons is a later step; these are only rules."""
    p, o, reasons = i.get("paper") or {}, i.get("ours") or {}, " ".join(i.get("reasons") or [])
    if i.get("verdict") == "missing":
        return ["论文用到只有 R 才有的方法，等装了 R 再补" if "需要 R" in reasons else
                "输出表的表名、模型列或变量名和论文不一致，可以在 mapping.json 里写明对应关系"]
    out = []
    if "符号相反" in reasons:
        out.append("变量的编码方向、处理组或对照组的定义不同")
    if (p.get("band") or 0) > 0 and o.get("band") == 0:
        out.append("论文显著而复现不显著：标准误算法（聚类层级、稳健标准误）不同，或样本变小了")
    if i.get("n_match") is False:
        out.append(f"样本不同（论文 {_num(p.get('n'))}、复现 {_num(o.get('n'))}）：样本规则、时期或缺失值处理")
    if p.get("se") and o.get("se") and not 2 / 3 <= o["se"] / p["se"] <= 1.5:
        out.append(f"标准误相差较大（论文 {_num(p['se'])}、复现 {_num(o['se'])}）：聚类层级或标准误算法")
    return out or ["变量构造（取对数、标准化、单位）、控制变量或软件默认设置不同"]


def _gaps(comparison: dict[str, Any] | None, runs: list[dict[str, Any]]) -> list[str]:
    if comparison is None:
        return [f"{NOT_COMPARED}。"]
    bad = [i for i in comparison.get("items") or [] if i.get("verdict") in ("mismatch", "direction", "missing")]
    out = []
    for i in bad:
        where = " ".join(x for x in (i.get("table"), i.get("model"), i.get("term")) if x)
        reasons = "；".join(i.get("reasons") or [])
        out.append(f"- **{_esc(i.get('id'))}**（{_esc(where)}）：{VERDICT_LABELS.get(i.get('verdict'), '')}"
                   + (f"。{_esc(reasons)}" if reasons else "") + f"。可能原因：{'；'.join(_esc(h) for h in _hints(i))}。")
    if out:
        out += ["", "以上可能原因是按数字规则给的提示，不是结论，要逐项核对。"]
    needs_r = [n for r in runs for n in r.get("needs_r") or []]
    if needs_r:
        out += ["", "**需要 R、还没复现的部分**", "", *[f"- {_esc(n)}" for n in needs_r]]
    return out or ["没有对不上的项。"]


def _cell_table(t: dict[str, Any]) -> list[str]:
    if "rows" in t and "columns" not in t:            # outputs/results.json (card 0158)
        head = " ".join(x for x in (t.get("label"), t.get("model")) if x) or "输出表"
        out = [f"**{_esc(head)}**" + (f" · {_esc(t['estimator'])}" if t.get("estimator") else ""), "",
               md_table(["项", "估计值", "标准误", "p", "区间", "样本量"],
                        [[_esc(r.get("term")), _num(r.get("estimate")), _num(r.get("se")), _num(r.get("p")),
                          "[" + ", ".join(_num(x) for x in r["ci"]) + "]" if isinstance(r.get("ci"), list) and len(r["ci"]) == 2 else "",
                          _num(r.get("n"))] for r in t.get("rows") or [] if isinstance(r, dict)])]
        notes = [*(t.get("notes") or [])]
    else:                                             # a method run's table (runs.py)
        out = [f"**{_esc(t.get('title') or '结果表')}**" + (f"（n = {_num(t.get('n'))}）" if t.get("n") is not None else ""), "",
               md_table([_esc(c) for c in t.get("columns") or []], [[_esc(_num(c)) for c in r] for r in t.get("rows") or []])]
        notes = [t["note"]] if t.get("note") else []
    return [*out, *(["", *[f"> 注：{_esc(n)}" for n in notes]] if notes else []), ""]


def _run_body(run: dict[str, Any]) -> list[str]:
    out = [line for t in run.get("tables") or [] if isinstance(t, dict) for line in _cell_table(t)] or ["这次运行没有输出表。", ""]
    for f in run.get("figures") or []:
        out.append(f"![{run['id']} 图](<{_url(f)}>)" if str(f).startswith("/") else f"图：`{_esc(f)}`")
    if (run.get("reading") or "").strip():
        out += ["", f"**我的解读**：{_esc(run['reading'])}"]
    return out


def _extensions(runs: list[dict[str, Any]]) -> list[str]:
    ext = [r for r in runs if r.get("extension")]
    if not ext:
        return ["还没有扩展运行。"]
    return [line for r in ext for line in (f"### {r['id']}" + (f"（{_esc(r.get('label'))}）" if r.get("label") else ""), "",
                                           *_run_body(r), "")]


def _fence(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _code(runs: list[dict[str, Any]]) -> list[str]:
    if not runs:
        return ["还没有运行记录。"]
    out = []
    for r in [*[r for r in runs if not r.get("extension")], *[r for r in runs if r.get("extension")]]:
        out += [f"### {r['id']}" + ("（扩展）" if r.get("extension") else ""), "", f"完整代码见运行记录 [{r['id']}](/data/runs/{r['id']})。", ""]
        files = r.get("code") or []
        if not files:
            out += ["这次运行没有存代码。", ""]
        for f in files:
            lines = tilde(str(f.get("text") or "")).splitlines()
            shown = "\n".join(lines[:EXCERPT_LINES])
            cut = f"，节选前 {EXCERPT_LINES} 行" if len(lines) > EXCERPT_LINES else ""
            fence = _fence(shown)
            out += [f"**{_esc(f.get('name'))}**（共 {len(lines)} 行{cut}）", "", f"{fence}python", shown, fence, ""]
    return out


def build(plan: dict[str, Any], targets: list[dict[str, Any]], runs: list[dict[str, Any]],
          comparison: dict[str, Any] | None, *, datasets: list[dict[str, Any]] | None = None) -> str:
    """The report as Markdown: 方案摘要, 数据来源, 复现结果与对照表, 对不上的地方与可能原因, 扩展, 代码附录."""
    title = plan.get("paper_title") or plan.get("id") or ""
    # every run-derived text (notes, needs_r, readings, labels, reasons, code) shows home paths as ~; the last pass
    # below catches whatever else carries one
    runs, comparison = tilde_all(runs), tilde_all(comparison)
    parts = [f"# 复现报告：{_esc(title)}", "",
             "> 按规则从复现方案、复现目标、运行记录和对照结果拼成，没有调用 AI。论文报告值照论文原样抄录。", "",
             "## 方案摘要", "", *_summary(plan, targets), "",
             "## 数据来源", "", *_sources(plan, datasets), "",
             "## 复现结果与对照表", "", *_results(targets, comparison, runs), "",
             "## 对不上的地方与可能原因", "", *_gaps(comparison, runs), "",
             "## 扩展", "", *_extensions(runs), "",
             "## 代码附录", "", *_code(runs)]
    return tilde(re.sub(r"\n{3,}", "\n\n", "\n".join(parts)).strip()) + "\n"


# ── the one write ────────────────────────────────────────────────────────────────────────────────────────────

def _demote(markdown: str) -> str:
    """Headings one level down, so the report's sections sit inside the artifact's Evidence; code is left alone."""
    out, fence = [], ""
    for line in markdown.split("\n"):
        m = re.match(r"(`{3,})", line)
        if m and (not fence or (m.group(1).startswith(fence) and not line.strip("`"))):
            fence = "" if fence else m.group(1)
        elif not fence and re.match(r"#{2,5} ", line):
            line = "#" + line
        out.append(line)
    return "\n".join(out)


def save(conn: sqlite3.Connection, plan: dict[str, Any], markdown: str, project: str | None = None) -> str:
    """The report as a draft 研究档案 of type analysis, direction from the plan, the report in the Evidence section
    (its headings one level down; the Claim and what-did-I-figure-out sections stay Leo's). Returns the ref."""
    from ..files import artifacts
    from ..files.weeks import week_of
    from .export import _slug, default_project
    project = project or default_project()
    title = f"复现报告：{plan.get('paper_title') or plan.get('id')}"
    base = slug = f"report-{_slug(str(plan.get('id') or 'plan'))}"
    for n in range(2, 52):
        try:
            path = artifacts.create("analysis", slug, project, title=title, week=week_of(), direction=plan.get("direction") or None)
            break
        except FileExistsError:
            slug = f"{base}-{n}"
    else:
        raise FileExistsError(base)
    a = artifacts.load(path)
    artifacts.set_front(a, "status", "draft")
    a.front["replication_plan"] = plan.get("id")
    if plan.get("paper_id"):
        a.front["paper"] = plan["paper_id"]
    artifacts.append_to_section(a, "Evidence", _demote(re.sub(r"\A# .*\n+", "", markdown)).strip())
    artifacts.save(a)
    artifacts.index(conn)
    return a.ref


def matching_comparison(comparison: dict[str, Any] | None, run: dict[str, Any] | None) -> dict[str, Any] | None:
    """The comparison of the run being reported (0162 review): a comparison names the run it was computed from
    (run_id, card 0166); one from another run is refused, none falls back to the one kept with the run itself."""
    rid = str((run or {}).get("id") or "")
    if comparison is not None:
        other = comparison.get("run_id")
        if other and other != rid:
            raise plans.PlanError(f"对照结果来自 {other}，不是这份报告的 {rid or '运行'}：请先对照这次运行。")
        return comparison
    ws = (run or {}).get("workspace") or (run or {}).get("workspace_json") or {}
    if isinstance(ws, str):
        ws = json.loads(ws or "{}")
    kept = ws.get("comparison") if isinstance(ws, dict) else None
    return kept if isinstance(kept, dict) and kept.get("run_id") == rid else None


def generate(conn: sqlite3.Connection, plan_id: str, comparison: dict[str, Any] | None = None, *,
             run_id: str | None = None, extensions: tuple[str, ...] | list[str] = (), project: str | None = None) -> str:
    """Load what build() reads (the latest finished run unless `run_id` names one; `extensions` are 扩展 runs), build
    the report and save it. Returns the artifact ref."""
    from . import datasets as ds
    from . import runs as rs
    plan = plans.get(conn, plan_id)
    if plan is None:
        raise plans.PlanError(f"没有复现方案 {plan_id}")
    stored = [r for r in rs.list_all(conn, plan_id=plan_id) if r.get("status") == "done"]
    main = next((r for r in stored if r["id"] == run_id), None) if run_id else \
        next((r for r in stored if r["id"] not in extensions), None)
    views = ([run_view(main)] if main else []) + [run_view(r, extension=True) for r in stored if r["id"] in extensions]
    comparison = matching_comparison(comparison, main)
    sets = [d for d in (ds.get(conn, i) for i in (plan.get("plan") or {}).get("datasets") or []) if d]
    md = build(plan, plans.target_records(conn, plan_id), views, comparison, datasets=sets)
    return save(conn, plan, md, project)
