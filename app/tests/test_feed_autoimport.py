"""推送论文直接入库 (cards 0073, 0156): right after Leo's own refresh, the articles that arrived that day with a PDF to
download enter the library, picks first, at most 15, without any AI; nothing runs on a page view, the timer or the
8 o'clock refresh, and the backlog is left alone. Each is tried once a day; the 精选 says which can be had; the reader
says where a paper came from."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

DOI = "10.1234/rl.2023.001"     # printed in the sample PDF
TITLE = "Who Convenes Asia? Forum attendance and the durability of ASEAN-led institutions"


def _import_transport(pdf_bytes: bytes, calls: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        host, path = request.url.host, request.url.path
        if host == "oa.example.org":
            return httpx.Response(200, content=pdf_bytes, headers={"content-type": "application/pdf"})
        if host == "api.crossref.org" and path == f"/works/{DOI}":
            return httpx.Response(200, json={"status": "ok", "message": {
                "DOI": DOI, "title": [TITLE], "author": [{"family": "Tan", "given": "Lin"}],
                "issued": {"date-parts": [[2023]]}, "container-title": ["The Pacific Review"], "type": "journal-article"}})
        if host == "api.unpaywall.org":
            return httpx.Response(200, json={"is_oa": False, "best_oa_location": None})
        return httpx.Response(404, json={})
    return httpx.MockTransport(handler)


def _paper(conn, pid, *, doi, pdf_url=None, is_oa=None, score=4, title=None, days_ago=0, kind="article", status="new",
           minutes_ago=1):
    """A feed paper; by default it arrived a minute ago (today, Beijing time, except in the first minute of the day)."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago, minutes=minutes_ago)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO feed_papers(id, doi, journal, journal_name, title, authors_json, published, first_seen, score, "
                 "direction, oa_pdf_url, is_oa, kind, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (pid, doi, "TPR", "The Pacific Review", title or f"Paper {pid}", '["Lin Tan"]', ts[:10], ts, score,
                  "sea-asean", pdf_url, is_oa, kind, status))


def _oa(conn, pid, **kw):
    _paper(conn, pid, doi=kw.pop("doi", f"10.9/{pid}"), pdf_url=f"https://oa.example.org/{pid}.pdf", is_oa=1, **kw)


def test_only_todays_downloadable_articles_are_imported_and_without_ai(conn, sample_pdf):
    from research_lab.feeds import importer
    _paper(conn, "oa", doi=DOI, pdf_url="https://oa.example.org/tan2023.pdf", is_oa=1, title=TITLE)
    _oa(conn, "backlog", days_ago=2)                                             # arrived before today: left alone
    _paper(conn, "paywalled", doi="10.9/closed", is_oa=0)
    _paper(conn, "unknown", doi="10.9/unknown")                                  # Unpaywall has not said: not tried
    _oa(conn, "review", kind="review")                                           # book reviews are Leo's call
    _oa(conn, "dismissed", status="dismissed")
    assert importer.autoimport_pending(conn) == ["oa"]
    calls: list[str] = []
    with httpx.Client(transport=_import_transport(sample_pdf.read_bytes(), calls)) as http:
        results = importer.autoimport(conn, http=http)
    rows = {r["id"]: r["paper_id"] for r in conn.execute("SELECT id, paper_id FROM feed_papers")}
    assert rows["oa"] and not any(rows[k] for k in ("backlog", "paywalled", "unknown", "review", "dismissed"))
    assert [bool(r.paper_id) for r in results] == [True]
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1                # no 待补原文 record
    assert conn.execute("SELECT COUNT(*) FROM paper_files").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0] == 0               # no summary, no translation
    assert not any(h in c for c in calls for h in ("closed", "backlog", "review", "dismissed"))
    with httpx.Client(transport=_import_transport(sample_pdf.read_bytes(), calls)) as http:
        assert importer.autoimport(conn, http=http) == []                               # tried once a day


