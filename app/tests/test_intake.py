"""Intake module: pipeline, metadata, summary job, CLI and web routes."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

DOI = "10.1234/rl.2023.001"
TITLE = "Who Convenes Asia? Forum attendance and the durability of ASEAN-led institutions"


def crossref_hit_transport(calls: list[str] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        host, path = request.url.host, request.url.path
        if host == "api.crossref.org" and path == f"/works/{DOI}":
            return httpx.Response(200, json={"status": "ok", "message": {
                "DOI": DOI, "title": [TITLE], "author": [{"family": "Tan", "given": "Lin"}],
                "issued": {"date-parts": [[2023]]}, "container-title": ["The Pacific Review"], "type": "journal-article"}})
        if host == "api.openalex.org" and path == f"/works/doi:{DOI}":
            return httpx.Response(200, json={"id": "https://openalex.org/W42", "doi": f"https://doi.org/{DOI}",
                                             "display_name": TITLE, "publication_year": 2023,
                                             "abstract_inverted_index": {"Great": [0], "powers": [1], "keep": [2], "attending.": [3]}})
        return httpx.Response(404, json={"status": "error"})
    return httpx.MockTransport(handler)


def failing_transport() -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(500, text="upstream down"))


@pytest.fixture()
def full_summaries(repo):
    """These tests read the full-text summary; light reading is the default since card 0122."""
    from research_lab import tunables as TU
    from research_lab.db.connection import connect, migrate
    c = connect()
    migrate(c)
    TU.set_value(c, "papers.summary_mode", "full")
    c.close()


@pytest.fixture()
def http():
    with httpx.Client(transport=crossref_hit_transport()) as c:
        yield c


@pytest.fixture()
def ingested(conn, sample_pdf, http, full_summaries):
    from research_lab.intake import pipeline
    return pipeline.ingest(conn, sample_pdf, http=http)


def test_ingest_creates_paper_pages_and_job(conn, ingested):
    r = ingested
    assert r.created and r.duplicate_of is None and r.paper_id == "tan2023-convenes"
    paper = conn.execute("SELECT * FROM papers WHERE id = ?", (r.paper_id,)).fetchone()
    assert paper["doi"] == DOI
    assert paper["metadata_status"] == "verified"
    assert paper["title"] == TITLE and paper["year"] == 2023 and json.loads(paper["authors_json"]) == ["Tan, Lin"]
    assert paper["abstract"] == "Great powers keep attending." and paper["openalex_id"] == "W42"
    labels = [row[0] for row in conn.execute(
        "SELECT page_label FROM paper_pages pp JOIN paper_files f ON f.id = pp.file_id WHERE f.paper_id = ? ORDER BY page_index", (r.paper_id,))]
    assert labels == ["244", "245", "246"]
    f = conn.execute("SELECT * FROM paper_files WHERE paper_id = ?", (r.paper_id,)).fetchone()
    from research_lab.config import get_paths
    assert (get_paths().data / f["rel_path"]).exists() and f["rel_path"].startswith(f"papers/{r.paper_id}/")
    kinds = {row[0] for row in conn.execute("SELECT kind FROM paper_sections WHERE file_id = ?", (f["id"],))}
    assert {"abstract", "introduction", "conclusion", "references"} <= kinds
    # card 0130: an import joins no 课题 unless one is given; card 0202: nor takes the current project's direction
    assert conn.execute("SELECT 1 FROM paper_projects WHERE paper_id = ?", (r.paper_id,)).fetchone() is None
    assert paper["direction"] is None                                          # now.project is not a default
    job = conn.execute("SELECT * FROM ai_jobs WHERE id = ?", (r.job_id,)).fetchone()
    assert job["kind"] == "paper.summarize" and job["status"] == "queued" and job["target_id"] == r.paper_id
    req = json.loads(job["request_json"])
    user = req["messages"][1]["content"]
    assert "lapsed in 5 of 17 summits" in user and "=== p. 245 ===" in user
    assert "Acharya, A. 2001" not in user            # references never enter a prompt
    assert "Project: none assigned" in user and "asean-centrality" not in user     # card 0201: no 课题, no project
    files = json.loads(job["input_files_json"])
    assert files and Path(files[0]).read_text().count("=== p.") == 3
    assert conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] >= 2


def test_second_ingest_is_duplicate(conn, sample_pdf, http, ingested):
    from research_lab.intake import pipeline
    again = pipeline.ingest(conn, sample_pdf, http=http)
    assert not again.created and again.duplicate_of == ingested.paper_id and again.paper_id == ingested.paper_id
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1


def test_similar_title_is_held_then_attached(conn, sample_pdf, http, ingested, tmp_path):
    import pymupdf
    from research_lab.intake import pipeline
    doc = pymupdf.open(sample_pdf)
    doc[0].insert_text((72, 700), "Accepted manuscript", fontsize=8)
    other = tmp_path / "copy.pdf"
    doc.save(other)
    res = pipeline.ingest(conn, other, http=http)
    assert res.paper_id is None and res.duplicate_of == ingested.paper_id and res.pending and res.reason == "same DOI"
    pipeline.attach_pending(conn, res.pending, ingested.paper_id)
    assert conn.execute("SELECT COUNT(*) FROM paper_files WHERE paper_id = ?", (ingested.paper_id,)).fetchone()[0] == 2
    assert pipeline.pending_info(res.pending) is None


def test_non_pdf_rejected(conn, tmp_path):
    from research_lab.intake import pipeline
    bogus = tmp_path / "notes.pdf"
    bogus.write_text("just text, not a pdf")
    with pytest.raises(pipeline.RejectedFile):
        pipeline.ingest(conn, bogus, ai=False)
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.7\nthis is not really a pdf")
    with pytest.raises(pipeline.RejectedFile):
        pipeline.ingest(conn, broken, ai=False)
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 0


def test_encrypted_pdf_rejected(conn, tmp_path):
    import pymupdf
    from research_lab.intake import pipeline
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "secret")
    p = tmp_path / "locked.pdf"
    doc.save(p, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    with pytest.raises(pipeline.RejectedFile, match="已加密"):
        pipeline.ingest(conn, p, ai=False)


def test_crossref_500_leaves_paper_unverified(conn, sample_pdf):
    from research_lab.intake import pipeline
    with httpx.Client(transport=failing_transport()) as c:
        r = pipeline.ingest(conn, sample_pdf, http=c, ai=False)
    assert r.created and r.job_id is None
    paper = conn.execute("SELECT * FROM papers WHERE id = ?", (r.paper_id,)).fetchone()
    assert paper["metadata_status"] == "unverified"
    assert paper["title"].startswith("Who Convenes Asia?")
    assert conn.execute("SELECT COUNT(*) FROM paper_pages").fetchone()[0] == 3
    assert any("暂时连不上 api.crossref.org" in n for n in r.notes)
    # a later lookup with Crossref back up verifies it
    with httpx.Client(transport=crossref_hit_transport()) as c:
        res = pipeline.lookup(conn, r.paper_id, http=c)
    assert res.status == "verified"
    assert pipeline.get_paper(conn, r.paper_id)["metadata_status"] == "verified"


def test_search_candidates_when_no_doi(conn, tmp_path):
    import pymupdf
    from research_lab.intake import pipeline
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), "Hedging in Southeast Asia", fontsize=20)
    page.insert_text((72, 130), "Body text about hedging strategies of small states in 2019 and beyond. " * 1, fontsize=10)
    for _ in range(2):
        doc.new_page().insert_text((72, 72), "More body text on alignment choices and hedging. " * 3, fontsize=10)
    p = tmp_path / "hedging.pdf"
    doc.save(p)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("query.bibliographic", "").startswith("Hedging in Southeast Asia")
        return httpx.Response(200, json={"message": {"items": [
            {"DOI": "10.9/abc", "title": ["Hedging in Southeast Asia"], "author": [{"family": "Kuik", "given": "C."}],
             "issued": {"date-parts": [[2008]]}, "type": "journal-article", "score": 50.0},
            {"DOI": "10.9/xyz", "title": ["Something else entirely"], "issued": {"date-parts": [[2019]]}, "type": "book"}]}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        r = pipeline.ingest(conn, p, http=c, ai=False)
    paper = pipeline.get_paper(conn, r.paper_id)
    assert paper["metadata_status"] == "unverified"       # title matches but 2008 is not near a year in the PDF
    state = pipeline.lookup_state(paper)
    assert state["candidates"][0]["doi"] == "10.9/abc"
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(500))) as c:
        assert pipeline.accept_candidate(conn, r.paper_id, 0, http=c) == []
    paper = pipeline.get_paper(conn, r.paper_id)
    assert paper["metadata_status"] == "verified" and paper["doi"] == "10.9/abc" and paper["year"] == 2008


def valid_summary() -> dict:
    return {
        "research_question": {"text": "Why do great powers attend ASEAN forums?", "pages": ["244"]},
        "argument": {"text": "Mutual veto leaves ASEAN as the acceptable host.", "pages": [246]},
        "mechanism": "Mutual veto",
        "cases": ["EAS"], "period": {"text": "2005-2022", "pages": ["245"]},
        "method": {"text": "Attendance coding", "pages": []}, "level_of_analysis": {"text": "region"},
        "concept_usage": {"text": "Centrality as convening power"},
        "camp_guess": {"camp": "great-power-acceptance", "confidence": "Medium", "why": "Great-power preferences"},
        "contests": ["institutional inertia"], "cannot_explain_guess": {"text": "Agenda control"},
        "relevance_to_project": {"text": "Speaks directly to the project question.", "pages": ["245"]},
        "read_first": [{"page": 245, "why": "attendance data"}], "key_terms": ["convening power"],
    }


def test_summary_apply_sets_ai_summary_only(conn, ingested):
    from research_lab.ai import queue
    run_id = queue.complete(conn, ingested.job_id, valid_summary(), provider="claude-code", model="test")
    paper = conn.execute("SELECT * FROM papers WHERE id = ?", (ingested.paper_id,)).fetchone()
    data = json.loads(paper["ai_summary_json"])
    assert paper["ai_summary_run_id"] == run_id
    assert data["argument"]["pages"] == ["246"] and data["mechanism"]["text"] == "Mutual veto"
    assert data["camp_guess"]["confidence"] == "medium"
    assert paper["claim_text"] is None and paper["camp"] is None     # user-owned fields untouched


def test_summary_rejects_quote_fields(conn, ingested):
    from pydantic import ValidationError
    from research_lab.ai import queue
    bad = valid_summary()
    bad["argument"]["quote"] = "copied sentence"
    with pytest.raises(ValidationError):
        queue.complete(conn, ingested.job_id, bad, provider="claude-code", model="test")
    assert conn.execute("SELECT status FROM ai_jobs WHERE id = ?", (ingested.job_id,)).fetchone()[0] == "queued"


def test_adopt_and_user_fields(conn, ingested):
    from research_lab.ai import queue
    from research_lab.intake import pipeline
    queue.complete(conn, ingested.job_id, valid_summary(), provider="claude-code", model="test")
    pipeline.adopt(conn, ingested.paper_id, "claim_text")
    pipeline.adopt(conn, ingested.paper_id, "camp")
    pipeline.update_user_fields(conn, ingested.paper_id, {"read_depth": "skimmed"}, projects=["asean-centrality", "institutional-reassurance"])
    p = pipeline.get_paper(conn, ingested.paper_id)
    assert p["claim_text"] == "Mutual veto leaves ASEAN as the acceptable host." and p["camp"] == "great-power-acceptance"
    assert p["read_depth"] == "skimmed"
    assert conn.execute("SELECT COUNT(*) FROM paper_projects WHERE paper_id = ?", (ingested.paper_id,)).fetchone()[0] == 2
    with pytest.raises(ValueError):
        pipeline.update_user_fields(conn, ingested.paper_id, {"read_depth": "devoured"})


def test_long_paper_uses_outline(conn, tmp_path):
    import pymupdf
    from research_lab.intake import pipeline
    from research_lab.ai import registry
    doc = pymupdf.open()
    filler = ("Hedging and alignment in Southeast Asian diplomacy across forums and decades. " * 9 + "\n") * 40
    for i in range(40):
        page = doc.new_page()
        head = "Introduction\n" if i == 1 else "Conclusion\n" if i == 38 else "References\n" if i == 39 else ""
        page.insert_textbox(page.rect + (40, 40, -40, -40), head + filler, fontsize=4)
    p = tmp_path / "long.pdf"
    doc.save(p)
    with httpx.Client(transport=failing_transport()) as c:
        r = pipeline.ingest(conn, p, http=c, ai=False)
    mode, text = registry.get("paper.summarize").material(conn, r.paper_id)
    assert mode.startswith("outline")
    assert "## Introduction" in text and "## Conclusion" in text
    assert len(text) < 60_000


# ---------------------------------------------------------------- web

@pytest.fixture()
def web_paper(client, http, full_summaries):
    from research_lab.db.connection import connect
    from research_lab.intake import pipeline
    client.app.state.intake_http = http
    c = connect()
    yield c
    c.close()


def test_upload_route_and_pages(client, web_paper, sample_pdf, tmp_path):
    with open(sample_pdf, "rb") as fh:
        r = client.post("/papers", files=[("files", ("tan2023.pdf", fh, "application/pdf"))], follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/papers/tan2023-convenes"
    bogus = tmp_path / "x.pdf"
    bogus.write_text("hello")
    with open(sample_pdf, "rb") as a, open(bogus, "rb") as b:
        r = client.post("/papers", files=[("files", ("tan2023.pdf", a, "application/pdf")), ("files", ("x.pdf", b, "application/pdf"))],
                        follow_redirects=False)
    loc = r.headers["location"]
    assert "dup=tan2023-convenes" in loc and "rejected=x.pdf" in loc

    listing = client.get("/papers?view=all")
    assert listing.status_code == 200 and "Who Convenes Asia?" in listing.text and 'id="dropzone"' in listing.text
    assert "Who Convenes Asia?" in client.get("/papers?filter=unsummarized&q=attendance").text
    assert "Who Convenes Asia?" not in client.get("/papers?filter=unverified").text

    detail = client.get("/papers/tan2023-convenes")
    assert detail.status_code == 200
    assert "概要已排队" in detail.text and "10.1234/rl.2023.001" in detail.text
    assert "我的判断" not in detail.text and "文献地图" in detail.text          # judgments live on the map (card 0036)

    read = client.get("/papers/tan2023-convenes/read?page=245")
    assert read.status_code == 200
    assert 'id="pdf-viewer"' in read.text and 'data-initial-page="1"' in read.text
    assert "/static/paper-reader.js" in read.text and "/static/excerpts.js" not in read.text

    png = client.get("/papers/tan2023-convenes/page/1.png")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png" and png.content[:4] == b"\x89PNG"
    assert client.get("/papers/tan2023-convenes/page/9.png").status_code == 404

    s = client.get("/search?q=attendance")
    assert s.status_code == 200 and "<mark>attendance</mark>" in s.text and "<h2>正文</h2>" in s.text
    assert client.get('/search?q="unbalanced').status_code == 200


def test_detail_mutations(client, web_paper, sample_pdf, http):
    from research_lab.ai import queue
    from research_lab.intake import pipeline
    r = pipeline.ingest(web_paper, sample_pdf, http=http)
    queue.complete(web_paper, r.job_id, valid_summary(), provider="claude-code", model="test")
    card = client.post(f"/papers/{r.paper_id}/adopt/mechanism")
    assert card.status_code == 200 and "已复制到“机制”" in card.text and "核对与整理" in card.text
    saved = client.post(f"/papers/{r.paper_id}/fields", data={"projects_present": "1", "projects": ["us-rok-reassurance"],
                                                              "read_depth": "read", "camp": "my-camp"})
    assert "已保存。" in saved.text
    p = pipeline.get_paper(web_paper, r.paper_id)
    assert p["read_depth"] == "read" and p["camp"] == "my-camp" and p["mechanism"] == "Mutual veto"
    meta = client.post(f"/papers/{r.paper_id}/metadata", data={"title": "Who Convenes Asia?", "authors": "Tan, Lin\nLee, K.",
                                                                "year": "2023", "venue": "TPR", "kind": "article", "doi": "10.1234/rl.2023.001"})
    assert "已保存。" in meta.text and "Lee, K." in meta.text
    assert pipeline.get_paper(web_paper, r.paper_id)["metadata_status"] == "verified"
    again = client.post(f"/papers/{r.paper_id}/summarize")
    assert "概要已排队" in again.text
    assert pipeline.latest_summary_job(web_paper, r.paper_id)["status"] == "queued"
    client.headers.pop("HX-Request")
    assert client.post(f"/papers/{r.paper_id}/summarize").status_code == 403


def test_reflow_joins_hard_line_breaks():
    from research_lab.excerpts import normalize
    from research_lab.intake.extract import reflow

    page = ("1. Introduction\n"
            "Between 2005 and 2022 US leader-level attendance at the East Asia Summit lapsed in 5 of 17\n"
            "summits, yet no participant proposed moving the chair outside ASEAN. The institu-\n"
            "tions of the ASEAN-\n"
            "led order are the subject of this paper, which codes attendance for three forums.\n\n"
            "2. Argument\n"
            "中国的周边外交政策在过去十年中发生了显著变化，这一变化首先体现在对区域机制的参与和\n"
            "多个方面。")
    out = reflow(page)
    assert out.splitlines()[0] == "1. Introduction"
    assert "5 of 17 summits" in out and "institutions of the ASEAN-led order" in out
    assert "参与和多个方面" in out
    assert "\n\n2. Argument\n" in out
    # A passage selected from the reflowed text still matches the stored page text.
    assert normalize("5 of 17 summits, yet no participant") in normalize(page)


def test_reflow_double_spaced_pages_keep_their_paragraphs():
    """RSIS-style PDFs put a blank line after every line and two after a paragraph (card 0030)."""
    from research_lab.excerpts import normalize
    from research_lab.intake.extract import paragraphs, reflow
    page = ("             China’s Emerging Disaster Diplomacy:\n\n             What It Means for Southeast Asia\n\n"
            "                              by Lina Gong\n\n\nSYNOPSIS\n\n\n"
            "The Chinese government formed two new bodies in 2018 that have been expected to\n\n"
            "improve China’s response to natural hazards and humanitarian emergencies in other\n\n"
            "countries. What are the implications for Southeast Asia, where the risk and threat of\n\n"
            "different types of disaster persist?\n\n\nCOMMENTARY\n\n\n"
            "THE CHINESE government has carried out extensive institutional restructuring since\n\n"
            "March 2018, which includes the establishment of the Ministry of Emergency\n\n"
            "Management and China International Development Cooperation Agency. The two\n\n"
            "institutions are expected to improve China’s ability to respond to natural hazards and\n\n"
            "humanitarian emergencies in other countries.\n\n\n"
            "Disaster relief and humanitarian assistance has become more relevant to China’s\n\n"
            "diplomacy as the country seeks a greater role in global governance and its grand\n\n"
            "developmental initiative, the Belt and Road, passes through areas vulnerable to\n\n"
            "different types of disaster.\n")
    paras = paragraphs(page)
    assert paras[3] == "SYNOPSIS" and paras[5] == "COMMENTARY"
    assert paras[4].startswith("The Chinese government formed two new bodies") and paras[4].endswith("disaster persist?")
    assert paras[6].startswith("THE CHINESE government") and paras[6].endswith("in other countries.")
    assert paras[7].startswith("Disaster relief") and len(paras) == 8
    assert normalize("expected to improve China’s response") in normalize(page)
    assert reflow(page).count("\n\n") == 7


def test_reflow_single_spaced_pages_split_on_indent_and_lists():
    """JSTOR scans mark paragraphs only by a first-line indent; numbered items start their own line."""
    from research_lab.intake.extract import paragraphs
    page = ("                                            War  381\n\n\n"
            "length below, these arguments are given the following labels: (1) anarchy; (2)\n"
            "expected benefits greater than expected costs; (3) rational preventive war. I argue that the first three\n"
            "arguments simply do not address the question of what prevents state leaders\n"
            "from bargaining to a settlement that would avoid the costs of fighting.\n"
            "   If these standard arguments do not resolve the puzzle on rationalist terms,\n"
            "what does? I propose that there are three defensible answers, which take the\n"
            "form of general mechanisms, or causal logics, that operate in a variety of more\n"
            "specific international contexts.\n"
            "1. Leaders know things about their military capabilities that other states do not,\n"
            "and in bargaining situations they can have incentives to misrepresent them.\n"
            "2. Commitment problems arise when no third party can enforce an agreement that\n"
            "both sides would otherwise prefer to a costly war.\n")
    paras = paragraphs(page)
    assert paras[0] == "War  381"
    assert paras[1].startswith("length below") and paras[1].endswith("costs of fighting.")
    assert paras[2].startswith("If these standard arguments") and paras[2].endswith("international contexts.")
    assert paras[3].startswith("1. Leaders know") and paras[3].endswith("misrepresent them.")
    assert paras[4].startswith("2. Commitment problems") and len(paras) == 5


def test_reflow_keeps_hanging_indents_and_cjk_double_spacing():
    """Reference lists wrap with a hanging indent inside one entry; Chinese double-spaced pages join without spaces."""
    from research_lab.intake.extract import paragraphs
    refs = ("Acharya, Amitav. 2001. Constructing a Security Community in Southeast Asia: ASEAN and the Problem of\n"
            "     Regional Order. London: Routledge.\n"
            "Goh, Evelyn. 2013. The Struggle for Order: Hegemony, Hierarchy, and Transition in Post-Cold War East\n"
            "     Asia. Oxford: Oxford University Press.\n")
    paras = paragraphs(refs)
    assert len(paras) == 2 and paras[0].endswith("London: Routledge.") and paras[1].startswith("Goh, Evelyn")
    zh = ("一、地区秩序的变化\n\n"
          "中国的周边外交政策在过去十年中发生了显著变化，这一变化首先体现在对区域机制的参与和\n\n"
          "投入的加深，其次体现在对周边国家关切的回应方式上，第三则体现在对多边场合议程设置的\n\n"
          "主动性上。\n\n\n"
          "二、周边环境的演变态势\n\n"
          "东南亚国家在大国竞争加剧的背景下普遍采取对冲策略，既不愿在安全上完全倒向一方，也不愿\n\n"
          "在经济上失去任何一方的机会。\n")
    paras = paragraphs(zh)
    assert paras[0] == "一、地区秩序的变化" and paras[2] == "二、周边环境的演变态势"
    assert paras[1] == "中国的周边外交政策在过去十年中发生了显著变化，这一变化首先体现在对区域机制的参与和投入的加深，其次体现在对周边国家关切的回应方式上，第三则体现在对多边场合议程设置的主动性上。"
    assert paras[3].startswith("东南亚国家") and paras[3].endswith("机会。") and len(paras) == 4


# ---------------------------------------------------------------- card 0035: filing on import, 补 PDF, batch summaries

def _biblio(conn, pid="tan2023-who", title="Who Convenes Asia? A bibliography-only record"):
    from research_lab.db.connection import now_iso
    ts = now_iso()
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, created_at, updated_at) "
                 "VALUES (?, ?, ?, '[\"Tan, L.\"]', 2023, ?, ?)", (pid, title, "h-" + pid, ts, ts))
    return pid


def test_new_papers_get_a_direction(conn, sample_pdf, http):
    """A new paper's 方向: the one given, else its 课题's, else none; the current project is no default (card 0202)."""
    from research_lab.intake import pipeline
    r = pipeline.ingest(conn, sample_pdf, http=http)
    assert pipeline.get_paper(conn, r.paper_id)["direction"] is None                        # now.project = asean-centrality
    conn.execute("DELETE FROM papers WHERE id = ?", (r.paper_id,))
    r = pipeline.ingest(conn, sample_pdf, http=http, project="us-rok-reassurance")
    assert pipeline.get_paper(conn, r.paper_id)["direction"] == "alliance-us"
    conn.execute("DELETE FROM papers WHERE id = ?", (r.paper_id,))
    r = pipeline.ingest(conn, sample_pdf, http=http, direction="periphery-nts")
    assert pipeline.get_paper(conn, r.paper_id)["direction"] == "periphery-nts"
    conn.execute("DELETE FROM papers WHERE id = ?", (r.paper_id,))
    r = pipeline.ingest(conn, sample_pdf, http=http, direction="made-up")
    assert pipeline.get_paper(conn, r.paper_id)["direction"] is None                        # an unknown slug is dropped


def test_import_is_filed_where_the_pdf_was_dropped(client, web_paper, sample_pdf):
    from research_lab.intake import pipeline
    from research_lab.web.routes.papers import import_target
    assert import_target("/projects/us-rok-reassurance?tab=papers") == {"project": "us-rok-reassurance", "direction": None, "paper": None}
    assert import_target("http://127.0.0.1:4180/papers?direction=periphery-nts")["direction"] == "periphery-nts"
    assert import_target("/papers?direction=none")["direction"] is None
    assert import_target("/papers?direction=_none")["direction"] is None
    assert import_target("/papers/goh2013-struggle")["paper"] == "goh2013-struggle"
    with open(sample_pdf, "rb") as fh:
        r = client.post("/papers", data={"page": "/projects/us-rok-reassurance"},
                        files=[("files", ("tan2023.pdf", fh, "application/pdf"))], follow_redirects=False)
    pid = r.headers["location"].rsplit("/", 1)[-1]
    p = pipeline.get_paper(web_paper, pid)
    assert p["direction"] == "alliance-us"
    assert web_paper.execute("SELECT 1 FROM paper_projects WHERE paper_id = ? AND project_slug = 'us-rok-reassurance'", (pid,)).fetchone()
    web_paper.execute("DELETE FROM papers WHERE id = ?", (pid,))
    with open(sample_pdf, "rb") as fh:                              # no page field: the Referer decides
        r = client.post("/papers", files=[("files", ("tan2023.pdf", fh, "application/pdf"))],
                        headers={"Referer": "http://127.0.0.1:4100/papers?direction=periphery-nts"}, follow_redirects=False)
    assert pipeline.get_paper(web_paper, r.headers["location"].rsplit("/", 1)[-1])["direction"] == "periphery-nts"
    page = client.get("/projects/asean-centrality").text
    assert 'name="page" value="/projects/asean-centrality"' in page             # the global drop form carries it


def test_bibliography_record_takes_a_dropped_pdf(client, web_paper, sample_pdf, tmp_path):
    """补 PDF: dropped on the record's page (or chosen with its button) it becomes the primary file,
    the pages appear and a summary is queued; no duplicate question."""
    from research_lab.intake import pipeline
    pid = _biblio(web_paper)
    with open(sample_pdf, "rb") as fh:
        r = client.post("/papers", data={"page": f"/papers/{pid}"}, files=[("files", ("tan.pdf", fh, "application/pdf"))],
                        follow_redirects=False)
    assert r.headers["location"] == f"/papers/{pid}?note=pdf-added"
    f = pipeline.primary_file(web_paper, pid)
    assert f is not None and f["is_primary"] == 1 and f["original_name"] == "tan.pdf"
    assert pipeline.get_paper(web_paper, pid)["language"] == "en"
    assert web_paper.execute("SELECT status FROM ai_jobs WHERE kind = 'paper.summarize' AND target_id = ?", (pid,)).fetchone()
    assert web_paper.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1          # no second paper was created
    done = client.get(f"/papers/{pid}?note=pdf-added").text
    assert 'id="pdf-viewer"' in done and "已补上 PDF" in done and "只有书目" not in done
    # the same file again, or a file another paper owns, is refused with a reason
    with open(sample_pdf, "rb") as fh:
        again = client.post(f"/papers/{pid}/files", files=[("files", ("tan.pdf", fh, "application/pdf"))], follow_redirects=False)
    assert "pdf_error=" in again.headers["location"]
    other = _biblio(web_paper, "goh2013-struggle", "The Struggle for Order")
    with open(sample_pdf, "rb") as fh:
        taken = client.post(f"/papers/{other}/files", files=[("files", ("tan.pdf", fh, "application/pdf"))], follow_redirects=False)
    assert "pdf_error=" in taken.headers["location"] and pipeline.primary_file(web_paper, other) is None
    shown = client.get(taken.headers["location"]).text
    assert "没有补上 PDF" in shown and "已经属于文献库里的另一篇" in shown
    assert 'action="/papers/goh2013-struggle/files"' in shown and "添加 PDF" in shown and "是同一篇" not in shown
    bogus = tmp_path / "x.pdf"
    bogus.write_text("hello")
    with open(bogus, "rb") as fh:
        bad = client.post(f"/papers/{other}/files", files=[("files", ("x.pdf", fh, "application/pdf"))], follow_redirects=False)
    assert "pdf_error=" in bad.headers["location"]
    assert client.post("/papers/no-such/files").status_code == 404


def test_same_paper_answer_gives_a_bibliography_record_its_primary_file(conn, sample_pdf, http):
    """The duplicate flow (是同一篇) on a record without a PDF also makes it the primary file and queues a summary."""
    from research_lab.intake import pipeline
    pid = _biblio(conn, "tan2023-convenes-b", "Who Convenes Asia? Forum attendance and the durability of ASEAN-led institutions")
    r = pipeline.ingest(conn, sample_pdf, http=http)
    assert r.pending and r.duplicate_of == pid and r.reason == "similar title"     # held for the user's answer
    pipeline.attach_pending(conn, r.pending, pid)
    f = pipeline.primary_file(conn, pid)
    assert f["is_primary"] == 1
    assert conn.execute("SELECT 1 FROM ai_jobs WHERE kind = 'paper.summarize' AND target_id = ?", (pid,)).fetchone()


def test_project_page_queues_missing_summaries(client, web_paper, sample_pdf):
    from research_lab.intake import pipeline
    with open(sample_pdf, "rb") as fh:
        client.post("/papers", data={"page": "/projects/asean-centrality"}, files=[("files", ("tan.pdf", fh, "application/pdf"))])
    pid = web_paper.execute("SELECT id FROM papers").fetchone()[0]
    web_paper.execute("UPDATE ai_jobs SET status = 'cancelled' WHERE target_id = ?", (pid,))   # as if the first try was stopped
    bib = _biblio(web_paper, "goh2013-struggle", "The Struggle for Order")
    web_paper.execute("INSERT INTO paper_projects(paper_id, project_slug) VALUES (?, 'asean-centrality')", (bib,))
    page = client.get("/projects/asean-centrality").text
    assert "1 篇还没有中文概要（另有 1 篇只有书目" in page and 'action="/papers/summarize"' in page
    r = client.post("/papers/summarize", data={"project": "asean-centrality", "next": "/projects/asean-centrality?tab=papers"},
                    follow_redirects=False)
    loc = r.headers["location"]
    assert loc.startswith("/projects/asean-centrality?tab=papers&") and "summarized=1" in loc and "summary_no_file=1" in loc
    assert web_paper.execute("SELECT COUNT(*) FROM ai_jobs WHERE kind = 'paper.summarize' AND target_id = ? AND status = 'queued'", (pid,)).fetchone()[0] == 1
    again = client.post("/papers/summarize", data={"project": "asean-centrality"}, follow_redirects=False)
    assert "summarized=0" in again.headers["location"] and "summary_waiting=1" in again.headers["location"]
    assert "已排入 1 篇的中文概要" in client.get(loc).text and "补上 PDF 后会自动生成" in client.get(loc).text
    assert client.post("/papers/summarize", data={"project": "nope"}).status_code == 400
    assert pipeline.get_paper(web_paper, pid)["direction"] == "sea-asean"


def _other_pdf(tmp_path, text="Umbrella states and their patron\nA different paper entirely.\n"):
    import pymupdf
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text, fontsize=11)
    p = tmp_path / "right.pdf"
    doc.save(p)
    doc.close()
    return p


def test_replace_primary_file_swaps_the_text_and_clears_the_summary(conn, ingested, tmp_path):
    from research_lab.config import get_paths
    from research_lab.intake import pipeline
    pid = ingested.paper_id
    conn.execute("UPDATE papers SET ai_summary_json = '{}' WHERE id = ?", (pid,))
    old = pipeline.primary_file(conn, pid)
    file_id, job = pipeline.replace_primary_file(conn, _other_pdf(tmp_path), pid, ai=False)
    files = conn.execute("SELECT * FROM paper_files WHERE paper_id = ?", (pid,)).fetchall()
    assert [f["id"] for f in files] == [file_id] and files[0]["is_primary"] == 1 and files[0]["page_count"] == 1 and job is None
    assert conn.execute("SELECT COUNT(*) FROM paper_pages WHERE file_id = ?", (old["id"],)).fetchone()[0] == 0
    assert "Umbrella" in conn.execute("SELECT text FROM paper_pages WHERE file_id = ?", (file_id,)).fetchone()[0]
    assert conn.execute("SELECT ai_summary_json FROM papers WHERE id = ?", (pid,)).fetchone()[0] is None
    assert not (get_paths().data / old["rel_path"]).exists() and (get_paths().data / files[0]["rel_path"]).exists()
    with pytest.raises(ValueError):        # the same file again
        pipeline.replace_primary_file(conn, tmp_path / "right.pdf", pid, ai=False)


def test_remove_file_promotes_the_next_and_highlights_hold_it(conn, ingested, tmp_path):
    from research_lab.intake import pipeline
    pid = ingested.paper_id
    old = pipeline.primary_file(conn, pid)
    second, _ = pipeline.attach_file(conn, _other_pdf(tmp_path), pid, ai=False)
    pipeline.remove_file(conn, pid, int(old["id"]))
    now = pipeline.primary_file(conn, pid)
    assert now["id"] == second and now["is_primary"] == 1
    assert conn.execute("SELECT COUNT(*) FROM paper_files WHERE paper_id = ?", (pid,)).fetchone()[0] == 1
    conn.execute("INSERT INTO excerpts(id, paper_id, file_id, page_index, locator, quote, my_note, created_at, updated_at) "
                 "VALUES ('EXC-2026-9001', ?, ?, 0, 'p.1', 'Umbrella states', 'n', '2026-01-01', '2026-01-01')", (pid, second))
    with pytest.raises(ValueError):
        pipeline.remove_file(conn, pid, second)
    with pytest.raises(ValueError):
        pipeline.replace_primary_file(conn, ingested_path(tmp_path), pid, ai=False)
    assert conn.execute("SELECT COUNT(*) FROM paper_files WHERE paper_id = ?", (pid,)).fetchone()[0] == 1


def ingested_path(tmp_path):
    return _other_pdf(tmp_path / "x" if (tmp_path / "x").mkdir(exist_ok=True) is None else tmp_path, "Another text\n")
