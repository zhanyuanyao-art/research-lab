"""Card 0160 (docs/design/REPLICATION-v2.md 第四步 and the appendix): the comparison engine lines a run's
outputs/results.json up against the paper's reported targets and says, per target, exact / within / direction /
mismatch / missing, plus a summary with counts, whether N matches and a rule-built paragraph. Pure functions."""
from __future__ import annotations

import copy

import pytest

from research_lab.replication import compare as C


def _results(*tables):
    return {"tables": [{"label": label, "model": model, "estimator": "OLS", "rows": rows, "notes": [],
                        "needs_r": needs_r} for label, model, rows, needs_r in
                       ((*t, False) if len(t) == 3 else t for t in tables)]}


def _row(term="treated_post", estimate=0.139, se=0.050, p=0.02, n=4210, **kw):
    return {"term": term, "estimate": estimate, "se": se, "p": p, "ci": None, "n": n, **kw}


def _one(target, row, *, stars="econ", mapping=None):
    target = {"id": "T1", "table": "Table 2", "model": "(3)", "term": "treated_post", **target}
    out = C.compare([target], _results(("Table 2", "(3)", [row])), mapping, stars=stars)
    return out["items"][0]


# ── one verdict per row ──────────────────────────────────────────────────────────────────────────────────────

VERDICT_CASES = [
    # exact: equal after rounding to the paper's decimals (half up), same band
    ("exact, rounds half up", {"value": "0.142", "se": "0.051", "sig": "**"}, _row(estimate=0.1415, p=0.03), "exact"),
    ("exact at two decimals", {"value": "0.14", "sig": "**"}, _row(estimate=0.139, p=0.03), "exact"),
    ("exact, negative", {"value": "−0.231", "sig": "***"}, _row(estimate=-0.2306, p=0.001), "exact"),
    # within: |diff| ≤ one reported SE, same band
    ("within one reported SE", {"value": 0.142, "se": 0.051, "sig": "**"}, _row(estimate=0.120, p=0.03), "within"),
    ("within, output SE as the fallback", {"value": 0.142, "sig": "**"}, _row(estimate=0.100, se=0.05, p=0.04), "within"),
    ("within, band unknown on the paper", {"value": 0.142, "se": 0.2}, _row(estimate=0.2, se=0.2, p=0.3), "within"),
    # direction: same sign, different size or band
    ("direction, more than one SE away", {"value": 0.142, "se": 0.051, "sig": "**"}, _row(estimate=0.30, p=0.03), "direction"),
    ("direction, same size but a different band", {"value": 0.142, "se": 0.051, "sig": "**"},
     _row(estimate=0.139, p=0.006), "direction"),
    ("direction, exact number but a different band", {"value": "0.142", "sig": "*"}, _row(estimate=0.142, p=0.001), "direction"),
    ("direction, no SE anywhere", {"value": 0.142}, _row(estimate=0.2, se=None, p=None), "direction"),
    # mismatch: opposite sign, or significance appears or disappears
    ("mismatch, opposite sign", {"value": 0.142, "se": 0.051, "sig": "**"}, _row(estimate=-0.01, p=0.9), "mismatch"),
    ("mismatch, significant becomes not", {"value": 0.142, "se": 0.051, "sig": "***"}, _row(estimate=0.12, p=0.4), "mismatch"),
    ("direction, not significant becomes significant with the same sign", {"value": 0.05, "se": 0.05, "sig": "n.s."},
     _row(estimate=0.06, p=0.01), "direction"),
    ("direction, not significant becomes significant at the same number", {"value_text": "0.06", "sig": "n.s."},
     _row(estimate=0.06, p=0.001), "direction"),
    ("mismatch, not significant becomes significant with the opposite sign", {"value": 0.05, "se": 0.05, "sig": "n.s."},
     _row(estimate=-0.2, p=0.01), "mismatch"),
    ("mismatch beats an exact number", {"value": "0.142", "sig": "***"}, _row(estimate=0.142, se=0.2, p=0.48), "mismatch"),
    # missing: the row has no estimate
    ("missing, no estimate in the row", {"value": 0.142}, _row(estimate=None), "missing"),
]


@pytest.mark.parametrize("target,row,verdict", [c[1:] for c in VERDICT_CASES], ids=[c[0] for c in VERDICT_CASES])
def test_each_verdict(target, row, verdict):
    item = _one(target, row)
    assert item["verdict"] == verdict, item["reasons"]
    assert item["reasons"]


