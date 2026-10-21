"""stargazer tables printed by R scripts → results.json rows (card 0186).

A replication run prints its regression tables to the console through stargazer(), as LaTeX (the default) or as text
(type = "text"). This reads those tables out of a script's printed output: the model columns, the coefficients with
their standard errors and stars, the observations row; reads the table's name from the comment above the stargazer()
call ("#### Table 3: ... ####"); and builds the shared results format (workspace.validate_results). The output is the
scripts' own text, so it is untrusted and can be huge: every loop and regex here is linear, lines are cut before they are
matched, and blocks, columns and rows are capped (AGENTS.md, "Scanning outside text").
"""
from __future__ import annotations

import posixpath
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from itertools import accumulate, islice
from typing import Any

from . import rtext

MAX_LINE = 20_000                      # characters of one line that are read
MAX_BLOCKS = 200                       # tables read out of one script's output
MAX_COLS = 100
MAX_ROWS = 3_000
MAX_RAW = 200_000                      # characters of one table kept as a file
MAX_CALLS = 2_000

STAT_LABEL = re.compile(r"(?:Observations$|N$|Num\.? ?Obs\.?|R\$?\^?\{?2\}?$|R2$|Adjusted R|Log Likelihood|Akaike|Bayesian|"
                        r"Residual Std|F Statistic|Theta|Pseudo|AIC$|BIC$|Sigma|Groups$|Wald|chi|Max\. Possible|Deviance|"
                        r"Null Deviance|Residual Deviance|Cragg|Weak instr|Wu-Hausman|Sargan|df$|Df$|Note:?)", re.I)
MODEL_TYPE = re.compile(r"(?:OLS|Poisson|logistic|probit|negative\s+binomial|normal|Cox|ordered\s+logistic|"
                        r"ordered\s+probit|multinomial\s+logistic|linear|binomial|Gamma|panel|tobit|survival|"
                        r"quasi-?\w+|zero-?inflated\s+\w+|\w+\s+regression)", re.I)

_NUM = r"-?(?:\d[\d,]*(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_NUM_RE = re.compile(_NUM)
_CELL_NUM = re.compile(r"([^\d.\-−]*)(" + _NUM + r")(\**)")
_STARS = re.compile(r"\$?\^\{?\\?(\*+)\}?\$?|\\textsuperscript\{(\*+)\}")


@dataclass
class Cell:
    estimate: float | None = None
    se: float | None = None
    ci: tuple[float, float] | None = None
    stars: int = 0


@dataclass
class Block:
    kind: str                                     # latex | text
    models: list[str] = field(default_factory=list)       # "(1)", "(2)" …
    dep: list[str] = field(default_factory=list)          # the dependent variable names, one per column
    types: list[str] = field(default_factory=list)        # OLS / Poisson / … one per column
    rows: list[tuple[str, list[Cell | None]]] = field(default_factory=list)
    stats: dict[str, list[str | None]] = field(default_factory=dict)
    caption: str = ""
    start: int = 0
    raw: str = ""                                 # the printed table as it stood in the log


def _number(text: str) -> float | None:
    t = text.replace("−", "-").replace(",", "").strip()
    if len(t) > 40 or _NUM_RE.fullmatch(t) is None:
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return v if v == v and abs(v) != float("inf") else None


def _bracketed(t: str) -> str | None:
    """The inside of "( … )" or "[ … ]", or None. Plain slicing: no pattern with a lazy middle between two optional blanks
    (that shape is cubic on a long run of spaces)."""
    if len(t) >= 2 and t[0] in "([" and t[-1] in ")]":
        return t[1:-1].strip()
    return None


def _cell(raw: str) -> Cell | None:
    """A coefficient cell ("5.101^{***}", "0.881**", "$-$0.020"), a standard error ("(1.450)") or a confidence interval
    ("(0.1, 0.9)" / "[0.1, 0.9]"), or None for a blank or anything else."""
    t = raw.strip()[:200]
    if not t:
        return None
    stars = 0
    m = _STARS.search(t)
    if m:
        stars = len(m.group(1) or m.group(2))
        t = (t[:m.start()] + t[m.end():]).strip()
    t = t.replace("$", "").replace("\\", "").strip()
    sm = _CELL_NUM.fullmatch(t)
    if sm and sm.group(1).strip() == "":
        v = _number(sm.group(2))
        if v is not None:
            return Cell(estimate=v, stars=stars or len(sm.group(3)))
    inner = _bracketed(t)
    if inner is not None:
        parts = [x.strip() for x in re.split(r"\s*,\s*|\s+to\s+", inner, maxsplit=2)]
        if len(parts) == 2 and all(_number(x) is not None for x in parts):
            lo, hi = (_number(parts[0]), _number(parts[1]))
            return Cell(ci=(lo, hi)) if lo is not None and hi is not None and lo <= hi else None
        v = _number(inner)
        if v is not None and v >= 0:
            return Cell(se=v)
    return None


