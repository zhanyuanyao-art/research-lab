"""Excerpts + search/association module. Papers and pages are seeded with SQL (no Intake dependency)."""
from __future__ import annotations

import json

import pytest

from research_lab import excerpts as ex

PAGES = [
    "Who Convenes Asia? Forum attendance and the durability of ASEAN-led institutions.",
    "Between 2005 and 2022 US leader-level attendance at the EAS lapsed in 5 of 17 summits,\n"
    "yet no participant proposed moving the chair outside ASEAN.",
    "Convening power is a residual of mutual veto. Great powers accept ASEAN as the host.",
]
NOTE = "Attendance drops but the chair rule is never questioned; centrality looks like a rule."


@pytest.fixture()
def seeded(conn):
    ts = "2026-09-29T07:00:00+00:00"
    for pid, title, authors, year in (("tan2023-convenes", "Who Convenes Asia?", ["Tan, L."], 2023),
                                      ("acharya2001-community", "Constructing a Security Community", ["Amitav Acharya"], 2001)):
        conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                     (pid, title, pid, json.dumps(authors), year, ts, ts))
    conn.execute("INSERT INTO paper_files(id, paper_id, sha256, rel_path, bytes, page_count, has_text_layer, extractor, created_at) "
                 "VALUES (1, 'tan2023-convenes', 'abc', 'x.pdf', 10, 3, 1, 'pymupdf', ?)", (ts,))
    for i, t in enumerate(PAGES):
        conn.execute("INSERT INTO paper_pages(file_id, page_index, page_label, text) VALUES (1, ?, ?, ?)", (i, str(244 + i), t))
    return conn


def make(conn, quote, note=NOTE, paper="tan2023-convenes", **kw):
    return ex.create(conn, paper_id=paper, quote=quote, my_note=note, locator=kw.pop("locator", "245"), **kw)


def test_create_verifies_pasted_quote_not_typed(seeded):
    good = make(seeded, "US leader-level attendance at the EAS lapsed in 5 of 17 summits, yet no participant proposed moving the chair",
                page_index=0, file_id=1, uses=["evidence"], cases=["eas"])
    typed = make(seeded, "American presidents skipped most summits and nobody cared at all", kind="paraphrase")
    g, t = ex.get(seeded, good), ex.get(seeded, typed)
    assert g["quote_verified"] == 1 and g["page_index"] == 1       # found on the next page, not the one given
    assert t["quote_verified"] == 0
    assert good.startswith("EXC-") and typed > good and g["status"] == "inbox"
    assert g["project_slug"] is None                               # the paper is in no 课题 (card 0202)
    assert ex.tags(seeded, good)["cases"] == ["EAS"]
    with pytest.raises(ex.ExcerptError):
        make(seeded, "anything", note="ok")
    with pytest.raises(ex.ExcerptError):
        make(seeded, "anything", uses=["nonsense"])


def test_related_lexical_and_tag_overlap(seeded):
    from research_lab.search.related import related
    src = make(seeded, "Leader attendance at the East Asia Summit lapsed while the chair rule stayed with ASEAN")
    similar = make(seeded, "Summit attendance lapsed again; the ASEAN chair rule was not questioned by leaders",
                   note="Same pattern of lapsed attendance and a stable chair rule.")
    unrelated = make(seeded, "Monsoon rainfall variability shapes rice harvests in the Mekong delta",
                     note="Agricultural background, probably irrelevant to institutions.", paper="acharya2001-community")
    items = related(seeded, excerpt_id=src)
    ids = [c.id for c in items]
    assert src not in ids and similar in ids
    if unrelated in ids:
        assert ids.index(similar) < ids.index(unrelated)

    tagged_src = make(seeded, "Great powers accept a convener they cannot veto", cases=["EAS"], paper="acharya2001-community",
                      note="Mutual veto explains who hosts forums in the region.")
    tag_only = make(seeded, "Washington skipped Phnom Penh in 2012", cases=["EAS"], paper="acharya2001-community",
                    note="Presidential absence, recorded in newspapers of that year.")
    items = related(seeded, excerpt_id=tagged_src, explain=True)
    by_id = {c.id: c for c in items}
    assert tag_only in by_id and by_id[tag_only].T == 0 and by_id[tag_only].G > 0.2
    assert "case:EAS" in by_id[tag_only].explain["shared"]
    if unrelated in by_id:
        assert by_id[tag_only].score > by_id[unrelated].score


