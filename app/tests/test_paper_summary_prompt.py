"""Card 0201 (Leo 10-21: 「摘要的 prompt 可能默认关联东盟，去掉对特定话题的回应」): a paper outside every project is
summarized without the current project's question, 概念用法 is the paper's own concept, and neither the summary nor
the push example carries a topic of its own."""
from __future__ import annotations

import json

import pytest

from research_lab.ai import registry
from research_lab.ai.jobs import paper_summarize as PS
from research_lab.feeds import enrich

from .test_paper_summary_light import REAL_FIRST, SECOND, add_paper

TOPIC = ("东盟", "ASEAN", "asean", "centrality", "中心地位", "East Asia")


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    from research_lab.ai import dispatch
    monkeypatch.setattr(dispatch, "dispatch", lambda job_id: "queued")


@pytest.fixture()
def now_on_a_project(repo):
    (repo / "_data" / "now.yml").write_text(
        "question: What explains the persistence of ASEAN institutional centrality?\nproject: asean-centrality\n"
        "puzzle: ASEAN centrality survives despite wide variation in members' commitment.\n", encoding="utf-8")


def prompt(conn, pid: str) -> str:
    with PS.forced_mode("full"):
        req, _ = registry.get("paper.summarize").build(pid, conn)
    return req.messages[1]["content"].split("# MATERIAL")[0]


def test_a_paper_outside_every_project_gets_no_project_and_no_topic(conn, now_on_a_project):
    pid = add_paper(conn, "cyn", [REAL_FIRST.replace("ASEAN", "the forum"), SECOND], title="Structural Cynicism")
    user = prompt(conn, pid)
    assert "Project: none assigned. Leave relevance_to_project null." in user
    assert "Current puzzle" not in user and "Project question" not in user
    head = user.split("Research directions")[0] + user.split("Rules:")[1]
    assert not [w for w in TOPIC if w in head], head
    assert "the paper's own central concept" in user and "Never write what it does not discuss" in user


def test_a_paper_in_the_current_project_still_gets_its_question(conn, now_on_a_project):
    pid = add_paper(conn, "linked", [REAL_FIRST, SECOND])
    conn.execute("INSERT INTO paper_projects(paper_id, project_slug) VALUES (?, 'asean-centrality')", (pid,))
    user = prompt(conn, pid)
    assert "Project: " in user and "(asean-centrality)" in user and "Current puzzle: ASEAN centrality survives" in user


def test_the_examples_name_no_topic():
    for example in (PS.EXAMPLE, enrich.EXAMPLE):
        text = json.dumps(example, ensure_ascii=False)
        assert not [w for w in TOPIC if w in text], text
    assert not [w for w in TOPIC if w in PS.FIELD_GUIDE + "".join(PS.LANGUAGE_RULES)]
    PS.PaperSummary.model_validate(PS.EXAMPLE)
    enrich.FeedEnrichment.model_validate(enrich.EXAMPLE)