def test_no_output_row_is_missing_and_the_numbers_sit_side_by_side():
    out = C.compare([{"id": "T1", "table": "Table 2", "model": "(3)", "term": "treated_post", "value": 0.142,
                      "se": 0.051, "sig": "**", "n": 4210, "page": 17},
                     {"id": "T2", "table": "Table 2", "model": "(3)", "term": "log_gdp", "value": 0.3}],
                    _results(("Table 2", "(3)", [_row(estimate=0.120, p=0.03)])))
    t1, t2 = out["items"]
    assert t1["verdict"] == "within" and t1["via"] == "auto" and t1["page"] == 17
    assert t1["paper"]["value"] == 0.142 and t1["paper"]["se"] == 0.051 and t1["paper"]["band"] == 2
    assert t1["ours"]["estimate"] == 0.120 and t1["ours"]["sig"] == "**" and t1["ours"]["n"] == 4210
    assert t1["diff"] == pytest.approx(-0.022) and t1["diff_se"] == pytest.approx(0.022 / 0.051)
    assert t2["verdict"] == "missing" and t2["ours"] is None and t2["matched"] is None and "log_gdp" in t2["reasons"][0]


# ── decimals ─────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,number,decimals", [
    ("0.140", 0.14, 3), ("0.14", 0.14, 2), ("−0.231", -0.231, 3), ("–0.5", -0.5, 1), ("1,204", 1204.0, 0),
    ("(0.051)", 0.051, 3), ("12.5", 12.5, 1), ("1.20e-3", 0.0012, 5), ("abc", None, None), ("", None, None),
])
def test_printed_numbers_keep_their_decimals(text, number, decimals):
    got = C.printed_number(text)
    assert (got is None and number is None) or got == (pytest.approx(number), decimals)


@pytest.mark.parametrize("target,estimate,verdict", [
    ({"value": 0.14, "value_text": "0.14"}, 0.1352, "exact"),       # the paper prints 0.14
    ({"value": 0.14, "se": 0.05}, 0.1352, "within"),                # only the number: decimals unknown, never exact
    ({"value": 0.14}, 0.1352, "direction"),                         # … and without any SE not even within
    ({"value": 0.14, "se": 0.05}, 0.14, "within"),                  # not even when the numbers are equal
    ({"value_text": "0.14"}, 0.1352, "exact"),                      # value_text alone carries the number
    ({"value": "0.14"}, 0.139, "exact"),                            # card 0157 keeps value itself as printed text
    ({"value": 0.14, "value_text": "0.140"}, 0.1396, "exact"),     # trailing zero counts
    ({"value": 0.14, "value_text": "0.140"}, 0.1394, "direction"),
    ({"value_text": "12.5"}, 12.46, "exact"),
])
def test_decimals_come_only_from_printed_text(target, estimate, verdict):
    item = _one(target, _row(estimate=estimate, se=None, p=None))
    assert item["verdict"] == verdict, item["reasons"]
    assert (item["paper"]["decimals"] is None) is ("value_text" not in target and not isinstance(target["value"], str))


def test_se_text_is_read_when_the_se_number_is_absent():
    item = _one({"value_text": "0.142", "se_text": "(0.051)"}, _row(estimate=0.120, p=0.004))   # z 2.8: *** too
    assert item["paper"]["se"] == 0.051 and item["paper"]["se_decimals"] == 3 and item["verdict"] == "within"
    item = _one({"value_text": "0.142", "se_text": "[0.04, 0.24]"}, _row(estimate=0.142, p=0.004))
    assert item["paper"]["se_from"] == "ci" and item["verdict"] == "exact"


def test_stars_printed_after_the_value_count_as_its_significance():
    item = _one({"value": "0.142***"}, _row(estimate=0.142, p=0.3))
    assert item["paper"]["value"] == 0.142 and item["paper"]["band"] == 3 and item["verdict"] == "mismatch"


# ── significance ─────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,econ,apa", [
    ("***", 3, 3), ("**", 2, 2), ("*", 1, 1), ("＊＊", 2, 2), ("", None, None), (None, None, None),
    ("n.s.", 0, 0), ("ns", 0, 0), ("不显著", 0, 0),
    ("p<0.05", 2, 1), ("p < .01", 3, 2), ("p≤0.001", 3, 3), ("p = 0.032", 2, 1), ("p > 0.1", 0, 0),
    ("0.032", 2, 1), (0.2, 0, 0), ("†", 1, 0), ("** (p<0.05)", 2, 1),
])
def test_significance_from_stars_and_p_thresholds(text, econ, apa):
    assert C.sig_band(text) == econ
    assert C.sig_band(text, "apa") == apa