def _tex_text(s: str) -> str:
    s = re.sub(r"\\(?:textit|textbf|emph|text|mathrm|mathit)\{([^{}]*)\}", r"\1", s[:MAX_LINE])
    s = s.replace("\\_", "_").replace("\\&", "&").replace("\\%", "%").replace("\\#", "#")
    s = re.sub(r"\$([^$]*)\$", r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


def _tex_cells(line: str) -> list[str]:
    s = line.strip()[:MAX_LINE]
    if s.startswith("\\\\["):
        close = s.find("]")
        s = s[close + 1:].lstrip() if close >= 0 else s
    s = re.sub(r"\\\\(?:\[[^\]]{0,20}\])?\s*$", "", s).strip()
    out: list[str] = []
    for part in re.split(r"(?<!\\)&", s):
        p = part.strip()
        m = re.fullmatch(r"\\multicolumn\{(\d{1,4})\}\{[^}]*\}\{(.*)\}", p, re.S)
        if m:
            out += [m.group(2).strip()] * max(1, min(int(m.group(1)), 40))
        else:
            out.append(p)
        if len(out) > MAX_COLS + 1:
            break
    return out[:MAX_COLS + 1]


def _is_stat(label: str) -> bool:
    return STAT_LABEL.match(_tex_text(label)) is not None


def _finish_rows(block: Block, rows: list[tuple[str, list[str]]], stats: list[tuple[str, list[str]]]) -> Block:
    """rows: (label, cells) of the body in order; an empty label is the line under the previous coefficient."""
    n = len(block.models)
    cur: tuple[str, list[Cell | None]] | None = None
    for label, cells in rows[:MAX_ROWS * 2]:
        cells = (cells + [""] * n)[:n]
        parsed = [_cell(c) for c in cells]
        if label:
            if any(p is not None and p.estimate is not None for p in parsed):
                cur = (label, [p if p is not None and p.estimate is not None else None for p in parsed])
                if len(block.rows) < MAX_ROWS:
                    block.rows.append(cur)
            else:
                cur = None
        elif cur is not None:
            for i, p in enumerate(parsed):
                if p is None or cur[1][i] is None:
                    continue
                if p.se is not None and cur[1][i].se is None:
                    cur[1][i].se = p.se
                if p.ci is not None and cur[1][i].ci is None:
                    cur[1][i].ci = p.ci
    for label, cells in stats[:100]:
        if _tex_text(label).lower().startswith("note"):
            continue
        block.stats[_tex_text(label)] = [(c.strip() or None) for c in (cells + [""] * n)[:n]]
    return block


def _headers(block: Block, header_rows: list[list[str]]) -> None:
    """The text rows above the model numbers: the dependent variable names and the model types, in stargazer's order."""
    texts = [[_tex_text(c) for c in r] for r in header_rows[:20]]
    texts = [r for r in texts if not any(re.search(r"Dependent variable", c, re.I) for c in r)]
    n = len(block.models)
    for r in texts:
        vals = (r + [""] * n)[:n]
        if vals and any(vals) and all(MODEL_TYPE.match(v) for v in vals if v):
            block.types = vals
        elif any(vals) and not block.dep:
            block.dep = vals


def _parse_latex(lines: list[str], start: int) -> Block | None:
    block = Block("latex", start=start)
    header: list[list[str]] = []
    body: list[tuple[str, list[str]]] = []
    stats: list[tuple[str, list[str]]] = []
    seen_models = False
    for ln in lines[:MAX_ROWS * 3]:
        s = ln.strip()[:MAX_LINE]
        cap = re.match(r"\\caption\{(.*)\}", s)
        if cap:
            block.caption = _tex_text(cap.group(1))
        if "&" not in s or s.startswith("%"):
            continue
        cells = _tex_cells(s)
        if not cells:
            continue
        label, rest = _tex_text(cells[0]), cells[1:]
        nums = [_tex_text(c) for c in rest]
        if not seen_models:
            if not label and any(re.fullmatch(r"\(\d+\)", c) for c in nums):
                block.models = [c if re.fullmatch(r"\(\d+\)", c) else "" for c in nums][:MAX_COLS]
                seen_models = True
                continue
            header.append(cells[1:] if not label else cells)
            continue
        if label and _is_stat(label):
            stats.append((label, rest))
        else:
            body.append((label, rest))
    if not seen_models or not block.models:
        return None
    _headers(block, header)
    return _finish_rows(block, body, stats)


def _parse_text(lines: list[str], start: int) -> Block | None:
    block = Block("text", start=start)
    lines = [ln[:MAX_LINE] for ln in lines[:MAX_ROWS * 3]]
    model_line = next((i for i, ln in enumerate(lines) if "(" in ln and re.search(r"\(\d+\)", ln)
                       and not re.search(r"[A-Za-z]{2}", ln)), None)
    if model_line is None:
        return None
    anchors = [(m.group(0), (m.start() + m.end()) / 2) for m in islice(re.finditer(r"\(\d+\)", lines[model_line]), MAX_COLS)]
    block.models = [a[0] for a in anchors]
    centres = [a[1] for a in anchors]

    def split(line: str) -> tuple[str, list[str]]:
        """label and one cell per column, tokens assigned to the column whose number they stand closest to."""
        text = line.rstrip()
        stripped = text.lstrip()
        label = ""
        cut = 0
        if stripped and len(text) - len(stripped) < 3:
            gap = re.search(r"\s{2,}", stripped)
            label = stripped[:gap.start()] if gap else stripped
            cut = (len(text) - len(stripped)) + len(label)
        cells = [""] * len(centres)
        for t in re.finditer(r"\S+", text):
            if t.start() < cut:
                continue
            k = min(range(len(centres)), key=lambda i: abs(centres[i] - (t.start() + t.end()) / 2))
            cells[k] = (cells[k] + " " + t.group(0)).strip()
        return label.strip(), cells

    header: list[list[str]] = []
    for ln in lines[:model_line]:
        if re.fullmatch(r"[=\-\s]*", ln):
            continue
        header.append(split(ln)[1])
    body: list[tuple[str, list[str]]] = []
    stats: list[tuple[str, list[str]]] = []
    for ln in lines[model_line + 1:]:
        if re.fullmatch(r"[=\-\s]*", ln) or ln.startswith("Note:"):
            continue
        label, cells = split(ln)
        (stats if label and _is_stat(label) else body).append((label, cells))
    _headers(block, header)
    return _finish_rows(block, body, stats)


def _raw(lines: list[str]) -> str:
    out, size = [], 0
    for ln in lines:
        size += len(ln) + 1
        if size > MAX_RAW:
            break
        out.append(ln)
    return "\n".join(out) + "\n"


def parse_stargazer(text: str) -> list[Block]:
    """The stargazer tables (LaTeX or type = "text") in a run's printed output, in order of appearance (at most
    MAX_BLOCKS). Tables without model numbers (descriptive statistics) are left out."""
    lines = (text or "").splitlines()
    found: list[Block] = []
    # where each table ends, found once: the next \end{tabular}, the next closing rule of a text table
    ends_tabular = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("\\end{tabular}")]
    rule = [i for i, ln in enumerate(lines) if len(ln.strip()) >= 20 and set(ln.strip()) == {"="}]
    rule_ids = set(rule)
    i = 0
    while i < len(lines) and len(found) < MAX_BLOCKS:
        s = lines[i].lstrip()
        if s.startswith("\\begin{tabular}"):
            k = bisect_right(ends_tabular, i)
            if k >= len(ends_tabular):
                break                                                  # no table closes after this one: none does
            j = ends_tabular[k]
            b = _parse_latex(lines[i + 1:j], i)
            if b is not None:
                top = i
                for k2 in range(max(0, i - 6), i):
                    cap = re.match(r"\s*\\caption\{(.*)\}", lines[k2][:MAX_LINE])
                    if cap and not b.caption:
                        b.caption = _tex_text(cap.group(1))
                    if top == i and (lines[k2].startswith("% Table created by stargazer") or lines[k2].lstrip().startswith("\\begin{table}")):
                        top = k2
                tail = j + 1 if j + 1 < len(lines) and lines[j + 1].lstrip().startswith("\\end{table}") else j
                b.raw = _raw(lines[top:tail + 1])
                found.append(b)
            i = j + 1
            continue
        if i in rule_ids:
            k = bisect_right(rule, i)
            if k >= len(rule):
                i += 1
                continue
            j = rule[k]
            b = _parse_text(lines[i + 1:j], i)
            if b is not None and b.models:
                end = j + 2 if j + 1 < len(lines) and lines[j + 1].startswith("Note:") else j + 1
                b.raw = _raw(lines[i:end])
                found.append(b)
            i = j + 1
            continue
        i += 1
    return found


