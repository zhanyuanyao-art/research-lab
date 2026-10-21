"""Card 0194: 复现检查图. The paper's estimates and ours drawn together per table (a panel per term, a row per model),
and an overview with one strip of verdict squares per table. Pure functions returning SVG or PNG bytes."""
from __future__ import annotations

import re
import time
import warnings
import xml.etree.ElementTree as ET

import pytest

from research_lab.replication import checkviz as V
from research_lab.replication import compare as C

RLO, ZWSP = chr(0x202E), chr(0x200B)          # right-to-left override, zero-width space
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JOST_TERMS = ("MEAN HAWKISHNESS", "PRESIDENT’S HAWKISHNESS", "ADVISERS’ HAWKISHNESS (ACTS)")


def _jost() -> list[dict]:
    """Table 3: eight models × three terms, every estimate reproduced to the printed decimals. Table 4: six targets
    with no output at all."""
    base = {JOST_TERMS[0]: 0.412, JOST_TERMS[1]: 0.031, JOST_TERMS[2]: 2.73}
    targets, rows = [], []
    for m in range(1, 9):
        for term in JOST_TERMS:
            v = round(base[term] * (1 + 0.1 * m), 3)
            se = round(abs(v) * 0.3, 3)
            targets.append({"table": "Table 3", "model": f"({m})", "term": term, "value": f"{v:.3f}",
                            "se": f"{se:.3f}", "sig": "***"})
            rows.append({"model": f"({m})", "term": term, "estimate": v, "se": se, "p": 0.001, "ci": None, "n": 412})
    for m in range(1, 7):
        targets.append({"table": "Table 4", "model": f"({m})", "term": JOST_TERMS[0], "value": "0.30", "se": "0.10",
                        "sig": "**"})
    return C.compare(targets, {"tables": [{"label": "Table 3", "rows": rows}]})["items"]


def _item(verdict="exact", *, table="Table 2", model="(1)", term="treated_post", value=0.14, se=0.05, est=0.14,
          ose=0.05, ours=True) -> dict:
    return {"id": "T1", "table": table, "model": model, "term": term, "page": 7, "verdict": verdict,
            "paper": {"value": value, "decimals": 2, "sign": 1, "se": se, "se_from": "se", "sig": "**", "band": 2,
                      "n": 100},
            "ours": {"estimate": est, "se": ose, "p": 0.01, "band": 2, "sig": "**", "n": 100} if ours else None,
            "diff": None, "diff_se": None, "reasons": []}


def _svg_ok(data: bytes) -> str:
    text = data.decode("utf-8")
    root = ET.fromstring(data)
    assert root.tag.endswith("svg")
    assert "<script" not in text.lower()
    assert "<text" in text                      # svg.fonttype none: labels stay text
    return text


def _png_ok(data: bytes) -> None:
    assert data.startswith(PNG_SIGNATURE)
    assert len(data) > 2000


def _png_size(data: bytes) -> tuple[int, int]:
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


# ── helpers ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_jost_items_group_and_summarize():
    groups = V.group_by_table(_jost())
    assert list(groups) == ["Table 3", "Table 4"]
    assert {x["verdict"] for x in groups["Table 3"]} == {"exact"}
    assert {x["verdict"] for x in groups["Table 4"]} == {"missing"}
    assert V.summary_line(groups["Table 3"]) == "24 个一致"
    assert V.summary_line(_jost()) == "24 个一致，6 个未复现"


def test_group_by_table_merges_spellings_and_keeps_order():
    items = [_item(table="Table 2"), _item(table="表 1"), _item(table="table 2."), _item(table=""), "junk", None]
    groups = V.group_by_table(items)
    assert list(groups) == ["Table 2", "表 1", "未标表"]
    assert len(groups["Table 2"]) == 2


def test_summary_line_counts_every_group_in_order():
    items = [_item("mismatch"), _item("within"), _item("direction"), _item("exact"), _item("missing"),
             _item("bogus"), {"verdict": None}]
    assert V.summary_line(items) == "2 个一致，1 个仅方向一致，1 个对不上，3 个未复现"
    assert V.summary_line([]) == "没有复现目标"
    assert "（" not in V.summary_line(items) and "(" not in V.summary_line(items)


