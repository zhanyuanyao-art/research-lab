"""Card 0157 (docs/design/REPLICATION-v2.md 第一步): a replication plan names its 复现目标 (the numbers the paper reports,
copied as printed with their page, stored apart from the plan and from our results), its data links (public http(s)
only) and its identifying assumptions (待检验). An AI target is kept only when its value is printed on the cited page;
targets count, and the workspace opens, only after Leo reviews them."""
from __future__ import annotations

from pathlib import Path

import pytest

from research_lab.replication import plans

from .test_data import VALID_PLAN, data_client, ingest_quiet  # noqa: F401 - data_client is a fixture


def did_pdf(tmp_path: Path) -> Path:
    import pymupdf
    doc = pymupdf.open()
    for t in [
        "Staggered Reassurance\nA difference-in-differences study\nK. Park\nAbstract\nWe study alliance visits.\n",
        "2. Data and Methods\nWe build a dyad-year panel, 1990-2020, and estimate a staggered difference-in-differences.\n"
        "Replication data: https://doi.org/10.7910/DVN/ABCDEF\nThe design assumes parallel trends before treatment.\n",
        "4. Results\nTable 2: Effect of reassurance visits\n(1) (2)\nVisit 0.231*** 0.198**\n(0.054) (0.071)\nN 1,204 1,180\n"
        "95% CI [0.125, 0.337] [0.059, 0.337]\n",
        "References\nCallaway, B. 2021.\n",
    ]:
        page = doc.new_page()
        page.insert_text((72, 72), t, fontsize=11)
    p = tmp_path / "park2024.pdf"
    doc.save(p)
    doc.close()
    return p


def table_page(conn, paper_id):
    return next(label for label, text in plans.page_texts(conn, paper_id).items() if "Table 2" in text)


def suggest(conn, paper_id, **extra):
    from research_lab.ai import queue
    job_id = queue.enqueue(conn, "methods.replicate", paper_id)
    queue.complete(conn, job_id, {**VALID_PLAN, **extra}, provider="test", model="test")
    return plans.plan_for_job(conn, job_id)


def test_the_request_sends_the_methods_and_the_results_table_pages(conn, tmp_path):
    from research_lab.ai import registry
    paper_id = ingest_quiet(conn, did_pdf(tmp_path))
    req, _ = registry.get("methods.replicate").build(paper_id, conn)
    text = req.messages[-1]["content"]
    assert "<results_pages>" in text and "Visit 0.231***" in text and "staggered difference-in-differences" in text
    assert "Callaway, B. 2021" not in text                                   # references stay out
    assert "copied exactly as printed" in text and "never make up a URL" in text and "待检验" in text