def test_picks_go_first_then_the_newest_and_a_run_takes_fifteen(conn, monkeypatch):
    from research_lab.feeds import digest, importer
    for i in range(20):
        _oa(conn, f"p{i:02d}", minutes_ago=i + 1)                                    # p00 the newest
    _oa(conn, "old-pick", days_ago=3)
    monkeypatch.setattr(digest, "fill", lambda conn, *a, **k: {"papers": ["p17", "old-pick", "p05"]})
    pending = importer.autoimport_pending(conn)
    assert len(pending) == importer.AUTO_CAP == 15
    assert pending[:2] == ["p17", "p05"]                       # today's picks in their order; an old pick is backlog
    assert pending[2:5] == ["p00", "p01", "p02"] and "old-pick" not in pending


def test_auto_import_can_be_turned_off_and_a_page_view_imports_nothing(client, conn, monkeypatch):
    from research_lab.db.connection import set_setting
    from research_lab.feeds import importer
    _oa(conn, "oa")
    conn.commit()
    started = []
    monkeypatch.setattr(importer, "_autoimport_run", lambda http=None: started.append(http) or importer._auto_lock.release())
    assert importer.autoimport_pending(conn) == ["oa"]
    assert not hasattr(importer, "autoimport_on_view")         # gone with card 0156's change: views import nothing
    assert client.get("/feed?tab=papers").status_code == 200 and client.get("/").status_code == 200
    assert started == [] and conn.execute("SELECT paper_id FROM feed_papers WHERE id = 'oa'").fetchone()[0] is None
    set_setting(conn, importer.AUTO_KEY, False)
    assert importer.autoimport_pending(conn) == []


def _record_imports(monkeypatch) -> list:
    from research_lab.feeds import importer
    calls: list = []
    monkeypatch.setattr(importer, "autoimport_after_refresh", lambda http=None: calls.append(http) or True)
    return calls


def test_only_leos_own_refresh_imports_not_the_8_oclock_one_or_the_timer(conn, monkeypatch):
    from research_lab.feeds import scheduler
    calls = _record_imports(monkeypatch)
    monkeypatch.setattr(scheduler, "run_today", lambda http=None, **kw: {"kind": kw.get("kind")})
    # the real run_refresh clears the running flag on its way out; the stand-in must too
    monkeypatch.setattr(scheduler, "run_refresh", lambda http=None, **kw: scheduler._running.clear() or {"refreshed": True})
    assert scheduler.start_today(conn, manual=False) == "started"                        # 8 点的自动刷新
    scheduler.wait()
    assert scheduler.start_background() is True                                          # timer / page-view refresh
    scheduler.wait()
    assert calls == []
    assert scheduler.start_today(conn, manual=True) == "started"                         # 首页的刷新今天
    scheduler.wait()
    assert scheduler.start_background(manual=True) is True                               # 推送页的刷新
    scheduler.wait()
    assert len(calls) == 2


def test_a_refresh_that_did_not_run_imports_nothing(conn, monkeypatch):
    from research_lab.feeds import scheduler
    calls = _record_imports(monkeypatch)
    monkeypatch.setattr(scheduler, "run_today", lambda http=None, **kw: None)            # another process was refreshing
    scheduler.start_today(conn, manual=True)
    scheduler.wait()
    assert calls == []


def test_the_refresh_buttons_import_todays_downloadable_articles(client, conn, monkeypatch, sample_pdf):
    """End to end through POST /feed/refresh-today: the refresh (stubbed, no network), then the import thread."""
    from research_lab.feeds import importer, scheduler
    _paper(conn, "oa", doi=DOI, pdf_url="https://oa.example.org/tan2023.pdf", is_oa=1, title=TITLE)
    _oa(conn, "backlog", days_ago=2)
    conn.commit()
    monkeypatch.setattr(scheduler, "refresh_today", lambda conn, http=None, **kw: {"kind": kw.get("kind")})
    calls: list[str] = []
    client.app.state.feeds_http = httpx.Client(transport=_import_transport(sample_pdf.read_bytes(), calls))
    try:
        assert client.post("/feed/refresh-today").status_code == 200
        scheduler.wait()
        importer.wait_autoimport()
    finally:
        client.app.state.feeds_http.close()
        client.app.state.feeds_http = None
    rows = {r["id"]: r["paper_id"] for r in conn.execute("SELECT id, paper_id FROM feed_papers")}
    assert rows["oa"] and rows["backlog"] is None
    assert conn.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0] == 0
    monkeypatch.setattr(scheduler, "run_refresh", lambda http=None, **kw: scheduler._running.clear() or {"refreshed": True})
    calls2 = _record_imports(monkeypatch)
    client.post("/feed/refresh")                                                         # 推送页的刷新
    scheduler.wait()
    assert len(calls2) == 1


