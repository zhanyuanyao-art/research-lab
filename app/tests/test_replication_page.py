"""Card 0196: a replication is one page — 研究, 数据, 方法 and 结果 open in place, drawn with components (explanations
with their support, headline numbers, a method × result matrix, results beside the paper with pictures that open large
beside a note); only the deck has a page of its own; the plan's fields live on /detail; /data lists replications first."""
from __future__ import annotations

from research_lab.replication import workspace as W

from .test_replication_deck import add_run, data_client, fixture_plan, results_json  # noqa: F401
from .test_report_content import AI, write_ai


def code(plan_id: str) -> None:
    d = W.workspace_dir(plan_id) / "author" / "code"
    d.mkdir(parents=True, exist_ok=True)
    (d / "main.R").write_text('fit = glm(n ~ x + factor(admin), data = d, family = "poisson")\n')


def made(data_client, conn, plan_id: str, ai: dict | None = None) -> str:
    code(plan_id)
    write_ai(plan_id, ai or AI)
    add_run(conn, plan_id, results_json())
    assert data_client.post(f"/data/plans/{plan_id}/deck", follow_redirects=False).status_code == 303
    r = data_client.get(f"/data/plans/{plan_id}")
    assert r.status_code == 200
    return r.text


def test_before_a_deck_the_page_says_what_comes_and_offers_the_one_action(data_client, fixture_plan):
    r = data_client.get(f"/data/plans/{fixture_plan['id']}")
    assert r.status_code == 200 and 'data-testid="replication-report"' in r.text
    assert "还没有运行" in r.text and "Make deck" in r.text and f"/data/plans/{fixture_plan['id']}/detail" in r.text
    assert "还没有研究概要" in r.text and "还没有结果" in r.text


def test_sections_open_in_place_with_components(data_client, conn, fixture_plan):
    ai = {**AI, "models": [{**m, "support": s} for m, s in zip(AI["models"], ("不支持", "支持"))],
          "sources": [{"big": "2,685", "name": "场会议", "what": "会议记录"}],
          "measurement": [{"name": "训练", "what": "用传记特征预测"}, {"name": "外推", "what": "用到国安会成员"}]}
    page = made(data_client, conn, fixture_plan["id"], ai)
    for want in ('id="study"', 'id="data"', 'id="methods"', 'id="results"', "2 种解释，顾问模型得到支持",
                 '<span class="chip crit">不支持</span>', '<span class="chip ok">支持</span>', "主要发现", "值得讨论",
                 '<div class="tile-value">2,685</div>', "第 2 步", "作者脚本", 'id="method-poisson"',
                 "要做哪些检验，为什么", "完整讲解", "1 / 3 一致", 'data-zoom="/data/plans/', 'id="rp-zoom"'):
        assert want in page, want
    report = page.split('data-testid="replication-report"')[1]
    assert "（" not in report.split("<dialog")[0].split('class="prose"')[0]


def test_no_third_level_but_the_deck(data_client, fixture_plan):
    plan_id = fixture_plan["id"]
    for gone in ("study", "sources", "methods", "results", "results/0", "methods/poisson"):
        assert data_client.get(f"/data/plans/{plan_id}/{gone}").status_code in (404, 405), gone   # 405: a POST lives there
    assert data_client.get(f"/data/plans/{plan_id}/deck").status_code == 200


def test_a_result_without_a_page_says_why_and_methods_map_to_results(data_client, conn, fixture_plan):
    import json
    plan_id = fixture_plan["id"]
    for label, scripts in (("Figure 7", [{"path": "descPlots.R", "have": False}]), ("Table 3", [{"path": "main.R", "have": True}])):
        conn.execute("UPDATE replication_coverage SET scripts_json = ? WHERE plan_id = ? AND label = ?",
                     (json.dumps(scripts), plan_id, label))
    page = made(data_client, conn, plan_id)
    assert "缺脚本" in page and "复现包里没有这个脚本，取到后会自动跑" in page
    assert "方法用在哪" in page and '<a href="#method-poisson">泊松回归</a>' in page


def test_the_plans_fields_live_on_the_detail_page_and_edits_return_there(data_client, fixture_plan):
    plan_id = fixture_plan["id"]
    r = data_client.get(f"/data/plans/{plan_id}/detail")
    assert r.status_code == 200 and "方案详情" in r.text and f'<a href="/data/plans/{plan_id}">' in r.text
    r = data_client.post(f"/data/plans/{plan_id}/steps", data={"text": "下载数据"}, follow_redirects=False)
    assert r.status_code == 200 or "/detail" in r.headers["location"]


def test_the_data_home_lists_replications_by_direction(data_client, conn, fixture_plan):
    add_run(conn, fixture_plan["id"], results_json())
    r = data_client.get("/data")
    assert r.status_code == 200 and 'data-testid="replications"' in r.text
    assert f'href="/data/plans/{fixture_plan["id"]}"' in r.text and "Advisers and Aggregation" in r.text
    assert "3 个数里 1 个一致" in r.text


def test_links_to_the_plans_controls_go_to_the_detail_page(data_client, conn, fixture_plan):
    plan_id = fixture_plan["id"]
    conn.execute("UPDATE replication_plans SET targets_reviewed_at = NULL WHERE id = ?", (plan_id,))
    work = data_client.get(f"/replicate/{plan_id}").text
    assert f'/data/plans/{plan_id}/detail' in work and "/detail/detail" not in work
    panel = data_client.get(f"/data/plans/{plan_id}/missing").text
    assert f'href="/data/plans/{plan_id}#missing"' not in panel
