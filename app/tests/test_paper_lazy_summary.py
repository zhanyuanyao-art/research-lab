"""Card 0156: a paper the feed brought in has no summary; opening its reader queues one light summary
(papers.summary_mode), once. A reload queues nothing, a summary already there means nothing, and list rows never
queue anything."""
from __future__ import annotations

import httpx
import pytest


@pytest.fixture()
def bare_paper(conn, sample_pdf):
    """A paper with its PDF and no summary, as a feed import leaves it (ai=False)."""
    from research_lab.intake import pipeline
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500, text="down"))) as http:
        r = pipeline.ingest(conn, sample_pdf, http=http, ai=False)
    assert conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0] == 0
    return r.paper_id


def _summary_jobs(conn, pid):
    from research_lab.intake import pipeline
    return conn.execute("SELECT status, request_json FROM ai_jobs WHERE kind = ? AND target_id = ?",
                        (pipeline.SUMMARY_KIND, pid)).fetchall()


def test_opening_the_reader_queues_one_light_summary_and_a_reload_none(client, conn, bare_paper):
    assert client.get("/papers").status_code == 200                       # the list never asks for summaries
    assert _summary_jobs(conn, bare_paper) == []
    assert client.get(f"/papers/{bare_paper}").status_code == 200
    jobs = _summary_jobs(conn, bare_paper)
    assert len(jobs) == 1 and jobs[0]["status"] == "queued" and '"summary_mode": "light"' in jobs[0]["request_json"]
    assert client.get(f"/papers/{bare_paper}").status_code == 200         # reload
    assert client.get(f"/papers/{bare_paper}/read").status_code == 200    # the old reader link is the same page
    assert len(_summary_jobs(conn, bare_paper)) == 1


def test_a_paper_with_a_summary_or_without_a_file_queues_nothing(client, conn, bare_paper):
    conn.execute("UPDATE papers SET ai_summary_json = ? WHERE id = ?", ('{"mode": "light"}', bare_paper))
    client.get(f"/papers/{bare_paper}")
    assert _summary_jobs(conn, bare_paper) == []
    from research_lab.db.connection import now_iso
    ts = now_iso()
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, created_at, updated_at) "
                 "VALUES ('nofile2020-x', 'Only the citation', 'h-nofile', '[]', 2020, ?, ?)", (ts, ts))
    client.get("/papers/nofile2020-x")
    assert _summary_jobs(conn, "nofile2020-x") == []
