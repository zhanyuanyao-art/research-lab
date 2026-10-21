"""数据页 as one list of datasets (card 0065, docs/design/DATA-v2.md): plans and analyses hang on their dataset,
plans without one wait in 待配数据, and the research direction filters the list."""
from __future__ import annotations

import json


def _dataset(conn, ds_id: str, name: str, direction: str | None, created: str):
    conn.execute("INSERT INTO datasets(id, name, source_kind, original_name, rows, cols, schema_json, profile_json, direction, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (ds_id, name, "csv", f"{name}.csv", 10, 3, "{}", "{}", direction, created, created))


def _plan(conn, plan_id: str, datasets: list[str], direction: str | None = None):
    conn.execute("INSERT INTO replication_plans(id, paper_id, direction, status, plan_json, origin, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?,?,?)", (plan_id, None, direction, "draft",
                                              json.dumps({"datasets": datasets, "steps": [], "model": {}}), "user",
                                              "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"))


def test_data_page_lists_datasets_with_their_plans_and_waiting_plans(client, conn):
    _dataset(conn, "DS-2026-0001", "东盟峰会出席", "sea-asean", "2026-10-01T00:00:00+00:00")
    _dataset(conn, "DS-2026-0002", "同盟承诺", "alliance-us", "2026-10-02T00:00:00+00:00")
    _plan(conn, "RP-2026-0001", ["DS-2026-0001"])
    _plan(conn, "RP-2026-0002", [], direction="sea-asean")
    conn.commit()
    page = client.get("/data?tab=datasets").text                                     # card 0196: datasets are one tab over
    assert 'data-testid="replications"' not in page and "复现方案</th>" in page and "分析</th>" in page
    row = page.split("东盟峰会出席")[1].split("</tr>")[0]
    assert '<td class="num">1</td>' in row                                  # its one plan
    assert "待配数据" in page and "未关联文献的方案" in page.split("待配数据")[1]
    assert "DS-2026-" not in page.split("</table>")[0].split("<tbody>")[1].replace('href="/data/datasets/DS-2026-', "")  # no IDs shown

    sea = client.get("/data?direction=sea-asean&tab=datasets").text
    assert "东盟峰会出席" in sea and "同盟承诺" not in sea and "待配数据" in sea
    us = client.get("/data?direction=alliance-us&tab=datasets").text
    assert "同盟承诺" in us and "待配数据" not in us


def test_dataset_page_puts_its_plans_and_analyses_first_without_ids(client, conn):
    _dataset(conn, "DS-2026-0001", "东盟峰会出席", "sea-asean", "2026-10-01T00:00:00+00:00")
    _plan(conn, "RP-2026-0001", ["DS-2026-0001"])
    conn.commit()
    page = client.get("/data/datasets/DS-2026-0001").text
    assert page.index('class="ds-work"') < page.index("<h2>预览")
    work = page.split('class="ds-work"')[1].split("</section>")[0]
    assert "未关联文献的方案" in work and ">RP-2026-0001<" not in work and "Plan from papers" in work


def test_a_deleted_dataset_leaves_its_plans_and_analyses_reachable(client, conn):
    """Card 0065 review: a plan that only named a deleted dataset waits in 待配数据; an analysis whose dataset was
    deleted keeps its results and stays listed."""
    _plan(conn, "RP-2026-0003", ["DS-2026-0099"])               # its dataset is gone
    conn.execute("INSERT INTO analysis_runs(id, dataset_id, method, params_json, result_json, status, reading, created_at, updated_at) "
                 "VALUES ('RUN-2026-0001', NULL, 'ols', '{}', '{}', 'done', '出席率与峰会主办无关。', ?, ?)",
                 ("2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"))
    conn.commit()
    assert 'href="/data/plans/RP-2026-0003"' in client.get("/data").text          # the replications tab lists it too
    page = client.get("/data?tab=datasets").text                                     # card 0196: datasets are one tab over
    assert "待配数据" in page and 'href="/data/plans/RP-2026-0003"' in page
    assert "数据集已删除的分析" in page and 'href="/data/runs/RUN-2026-0001"' in page and "已写解读" in page


def test_dataset_page_shows_plan_status(client, conn):
    _dataset(conn, "DS-2026-0001", "东盟峰会出席", "sea-asean", "2026-10-01T00:00:00+00:00")
    _plan(conn, "RP-2026-0001", ["DS-2026-0001"])
    conn.commit()
    work = client.get("/data/datasets/DS-2026-0001").text.split('class="ds-work"')[1].split("</section>")[0]
    assert "草稿" in work