def test_clean_label_strips_controls_and_truncates():
    hostile = "\x00\x1b[31m<script>alert(1)</script>" + RLO + ZWSP + "x" * 100_000
    out = V.clean_label(hostile)
    assert len(out) <= V.LABEL_CHARS
    assert out.startswith("[31m<script>")
    assert all(c not in out for c in ("\x00", "\x1b", RLO, ZWSP, "\n"))
    assert V.clean_label(None) == "" and V.clean_label(True) == "" and V.clean_label({"a": 1}) == ""
    assert V.clean_label(3) == "3"


# ── table chart ──────────────────────────────────────────────────────────────────────────────────────────────

def test_table_chart_jost_svg_and_png():
    groups = V.group_by_table(_jost())
    svg = _svg_ok(V.table_chart(groups["Table 3"], title="表 3"))
    for term in JOST_TERMS:
        assert term in svg
    assert "(8)" in svg and "论文" in svg and "复现：一致" in svg
    assert "24 个一致" in svg
    assert "未复现" not in svg and "对不上" not in svg
    png = V.table_chart(groups["Table 3"], title="表 3", fmt="png")
    _png_ok(png)
    w, h = _png_size(png)
    assert w == 1600 and 850 <= h <= 950              # a 16:9 slide picture


def test_table_chart_missing_rows_say_so():
    t4 = V.group_by_table(_jost())["Table 4"]
    svg = _svg_ok(V.table_chart(t4, title="表 4"))
    assert svg.count("未复现") == 6 + 1               # one per row, and the summary line
    _png_ok(V.table_chart(t4, fmt="png"))


def test_table_chart_names_drifting_rows():
    items = [_item("exact", model="(1)"), _item("mismatch", model="(2)", est=-0.3),
             _item("direction", model="(3)", est=0.4), _item("within", model="(4)", est=0.12)]
    svg = _svg_ok(V.table_chart(items))
    assert "对不上" in svg and "仅方向一致" in svg
    assert "复现：对不上" in svg and "复现：仅方向一致" in svg
    assert V.RUST in svg and V.OCHRE in svg and V.GREEN in svg


def test_table_chart_orders_models_by_column_number():
    items = [_item(model=m) for m in ("(3)", "(10)", "(1)", "Model 2")]
    svg = V.table_chart(items).decode()
    positions = [svg.index(f">{m}<") for m in ("(1)", "Model 2", "(3)", "(10)")]
    assert positions == sorted(positions)


def test_table_chart_without_se_draws_points_only():
    items = [_item(se=None, ose=None), _item(model="(2)", value=None, se=None, est=0.2),
             _item(model="(3)", value=float("nan"), se=float("inf"), est=float("-inf"), ose=float("nan"))]
    _svg_ok(V.table_chart(items))
    _png_ok(V.table_chart(items, fmt="png"))


def test_table_chart_far_apart_terms_keep_their_own_range():
    items = [_item(term="gdp", value=12000.0, se=900.0, est=12100.0, ose=880.0),
             _item(term="treated_post", value=0.0012, se=0.0003, est=0.0011, ose=0.0003)]
    svg = _svg_ok(V.table_chart(items))
    assert "gdp" in svg and "treated_post" in svg


def test_empty_input_renders_a_message():
    for items in ([], None, ["junk", 3]):
        svg = _svg_ok(V.table_chart(items))
        assert "没有可画的复现目标" in svg
        _png_ok(V.table_chart(items, fmt="png"))
    svg = _svg_ok(V.overview_chart({}))
    assert "没有复现目标" in svg
    _png_ok(V.overview_chart({}, fmt="png"))


def test_unknown_format_is_refused():
    with pytest.raises(ValueError):
        V.table_chart([_item()], fmt="pdf")
    with pytest.raises(ValueError):
        V.overview_chart({}, fmt="html")


def test_hostile_labels_stay_plain_short_text():
    big = "<script>alert(1)</script>$\\frac{1}{0}$\x07\x1b" + RLO + "A" * 100_000
    items = [_item(table=big, model=big, term=big), _item(model="(2)", term=big + "B")]
    for data in (V.table_chart(items, title=big), V.overview_chart({big: items}, title=big)):
        svg = _svg_ok(data)
        assert len(svg) < 200_000
        assert "A" * 60 not in svg
        assert "&lt;script&gt;" in svg
        assert "\x07" not in svg and "\x1b" not in svg and RLO not in svg
        assert "\\frac" in svg                            # drawn as typed, not as mathtext
    _png_ok(V.table_chart(items, title=big, fmt="png"))
    _png_ok(V.overview_chart({big: items}, title=big, fmt="png"))


