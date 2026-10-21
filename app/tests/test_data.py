"""数据复现: dataset import (Chinese headers, GBK), statistics, network measures, replication plans,
analysis.read, check linking, artifact export, routes and CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

from research_lab.replication import datasets, export, network, plans, runs, stats, store


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def data_client(repo: Path):
    """TestClient with the data router mounted even before web/app.py lists it in ROUTERS."""
    from fastapi.testclient import TestClient
    from research_lab.web.app import create_app
    from research_lab.web.routes import data as data_routes
    app = create_app()
    if not any(getattr(r, "path", "").startswith("/data") for r in app.routes):
        app.include_router(data_routes.router)
    with TestClient(app, base_url="http://127.0.0.1:4100") as c:
        c.headers["HX-Request"] = "true"
        yield c


def write_cn_csv(path: Path, encoding: str = "gbk") -> Path:
    df = pd.DataFrame({
        "国家": ["中国", "美国", "日本", "韩国", None, "印尼"],
        "年份": [2001, 2002, 2003, 2004, 2005, 2006],
        "出席率": [0.5, 0.75, None, 1.0, 0.25, 0.6],
        "成员": ["是", "否", "是", "是", "否", "是"],
    })
    df.to_csv(path, index=False, encoding=encoding)
    return path


def synthetic_ds(conn, tmp_path: Path, n: int = 400, seed: int = 7) -> str:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({"收入": rng.normal(size=n), "地区": rng.choice(["东部", "中部", "西部"], n),
                       "年份": rng.integers(2000, 2010, n)})
    df["增长"] = 1.5 + 2.0 * df["收入"] + rng.normal(scale=0.5, size=n)
    df["高增长"] = (df["增长"] > 1.5).astype(int)
    df.loc[:9, "收入"] = np.nan          # 10 rows lost to listwise deletion
    p = tmp_path / "synthetic.csv"
    df.to_csv(p, index=False, encoding="utf-8")
    return datasets.import_file(conn, p, name="合成数据")


def ingest_quiet(conn, pdf: Path) -> str:
    from research_lab.intake import pipeline
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as http:
        res = pipeline.ingest(conn, pdf, http=http, ai=False)
    assert res.paper_id
    return res.paper_id


def methods_pdf(tmp_path: Path) -> Path:
    import pymupdf
    doc = pymupdf.open()
    for t in [
        "Bypass and Centrality\nA network account of ASEAN forums\nR. Lim\nAbstract\nWe study forum ties. doi:10.1234/rl.2024.009\n",
        "1. Introduction\nWhy ASEAN stays central is contested.\n",
        "2. Data and Methods\nWe code state-forum membership per year, 1994-2022, from chairman's statements.\n"
        "The dependent variable is betweenness of the ASEAN node; we estimate OLS with year trends.\n",
        "3. Findings\nThe results section.\nReferences\nAcharya, A. 2001.\n",
    ]:
        page = doc.new_page()
        page.insert_text((72, 72), t, fontsize=11)
    p = tmp_path / "lim2024.pdf"
    doc.save(p)
    doc.close()
    return p


VALID_PLAN = {
    "data_sources": [{"name": "主席声明中的成员名单", "access": "公开", "url_or_where": "东盟秘书处网站"}],
    "unit_of_analysis": "国家—论坛—年",
    "period": "1994–2022",
    "variables": [{"role": "DV", "name": "ASEAN 中介中心度", "measure": "标准化中介中心度", "source": "作者计算"},
                  {"role": "iv", "name": "年份趋势", "measure": "年", "source": ""}],
    "sample_rule": "所有东盟主导论坛的成员国—年",
    "model": {"family": "ols", "specification": "betweenness ~ year"},
    "steps": ["收集主席声明", "编码成员关系", "计算中介中心度"],
    "robustness": ["去掉线上会议年份"],
    "feasibility": {"level": "High", "why": "资料公开"},
    "to_obtain": ["作者的编码表"],
    "caveats": ["论文未说明观察员如何编码"],
}


# ---------------------------------------------------------------- datasets

def test_import_csv_with_chinese_headers_gbk(conn, tmp_path):
    src = write_cn_csv(tmp_path / "出席.csv", encoding="gbk")
    ds_id = datasets.import_file(conn, src, paper_id=None)
    assert ds_id.startswith("DS-") and ds_id.endswith("-0001")
    row = conn.execute("SELECT * FROM datasets WHERE id = ?", (ds_id,)).fetchone()
    assert row["rows"] == 6 and row["cols"] == 4 and row["source_kind"] == "csv" and row["name"] == "出席"
    schema = json.loads(row["schema_json"])
    assert schema["encoding"] in ("gb18030", "big5") and [c["name"] for c in schema["columns"]] == ["国家", "年份", "出席率", "成员"]
    # DuckDB table + parquet copy + original file
    assert store.exists(ds_id)
    with store.connect(read_only=True) as con:
        assert con.execute(f'SELECT COUNT(*), COUNT("出席率") FROM {store.table_name(ds_id)}').fetchone() == (6, 5)
    assert store.parquet_path(ds_id).exists()
    assert (store.dataset_dir(ds_id) / "出席.csv").exists()
    prof = json.loads(row["profile_json"])
    cols = {c["name"]: c for c in prof["columns"]}
    assert prof["n"] == 6
    assert cols["出席率"]["missing"] == 1 and cols["出席率"]["missing_pct"] == pytest.approx(16.7)
    assert cols["出席率"]["min"] == 0.25 and cols["出席率"]["max"] == 1.0 and cols["出席率"]["mean"] == pytest.approx(0.62)
    assert cols["年份"]["dtype"] == "integer" and cols["国家"]["dtype"] == "text"
    assert ["是", 4] in cols["成员"]["top"] and cols["国家"]["unique"] == 5
    head, rows = datasets.preview(ds_id)
    assert head[0] == "国家" and rows[0][0] == "中国" and len(rows) == 6

    lost = datasets.rename_cast(conn, ds_id, {"国家": "国名"}, {"年份": "text", "成员": "number"})
    ds = datasets.get(conn, ds_id)
    assert ds["columns"][0] == "国名" and datasets.column_kinds(ds)["年份"] == "text"
    assert lost["成员"] == 6                      # 是/否 cannot become numbers → NULL, reported
    datasets.delete(conn, ds_id)
    assert not store.exists(ds_id) and not store.dataset_dir(ds_id).exists()


def test_xlsx_sheet_select_and_parquet(conn, tmp_path):
    p = tmp_path / "book.xlsx"
    with pd.ExcelWriter(p, engine="openpyxl") as w:
        pd.DataFrame({"a": [1, 2]}).to_excel(w, sheet_name="第一页", index=False)
        pd.DataFrame({"变量": [3, 4, 5]}).to_excel(w, sheet_name="第二页", index=False)
    ds_id = datasets.import_file(conn, p, sheet="第二页")
    ds = datasets.get(conn, ds_id)
    assert ds["rows"] == 3 and ds["columns"] == ["变量"] and ds["schema"]["sheets"] == ["第一页", "第二页"]
    datasets.reimport_sheet(conn, ds_id, "第一页")
    assert datasets.get(conn, ds_id)["columns"] == ["a"]
    # a parquet file round-trips through DuckDB (no pyarrow needed)
    pq_id = datasets.import_file(conn, store.parquet_path(ds_id), original_name="copy.parquet")
    assert datasets.get(conn, pq_id)["source_kind"] == "parquet" and datasets.get(conn, pq_id)["rows"] == 2


# ---------------------------------------------------------------- statistics

def test_ols_recovers_slope_and_code_reproduces(conn, tmp_path, monkeypatch):
    ds_id = synthetic_ds(conn, tmp_path)
    run_id = runs.create(conn, "ols", ds_id, {"y": "增长", "x": ["收入", "地区"], "se": "robust"})
    run = runs.get(conn, run_id)
    assert run["status"] == "done" and run_id.startswith("RUN-")
    s = run["result"]["summary"]
    assert s["n"] == 390 and s["missing_dropped"] == 10 and s["r2"] > 0.9
    coef = next(t for t in run["result"]["tables"] if t["kind"] == "coef")
    assert coef["n"] == 390 and coef["missing"] == 10
    assert coef["columns"][:7] == ["项", "系数", "标准误", "t", "p", "95% CI 下限", "95% CI 上限"]
    slope = next(r for r in coef["rows"] if r[0] == "收入")
    assert slope[1] == pytest.approx(2.0, abs=0.1) and slope[5] < 2.0 < slope[6] and slope[4] < 0.001
    assert any(r[0] == "地区 = 西部" for r in coef["rows"])
    assert runs.figure_file(run) is not None
    # the stored code reproduces the estimate from the parquet copy
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MPLBACKEND", "Agg")
    ns: dict = {}
    exec(compile(run["code"], run_id, "exec"), ns)
    assert float(ns["res"].params['Q("收入")']) == pytest.approx(slope[1], rel=1e-4)
    assert int(ns["res"].nobs) == 390
    # rerun keeps the id and the user's reading
    runs.set_reading(conn, run_id, "收入与增长正相关。")
    assert runs.rerun(conn, run_id) == "done"
    assert runs.get(conn, run_id)["reading"] == "收入与增长正相关。"


def test_crosstab_chi2(conn, tmp_path):
    rows = [("甲", "是")] * 30 + [("甲", "否")] * 10 + [("乙", "是")] * 12 + [("乙", "否")] * 28 + [(None, "是")] * 3
    p = tmp_path / "ct.csv"
    pd.DataFrame(rows, columns=["组别", "结果"]).to_csv(p, index=False)
    ds_id = datasets.import_file(conn, p)
    run = runs.get(conn, runs.create(conn, "crosstab", ds_id, {"row": "组别", "col": "结果"}))
    s = run["result"]["summary"]
    from scipy.stats import chi2_contingency
    expected = chi2_contingency(np.array([[10, 30], [28, 12]]))[0]
    assert s["n"] == 80 and s["missing_dropped"] == 3 and s["df"] == 1
    assert s["chi2"] == pytest.approx(expected, rel=1e-4) and s["p"] < 0.001
    freq = run["result"]["tables"][0]
    assert freq["rows"][-1] == ["合计", 38, 42, 80]


def test_other_methods_run(conn, tmp_path):
    ds_id = synthetic_ds(conn, tmp_path)
    cases = {
        "describe": {"vars": ["收入", "地区"]},
        "correlation": {"vars": ["收入", "增长"], "corr_method": "spearman"},
        "ttest": {"y": "增长", "group": "高增长"},
        "logit": {"y": "高增长", "x": ["收入"]},
        "fe": {"y": "增长", "x": ["收入"], "entity": "地区", "time": "年份", "se": "cluster"},
    }
    for method, params in cases.items():
        run = runs.get(conn, runs.create(conn, method, ds_id, params))
        assert run["status"] == "done", method
        for t in run["result"]["tables"]:
            assert t["n"] is not None and t["missing"] is not None, (method, t["title"])
    fe = runs.list_all(conn)[0]
    assert fe["method"] == "fe" and fe["result"]["summary"]["entities"] == 3
    assert all(not r[0].startswith("C(") for r in fe["result"]["tables"][0]["rows"])
    with pytest.raises(stats.StatsError):
        runs.create(conn, "logit", ds_id, {"y": "地区", "x": ["收入"]})     # three outcome values
    with pytest.raises(stats.StatsError):
        runs.parse_params("ols", {"y": "增长"})                              # no predictor


def test_timeout_guard(monkeypatch, conn, tmp_path):
    import time
    ds_id = synthetic_ds(conn, tmp_path, n=20)
    slow = runs.Method("slow", "slow", lambda df, params, parquet: time.sleep(2), [])
    monkeypatch.setitem(runs.METHODS, "slow", slow)
    with pytest.raises(runs.RunError, match="超过"):
        runs.execute("slow", ds_id, {}, timeout=0.2)


def test_network_betweenness_ordering_years_and_brokerage():
    edges = pd.DataFrame({
        "起点": ["A", "B", "C", "C", "A", "B"],
        "终点": ["B", "C", "D", "E", "B", "C"],
        "年份": [2001, 2001, 2001, 2001, 2002, 2002],
        "组": ["东盟", "东盟", "东盟", "东盟", "东盟", "东盟"],
    })
    out = network.run(edges[edges["年份"] == 2001], {"source": "起点", "target": "终点"})
    nodes = out.tables[1]["rows"]
    btw = {r[0]: r[2] for r in nodes}
    assert [r[0] for r in nodes[:2]] == ["C", "B"]
    assert btw["C"] > btw["B"] > btw["A"] == btw["D"] == btw["E"] == 0
    assert out.summary["n"] == 4 and out.summary["missing_dropped"] == 0
    per_year = network.run(edges, {"source": "起点", "target": "终点", "year": "年份"})
    graph = per_year.tables[0]
    assert [r[0] for r in graph["rows"]] == ["2001", "2002"] and graph["rows"][1][1] == 3
    assert any("随时间" in t["title"] for t in per_year.tables)
    # brokerage: a star with an outside group — the hub between two outsiders is a liaison-free itinerant
    star = pd.DataFrame({"s": ["hub", "hub", "hub"], "t": ["x1", "x2", "a1"], "g": ["东盟", "东盟", "东盟"],
                         "tg": ["外部", "外部", "东盟"]})
    br = network.run(star, {"source": "s", "target": "t", "group": "g", "target_group": "tg"})
    table = next(t for t in br.tables if "Gould" in t["title"])
    hub = next(r for r in table["rows"] if r[0] == "hub")
    roles = dict(zip(network.ROLES, hub[2:7]))
    assert roles["itinerant"] == 2          # x1–hub–x2 both directions: outsiders brokered by an insider
    assert roles["representative"] == roles["gatekeeper"] == 2   # a1 ↔ x1/x2, undirected
    assert network.run(edges, {"source": "起点", "target": "终点"}).summary.get("brokerage") is None
    # the network script embeds the measure functions verbatim
    assert "def brokerage(" in out.code and "def betweenness_centralization(" in out.code


def test_network_run_code_executes(conn, tmp_path, monkeypatch):
    p = tmp_path / "edges.csv"
    pd.DataFrame({"起点": ["A", "B", "C", "C"], "终点": ["B", "C", "D", "E"]}).to_csv(p, index=False)
    ds_id = datasets.import_file(conn, p)
    run = runs.get(conn, runs.create(conn, "network", ds_id, {"source": "起点", "target": "终点"}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MPLBACKEND", "Agg")
    ns: dict = {}
    exec(compile(run["code"], "net", "exec"), ns)
    assert ns["graph"]["nodes"] == 5 and ns["nodes"][0][0] == "C"


# ---------------------------------------------------------------- plans and jobs

def test_methods_replicate_build_from_sample_pdf_and_apply(conn, sample_pdf):
    from research_lab.ai import queue, registry
    paper_id = ingest_quiet(conn, sample_pdf)
    spec = registry.get("methods.replicate")
    req, files = spec.build(paper_id, conn)
    text = req.messages[-1]["content"]
    assert files == [] and "No methods section was detected" in text      # fallback: pages 2..N
    assert "US leader-level attendance" in text and "Acharya, A. 2001" not in text   # references left out
    assert "Simplified Chinese" in text
    mat = plans.methods_material(conn, paper_id)
    assert mat["source"] == "pages" and mat["pieces"][0][0] == "245"         # page label of the second page

    job_id = queue.enqueue(conn, "methods.replicate", paper_id)
    run_id = queue.complete(conn, job_id, VALID_PLAN, provider="test", model="test")
    plan_id = plans.plan_for_job(conn, job_id)
    p = plans.get(conn, plan_id)
    assert p["origin"] == "ai-suggested" and p["status"] == "draft" and p["ai_run_id"] == run_id and p["paper_id"] == paper_id
    assert p["plan"]["model"]["family"] == "OLS" and p["plan"]["variables"][1]["role"] == "IV"
    assert p["plan"]["feasibility"]["level"] == "high"
    assert [s["done"] for s in p["plan"]["steps"]] == [False, False, False]
    # results can never ride along in a plan
    with pytest.raises(Exception):
        queue.validate("methods.replicate", {**VALID_PLAN, "results": "significant"})
    # suggestions don't count: no status move and no export before review
    with pytest.raises(plans.PlanError):
        plans.set_status(conn, plan_id, "in-progress")
    with pytest.raises(export.ExportError):
        export.export_plan(conn, plan_id)
    plans.set_step(conn, plan_id, 0, done=True, note="已下载 1994–2010")
    plans.save_variables(conn, plan_id, [{"role": "DV", "name": "中介中心度", "measure": "", "source": ""}, {"role": "IV", "name": ""}])
    plans.mark_reviewed(conn, plan_id)
    p = plans.get(conn, plan_id)
    assert p["origin"] == "user" and p["status"] == "reviewed" and p["steps_done"] == 1 and len(p["plan"]["variables"]) == 1


def test_methods_material_uses_methods_heading(conn, tmp_path):
    paper_id = ingest_quiet(conn, methods_pdf(tmp_path))
    mat = plans.methods_material(conn, paper_id)
    assert mat["source"] == "sections" and any("Data and Methods" in h for h in mat["headings"])
    joined = " ".join(t for _, t in mat["pieces"])
    assert "state-forum membership" in joined and "Why ASEAN stays central" not in joined


def test_analysis_read_apply_writes_only_ai_reading(conn, tmp_path):
    from research_lab.ai import queue, registry
    ds_id = synthetic_ds(conn, tmp_path, n=120)
    run_id = runs.create(conn, "ols", ds_id, {"y": "增长", "x": ["收入"]})
    runs.set_reading(conn, run_id, "我的解读")
    before = dict(conn.execute("SELECT * FROM analysis_runs WHERE id = ?", (run_id,)).fetchone())
    req, _ = registry.get("analysis.read").build(run_id, conn)
    assert "系数" in req.messages[-1]["content"] and "n=110" in req.messages[-1]["content"]
    job_id = queue.enqueue(conn, "analysis.read", run_id)
    queue.complete(conn, job_id, {"reading": "斜率为正。", "caveats": ["样本小"], "next_checks": ["加控制变量"], "verdict": "x"},
                   provider="test", model="test")
    after = dict(conn.execute("SELECT * FROM analysis_runs WHERE id = ?", (run_id,)).fetchone())
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"ai_reading"}
    ai = json.loads(after["ai_reading"])
    assert ai["reading"] == "斜率为正。" and "verdict" not in ai and after["reading"] == "我的解读"


def test_attach_check_and_export_run(conn, tmp_path, repo):
    from research_lab.files import artifacts
    from research_lab.method import checks
    ds_id = synthetic_ds(conn, tmp_path, n=150)
    run_id = runs.create(conn, "ols", ds_id, {"y": "增长", "x": ["收入"]})
    check_id = checks.create(conn, "asean-centrality", title="收入与增长", prediction="收入越高增长越快")
    with pytest.raises(runs.RunError):
        runs.attach_check(conn, run_id, check_id)          # the user's reading comes first
    runs.set_reading(conn, run_id, "斜率约为 2，区间不含零。")
    runs.attach_check(conn, run_id, check_id)
    k = checks.get(conn, check_id)
    assert k["status"] == "recorded" and k["script_ref"] == run_id and "收入" in k["result_note"] and "95% CI" in k["result_note"]
    assert k["reading"] == "斜率约为 2，区间不含零。"
    conn.execute("UPDATE analysis_runs SET ai_reading = ? WHERE id = ?",
                 (json.dumps({"reading": "AI 的话", "caveats": [], "next_checks": []}, ensure_ascii=False), run_id))
    ref = export.export_run(conn, run_id, "asean-centrality")
    row = conn.execute("SELECT path, type FROM artifacts WHERE ref = ?", (ref,)).fetchone()
    assert row["type"] == "analysis"
    a = artifacts.load(repo / row["path"])
    evidence = artifacts.section_body(a.body, "Evidence")
    assert "| 项 | 系数 |" in evidence and "n = " in evidence and f"/data/runs/{run_id}/figure.png" in evidence
    assert "AI 的话" not in (artifacts.section_body(a.body, "Claim") or "") and "AI 的话" in artifacts.section_body(a.body, "AI notes")
    assert "smf.ols" in artifacts.section_body(a.body, "Reproduction")
    assert not artifacts.counts(a.body) and a.front["script"] == run_id


def test_export_reviewed_plan(conn, repo):
    from research_lab.files import artifacts
    plan_id = plans.create_blank(conn)
    plans.save_fields(conn, plan_id, {"unit_of_analysis": "国家—年", "model_family": "panel-FE"})
    plans.add_step(conn, plan_id, "下载数据")
    ref = export.export_plan(conn, plan_id, "asean-centrality")
    row = conn.execute("SELECT path, type FROM artifacts WHERE ref = ?", (ref,)).fetchone()
    assert row["type"] == "research-design"
    a = artifacts.load(repo / row["path"])
    design = artifacts.section_body(a.body, "Design")
    assert "国家—年" in design and "- [ ] 下载数据" in design and "panel-FE" in design
    assert plans.get(conn, plan_id)["artifact_ref"] == ref


# ---------------------------------------------------------------- web and CLI

def test_routes(data_client, repo, tmp_path, sample_pdf):
    c = data_client
    r = c.get("/data")                     # replications first, datasets one tab over (card 0196, Leo 10-19)
    assert r.status_code == 200 and "还没有复现" in r.text and 'data-testid="replications"' in r.text
    r = c.get("/data?tab=datasets")
    assert r.status_code == 200 and "还没有数据集" in r.text
    src = write_cn_csv(tmp_path / "出席.csv")
    with src.open("rb") as fh:
        r = c.post("/data/datasets", files={"file": ("出席.csv", fh, "text/csv")}, data={"name": "出席数据"})
    assert r.status_code == 200 and "出席数据" in r.text and "出席率" in r.text   # redirected to the dataset page
    ds_id = str(r.url).rsplit("/", 1)[1].split("?")[0]
    assert c.get(f"/data/datasets/{ds_id}").status_code == 200
    r = c.post(f"/data/datasets/{ds_id}/columns", data={"name_0": "国名", "type_1": "integer", "type_0": "text",
                                                        "type_2": "number", "type_3": "text", "name_1": "年份", "name_2": "出席率", "name_3": "成员"})
    assert r.status_code == 200 and "国名" in r.text
    r = c.get(f"/data/runs/new?method=ols&dataset={ds_id}")
    assert r.status_code == 200 and "因变量" in r.text and "出席率" in r.text
    r = c.post("/data/runs", data={"method": "ols", "dataset_id": ds_id, "y": "出席率", "x": ["年份"], "se": "classical"})
    assert r.status_code == 200 and "系数" in r.text and "n = " in r.text
    run_id = str(r.url).rsplit("/", 1)[1]
    assert c.get(f"/data/runs/{run_id}/figure.png").headers["content-type"] == "image/png"
    assert "smf.ols" in c.get(f"/data/runs/{run_id}/code.py").text
    r = c.post("/data/runs", data={"method": "ols", "dataset_id": ds_id, "y": "出席率", "se": "classical"})
    assert "请填写" in r.text                                                     # error re-renders the form
    r = c.post(f"/data/runs/{run_id}/reading", data={"reading": "年份与出席率。"})
    assert r.status_code == 200
    assert c.post(f"/data/runs/{run_id}/rerun").status_code == 200
    r = c.post(f"/data/runs/{run_id}/ai-read")
    assert r.status_code == 200 and "队列中" in c.get(f"/data/runs/{run_id}").text
    # plans
    paper_id = ingest_quiet_via_conn(repo, sample_pdf)
    r = c.get("/data/plans/new")
    assert r.status_code == 200 and paper_id in r.text
    r = c.get(f"/data/plans/new?paper={paper_id}")
    assert r.status_code == 200 and "第 2 页" in r.text
    r = c.post("/data/plans/new", data={"paper_id": paper_id}, headers={"HX-Target": "plan-job"})
    assert r.status_code == 200 and "队列中" in r.text
    job_id = r.text.split("data-job-")[1].split('"')[0]
    assert c.get(f"/data/jobs/{job_id}").status_code == 200
    r = c.post("/data/plans/blank", data={"paper_id": paper_id})
    assert r.status_code == 200 and "设计概要" in r.text
    plan_id = str(r.url).split("/data/plans/", 1)[1].split("/")[0].split("?")[0]
    assert c.post(f"/data/plans/{plan_id}/steps", data={"text": "下载数据"}).status_code == 200
    r = c.post(f"/data/plans/{plan_id}/steps/0", data={"toggle": "1", "done": "1"})
    assert r.status_code == 200 and "checked" in r.text
    r = c.post(f"/data/plans/{plan_id}/variables", data={"var_role": ["DV", "IV"], "var_name": ["出席率", ""],
                                                          "var_measure": ["", ""], "var_source": ["", ""]})
    assert r.status_code == 200 and 'value="出席率"' in r.text
    assert c.post(f"/data/plans/{plan_id}/datasets", data={"dataset_id": ds_id}).status_code == 200
    assert ds_id in c.get(f"/data/plans/{plan_id}/detail").text
    r = c.get(f"/data/datasets/{ds_id}")               # card 0065: plans and analyses hang on their dataset
    assert r.status_code == 200 and f'href="/data/plans/{plan_id}"' in r.text and f'href="/data/runs/{run_id}"' in r.text
    assert c.get("/data/datasets/DS-1999-0001").status_code == 404
    # mutations need HTMX or a same-origin Origin header
    bare = c.post(f"/data/runs/{run_id}/delete", headers={"HX-Request": "false"})
    assert bare.status_code == 403


def ingest_quiet_via_conn(repo: Path, pdf: Path) -> str:
    from research_lab.db.connection import connect, migrate
    conn = connect()
    migrate(conn)
    try:
        return ingest_quiet(conn, pdf)
    finally:
        conn.close()


def test_cli_import_run_list(repo, tmp_path):
    from typer.testing import CliRunner
    from research_lab.cli import app
    runner = CliRunner()
    src = write_cn_csv(tmp_path / "出席.csv", encoding="utf-8")
    r = runner.invoke(app, ["data", "import", str(src), "--name", "出席"])
    assert r.exit_code == 0, r.output
    assert "出席率" in r.output and "6 rows × 4 cols" in r.output
    ds_id = r.output.split()[0]
    r = runner.invoke(app, ["data", "run", "ols", "--dataset", ds_id, "--y", "出席率", "--x", "年份"])
    assert r.exit_code == 0, r.output
    assert "n=5" in r.output and "年份" in r.output
    r = runner.invoke(app, ["data", "list"])
    assert r.exit_code == 0 and ds_id in r.output and "RUN-" in r.output
