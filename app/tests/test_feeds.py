"""Local feed engine (research_lab/feeds): parsing, fetchers with mock transports, the history importer,
item status routes, 一键导入 with and without an open-access PDF, the assistant batch job, scheduling."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

NOW = datetime.now(timezone.utc)
YESTERDAY = NOW - timedelta(days=1)


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    """Crossref's polite spacing (1/3 s per call) would make the fetcher tests slow."""
    from research_lab.feeds import net
    from research_lab.intake import metadata
    monkeypatch.setattr(metadata, "MIN_INTERVAL", {})
    monkeypatch.setattr(net, "EXTRA_INTERVAL", {})


# ---------------------------------------------------------------- sources

def test_sources_file_has_all_journals_and_feeds(repo):
    from research_lab.feeds import sources
    s = sources.load()
    assert len(s.journals) == 42 and len(s.news) == 30
    # East Asia Forum (403 to any automated client) and CSIS's frozen main feed are switched off with a reason
    assert [n for n, _why in s.disabled_news] == ["East Asia Forum", "CSIS"] and all(why for _n, why in s.disabled_news)
    assert {"East Asia Forum", "CSIS"}.isdisjoint(f.name for f in s.news)
    assert {j.pool for j in s.journals} == {"core", "watch", "hunt", "expansion"}
    assert sorted(j.abbr for j in s.journals if j.gp_filter) == ["EJPG", "GLQ", "PG", "SEX", "SIGNS"]
    assert s.journal("SD").issns == ("1460-3640", "0967-0106") and s.journal("EJIR").weight == 100
    assert " war" in s.gp_terms and "care " in s.radar_terms
    assert sources.gp_relevant("Women at war", s.gp_terms) and not sources.gp_relevant("Care work at home", s.gp_terms)


# ---------------------------------------------------------------- RSS and Atom

def rss(items: list[tuple[str, str, datetime]]) -> str:
    body = "".join(
        f"""<item><title>{t}</title><link>{link}</link><pubDate>{format_datetime(d)}</pubDate>
        <description><![CDATA[<p>About <b>{t}</b> &amp; ASEAN.</p><p>The post {t} appeared first on Fulcrum.</p>]]></description>
        <media:content url="https://fulcrum.sg/{abs(hash(t)) % 999}.jpg" medium="image"/></item>""" for t, link, d in items)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Fulcrum</title><link>https://fulcrum.sg</link>{body}</channel></rss>"""


ATOM = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>The Interpreter</title>
 <link rel="self" href="https://www.lowyinstitute.org/the-interpreter/rss.xml"/>
 <entry>
  <title type="html">Reassuring Seoul &lt;em&gt;after&lt;/em&gt; the summit</title>
  <link rel="enclosure" href="https://media.example.org/episode.mp3"/>
  <link rel="alternate" href="https://www.lowyinstitute.org/the-interpreter/reassuring-seoul"/>
  <published>{YESTERDAY.strftime('%Y-%m-%dT%H:%M:%SZ')}</published>
  <summary type="html">&lt;p&gt;Extended deterrence &amp;amp; alliance politics after the summit.&lt;/p&gt;</summary>
 </entry>