def test_rows_and_panels_are_capped():
    many_rows = [_item(model=f"({i})") for i in range(1, 200)]
    svg = _svg_ok(V.table_chart(many_rows))
    assert f"另有 {199 - V.MAX_ROWS} 项未画出" in svg
    many_terms = [_item(term=f"x{i}") for i in range(30)]
    svg = _svg_ok(V.table_chart(many_terms))
    assert f"另有 {30 - V.MAX_PANELS} 项未画出" in svg
    assert "x11" in svg and "x12" not in svg


def test_output_is_deterministic():
    items = _jost()
    for fmt in ("svg", "png"):
        assert V.table_chart(items[:24], title="表 3", fmt=fmt) == V.table_chart(items[:24], title="表 3", fmt=fmt)
        groups = V.group_by_table(items)
        assert V.overview_chart(groups, fmt=fmt) == V.overview_chart(groups, fmt=fmt)


# ── overview ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_overview_jost():
    groups = V.group_by_table(_jost())
    svg = _svg_ok(V.overview_chart(groups, title="复现总览"))
    assert "24 / 24 一致" in svg and "0 / 6 一致" in svg
    for label in ("一致", "仅方向一致", "对不上", "未复现"):
        assert f">{label}<" in svg
    assert V.LINE in svg and V.GREEN in svg
    png = V.overview_chart(groups, fmt="png")
    _png_ok(png)
    assert _png_size(png)[0] == 1600


def test_overview_accepts_pairs_and_caps_tables():
    pairs = [(f"Table {i}", [_item()]) for i in range(V.MAX_TABLES + 5)]
    svg = _svg_ok(V.overview_chart(pairs))
    assert "另有 5 张表未画出" in svg
    assert f"Table {V.MAX_TABLES - 1}" in svg and f"Table {V.MAX_TABLES}<" not in svg


def test_overview_scales_a_huge_table():
    items = [_item("exact")] * 300 + [_item("mismatch")] * 100
    assert V._cells(V.counts(items), V.MAX_CELLS).count("mismatch") == 50
    svg = _svg_ok(V.overview_chart({"Table 9": items}))
    assert "300 / 400 一致" in svg and "按比例画格" in svg


# ── fonts and speed ──────────────────────────────────────────────────────────────────────────────────────────

def test_chinese_renders_without_missing_glyphs():
    if not V.cjk_fonts():
        pytest.skip("no CJK font on this machine")
    items = _jost()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        V.table_chart(items[:24], title="表 3 总统与顾问的鹰派程度", fmt="png")
        V.table_chart(items[24:], title="表 4", fmt="png")
        V.overview_chart(V.group_by_table(items), title="复现总览", fmt="png")
        V.table_chart(items[:3], title="中文标题", fmt="svg")
    missing = [str(w.message) for w in caught if "missing from font" in str(w.message)]
    assert not missing, missing


@pytest.mark.parametrize("fmt", ["svg", "png"])
def test_five_hundred_items_render_fast(fmt):
    verdicts = ("exact", "within", "direction", "mismatch", "missing")
    items = [_item(verdicts[i % 5], table=f"Table {i % 40}", model=f"({i % 9})", term=f"var{i % 17}",
                   value=0.1 * (i % 7) + 0.01, est=0.1 * (i % 5)) for i in range(500)]
    V.table_chart(items[:5], fmt=fmt)                     # fonts registered outside the clock
    start = time.perf_counter()
    V.table_chart(items, title="Table", fmt=fmt)
    V.overview_chart(V.group_by_table(items), fmt=fmt)
    assert time.perf_counter() - start < 3.0


def test_megabyte_labels_cost_no_more_than_short_ones():
    huge = "(" * 1_000_000 + RLO + "$" * 1_000_000
    tables = [huge + str(k) for k in range(3)]                  # a few shared strings, not one per item
    terms = [huge + "t" + str(k) for k in range(4)]
    items = [_item(table=tables[i % 3], model=huge, term=terms[i % 4]) for i in range(500)]
    V.table_chart(items[:1])                                   # fonts and stats warmed outside the clock
    start = time.perf_counter()
    groups = V.group_by_table(items)
    V.summary_line(items)
    assert time.perf_counter() - start < 1.0
    start = time.perf_counter()
    _svg_ok(V.table_chart(items, title=huge))
    assert time.perf_counter() - start < 1.0
    start = time.perf_counter()
    _svg_ok(V.overview_chart(groups, title=huge))
    assert time.perf_counter() - start < 1.0


