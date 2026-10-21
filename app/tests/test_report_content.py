"""Card 0196: the replication report assembled into deck v2 — the study, the data, the methods from the chapters, the
results beside the paper's own tables, the conclusion — from what the app holds, with the model-written words when a
replication.report_text job has left them."""
from __future__ import annotations

import io
import json
import time

from research_lab.replication import deck, report_content as R
from research_lab.replication import workspace as W

from .test_replication_deck import (add_run, data_client, fixture_plan, png_bytes, read, results_json,  # noqa: F401
                                    texts)
from ._timing import SLOW

AI = {"title_zh": "顾问与外交决策", "study_sub": "谁的倾向决定决策", "question_title": "一群人开会听谁的",
      "question": "与会者的倾向怎样变成一个决定",
      "models": [{"tag": "涌现模型", "name": "大家的平均", "claim": "决策反映平均倾向", "measure": "平均鹰派程度"},
                 {"tag": "顾问模型", "name": "谁发言多听谁", "claim": "发言多的顾问影响大", "measure": "顾问鹰派程度"}],
      "findings": ["顾问越鹰派，冲突性决策越多"], "takeaways": ["顾问模型最能解释"],
      "questions": ["发言多就等于影响大吗", "外推到历史人物可靠吗"], "method_usage": {"ols": "冲突减合作的差值"}}


def build(conn, plan_id):
    return R.build(conn, plan_id, deadline=time.monotonic() + 60)


def kinds_titles(d):
    return [(s.kind, s.title) for s in d.slides]


def write_ai(plan_id, data=AI):
    p = deck.exports_dir(plan_id)
    p.mkdir(parents=True, exist_ok=True)
    (p / R.AI_TEXT).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def slide(d, kind, starts=""):
    return next(s for s in d.slides if s.kind == kind and s.title.startswith(starts))


def test_the_parts_run_in_order_and_the_pptx_has_every_slide(conn, fixture_plan):
    add_run(conn, fixture_plan["id"], results_json())
    b = build(conn, fixture_plan["id"])
    sections = [s.title for s in b.deck.slides if s.kind == "section"]
    assert sections[0] == "研究问题与理论" and sections[1] == "数据与测量" and sections[-1] == "复现结果"
    cover = b.deck.slides[0]
    assert cover.kind == "cover" and cover.title.startswith("Advisers and Aggregation")
    assert "Tyler Jost, Joshua D. Kertzer" in cover.meta and "International Organization · 2024" in cover.meta
    made = deck.make(conn, fixture_plan["id"])
    assert len(read(made.path).slides) == made.slides == len(R.load_saved(fixture_plan["id"]).slides)


def test_without_model_words_the_study_comes_from_the_stored_summary(conn, fixture_plan):
    d = build(conn, fixture_plan["id"]).deck
    study = slide(d, "cards", "研究问题")
    bodies = {c.head: c.body for c in study.cards}
    assert bodies["研究问题"] == "顾问是否系统性地影响外交政策？" and bodies["核心论点"] == "顾问的倾向会进入决策。"
    assert "机制" not in bodies                                       # the summary has none: no empty card
    assert not any(s.kind == "questions" for s in d.slides)          # no questions are made up without the model


def test_model_words_replace_the_fallbacks(conn, fixture_plan):
    write_ai(fixture_plan["id"])
    d = build(conn, fixture_plan["id"]).deck
    assert d.slides[0].subtitle == "顾问与外交决策"
    q = slide(d, "cards", "一群人开会听谁的")
    assert [c.tag for c in q.cards] == ["涌现模型", "顾问模型"] and q.lede == "与会者的倾向怎样变成一个决定"
    last = d.slides[-1]
    assert last.kind == "questions" and last.points == AI["questions"] and last.cards[0].points == AI["takeaways"]


def test_a_broken_or_huge_ai_text_is_ignored(conn, fixture_plan):
    p = deck.exports_dir(fixture_plan["id"])
    p.mkdir(parents=True, exist_ok=True)
    (p / R.AI_TEXT).write_text("{not json", encoding="utf-8")
    assert R.load_ai_text(fixture_plan["id"]) == {}
    (p / R.AI_TEXT).write_text(json.dumps({"x": "y" * (R.AI_TEXT_BYTES + 10)}), encoding="utf-8")
    assert R.load_ai_text(fixture_plan["id"]) == {}


