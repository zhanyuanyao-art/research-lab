"""Card 0202 (Leo 10-21: 「一起改吧」, after card 0201): an excerpt of a paper in no 课题 is filed under none and is
associated without the ASEAN codebook, the association example names no topic, and a summary that copies the
example's camp placeholder does not store it."""
from __future__ import annotations

import json

from research_lab import excerpts as ex
from research_lab.ai import registry
from research_lab.ai.jobs import paper_summarize as PS

from .test_excerpts import make, seeded  # noqa: F401

QUOTE = "US leader-level attendance at the EAS lapsed in 5 of 17 summits, yet no participant proposed moving the chair"
CODEBOOK = ("ARF", "ADMM+", "institutional-constructivist", "small-state-strategy", "EAS-2017")


def prompt(conn, excerpt_id: str) -> str:
    req, _ = registry.get("excerpt.associate").build(excerpt_id, conn)
    return req.messages[1]["content"]


def test_an_excerpt_outside_every_project_gets_no_codebook(seeded):  # noqa: F811
    from research_lab.method import seed
    seed.seed(seeded, seed.ASEAN)                       # the ASEAN camps and cases exist, as in Leo's lab
    eid = make(seeded, QUOTE, page_index=0, file_id=1)
    assert ex.get(seeded, eid)["project_slug"] is None
    whole = prompt(seeded, eid)
    vocab = whole.split("## Vocabularies")[1].split("## Candidates")[0]
    assert not [w for w in CODEBOOK if w in vocab], vocab
    assert "B camps: (none; leave camp empty)" in vocab and "<CASE>-2017" in vocab
    assert "anchor:camp:" not in whole and "anchor:case:" not in whole and "anchor:now:" not in whole
    assert "anchor:project:asean-centrality" in whole            # every project's question stays a candidate


def test_the_excerpt_drawer_without_a_paper_files_under_no_project(client):
    html = client.get("/_p/excerpt-form").text
    assert '<option value="" selected>不挂课题</option>' in html and 'value="asean-centrality" selected' not in html


def test_an_asean_centrality_excerpt_keeps_its_codebook(seeded):  # noqa: F811
    from research_lab.method import seed
    seed.seed(seeded, seed.ASEAN)
    seeded.execute("INSERT INTO paper_projects(paper_id, project_slug) VALUES ('tan2023-convenes', 'asean-centrality')")
    eid = make(seeded, QUOTE, page_index=0, file_id=1)
    assert ex.get(seeded, eid)["project_slug"] == "asean-centrality"
    vocab = prompt(seeded, eid).split("## Vocabularies")[1]
    assert "institutional-constructivist" in vocab and "ARF" in vocab and "anchor:camp:" in vocab


def test_the_association_example_names_no_topic():
    text = json.dumps(registry.get("excerpt.associate").example(), ensure_ascii=False)
    assert not [w for w in ("东盟", "ASEAN", "EAS", "centrality", "中心地位") if w in text], text


def test_a_copied_camp_placeholder_is_not_stored(conn):
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, created_at, updated_at) "
                 "VALUES ('p', 'T', 'h', '[]', '2026-10-21', '2026-10-21')")
    result = PS.PaperSummary.model_validate({**PS.EXAMPLE, "camp_guess": {**PS.EXAMPLE["camp_guess"]}})
    PS.PaperSummarize().apply(result, 0, conn, "p")
    stored = json.loads(conn.execute("SELECT ai_summary_json FROM papers WHERE id = 'p'").fetchone()[0])
    assert stored["camp_guess"]["camp"] is None and stored["direction"]["slug"] is None
