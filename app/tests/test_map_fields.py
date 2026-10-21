"""Card 0036: 我的判断 (论点、机制、方法…) is edited on the 课题's literature map, not in the reader."""
from __future__ import annotations


def test_judgment_fields_on_the_map_card(client, repo, monkeypatch):
    from research_lab.db.connection import connect, now_iso
    from research_lab.intake import pipeline
    c = connect()
    c.execute("INSERT INTO papers(id, title, title_hash, year, metadata_status, created_at, updated_at) VALUES ('tan2023', 'Who Convenes Asia?', 'h', 2023, "
              "'verified', ?, ?)", (now_iso(), now_iso()))
    c.execute("INSERT INTO paper_projects(paper_id, project_slug) VALUES ('tan2023', 'asean-centrality')")
    c.close()
    panel = client.get("/projects/asean-centrality/map/node?id=paper:tan2023").text
    assert 'hx-post="/projects/asean-centrality/map/paper/tan2023"' in panel and "核心主张" in panel and "保存判断" in panel
    r = client.post("/projects/asean-centrality/map/paper/tan2023",
                    data={"claim_text": "Great powers attend to keep options open", "method": "process tracing",
                          "relation_to_puzzle": "rivals"})
    assert r.status_code == 200 and "已保存。" in r.text and "process tracing" in r.text
    c = connect()
    p = pipeline.get_paper(c, "tan2023")
    c.close()
    assert (p["claim_text"], p["method"], p["relation_to_puzzle"]) == ("Great powers attend to keep options open", "process tracing", "rivals")
    bad = client.post("/projects/asean-centrality/map/paper/tan2023", data={"relation_to_puzzle": "maybe"})
    assert bad.status_code == 200 and "与谜题的关系只能从列表里选" in bad.text and "must be one of" not in bad.text
    assert client.post("/projects/asean-centrality/map/paper/nope", data={"method": "x"}).status_code == 404
    reader = client.get("/papers/tan2023").text
    assert "保存字段" not in reader and 'name="claim_text"' not in reader and "/projects/asean-centrality/map" in reader