def test_a_table_beside_the_paper_with_the_verdict_of_each_number(conn, fixture_plan):
    add_run(conn, fixture_plan["id"], results_json())
    d = build(conn, fixture_plan["id"]).deck
    t3 = slide(d, "compare", "表 3")
    assert t3.columns == ["变量", "模型", "论文", "复现"]
    rows = {(r[0], r[1]): (r[2], r[3], hit) for r, hit in zip(t3.rows, t3.hits)}
    assert rows[("Mean hawkishness", "(1)")] == ("0.231", "0.231", True)
    assert rows[("President hawkishness", "(1)")][2] is False       # 0.1 against −0.900
    assert t3.verdict == "1 / 3 一致" and t3.verdict_ok is False
    assert t3.image in d.assets and t3.src.startswith("正文表 3 · 论文第 2 页")
    overview = slide(d, "table", "复现概览")
    i = next(n for n, r in enumerate(overview.rows) if r[0] == "正文表 3")
    assert overview.rows[i][3] == "1 / 3 一致" and overview.chips[i] == "no"


def test_only_results_appear_in_the_results_part(conn, fixture_plan):
    add_run(conn, fixture_plan["id"], results_json())
    d = build(conn, fixture_plan["id"]).deck
    labels = [s.title.split(" · ")[0] for s in d.slides if s.kind == "compare"]
    assert labels == ["表 3"]          # no script maps the others in this fixture, and only Table 3 has the paper's numbers


def test_a_figure_the_run_drew_sits_beside_the_papers(conn, fixture_plan):
    add_run(conn, fixture_plan["id"], results_json(), {"Figure_7.png": png_bytes(300, 200)})
    made = deck.make(conn, fixture_plan["id"])
    d = R.load_saved(fixture_plan["id"])
    n = next(i for i, s in enumerate(d.slides) if s.kind == "compare" and s.title.startswith("图 7"))
    assert d.slides[n].image2 and d.slides[n].image
    pictures = [sh for sh in read(made.path).slides[n].shapes if sh.shape_type == 13]
    assert len(pictures) == 2


def test_a_result_waiting_for_data_names_the_files(conn, fixture_plan, monkeypatch):
    add_run(conn, fixture_plan["id"], results_json(table3=False))
    from research_lab.replication import missing
    monkeypatch.setattr(missing, "waiting_scripts", lambda conn, plan_id, include=None: {"code/meetingAnalysis.R": ["meetings.dta", "a.csv"]})
    monkeypatch.setattr(deck, "_scripts_of", lambda row: ["meetingAnalysis.R"] if row["label"] == "Table 3" else [])
    t3 = slide(build(conn, fixture_plan["id"]).deck, "compare", "表 3")
    assert t3.verdict == "等数据" and t3.verdict_note == "缺 meetings.dta、a.csv"


def test_a_result_held_up_by_a_missing_r_function_says_so(conn, fixture_plan, monkeypatch):
    run_id = add_run(conn, fixture_plan["id"], results_json(table3=False))
    ws = {"state": "done", "mode": "rrun", "failed": {"code/meetingAnalysis.R": 'Error: could not find function "glmer.nb"'}}
    conn.execute("UPDATE analysis_runs SET workspace_json = ? WHERE id = ?", (json.dumps(ws), run_id))
    monkeypatch.setattr(deck, "_scripts_of", lambda row: ["meetingAnalysis.R"] if row["label"] == "Table 3" else [])
    t3 = slide(build(conn, fixture_plan["id"]).deck, "compare", "表 3")
    assert t3.verdict == "缺依赖" and t3.verdict_note == "缺 glmer.nb"


def test_methods_come_from_the_authors_code_with_their_chapters(conn, fixture_plan):
    code = W.workspace_dir(fixture_plan["id"]) / "author" / "code"
    code.mkdir(parents=True)
    (code / "main.R").write_text('fit = glm(n ~ x + factor(admin), data = d, family = "poisson")\nols = lm(y ~ x, data = d)\n')
    write_ai(fixture_plan["id"])
    d = build(conn, fixture_plan["id"]).deck
    sec = slide(d, "section", "方法")
    assert sec.topics == ["泊松回归", "最小二乘回归", "固定效应与面板数据"] or set(sec.topics) == {"泊松回归", "最小二乘回归", "固定效应与面板数据"}
    pois = slide(d, "method", "泊松回归")
    assert pois.code.startswith("fit = glm(") and pois.code_head == "main.R · 第 1 行" and "μ" in pois.formula
    ols = slide(d, "method", "最小二乘回归")
    assert ols.cards[1].body == "冲突减合作的差值"                    # the model's sentence on how this paper uses it
    checks = slide(d, "table", "泊松回归：")
    assert checks.columns == ["检验", "检查哪条前提", "怎么看"] and len(checks.rows) >= 4