def test_only_targets_printed_on_their_page_are_kept(conn, tmp_path):
    paper_id = ingest_quiet(conn, did_pdf(tmp_path))
    page = table_page(conn, paper_id)
    plan_id = suggest(conn, paper_id, targets=[
        {"label": "Table 2", "column": "(1)", "coefficient": "Visit", "value": 0.231, "se": "(0.054)", "stars": "***",
         "N": "1,204", "page": page},
        {"table": "Table 2", "model": "(2)", "term": "Visit", "value": "0.20", "page": page},       # 0.198 rounded: made up
        {"table": "Table 2", "model": "(2)", "term": "Visit", "value": "0.198", "se": "0.09", "page": page},   # se made up
        {"table": "Table 2", "model": "(1)", "term": "Visit", "value": "0.231", "page": "999"},    # wrong page
        {"table": "Table 3", "term": "Visit"},                                                    # no value, no page
        {"table": "Figure 1", "term": "Visit", "note": "图中只给出方向：处理后上升", "page": page},  # no number: fine
    ], links=[{"url": "https://doi.org/10.7910/DVN/ABCDEF"}, {"url": "http://192.168.1.4/data.zip"}, {"url": "javascript:alert(1)"}],
        assumptions=[{"text": "处理前平行趋势", "status": "成立"}])
    t, figure = plans.targets(conn, plan_id)
    assert (t["tid"], t["table"], t["model"], t["term"], t["value"], t["se"], t["sig"], t["n"], t["page"], t["origin"]) == \
        ("T1", "Table 2", "(1)", "Visit", "0.231", "(0.054)", "***", "1,204", page, "ai-suggested")
    assert figure["tid"] == "T2" and figure["value"] == "" and figure["note"].startswith("图中只给出方向")
    record, direction = plans.target_records(conn, plan_id)                  # the appendix's shape, for R4
    assert record == {"id": "T1", "table": "Table 2", "model": "(1)", "term": "Visit", "value": 0.231, "se": 0.054,
                      "ci": None, "sig": "***", "n": 1204, "page": int(page) if page.isdigit() else page,
                      "value_text": "0.231", "se_text": "(0.054)", "note": ""}
    assert direction["value"] is None and direction["se"] is None and direction["value_text"] is None
    p = plans.get(conn, plan_id)
    assert "targets" not in p["plan"]                                         # never in plan_json
    assert any("找不到它的数值或标准误" in c for c in p["plan"]["caveats"]) and any("缺页码" in c for c in p["plan"]["caveats"])
    assert [l["url"] for l in p["plan"]["links"]] == ["https://doi.org/10.7910/DVN/ABCDEF"]
    assert p["plan"]["links"][0]["kind"] == "dataverse" and any("http(s)" in c for c in p["plan"]["caveats"])
    assert p["plan"]["assumptions"] == [{"text": "处理前平行趋势", "status": "待检验"}]   # the model does not get to say 成立


def test_findings_and_verdicts_are_still_refused():
    from research_lab.ai import queue
    with pytest.raises(Exception):
        queue.validate("methods.replicate", {**VALID_PLAN, "findings": "the effect is robust"})
    with pytest.raises(Exception):
        queue.validate("methods.replicate", {**VALID_PLAN, "targets": [{"value": "0.2", "page": "3", "verdict": "wrong"}]})


def test_leo_edits_reviews_and_then_the_workspace_opens(data_client, conn, tmp_path):
    paper_id = ingest_quiet(conn, did_pdf(tmp_path))
    page = table_page(conn, paper_id)
    plan_id = suggest(conn, paper_id, targets=[{"table": "Table 2", "model": "(1)", "term": "Visit", "value": "0.231", "page": page}])
    conn.commit()
    html = data_client.get(f"/data/plans/{plan_id}/detail").text
    assert "论文报告值" in html and ">Mark targets reviewed</button>" in html
    assert '<span class="btn disabled" aria-disabled="true"' in html and f'href="/replicate/{plan_id}"' not in html
    fields = ("tid", "table", "model", "term", "value", "se", "ci", "sig", "n", "page", "note")
    unchanged = ("T1", "Table 2", "(1)", "Visit", "0.231", "", "", "", "", page, "")
    added = ("", "Table 2", "(2)", "Visit", "0.198", "0.071", "", "**", "1,180", page, "")
    rows = {f"tgt_{k}": [v1, v2, ""] for k, v1, v2 in zip(fields, unchanged, added)}
    r = data_client.post(f"/data/plans/{plan_id}/targets", data=rows, follow_redirects=False)
    assert r.status_code == 303
    first, second = plans.targets(conn, plan_id)
    assert (first["tid"], first["origin"]) == ("T1", "ai-suggested") and (second["tid"], second["origin"]) == ("T2", "user")
    cleared = ("T1", *[""] * 10)                                                # the form deletes a row by emptying it
    rows = {f"tgt_{k}": [v1, v2] for k, v1, v2 in zip(fields, cleared, ("T2", *added[1:]))}
    data_client.post(f"/data/plans/{plan_id}/targets", data=rows, follow_redirects=False)
    assert [t["tid"] for t in plans.targets(conn, plan_id)] == ["T2"]       # T1 deleted; T2 keeps its id
    rows = {f"tgt_{k}": [v1, v2] for k, v1, v2 in zip(fields, ("T2", *added[1:]), ("", "Table 3", "(1)", "Visit", "0.5", "", "", "", "", page, ""))}
    data_client.post(f"/data/plans/{plan_id}/targets", data=rows, follow_redirects=False)
    assert [t["tid"] for t in plans.targets(conn, plan_id)] == ["T2", "T3"]  # never T1 again
    assert not plans.targets_reviewed(plans.get(conn, plan_id))
    data_client.post(f"/data/plans/{plan_id}/targets/review", follow_redirects=False)
    p = plans.get(conn, plan_id)
    assert plans.targets_reviewed(p) and all(t["origin"] == "user" for t in plans.targets(conn, plan_id))
    html = data_client.get(f"/data/plans/{plan_id}/detail").text
    assert f'<a class="btn" href="/replicate/{plan_id}">Open workspace</a>' in html and "复现目标已审阅" in html
    empty = plans.create_blank(conn, paper_id)
    with pytest.raises(plans.PlanError):
        plans.review_targets(conn, empty)
    plans.delete(conn, plan_id)
    assert conn.execute("SELECT COUNT(*) FROM replication_targets WHERE plan_id = ?", (plan_id,)).fetchone()[0] == 0


