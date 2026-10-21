"""复现检查图 (card 0194): the paper's estimates and ours drawn side by side, so it shows at a glance which targets
line up and which drift. Pure functions over compare.compare(...)["items"]: no DB, no AI, nothing is written; each call
returns the image as bytes, SVG for the page and PNG for the 展示稿.

- table_chart(items, title=…): one table. A panel per term (the paper's row), so every panel has its own x range even
  when coefficients differ by orders of magnitude; inside a panel one row per model, in the table's column order. Each
  row carries the paper (hollow, muted, value ± 1.96·se) and, just below it, ours (filled, coloured by verdict, with its
  own interval). A point without an se is drawn without an interval; a paper target that only gives a sign has no
  paper mark. Rows that do not line up are also named at the right edge (仅方向一致 / 对不上 / 未复现), so the verdict
  never rests on colour alone.
- overview_chart(groups): one row per table, a square per target coloured by verdict and sorted green → ochre → rust →
  grey like a waffle bar, with 「8 / 10 一致」 at the right.
- group_by_table(items), summary_line(items): helpers the page and the deck share with the charts.

Verdict colours come from Leo's slide template: exact and within share the green (both count as 一致), direction ochre,
mismatch rust, missing the line grey.

Outside text. Table, model and term labels come from the paper and the authors' code (AGENTS.md, "Scanning outside
text"): every label is cut before anything reads it, control, bidirectional and zero-width characters are dropped, it is
shortened to LABEL_CHARS and then to the room it has, and it is drawn with parse_math=False and without usetex, so `$`
and backslashes are plain characters; SVG keeps text as escaped <text> nodes (svg.fonttype none). Only finite int and
float values up to ±MAX_ABS are drawn, and any error while drawing gives a picture saying 这张图没能画出来 instead of an
exception. Rows per table chart (MAX_ROWS), panels (MAX_PANELS), tables in the overview (MAX_TABLES) and squares per
overview row (MAX_CELLS) are capped, and the chart says what was left out.

Rendering. matplotlib's object API only (Figure + FigureCanvasAgg / FigureCanvasSVG), never pyplot. Fonts and colours
are set on each artist, so other figures in the process inherit nothing from these charts. What the SVG writer reads at
save time (svg.fonttype, svg.hashsalt) can only come from rcParams, so a render runs inside rc_context: for that window
the process-wide rcParams carry those keys, and on exit rc_context restores the whole rcParams as it found it, undoing
any rcParams write another thread made meanwhile. Renders are therefore serialized by a lock, and stats.new_figure()'s
one-time font setup (a global rcParams write guarded by a flag) is run under that lock before the first render, so it
can never land inside the window. The same input gives the same bytes (fixed svg.hashsalt, no date in the metadata), so
callers may cache by content."""
from __future__ import annotations

import io
import logging
import math
import os
import re
import threading
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

from . import compare

log = logging.getLogger("research_lab.replication.checkviz")

# Leo's slide template
BG, INK, GREEN, MUTED, LINE = "#fbfaf6", "#213831", "#2b5148", "#657267", "#d5dbd4"
RUST, BLUE, OCHRE = "#96513c", "#365f77", "#a58156"

# Verdict groups as the charts and summary_line show them: (key, label, colour, verdicts)
GROUPS = (("ok", "一致", GREEN, ("exact", "within")),
          ("direction", "仅方向一致", OCHRE, ("direction",)),
          ("mismatch", "对不上", RUST, ("mismatch",)),
          ("missing", "未复现", LINE, ("missing",)))
GROUP_OF = {v: key for key, _, _, verdicts in GROUPS for v in verdicts}
GROUP_LABEL = {key: label for key, label, _, _ in GROUPS}
GROUP_COLOR = {key: color for key, _, color, _ in GROUPS}
ROW_NOTE = {"direction": "仅方向一致", "mismatch": "对不上", "missing": "未复现"}

# Ceilings
MAX_ROWS = 60            # rows (targets) drawn in one table chart
MAX_PANELS = 12          # terms in one table chart
MAX_TABLES = 30          # rows of the overview
MAX_CELLS = 200          # squares in one overview row; past it the row is drawn to scale
MAX_SCAN = 5000          # items looked at for one table chart
LABEL_CHARS = 40
MAX_ABS = 1e150          # larger numbers are not drawn