def test_a_plan_without_a_paper_still_makes_a_deck(conn):
    from research_lab.replication import plans
    plan_id = plans.create_blank(conn)
    made = deck.make(conn, plan_id)
    d = R.load_saved(plan_id)
    assert d.slides[0].title == "未关联文献" and not any(s.kind == "compare" for s in d.slides)
    assert made.replicated == 0 and made.pending == 0


def test_the_pictures_of_a_deck_have_a_total_budget(conn, fixture_plan, monkeypatch):
    add_run(conn, fixture_plan["id"], results_json(), {"Figure_7.png": png_bytes(300, 200)})
    full = build(conn, fixture_plan["id"]).deck
    assert len(full.assets) >= 3
    monkeypatch.setattr(deck, "IMAGE_TOTAL_BYTES", 6_000)
    small = build(conn, fixture_plan["id"]).deck
    assert sum(len(i.data) for i in small.assets.values()) <= 6_000
    compares = lambda d: [s.title for s in d.slides if s.kind == "compare"]     # noqa: E731
    assert compares(small) == compares(full)                 # a result page stays; only charts without room are left out


def test_an_unreadable_pdf_leaves_out_the_originals_and_says_so(conn, fixture_plan):
    from research_lab.config import get_paths
    f = conn.execute("SELECT rel_path FROM paper_files WHERE paper_id = ? AND is_primary = 1", (fixture_plan["paper"],)).fetchone()
    (get_paths().data / f["rel_path"]).write_bytes(b"%PDF-1.4 not really")
    add_run(conn, fixture_plan["id"], results_json())
    b = build(conn, fixture_plan["id"])
    assert any("论文 PDF 打不开" in n for n in b.notes)
    assert not slide(b.deck, "compare", "表 3").image and "cover.png" not in b.deck.assets


def test_the_saved_slides_round_trip_for_the_preview(conn, fixture_plan):
    write_ai(fixture_plan["id"])
    add_run(conn, fixture_plan["id"], results_json())
    deck.make(conn, fixture_plan["id"])
    saved = R.load_saved(fixture_plan["id"])
    again = build(conn, fixture_plan["id"]).deck
    assert kinds_titles(saved) == kinds_titles(again) and set(saved.sizes if hasattr(saved, "sizes") else saved.assets) == set(again.assets)
    assert all(img.w > 0 and img.h > 0 for img in saved.assets.values())


def test_control_characters_in_a_title_are_dropped(conn, fixture_plan):
    conn.execute("UPDATE papers SET title = ? WHERE id = ?", ("T\x00itle\x1f of ​ the ‮ paper", fixture_plan["paper"]))
    assert build(conn, fixture_plan["id"]).deck.slides[0].title == "T itle of the paper"


def test_megabyte_inputs_are_cut_and_every_part_finishes_in_time():
    huge = "表 3 " * 250_000
    summary = {k: {"text": huge, "pages": [huge] * 1_000} for k in ("research_question", "argument", "mechanism")}
    plan_json = {"data_sources": [{"name": huge, "url_or_where": huge}] * 50_000, "unit_of_analysis": huge}
    rows = [{"label": huge, "kind": "table", "caption": huge, "page": huge, "source": "main", "group": "main",
             "scripts": [{"path": huge, "have": True}] * 5_000, "outputs": [huge] * 5_000, "ticked": True, "owner": huge}] * 2_000
    ctx = deck.Ctx({"id": "RP-2026-9999", "paper_id": None, "plan": plan_json},
                   {"title": huge, "authors_json": json.dumps([huge] * 5_000), "venue": huge}, summary, rows, True)
    ai = {k: huge for k in ("title_zh", "question", "question_title", "unit")}
    ai.update({"models": [{"name": huge, "claim": huge}] * 10_000, "findings": [huge] * 10_000, "questions": [huge] * 10_000,
               "takeaways": [huge] * 10_000, "sources": [{"name": huge, "what": huge}] * 10_000})
    from research_lab.replication import method_notes
    found = method_notes.detect({"a.R": ("fit = glm(y ~ x, family = poisson)\n" + huge[:200]) * 2_000})
    t = time.perf_counter()
    out = [R.cover(ctx, ai, "")] + R.study(ctx, ai) + R.data(ctx, ai, {huge[:50]: huge}, {}) + R.conclusion(ctx, ai)
    out += R.methods(ctx, ai, found)
    res, _, _ = R.results(ctx, None, R.Assets(None), [])
    assert time.perf_counter() - t < 1.0 * SLOW
    texts_ = [s.title for s in out + res] + [x for s in out + res for c in s.cards for x in (c.head, c.body, *c.points)]
    texts_ += [x for s in out + res for x in s.points + s.meta + [s.lede, s.subtitle]]
    assert max(len(x) for x in texts_) <= 200
    assert len(res) <= R.MAX_RESULT_SLIDES + 4