def test_links_must_be_public_http_and_assumptions_keep_a_status(data_client, conn, tmp_path):
    paper_id = ingest_quiet(conn, did_pdf(tmp_path))
    plan_id = plans.create_blank(conn, paper_id)
    with pytest.raises(plans.PlanError):
        plans.save_links(conn, plan_id, [{"url": "https://osf.io/abcd/"}, {"url": "file:///etc/passwd"}])
    assert plans.get(conn, plan_id)["plan"]["links"] == []                    # nothing half-saved
    plans.save_links(conn, plan_id, [{"url": "https://osf.io/abcd/", "access": "需申请", "note": "注册后下载"},
                                     {"url": "https://github.com/kpark/reassurance", "kind": "github"}, {"url": ""}])
    links = plans.get(conn, plan_id)["plan"]["links"]
    assert [(l["kind"], l["access"]) for l in links] == [("osf", "request"), ("github", "public")]
    plans.save_assumptions(conn, plan_id, [{"text": "交错处理的时点外生", "status": "bogus"}, {"text": ""}])
    assert plans.get(conn, plan_id)["plan"]["assumptions"] == [{"text": "交错处理的时点外生", "status": "待检验"}]
    conn.commit()
    html = data_client.get(f"/data/plans/{plan_id}/detail").text
    assert 'href="https://osf.io/abcd/" target="_blank" rel="noopener noreferrer"' in html
    bad = data_client.post(f"/data/plans/{plan_id}/links", data={"lnk_url": ["javascript:alert(1)"], "lnk_kind": ["other"],
                                                               "lnk_access": ["public"], "lnk_note": [""]}, follow_redirects=False)
    assert bad.status_code == 303 and len(plans.get(conn, plan_id)["plan"]["links"]) == 2


FIELDS = ("tid", *plans.TARGET_FIELDS)


def form(*rows: tuple[str, ...], version: str | None = None) -> dict[str, list[str]]:
    data = {f"tgt_{k}": [r[i] for r in rows] for i, k in enumerate(FIELDS)}
    return {**data, "targets_version": [version]} if version is not None else data


def row(tid: str, term: str, value: str, page: str = "3") -> tuple[str, ...]:
    return (tid, "Table 2", "(1)", term, value, "", "", "", "", page, "")