_TABLE_LABEL = re.compile(r"\b(?:Table|Tab\.?|表)\s*([A-Z]?\d{1,3}[A-Za-z]?)\b[\s:.：]*(.*)", re.I)


def stargazer_calls(text: str) -> list[dict[str, str]]:
    """The stargazer() calls of an R script in source order (at most MAX_CALLS), each with the table the comment above it
    names ("#### Table 3: Effect of ... ####" → label "Table 3", title "Effect of ...")."""
    clean, masked = rtext.mask(text)
    lines = text[:rtext.SCAN_CHARS].splitlines()
    offsets = [0, *accumulate(len(ln) + 1 for ln in lines)]
    calls: list[dict[str, str]] = []
    for m in islice(re.finditer(r"(?<![\w.$@])(?:stargazer::)?stargazer\s*\(", masked), MAX_CALLS):
        line_no = max(0, bisect_right(offsets, m.start()) - 1)
        label = title = ""
        for k in range(line_no - 1, max(-1, line_no - 8), -1):
            ln = lines[k].strip()[:600]
            if not ln:
                continue
            if not ln.startswith("#"):
                break
            c = _TABLE_LABEL.search(ln.lstrip("# "))
            if c:
                label, title = f"Table {c.group(1)}", c.group(2).rstrip("# \t")
                break
        calls.append({"label": label, "title": title[:300], "line": str(line_no + 1)})
    return calls