FORMATS = ("svg", "png")
WIDTH_IN = 40 / 3        # 13.33 in: 1600 px at PNG_DPI, 960 pt in the SVG viewBox
SLIDE_H_IN = 7.5         # 16:9 at WIDTH_IN
PNG_DPI = 120
NOTE_PT = 10.5
Z = 1.96

CJK_FONT_FILES = ("/System/Library/Fonts/Hiragino Sans GB.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
                  "/Library/Fonts/Arial Unicode.ttf")
CJK_FAMILIES = ("Hiragino Sans GB", "Heiti SC", "Arial Unicode MS", "PingFang SC", "Noto Sans CJK SC",
                "Source Han Sans SC", "WenQuanYi Zen Hei")
# Latin first: the CJK fonts set ’ “ ” full width ("PRESIDENT’ S"). matplotlib and the browser fall back per glyph.
LATIN_FAMILIES = ("Helvetica Neue", "Arial")

_RENDER_LOCK = threading.Lock()
_FONT_LOCK = threading.Lock()
_families: tuple[list[str], list[str]] | None = None


# ── text from outside ────────────────────────────────────────────────────────────────────────────────────────

_BAD = re.compile("[\x00-\x1f\x7f-\x9f\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u206f"
                  "\ud800-\udfff\ufdd0-\ufdef\ufeff\ufff0-\uffff]")


def clean_label(text: Any, limit: int = LABEL_CHARS) -> str:
    """One line, no control, bidirectional or zero-width characters, at most `limit` characters. The input is cut
    before any pattern sees it, so a 100 kB label costs no more than a short one."""
    if isinstance(text, bool) or not isinstance(text, (str, int, float)):
        return ""
    s = text if isinstance(text, str) else str(text)
    s = " ".join(_BAD.sub(" ", s[: limit * 4 + 64]).split())
    return s if len(s) <= limit else s[: max(0, limit - 1)].rstrip() + "…"


def _em(s: str) -> float:
    """Rough width in ems: wide (CJK) characters 1, the rest 0.62."""
    return sum(1.0 if unicodedata.east_asian_width(c) in "WF" else 0.62 for c in s)


def _fit(s: str, max_em: float) -> str:
    """`s` shortened with an ellipsis to about `max_em` ems."""
    if _em(s) <= max_em:
        return s
    out, used = [], 1.0
    for c in s:
        used += 1.0 if unicodedata.east_asian_width(c) in "WF" else 0.62
        if used > max_em:
            break
        out.append(c)
    return "".join(out).rstrip() + "…"


def _num(v: Any) -> float | None:
    """A finite int or float within ±MAX_ABS, else None: past that, interval ends and axis spans overflow."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        x = float(v)
    except OverflowError:
        return None
    return x if math.isfinite(x) and abs(x) <= MAX_ABS else None


def _interval(value: float | None, se: float | None) -> tuple[float, float] | None:
    if value is None or se is None:
        return None
    lo, hi = value - Z * abs(se), value + Z * abs(se)
    return (lo, hi) if math.isfinite(lo) and math.isfinite(hi) else None


def _verdict(item: dict[str, Any]) -> str:
    v = item.get("verdict")
    return v if isinstance(v, str) and v in GROUP_OF else "missing"


# ── helpers the page shares ──────────────────────────────────────────────────────────────────────────────────

def group_by_table(items: Iterable[Any]) -> dict[str, list[dict[str, Any]]]:
    """Items by table, in order of first appearance. 'Table 3', 'table 3.' and '表 3' are one table, shown with the
    first spelling met; an item without a table goes under 未标表."""
    out: dict[str, list[dict[str, Any]]] = {}
    label_of: dict[str, str] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        label = clean_label(item.get("table"))
        key = compare.norm_table(label) if label else ""
        shown = label_of.setdefault(key, label or "未标表")
        out.setdefault(shown, []).append(item)
    return out


def counts(items: Iterable[Any]) -> dict[str, int]:
    """How many items fall in each verdict group (ok, direction, mismatch, missing); an unknown verdict is missing."""
    c = dict.fromkeys(GROUP_OF.values(), 0)
    for item in items or []:
        if isinstance(item, dict):
            c[GROUP_OF[_verdict(item)]] += 1
    return c


def summary_line(items: Iterable[Any]) -> str:
    """「14 个一致，6 个未复现」: the non-empty groups in a fixed order."""
    c = counts(items)
    parts = [f"{c[key]} 个{label}" for key, label, _, _ in GROUPS if c[key]]
    return "，".join(parts) if parts else "没有复现目标"


# ── fonts and rendering ──────────────────────────────────────────────────────────────────────────────────────

class _QuietWeights(logging.Filter):
    """Hiragino Sans GB ships weights 300 and 600 only; matplotlib warns each time it substitutes them for 400/700."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not str(record.msg).startswith("findfont: Failed to find font weight")