def test_a_stale_or_forged_save_writes_nothing(data_client, conn, tmp_path):
    """Card 0163: one transaction, a version check (409 when the table changed since the form was drawn), and two
    rows with one id refused (400); either way the stored targets stay exactly as they were."""
    plan_id = plans.create_blank(conn, ingest_quiet(conn, did_pdf(tmp_path)))
    plans.save_targets(conn, plan_id, [dict(zip(FIELDS, row("", "Visit", "0.231"))), dict(zip(FIELDS, row("", "Ally", "0.1")))])
    conn.commit()
    html = data_client.get(f"/data/plans/{plan_id}/detail").text
    drawn = plans.targets_version(conn, plan_id)
    assert f'name="targets_version" value="{drawn}"' in html
    before = plans.targets(conn, plan_id)
    plans.save_targets(conn, plan_id, [dict(zip(FIELDS, row("T1", "Visit", "0.232"))), dict(zip(FIELDS, row("T2", "Ally", "0.1")))])
    conn.commit()                                                              # someone else's edit lands first
    after = plans.targets(conn, plan_id)
    assert plans.targets_version(conn, plan_id) != drawn
    r = data_client.post(f"/data/plans/{plan_id}/targets", data=form(row("T1", "Visit", "0.9"), row("T2", "Ally", "0.1"),
                                                                     version=drawn), follow_redirects=False)
    assert r.status_code == 409 and plans.targets(conn, plan_id) == after
    with pytest.raises(plans.StaleTargets):                                    # a form without the version: rows gone missing
        plans.save_targets(conn, plan_id, [dict(zip(FIELDS, row("T1", "Visit", "0.9")))])
    r = data_client.post(f"/data/plans/{plan_id}/targets", data=form(row("T1", "Visit", "0.9"), row("T1", "Ally", "0.5"),
                                                                     version=plans.targets_version(conn, plan_id)),
                         follow_redirects=False)
    assert r.status_code == 400 and "T1" in r.text and plans.targets(conn, plan_id) == after
    current = plans.targets_version(conn, plan_id)
    r = data_client.post(f"/data/plans/{plan_id}/targets", data=form(row("T1", "Visit", "0.9"), row("T2", "Ally", "0.1"),
                                                                     row("", "Trade", "0.4"), version=current),
                         follow_redirects=False)
    assert r.status_code == 303
    assert [(t["tid"], t["value"], t["origin"]) for t in plans.targets(conn, plan_id)] == \
        [("T1", "0.9", "user"), ("T2", "0.1", "user"), ("T3", "0.4", "user")]
    assert before[0]["value"] == "0.231"


def test_a_save_that_fails_halfway_leaves_the_table_as_it_was(conn, tmp_path, monkeypatch):
    plan_id = plans.create_blank(conn, ingest_quiet(conn, did_pdf(tmp_path)))
    plans.save_targets(conn, plan_id, [dict(zip(FIELDS, row("", "Visit", "0.231")))])
    stored = plans.targets(conn, plan_id)

    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(plans, "_insert_targets", broken)                     # after the DELETE, before the INSERTs
    with pytest.raises(RuntimeError):
        plans.save_targets(conn, plan_id, [dict(zip(FIELDS, row("T1", "Visit", "0.5")))], version=plans.targets_version(conn, plan_id))
    assert plans.targets(conn, plan_id) == stored and not conn.in_transaction


def test_an_interval_or_sample_size_not_on_the_page_is_dropped_with_a_note(conn, tmp_path):
    paper_id = ingest_quiet(conn, did_pdf(tmp_path))
    page = table_page(conn, paper_id)
    plan_id = suggest(conn, paper_id, targets=[
        {"table": "Table 2", "model": "(1)", "term": "Visit", "value": "0.231", "ci": [0.125, 0.337], "n": "1204", "page": page},
        {"table": "Table 2", "model": "(2)", "term": "Visit", "value": "0.198", "ci": "[0.06, 0.34]", "n": "1,190",
         "note": "聚类到二元组", "page": page},                                    # both made up
        {"table": "Table 2", "model": "(2)", "term": "Visit", "value": "0.198", "ci": "0.059\u20130.337", "n": "180",
         "page": page},                                                       # 180 is only part of 1,180
    ])
    printed, made_up, partial = plans.targets(conn, plan_id)
    assert (printed["ci"], printed["n"], printed["note"]) == ("[0.125, 0.337]", "1204", "")
    assert (made_up["ci"], made_up["n"], made_up["value"]) == ("", "", "0.198")
    assert made_up["note"] == f"聚类到二元组；助手给的区间和样本量在第 {page} 页找不到，已去掉"
    assert (partial["ci"], partial["n"]) == ("0.059\u20130.337", "") and partial["note"] == f"助手给的样本量在第 {page} 页找不到，已去掉"
    assert any("2 条复现目标的区间或样本量" in c for c in plans.get(conn, plan_id)["plan"]["caveats"])
    record = plans.target_records(conn, plan_id)[2]
    assert record["ci"] == [0.059, 0.337]