def test_the_star_convention_is_configurable():
    target, row = {"value": 0.142, "se": 0.051, "sig": "*"}, _row(estimate=0.130, p=0.03)
    assert _one(target, row)["verdict"] == "direction"           # econ: p .03 is **, the paper printed *
    assert _one(target, row, stars="apa")["verdict"] == "within"  # apa: p .03 is *
    assert C.p_band(0.03, (0.2, 0.1)) == 2 and C.thresholds([0.01, 0.1, 0.05]) == (0.1, 0.05, 0.01)
    with pytest.raises(ValueError):
        C.thresholds("strict-ish")


def test_the_output_band_falls_back_to_the_estimate_over_its_se():
    item = _one({"value_text": "0.142", "se": 0.051, "sig": "***"}, _row(estimate=0.142, se=0.04, p=None))
    assert item["ours"]["band"] == 3 and item["verdict"] == "exact"
    item = _one({"value": "0.142", "uncertainty": "(0.051)"}, _row(estimate=0.142, p=0.003))
    assert item["paper"]["band_from"] == "z" and item["paper"]["band"] == 3 and item["verdict"] == "exact"


# ── matching ─────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("treat×post", "treated_post"), ("treated_post", "did"), ("DiD", "Post:Treated"), ("treat x post", "treatment * post"),
    ("_cons", "(Intercept)"), ("const", "Constant"), ("log(GDP)", "ln_gdp"), ("ln gdp", "log GDP"), ("Trade Share", "trade_share"),
])
def test_term_aliases(a, b):
    assert C.norm_term(a) == C.norm_term(b)


@pytest.mark.parametrize("a,b", [("treat", "post"), ("gdp", "log(gdp)"), ("treat×post", "treat×post×high")])
def test_different_terms_stay_different(a, b):
    assert C.norm_term(a) != C.norm_term(b)


@pytest.mark.parametrize("a,b,same", [
    ("(3)", "Model 3", True), ("(3)", "3", True), ("M3", "Column (3)", True), ("(iii)", "3", True), ("第3列", "(3)", True),
    ("(3)", "(4)", False), ("Model 3", "Model 3a", False), ("mixed", "", False),
])
def test_model_spellings(a, b, same):
    assert (C.norm_model(a) == C.norm_model(b)) is same


@pytest.mark.parametrize("a,b,same", [
    ("Table 2", "table  2", True), ("Table 2", "TABLE 2.", True), ("Table 2", "表 2", True), ("Table 2", "Table II", True),
    ("Table 2", "Tab. 2", True), ("Table 2", "Table 2A", False), ("Table 2", "Table 3", False), ("Figure 3", "Fig. 3", True),
])
def test_table_spellings(a, b, same):
    assert (C.norm_table(a) == C.norm_table(b)) is same


def test_spelling_differences_still_match():
    out = C.compare([{"id": "T1", "table": "table 2", "model": "Model 3", "term": "treat×post", "value": "0.139"}],
                    _results(("Table 2", "(3)", [_row(term="did")])))
    assert out["items"][0]["verdict"] == "exact" and out["items"][0]["matched"] == {"table": "Table 2", "model": "(3)", "term": "did"}


def test_never_matches_across_tables():
    out = C.compare([{"id": "T1", "table": "Table 2", "model": "(3)", "term": "treated_post", "value": 0.139}],
                    _results(("Table 3", "(3)", [_row()]), ("Table 2", "(3)", [_row(term="other")])))
    item = out["items"][0]
    assert item["verdict"] == "missing" and item["matched"] is None


def test_an_ambiguous_match_is_missing_and_asks_for_a_mapping():
    out = C.compare([{"id": "T1", "table": "Table 2", "term": "treated_post", "value": 0.139}],
                    _results(("Table 2", "(1)", [_row()]), ("Table 2", "(2)", [_row(estimate=0.2)])))
    item = out["items"][0]
    assert item["verdict"] == "missing" and "mapping.json" in item["reasons"][0]


def test_a_row_without_a_model_stands_in_only_when_its_table_has_one_model():
    target = {"id": "T1", "table": "Table 2", "model": "(3)", "term": "treated_post", "value": "0.139"}
    one = C.compare([target], _results(("Table 2", None, [_row()])))["items"][0]
    assert one["verdict"] == "exact"
    several = C.compare([target], _results(("Table 2", None, [_row()]), ("Table 2", "(1)", [_row(estimate=0.3)])))
    item = several["items"][0]
    assert item["verdict"] == "missing" and "不止一个模型" in item["reasons"][0] and "mapping.json" in item["reasons"][0]