def test_fts_query_builder(seeded):
    from research_lab.search import fts
    make(seeded, "ASEAN centrality persists under great-power competition")
    seeded.execute("INSERT INTO concepts(id, label, updated_at) VALUES ('centrality', 'ASEAN centrality', 'x')")
    q = fts.build_query(seeded, "ASEAN centrality and the chair rule; the chair rule again", ["ASEAN centrality"])
    assert q.match.startswith('"ASEAN centrality" OR') and len(q.terms) <= 12
    assert ("chair", q.terms[0][1]) in q.terms and all(t not in fts.STOPWORDS for t, _ in q.terms)
    assert fts.search_excerpts(seeded, q) and fts.search_excerpts(seeded, 'x" OR bad(') == []


def setup_job(seeded):
    from research_lab.search import anchors
    # card 0202: an excerpt gets the current project's anchors only when its paper is in that 课题
    seeded.execute("INSERT OR IGNORE INTO paper_projects(paper_id, project_slug) VALUES ('tan2023-convenes', 'asean-centrality')")
    ids = [a.id for a in anchors.all_anchors(seeded)]
    assert "anchor:now:1" in ids and "anchor:now:question" in ids and "anchor:project:asean-centrality" in ids
    src = make(seeded, "Summit attendance lapsed while the ASEAN chair rule held")
    near = [make(seeded, f"Summit attendance lapsed in year {y} and the ASEAN chair rule held firm",
                 note=f"Chair rule persistence despite lapsed attendance, variant {y}.") for y in (2011, 2012, 2013)]
    job_id = seeded.execute("SELECT id FROM ai_jobs WHERE target_id = ?", (src,)).fetchone()
    assert job_id is None
    job = ex.enqueue_associate(seeded, src)
    row = seeded.execute("SELECT * FROM ai_jobs WHERE id = ?", (job,)).fetchone()
    assert row["kind"] == "excerpt.associate" and row["status"] == "queued" and row["target_type"] == "excerpt"
    prompt = json.loads(row["request_json"])["messages"][1]["content"]
    assert "- id: anchor:now:1 · type: anchor" in prompt
    listed = [n for n in near if f"- id: {n} · type: excerpt" in prompt]
    assert len(listed) == 2                                  # diversity: at most 2 per paper
    assert prompt.index("- id: anchor:now:") < prompt.index(f"- id: {listed[0]}")   # anchors first under 20 excerpts
    assert ex.enqueue_associate(seeded, src) == job         # open job is reused
    return src, job, listed


def test_anchors_and_job_prompt(seeded):
    setup_job(seeded)


def fabricated(near):
    return {
        "tags": {"uses": ["evidence"], "cases": ["EAS", "not a case!"], "concepts": ["centrality"], "camp": "institutional-constructivist"},
        "links": [
            {"target_type": "anchor", "target_id": "anchor:now:2", "relation": "supports", "reason": "Rule, not participation."},
            {"target_type": "excerpt", "target_id": near[0], "relation": "parallel", "reason": "Same lapse pattern."},
            {"target_type": "excerpt", "target_id": "EXC-2099-9999", "relation": "supports", "reason": "Invented."},
            {"target_type": "artifact", "target_id": "ART-1999-0101", "relation": "supports", "reason": "Invented."},
        ],
        "claim": {"text": "Centrality in the EAS rests on an uncontested chair rule.", "strength": "seed"},
        "next_question": "Has anyone proposed a non-ASEAN chair for the EAS?",
        "targets": [{"kind": "artifact_section", "ref": "ART-2026-4001 § Evidence", "note": "one bullet"}],
    }