def _loose(s: str) -> str:
    return re.sub(r"[\W_]+", "", s[:200].casefold())


def results_from_blocks(parsed: list[tuple[str, list[Block], list[dict[str, str]]]],
                        targets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """results.json (shared format) from stargazer blocks. `parsed`: (script, its blocks in order, its calls in order).
    A block is labelled by the call at the same position; one table (label, model) per column. A term that matches a
    target's term apart from case and punctuation takes the target's spelling ("President's" / "PRESIDENT’S"), so the
    comparison lines them up without a mapping."""
    from .compare import norm_model, norm_table
    want: dict[tuple[str, str], dict[str, str]] = {}
    for t in (targets or [])[:2_000]:
        want.setdefault((norm_table(t.get("table")), norm_model(t.get("model"))), {})[_loose(str(t.get("term") or ""))] = \
            str(t.get("term") or "")
    tables: list[dict[str, Any]] = []
    used: dict[tuple[str, str], int] = {}
    for script, blocks, calls in parsed:
        stem = posixpath.basename(script)
        for k, b in enumerate(blocks):
            call = calls[k] if k < len(calls) else {}
            label = call.get("label") or (f"{stem} 第 {k + 1} 个表" if not b.caption else b.caption)
            n_stats = {key: v for key, v in b.stats.items() if re.fullmatch(r"(?:Observations|N)", key, re.I)}
            for j, model in enumerate(b.models):
                model = model or f"({j + 1})"
                rows = []
                n_txt = next((v[j] for v in n_stats.values() if j < len(v) and v[j]), None)
                n_val = None
                if n_txt:
                    m = re.search(r"\d[\d,]*", _tex_text(n_txt))
                    n_val = int(m.group(0).replace(",", "")) if m and len(m.group(0)) < 15 else None
                key = (norm_table(label), norm_model(model))
                names = want.get(key, {})
                for term, cells in b.rows:
                    c = cells[j] if j < len(cells) else None
                    if c is None or c.estimate is None:
                        continue
                    ci = [c.ci[0], c.ci[1]] if c.ci else None
                    rows.append({"term": names.get(_loose(term), term), "estimate": c.estimate, "se": c.se, "p": None,
                                 "ci": ci, "n": n_val})
                if not rows:
                    continue
                shown = label
                dup = used.get((label, model), 0)
                used[(label, model)] = dup + 1
                if dup:
                    shown = f"{label}（{stem} 的第 {dup + 1} 个）"
                kind = (b.types[j] if j < len(b.types) and b.types[j] else "") or "R 模型"
                notes = [f"来自 {stem} 的 stargazer 输出" + (f"：{call['title']}" if call.get("title") else ""),
                         "p 值 stargazer 不输出，留空；显著性对照时按估计值和标准误推算"]
                if b.dep and j < len(b.dep) and b.dep[j]:
                    notes.append(f"因变量（stargazer 的写法）：{b.dep[j]}")
                tables.append({"label": shown, "model": model, "estimator": kind, "rows": rows, "notes": notes,
                               "needs_r": False})
    return {"tables": tables}
