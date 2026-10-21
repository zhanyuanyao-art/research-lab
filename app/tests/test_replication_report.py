"""Card 0162 (docs/design/REPLICATION-v2.md 第五步): the 复现报告 is Markdown built by rules from the plan, its targets,
the runs and card 0160's comparison, saved as a draft 研究档案 with the plan's direction. No AI."""
from __future__ import annotations

from research_lab.replication import plans, report, runs

from .test_data import synthetic_ds

SECTIONS = ["## 方案摘要", "## 数据来源", "## 复现结果与对照表", "## 对不上的地方与可能原因", "## 扩展", "## 代码附录"]

PLAN = {"id": "RP-2026-0007", "paper_id": "P-2026-0101", "paper_title": "Staggered Reassurance", "direction": "alliance-us",
        "status": "in-progress", "targets_reviewed_at": "2026-10-13T10:00:00",
        "plan": {"unit_of_analysis": "国家—年", "period": "1990—2020", "sample_rule": "排除内战年份",
                 "model": {"family": "panel-FE", "specification": "y ~ treat×post + 国家、年份固定效应"},
                 "feasibility": {"level": "high", "why": "数据公开"},
                 "data_sources": [{"name": "Visits dataset", "access": "公开", "url_or_where": "Dataverse"}],
                 "links": [{"url": "https://doi.org/10.7910/DVN/ABCDEF", "kind": "dataverse", "access": "public", "note": ""}],
                 "assumptions": [{"text": "处理前平行趋势", "status": "待检验"}], "datasets": ["DS-2026-0003"]}}

TARGETS = [
    {"id": "T1", "table": "Table 2", "model": "(3)", "term": "treated_post", "value": 0.14, "se": 0.051, "ci": None,
     "sig": "**", "n": 4210, "page": 17, "value_text": "0.140", "se_text": "(0.051)", "note": ""},
    {"id": "T2", "table": "Table 2", "model": "(4)", "term": "treated_post", "value": 0.2, "se": 0.06, "ci": None,
     "sig": "***", "n": 4210, "page": 17, "value_text": "0.20", "se_text": "(0.06)", "note": ""},
    {"id": "T3", "table": "Table 4", "model": "", "term": "double_did", "value": 0.31, "se": 0.1, "ci": None,
     "sig": "**", "n": None, "page": 22, "value_text": "0.31", "se_text": "(0.10)", "note": ""},
]


def item(tid, verdict, ours=None, paper=None, reasons=(), n_match=None, diff=None, diff_se=None):
    t = next(x for x in TARGETS if x["id"] == tid)
    return {"id": tid, "table": t["table"], "model": t["model"], "term": t["term"], "page": t["page"], "verdict": verdict,
            "paper": {"value": t["value"], "se": t["se"], "band": 2, "n": t["n"], **(paper or {})}, "ours": ours,
            "diff": diff, "diff_se": diff_se, "n_match": n_match, "reasons": list(reasons)}


COMPARISON = {"items": [
    item("T1", "exact", {"estimate": 0.1402, "se": 0.05, "p": 0.006, "band": 2, "n": 4210}, reasons=["四舍五入到 3 位小数后相同"],
         n_match=True, diff=0.0002, diff_se=0.004),
    item("T2", "mismatch", {"estimate": -0.05, "se": 0.2, "p": 0.8, "band": 0, "n": 3900},
         reasons=["符号相反：论文 0.2，复现 -0.05"], n_match=False, diff=-0.25, diff_se=4.2),
    item("T3", "missing", None, reasons=["输出里没有 Table 4", "这张表需要 R，还没跑"]),
], "summary": {"text": "共 3 项复现目标：1 项完全一致，1 项对不上，1 项没找到对应输出。", "counts": {}}}

