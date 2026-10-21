import json

import pytest

from research_lab.db.connection import now_iso
from research_lab.method import exports, hunt, mapgraph, matrix, milestones, seed

SLUG = "asean-centrality"
CAMPS = ["institutional-constructivist", "great-power-hierarchy", "sceptic"]


def add_paper(conn, pid, camp=None, year=2001, status="verified", **fields):
    now = now_iso()
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, metadata_status, camp, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (pid, f"Title {pid}", pid, json.dumps([f"Author {pid}"]), year, status, camp, now, now))
    for k, v in fields.items():
        conn.execute(f"UPDATE papers SET {k} = ? WHERE id = ?", (v, pid))


def add_excerpt(conn, eid, pid, source_type="secondary-scholarly", locator="p. 12"):
    now = now_iso()
    conn.execute("INSERT INTO excerpts(id, paper_id, locator, quote, my_note, source_type, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                 (eid, pid, locator, "a quoted passage", "why it matters", source_type, now, now))


def cell_id(conn, code, case):
    return conn.execute("SELECT ec.id FROM evidence_cells ec JOIN claims c ON c.id = ec.claim_id "
                        "WHERE c.project_slug = ? AND c.code = ? AND ec.case_id = ?", (SLUG, code, case)).fetchone()[0]


@pytest.fixture()
def seeded(conn):
    seed.seed(conn, SLUG)
    seed.seed(conn, "us-rok-reassurance")
    return conn


def test_seed_idempotent(seeded):
    again = seed.seed(seeded, SLUG)
    assert sum(again.values()) == 0
    assert seeded.execute("SELECT COUNT(*) FROM evidence_cells WHERE status NOT IN ('missing','na')").fetchone()[0] == 0
    assert seeded.execute("SELECT is_own FROM claims WHERE code = 'H4'").fetchone()[0] == 1
    assert seeded.execute("SELECT COUNT(*) FROM cases WHERE project_slug = ? AND is_observation = 1", (SLUG,)).fetchone()[0] == 6
    assert seeded.execute("SELECT concept_id FROM concept_projects WHERE project_slug = 'us-rok-reassurance'").fetchall()[0][0] == "reassurance"
    seed.seed(seeded, SLUG, force=True)


def test_m1_and_next_unmet(repo, seeded):
    ms = {m.id: m for m in milestones.evaluate(seeded, SLUG)}
    assert not ms["M1"].passed
    assert "0 / 12" in ms["M1"].checks[0].detail
    nxt = milestones.next_unmet(seeded, SLUG)
    assert isinstance(nxt, dict) and nxt["milestone"] == "M1" and "12 篇" in nxt["failing"]
    assert (repo / "_data" / "lab.yml").exists()
    assert "next_milestone: M1" in (repo / "_data" / "lab.yml").read_text()

    for i in range(12):
        add_paper(seeded, f"p{i}", camp=CAMPS[i % 3])
    seeded.execute("INSERT INTO artifacts(ref, path, type, level, project, sha256, mtime) VALUES "
                   "('ART-2026-4001', '_artifacts/x.md', 'landscape', 'developed', ?, 'x', 0)", (SLUG,))
    ms = {m.id: m for m in milestones.evaluate(seeded, SLUG)}
    assert ms["M1"].passed
    assert milestones.next_unmet(seeded, SLUG)["milestone"] == "M2"
    assert "papers: 12" in (repo / "_data" / "lab.yml").read_text()


def test_submitted_project_has_nothing_unmet(seeded):
    assert milestones.next_unmet(seeded, "us-rok-reassurance") is None


def test_cell_status_rules(seeded):
    cid = cell_id(seeded, "H3", "O3-2011-us-russia-eas")
    with pytest.raises(matrix.CellRuleError):
        matrix.set_status(seeded, cid, "strong")
    with pytest.raises(matrix.CellRuleError):
        matrix.set_status(seeded, cid, "searched-none")
    add_paper(seeded, "sec1")
    add_excerpt(seeded, "EXC-2026-0001", "sec1")
    matrix.attach_excerpt(seeded, cid, "EXC-2026-0001")
    matrix.set_status(seeded, cid, "weak")
    with pytest.raises(matrix.CellRuleError):
        matrix.set_status(seeded, cid, "strong")
    add_paper(seeded, "state1")
    add_excerpt(seeded, "EXC-2026-0002", "state1", source_type="primary-official")
    matrix.attach_excerpt(seeded, cid, "EXC-2026-0002")
    matrix.set_status(seeded, cid, "strong")
    assert matrix.verdicts(seeded, SLUG)["H3"] == "survives"
    matrix.log_search(seeded, cell_id(seeded, "H1", "O4-2012-phnom-penh-amm"), "AMM communiqués 2012", "joint communiqué")
    matrix.set_status(seeded, cell_id(seeded, "H1", "O4-2012-phnom-penh-amm"), "searched-none")
    assert matrix.verdicts(seeded, SLUG)["H1"] == "eliminated"
    with pytest.raises(matrix.CellRuleError):
        matrix.detach_excerpt(seeded, cid, "EXC-2026-0002")


def test_hunt_hoop_first_and_rule_8(seeded):
    items = hunt.hunt_list(seeded, SLUG)
    assert [(i["code"], i["case_id"]) for i in items[:3]] == [
        ("H2", "O2-2005-eas-criteria"), ("H4", "O3-2011-us-russia-eas"), ("H4", "O6-2021-quad")]
    # A hoop outranks a non-test cell with the same scores.
    h2 = cell_id(seeded, "H2", "O2-2005-eas-criteria")
    matrix.update_cell(seeded, h2, test_type="straw")
    assert hunt.hunt_list(seeded, SLUG)[0]["test_type"] == "hoop"
    assert seeded.execute("SELECT priority FROM evidence_cells WHERE id = ?", (cell_id(seeded, "H4", "O3-2011-us-russia-eas"),)).fetchone()[0] == 6

    # H4 least evidenced: give each rival one evidenced cell and bury H4's hoops.
    add_paper(seeded, "e1")
    add_excerpt(seeded, "EXC-2026-0009", "e1")
    for code in ("H1", "H2", "H3"):
        c = cell_id(seeded, code, "O5-2019-aoip")
        matrix.attach_excerpt(seeded, c, "EXC-2026-0009")
        matrix.set_status(seeded, c, "weak")
    for case in ("O3-2011-us-russia-eas", "O6-2021-quad"):
        matrix.update_cell(seeded, cell_id(seeded, "H4", case), feasibility=0)
    items = hunt.hunt_list(seeded, SLUG, n=3)
    assert len(items) == 3
    forced = [i for i in items if i["forced"]]
    assert forced and forced[0]["code"] == "H4" and forced[0]["test_type"] == "hoop"


def test_map_elements_suggested(seeded):
    add_paper(seeded, "acharya2001", camp="institutional-constructivist")
    add_paper(seeded, "goh2007", camp="great-power-hierarchy")
    add_paper(seeded, "caballero2014")
    seeded.execute("INSERT INTO paper_projects(paper_id, project_slug) VALUES ('caballero2014', ?)", (SLUG,))
    now = now_iso()
    seeded.execute("INSERT INTO associations(src_type, src_id, rel, dst_type, dst_id, origin, status, created_at) VALUES "
                   "('paper','goh2007','contests','paper','acharya2001','ai-suggested','proposed',?)", (now,))
    el = mapgraph.elements(seeded, SLUG)
    edge = [e for e in el["edges"] if e["data"]["rel"] == "contests"][0]
    assert "suggested" in edge["classes"]
    paper = [n for n in el["nodes"] if n["data"]["id"] == "paper:acharya2001"][0]
    assert paper["data"]["parent"] == "camp:institutional-constructivist"
    assert [u["id"] for u in el["unplaced"]] == ["caballero2014"]
    assert not mapgraph.map_checks(seeded, SLUG)["debate"]["passed"]
    mapgraph.set_edge_status(seeded, edge["data"]["assoc_id"], "accepted")
    assert mapgraph.map_checks(seeded, SLUG)["debate"]["passed"]
    mapgraph.assign_camp(seeded, SLUG, "caballero2014", "relational-network")
    assert mapgraph.elements(seeded, SLUG)["unplaced"] == []


def test_exports_only_inside_markers(repo, seeded):
    path = repo / "projects" / SLUG / "literature.md"
    before = path.read_text()
    add_paper(seeded, "acharya2001", camp="institutional-constructivist", mechanism="socialization")
    exports.write_literature_md(seeded, SLUG)
    text = path.read_text()
    assert text.startswith(before.rstrip("\n"))
    assert "<!-- rl:begin table -->" in text and "socialization" in text
    edited = text.replace("# Literature", "# Literature (my edit)") + "\nMy trailing note.\n"
    path.write_text(edited)
    exports.write_literature_md(seeded, SLUG)
    again = path.read_text()
    assert "# Literature (my edit)" in again and "My trailing note." in again
    assert again.count("<!-- rl:begin table -->") == 1

    from research_lab.files import artifacts as A
    A.index(seeded)
    ref = exports.export_literature_map(seeded, SLUG)
    art = A.load(next(p for p in (repo / "_artifacts").glob("*literature-map.md")))
    assert art.ref == ref and art.front["level"] == "seed"
    assert "rl:begin terrain" in art.body and "```mermaid" in art.body
    claim = A.section_body(art.body, "Claim")
    assert "rl:" not in claim and "..." in claim
    exports.write_evidence_md(seeded, SLUG)
    assert "<!-- rl:begin table -->" in (repo / "projects" / SLUG / "evidence.md").read_text()


def test_stage_suggestion(repo, seeded):
    assert milestones.stage_suggestion(seeded, SLUG) is None
    for i in range(12):
        add_paper(seeded, f"p{i}", camp=CAMPS[i % 3])
    seeded.execute("INSERT INTO artifacts(ref, path, type, level, project, sha256, mtime) VALUES "
                   "('A1','a','landscape','developed',?,'x',0), ('A2','b','puzzle','seed',?,'x',0)", (SLUG, SLUG))
    for case in ("O1-1994-arf", "O2-2005-eas-criteria", "O3-2011-us-russia-eas"):
        add_excerpt(seeded, f"EXC-{case}", "p0")
        matrix.attach_excerpt(seeded, cell_id(seeded, "H2", case), f"EXC-{case}")
    milestones.confirm(seeded, SLUG, "M2", "pattern", True)
    assert milestones.stage_suggestion(seeded, SLUG) is None
    milestones.confirm(seeded, SLUG, "M2", "expectation", True)
    assert milestones.stage_suggestion(seeded, SLUG) == "puzzle"


def test_checks_derive_job(seeded):
    from research_lab.ai import queue
    from research_lab.method import checks
    h4 = seeded.execute("SELECT id FROM claims WHERE code = 'H4'").fetchone()[0]
    job = queue.enqueue(seeded, "checks.derive", str(h4))
    assert job
    queue.complete(seeded, job, {"checks": [{"title": "Bypass ratio", "prediction": "Betweenness falls as bypass grows.",
                                            "data_needed": "membership lists", "method": "yearly projection",
                                            "would_weaken_if": "betweenness flat"}]}, provider="claude-code", model="test")
    ks = checks.list_checks(seeded, SLUG)
    assert ks[0]["origin"] == "ai-suggested" and ks[0]["ai_run_id"]
    with pytest.raises(checks.CheckRuleError):
        checks.set_status(seeded, ks[0]["id"], "run")
    checks.accept(seeded, ks[0]["id"])
    assert checks.get(seeded, ks[0]["id"])["origin"] == "user"


def test_routes(client, conn):
    seed.seed(conn, SLUG)
    seed.seed(conn, "us-rok-reassurance")
    add_paper(conn, "acharya2001", camp="institutional-constructivist")
    for url in ("/projects", f"/projects/{SLUG}", f"/projects/{SLUG}/map", f"/projects/{SLUG}/matrix",
                f"/projects/{SLUG}/checks", f"/projects/{SLUG}/concepts/centrality", "/pipeline",
                f"/projects/{SLUG}/hunt", "/projects/us-rok-reassurance", "/"):
        r = client.get(url)
        assert r.status_code == 200, url
    r = client.get(f"/api/projects/{SLUG}/map.json")
    assert r.status_code == 200 and r.json()["nodes"]
    assert client.get(f"/projects/{SLUG}/map/node", params={"id": "paper:acharya2001"}).status_code == 200
    cid = cell_id(conn, "H3", "O3-2011-us-russia-eas")
    assert client.get(f"/projects/{SLUG}/cells/{cid}").status_code == 200
    r = client.post(f"/projects/{SLUG}/cells/{cid}", data={"status": "strong", "implication": "US says EAS is useful"})
    assert r.status_code == 200 and "needs at least one attached excerpt" in r.text
    r = client.post(f"/projects/{SLUG}/confirm", data={"milestone": "M2", "item": "pattern", "checked": "1"})
    assert r.status_code == 200 and "M2" in r.text
    r = client.post(f"/projects/{SLUG}/checks", data={"title": "T", "prediction": "P before looking"}, follow_redirects=False)
    assert r.status_code == 204
    r = client.post(f"/projects/{SLUG}/concepts/centrality/definition", data={"text": "Centrality is a convening rule."})
    assert conn.execute("SELECT def_version FROM concepts WHERE id = 'centrality'").fetchone()[0] == "v0.1"


def test_derive_route_and_matrix_markup(client, conn):
    seed.seed(conn, SLUG)
    h4 = conn.execute("SELECT id FROM claims WHERE code = 'H4'").fetchone()[0]
    r = client.post(f"/projects/{SLUG}/checks/derive", data={"target": str(h4)}, follow_redirects=False)
    assert r.status_code == 204 and "error" not in r.headers["HX-Redirect"]
    assert conn.execute("SELECT COUNT(*) FROM ai_jobs WHERE kind = 'checks.derive'").fetchone()[0] == 1
    html = client.get(f"/projects/{SLUG}/matrix").text
    assert "○" in html and "missing" in html and "Rule 8" not in html and "hoop" in html