# ── odd numbers and failures ─────────────────────────────────────────────────────────────────────────────────

EXTREME_CASES = {
    "past the limit": [_item(value=1e308, est=-1e308, se=1, ose=1)],
    "at the limit": [_item(value=1e150, est=-1e150, se=1e150, ose=1e150)],
    "huge next to tiny": [_item(value=1e150, se=1.0), _item(model="(2)", value=1e-150, est=-1e-150, se=1e-150)],
    "tiny": [_item(value=1e-300, est=2e-300, se=1e-300, ose=1e-300)],
    "subnormal": [_item(value=5e-324, est=-5e-324, se=5e-324, ose=None)],
    "all zero": [_item(value=0.0, est=0.0, se=0.0, ose=0.0)],
    "one huge point": [_item(value=1e150, est=1e150, se=None, ose=None)],
    "huge ints": [_item(value=10 ** 400, est=-(10 ** 200), se=10 ** 10)],
    "huge se on a tiny value": [_item(value=1e-150, est=1e-150, se=1e150, ose=1e-300)],
}


@pytest.mark.parametrize("name", list(EXTREME_CASES))
def test_extreme_numbers_still_draw(name):
    items = EXTREME_CASES[name]
    for fmt in ("svg", "png"):
        table, overview = V.table_chart(items, fmt=fmt), V.overview_chart({"Table 2": items}, fmt=fmt)
        if fmt == "svg":
            for data in (table, overview):
                assert "没能画出来" not in _svg_ok(data)
        else:
            _png_ok(table)
            _png_ok(overview)
    assert V._num(1e308) is None and V._num(-1e151) is None and V._num(10 ** 400) is None
    assert V._num(1e150) == 1e150


def test_a_drawing_error_gives_a_placeholder(monkeypatch):
    def boom(*a, **k):
        raise ValueError("arange: cannot compute length")
    monkeypatch.setattr(V, "_draw_panel", boom)
    assert "这张图没能画出来" in _svg_ok(V.table_chart([_item()], title="表 2"))
    _png_ok(V.table_chart([_item()], fmt="png"))
    monkeypatch.setattr(V, "_em", boom)
    assert "这张图没能画出来" in _svg_ok(V.overview_chart({"Table 2": [_item()]}))


def test_stats_fonts_survive_a_render(monkeypatch):
    """stats.new_figure() writes its fonts into the process-wide rcParams once. Run for the first time while a chart
    is being drawn, the write would be undone when the render's rc_context exits; the render warms stats first."""
    import matplotlib
    from research_lab.replication import stats
    seen = {}
    real = V._draw_panel

    def other_thread_meanwhile(*a, **k):
        fig = stats.new_figure(2, 2)                          # what a stats run in another thread would do
        seen["facecolor"] = fig.get_facecolor()
        seen["family"] = list(matplotlib.rcParams["font.family"])
        return real(*a, **k)

    with matplotlib.rc_context():                             # put the test process's rcParams back afterwards
        monkeypatch.setattr(stats, "_fonts_ready", False)
        monkeypatch.setattr(V, "_stats_warm", False)
        monkeypatch.setattr(V, "_draw_panel", other_thread_meanwhile)
        V.table_chart([_item()])
        assert stats._fonts_ready
        wanted = [f for f in stats.CJK_FONTS if f in {x.name for x in matplotlib.font_manager.fontManager.ttflist}]
        assert matplotlib.rcParams["font.sans-serif"] == (wanted or ["DejaVu Sans"])
        assert matplotlib.colors.to_hex(seen["facecolor"]) != V.BG          # no chart style leaks into other figures
        assert seen["family"] == ["sans-serif"]


def test_svg_fonts_end_with_a_generic_family():
    svg = _svg_ok(V.table_chart(_jost()[:24], title="表 3"))
    declared = re.findall(r"font-family: ([^;\"]*)", svg)
    assert declared and all(d.endswith(", sans-serif") for d in declared)