LONG = "\n".join(f"x{i} = {i}" for i in range(45))
RUNS = [
    {"id": "RUN-2026-0031", "label": "复现工作区", "extension": False, "needs_r": ["Table 4：需要 R（DIDdesign）"],
     "code": [{"name": "scripts/clean.py", "text": "import pandas as pd\n## not a heading\ndf = pd.read_csv('data.csv')"},
              {"name": "scripts/model.py", "text": LONG}],
     "tables": [{"label": "Table 2", "model": "(3)", "estimator": "TWFE DID",
                 "rows": [{"term": "treated_post", "estimate": 0.1402, "se": 0.05, "p": 0.006, "ci": [0.042, 0.238], "n": 4210}],
                 "notes": ["clustered by country"]}],
     "figures": ["outputs/figures/event_study.png"], "reading": ""},
    {"id": "RUN-2026-0035", "label": "平行趋势检验", "extension": True, "needs_r": [],
     "code": [{"name": "scripts/pretrends.py", "text": "print('leads')"}],
     "tables": [{"label": "Pre-trends", "rows": [{"term": "lead_2", "estimate": 0.01, "se": 0.03, "p": 0.74}]}],
     "figures": [], "reading": "处理前两期系数接近零。"},
]


def test_the_report_has_every_section_in_order_with_verdicts_hints_and_code():
    md = report.build(PLAN, TARGETS, RUNS, COMPARISON)
    assert md.startswith("# 复现报告：Staggered Reassurance") and "没有调用 AI" in md
    at = [md.index(s) for s in SECTIONS]
    assert at == sorted(at)
    summary, sources, results, gaps, ext, code = (md[a:b] for a, b in zip(at, [*at[1:], len(md)]))
    assert "RP-2026-0007（进行中）" in summary and "国家—年" in summary and "panel-FE" in summary and "处理前平行趋势（待检验）" in summary
    assert "3 条" in summary and "已审阅" in summary
    assert "Visits dataset" in sources and "<https://doi.org/10.7910/DVN/ABCDEF>" in sources and "DS-2026-0003" in sources
    assert "1 项完全一致" in results
    assert "| T1 | Table 2 | (3) | treated_post | 0.140 (0.051) \\*\\* | 0.1402 (0.05)\\*\\* |" in results   # printed decimals kept
    assert "| 4210 / 4210 | 完全一致 |" in results and "| 4210 / 3900 | 对不上 |" in results and "没找到对应输出" in results
    assert "### 运行 RUN-2026-0031 的输出" in results and "TWFE DID" in results and "[0.042, 0.238]" in results
    assert "clustered by country" in results and "`outputs/figures/event_study.png`" in results
    assert "RUN-2026-0035" not in results                                            # the extension has its own section
    assert "**T1**" not in gaps                                                       # an exact match is not a gap
    assert "**T2**（Table 2 (4) treated_post）：对不上。符号相反" in gaps and "可能原因：变量的编码方向" in gaps
    assert "样本不同（论文 4210、复现 3900）" in gaps and "标准误相差较大（论文 0.06、复现 0.2）" in gaps and "稳健标准误" in gaps
    assert "**T3**" in gaps and "等装了 R 再补" in gaps and "Table 4：需要 R（DIDdesign）" in gaps and "不是结论" in gaps
    assert "### RUN-2026-0035（平行趋势检验）" in ext and "lead_2" in ext and "处理前两期系数接近零" in ext
    assert code.index("### RUN-2026-0031") < code.index("### RUN-2026-0035（扩展）")
    assert "[RUN-2026-0031](/data/runs/RUN-2026-0031)" in code and "**scripts/clean.py**（共 3 行）" in code
    assert "**scripts/model.py**（共 45 行，节选前 30 行）" in code and "x29 = 29" in code and "x30 = 30" not in code
    assert "尚未对照" not in md


def test_without_a_comparison_every_target_is_not_yet_compared():
    md = report.build({"id": "RP-2026-0008", "status": "draft", "plan": {}}, TARGETS[:2], [], None)
    results = md[md.index("## 复现结果与对照表"):md.index("## 对不上的地方与可能原因")]
    assert results.count("| 尚未对照 |") == 2 and "| 0.20 (0.06) \\*\\*\\* | — | — | 4210 / — |" in results
    gaps = md[md.index("## 对不上的地方与可能原因"):md.index("## 扩展")]
    assert "尚未对照。" in gaps
    assert "还没有扩展运行。" in md and "还没有运行记录。" in md and "方案里还没有写数据来源。" in md
    assert "还没审阅" in md
    clean = report.build(PLAN, TARGETS[:1], [], {"items": COMPARISON["items"][:1], "summary": {"text": "全部完全一致。"}})
    assert "没有对不上的项。" in clean and "尚未对照" not in clean