def test_apply_creates_proposals_and_drops_unknown_ids(seeded):
    from pydantic import ValidationError
    from research_lab.ai import queue
    src, job, near = setup_job(seeded)
    quote_before = ex.get(seeded, src)["quote"]
    with pytest.raises(ValidationError):
        queue.complete(seeded, job, {**fabricated(near), "quote": "AI text"}, provider="test", model="t")
    queue.complete(seeded, job, fabricated(near), provider="test", model="t")
    rows = seeded.execute("SELECT * FROM associations WHERE src_id = ? AND origin = 'ai-suggested'", (src,)).fetchall()
    assert all(r["status"] == "proposed" and r["ai_run_id"] for r in rows)
    links = {(r["dst_type"], r["dst_id"]) for r in rows if r["rel"] != "about"}
    assert links == {("anchor", "anchor:now:2"), ("excerpt", near[0])}
    tags = {(r["dst_type"], r["dst_id"]) for r in rows if r["rel"] == "about"}
    assert ("use", "evidence") in tags and ("case", "EAS") in tags and ("concept", "centrality") in tags
    assert not any(d == "not a case!" for _, d in tags)
    claim = seeded.execute("SELECT * FROM claims WHERE origin = 'ai-suggested'").fetchone()
    assert claim["status"] == "proposed" and claim["kind"] == "claim" and json.loads(claim["source_excerpts_json"]) == [src]
    assert ex.get(seeded, src)["quote"] == quote_before and ex.get(seeded, src)["status"] == "inbox"
    assert seeded.execute("SELECT COUNT(*) FROM evidence_cells").fetchone()[0] == 0


def test_panel_accept_reject_claim_and_next_question(seeded, client, repo):
    from research_lab.ai import queue
    from research_lab.files import datafiles
    src, job, near = setup_job(seeded)
    queue.complete(seeded, job, fabricated(near), provider="test", model="t")
    r = client.get(f"/_p/job/{job}/status")
    assert r.status_code == 200 and "Accept" in r.text and "Use as claim" in r.text and "Set as next step" in r.text
    link = seeded.execute("SELECT id FROM associations WHERE src_id=? AND dst_type='anchor'", (src,)).fetchone()[0]
    use = seeded.execute("SELECT id FROM associations WHERE src_id=? AND dst_type='use'", (src,)).fetchone()[0]
    other = seeded.execute("SELECT id FROM associations WHERE src_id=? AND dst_type='excerpt'", (src,)).fetchone()[0]
    assert client.post(f"/_p/suggestion/{link}/accept", data={"reason": "Edited reason."}).status_code == 200
    assert client.post(f"/_p/suggestion/{use}/accept").status_code == 200
    assert client.post(f"/_p/suggestion/{other}/reject", data={"reason": "trivial"}).status_code == 200
    assert client.post(f"/_p/suggestion/{other}/reject", data={"reason": "bogus"}).status_code == 400
    st = {r["id"]: r for r in seeded.execute("SELECT * FROM associations WHERE id IN (?,?,?)", (link, use, other))}
    assert st[link]["status"] == "accepted" and st[link]["reason"] == "Edited reason."
    assert st[other]["status"] == "rejected" and st[other]["reject_reason"] == "trivial"
    e = ex.get(seeded, src)
    assert e["status"] == "linked" and "evidence" in json.loads(e["uses_json"])
    claim = seeded.execute("SELECT id FROM claims WHERE origin = 'ai-suggested'").fetchone()[0]
    assert client.post(f"/_p/claim/{claim}/accept", data={"text": "Edited claim text."}).status_code == 200
    c = seeded.execute("SELECT * FROM claims WHERE id = ?", (claim,)).fetchone()
    assert c["status"] == "accepted" and c["statement"] == "Edited claim text."
    assert client.post(f"/_p/excerpt/{src}/next-question").status_code == 200
    assert "non-ASEAN chair" in " ".join(datafiles.read("now")["need_to_find_out"])
    for url in (f"/excerpts/{src}", "/excerpts?filter=suggestions", "/excerpts"):
        assert client.get(url).status_code == 200, url
    assert "历史" in client.get(f"/excerpts/{src}").text
    # rejections become negative examples for the next run
    job2 = ex.enqueue_associate(seeded, src)
    prompt = json.loads(queue.get(seeded, job2)["request_json"])["messages"][1]["content"]
    assert "rejected as trivial" in prompt


