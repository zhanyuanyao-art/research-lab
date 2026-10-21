"""Free PDFs from the publisher's own page (card 0155): ISEAS's issue page for Contemporary Southeast Asia marks free items
with a Download button; U+FFFD quotes mended; `rl feeds repair` over stored rows. No network: pages and Crossref are
mocked."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from research_lab.feeds import publisher_oa
from research_lab.feeds.papers import clean_text, fix_mojibake

ISSUE_URL = "https://bookshop.iseas.edu.sg/publication/8019"
# the structure of bookshop.iseas.edu.sg/publication/8019 (Vol. 48/2), trimmed to four items
ISSUE_PAGE = """
<ul class="list-unstyled">
  <li class="p-1 border" style="margin: -1px;">
    <div class="d-flex flex-row align-items-center flex-wrap flex-md-nowrap">
      <div class="me-2 flex-fill">
        <span class="me-2">Contemporary Southeast Asia Vol. 48/2 (August 2026)<br/><span class="whole-publication">[Whole Publication]</span></span>
      </div>
      <div class="d-flex flex-row flex-fill justify-content-end">
        <a class="btn btn-primary btn-sm" target="_blank" href="https://readabook.store/products/J1793284XV48N02" role="button">Check&nbsp;Price</a>
      </div>
    </div>
  </li>
  <li class="p-1 border" style="margin: -1px;">
    <div class="d-flex flex-row align-items-center flex-wrap flex-md-nowrap">
      <div class="me-2 flex-fill"><span class="me-2">preliminary pages</span></div>
      <div class="d-flex flex-row flex-fill justify-content-end">
        <a class="btn x-btn-download btn-sm" target="_blank"
           href="/component/get/23784" role="button">Download</a>
      </div>
    </div>
  </li>
  <li class="p-1 border" style="margin: -1px;">
    <div class="d-flex flex-row align-items-center flex-wrap flex-md-nowrap">
      <div class="me-2 flex-fill">
        <span class="me-2">1. Command Politics: Militarized Governance and Its Fragility in Prabowo’s Indonesia, by&nbsp;<a href='/search?person=5606'>Jefferson Ng</a>, <span class='postfix'>author</span></span>
        <a data-bs-toggle="collapse" href="#abstract-cid23785" role="button">see&nbsp;abstract</a>
      </div>
      <div class="d-flex flex-row flex-fill justify-content-end">
        <a class="btn x-btn-download btn-sm" target="_blank"
           href="/component/get/23785" role="button">Download</a>
      </div>
    </div>
    <div class="collapse p-2 mt-1" id="abstract-cid23785">This article introduces the concept of command politics.</div>
  </li>
  <li class="p-1 border" style="margin: -1px;">
    <div class="d-flex flex-row align-items-center flex-wrap flex-md-nowrap">
      <div class="me-2 flex-fill">
        <span class="me-2">2. Fade to Blue: The 2024 Thai Senate and the Rise of Transactional Conservatism, by&nbsp;<a href='/search?person=1507'>Duncan McCargo</a>, <span class='postfix'>author</span></span>
      </div>
      <div class="d-flex flex-row flex-fill justify-content-end">
        <a class="btn btn-primary btn-sm" target="_blank" href="https://readabook.store/products/J1793284XV48N02C02" role="button">Check&nbsp;Price</a>
      </div>
    </div>
  </li>
  <li class="p-1 pb-0 border" style="margin: -1px;"><strong>BOOK REVIEWS</strong></li>
  <li class="p-1 border" style="margin: -1px;">
    <div class="d-flex flex-row align-items-center flex-wrap flex-md-nowrap">
      <div class="me-2 flex-fill">
        <span class="me-2">BOOK REVIEW: <i>The Dragon’s Emerging Order: Sino-centric Multilateralism and Global Responses</i>. Edited by Joel Ng, by&nbsp;<a href='/search?person=8151'>Ngeow Chow Bing</a>, <span class='postfix'>author</span></span>
      </div>
      <div class="d-flex flex-row flex-fill justify-content-end">
        <a class="btn x-btn-download btn-sm" target="_blank"
           href="/component/get/23790" role="button">Download</a>
      </div>
    </div>
  </li>