def test_the_report_text_job_shares_the_paper_opening_and_writes_its_file(conn, fixture_plan):
    from research_lab.ai import registry
    from research_lab.ai.provider import SYSTEM_RULES
    spec = registry.get("replication.report_text")
    req, files = spec.build(fixture_plan["id"], conn)
    assert files == [] and req.messages[0] == {"role": "system", "content": SYSTEM_RULES}
    user = req.messages[1]["content"]
    from research_lab.replication import plans
    assert user.startswith(plans.paper_block(conn, fixture_plan["paper"]))     # the same opening as methods.replicate
    assert "json" in user and '"questions"' in user and req.max_tokens <= 4000
    result = spec.Result.model_validate({**AI, "question": "问" * 500, "models": AI["models"] * 3})
    spec.apply(result, 7, conn, fixture_plan["id"])
    saved = R.load_ai_text(fixture_plan["id"])
    assert saved["ai_run_id"] == 7 and len(saved["question"]) <= 90 and len(saved["models"]) == 4


# ---------------------------------------------------------------- Leo's edits to the deck's words

def _titles_in_pptx(path) -> str:
    return "\n".join(sh.text_frame.text for s in read(path).slides for sh in s.shapes if sh.has_text_frame)


def test_edits_reach_the_preview_and_the_pptx_and_survive_a_remake(conn, fixture_plan):
    plan_id = fixture_plan["id"]
    write_ai(plan_id)
    deck.make(conn, plan_id)
    saved = R.load_saved(plan_id)
    i = next(n for n, s in enumerate(saved.slides) if s.kind == "questions")
    kept = R.save_edits(plan_id, {f"{i}|title": "结论与疑问，改过", f"{i}|points.0": "第一个问题改了",
                                  f"{i}|cards.0.points.0": "结论改了", "0|title": "Cover edited",
                                  f"{i}|nope": "x", "999|title": "x", f"{i}|points.9": "no such point", "x|title": "x"})
    assert kept == 4
    again = R.load_saved(plan_id)
    q = again.slides[i]
    assert (q.title, q.points[0], q.cards[0].points[0]) == ("结论与疑问，改过", "第一个问题改了", "结论改了")
    assert R.load_saved(plan_id, edited=False).slides[i].title == "结论与疑问"     # the slides as made stay as made
    R.rewrite_pptx(plan_id)
    text = _titles_in_pptx(deck.deck_path(plan_id))
    assert "结论与疑问，改过" in text and "第一个问题改了" in text and "Cover edited" in text
    deck.make(conn, plan_id)                                                        # a remake keeps the edits
    assert "第一个问题改了" in _titles_in_pptx(deck.deck_path(plan_id))
    assert R.load_saved(plan_id).slides[i].points[0] == "第一个问题改了"


def test_edits_are_capped_and_never_markup(conn, fixture_plan):
    plan_id = fixture_plan["id"]
    deck.make(conn, plan_id)
    R.save_edits(plan_id, {"0|title": "<script>x</script>" + "长" * 5_000})
    t = R.load_saved(plan_id).slides[0].title
    assert len(t) <= R.EDIT_CHARS and t.startswith("<script>")                       # stored as text; the page escapes it