</feed>"""


def test_parse_rss_and_atom():
    from research_lab.feeds import news
    items = news.parse_feed(rss([("ASEAN&#8217;s centrality", "https://fulcrum.sg/asean/?utm_source=rss&amp;utm_medium=rss", YESTERDAY)]), "Fulcrum · ISEAS")
    assert len(items) == 1
    it = items[0]
    assert it["title"] == "ASEAN’s centrality" and it["source"] == "Fulcrum · ISEAS"
    assert it["summary"] == "About ASEAN’s centrality & ASEAN."            # tags, entities and WordPress boilerplate gone
    assert it["image"].startswith("https://fulcrum.sg/") and it["published"] == YESTERDAY.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert news.canonical_link(it["link"]) == "https://fulcrum.sg/asean"
    assert news.news_key(it["link"]) == news.news_key("http://www.fulcrum.sg/asean#comments")

    atom = news.parse_feed(ATOM.encode("utf-8"), "The Interpreter · Lowy")
    assert [a["title"] for a in atom] == ["Reassuring Seoul after the summit"]
    assert atom[0]["link"] == "https://www.lowyinstitute.org/the-interpreter/reassuring-seoul"
    assert atom[0]["summary"] == "Extended deterrence & alliance politics after the summit."
    with pytest.raises(ValueError):
        news.parse_feed(b"<html><body>Forbidden</body", "x")


def test_refresh_news_isolates_dead_feeds_and_dedupes(conn):
    from research_lab.feeds import news
    from research_lab.feeds.sources import NewsFeed
    good = rss([("ASEAN centrality and hedging", "https://fulcrum.sg/a/", YESTERDAY),
                ("An old piece", "https://fulcrum.sg/old/", NOW - timedelta(days=30))])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fulcrum.sg":
            return httpx.Response(200, text=good, headers={"content-type": "application/rss+xml"})
        if request.url.host == "www.lowyinstitute.org":
            return httpx.Response(200, text=ATOM)
        if request.url.host == "slow.example.org":
            raise httpx.ConnectTimeout("timeout")
        return httpx.Response(403, text="<html>blocked</html>")

    feeds = [NewsFeed("Fulcrum · ISEAS", "https://fulcrum.sg/feed/"), NewsFeed("The Interpreter · Lowy", "https://www.lowyinstitute.org/rss.xml"),
             NewsFeed("East Asia Forum", "https://eastasiaforum.org/feed/"), NewsFeed("Slow", "https://slow.example.org/feed")]
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        s = news.refresh_news(conn, http, days=4, feeds=feeds)
        assert s["new"] == 2 and s["old"] == 1 and s["fetched"] == 3
        assert s["per_source"] == {"Fulcrum · ISEAS": 1, "The Interpreter · Lowy": 1}
        assert sorted((f["source"], f["error"]) for f in s["failed"]) == [("East Asia Forum", "HTTP 403"), ("Slow", "ConnectTimeout")]
        again = news.refresh_news(conn, http, days=4, feeds=feeds)
        assert again["new"] == 0
    rows = {r["title"]: r for r in conn.execute("SELECT * FROM feed_news")}
    assert rows["ASEAN centrality and hedging"]["direction"] == "sea-asean"
    assert rows["Reassuring Seoul after the summit"]["direction"] == "alliance-us"
    assert all(r["status"] == "new" and r["origin"] == "rss" for r in rows.values())


# ---------------------------------------------------------------- Crossref

def _work(doi: str, title: str, *, abstract: str | None = None, days_ago: int = 1, authors=(("Lin", "Tan"),)) -> dict:
    created = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    w = {"DOI": doi, "title": [title], "author": [{"given": g, "family": f} for g, f in authors],
         "created": {"date-time": created, "date-parts": [[2026, 9, 30]]}, "type": "journal-article"}
    if abstract:
        w["abstract"] = f"<jats:p>{abstract}</jats:p>"
    return w


def crossref_transport(calls: list[str]) -> httpx.MockTransport:
    tpr = [_work("10.1080/tpr.2026.1", "ASEAN centrality and hedging in the Indo-Pacific",
                 abstract="How ASEAN regionalism survives great-power rivalry."),
           _work("10.1080/tpr.2026.2", "Correction to: ASEAN centrality and hedging"),
           _work("10.1080/tpr.2026.3", "Cover and Front Matter"),
           _work("10.1080/tpr.2026.4", "Hedging Southeast Asia, by J. Smith. Cambridge University Press, 2026"),
           _work("10.1080/tpr.2026.5", "Costly signals and crisis bargaining revisited")]
    pg = [_work("10.1017/pg.2026.1", "Gendered care work in municipal councils", abstract="Local politics and care."),
          _work("10.1017/pg.2026.2", "Feminist foreign policy and the politics of war", abstract="A feminist reading of security.")]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        host, path = request.url.host, request.url.path
        if host == "api.crossref.org" and path == "/journals/0951-2748/works":
            assert "from-created-date:" in request.url.params["filter"] and request.url.params["sort"] == "created"
            return httpx.Response(200, json={"status": "ok", "message": {"items": tpr}})
        if host == "api.crossref.org" and path == "/journals/1743-923X/works":
            return httpx.Response(200, json={"status": "ok", "message": {"items": []}})   # primary ISSN empty
        if host == "api.crossref.org" and path == "/journals/1743-9248/works":
            return httpx.Response(200, json={"status": "ok", "message": {"items": pg}})
        if host == "api.crossref.org" and path.startswith("/journals/0000-"):
            return httpx.Response(503, text="down")
        if host == "api.openalex.org" and path == "/works":
            return httpx.Response(200, json={"results": [
                {"doi": "https://doi.org/10.1080/tpr.2026.5", "abstract_inverted_index": {"Resolve": [0], "and": [1], "deterrence.": [2]},
                 "open_access": {"is_oa": True}, "best_oa_location": {"pdf_url": "https://oa.example.org/tpr5.pdf", "license": "cc-by"}},
                {"doi": "https://doi.org/10.1080/tpr.2026.1", "open_access": {"is_oa": False}, "best_oa_location": None}]})
        if host == "api.unpaywall.org":
            return httpx.Response(200, json={"is_oa": False, "best_oa_location": None})
        return httpx.Response(404, json={})
    return httpx.MockTransport(handler)


def test_refresh_papers_filters_and_isolates(conn, monkeypatch):
    from research_lab.feeds import papers, sources
    from research_lab.feeds.sources import Journal
    monkeypatch.setenv("CONTACT_EMAIL", "lab@example.org")
    srcs = sources.load()
    journals = [Journal("TPR", "The Pacific Review", ("0951-2748", "1470-1332"), "后殖民与Global IR", "hunt", 54),
                Journal("PG", "Politics & Gender", ("1743-923X", "1743-9248"), "性别研究", "hunt", 48, gp_filter=True),
                Journal("DEAD", "Dead Journal", ("0000-0001",), "x", "core", 40)]
    calls: list[str] = []
    with httpx.Client(transport=crossref_transport(calls)) as http:
        s = papers.refresh_papers(conn, http, days=10, journals=journals, srcs=srcs)
        # the book review is kept apart since card 0154; the correction and the front matter are still dropped
        assert s["new"] == 3 and s["reviews"] == 1 and s["skipped"] == 2 and s["filtered"] == 1
        assert s["per_source"] == {"TPR": 2, "PG": 1}
        assert s["failed"] == [{"source": "DEAD", "error": "HTTP 503"}]
        assert any("mailto=lab%40example.org" in c for c in calls if "crossref" in c)
        again = papers.refresh_papers(conn, http, days=10, journals=journals, srcs=srcs)
        assert again["new"] == 0 and again["known"] == 4
    rows = {r["doi"]: r for r in conn.execute("SELECT * FROM feed_papers WHERE kind = 'article'")}
    assert set(rows) == {"10.1080/tpr.2026.1", "10.1080/tpr.2026.5", "10.1017/pg.2026.2"}
    a = rows["10.1080/tpr.2026.1"]
    assert a["abstract"] == "How ASEAN regionalism survives great-power rivalry." and a["direction"] == "sea-asean"
    assert a["journal_name"] == "The Pacific Review" and a["pool"] == "hunt" and json.loads(a["authors_json"]) == ["Lin Tan"]
    assert a["is_oa"] == 0 and a["oa_pdf_url"] is None and a["published"] == (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
    b = rows["10.1080/tpr.2026.5"]
    assert b["abstract"] == "Resolve and deterrence." and b["oa_pdf_url"] == "https://oa.example.org/tpr5.pdf" and b["is_oa"] == 1
    assert b["direction"] == "strategy-signaling"
    g = rows["10.1017/pg.2026.2"]
    assert g["radar"] == 1 and "feminis" in json.loads(g["radar_json"])


def test_non_paper_filter():
    from research_lab.feeds.papers import is_non_paper
    for t in ("Correction to: X", "Erratum", "Editorial", "Editorial: Our new editors", "Issue Information",
              "Book Review: The Ends of Empire", "Voting the Same – CORRIGENDUM", "Empire, by A. Author. 252pp. Hardcover $75.00"):
        assert is_non_paper(t), t
    for t in ("Market corrections and Chinese statecraft", "Editorial politics in authoritarian media", "Reviewing alliance burdens"):
        assert not is_non_paper(t), t


# ---------------------------------------------------------------- history import

DIGEST = """# 核心池周报 2026-09-14