def test_add_to_artifact_creates_seed_and_marker(seeded, repo):
    from research_lab.files import artifacts as A
    from research_lab.files.weeks import week_of
    for p in (repo / "_artifacts").glob(f"{week_of()}-*.md"):
        p.unlink()
    eid = make(seeded, "US leader-level attendance at the EAS lapsed in 5 of 17 summits", locator="245")
    with pytest.raises(ex.ExcerptError):
        ex.add_to_artifact(seeded, eid, section="Claim")
    path, ref = ex.add_to_artifact(seeded, eid)
    assert path.name.startswith(week_of()) and ref.startswith("ART-")
    a = A.load(path)
    assert a.get("level") == "seed" and a.get("type") == "evidence"
    line = f"- Tan (2023, p. 245): “US leader-level attendance at the EAS lapsed in 5 of 17 summits” <!-- rl:{eid} -->"
    assert line in A.section_body(a.body, "Evidence")
    assert "rl:" not in (A.section_body(a.body, "Claim") or "")
    assert ex.get(seeded, eid)["status"] == "used"
    path2, ref2 = ex.add_to_artifact(seeded, eid)
    assert (path2, ref2) == (path, ref) and A.load(path).body.count(f"rl:{eid}") == 1


def test_routes(seeded, client, repo):
    r = client.post("/excerpts", data={"paper_id": "tan2023-convenes", "locator": "245", "kind": "quote",
                                       "quote": "no participant proposed moving the chair outside ASEAN", "my_note": NOTE,
                                       "uses": ["evidence", "framing"], "cases": "EAS, 2017", "source_type": "secondary-scholarly"})
    assert r.status_code == 200 and "excerpt-saved" in r.headers["HX-Trigger"]
    eid = json.loads(r.headers["HX-Trigger"])["excerpt-saved"]["id"]
    assert "已交给助手" in r.text and "原文未核对上" not in r.text
    bad = client.post("/excerpts", data={"paper_id": "tan2023-convenes", "locator": "1", "quote": "x", "my_note": ""})
    assert bad.status_code == 200 and "form-error" in bad.text and bad.headers["HX-Retarget"] == "#excerpt-form-errors"
    plain = client.post("/excerpts", headers={"HX-Request": "", "Origin": "http://127.0.0.1:4100"}, follow_redirects=False,
                        data={"paper_id": "tan2023-convenes", "locator": "246", "quote": "Convening power", "my_note": NOTE})
    assert plain.status_code == 303 and plain.headers["location"].startswith("/excerpts/EXC-")
    for url in ("/excerpts", "/excerpts?filter=suggestions", "/excerpts?filter=all&use=evidence", f"/excerpts/{eid}",
                "/_p/excerpt-form?paper_id=tan2023-convenes&file_id=1&page_index=1&page_label=245&quote=hello",
                "/_p/excerpt-form", f"/_p/related?src={eid}&explain=1", "/_p/related?text=chair+rule"):
        resp = client.get(url)
        assert resp.status_code == 200, url
    form = client.get("/_p/excerpt-form?paper_id=tan2023-convenes&page_label=245&quote=hello").text
    assert 'name="my_note"' in form and "autofocus" in form and 'value="245"' in form and ">hello</textarea>" in form
    job = seeded.execute("SELECT id FROM ai_jobs WHERE target_id = ?", (eid,)).fetchone()[0]
    st = client.get(f"/_p/job/{job}/status")
    assert st.status_code == 200 and 'hx-trigger="every 8s"' in st.text
    assert client.post(f"/excerpts/{eid}/reassociate").status_code == 200
    assert client.post(f"/_p/excerpt/{eid}/add-to-artifact", data={"section": "Evidence"}).status_code == 200
    assert client.get("/excerpts/EXC-1999-0001").status_code == 404