def test_the_deck_page_edits_over_http(data_client, conn, fixture_plan):
    plan_id = fixture_plan["id"]
    write_ai(plan_id)
    assert data_client.post(f"/data/plans/{plan_id}/deck", follow_redirects=False).status_code == 303
    page = data_client.get(f"/data/plans/{plan_id}/deck").text
    assert 'data-testid="deck-edit"' in page and 'data-edit="0|title"' in page and "deck-edit.js" in page
    url = f"/data/plans/{plan_id}/deck/edits"
    r = data_client.post(url, json={"changes": {"0|title": "新标题"}}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and r.json() == {"saved": 1}
    assert "新标题" in data_client.get(f"/data/plans/{plan_id}/deck").text
    assert "新标题" in _titles_in_pptx(io.BytesIO(data_client.get(f"/data/plans/{plan_id}/deck.pptx").content))
    assert data_client.post(url, content=b"not json", headers={"HX-Request": "true", "Content-Type": "application/json"}).status_code == 400
    assert data_client.post(url, json={"changes": "x"}, headers={"HX-Request": "true"}).status_code == 400
    assert data_client.post(url, json={"changes": {}}, headers={"HX-Request": "", "Origin": "http://evil.example"}).status_code == 403



# ---------------------------------------------------------------- review of card 0196

def test_a_miss_against_unchecked_paper_numbers_says_so(conn, fixture_plan):
    add_run(conn, fixture_plan["id"], results_json())
    conn.execute("UPDATE replication_plans SET targets_reviewed_at = NULL WHERE id = ?", (fixture_plan["id"],))
    t3 = slide(build(conn, fixture_plan["id"]).deck, "compare", "表 3")
    assert t3.verdict == "1 / 3 一致" and t3.verdict_note == "论文的数未核对"
    plans_mod = __import__("research_lab.replication.plans", fromlist=["review_targets"])
    plans_mod.review_targets(conn, fixture_plan["id"])
    assert slide(build(conn, fixture_plan["id"]).deck, "compare", "表 3").verdict_note == ""


def test_the_saved_slides_are_never_written_through_a_link(conn, fixture_plan, tmp_path):
    import os
    deck.make(conn, fixture_plan["id"])
    folder = R.assets_dir(fixture_plan["id"])
    outside = tmp_path / "outside.json"
    outside.write_text("keep")
    (folder / "slides.json").unlink()
    os.symlink(outside, folder / "slides.json")
    deck.make(conn, fixture_plan["id"])
    assert outside.read_text() == "keep" and not (folder / "slides.json").is_symlink()
    os.symlink(outside, folder / R.EDITS)
    R.save_edits(fixture_plan["id"], {"0|title": "x"})
    assert outside.read_text() == "keep" and not (folder / R.EDITS).is_symlink()


def test_a_long_overview_goes_on_pages_and_the_page_joins_them(conn, fixture_plan):
    ctx = deck.Ctx({"id": fixture_plan["id"], "paper_id": None, "plan": {}}, {}, {},
                   [{"label": f"Table {i}", "kind": "table", "caption": f"c{i}", "page": "1", "source": "main", "group": "main",
                     "scripts": [{"path": "a.R", "have": False}], "outputs": [], "ticked": True, "owner": ""} for i in range(24)], True)
    res, _, _ = R.results(ctx, None, R.Assets(None), [])
    pages = [s for s in res if s.kind == "table"]
    assert [p.title for p in pages] == ["复现概览 1/3", "复现概览 2/3", "复现概览 3/3"]
    assert sum(len(p.rows) for p in pages) == 24 and all(len(p.rows) <= R.OVERVIEW_ROWS for p in pages)
    view = R.PageView({}, "t", "", R.Deck("t", [R.Slide("section", title="复现结果")] + res), [])
    assert len(view.overview().rows) == 24


def test_a_missing_paper_picture_is_said_not_left_blank(conn, fixture_plan):
    from research_lab.config import get_paths
    f = conn.execute("SELECT rel_path FROM paper_files WHERE paper_id = ? AND is_primary = 1", (fixture_plan["paper"],)).fetchone()
    (get_paths().data / f["rel_path"]).write_bytes(b"%PDF-1.4 not really")
    add_run(conn, fixture_plan["id"], results_json())
    t3 = slide(build(conn, fixture_plan["id"]).deck, "compare", "表 3")
    assert not t3.image and t3.image_label == "论文原表没有截到"


def test_the_parts_are_numbered_in_order_without_gaps(conn, fixture_plan):
    add_run(conn, fixture_plan["id"], results_json())
    d = build(conn, fixture_plan["id"]).deck                    # no author code: no 方法 part
    nos = [s.no for s in d.slides if s.kind == "section"]
    assert nos == list(R.PARTS[:len(nos)])
    crumbs = [s.crumb.split(" · ")[0] for s in d.slides if s.crumb]
    assert set(crumbs) <= set(R.PARTS[:len(nos) + 1]) and crumbs[-1] == R.PARTS[len(nos)]   # 结论 is the next part


def test_edits_with_odd_indexes_are_dropped_and_a_remake_since_the_page_refuses_them(conn, fixture_plan):
    plan_id = fixture_plan["id"]
    deck.make(conn, plan_id)
    assert R.save_edits(plan_id, {"²|title": "x", "9" * 5_000 + "|title": "x", "-1|title": "x"}) == 0
    at = R.saved_at(plan_id)
    assert at and R.save_edits(plan_id, {"0|title": "ok"}, at) == 1
    deck.make(conn, plan_id)
    try:
        R.save_edits(plan_id, {"0|title": "late"}, at)
        raise AssertionError("a stale page must be refused")
    except R.StaleDeck:
        pass


def test_a_pptx_is_not_rewritten_with_pictures_missing(conn, fixture_plan):
    plan_id = fixture_plan["id"]
    add_run(conn, plan_id, results_json())
    deck.make(conn, plan_id)
    before = deck.deck_path(plan_id).read_bytes()
    for f in R.assets_dir(plan_id).glob("orig-*.png"):
        f.unlink()
    try:
        R.rewrite_pptx(plan_id)
        raise AssertionError("must refuse")
    except deck.DeckError:
        pass
    assert deck.deck_path(plan_id).read_bytes() == before


def test_saved_slides_of_the_wrong_types_still_load(conn, fixture_plan):
    deck.make(conn, fixture_plan["id"])
    path = R.assets_dir(fixture_plan["id"]) / "slides.json"
    raw = json.loads(path.read_text())
    raw["slides"][0].update(title=["not", "text"], meta="not a list", hits={"x": 1}, cards=[{"points": 5, "head": {"a": 1}}])
    raw["sizes"] = {"cover.png": ["a", 2], "x.png": [10 ** 9, 5]}
    path.write_text(json.dumps(raw))
    d = R.load_saved(fixture_plan["id"])
    assert d.slides[0].title == "" and d.slides[0].meta == [] and d.assets == {}
    from research_lab.replication import slides as S
    from research_lab.web.context import templates
    S.render_html(d, lambda n: n, templates().env)
    S.write_pptx(d, io.BytesIO())


def test_the_edits_route_refuses_big_bodies_and_stale_pages(data_client, conn, fixture_plan):
    plan_id = fixture_plan["id"]
    assert data_client.post(f"/data/plans/{plan_id}/deck", follow_redirects=False).status_code == 303
    url = f"/data/plans/{plan_id}/deck/edits"
    big = {"changes": {"0|title": "x" * 300_000}}
    assert data_client.post(url, json=big, headers={"HX-Request": "true"}).status_code == 413
    r = data_client.post(url, json={"changes": {"0|title": "y"}, "at": "an old stamp"}, headers={"HX-Request": "true"})
    assert r.status_code == 409 and "刷新" in r.json()["detail"]
    page = data_client.get(f"/data/plans/{plan_id}/deck").text
    assert f'data-at="{R.saved_at(plan_id)}"' in page


def test_without_a_stored_checklist_the_default_ticks_make_the_deck_and_nothing_is_written(conn, fixture_plan):
    conn.execute("DELETE FROM replication_coverage WHERE plan_id = ?", (fixture_plan["id"],))
    add_run(conn, fixture_plan["id"], results_json())
    b = build(conn, fixture_plan["id"])
    assert any("还没有 Scan" in n for n in b.notes) and slide(b.deck, "compare", "表 3")
    assert conn.execute("SELECT COUNT(*) FROM replication_coverage WHERE plan_id = ?", (fixture_plan["id"],)).fetchone()[0] == 0


def test_with_no_run_the_results_part_lists_what_waits(conn, fixture_plan):
    conn.execute("UPDATE replication_coverage SET scripts_json = ? WHERE plan_id = ? AND label = 'Table 3'",
                 (json.dumps([{"path": "meetingAnalysis.R", "have": False}]), fixture_plan["id"]))
    d = build(conn, fixture_plan["id"]).deck
    overview = next(s for s in d.slides if s.kind == "table" and s.title.startswith("复现概览"))
    assert overview.rows and all(r[3] in ("未复现", "等数据", "缺脚本") for r in overview.rows)
    assert not any(s.kind == "chart" for s in d.slides)