def test_a_mapping_that_hits_several_rows_is_missing():
    results = _results(("Table 2", "(1)", [_row()]), ("Table 2", "(2)", [_row(estimate=0.3)]))
    item = C.compare([{"id": "T1", "table": "Table 2", "model": "(1)", "term": "treated_post", "value": "0.139"}],
                     results, {"T1": ["Table 2", None, "treated_post"]})["items"][0]
    assert item["verdict"] == "missing" and item["matched"] is None and "命中 2 行" in item["reasons"][0]


def test_mapping_overrides_the_automatic_match():
    targets = [{"id": "T1", "table": "Table 2", "model": "(3)", "term": "treated_post", "value": "0.142"},
               {"id": "T2", "table": "Table 2", "model": "(3)", "term": "post", "value": "0.010"},
               {"id": "T3", "table": "Table 2", "model": "(3)", "term": "treated_post", "value": "0.142"},
               {"id": "T4", "table": "Table 2", "model": "(3)", "term": "treated_post", "value": "0.142"}]
    results = _results(("Table 2", "(3)", [_row(estimate=-0.5, p=0.001), _row(term="post", estimate=0.0101, p=0.5)]),
                       ("Table 2 main", "(3)", [_row(term="treat_post_alt", estimate=0.1423, p=0.03)]))
    mapping = {"T1": ["Table 2 main", "(3)", "treat_post_alt"], "T3": ["Table 9", "(1)", "x"], "T4": None}
    t1, t2, t3, t4 = C.compare(targets, results, mapping)["items"]
    assert t1["verdict"] == "exact" and t1["via"] == "mapping" and t1["matched"]["table"] == "Table 2 main"
    assert C.compare(targets[:1], results)["items"][0]["verdict"] == "mismatch"    # what auto would have said
    assert t2["verdict"] == "exact" and t2["via"] == "auto"                         # not in the mapping: auto
    assert t3["verdict"] == "missing" and "mapping.json" in t3["reasons"][0]        # never falls back to auto
    assert t4["verdict"] == "missing" and "没有对应输出" in t4["reasons"][0]


def test_a_table_that_needs_r_is_named_in_the_reason():
    out = C.compare([{"id": "T1", "table": "Table 4", "model": "(1)", "term": "att", "value": 0.2}],
                    _results(("Table 4", "(1)", [], True)))
    assert "需要 R" in out["items"][0]["reasons"][-1] and "需要 R" in out["summary"]["text"]


# ── direction-only targets ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("target,estimate,verdict", [
    ({"value": None, "direction": "+"}, 0.3, "direction"),
    ({"value": None, "direction": "+"}, -0.3, "mismatch"),
    ({"value": "负"}, -0.01, "direction"),                        # card 0157 keeps value as printed text
    ({"value": "negative", "sig": "***"}, -0.01, "direction"),    # sign only: the band is not compared
    ({"value": None, "ci": [0.1, 0.4]}, 0.2, "direction"),        # a CI on one side of zero gives the sign
    ({"value": None}, 0.2, "missing"),                             # no number and no direction: nothing to compare
])
def test_null_value_targets_compare_by_sign_only(target, estimate, verdict):
    item = _one(target, _row(estimate=estimate, p=0.6))
    assert item["verdict"] == verdict, item["reasons"]
    assert item["paper"]["value"] is None


# ── sample size and the summary ──────────────────────────────────────────────────────────────────────────────

def test_an_n_mismatch_shows_per_target_and_in_the_summary():
    out = C.compare([{"id": "T1", "table": "Table 2", "model": "(3)", "term": "treated_post", "value": "0.139", "n": "4,210"},
                     {"id": "T2", "table": "Table 2", "model": "(3)", "term": "post", "value": "0.010", "n": "N = 4210"}],
                    _results(("Table 2", "(3)", [_row(n=4198), _row(term="post", estimate=0.01, p=0.5, n=4210)])))
    t1, t2 = out["items"]
    assert t1["verdict"] == "exact" and t1["n_match"] is False and t2["n_match"] is True
    s = out["summary"]
    assert s["n_match"] is False and s["n_diffs"] == [{"id": "T1", "paper": 4210, "ours": 4198}]
    assert "样本量不一致：T1 论文 4210、复现 4198" in s["text"]