def test_the_digest_says_which_originals_can_be_had(client, conn):
    _paper(conn, "oa", doi=DOI, pdf_url="https://oa.example.org/tan2023.pdf", title="Open paper")
    _paper(conn, "oa_nolink", doi="10.9/oa-page", is_oa=1, title="Readable paper")
    _paper(conn, "paywalled", doi="10.9/closed", is_oa=0, title="Closed paper")
    conn.commit()
    block = client.get("/feed?tab=papers").text.split('id="digest"')[1].split("</section>")[0]
    open_row = block.split("Open paper")[1].split("</article>")[0]
    closed_row = block.split("Closed paper")[1].split("</article>")[0]
    page_row = block.split("Readable paper")[1].split("</article>")[0]
    # card 0106: a lock mark whose tooltip says it
    assert 'data-tip="期刊页可读"' in page_row and "可获取原文" not in page_row          # review: no PDF, no promise
    assert 'data-tip="可获取原文"' in open_row and 'class="access-mark closed tip"' in closed_row
    assert 'data-tip="需订阅"' in closed_row and 'href="https://doi.org/10.9/closed"' in block


def test_the_reader_says_the_paper_came_from_the_push(client, conn, sample_pdf):
    from research_lab.feeds import importer
    _paper(conn, "oa", doi=DOI, pdf_url="https://oa.example.org/tan2023.pdf", is_oa=1, title=TITLE)
    with httpx.Client(transport=_import_transport(sample_pdf.read_bytes(), [])) as http:
        paper_id = importer.autoimport(conn, http=http)[0].paper_id
    conn.commit()
    assert "来自推送 · The Pacific Review" in client.get(f"/papers/{paper_id}").text


def test_a_doi_already_filed_with_its_file_is_just_linked(conn, sample_pdf):
    from research_lab.feeds import importer
    from research_lab.intake import pipeline
    with httpx.Client(transport=_import_transport(sample_pdf.read_bytes(), [])) as http:
        mine = pipeline.ingest(conn, sample_pdf, http=http, original_name="mine.pdf").paper_id   # Leo's own copy
    _paper(conn, "same_doi", doi=DOI, pdf_url="https://oa.example.org/tan2023.pdf")
    with httpx.Client(transport=_import_transport(sample_pdf.read_bytes(), [])) as http:
        r = importer.import_item(conn, "same_doi", http=http, pdf_only=True)
    assert r.paper_id == mine and conn.execute("SELECT COUNT(*) FROM paper_files").fetchone()[0] == 1


def test_a_similar_title_already_filed_is_left_for_leo(conn, sample_pdf):
    from research_lab.feeds import importer
    from research_lab.intake import dedupe
    conn.execute("INSERT INTO papers(id, doi, title, title_hash, authors_json, year, metadata_status, created_at, updated_at) "
                 "VALUES ('P-MINE', '10.5555/other', ?, ?, '[\"Lin Tan\"]', 2023, 'verified', '2026-01-01', '2026-01-01')",
                 (TITLE, dedupe.title_hash(TITLE)))
    _paper(conn, "similar", doi=DOI, pdf_url="https://oa.example.org/tan2023.pdf")
    with httpx.Client(transport=_import_transport(sample_pdf.read_bytes(), [])) as http:
        r = importer.import_item(conn, "similar", http=http, pdf_only=True)
    assert r.paper_id == "" and "标题相近" in r.note
    assert conn.execute("SELECT COUNT(*) FROM paper_files").fetchone()[0] == 0                 # nothing attached
    assert conn.execute("SELECT paper_id FROM feed_papers WHERE id = 'similar'").fetchone()[0] is None


def test_a_failed_download_creates_nothing(conn):
    from research_lab.feeds import importer
    _paper(conn, "broken", doi="10.9/broken", pdf_url="https://oa.example.org/missing.pdf")
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as http:
        r = importer.import_item(conn, "broken", http=http, pdf_only=True)
    assert r.paper_id == "" and conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 0
