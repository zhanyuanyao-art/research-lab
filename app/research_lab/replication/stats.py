"""Basic statistics for analysis runs: descriptives, crosstab + χ², correlation matrix, two-group
t-test, OLS, logit and fixed effects (entity dummies through the statsmodels formula).

Every function takes a DataFrame and its params and returns an `Outcome`: tables (each carrying n
and missing), a summary, an optional matplotlib Figure (Agg, no pyplot state) and `code`, the
standalone Python that reproduces the numbers from the dataset's parquet copy. Listwise deletion
is explicit and its count is reported; nothing here interprets a result."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

CJK_FONTS = ["PingFang SC", "Hiragino Sans GB", "Heiti SC", "Songti SC", "Arial Unicode MS",
             "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Zen Hei", "DejaVu Sans"]
MAX_LEVELS = 60
_fonts_ready = False


class StatsError(ValueError):
    """A user-fixable problem with the chosen variables (shown in Chinese on the form)."""


@dataclass
class Outcome:
    tables: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    figure: Any = None
    code: str = ""

    def result(self) -> dict[str, Any]:
        return {"tables": self.tables, "summary": self.summary, "notes": self.notes}


# ---------------------------------------------------------------- helpers

def num(v: Any, digits: int = 6) -> Any:
    """JSON-safe number rounded to `digits` significant figures; NaN/inf → None."""
    if v is None:
        return None
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (np.integer, int)):
        return int(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if math.isnan(f) or math.isinf(f):
        return None
    if f == 0:
        return 0.0
    return float(f"{f:.{digits}g}")


def table(title: str, columns: list[str], rows: list[list[Any]], *, n: int | None = None,
          missing: int | None = None, note: str = "", kind: str = "") -> dict[str, Any]:
    return {"title": title, "columns": columns, "rows": [[num(v) if not isinstance(v, str) else v for v in r] for r in rows],
            "n": n, "missing": missing, "note": note, "kind": kind}


def q(name: str) -> str:
    """Formula-safe reference to a column (Chinese, spaces, punctuation)."""
    return f"Q({json.dumps(str(name), ensure_ascii=False)})"


TERM_Q = re.compile(r'Q\("((?:[^"\\]|\\.)*)"\)')


def clean_term(term: str) -> str:
    t = TERM_Q.sub(lambda m: json.loads(f'"{m.group(1)}"'), term)
    t = re.sub(r"^C\((.+?)\)\[T\.(.+)\]$", r"\1 = \2", t)
    t = re.sub(r"^(.+?)\[T\.(.+)\]$", r"\1 = \2", t)
    return "截距" if t == "Intercept" else t


def require(df: pd.DataFrame, cols: list[str]) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise StatsError(f"数据集中没有这些列：{', '.join(missing)}")


def listwise(df: pd.DataFrame, cols: list[str]) -> tuple[pd.DataFrame, int]:
    cols = list(dict.fromkeys(cols))
    require(df, cols)
    data = df[cols].dropna()
    return data, int(len(df) - len(data))


def require_numeric(data: pd.DataFrame, cols: list[str]) -> None:
    bad = [c for c in cols if not pd.api.types.is_numeric_dtype(data[c]) or pd.api.types.is_bool_dtype(data[c])]
    if bad:
        raise StatsError(f"这些变量不是数值型：{', '.join(bad)}（可在数据集页面转换类型）")


def p_stars(p: float | None) -> str:
    if p is None:
        return ""
    return "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else ""


def header(parquet: str, extra_imports: list[str] | None = None) -> str:
    imports = ["import duckdb", "import numpy as np", "import pandas as pd", *(extra_imports or [])]
    return ("\n".join(imports) + "\n\n"
            "# the dataset's parquet copy (DuckDB reads it, so pandas needs no pyarrow)\n"
            f"PARQUET = {parquet!r}\n"
            "df = duckdb.connect().execute(\"SELECT * FROM read_parquet(?)\", [PARQUET]).df()\n")


def listwise_code(cols: list[str]) -> str:
    return (f"cols = {list(dict.fromkeys(cols))!r}\n"
            "data = df[cols].dropna()          # listwise deletion\n"
            "print('n =', len(data), '| dropped for missing values:', len(df) - len(data))\n")


# ---------------------------------------------------------------- figures

def new_figure(w: float = 6.4, h: float = 4.0):
    global _fonts_ready
    import matplotlib
    matplotlib.use("Agg", force=False)
    from matplotlib.figure import Figure
    if not _fonts_ready:
        import logging
        from matplotlib import font_manager, rcParams
        have = {f.name for f in font_manager.fontManager.ttflist}
        rcParams["font.family"] = "sans-serif"
        rcParams["font.sans-serif"] = [f for f in CJK_FONTS if f in have] or ["DejaVu Sans"]
        rcParams["axes.unicode_minus"] = False
        logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
        _fonts_ready = True
    fig = Figure(figsize=(w, h), dpi=110)
    return fig


def coef_plot(terms: list[str], coefs: list[float], lo: list[float], hi: list[float], title: str):
    fig = new_figure(6.4, max(2.2, 0.42 * len(terms) + 1.2))
    ax = fig.add_subplot(111)
    ys = list(range(len(terms)))[::-1]
    ax.errorbar(coefs, ys, xerr=[np.array(coefs) - np.array(lo), np.array(hi) - np.array(coefs)],
                fmt="o", color="#2f6b5c", ecolor="#5a9a86", capsize=3)
    ax.axvline(0, color="#999999", lw=0.8, ls="--")
    ax.set_yticks(ys)
    ax.set_yticklabels(terms)
    ax.set_xlabel("系数与 95% 置信区间")
    ax.set_title(title, fontsize=10)
    fig.tight_layout()
    return fig


COEF_PLOT_CODE = """
# coefficient plot (95% CI), intercept and dummies left out
import matplotlib.pyplot as plt
ci = res.conf_int()
keep = [t for t in res.params.index if t != "Intercept" and not t.startswith("C(")]
fig, ax = plt.subplots(figsize=(6.4, 0.42 * len(keep) + 1.2))
ax.errorbar(res.params[keep], range(len(keep))[::-1],
            xerr=[res.params[keep] - ci.loc[keep, 0], ci.loc[keep, 1] - res.params[keep]], fmt="o", capsize=3)