</ul>
"""
COMMAND = "Command Politics: Militarized Governance and Its Fragility in Prabowo\ufffds Indonesia"
FADE = "Fade to Blue: The 2024 Thai Senate and the Rise of Transactional Conservatism"
DRAGON = "BOOK REVIEW: The Dragon\ufffds Emerging Order: Sino-centric Multilateralism and Global Responses. Edited by Joel Ng"


def test_the_iseas_page_lists_every_item_and_the_free_ones_pdf():
    entries = publisher_oa.parse_iseas(ISSUE_PAGE, ISSUE_URL)
    assert [(e.person, e.pdf) for e in entries] == [
        ("", None),                                                                       # the whole issue, priced
        ("", "https://bookshop.iseas.edu.sg/component/get/23784"),
        ("Jefferson Ng", "https://bookshop.iseas.edu.sg/component/get/23785"),
        ("Duncan McCargo", None),                                                         # Check Price
        ("Ngeow Chow Bing", "https://bookshop.iseas.edu.sg/component/get/23790")]
    assert publisher_oa.match(COMMAND, entries) == "https://bookshop.iseas.edu.sg/component/get/23785"     # U+FFFD vs ’
    assert publisher_oa.match(DRAGON, entries) == "https://bookshop.iseas.edu.sg/component/get/23790"      # the review, in italics
    assert publisher_oa.match(FADE, entries) is None                                                       # priced
    assert publisher_oa.match("Preliminary", entries) is None and publisher_oa.match("", entries) is None
    assert publisher_oa.handles(ISSUE_URL) and not publisher_oa.handles("https://muse.jhu.edu/article/614290")


def _item(title, href=None, person="A Author", price=False):
    button = (f'<a class="btn x-btn-download btn-sm" target="_blank" href="{href}" role="button">Download</a>' if not price
              else '<a class="btn btn-primary btn-sm" href="https://readabook.store/p/1" role="button">Check&nbsp;Price</a>')
    return (f'<li class="p-1 border"><div class="d-flex"><div class="me-2 flex-fill"><span class="me-2">{title}, by&nbsp;'
            f"<a href='/search?person=1'>{person}</a>, <span class='postfix'>author</span></span></div>"
            f'<div class="d-flex justify-content-end">{button}</div></div></li>')


@pytest.mark.parametrize("href", ["javascript:alert(document.domain)", "JaVaScRiPt:alert(1)", "data:text/html,hi",
                                  "https://evil.example/component/get/1", "http://bookshop.iseas.edu.sg/component/get/1",
                                  "//evil.example/component/get/1", "https://bookshop.iseas.edu.sg.evil.example/x",
                                  "https://user@bookshop.iseas.edu.sg/x", "https://127.0.0.1/component/get/1", ""])
def test_only_https_links_on_the_publishers_own_host_are_kept(href):
    entries = publisher_oa.parse_iseas(f"<ul>{_item('A Title Long Enough To Match', href)}</ul>", ISSUE_URL)
    assert entries[0].pdf is None and publisher_oa.match("A Title Long Enough To Match", entries) is None


def test_relative_and_absolute_publisher_links_are_kept():
    for href in ("/component/get/9", "https://bookshop.iseas.edu.sg/component/get/9", "component/get/9"):
        entries = publisher_oa.parse_iseas(f"<ul>{_item('A Title', href)}</ul>", ISSUE_URL)
        assert entries[0].pdf == "https://bookshop.iseas.edu.sg/component/get/9", href


def test_a_title_that_only_starts_another_never_takes_its_pdf():
    page = "<ul>" + _item("Southeast Asia and Great Power Rivalry", price=True) + \
        _item("Southeast Asia and Great Power Rivalry: A Rejoinder", "/component/get/2") + "</ul>"
    entries = publisher_oa.parse_iseas(page, ISSUE_URL)
    assert publisher_oa.match("Southeast Asia and Great Power Rivalry", entries) is None                 # priced, no prefix match
    assert publisher_oa.match("Southeast Asia and Great Power Rivalry: A Rejoinder", entries).endswith("/2")
    alone = publisher_oa.parse_iseas("<ul>" + _item("Southeast Asia and Great Power Rivalry: A Rejoinder", "/component/get/2") + "</ul>", ISSUE_URL)
    assert publisher_oa.match("Southeast Asia and Great Power Rivalry", alone) is None                   # even when it is the only one


def test_two_reviews_of_one_book_are_told_apart_by_the_reviewer():
    title = "BOOK REVIEW: <i>Rice Politics in Southeast Asia</i>. By Jamie S. Davidson"
    page = "<ul>" + _item(title, "/component/get/7", person="Paul Teng Piang Siong") + _item(title, person="Lee Jones", price=True) + "</ul>"
    entries = publisher_oa.parse_iseas(page, ISSUE_URL)
    feed_title = "BOOK REVIEW: Rice Politics in Southeast Asia. By Jamie S. Davidson"
    assert publisher_oa.match(feed_title, entries, ["Paul Teng Piang Siong"]).endswith("/7")
    assert publisher_oa.match(feed_title, entries, ["Lee Jones"]) is None                                 # the priced one
    assert publisher_oa.match(feed_title, entries) is None                                                # no reviewer: no guess
    assert publisher_oa.match(feed_title, entries, ["Someone Else"]) is None


def test_the_review_row_never_links_a_script(client, conn):
    from research_lab.feeds.papers import insert
    insert(conn, {"id": "bad", "doi": "10.1355/x", "journal": "CSA", "journal_name": "Contemporary Southeast Asia", "title": DRAGON,
                  "authors": ["Ngeow Chow Bing"], "kind": "review", "book_title": "The Dragon’s Emerging Order", "is_oa": 1,
                  "oa_pdf_url": "javascript:alert(document.domain)", "oa_license": "publisher"})
    conn.commit()
    page = client.get("/feed?tab=papers&kind=review").text
    cell = page.split('id="fp-bad"', 1)[1].split("</article>", 1)[0]
    assert "javascript:" not in page and ">Download</a>" not in cell and "出版社免费提供" not in cell


@pytest.mark.parametrize("raw, fixed", [
    ("Prabowo\ufffds Indonesia", "Prabowo’s Indonesia"),
    ("Myanmar\ufffds Conflict Landscape", "Myanmar’s Conflict Landscape"),
    ("why states don\ufffdt and won\ufffdt", "why states don’t and won’t"),
    ("we\ufffdll see, they\ufffdre here, I\ufffdve read, I\ufffdd go, I\ufffdm", "we’ll see, they’re here, I’ve read, I’d go, I’m"),
    ("From \ufffdBidenomics\ufffd to \"MAGAnomics\"", "From “Bidenomics” to \"MAGAnomics\""),
    ("the \ufffdhub and spokes\ufffd model", "the “hub and spokes” model"),
    ("(\ufffdstrategic autonomy\ufffd)", "(“strategic autonomy”)"),
    ("rock\ufffdn\ufffdroll", "rock’n’roll"),
    ("stray \ufffd mark", "stray ’ mark"),
    ("no damage here", "no damage here"),
])
def test_mojibake_quotes_are_mended(raw, fixed):
    assert fix_mojibake(raw) == fixed
    assert clean_text(f"<jats:p>{raw}</jats:p>") == fixed


def _client(calls, *, page=ISSUE_PAGE, items=()):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "bookshop.iseas.edu.sg":
            return httpx.Response(200, text=page, headers={"content-type": "text/html; charset=utf-8"})
        if request.url.host == "api.crossref.org":
            return httpx.Response(200, json={"status": "ok", "message": {"items": list(items)}})
        if request.url.host == "api.openalex.org":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"is_oa": False, "best_oa_location": None})   # Unpaywall: closed
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_resolve_fetches_each_issue_page_once():
    rows = [{"title": COMMAND, "resource_url": ISSUE_URL}, {"title": FADE, "resource_url": ISSUE_URL},
            {"title": DRAGON, "resource_url": ISSUE_URL, "authors": ["Ngeow Chow Bing"]},
            {"title": "Elsewhere", "resource_url": "https://muse.jhu.edu/article/1"}]
    calls: list[str] = []
    with _client(calls) as http:
        assert publisher_oa.resolve(rows, http) == 2
    assert sum("bookshop.iseas.edu.sg" in c for c in calls) == 1 and not any("muse" in c for c in calls)
    assert rows[0] == {"title": COMMAND, "resource_url": ISSUE_URL, "is_oa": 1, "oa_license": "publisher",
                       "oa_pdf_url": "https://bookshop.iseas.edu.sg/component/get/23785"}
    assert "oa_pdf_url" not in rows[1] and rows[2]["oa_pdf_url"].endswith("/23790")


def _work(doi, title, page, authors=("Jefferson Ng",)):
    now = datetime.now(timezone.utc) - timedelta(days=1)
    return {"DOI": doi, "title": [title], "type": "journal-article", "created": {"date-time": now.isoformat()}, "page": page,
            "author": [{"given": a.split()[0], "family": a.split()[-1]} for a in authors],
            "resource": {"primary": {"URL": ISSUE_URL}}}


CSA_ITEMS = [_work("10.1355/cs48-2a", COMMAND, "225-251"), _work("10.1355/cs48-2b", FADE, "252-279", ("Duncan McCargo",)),
             _work("10.1355/cs48-2f", DRAGON, "362-365", ("Ngeow Chow Bing",))]


def _csa():
    from research_lab.feeds.sources import Journal
    return [Journal("CSA", "Contemporary Southeast Asia", ("0129-797X", "1793-284X"), "后殖民与Global IR", "hunt", 75)]


def test_a_refresh_finds_the_free_csa_papers_and_reviews(conn, monkeypatch):
    from research_lab.feeds import papers, sources
    monkeypatch.setenv("CONTACT_EMAIL", "lab@example.org")
    calls: list[str] = []
    with _client(calls, items=CSA_ITEMS) as http:
        papers.refresh_papers(conn, http, days=10, journals=_csa(), srcs=sources.load())
    rows = {r["doi"]: r for r in conn.execute("SELECT * FROM feed_papers")}
    command, fade, dragon = rows["10.1355/cs48-2a"], rows["10.1355/cs48-2b"], rows["10.1355/cs48-2f"]
    assert command["title"] == "Command Politics: Militarized Governance and Its Fragility in Prabowo’s Indonesia"   # mended
    assert command["is_oa"] == 1 and command["oa_pdf_url"] == "https://bookshop.iseas.edu.sg/component/get/23785"
    assert command["oa_license"] == "publisher" and fade["oa_pdf_url"] is None and fade["is_oa"] == 0
    assert dragon["kind"] == "review" and dragon["oa_pdf_url"].endswith("/23790") and dragon["book_title"] == "The Dragon’s Emerging Order: Sino-centric Multilateralism and Global Responses"
    assert sum("bookshop.iseas.edu.sg" in c for c in calls) == 1


def test_the_feed_says_free_from_the_publisher(client, conn):
    from research_lab.feeds.papers import insert
    insert(conn, {"id": "rev", "doi": "10.1355/cs48-2f", "journal": "CSA", "journal_name": "Contemporary Southeast Asia",
                  "title": DRAGON, "authors": ["Ngeow Chow Bing"], "kind": "review", "book_title": "The Dragon’s Emerging Order",
                  "is_oa": 1, "oa_pdf_url": "https://bookshop.iseas.edu.sg/component/get/23790", "oa_license": "publisher"})
    conn.commit()
    page = client.get("/feed?tab=papers&kind=review").text
    cell = page.split('id="fp-rev"', 1)[1].split("</article>", 1)[0]
    assert 'href="https://bookshop.iseas.edu.sg/component/get/23790"' in cell and ">Download</a>" in cell and "出版社免费提供" in cell


def _stored(conn, pid, doi, title, *, abstract=None, direction=None, oa=None, days_ago=1):
    from research_lab.feeds.papers import insert
    seen = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    insert(conn, {"id": pid, "doi": doi, "journal": "CSA", "journal_name": "Contemporary Southeast Asia", "title": title,
                  "abstract": abstract, "authors": ["A B"], "direction": direction, "oa_pdf_url": oa, "is_oa": 1 if oa else 0},
           first_seen=seen)


def test_repair_mends_finds_and_tags_what_is_stored(conn):
    from research_lab.feeds import papers, sources
    _stored(conn, "cmd", "10.1355/cs48-2a", COMMAND, abstract="The administration\ufffds \ufffdcommand politics\ufffd in ASEAN.")
    _stored(conn, "fade", "10.1355/cs48-2b", FADE, direction="alliance-us")       # set: never overridden
    _stored(conn, "old", "10.1355/cs40-1a", "Old Prabowo\ufffds piece on ASEAN", days_ago=200)   # outside --days
    conn.execute("INSERT INTO feed_news(id, source, title, link, first_seen, lane) VALUES (?,?,?,?,?,?)",
                 ("n1", "The Diplomat", "ASEAN summit opens in Kuala Lumpur", "https://x.example/n1",
                  datetime.now(timezone.utc).isoformat(timespec="seconds"), "research"))
    conn.commit()
    from dataclasses import replace
    srcs = replace(sources.load(), journals=_csa())
    calls: list[str] = []
    with _client(calls, items=CSA_ITEMS) as http:
        dry = papers.repair(conn, http, days=90, dry_run=True, srcs=srcs)
        assert dry == {"titles": 1, "abstracts": 1, "publisher_pdfs": 1, "directions": 1, "news_directions": 1}
        assert "\ufffd" in conn.execute("SELECT title FROM feed_papers WHERE id = 'cmd'").fetchone()[0]   # nothing written
        done = papers.repair(conn, http, days=90, srcs=srcs)
    assert done == dry
    row = conn.execute("SELECT * FROM feed_papers WHERE id = 'cmd'").fetchone()
    assert row["title"].endswith("Prabowo’s Indonesia") and row["abstract"] == "The administration’s “command politics” in ASEAN."
    assert row["oa_pdf_url"].endswith("/23785") and row["oa_license"] == "publisher" and row["direction"] == "sea-asean"
    assert conn.execute("SELECT direction FROM feed_papers WHERE id = 'fade'").fetchone()[0] == "alliance-us"
    assert "\ufffd" in conn.execute("SELECT title FROM feed_papers WHERE id = 'old'").fetchone()[0]
    assert conn.execute("SELECT direction FROM feed_news WHERE id = 'n1'").fetchone()[0] == "sea-asean"
    assert conn.execute("SELECT COUNT(*) FROM feed_papers").fetchone()[0] == 3                       # nothing deleted


def test_the_repair_command(monkeypatch):
    from typer.testing import CliRunner
    from research_lab.cli import feeds as cli
    seen = {}

    def fake(conn, days=90, dry_run=False):
        seen.update(days=days, dry_run=dry_run)
        return {"titles": 3, "abstracts": 2, "publisher_pdfs": 7, "directions": 12, "news_directions": 4}
    monkeypatch.setattr(cli.papers, "repair", fake)
    out = CliRunner().invoke(cli.app, ["repair", "--days", "30", "--dry-run"])
    assert out.exit_code == 0 and seen == {"days": 30, "dry_run": True}
    assert "titles mended: 3, abstracts mended: 2" in out.output and "free PDFs from publishers: 7" in out.output
    assert "directions filled: 12 papers, 4 news (dry run: nothing written)" in out.output
