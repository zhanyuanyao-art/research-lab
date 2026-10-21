"""Book reviews in 推送 · 论文 (card 0154): told apart from junk, the reviewed book parsed from the four title styles of
Leo's journals, stored as kind 'review', kept out of 今日精选 and the assistant's batches, and shown under the
Articles · Book reviews switch."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from research_lab.feeds.papers import insert, is_book_review, is_junk, is_non_paper, parse_review

REVIEWS = [
    # Taylor & Francis (PAFF, IFJP, PCS): 「Title: by Author, City, Press, Year, pages, price」
    ("The Rise of the Network State: by John Smith, Oxford, Oxford University Press, 2024, 320 pp., £25.00 (hardback), ISBN 978-0-19",
     "The Rise of the Network State", "John Smith", "Oxford University Press, 2024"),
    ("Southeast Asia in Transition: edited by Jane Doe and Kai Lee, London, Routledge, 2025, 300 pp., $30",
     "Southeast Asia in Transition", "Jane Doe and Kai Lee", "Routledge, 2025"),
    ("Girt by Sea: Re-imagining Australia’s Security: by Rebecca Strating and Joanne Wallis, Melbourne, La Trobe University Press, "
     "2024, 288 pp.", "Girt by Sea: Re-imagining Australia’s Security", "Rebecca Strating and Joanne Wallis", "La Trobe University Press, 2024"),
    ("Hedging Southeast Asia, by J. Smith. Cambridge University Press, 2026", "Hedging Southeast Asia", "J. Smith",
     "Cambridge University Press, 2026"),
    # CSA: 「BOOK REVIEW: Title. By Author」
    ("BOOK REVIEW: Beijing's Global Media Offensive. By Joshua Kurlantzick", "Beijing's Global Media Offensive", "Joshua Kurlantzick", None),
    # SEX: 「Book review: Title …」
    ("Book review: Hidden Hands of the State", "Hidden Hands of the State", None, None),
    ("Book review: Translating sexualities: International queer popular culture in the Sinophone world GuoTingEvansJonathan. "
     "Translating Sexualities: International Queer Popular Culture in the Sinophone World. London. Routledge. 2026; 210 pp, $260 "
     "(hardcover), ISBN 9781041071488",
     "Translating Sexualities: International Queer Popular Culture in the Sinophone World", None, "Routledge, 2026"),
    ("Opposing Desires: The Contentious Politics of the South Korean Anti-LGBT Movement: by Hendrik Johannemann, Bielefeld: "
     "transcript publishing, 2025, 262 pp.", "Opposing Desires: The Contentious Politics of the South Korean Anti-LGBT Movement",
     "Hendrik Johannemann", "transcript publishing, 2025"),
    # CQ: 「Title Author. City: Press, Year …」
    ("Conserving China’s Northwest Frontier: Nature, Culture, and Future You-Tien Hsing. Oxford: Oxford University Press, 2025.",
     "Conserving China’s Northwest Frontier: Nature, Culture, and Future", "You-Tien Hsing", "Oxford University Press, 2025"),
    ("Governing Digital China Daniela Stockmann and Ting Luo. Cambridge: Cambridge University Press, 2025. 280 pp.",
     "Governing Digital China", "Daniela Stockmann and Ting Luo", "Cambridge University Press, 2025"),
    ("Arms and Influence in Asia Andrew Scobell. Cambridge, MA: MIT Press, 2025. 320 pp. $35.00",
     "Arms and Influence in Asia", "Andrew Scobell", "MIT Press, 2025"),
    ("The Party and the People: Chinese Politics in the 21st Century Bruce J. Dickson. Princeton, NJ: Princeton University Press, 2021. 336 pp.",
     "The Party and the People: Chinese Politics in the 21st Century", "Bruce J. Dickson", "Princeton University Press, 2021"),
    ("China's Grand Strategy Andrew J. Scobell and Mary Lee. Santa Monica: RAND Corporation, 2024",
     "China's Grand Strategy", "Andrew J. Scobell and Mary Lee", "RAND Corporation, 2024"),
    ("Remaking the Chinese City. By Joseph W. Esherick. Honolulu: University of Hawai'i Press, 2000.",
     "Remaking the Chinese City", "Joseph W. Esherick", "University of Hawai'i Press, 2000"),
]


@pytest.mark.parametrize("title, book, authors, imprint", REVIEWS)
def test_reviews_are_recognised_and_their_book_parsed(title, book, authors, imprint):
    assert is_book_review(title) and not is_junk(title) and is_non_paper(title)   # history.py's import still drops them
    got = parse_review(title)
    assert (got["book_title"], got["book_authors"], got["book_imprint"]) == (book, authors, imprint)


@pytest.mark.parametrize("title", ["Correction to: X", "Erratum", "Editorial", "Editorial: Our new editors", "Issue Information",
                                   "Voting the Same – CORRIGENDUM", "Books Received", "In Memoriam: A Scholar", "Cover and Front Matter",
                                   "Book Reviews editorial", "Book Reviews"])
def test_junk_is_still_dropped(title):
    assert is_junk(title) and not is_book_review(title) and is_non_paper(title)


@pytest.mark.parametrize("title", ["Market corrections and Chinese statecraft", "Editorial politics in authoritarian media",
                                   "Reviewing alliance burdens", "Costly signals and crisis bargaining revisited",
                                   "Who reviews the reviewers? Peer review in IR"])
def test_articles_stay_articles(title):
    assert not is_junk(title) and not is_book_review(title) and not is_non_paper(title)


def _work(doi, title, authors=("Lin Tan",)):
    now = datetime.now(timezone.utc) - timedelta(days=1)
    return {"DOI": doi, "title": [title], "type": "journal-article", "created": {"date-time": now.isoformat()},
            "author": [{"given": a.split()[0], "family": a.split()[-1]} for a in authors]}


def test_a_refresh_keeps_reviews_apart_without_extra_lookups(conn, monkeypatch):
    from research_lab.feeds import papers, sources
    from research_lab.feeds.sources import Journal
    monkeypatch.setenv("CONTACT_EMAIL", "lab@example.org")
    items = [_work("10.1080/paff.1", "ASEAN centrality and hedging in the Indo-Pacific"),
             _work("10.1080/paff.2", "Southeast Asia in Transition: edited by Jane Doe and Kai Lee, London, Routledge, 2025, 300 pp., $30",
                   authors=("Rita Reviewer",)),
             _work("10.1080/paff.3", "Correction to: ASEAN centrality")]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "api.crossref.org":
            return httpx.Response(200, json={"status": "ok", "message": {"items": items}})
        if request.url.host == "api.openalex.org":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"is_oa": False, "best_oa_location": None})
    journals = [Journal("PAFF", "Pacific Affairs", ("0030-851X",), "东南亚", "core", 50)]
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        s = papers.refresh_papers(conn, http, days=10, journals=journals, srcs=sources.load())
    assert s["new"] == 1 and s["reviews"] == 1 and s["skipped"] == 1
    rows = {r["doi"]: r for r in conn.execute("SELECT * FROM feed_papers")}
    assert rows["10.1080/paff.1"]["kind"] == "article"
    review = rows["10.1080/paff.2"]
    assert review["kind"] == "review" and review["book_title"] == "Southeast Asia in Transition"
    assert review["book_authors"] == "Jane Doe and Kai Lee" and review["book_imprint"] == "Routledge, 2025"
    assert json.loads(review["authors_json"]) == ["Rita Reviewer"] and review["direction"] == "sea-asean"   # keyword tag on the book
    assert not any("paff.2" in c for c in calls if "openalex" in c or "unpaywall" in c)               # no lookups for a review


def _review(conn, rid, *, title="Arms and Influence in Asia Andrew Scobell. Cambridge, MA: MIT Press, 2025", status="new",
            reviewer="Rita Reviewer", days_ago=0):
    seen = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    insert(conn, {"id": rid, "doi": f"10.1017/{rid}", "journal": "CQ", "journal_name": "The China Quarterly", "pool": "core",
                  "title": title, "authors": [reviewer], "published": seen[:10], "kind": "review", "status": status,
                  **parse_review(title)}, first_seen=seen)


def _article(conn, aid):
    seen = datetime.now(timezone.utc).isoformat(timespec="seconds")
    insert(conn, {"id": aid, "doi": f"10.1017/{aid}", "journal": "CQ", "journal_name": "The China Quarterly", "pool": "core",
                  "title": f"Article {aid}", "authors": ["Ada Lee"], "published": seen[:10]}, first_seen=seen)


def test_reviews_never_reach_the_picks_and_have_their_own_counts(conn):
    from research_lab.feeds import digest, store
    _review(conn, "rev1")
    _article(conn, "art1")
    conn.commit()
    picks = digest.fill(conn)["papers"]
    assert "art1" in picks and "rev1" not in picks                     # so the assistant's batch never sees it either
    articles, n = store.list_items(conn, "papers", view="all")
    assert [it["id"] for it in articles] == ["art1"] and n == 1
    reviews, n = store.list_items(conn, "papers", view="all", kind="review")
    assert [it["id"] for it in reviews] == ["rev1"] and reviews[0]["display_title"] == "Arms and Influence in Asia"
    c = store.counts(conn)
    assert c["papers"]["new"] == 1 and c["reviews"]["new"] == 1


def test_the_book_reviews_switch_and_cells(client, conn):
    page = client.get("/feed?tab=papers").text
    switch = page.split('class="segmented small kind-switch"', 1)[1].split("</nav>", 1)[0]
    assert ">Articles<" in switch and ">Book reviews<" in switch and 'href="/feed?tab=papers&amp;kind=review"' in switch
    empty = client.get("/feed?tab=papers&kind=review").text
    assert "最近没有书评" in empty and 'class="edition-nav"' not in empty
    _review(conn, "rev1")
    _review(conn, "rev2", title="BOOK REVIEW: Beijing's Global Media Offensive. By Joshua Kurlantzick", reviewer="Bo Lin", days_ago=1)
    _review(conn, "gone", status="dismissed")
    conn.commit()
    page = client.get("/feed?tab=papers&kind=review").text
    switch = page.split('class="segmented small kind-switch"', 1)[1].split("</nav>", 1)[0]
    assert 'aria-current="page">Book reviews<span class="count">2</span>' in switch
    cell = page.split('id="fp-rev1"', 1)[1].split("</article>", 1)[0]
    assert 'href="https://doi.org/10.1017/rev1"' in cell and ">Arms and Influence in Asia</a>" in cell
    assert "by Andrew Scobell · MIT Press, 2025" in cell and "Reviewed by Rita Reviewer" in cell and "The China Quarterly" in cell
    assert "摘要待翻译" not in cell and 'id="fp-gone"' not in page
    assert page.index('id="fp-rev1"') < page.index('id="fp-rev2"')                 # newest first
    saved = client.post("/feed/papers/rev1/save", data={"layout": "story"}).text
    assert 'class="story review"' in saved and "Reviewed by Rita Reviewer" in saved and 'aria-pressed="true"' in saved
    shelf = client.get("/feed?tab=saved").text
    assert "<h3>书评</h3>" in shelf and 'id="fp-rev1"' in shelf
    gone = client.post("/feed/papers/rev2/dismiss", data={"layout": "story"}).text
    assert "已忽略" in gone and "Undo" in gone
    assert 'id="fp-rev1"' not in client.get("/feed?tab=papers&pick=all&view=all").text   # Articles never list reviews


# ---------------------------------------------------------------- International Affairs: reviews titled by the book

from research_lab.feeds.papers import is_short_review, page_span  # noqa: E402


@pytest.mark.parametrize("page, span", [("1430-1432", 3), ("1849-1850", 2), ("221", 1), ("1–24", 24), ("v-x", None), ("", None), (None, None)])
def test_page_span(page, span):
    assert page_span(page) == span


def test_short_items_without_an_abstract_are_reviews():
    ia = {"title": "When rebels win: ideology, statebuilding, and power after civil wars", "authors": ["Rita Reviewer"],
          "abstract": None, "page": "1430-1432"}
    assert is_short_review(ia) and not is_book_review(ia["title"])
    assert is_short_review({**ia, "page": "1849-1850"})
    assert not is_short_review({**ia, "abstract": "We show that rebels who win build states."})   # a short article
    assert not is_short_review({**ia, "page": "1401-1424"})                                     # a full-length article
    assert not is_short_review({**ia, "authors": ["A One", "B Two", "C Three"]})                 # a short co-written piece
    assert not is_short_review({**ia, "page": None})                                            # online first, no pages yet


@pytest.mark.parametrize("title, page", [("Contributors", "v-x"), ("Abstracts", "xi-xxi"), ("Books reviewed: September 2026", None),
                                         ("A perfectly normal title", "xi-xxi")])
def test_front_matter_by_title_or_roman_pages(title, page):
    assert is_junk(title, page)


def _ia_items():
    return [
        {**_work("10.1093/ia/iiad001", "When rebels win: ideology, statebuilding, and power after civil wars", authors=("Rita Reviewer",)),
         "page": "1430-1432"},
        {**_work("10.1093/ia/iiad002", "The anxious superpower", authors=("Bo Lin",)), "page": "1849-1850"},
        {**_work("10.1093/ia/iiad003", "Alliance politics after Ukraine", authors=("Ada Lee", "Kai Tan")), "page": "1401-1424",
         "abstract": "<jats:p>Alliances adapt.</jats:p>"},
        {**_work("10.1093/ia/iiad004", "Short forum: rethinking deterrence", authors=("Ada Lee",)), "page": "1500-1501",
         "abstract": "<jats:p>A two-page piece with an abstract.</jats:p>"},
        {**_work("10.1093/ia/iiad005", "Contributors"), "page": "v-x"},
        {**_work("10.1093/ia/iiad006", "Books reviewed: September 2026"), "page": "xi-xii"},
    ]


def _ia_client(items, calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "api.crossref.org":
            assert "page" in request.url.params.get("select", "page")
            return httpx.Response(200, json={"status": "ok", "message": {"items": items}})
        if request.url.host == "api.openalex.org":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"is_oa": False, "best_oa_location": None})
    return httpx.Client(transport=httpx.MockTransport(handler))



def _ia():
    from research_lab.feeds.sources import Journal
    return [Journal("IA", "International Affairs", ("0020-5850",), "国际关系", "core", 70)]


def test_a_refresh_sorts_international_affairs(conn, monkeypatch):
    from research_lab.feeds import papers, sources
    monkeypatch.setenv("CONTACT_EMAIL", "lab@example.org")
    calls: list[str] = []
    with _ia_client(_ia_items(), calls) as http:
        s = papers.refresh_papers(conn, http, days=10, journals=_ia(), srcs=sources.load())
    kinds = dict(conn.execute("SELECT doi, kind FROM feed_papers").fetchall())
    assert kinds == {"10.1093/ia/iiad001": "review", "10.1093/ia/iiad002": "review",
                     "10.1093/ia/iiad003": "article", "10.1093/ia/iiad004": "article"}
    assert s["reviews"] == 2 and s["new"] == 2 and s["skipped"] == 2
    title = conn.execute("SELECT book_title FROM feed_papers WHERE doi = '10.1093/ia/iiad001'").fetchone()[0]
    assert title == "When rebels win: ideology, statebuilding, and power after civil wars"


def test_the_backfill_fixes_what_the_old_rules_got_wrong(conn, monkeypatch):
    from research_lab.db.connection import get_setting, set_setting
    from research_lab.feeds import digest, papers, sources
    monkeypatch.setenv("CONTACT_EMAIL", "lab@example.org")
    items = _ia_items()
    # what the old rules left behind: the IA review stored as an article and picked today, the front matter stored too
    for i in (items[0], items[4]):
        row = papers.parse_item(i, _ia()[0])
        insert(conn, row)
    set_setting(conn, digest.KEY + "2026-10-13", {"news": [], "papers": [papers.paper_key("10.1093/ia/iiad001"), "other"]})
    conn.commit()
    calls: list[str] = []
    with _ia_client(items, calls) as http:
        dry = papers.backfill_reviews(conn, http, days=60, journals=_ia(), srcs=sources.load(), dry_run=True)
        assert dry == {"IA": {"new": 1, "reclassified": 1, "junk": 1}}
        assert conn.execute("SELECT COUNT(*) FROM feed_papers").fetchone()[0] == 2               # nothing written
        assert conn.execute("SELECT kind FROM feed_papers WHERE doi = '10.1093/ia/iiad001'").fetchone()[0] == "article"
        done = papers.backfill_reviews(conn, http, days=60, journals=_ia(), srcs=sources.load())
    assert done == dry
    rows = {r["doi"]: r for r in conn.execute("SELECT * FROM feed_papers")}
    assert rows["10.1093/ia/iiad001"]["kind"] == "review" and rows["10.1093/ia/iiad002"]["kind"] == "review"
    assert rows["10.1093/ia/iiad005"]["status"] == "dismissed"
    assert "10.1093/ia/iiad003" not in rows                                     # articles are the refresh's business
    assert get_setting(conn, digest.KEY + "2026-10-13")["papers"] == ["other"]
    with _ia_client(items, calls) as http:                                       # a second run finds nothing left to do
        assert papers.backfill_reviews(conn, http, days=60, journals=_ia(), srcs=sources.load()) == {
            "IA": {"new": 0, "reclassified": 0, "junk": 0}}


def test_the_backfill_command(monkeypatch):
    from typer.testing import CliRunner
    from research_lab.cli import feeds as cli
    seen = {}

    def fake(conn, days=60, dry_run=False):
        seen.update(days=days, dry_run=dry_run)
        return {"IA": {"new": 2, "reclassified": 1, "junk": 1}, "TPR": {"new": 0, "reclassified": 0, "junk": 0},
                "DEAD": {"new": 0, "reclassified": 0, "junk": 0, "error": "HTTP 503"}}
    monkeypatch.setattr(cli.papers, "backfill_reviews", fake)
    out = CliRunner().invoke(cli.app, ["reviews-backfill", "--days", "30", "--dry-run"])
    assert out.exit_code == 0 and seen == {"days": 30, "dry_run": True}
    assert "IA: 2 new reviews, 1 articles → reviews, 1 front matter dismissed" in out.output and "TPR" not in out.output
    assert "DEAD: failed (HTTP 503)" in out.output and "(dry run: nothing written)" in out.output