时间窗：2026-09-06 起（近 8 天）。共 3 篇；🎯 雷达命中 1 篇；跳过疑似书评 0 篇。

## IA ｜ International Affairs ｜ 理论看新闻

- **Reassurance and alliance politics in East Asia**
  Lin Tan, K. Lee ｜ 2026-09-10 ｜ https://doi.org/10.9999/ia.2026.1
  摘要：How allies read US commitments.

- 🎯 **Securitization and gender at sea**
  （作者信息缺失） ｜ 2026-09-11 ｜ https://doi.org/10.9999/ia.2026.2
  命中：gender、securitiz
  摘要：Maritime security as gendered practice.

- **Correction to: Reassurance and alliance politics in East Asia**
  （作者信息缺失） ｜ 2026-09-12 ｜ https://doi.org/10.9999/ia.2026.3
"""

DAILY = """# 核心池日报 2026-09-15

## PAFF ｜ Pacific Affairs ｜ 亚太

- 🎯 **东盟中心地位与对冲**
  原题：ASEAN Centrality and Hedging
  Ana Lim ｜ 2026-09-12 ｜ https://doi.org/10.9999/paff.2026.7
  ✅ 可译可发
  命中：discourse
  拓展阅读：[OA PDF](https://oa.example.org/paff7.pdf) ｜ [Google Scholar](https://scholar.google.com/scholar?q=doi%3A10.9999/paff.2026.7)
  摘要：本文考察东盟如何在大国竞争中维持中心地位。
"""

HUNT = """# 猎场池月报 2026-09-01

## PG ｜ Politics & Gender ｜ 性别研究

- **Feminist foreign policy at war**
  A. Author ｜ 2026-08-28 ｜ https://doi.org/10.9999/pg.2026.9
  ⚠️ 闭源无全文，仅摘要

## 未命中全球政治词根（1 篇，扫标题自捞）

- [PG] Something local ｜ https://doi.org/10.9999/pg.2026.10
"""

NEWS = """# 时评精选 2026-09-20

近 2 天 ｜ 扫描 3 条，入选 2 条（相关性 ≥3 且为分析文）

## 1. 东盟峰会上的南海议题

Fulcrum · ISEAS ｜ 2026-09-19 ｜ [原文](https://fulcrum.sg/asean-summit-scs/)

原题：The South China Sea at the ASEAN Summit

文章分析东盟峰会如何处理南海争端。

> ✍️ 可从东盟中心地位与议程设置切入。

## 2. 美韩同盟的延伸威慑再保证

War on the Rocks ｜ 2026-09-19 ｜ [原文](https://warontherocks.com/us-rok-extended-deterrence/)

原题：Reassuring Seoul: Extended Deterrence after the Summit

文章讨论美国如何向韩国提供延伸威慑再保证。
"""


@pytest.fixture()
def history_repo(tmp_path: Path) -> Path:
    root = tmp_path / "old-newsletter"
    (root / "digests" / "news").mkdir(parents=True)
    (root / "state").mkdir()
    (root / "digests" / "digest-2026-09-14.md").write_text(DIGEST, encoding="utf-8")
    (root / "digests" / "daily-2026-09-15.md").write_text(DAILY, encoding="utf-8")
    (root / "digests" / "hunt-2026-09-01.md").write_text(HUNT, encoding="utf-8")
    (root / "digests" / "news" / "news-2026-09-20.md").write_text(NEWS, encoding="utf-8")
    (root / "state" / "papers_recent.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
        {"doi": "10.9999/ia.2026.1", "title": "Reassurance and alliance politics in East Asia", "zh_title": "东亚的再保证与同盟政治",
         "journal": "IA", "pool": "core", "reported": "2026-09-14", "radar": [], "verdict": "✅ 可译可发", "abstract": "How allies"},
        {"doi": "10.9999/ejir.2026.4", "title": "A theory of convening power", "zh_title": "", "journal": "EJIR", "pool": "daily",
         "reported": "2026-09-16", "radar": ["ontolog"], "verdict": "⚠️ 闭源无全文，仅摘要", "abstract": "Ontology of convening."},
    ]) + "\n", encoding="utf-8")
    (root / "brief_bundle.json").write_text(json.dumps({"today": "2026-08-16", "entries": [
        {"journal_abbr": "EJIR", "title": "Explaining indeterminacy", "zh_title": "解释不确定性", "authors": "Sebastian Schindler",
         "date": "2026-07-25", "doi": "10.9999/ejir.2026.1", "zh_abstract": "传统解释试图说明历史发展的确定性。", "radar": False,
         "access": {"verdict": "✅ 可译可发", "pdf_url": ""}}]}, ensure_ascii=False), encoding="utf-8")
    return root


def test_history_import_keeps_dates_titles_and_radar(conn, history_repo):
    from research_lab.feeds import history
    r = history.import_all(conn, history_repo)
    assert r["papers"] == {"found": 7, "new": 6, "filled": 0, "skipped": 1}      # the correction is not a paper
    assert r["news"] == {"found": 2, "new": 2, "filled": 0}
    rows = {x["doi"]: x for x in conn.execute("SELECT * FROM feed_papers")}
    ia = rows["10.9999/ia.2026.1"]
    assert ia["zh_title"] == "东亚的再保证与同盟政治" and ia["abstract"] == "How allies read US commitments."
    assert ia["first_seen"].startswith("2026-09-14") and ia["published"] == "2026-09-10" and ia["origin"] == "history"
    assert json.loads(ia["authors_json"]) == ["Lin Tan", "K. Lee"] and ia["journal_column"] == "理论看新闻"
    radar = rows["10.9999/ia.2026.2"]
    assert radar["radar"] == 1 and json.loads(radar["radar_json"]) == ["gender", "securitiz"]
    paff = rows["10.9999/paff.2026.7"]
    assert paff["title"] == "ASEAN Centrality and Hedging" and paff["zh_title"] == "东盟中心地位与对冲"
    assert paff["zh_abstract"].startswith("本文考察") and paff["abstract"] is None
    assert paff["oa_pdf_url"] == "https://oa.example.org/paff7.pdf" and paff["is_oa"] == 1 and paff["radar"] == 1
    assert rows["10.9999/pg.2026.9"]["is_oa"] == 0 and "10.9999/pg.2026.10" not in rows
    assert rows["10.9999/ejir.2026.1"]["zh_abstract"].startswith("传统解释") and rows["10.9999/ejir.2026.1"]["first_seen"].startswith("2026-08-16")
    assert rows["10.9999/ejir.2026.4"]["journal_name"] == "European Journal of International Relations"
    news = {x["link"]: x for x in conn.execute("SELECT * FROM feed_news")}
    scs = news["https://fulcrum.sg/asean-summit-scs/"]
    assert scs["title"] == "The South China Sea at the ASEAN Summit" and scs["zh_title"] == "东盟峰会上的南海议题"
    assert scs["zh_summary"].startswith("文章分析") and scs["angle"].startswith("可从东盟") and scs["published"] == "2026-09-19"
    assert scs["first_seen"].startswith("2026-09-20") and scs["direction"] == "sea-asean"

    again = history.import_all(conn, history_repo)
    assert again["papers"]["new"] == 0 and again["news"]["new"] == 0
    assert again["papers"]["filled"] == 0 and again["news"]["filled"] == 0
    assert conn.execute("SELECT COUNT(*) FROM feed_papers").fetchone()[0] == 6


def test_history_fills_items_a_live_refresh_found_first(conn, history_repo):
    from research_lab.feeds import history, papers
    live = {"id": papers.paper_key("10.9999/paff.2026.7"), "doi": "10.9999/paff.2026.7", "journal": "PAFF",
            "journal_name": "Pacific Affairs", "title": "ASEAN Centrality and Hedging", "abstract": "Live abstract."}
    papers.insert(conn, live)
    r = history.import_all(conn, history_repo)
    assert r["papers"]["new"] == 5 and r["papers"]["filled"] == 1
    row = conn.execute("SELECT * FROM feed_papers WHERE id = ?", (live["id"],)).fetchone()
    assert row["zh_title"] == "东盟中心地位与对冲" and row["abstract"] == "Live abstract." and row["origin"] == "crossref"
    assert row["first_seen"].startswith("2026-09-15") and row["radar"] == 1 and row["oa_pdf_url"] == "https://oa.example.org/paff7.pdf"
    assert history.import_all(conn, history_repo)["papers"]["filled"] == 0


# ---------------------------------------------------------------- routes: status toggles and pages

def _seed(conn, n_papers: int = 3, n_news: int = 2) -> tuple[list[str], list[str]]:
    from research_lab.feeds import news, papers
    pids, nids = [], []
    for i in range(n_papers):
        doi = f"10.9999/seed.{i}"
        row = {"id": papers.paper_key(doi), "doi": doi, "journal": "TPR", "journal_name": "The Pacific Review",
               "journal_column": "亚太", "pool": "hunt", "title": f"ASEAN hedging paper {i}", "authors": ["Lin Tan"],
               "published": (NOW - timedelta(days=i)).strftime("%Y-%m-%d"), "abstract": f"Abstract {i} on ASEAN regionalism.",
               "zh_title": "东盟对冲研究" if i == 0 else None, "direction": "sea-asean"}
        papers.insert(conn, row)
        pids.append(row["id"])
    for i in range(n_news):
        link = f"https://warontherocks.com/seed-{i}/"
        row = {"id": news.news_key(link), "source": "War on the Rocks", "title": f"Reassuring Seoul, part {i}", "link": link,
               "published": (NOW - timedelta(hours=i + 1)).strftime("%Y-%m-%dT%H:%M:%SZ"), "summary": "Extended deterrence.",
               "direction": "alliance-us", "score": 4}
        news.insert(conn, row)
        nids.append(row["id"])
    return pids, nids


def test_home_and_feed_pages(client, conn):
    pids, nids = _seed(conn)
    home = client.get("/").text
    assert 'id="front-tab-news"' in home and 'id="front-tab-papers"' in home and 'href="/feed?tab=papers"' in home
    assert "东盟对冲研究" in home and "ASEAN hedging paper 0" in home and "Reassuring Seoul, part 0" in home
    assert 'id="feed-tabs"' not in home and 'hx-post="/feed/' not in home and "GitHub" not in home
    page = client.get("/feed?tab=papers").text   # 推送 opens on 新闻 since card 0064
    assert '论文<span' in page and '新闻<span' in page and "收藏" in page and "ASEAN hedging paper 2" in page
    assert "摘要待翻译" in page and "Abstract 1 on ASEAN regionalism." not in page                  # 今日精选 (card 0070)
    assert 'class="tabs dir-filter"' in client.get("/feed?tab=papers&pick=all").text   # the direction bar (card 0066)
    news_page = client.get("/feed?tab=news").text
    assert "Reassuring Seoul, part 1" in news_page and "War on the Rocks" in news_page
    assert f'hx-post="/feed/papers/{pids[0]}/read"' in page and f'hx-post="/feed/news/{nids[0]}/save"' in news_page
    assert "还没有刷新过" in news_page
    assert 'id="feed-status"' in client.get("/").text        # 首页 shows the refresh it starts (card 0063)
    assert "ASEAN hedging" in client.get("/feed?tab=papers&q=hedging").text
    assert "没有符合条件的条目" in client.get("/feed?tab=papers&q=nothing-matches-this").text
    assert "ASEAN hedging paper" not in client.get("/feed?tab=papers&direction=alliance-us").text
    assert 'href="/feed"' not in client.get("/").text.split('<nav class="nav"')[1].split("</nav>")[0]   # not in the sidebar


def test_status_toggles(client, conn):
    from research_lab.feeds import store
    pids, nids = _seed(conn)
    r = client.post(f"/feed/papers/{pids[1]}/save")
    assert r.status_code == 200 and "已收藏" in r.text and 'aria-pressed="true"' in r.text
    assert store.get(conn, "papers", pids[1])["status"] == "saved"
    saved = client.get("/feed?tab=saved").text
    assert "ASEAN hedging paper 1" in saved and "还没有收藏新闻" in saved
    r = client.post(f"/feed/papers/{pids[1]}/save", data={"compact": "1"})
    assert "收藏" in r.text and "已收藏" not in r.text and store.get(conn, "papers", pids[1])["status"] == "new"
    assert "<details" not in r.text                                  # the home row stays compact

    r = client.post(f"/feed/news/{nids[0]}/dismiss")
    assert r.status_code == 200 and "已忽略" in r.text and "Undo" in r.text
    assert "Reassuring Seoul, part 0" not in client.get("/").text
    assert "Reassuring Seoul, part 0" not in client.get("/feed?tab=news").text
    assert "Reassuring Seoul, part 0" in client.get("/feed?tab=news&view=dismissed").text
    r = client.post(f"/feed/news/{nids[0]}/restore")
    assert "已忽略" not in r.text and store.get(conn, "news", nids[0])["status"] == "new"

    assert client.post("/feed/papers/nope/save").status_code == 404
    assert client.post("/feed/other/x/save").status_code == 404
    assert client.post(f"/feed/papers/{pids[0]}/save", headers={"HX-Request": ""}).status_code == 403   # guard


def test_load_more(client, conn):
    _seed(conn, n_papers=25, n_news=0)
    page = client.get("/feed?tab=papers&pick=all").text   # the full stream; 今日精选 has no paging
    assert "Load more" in page and page.count('class="stream-row"') == 20
    more = client.get("/feed/more?tab=papers&view=unread&offset=20")
    assert more.status_code == 200 and more.text.count('class="stream-row"') == 5 and "Load more" not in more.text


# ---------------------------------------------------------------- one-click import

DOI = "10.1234/rl.2023.001"     # printed in the sample PDF
TITLE = "Who Convenes Asia? Forum attendance and the durability of ASEAN-led institutions"


def _import_transport(pdf_bytes: bytes | None, calls: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        host, path = request.url.host, request.url.path
        if host == "oa.example.org" and pdf_bytes is not None:
            return httpx.Response(200, content=pdf_bytes, headers={"content-type": "application/pdf"})
        if host == "api.crossref.org" and path == f"/works/{DOI}":
            return httpx.Response(200, json={"status": "ok", "message": {
                "DOI": DOI, "title": [TITLE], "author": [{"family": "Tan", "given": "Lin"}],
                "issued": {"date-parts": [[2023]]}, "container-title": ["The Pacific Review"], "type": "journal-article"}})
        if host == "api.unpaywall.org":
            return httpx.Response(200, json={"is_oa": False, "best_oa_location": None})
        return httpx.Response(404, json={})
    return httpx.MockTransport(handler)


def _feed_paper(conn, *, pdf_url: str | None) -> str:
    from research_lab.feeds import papers
    row = {"id": papers.paper_key(DOI), "doi": DOI, "journal": "TPR", "journal_name": "The Pacific Review", "pool": "hunt",
           "title": TITLE, "authors": ["Lin Tan"], "published": "2023-05-01", "abstract": "Great powers keep attending.",
           "direction": "sea-asean", "oa_pdf_url": pdf_url, "is_oa": 1 if pdf_url else None}
    papers.insert(conn, row)
    return row["id"]


def test_import_with_open_access_pdf(client, conn, sample_pdf):
    item = _feed_paper(conn, pdf_url="https://oa.example.org/tan2023.pdf")
    calls: list[str] = []
    client.app.state.intake_http = httpx.Client(transport=_import_transport(sample_pdf.read_bytes(), calls))
    r = client.post(f"/feed/papers/{item}/import")
    assert r.status_code == 200 and "继续阅读" in r.text and "没有开放获取" not in r.text
    row = conn.execute("SELECT * FROM feed_papers WHERE id = ?", (item,)).fetchone()
    assert row["paper_id"] and row["status"] == "saved"
    paper = conn.execute("SELECT * FROM papers WHERE id = ?", (row["paper_id"],)).fetchone()
    assert paper["doi"] == DOI and paper["metadata_status"] == "verified" and paper["direction"] == "sea-asean"
    f = conn.execute("SELECT * FROM paper_files WHERE paper_id = ?", (paper["id"],)).fetchone()
    assert f["is_primary"] == 1 and f["page_count"] == 3
    assert conn.execute("SELECT COUNT(*) FROM paper_pages WHERE file_id = ?", (f["id"],)).fetchone()[0] == 3
    assert conn.execute("SELECT status FROM ai_jobs WHERE kind = 'paper.summarize' AND target_id = ?", (paper["id"],)).fetchone()[0] == "queued"
    assert any("oa.example.org" in c for c in calls)
    assert f'href="/papers/{paper["id"]}"' in r.text
    again = client.post(f"/feed/papers/{item}/import")
    assert "继续阅读" in again.text and conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1


def test_import_without_pdf_keeps_metadata_only(client, conn, monkeypatch):
    monkeypatch.setenv("CONTACT_EMAIL", "lab@example.org")
    item = _feed_paper(conn, pdf_url=None)
    calls: list[str] = []
    client.app.state.intake_http = httpx.Client(transport=_import_transport(None, calls))
    r = client.post(f"/feed/papers/{item}/import", data={"compact": "1"})
    assert r.status_code == 200 and "没有开放获取的 PDF，可稍后上传" in r.text and "继续阅读" in r.text
    assert any("api.unpaywall.org" in c and "email=lab%40example.org" in c for c in calls)
    row = conn.execute("SELECT * FROM feed_papers WHERE id = ?", (item,)).fetchone()
    paper = conn.execute("SELECT * FROM papers WHERE id = ?", (row["paper_id"],)).fetchone()
    assert paper["doi"] == DOI and paper["title"] == TITLE and paper["direction"] == "sea-asean" and row["is_oa"] == 0
    assert conn.execute("SELECT COUNT(*) FROM paper_files WHERE paper_id = ?", (paper["id"],)).fetchone()[0] == 0


def test_import_rejects_html_instead_of_pdf(client, conn):
    item = _feed_paper(conn, pdf_url="https://oa.example.org/landing")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oa.example.org":
            return httpx.Response(200, text="<html>Sign in</html>", headers={"content-type": "text/html"})
        return httpx.Response(404, json={})
    client.app.state.intake_http = httpx.Client(transport=httpx.MockTransport(handler))
    r = client.post(f"/feed/papers/{item}/import")
    assert "开放获取的 PDF 暂时下载不了，可稍后上传" in r.text and "继续阅读" in r.text
    from research_lab.config import get_paths
    assert not list((get_paths().cache / "incoming").glob("feed-*.pdf"))


def test_public_url_guard():
    from research_lab.feeds.net import public_url
    assert public_url("https://www.cambridge.org/x.pdf")
    for bad in ("file:///etc/passwd", "http://localhost:4100/papers", "http://127.0.0.1/x", "http://192.168.1.2/x", "ftp://a/b", None):
        assert not public_url(bad), bad


# ---------------------------------------------------------------- assistant batch job

def test_enrich_job_batches_and_guardrails(conn):
    from research_lab.ai import queue
    from research_lab.feeds import enrich
    pids, nids = _seed(conn, n_papers=2, n_news=2)
    jobs = enrich.enqueue_pending(conn, dispatch_jobs=False)
    assert len(jobs) == 2                                    # one news batch, one paper batch
    assert enrich.enqueue_pending(conn, dispatch_jobs=False) == []      # items are already in open batches
    news_job = next(queue.get(conn, j) for j in jobs if queue.get(conn, j)["target_id"].startswith("news:"))
    paper_job = next(queue.get(conn, j) for j in jobs if queue.get(conn, j)["target_id"].startswith("papers:"))
    user = json.loads(news_job["request_json"])["messages"][1]["content"]
    assert "i1" in user and "i2" in user and "Reassuring Seoul, part 0" in user and "alliance-us" in user
    assert "never invent" in user
    # the paper with an existing Chinese title is not sent again
    assert "ASEAN hedging paper 0" not in json.loads(paper_job["request_json"])["messages"][1]["content"]

    queue.complete(conn, news_job["id"], {"items": [
        {"id": "i1", "zh_title": "再保证首尔（一）", "zh_summary": "讨论延伸威慑。", "why_it_matters": "与美韩同盟课题相关。",
         "direction": "alliance-us", "score": "7", "category": "地区安全"},
        {"id": "i2", "zh_title": "再保证首尔（二）", "direction": "no-such-direction", "score": 1, "category": "编造的分类"},
        {"id": "i99", "zh_title": "编造的条目"},
    ]}, provider="test", model="t")
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM feed_news")}
    first = rows[nids[0]]
    assert first["zh_title"] == "再保证首尔（一）" and first["score"] == 5 and first["angle"] == "与美韩同盟课题相关。"
    assert first["category"] == "地区安全" and first["direction_source"] == "ai" and first["enriched_at"]
    second = rows[nids[1]]
    assert second["direction"] == "alliance-us" and second["direction_source"] == "keyword" and second["category"] is None
    assert "编造的条目" not in json.dumps([dict(r) for r in rows.values()], ensure_ascii=False)
    assert enrich.enqueue_pending(conn, dispatch_jobs=False) == []

    queue.cancel(conn, paper_job["id"])                       # a cancelled batch goes back to the pool
    assert len(enrich.enqueue_pending(conn, dispatch_jobs=False)) == 1


def test_enrich_caps_open_batches_and_recovers_abandoned_claims(conn):
    """Card 0083: only today's picks go out, a batch per stream; open batches count against the cap, and a claim
    nobody answers is cancelled and its picks are queued again."""
    from research_lab.ai import queue
    from research_lab.feeds import enrich
    _seed(conn, n_papers=12, n_news=2)
    first = enrich.enqueue_pending(conn, dispatch_jobs=False, max_batches=1)
    assert len(first) == 1 and queue.get(conn, first[0])["target_id"].startswith("news:")   # the papers wait
    assert enrich.enqueue_pending(conn, dispatch_jobs=False, max_batches=1) == []    # cap reached while it waits
    queue.claim(conn, first[0])
    conn.execute("UPDATE ai_jobs SET claimed_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (first[0],))
    again = enrich.enqueue_pending(conn, dispatch_jobs=False, max_batches=1)
    assert queue.get(conn, first[0])["status"] == "cancelled" and len(again) == 1
    batch = queue.get(conn, again[0])["target_id"]
    assert conn.execute("SELECT COUNT(*) FROM feed_news WHERE enrich_batch = ?", (batch,)).fetchone()[0] == 2


def test_enrich_job_is_registered_for_the_cli():
    from research_lab.ai import registry
    import research_lab.cli  # noqa: F401 - the CLI imports its sub-apps, including feeds
    assert "feeds.enrich" in registry.load_all()


# ---------------------------------------------------------------- scheduling

def test_scheduler_off_in_tests_and_due_logic(conn):
    from research_lab.db.connection import now_iso, set_setting
    from research_lab.feeds import papers, scheduler, store
    assert not scheduler.enabled() and scheduler.start() is False
    assert scheduler.seconds_until_due(conn) == 0
    set_setting(conn, store.LAST_KEY, {"at": now_iso()})
    assert scheduler.seconds_until_due(conn) > scheduler.INTERVAL - 60
    assert papers.default_days(None) == 21 and scheduler.news_days(None) == 4
    assert papers.default_days("2026-09-30T08:00:00+00:00", today=datetime(2026, 10, 1).date()) == 4


def test_manual_refresh_runs_in_background_and_isolates_failures(client, conn):
    from research_lab.feeds import scheduler, sources, store
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "fulcrum.sg":
            return httpx.Response(200, text=rss([("ASEAN centrality", "https://fulcrum.sg/c/", YESTERDAY)]))
        return httpx.Response(500, text="down")
    client.app.state.feeds_http = httpx.Client(transport=httpx.MockTransport(handler))
    r = client.post("/feed/refresh")
    assert r.status_code == 200 and 'id="feed-status"' in r.text
    scheduler.wait(60)
    assert not scheduler.running()
    last = store.last_refresh(conn)
    assert last["papers"]["new"] == 0 and len(last["papers"]["failed"]) == 42
    assert last["news"]["new"] == 1 and len(last["news"]["failed"]) == len(sources.load().news) - 1
    assert len(last["ai_jobs"]) == 1                      # the new news item was handed to the assistant
    done = client.get("/feed/status?primary=1&polling=1")
    assert "刷新完成" in done.text and "新闻 1 条" in done.text and f"{42 + len(sources.load().news) - 1} 个来源没有响应" in done.text
    assert "上次刷新" in client.get("/feed/status").text


def test_refresh_lease_blocks_a_second_process(conn):
    import time
    from research_lab.db.connection import set_setting
    from research_lab.feeds import scheduler
    set_setting(conn, scheduler.LEASE_KEY, {"pid": 999999, "until": time.time() + 600})
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as http:
        assert scheduler.run_refresh(http) is None
    set_setting(conn, scheduler.LEASE_KEY, {"pid": 999999, "until": time.time() - 1})    # expired: taken over
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as http:
        assert scheduler.run_refresh(http, ai=False) is not None


def test_dev_page_lists_disabled_news_sources(client):
    page = client.get("/dev").text   # the sources detail left 设置 (card 0081)
    assert "已停用：East Asia Forum（" in page and "CSIS（" in page



def _navigator(conn, *, score=5, category="地区安全"):
    conn.execute("INSERT INTO feed_news(id, source, title, link, zh_title, score, category, published, first_seen) "
                 "VALUES ('nav', '9DASHLINE', 'The Navigator’s September Issue — Out Now', 'https://example.org/nav', "
                 "'9DASHLINE《领航者》九月刊现已发布', ?, ?, ?, ?)", (score, category, NOW.strftime("%Y-%m-%d"), NOW.isoformat()))
    for i in range(4):
        conn.execute("INSERT INTO feed_news(id, source, title, link, zh_title, score, published, first_seen) VALUES (?,?,?,?,?,?,?,?)",
                     (f"ok{i}", "The Diplomat", f"Seoul and the alliance {i}", f"https://example.org/ok{i}", f"首尔与同盟{i}", 4,
                      NOW.strftime("%Y-%m-%d"), NOW.isoformat()))


def test_an_announcement_stays_off_the_home_page_before_any_refresh(conn):
    """Card 0056: a row stored before the rule existed (category still 地区安全, score 5) never reaches 首页."""
    from research_lab.feeds import store
    _navigator(conn)
    home, slides = store.home(conn), store.headlines(conn)
    assert "nav" not in {it["id"] for it in home["news"]} and len(home["news"]) == 2      # two per outlet (card 0083)
    assert "nav" not in {it["id"] for it in slides["news"]} and len(slides["news"]) == 2


def test_the_scheduler_applies_the_rule_at_startup(conn, monkeypatch):
    from research_lab.feeds import scheduler
    _navigator(conn)
    conn.commit()
    monkeypatch.setenv("RL_FEEDS_AUTO", "1")
    monkeypatch.setattr(scheduler, "_loop", lambda: None)     # no network: only the startup pass runs
    monkeypatch.setattr(scheduler, "_loop_thread", None)
    assert scheduler.start()
    scheduler.stop()
    assert conn.execute("SELECT category, score FROM feed_news WHERE id = 'nav'").fetchone()[:] == ("非新闻", 0)