def test_text_from_papers_and_code_cannot_break_out():
    plan = {**PLAN, "paper_title": "<script>alert(1)</script> [x](javascript:alert(1))"}
    run = {**RUNS[0], "code": [{"name": "scripts/a.py", "text": 'doc = """\n```\n"""'}], "tables": [], "figures": []}
    md = report.build(plan, [], [run], None)
    assert "<script>" not in md and "&lt;script&gt;" in md and "\\[x\\](javascript" in md
    assert "````python\ndoc = \"\"\"\n```\n\"\"\"\n````" in md                       # a longer fence around a fence
    demoted = report._demote("## 方案摘要\ntext\n```python\n## not a heading\n```\n### 运行")
    assert demoted == "### 方案摘要\ntext\n```python\n## not a heading\n```\n#### 运行"


def test_run_view_reads_a_method_run_and_a_workspace_run():
    method = {"id": "RUN-2026-0001", "method": "ols", "method_label": "线性回归", "code": "import statsmodels",
              "figure_path": "figures/RUN-2026-0001.png", "reading": "斜率为正",
              "result": {"params_label": "y = 增长", "tables": [{"title": "系数", "columns": ["项", "系数"], "rows": [["收入", 2.0]], "n": 150}]}}
    v = report.run_view(method)
    assert v["label"] == "线性回归 · y = 增长" and v["code"] == [{"name": "RUN-2026-0001.py", "text": "import statsmodels"}]
    assert v["figures"] == ["/data/runs/RUN-2026-0001/figure.png"] and not v["extension"]
    md = report.build({"id": "RP-1", "plan": {}}, [], [v], None)
    assert "**系数**（n = 150）" in md and "| 收入 | 2 |" in md and "![RUN-2026-0001 图](</data/runs/RUN-2026-0001/figure.png>)" in md
    ws = {"id": "RUN-2026-0002", "method": "workspace", "code": "# scripts/a.py\nprint(1)\n\n# scripts/b.py\nprint(2)\n",
          "workspace_json": '{"mode": "ai", "code": ["scripts/a.py", "scripts/b.py"], "figures": ["outputs/figures/f.png"], '
                            '"needs_r": ["Table 4：需要 R"]}',
          "result": {"results": {"tables": [{"label": "Table 2", "rows": []}]}}}
    w = report.run_view(ws, extension=True)
    assert w["code"] == [{"name": "scripts/a.py", "text": "print(1)"}, {"name": "scripts/b.py", "text": "print(2)"}]
    assert w["tables"] == [{"label": "Table 2", "rows": []}] and w["needs_r"] == ["Table 4：需要 R"] and w["extension"]


def test_generate_saves_a_draft_artifact_with_the_plans_direction(conn, tmp_path, repo):
    from research_lab.files import artifacts
    ds_id = synthetic_ds(conn, tmp_path, n=150)
    plan_id = plans.create_blank(conn, direction="alliance-us")
    plans.attach_dataset(conn, plan_id, ds_id)
    plans.save_targets(conn, plan_id, [{"tid": "", "table": "Table 2", "term": "收入", "value": "0.140", "page": "3"}])
    run_id = runs.create(conn, "ols", ds_id, {"y": "增长", "x": ["收入"]})
    conn.execute("UPDATE analysis_runs SET plan_id = ? WHERE id = ?", (plan_id, run_id))
    ref = report.generate(conn, plan_id, project="asean-centrality")                 # asean-centrality is sea-asean
    row = conn.execute("SELECT path, type FROM artifacts WHERE ref = ?", (ref,)).fetchone()
    a = artifacts.load(repo / row["path"])
    assert row["type"] == "analysis" and a.front["status"] == "draft" and a.front["direction"] == "alliance-us"
    assert a.front["replication_plan"] == plan_id and a.front["title"].startswith("复现报告")
    evidence = artifacts.section_body(a.body, "Evidence")
    assert "### 方案摘要" in evidence and "### 代码附录" in evidence and "| 尚未对照 |" in evidence
    assert f"[{run_id}](/data/runs/{run_id})" in evidence and "smf.ols" in evidence and ds_id in evidence
    assert artifacts.section_body(a.body, "方案摘要") is None                          # the report stays inside Evidence
    assert not artifacts.counts(a.body)                                               # Claim and the one line stay Leo's
    assert report.generate(conn, plan_id, project="asean-centrality") != ref          # a second report is a new artifact