def cjk_fonts() -> list[str]:
    """The CJK font families matplotlib can use here, best first. Empty on a machine without any: Chinese then comes
    out as boxes, and the charts still render."""
    return list(_fonts()[1])


def font_families() -> list[str]:
    """The family list the charts use: a Latin face, the CJK faces, DejaVu Sans (matplotlib's own, always there)."""
    latin, cjk = _fonts()
    return latin + cjk + ["DejaVu Sans"]


def _fonts() -> tuple[list[str], list[str]]:
    """Registers the macOS CJK fonts once when the font cache does not already list them."""
    global _families
    with _FONT_LOCK:
        if _families is None:
            from matplotlib import font_manager
            fm = font_manager.fontManager
            known = {f.fname for f in fm.ttflist}
            for path in CJK_FONT_FILES:
                if path not in known and os.path.isfile(path):
                    try:
                        fm.addfont(path)
                    except Exception as e:          # an unreadable font only costs us that font
                        log.warning("checkviz: could not register %s: %s", path, e)
            have = {f.name for f in fm.ttflist}
            _families = [f for f in LATIN_FAMILIES if f in have], [f for f in CJK_FAMILIES if f in have]
            logging.getLogger("matplotlib.font_manager").addFilter(_QuietWeights())
        return _families


def _txt() -> dict[str, Any]:
    """Keyword arguments for every text the charts draw: fonts set on the artist, labels never parsed as mathtext."""
    return {"parse_math": False, "fontfamily": font_families()}


# Only what the SVG writer reads at save time goes through rcParams; fonts and colours are set on each artist.
_RC = {"svg.fonttype": "none", "svg.hashsalt": "research-lab-checkviz", "text.usetex": False}
_SVG_FONT = re.compile(r'(<text\b[^>]*?font-family: )([^;"]*)')
_stats_warm = False


def _warm_stats() -> None:
    """Run stats' one-time rcParams font setup before the first render (caller holds _RENDER_LOCK): rc_context restores
    the whole rcParams on exit, so a first stats.new_figure() inside a render would lose its fonts for good."""
    global _stats_warm
    if _stats_warm:
        return
    _stats_warm = True
    try:
        from . import stats
        stats.new_figure(1, 1)
    except Exception as e:                     # stats' fonts are stats' business; the charts do not need them
        log.warning("checkviz: stats font setup failed: %s", e)


def _check_fmt(fmt: str) -> str:
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; known: {', '.join(FORMATS)}")
    return fmt


def _save(fig: Any, fmt: str) -> bytes:
    buf = io.BytesIO()
    if fmt == "png":
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        FigureCanvasAgg(fig)
        fig.savefig(buf, format="png", dpi=PNG_DPI, facecolor=BG, metadata={"Software": None})
    else:
        from matplotlib.backends.backend_svg import FigureCanvasSVG
        FigureCanvasSVG(fig)
        fig.savefig(buf, format="svg", facecolor=BG, metadata={"Date": None})
        svg = _SVG_FONT.sub(lambda m: m.group(1) + m.group(2) + ", sans-serif", buf.getvalue().decode("utf-8"))
        return svg.encode("utf-8")
    return buf.getvalue()


def _render(draw: Any, fmt: str, title: str = "") -> bytes:
    """draw() → bytes. Any error while drawing or saving gives a picture that says so instead of an exception: the
    numbers come from papers and author code, and a page should not fail over one odd value."""
    import matplotlib
    with _RENDER_LOCK:
        _warm_stats()
        with matplotlib.rc_context(_RC):
            try:
                return _save(draw(), fmt)
            except Exception:
                log.warning("checkviz: chart failed, drawing a placeholder", exc_info=True)
                return _save(_message_figure("这张图没能画出来", title), fmt)