def test_numbers_on_a_page_are_matched_as_printed():
    page = "N = 1,204 countries; 95% CI [\u22120.34, \u22120.12]; 12 450 observations"
    assert plans.printed_count("1204", page) and plans.printed_count("1 204", page) and plans.printed_count("12,450", page)
    assert not plans.printed_count("204", page) and not plans.printed_count("120", page)
    assert plans.printed_interval("[-0.34, -0.12]", page) and not plans.printed_interval("[-0.34, -0.13]", page)
    assert not plans.printed_interval("-0.34", page)                           # one end is not an interval


def test_the_record_keeps_the_printed_decimals(conn, tmp_path):
    """The appendix's value_text / se_text: 0.140 as a number is 0.14, and R4 needs the three decimals printed."""
    plan_id = plans.create_blank(conn, ingest_quiet(conn, did_pdf(tmp_path)))
    plans.save_targets(conn, plan_id, [{"tid": "", "table": "Table 2", "term": "Visit", "value": "0.140", "se": "(0.050)",
                                        "page": "3"}])
    record = plans.target_records(conn, plan_id)[0]
    assert (record["value"], record["value_text"], record["se"], record["se_text"]) == (0.14, "0.140", 0.05, "(0.050)")


@pytest.mark.parametrize("raw, label", [("p. 157", "157"), ("pp. 157–158", "157"), ("Page 157", "157"), ("第157页", "157"),
                                        ("157", "157"), ("p.xii", "xii"), ("  P 9 ", "9")])
def test_a_cited_page_is_reduced_to_the_label_page_texts_uses(raw, label):
    from research_lab.replication.plans import Target
    assert Target(table="Table 2", term="x", value="0.1", page=raw).page == label


@pytest.mark.parametrize("given,text", [
    ("处理前平行趋势", "处理前平行趋势"),                                                     # a bare string
    ({"text": "无预期效应"}, "无预期效应"),
    ({"assumption": "Parallel trends before 2009"}, "Parallel trends before 2009"),
    ({"statement": "No anticipation"}, "No anticipation"),
    ({"description": "Selection of pilot districts is as good as random"}, "Selection of pilot districts is as good as random"),
    ({"label": "SUTVA"}, "SUTVA"),
    ({"name": "Parallel trends", "description": "treated and control provinces moved together before 2009"},
     "Parallel trends：treated and control provinces moved together before 2009"),
    ({"name": "Parallel trends", "description": "Parallel trends"}, "Parallel trends"),
    ({"parallel_trends": "treated and control provinces moved together", "status": "待检验"},
     "treated and control provinces moved together"),                                          # one string in the object
])
def test_assumption_shapes_models_return(given, text):
    a = plans.Assumption.model_validate(given)
    assert a.text == text and a.status == "待检验"


def test_assumptions_without_any_text_are_still_dropped():
    with pytest.raises(Exception):
        plans.Assumption.model_validate({"a": "one", "b": "two"})                                # two strings: ambiguous
    with pytest.raises(Exception):
        plans.Assumption.model_validate({"status": "待检验"})


def test_a_plan_keeps_assumptions_in_any_of_these_shapes():
    r = plans.Result.model_validate({**VALID_PLAN, "assumptions": [
        "Parallel trends", {"name": "No anticipation", "description": "districts did not know in advance"},
        {"statement": "SUTVA"}, {"assumption": "Stable composition"}, {"note": "the only string"}]})
    assert [a.text for a in r.assumptions] == ["Parallel trends", "No anticipation：districts did not know in advance",
                                               "SUTVA", "Stable composition", "the only string"]
    assert not any("识别假设" in c for c in r.caveats)


def test_the_plan_job_leaves_room_for_twenty_targets():
    from research_lab.replication import plans
    assert plans.PLAN_MAX_TOKENS >= 8000