def test_include_contract_renders_drawer(repo):
    from research_lab.web.context import templates
    html = templates().env.get_template("partials/_excerpt_form.html").render(paper={"id": "tan2023-convenes"}, file_id=3)
    assert 'data-hotkey="excerpt"' in html and 'id="excerpt-drawer"' in html
    assert 'data-paper-id="tan2023-convenes"' in html and 'data-file-id="3"' in html and "/static/excerpts.js" in html


def test_cli_related_and_excerpt(seeded):
    from typer.testing import CliRunner
    from research_lab.cli import app
    runner = CliRunner()
    r = runner.invoke(app, ["excerpt", "add", "--paper", "tan2023-convenes", "--locator", "246", "--quote",
                            "Convening power is a residual of mutual veto", "--note", NOTE, "--use", "mechanism"])
    assert r.exit_code == 0, r.output
    assert "verified" in r.output and "excerpt.associate queued" in r.output
    eid = r.output.split()[0]
    r = runner.invoke(app, ["related", eid, "--explain"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["excerpt", "show", eid])
    assert r.exit_code == 0 and "mutual veto" in r.output


# ---------------------------------------------------------------- reader marks (card 0033)

def test_highlight_segments_cross_paragraphs_and_ignore_misses():
    from research_lab.web.routes.papers import highlight
    paras = ["Between 2005 and 2022 US attendance  lapsed.", "Yet no participant proposed moving the chair."]
    segs = highlight(paras, [("E1", "attendance lapsed. Yet no participant"), ("E2", "nothing like this here"), ("E3", "ab")])
    assert ["".join(g["text"] for g in p) for p in segs] == paras                # text is preserved exactly
    marked = [(g["text"], g["ids"]) for p in segs for g in p if g["ids"]]
    assert marked == [("attendance  lapsed.", ["E1"]), ("Yet no participant", ["E1"])]   # cut at the paragraph break


def test_reader_preserves_historical_quotes_and_page_groups(seeded, client, repo):
    hit = make(seeded, "US leader-level attendance at the EAS lapsed in 5 of 17 summits, yet no participant",
               page_index=1, file_id=1, locator="245")
    miss = make(seeded, "American presidents skipped most summits and nobody cared at all", page_index=1, file_id=1,
                locator="245", kind="paraphrase")
    other = make(seeded, "Convening power is a residual of mutual veto", page_index=2, file_id=1, locator="246")
    html = client.get("/papers/tan2023-convenes").text
    assert '<mark class="hl"' not in html and 'id="excerpt-drawer"' not in html
    assert '历史划线 · 3' in html
    for eid in (hit, miss, other):
        assert f'data-excerpt="{eid}"' in html and f'href="/excerpts/{eid}"' in html
        assert ex.get(seeded, eid) is not None
    assert 'class="excerpt-groups" id="paper-excerpts"' in html
    assert html.index("第 245 页") < html.index("第 246 页")



def test_excerpt_form_project_is_a_choice(seeded, client, repo):
    form = client.get("/_p/excerpt-form?paper_id=tan2023-convenes&file_id=1&page_index=1&page_label=245&quote=x").text
    assert '<select id="xf-project" name="project">' in form and "不挂课题" in form