def test_the_table_n_stands_in_for_a_missing_row_n():
    results = _results(("Table 2", "(3)", [_row(n=None)]))
    results["tables"][0]["n"] = 4210
    out = C.compare([{"id": "T1", "table": "Table 2", "model": "(3)", "term": "treated_post", "value": "0.139", "n": 4210}], results)
    assert out["items"][0]["n_match"] is True and out["summary"]["n_match"] is True


def test_the_summary_counts_every_verdict_and_reads_as_a_paragraph():
    targets = [{"id": f"T{i}", "table": "Table 2", "model": "(3)", "term": term, "value": value, "se": 0.05, "sig": "**"}
               for i, (term, value) in enumerate([("a", "0.139"), ("b", "0.150"), ("c", "0.400"), ("d", "-0.2"), ("e", "0.1")], 1)]
    rows = [_row(term="a"), _row(term="b"), _row(term="c"), _row(term="d")]
    s = C.compare(targets, _results(("Table 2", "(3)", rows)))["summary"]
    assert s["counts"] == {"exact": 1, "within": 1, "direction": 1, "mismatch": 1, "missing": 1} and s["total"] == 5
    assert s["text"].startswith("共 5 项复现目标：1 项完全一致，1 项在误差范围内，1 项方向一致但大小或显著性不同，1 项对不上，1 项没找到对应输出。")
    assert "对不上的是 T4" in s["text"] and "没找到输出的是 T5" in s["text"] and "论文错" not in s["text"]
    assert s["n_match"] is None and "没有能对照的样本量" in s["text"] and s["stars"] == [0.10, 0.05, 0.01]


@pytest.mark.parametrize("targets,rows,phrase", [
    ([("a", "0.139")], [_row(term="a")], "全部完全一致"),
    ([("a", "0.139"), ("b", "0.150")], [_row(term="a"), _row(term="b")], "全部在误差范围内复现出来了"),
    ([("a", "0.400")], [_row(term="a")], "方向全部一致"),
    ([("a", "0.139")], [], "输出里没有找到任何对应的结果"),
    ([], [], "没有复现目标"),
])
def test_the_overall_sentence_follows_rules(targets, rows, phrase):
    ts = [{"table": "Table 2", "model": "(3)", "term": t, "value": v, "se": 0.05, "sig": "**"} for t, v in targets]
    assert phrase in C.compare(ts, _results(("Table 2", "(3)", rows)))["summary"]["text"]


# ── input shapes ─────────────────────────────────────────────────────────────────────────────────────────────

def test_card_0157_text_targets_work_as_stored():
    targets = [{"id": 7, "label": "表 2", "model": "(1)", "term": "Visit", "value": "0.231", "uncertainty": "(0.054)",
                "significance": "***", "n": "1,204", "page": "12"},
               {"id": 8, "label": "表 2", "model": "(2)", "term": "Visit", "value": "0.198", "uncertainty": "[0.06, 0.34]",
                "significance": "p < 0.05", "n": "1,180", "page": "12"}]
    results = _results(("Table 2", "Model 1", [_row(term="visit", estimate=0.2312, se=0.054, p=0.0001, n=1204)]),
                       ("Table 2", "Model 2", [_row(term="visit", estimate=0.21, se=0.07, p=0.003, n=1180)]))
    a, b = C.compare(targets, results)["items"]
    assert a["id"] == "7" and a["verdict"] == "exact" and a["paper"]["se"] == 0.054 and a["n_match"] is True
    assert b["paper"]["se_from"] == "ci" and b["paper"]["se"] == pytest.approx(0.28 / (2 * C.Z95))
    assert b["verdict"] == "direction"           # within one SE, but p .003 is *** and the paper printed p < .05


def test_ids_default_to_list_order_and_inputs_are_left_alone():
    targets = [{"table": "Table 2", "model": "(3)", "term": "treated_post", "value": 0.139}]
    results = _results(("Table 2", "(3)", [_row()]))
    before = copy.deepcopy((targets, results))
    out = C.compare(targets, results)
    assert out["items"][0]["id"] == "T1" and (targets, results) == before


@pytest.mark.parametrize("results", [None, {}, {"tables": []}])
def test_no_results_means_everything_is_missing(results):
    out = C.compare([{"table": "Table 2", "term": "x", "value": 1}], results)
    assert out["summary"]["counts"]["missing"] == 1 and out["summary"]["n_match"] is None


def test_nan_and_infinity_in_results_json_read_as_absent():
    item = _one({"value": 0.142, "n": 10}, _row(estimate=float("inf"), se=float("nan"), p=float("nan"), n=float("inf")))
    assert item["verdict"] == "missing" and item["ours"]["n"] is None