def _message_figure(text: str, title: str = "") -> Any:
    from matplotlib.figure import Figure
    fig = Figure(figsize=(WIDTH_IN, 2.4), facecolor=BG)
    if title:
        fig.text(0.3 / WIDTH_IN, 1 - 0.45 / 2.4, _fit(title, 44), fontsize=16, fontweight="bold", color=INK,
                 va="center", **_txt())
    fig.text(0.5, 0.42, text, fontsize=14, color=MUTED, ha="center", va="center", **_txt())
    return fig


# ── one table ────────────────────────────────────────────────────────────────────────────────────────────────

def _row(item: dict[str, Any], index: int) -> dict[str, Any]:
    paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
    ours = item.get("ours") if isinstance(item.get("ours"), dict) else {}
    verdict = _verdict(item)
    value, se = _num(paper.get("value")), _num(paper.get("se"))
    est, ose = _num(ours.get("estimate")), _num(ours.get("se"))
    if verdict == "missing":
        est = ose = None
    model = clean_label(item.get("model"))
    term = clean_label(item.get("term"))
    return {"i": index, "verdict": verdict, "model": model or "未标模型", "term": term or "未标变量",
            "model_key": compare.norm_model(model), "term_key": compare.norm_term(term) if term else "",
            "value": value, "paper_ci": _interval(value, se), "est": est, "ours_ci": _interval(est, ose)}