ax.axvline(0, ls="--", lw=0.8)
ax.set_yticks(list(range(len(keep))[::-1]), keep)
fig.tight_layout()
fig.savefig("coefficients.png", dpi=144)
"""


# ---------------------------------------------------------------- descriptives

def describe(df: pd.DataFrame, params: dict[str, Any], parquet: str = "data.parquet") -> Outcome:
    cols = list(params.get("vars") or [])[:40] or list(df.columns)[:40]
    require(df, cols)
    n_total = int(len(df))
    num_rows, cat_rows, numeric = [], [], []
    for c in cols:
        s = df[c]
        miss = int(s.isna().sum())
        nn = s.dropna()
        if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s):
            numeric.append(c)
            num_rows.append([c, int(len(nn)), miss, nn.mean() if len(nn) else None, nn.std() if len(nn) > 1 else None,
                             nn.min() if len(nn) else None, nn.quantile(.25) if len(nn) else None,
                             nn.median() if len(nn) else None, nn.quantile(.75) if len(nn) else None, nn.max() if len(nn) else None])
        else:
            vc = nn.astype(str).value_counts()
            top = "；".join(f"{k} ({v})" for k, v in vc.head(5).items())
            cat_rows.append([c, int(len(nn)), miss, int(vc.shape[0]), top])
    out = Outcome(summary={"n": n_total, "missing_dropped": 0, "variables": len(cols)})
    if num_rows:
        out.tables.append(table("数值变量", ["变量", "n", "缺失", "均值", "标准差", "最小值", "P25", "中位数", "P75", "最大值"],
                                num_rows, n=n_total, missing=sum(r[2] for r in num_rows),
                                note="逐变量计算（n 为该变量的非缺失数），未做列表删除。"))
    if cat_rows:
        out.tables.append(table("分类变量", ["变量", "n", "缺失", "取值数", "最常见取值 (频数)"], cat_rows, n=n_total,
                                missing=sum(r[2] for r in cat_rows)))
    if numeric:
        show = numeric[:4]
        fig = new_figure(6.4, 2.6 * math.ceil(len(show) / 2))
        for i, c in enumerate(show):
            ax = fig.add_subplot(math.ceil(len(show) / 2), 2 if len(show) > 1 else 1, i + 1)
            ax.hist(df[c].dropna(), bins=20, color="#5a9a86", edgecolor="white")
            ax.set_title(f"{c}（n={int(df[c].notna().sum())}）", fontsize=9)
        fig.tight_layout()
        out.figure = fig
    out.code = header(parquet) + (
        f"\ncols = {cols!r}\n"
        "num = [c for c in cols if pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c])]\n"
        "cat = [c for c in cols if c not in num]\n"
        "if num:\n"
        "    desc = df[num].describe().T                       # count = non-missing n per variable\n"
        "    desc['missing'] = df[num].isna().sum()\n"
        "    print(desc)\n"
        "for c in cat:\n"
        "    print(c, 'n =', df[c].notna().sum(), 'missing =', df[c].isna().sum())\n"
        "    print(df[c].dropna().astype(str).value_counts().head(5))\n"
        "\n# histograms\n"
        "import matplotlib.pyplot as plt\n"
        "if num:\n"
        "    df[num[:4]].hist(bins=20)\n"
        "    plt.tight_layout(); plt.savefig('histograms.png', dpi=144)\n")
    return out


# ---------------------------------------------------------------- crosstab

def crosstab(df: pd.DataFrame, params: dict[str, Any], parquet: str = "data.parquet") -> Outcome:
    from scipy.stats import chi2_contingency
    row, col = params.get("row"), params.get("col")
    if not row or not col or row == col:
        raise StatsError("请选择两个不同的分类变量（行、列）")
    data, dropped = listwise(df, [row, col])
    if data.empty:
        raise StatsError("去掉缺失值后没有观测")
    ct = pd.crosstab(data[row].astype(str), data[col].astype(str))
    if ct.shape[0] > MAX_LEVELS or ct.shape[1] > MAX_LEVELS:
        raise StatsError(f"取值过多（{ct.shape[0]}×{ct.shape[1]}），交叉表最多 {MAX_LEVELS} 个取值；请先重编码")
    if ct.shape[0] < 2 or ct.shape[1] < 2:
        raise StatsError("每个变量至少需要两个取值")
    chi2, p, dof, expected = chi2_contingency(ct.values)
    n = int(ct.values.sum())
    k = min(ct.shape) - 1
    v = math.sqrt(chi2 / (n * k)) if n and k else None
    low = float((expected < 5).mean())
    columns = [f"{row} \\ {col}", *[str(c) for c in ct.columns], "合计"]
    rows = [[str(idx), *[int(x) for x in ct.loc[idx]], int(ct.loc[idx].sum())] for idx in ct.index]
    rows.append(["合计", *[int(x) for x in ct.sum(axis=0)], n])
    pct = (ct.div(ct.sum(axis=1), axis=0) * 100)
    pct_rows = [[str(idx), *[round(float(x), 1) for x in pct.loc[idx]]] for idx in pct.index]
    out = Outcome(summary={"n": n, "missing_dropped": dropped, "chi2": num(chi2), "df": int(dof), "p": num(p),
                           "cramers_v": num(v), "expected_lt5_share": round(low, 3)})
    out.tables.append(table("频数", columns, rows, n=n, missing=dropped))
    out.tables.append(table("行百分比 (%)", [f"{row} \\ {col}", *[str(c) for c in ct.columns]], pct_rows, n=n, missing=dropped))
    out.tables.append(table("χ² 检验", ["χ²", "自由度", "p", "Cramér's V", "期望频数<5 的格子比例", "n", "缺失删除"],
                            [[chi2, int(dof), p, v, round(low, 3), n, dropped]], n=n, missing=dropped,
                            note="2×2 表使用 Yates 连续性校正（scipy 默认）。"))
    if low > 0.2:
        out.notes.append(f"{low:.0%} 的格子期望频数小于 5，χ² 近似不可靠；考虑合并类别或使用 Fisher 精确检验。")
    fig = new_figure(6.4, max(2.8, 0.35 * ct.shape[0] + 1.5))
    ax = fig.add_subplot(111)
    im = ax.imshow(ct.values, cmap="Greens", aspect="auto")
    ax.set_xticks(range(ct.shape[1]), [str(c) for c in ct.columns], rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(ct.shape[0]), [str(i) for i in ct.index], fontsize=8)
    if ct.size <= 150:
        for i in range(ct.shape[0]):
            for j in range(ct.shape[1]):
                ax.text(j, i, int(ct.values[i, j]), ha="center", va="center", fontsize=7)
    ax.set_xlabel(col); ax.set_ylabel(row)
    fig.colorbar(im, ax=ax, shrink=0.8)
    fig.tight_layout()
    out.figure = fig
    out.code = header(parquet, ["from scipy.stats import chi2_contingency"]) + "\n" + listwise_code([row, col]) + (
        f"ct = pd.crosstab(data[{row!r}].astype(str), data[{col!r}].astype(str))\n"
        "print(ct)\n"
        "print((ct.div(ct.sum(axis=1), axis=0) * 100).round(1))   # row percentages\n"
        "chi2, p, dof, expected = chi2_contingency(ct.values)       # Yates correction on 2x2 tables\n"
        "n = ct.values.sum()\n"
        "cramers_v = np.sqrt(chi2 / (n * (min(ct.shape) - 1)))\n"
        "print(f'chi2={chi2:.4f} df={dof} p={p:.4g} V={cramers_v:.3f} n={n}')\n"
        "print('share of cells with expected < 5:', (expected < 5).mean())\n")
    return out


# ---------------------------------------------------------------- correlation

def correlation(df: pd.DataFrame, params: dict[str, Any], parquet: str = "data.parquet") -> Outcome:
    from scipy.stats import pearsonr, spearmanr
    cols = list(dict.fromkeys(params.get("vars") or []))
    method = params.get("corr_method") or "pearson"
    if method not in ("pearson", "spearman"):
        raise StatsError("相关方法须为 pearson 或 spearman")
    if len(cols) < 2:
        raise StatsError("相关矩阵至少需要两个数值变量")
    if len(cols) > 25:
        raise StatsError("一次最多 25 个变量")
    data, dropped = listwise(df, cols)
    require_numeric(data, cols)
    n = int(len(data))
    if n < 3:
        raise StatsError("去掉缺失值后观测少于 3 个")
    fn = pearsonr if method == "pearson" else spearmanr
    r = np.eye(len(cols))
    pv = np.zeros((len(cols), len(cols)))
    for i, a in enumerate(cols):
        for j, b in enumerate(cols):
            if j <= i:
                continue
            res = fn(data[a], data[b])
            r[i, j] = r[j, i] = float(res[0])
            pv[i, j] = pv[j, i] = float(res[1])
    out = Outcome(summary={"n": n, "missing_dropped": dropped, "method": method})
    out.tables.append(table(f"相关系数（{'Pearson' if method == 'pearson' else 'Spearman'}）", ["", *cols],
                            [[a, *[r[i, j] for j in range(len(cols))]] for i, a in enumerate(cols)], n=n, missing=dropped,
                            note="列表删除：所有变量都非缺失的观测。"))
    out.tables.append(table("p 值（双侧）", ["", *cols],
                            [[a, *[None if i == j else pv[i, j] for j in range(len(cols))]] for i, a in enumerate(cols)],
                            n=n, missing=dropped))
    fig = new_figure(1.2 + 0.55 * len(cols) + 1.5, 1.0 + 0.55 * len(cols) + 0.8)
    ax = fig.add_subplot(111)
    im = ax.imshow(r, cmap="RdBu_r", vmin=-1, vmax=1)
    ax.set_xticks(range(len(cols)), cols, rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(len(cols)), cols, fontsize=8)
    if len(cols) <= 12:
        for i in range(len(cols)):
            for j in range(len(cols)):
                ax.text(j, i, f"{r[i, j]:.2f}", ha="center", va="center", fontsize=7)
    fig.colorbar(im, ax=ax, shrink=0.8)
    ax.set_title(f"n = {n}", fontsize=9)
    fig.tight_layout()
    out.figure = fig
    out.code = header(parquet, [f"from scipy.stats import {'pearsonr' if method == 'pearson' else 'spearmanr'}"]) + "\n" + listwise_code(cols) + (
        f"print(data.corr(method={method!r}).round(3))\n"
        "for i, a in enumerate(cols):\n"
        "    for b in cols[i + 1:]:\n"
        f"        r, p = {'pearsonr' if method == 'pearson' else 'spearmanr'}(data[a], data[b])\n"
        "        print(f'{a} ~ {b}: r={r:.3f} p={p:.4g}')\n")
    return out


# ---------------------------------------------------------------- t-test

def ttest(df: pd.DataFrame, params: dict[str, Any], parquet: str = "data.parquet") -> Outcome:
    from statsmodels.stats.weightstats import CompareMeans, DescrStatsW
    y, g = params.get("y"), params.get("group")
    if not y or not g or y == g:
        raise StatsError("请选择一个数值结果变量和一个分组变量")
    data, dropped = listwise(df, [y, g])
    require_numeric(data, [y])
    levels = sorted(data[g].astype(str).unique().tolist())
    a_lvl, b_lvl = params.get("group_a") or None, params.get("group_b") or None
    if not (a_lvl and b_lvl):
        if len(levels) != 2:
            raise StatsError(f"分组变量有 {len(levels)} 个取值；请指定要比较的两组（组 A、组 B）")
        a_lvl, b_lvl = levels
    for lv in (a_lvl, b_lvl):
        if lv not in levels:
            raise StatsError(f"分组变量里没有取值“{lv}”")
    usevar = "pooled" if params.get("equal_var") else "unequal"
    a = data.loc[data[g].astype(str) == a_lvl, y].astype(float)
    b = data.loc[data[g].astype(str) == b_lvl, y].astype(float)
    if len(a) < 2 or len(b) < 2:
        raise StatsError("每组至少需要两个观测")
    cm = CompareMeans(DescrStatsW(a.values), DescrStatsW(b.values))
    t, p, dof = cm.ttest_ind(usevar=usevar)
    lo, hi = cm.tconfint_diff(alpha=0.05, usevar=usevar)
    n = int(len(a) + len(b))
    diff = float(a.mean() - b.mean())
    pooled_sd = math.sqrt(((len(a) - 1) * a.var() + (len(b) - 1) * b.var()) / (n - 2)) if n > 2 else float("nan")
    d = diff / pooled_sd if pooled_sd else None
    out = Outcome(summary={"n": n, "missing_dropped": dropped, "t": num(t), "df": num(dof), "p": num(p),
                           "diff": num(diff), "ci_low": num(lo), "ci_high": num(hi), "cohens_d": num(d), "usevar": usevar})
    out.tables.append(table("分组描述", ["组", "n", "均值", "标准差", "标准误"],
                            [[f"{g} = {a_lvl}", int(len(a)), a.mean(), a.std(), a.std() / math.sqrt(len(a))],
                             [f"{g} = {b_lvl}", int(len(b)), b.mean(), b.std(), b.std() / math.sqrt(len(b))]], n=n, missing=dropped))
    out.tables.append(table("均值差异检验", ["差值 (A−B)", "t", "自由度", "p", "95% CI 下限", "95% CI 上限", "Cohen's d", "n", "缺失删除"],
                            [[diff, t, dof, p, lo, hi, d, n, dropped]], n=n, missing=dropped,
                            note="Welch 检验（不假定方差相等）" if usevar == "unequal" else "Student 检验（假定方差相等）"))
    if len(levels) > 2:
        out.notes.append(f"分组变量共有 {len(levels)} 个取值，只比较了 {a_lvl} 与 {b_lvl}。")
    fig = new_figure(5.2, 3.6)
    ax = fig.add_subplot(111)
    ax.boxplot([a.values, b.values], tick_labels=[f"{a_lvl}\n(n={len(a)})", f"{b_lvl}\n(n={len(b)})"])
    ax.set_ylabel(y); ax.set_xlabel(g)
    fig.tight_layout()
    out.figure = fig
    out.code = header(parquet, ["from statsmodels.stats.weightstats import CompareMeans, DescrStatsW"]) + "\n" + listwise_code([y, g]) + (
        f"a = data.loc[data[{g!r}].astype(str) == {a_lvl!r}, {y!r}].astype(float)\n"
        f"b = data.loc[data[{g!r}].astype(str) == {b_lvl!r}, {y!r}].astype(float)\n"
        "print('A: n', len(a), 'mean', a.mean(), 'sd', a.std())\n"
        "print('B: n', len(b), 'mean', b.mean(), 'sd', b.std())\n"
        "cm = CompareMeans(DescrStatsW(a.values), DescrStatsW(b.values))\n"
        f"t, p, dof = cm.ttest_ind(usevar={usevar!r})\n"
        f"lo, hi = cm.tconfint_diff(alpha=0.05, usevar={usevar!r})\n"
        "print(f'diff={a.mean() - b.mean():.4f} t={t:.4f} df={dof:.1f} p={p:.4g} 95% CI [{lo:.4f}, {hi:.4f}]')\n")
    return out


# ---------------------------------------------------------------- regressions

def _coef_rows(res, keep) -> list[list[Any]]:
    ci = res.conf_int()
    rows = []
    for term in keep:
        p = float(res.pvalues[term])
        rows.append([clean_term(term), float(res.params[term]), float(res.bse[term]), float(res.tvalues[term]), p,
                     float(ci.loc[term, 0]), float(ci.loc[term, 1]), p_stars(p)])
    return rows


def _xs(params: dict[str, Any]) -> list[str]:
    xs = params.get("x") or []
    if isinstance(xs, str):
        xs = [xs]
    return [x for x in dict.fromkeys(xs) if x]


def _fit_figure(res, keep: list[str], title: str):
    shown = [t for t in keep if t != "Intercept"]
    if not shown:
        return None
    ci = res.conf_int()
    return coef_plot([clean_term(t) for t in shown], [float(res.params[t]) for t in shown],
                     [float(ci.loc[t, 0]) for t in shown], [float(ci.loc[t, 1]) for t in shown], title)


def _cov_args(params: dict[str, Any], data: pd.DataFrame, cluster_default: str | None = None) -> tuple[dict[str, Any], str, str]:
    """(fit kwargs, code snippet for fit(), label)."""
    se = params.get("se") or "classical"
    if se == "robust":
        return {"cov_type": "HC1"}, "cov_type='HC1'", "稳健标准误 (HC1)"
    if se == "cluster":
        cl = params.get("cluster") or cluster_default
        if not cl:
            raise StatsError("聚类标准误需要指定聚类变量")
        if cl not in data.columns:
            raise StatsError(f"聚类变量 {cl} 不在模型数据里")
        groups = pd.factorize(data[cl])[0]
        return ({"cov_type": "cluster", "cov_kwds": {"groups": groups}},
                f"cov_type='cluster', cov_kwds={{'groups': pd.factorize(data[{cl!r}])[0]}}", f"按 {cl} 聚类的标准误")
    return {}, "", "常规标准误"


def ols(df: pd.DataFrame, params: dict[str, Any], parquet: str = "data.parquet") -> Outcome:
    import statsmodels.formula.api as smf
    y, xs = params.get("y"), _xs(params)
    if not y or not xs:
        raise StatsError("OLS 需要一个因变量和至少一个自变量")
    if y in xs:
        raise StatsError("因变量不能同时作为自变量")
    extra = [params["cluster"]] if params.get("se") == "cluster" and params.get("cluster") else []
    data, dropped = listwise(df, [y, *xs, *extra])
    require_numeric(data, [y])
    if len(data) <= len(xs) + 1:
        raise StatsError(f"观测数（{len(data)}）不足以估计 {len(xs) + 1} 个参数")
    formula = f"{q(y)} ~ " + " + ".join(q(x) for x in xs)
    kw, kw_code, se_label = _cov_args(params, data)
    res = smf.ols(formula, data=data).fit(**kw)
    keep = list(res.params.index)
    n = int(res.nobs)
    out = Outcome(summary={"n": n, "missing_dropped": dropped, "r2": num(res.rsquared), "adj_r2": num(res.rsquared_adj),
                           "f": num(res.fvalue), "f_p": num(res.f_pvalue), "k": len(keep), "se": se_label, "formula": formula})
    out.tables.append(table("系数", ["项", "系数", "标准误", "t", "p", "95% CI 下限", "95% CI 上限", ""], _coef_rows(res, keep),
                            n=n, missing=dropped, note=se_label, kind="coef"))
    out.tables.append(table("模型拟合", ["n", "缺失删除", "R²", "调整 R²", "F", "F 检验 p"],
                            [[n, dropped, res.rsquared, res.rsquared_adj, res.fvalue, res.f_pvalue]], n=n, missing=dropped))
    out.figure = _fit_figure(res, keep, f"OLS：{y}（n={n}）")
    out.code = header(parquet, ["import statsmodels.formula.api as smf"]) + "\n" + listwise_code([y, *xs, *extra]) + (
        f"formula = {formula!r}\n"
        f"res = smf.ols(formula, data=data).fit({kw_code})\n"
        "print(res.summary())\n"
        "print(res.conf_int())   # 95% CI\n") + COEF_PLOT_CODE
    return out


def logit(df: pd.DataFrame, params: dict[str, Any], parquet: str = "data.parquet") -> Outcome:
    import statsmodels.formula.api as smf
    y, xs = params.get("y"), _xs(params)
    if not y or not xs:
        raise StatsError("Logit 需要一个二分因变量和至少一个自变量")
    if y in xs:
        raise StatsError("因变量不能同时作为自变量")
    extra = [params["cluster"]] if params.get("se") == "cluster" and params.get("cluster") else []
    data, dropped = listwise(df, [y, *xs, *extra])
    vals = sorted(pd.unique(data[y]).tolist(), key=lambda v: str(v))
    recode = ""
    if set(vals) <= {0, 1, True, False} and len(vals) == 2:
        data = data.assign(**{y: data[y].astype(int)})
        recode = f"data[{y!r}] = data[{y!r}].astype(int)\n"
    elif len(vals) == 2:
        positive = params.get("positive") or vals[1]
        if str(positive) not in [str(v) for v in vals]:
            raise StatsError(f"因变量取值为 {vals}，没有“{positive}”")
        data = data.assign(**{y: (data[y].astype(str) == str(positive)).astype(int)})
        recode = f"data[{y!r}] = (data[{y!r}].astype(str) == {str(positive)!r}).astype(int)   # 1 = {positive}\n"
    else:
        raise StatsError(f"Logit 的因变量必须恰好有两个取值（现在有 {len(vals)} 个）")
    kw, kw_code, se_label = _cov_args(params, data)
    formula = f"{q(y)} ~ " + " + ".join(q(x) for x in xs)
    try:
        res = smf.logit(formula, data=data).fit(disp=0, maxiter=200, **kw)
    except Exception as e:  # noqa: BLE001 - perfect separation, singular matrix
        raise StatsError(f"模型无法估计：{type(e).__name__}: {e}") from e
    keep = list(res.params.index)
    n = int(res.nobs)
    rows = _coef_rows(res, keep)
    for r, term in zip(rows, keep):
        r.insert(7, math.exp(float(res.params[term])) if abs(float(res.params[term])) < 50 else None)
    out = Outcome(summary={"n": n, "missing_dropped": dropped, "pseudo_r2": num(res.prsquared), "llf": num(res.llf),
                           "llr_p": num(res.llr_pvalue), "converged": bool(res.mle_retvals.get("converged", True)),
                           "positives": int(data[y].sum()), "se": se_label, "formula": formula})
    out.tables.append(table("系数（对数几率）", ["项", "系数", "标准误", "z", "p", "95% CI 下限", "95% CI 上限", "几率比", ""], rows,
                            n=n, missing=dropped, note=se_label, kind="coef"))
    out.tables.append(table("模型拟合", ["n", "缺失删除", "y=1 个数", "McFadden 伪 R²", "对数似然", "LR 检验 p"],
                            [[n, dropped, int(data[y].sum()), res.prsquared, res.llf, res.llr_pvalue]], n=n, missing=dropped))
    if not out.summary["converged"]:
        out.notes.append("最大似然未收敛，系数不可信。")
    out.figure = _fit_figure(res, keep, f"Logit：{y}（n={n}）")
    out.code = header(parquet, ["import statsmodels.formula.api as smf"]) + "\n" + listwise_code([y, *xs, *extra]) + recode + (
        f"formula = {formula!r}\n"
        f"res = smf.logit(formula, data=data).fit(disp=0, maxiter=200{', ' + kw_code if kw_code else ''})\n"
        "print(res.summary())\n"
        "print(np.exp(res.params))   # odds ratios\n") + COEF_PLOT_CODE
    return out


def fixed_effects(df: pd.DataFrame, params: dict[str, Any], parquet: str = "data.parquet") -> Outcome:
    import statsmodels.formula.api as smf
    y, xs, entity, time = params.get("y"), _xs(params), params.get("entity"), params.get("time") or None
    if not y or not xs or not entity:
        raise StatsError("固定效应需要因变量、至少一个自变量和个体（entity）变量")
    if y in xs or entity in xs or (time and time in xs):
        raise StatsError("因变量、个体、时间变量不能同时作为自变量")
    cols = [y, *xs, entity, *([time] if time else [])]
    extra = [params["cluster"]] if params.get("se") == "cluster" and params.get("cluster") and params["cluster"] not in cols else []
    data, dropped = listwise(df, cols + extra)
    require_numeric(data, [y])
    n_ent = int(data[entity].nunique())
    if n_ent < 2:
        raise StatsError("个体变量至少需要两个取值")
    if n_ent > 2000:
        raise StatsError(f"个体数过多（{n_ent}），虚拟变量法不适用")
    formula = f"{q(y)} ~ " + " + ".join(q(x) for x in xs) + f" + C({q(entity)})" + (f" + C({q(time)})" if time else "")
    kw, kw_code, se_label = _cov_args(params, data, cluster_default=entity)
    if len(data) <= len(xs) + n_ent + (data[time].nunique() if time else 0):
        raise StatsError("观测数不足以估计全部虚拟变量")
    res = smf.ols(formula, data=data).fit(**kw)
    keep = [t for t in res.params.index if not t.startswith("C(")]
    n = int(res.nobs)
    n_dummies = len(res.params) - len(keep)
    out = Outcome(summary={"n": n, "missing_dropped": dropped, "r2": num(res.rsquared), "adj_r2": num(res.rsquared_adj),
                           "entities": n_ent, "periods": int(data[time].nunique()) if time else None,
                           "dummies": n_dummies, "se": se_label, "formula": formula})
    out.tables.append(table("系数（个体虚拟变量未列出）", ["项", "系数", "标准误", "t", "p", "95% CI 下限", "95% CI 上限", ""],
                            _coef_rows(res, keep), n=n, missing=dropped,
                            note=f"{se_label}；{entity} 固定效应（{n_ent} 个个体）" + (f"，{time} 固定效应" if time else ""), kind="coef"))
    out.tables.append(table("模型拟合", ["n", "缺失删除", "个体数", "虚拟变量数", "R²（含虚拟变量）", "调整 R²"],
                            [[n, dropped, n_ent, n_dummies, res.rsquared, res.rsquared_adj]], n=n, missing=dropped))
    single = data.groupby(entity).size()
    if (single == 1).any():
        out.notes.append(f"{int((single == 1).sum())} 个个体只有一个观测，对斜率估计没有贡献。")
    out.figure = _fit_figure(res, keep, f"固定效应：{y}（n={n}，{n_ent} 个个体）")
    out.code = header(parquet, ["import statsmodels.formula.api as smf"]) + "\n" + listwise_code(cols + extra) + (
        f"formula = {formula!r}   # C(...) = entity dummies (LSDV fixed effects)\n"
        f"res = smf.ols(formula, data=data).fit({kw_code})\n"
        "print(res.summary())\n") + COEF_PLOT_CODE
    return out