def _model_order(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Column order: numbered models ((1), Model 2, iii) by number, otherwise as first met."""
    keys = list(dict.fromkeys(r["model_key"] for r in rows))
    if keys and all(k.isdigit() and len(k) < 10 for k in keys):
        keys.sort(key=int)
    return {k: n for n, k in enumerate(keys)}


def _panels(items: Sequence[Any]) -> tuple[list[tuple[str, list[dict[str, Any]]]], int]:
    """Terms in order of first appearance, each with its rows in column order; and how many items were left out."""
    by_term: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    drawn = 0
    for i, item in enumerate(items[:MAX_SCAN]):
        if drawn >= MAX_ROWS:
            break
        if not isinstance(item, dict):
            continue
        r = _row(item, i)
        key = r["term_key"] or r["term"]
        if key not in by_term:
            if len(by_term) >= MAX_PANELS:
                continue
            by_term[key] = (r["term"], [])
        by_term[key][1].append(r)
        drawn += 1
    order = _model_order([r for _, rows in by_term.values() for r in rows])
    panels = [(label, sorted(rows, key=lambda r: (order[r["model_key"]], r["i"]))) for label, rows in by_term.values()]
    return panels, len(items) - drawn


def _grid(n: int) -> int:
    return {1: 1, 2: 2, 3: 3, 4: 2}.get(n) or (3 if n <= 9 else 4)


def _xrange(rows: list[dict[str, Any]]) -> tuple[float, float, bool]:
    """Data limits for a panel, and whether any number was there to draw. Zero joins the range when it is near the
    data (within one data span), so the sign is visible; a panel far from zero keeps its own scale."""
    xs: list[float] = []
    for r in rows:
        for v in (r["value"], r["est"]):
            if v is not None:
                xs.append(v)
        for ci in (r["paper_ci"], r["ours_ci"]):
            if ci:
                xs.extend(ci)
    if not xs:
        return -1.0, 1.0, False
    lo, hi = min(xs), max(xs)
    span = hi - lo
    if span <= 0:
        span = abs(lo) * 0.5 or 1.0
        lo, hi = lo - span / 2, hi + span / 2
    if 0 < lo <= span:
        lo = 0.0
    elif -span <= hi < 0:
        hi = 0.0
    pad = (hi - lo) * 0.08
    if not math.isfinite(pad) or pad <= 0:
        pad = 1.0
    return lo - pad, hi + pad, True


def _tick(v: float, _pos: Any = None) -> str:
    s = f"{v:.3g}"
    return "0" if s in ("-0", "0", "-0.0") else s


def _draw_panel(ax: Any, label: str, rows: list[dict[str, Any]], slots: int, width_in: float) -> None:
    """One term: `slots` rows tall (the tallest panel in its grid row), about `width_in` inches of plot."""
    from matplotlib import ticker, transforms
    from matplotlib.collections import LineCollection

    lo, hi, has_data = _xrange(rows)
    notes = [(n, ROW_NOTE[r["verdict"]], r["verdict"]) for n, r in enumerate(rows) if r["verdict"] in ROW_NOTE]
    right = hi
    if notes:                          # a strip at the right edge for the notes, kept clear of the data
        share = min(0.45, (max(_em(t) for _, t, _ in notes) * NOTE_PT / 72 + 0.2) / width_in)
        right = hi + (hi - lo) * share / (1 - share)
    ax.set_xlim(lo, right)
    ax.set_ylim(slots - 0.5, -0.5)
    nbins = max(3, min(8, int(width_in / 0.75)))
    ticks = [t for t in ticker.MaxNLocator(nbins=nbins, steps=[1, 2, 2.5, 5, 10]).tick_values(lo, hi) if lo <= t <= hi]
    ax.set_xticks(ticks)
    ax.xaxis.set_major_formatter(ticker.FuncFormatter(_tick))
    ax.grid(axis="x", color=LINE, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(LINE)
    ax.set_facecolor(BG)
    ax.tick_params(axis="x", labelsize=9.5, colors=MUTED, length=3, color=LINE, labelfontfamily=font_families())
    ax.tick_params(axis="y", labelsize=11, labelcolor=INK, length=0, pad=6)
    ax.set_yticks(range(len(rows)), [_fit(r["model"], 12) for r in rows], **_txt())
    ax.set_title(_fit(label, (width_in + 0.4) * 72 / 12.5), loc="left", fontsize=12.5, fontweight="bold", color=INK,
                 pad=8, **_txt())
    if lo < 0 < hi:
        ax.axvline(0, color=MUTED, linewidth=1.0, linestyle=(0, (4, 3)), zorder=1)
    if not has_data:
        ax.text(0.5, 0.5, "没有可画的数值", transform=ax.transAxes, ha="center", va="center", fontsize=10.5,
                color=MUTED, **_txt())

    off = 0.18
    p_segs, p_x, p_y = [], [], []
    o_segs, o_seg_colors, o_x, o_y, o_colors = [], [], [], [], []
    for n, r in enumerate(rows):
        if r["value"] is not None:
            if r["paper_ci"]:
                p_segs.append([(r["paper_ci"][0], n - off), (r["paper_ci"][1], n - off)])
            p_x.append(r["value"])
            p_y.append(n - off)
        if r["est"] is not None:
            color = GROUP_COLOR[GROUP_OF[r["verdict"]]]
            if r["ours_ci"]:
                o_segs.append([(r["ours_ci"][0], n + off), (r["ours_ci"][1], n + off)])
                o_seg_colors.append(color)
            o_x.append(r["est"])
            o_y.append(n + off)
            o_colors.append(color)
    if p_segs:
        ax.add_collection(LineCollection(p_segs, colors=MUTED, linewidths=1.6, zorder=2, capstyle="butt"),
                          autolim=False)
    if o_segs:
        ax.add_collection(LineCollection(o_segs, colors=o_seg_colors, linewidths=2.0, zorder=2, capstyle="butt"),
                          autolim=False)
    if p_x:
        ax.scatter(p_x, p_y, s=58, marker="o", facecolors=BG, edgecolors=MUTED, linewidths=1.6, zorder=3)
    if o_x:
        ax.scatter(o_x, o_y, s=66, marker="o", c=o_colors, edgecolors=BG, linewidths=1.0, zorder=4)
    edge = transforms.blended_transform_factory(ax.transAxes, ax.transData)
    for n, text, verdict in notes:
        strong = verdict == "mismatch"
        ax.text(0.985, n, text, transform=edge, ha="right", va="center", fontsize=NOTE_PT,
                color=INK if strong else MUTED, fontweight="bold" if strong else "normal", **_txt())


def _legend_handles(panels: list[tuple[str, list[dict[str, Any]]]]) -> tuple[list[Any], list[str]]:
    from matplotlib.lines import Line2D
    present = {GROUP_OF[r["verdict"]] for _, rows in panels for r in rows if r["est"] is not None}
    handles = [Line2D([], [], color=MUTED, linewidth=1.6, marker="o", markersize=7.5, markerfacecolor=BG,
                      markeredgecolor=MUTED, markeredgewidth=1.6)]
    labels = ["论文"]
    shown = [key for key in ("ok", "direction", "mismatch") if key in present] or ["ok"]
    for key in shown:
        handles.append(Line2D([], [], color=GROUP_COLOR[key], linewidth=2.0, marker="o", markersize=8,
                              markerfacecolor=GROUP_COLOR[key], markeredgecolor=BG, markeredgewidth=1.0))
        labels.append(f"复现：{GROUP_LABEL[key]}" if present else "复现")
    return handles, labels


def table_chart(items: Sequence[Any], *, title: str = "", fmt: str = "svg") -> bytes:
    """One table's check as SVG or PNG bytes (see the module docstring for the layout). `items` are one table's
    compare items in target order; `title` is drawn as given (cleaned), with summary_line(items) under it."""
    fmt = _check_fmt(fmt)
    items = items if isinstance(items, (list, tuple)) else list(items or [])
    title = clean_label(title)
    panels, left_out = _panels(items)
    summary = summary_line(items)

    def draw() -> Any:
        from matplotlib.figure import Figure
        if not panels:
            return _message_figure("没有可画的复现目标", title)
        n = len(panels)
        ncols = _grid(n)
        nrows = math.ceil(n / ncols)
        slots = [max(len(rows) for _, rows in panels[g * ncols:(g + 1) * ncols]) for g in range(nrows)]
        header = 1.1 if title else 0.72
        footer = 0.42 if left_out else 0.12
        overhead = 0.72                                   # panel title above, tick labels below
        room = SLIDE_H_IN - header - footer - nrows * overhead
        row_h = min(0.8, max(0.3, room / sum(slots)))
        height = header + footer + sum(k * row_h + overhead for k in slots)
        fig = Figure(figsize=(WIDTH_IN, height), facecolor=BG, layout="constrained")
        fig.get_layout_engine().set(rect=(0, footer / height, 1, 1 - (header + footer) / height),
                                    w_pad=0.1, h_pad=0.06, wspace=0.05, hspace=0.06)
        gs = fig.add_gridspec(nrows, ncols, height_ratios=slots)
        width_in = WIDTH_IN / ncols - 0.9            # less the model labels and the gutters
        for k, (label, rows) in enumerate(panels):
            g, c = divmod(k, ncols)
            _draw_panel(fig.add_subplot(gs[g, c]), label, rows, slots[g], width_in)
        x0 = 0.3 / WIDTH_IN
        line2 = 1 - (0.36 if not title else 0.8) / height
        if title:
            fig.text(x0, 1 - 0.36 / height, _fit(title, (WIDTH_IN - 0.6) * 72 / 16), fontsize=16, fontweight="bold",
                     color=INK, va="center", **_txt())
        fig.text(x0, line2, summary, fontsize=11.5, color=MUTED, va="center", **_txt())
        handles, labels = _legend_handles(panels)
        fig.legend(handles, labels, loc="center right", bbox_to_anchor=(1 - 0.2 / WIDTH_IN, line2), ncols=len(labels),
                   frameon=False, prop={"family": font_families(), "size": 11}, labelcolor=INK, handlelength=2.2,
                   columnspacing=1.6, borderaxespad=0)
        if left_out:
            fig.text(x0, 0.2 / height, f"另有 {left_out} 项未画出", fontsize=10.5, color=MUTED, va="center",
                     **_txt())
        return fig

    return _render(draw, fmt, title)


# ── every table ──────────────────────────────────────────────────────────────────────────────────────────────

def _cells(c: dict[str, int], limit: int) -> list[str]:
    """Group keys, one per square, green first. More than `limit` targets are drawn to scale in `limit` squares
    (largest remainder), so the colours keep their share."""
    keys = [key for key, _, _, _ in GROUPS]
    total = sum(c[k] for k in keys)
    if total <= limit:
        return [k for k in keys for _ in range(c[k])]
    exact = {k: c[k] * limit / total for k in keys}
    n = {k: math.floor(exact[k]) for k in keys}
    for k in sorted(keys, key=lambda k: (n[k] - exact[k], keys.index(k)))[: limit - sum(n.values())]:
        n[k] += 1
    return [k for k in keys for _ in range(n[k])]


def overview_chart(groups: Mapping[str, Sequence[Any]] | Iterable[tuple[str, Sequence[Any]]], *, title: str = "",
                   fmt: str = "svg") -> bytes:
    """All tables at once as SVG or PNG bytes: one row per table (label, a strip of verdict squares, 「a / n 一致」).
    `groups` is an ordered mapping table label → items, such as group_by_table(items)."""
    fmt = _check_fmt(fmt)
    pairs = list(groups.items()) if isinstance(groups, Mapping) else list(groups or [])
    hidden = max(0, len(pairs) - MAX_TABLES)
    rows = []
    for pair in pairs[:MAX_TABLES]:
        label, items = (pair if isinstance(pair, (list, tuple)) and len(pair) == 2 else ("", []))
        items = items if isinstance(items, (list, tuple)) else []
        c = counts(items)
        rows.append({"label": clean_label(label) or "未标表", "counts": c, "total": sum(c.values()),
                     "cells": _cells(c, MAX_CELLS)})
    title = clean_label(title)

    def draw() -> Any:
        from matplotlib.collections import PolyCollection
        from matplotlib.figure import Figure
        if not rows:
            return _message_figure("没有复现目标", title)
        W, M, fs = WIDTH_IN, 0.35, 12
        label_w = min(3.4, max(_em(r["label"]) for r in rows) * fs / 72) + 0.3
        count_w = 1.9
        x0, x1 = M + label_w, W - M - count_w
        widest = max(1, max(len(r["cells"]) for r in rows))
        pitch = min(0.3, (x1 - x0) / widest)
        size = pitch * 0.82 if pitch >= 0.05 else pitch
        row_h = max(0.48, size + 0.2)
        legend_y = 0.95 if title else 0.4
        top = legend_y + 0.42
        bottom = top + len(rows) * row_h
        notes = ([f"另有 {hidden} 张表未画出"] if hidden else []) + \
                ([f"超过 {MAX_CELLS} 个目标的表按比例画格"] if any(r["total"] > MAX_CELLS for r in rows) else [])
        H = max(2.0, bottom + 0.25 + 0.32 * len(notes) + 0.15)
        fig = Figure(figsize=(W, H), facecolor=BG)
        ax = fig.add_axes((0, 0, 1, 1))
        ax.set_xlim(0, W)
        ax.set_ylim(H, 0)
        ax.set_axis_off()
        if title:
            ax.text(M, 0.42, _fit(title, (W - 2 * M) * 72 / 16), fontsize=16, fontweight="bold", color=INK,
                    va="center", **_txt())
        x, sq = M, 0.18
        legend = []
        for key, label, color, _ in GROUPS:
            legend.append([(x, legend_y - sq / 2), (x + sq, legend_y - sq / 2), (x + sq, legend_y + sq / 2),
                           (x, legend_y + sq / 2)])
            ax.text(x + sq + 0.08, legend_y, label, fontsize=11, color=INK, va="center", **_txt())
            x += sq + 0.08 + _em(label) * 11 / 72 + 0.4
        ax.add_collection(PolyCollection(legend, facecolors=[c for _, _, c, _ in GROUPS], edgecolors="none"))
        verts, colors = [], []
        for i, r in enumerate(rows):
            y = top + (i + 0.5) * row_h
            ax.text(M, y, _fit(r["label"], 3.4 * 72 / fs), fontsize=fs, color=INK, va="center", **_txt())
            for j, key in enumerate(r["cells"]):
                cx = x0 + j * pitch
                verts.append([(cx, y - size / 2), (cx + size, y - size / 2), (cx + size, y + size / 2),
                              (cx, y + size / 2)])
                colors.append(GROUP_COLOR[key])
            ok = r["counts"]["ok"]
            text = f"{ok} / {r['total']} 一致" if r["total"] else "没有复现目标"
            ax.text(W - M, y, text, fontsize=fs, color=INK, ha="right", va="center", **_txt())
        if verts:
            ax.add_collection(PolyCollection(verts, facecolors=colors, edgecolors="none"))
        ax.hlines([top + i * row_h for i in range(len(rows) + 1)], M, W - M, colors=LINE, linewidths=0.7)
        for k, note in enumerate(notes):
            ax.text(M, bottom + 0.3 + 0.32 * k, note, fontsize=10.5, color=MUTED, va="center", **_txt())
        return fig

    return _render(draw, fmt, title)
