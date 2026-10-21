"""Papers V2: Chinese papers (language, CJK search, sections), translation, Chinese summaries with a
direction suggestion, radar direction chips, the news tab and the library filters."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

CJK_TITLE = "东盟中心地位的形成机制研究"


def _failing() -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500, text="down")))


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
def cjk_pdf(tmp_path: Path) -> Path:
    """A three-page Chinese article typeset with PyMuPDF's built-in china-s font."""
    import pymupdf
    doc = pymupdf.open()
    pages = [
        [(CJK_TITLE, 20), ("张明（北京大学国际关系学院）", 10),
         ("摘要：本文考察东盟中心地位在东亚地区合作中的形成机制。", 10),
         ("研究发现，大国之间的相互否决使东盟成为各方都能接受的召集者。", 10),
         ("关键词：东盟；中心地位；地区合作；2021年", 10)],
        [("一、引言", 12), ("冷战结束以来，东盟主导的多边机制不断扩展，", 10),
         ("东亚峰会与东盟地区论坛成为大国互动的重要平台。", 10),
         ("三、研究设计", 12), ("本文对二〇〇五年至二〇二二年的峰会出席情况进行编码，", 10),
         ("并以过程追踪检验相互否决机制。", 10)],
        [("五、结论", 12), ("东盟中心地位是大国相互否决的剩余结果，而非东盟自身实力的产物。", 10),
         ("未来的研究可以比较不同议题领域中召集权的变化。", 10),
         ("参考文献", 12), ("阿查亚：《建构安全共同体》，上海人民出版社二〇〇四年版。", 9)],
    ]
    for lines in pages:
        page = doc.new_page()
        y = 80
        for text, size in lines:
            page.insert_text((60, y), text, fontname="china-s", fontsize=size)
            y += size * 2.2
    p = tmp_path / "zhang2021.pdf"
    doc.save(p)
    doc.close()
    return p


@pytest.fixture()
def cjk_paper(conn, cjk_pdf):
    from research_lab.intake import pipeline
    with _failing() as http:
        return pipeline.ingest(conn, cjk_pdf, http=http, ai=False)


# ---------------------------------------------------------------- Chinese papers

def test_cjk_pdf_language_title_sections(conn, cjk_paper):
    from research_lab.intake import pipeline
    r = cjk_paper
    assert r.created and r.language == "zh"
    paper = pipeline.get_paper(conn, r.paper_id)
    assert paper["language"] == "zh"
    assert paper["title"] == CJK_TITLE
    assert paper["metadata_status"] == "unverified"             # Crossref down / no CNKI coverage: stays unverified
    assert any("Crossref" in n for n in r.notes)
    assert r.paper_id.startswith("anon") and "-zh" in r.paper_id  # no ASCII word in the title → hashed noun
    f = pipeline.primary_file(conn, r.paper_id)
    assert f["has_text_layer"] == 1
    kinds = {row[0] for row in conn.execute("SELECT kind FROM paper_sections WHERE file_id = ?", (f["id"],))}
    assert {"abstract", "introduction", "methods", "conclusion", "references"} <= kinds


def test_cjk_summary_material_skips_references(conn, cjk_paper):
    from research_lab.ai import registry
    _mode, text = registry.get("paper.summarize").material(conn, cjk_paper.paper_id)
    assert "Language: zh" in text and "相互否决" in text
    assert "建构安全共同体" not in text                          # 参考文献 never enter a prompt


def test_detect_language_classes():
    from research_lab.intake.extract import detect_language
    assert detect_language("东盟中心地位的形成机制研究，本文考察东盟在东亚地区合作中的作用。") == "zh"
    assert detect_language("ASEANの中心性について、本稿では東アジアの地域協力を検討する。これはテストです。") == "ja"
    assert detect_language("아세안 중심성은 동아시아 지역 협력에서 중요한 역할을 한다고 본 연구는 주장한다.") == "ko"
    assert detect_language("The durability of ASEAN-led institutions is a puzzle for the theory of the field.") == "en"
    assert detect_language("La centralité de l'ASEAN reste une énigme pour les chercheurs européens aujourd'hui.") == "other"
    assert detect_language("12 34") is None


def test_cjk_fulltext_search(conn, cjk_paper, client):
    from research_lab.db.connection import now_iso
    from research_lab.web.routes.search import search_all
    res = search_all(conn, "中心地位")                           # trigram MATCH (3+ characters)
    hits = [h for g in res["pages"] for h in g["hits"]]
    assert res["pages"] and res["pages"][0]["paper_id"] == cjk_paper.paper_id
    assert any("<mark>中心地位</mark>" in str(h["snippet"]) for h in hits)
    two = search_all(conn, "否决")                               # two characters: LIKE fallback
    assert any(g["paper_id"] == cjk_paper.paper_id for g in two["pages"])
    ts = now_iso()
    conn.execute("INSERT INTO excerpts(id, paper_id, locator, quote, my_note, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                 ("EXC-2026-0901", cjk_paper.paper_id, "1", "大国之间的相互否决使东盟成为召集者", "机制", ts, ts))
    ex = search_all(conn, "相互否决")
    assert [e["id"] for e in ex["excerpts"]] == ["EXC-2026-0901"]
    assert any(p["id"] == cjk_paper.paper_id for p in search_all(conn, "形成机制")["papers"])
    page = client.get("/search?q=中心地位")
    assert page.status_code == 200 and "<mark>中心地位</mark>" in page.text and "<h2>正文</h2>" in page.text
    # both indexes hit page 0 for 东盟; the merge keeps one row per page
    idx = [h["page_index"] for g in search_all(conn, "东盟中心")["pages"] for h in g["hits"]]
    assert idx and len(idx) == len(set(idx))


def test_auto_translate_on_cjk_intake(conn, cjk_pdf):
    from research_lab.db.connection import set_setting
    from research_lab.intake import pipeline
    with _failing() as http:
        r = pipeline.ingest(conn, cjk_pdf, http=http)
    assert r.job_id and r.translate_jobs
    jobs = conn.execute("SELECT kind, target_id FROM ai_jobs WHERE id IN (%s)" % ",".join("?" * len(r.translate_jobs)),
                        r.translate_jobs).fetchall()
    assert {j["kind"] for j in jobs} == {"paper.translate"} and {j["target_id"] for j in jobs} == {f"{r.paper_id}:en"}
    # the setting switches it off
    conn.execute("DELETE FROM ai_jobs")
    conn.execute("DELETE FROM papers")
    set_setting(conn, "ai.auto_translate_cjk", False)
    with _failing() as http:
        r2 = pipeline.ingest(conn, cjk_pdf, http=http)
    assert r2.created and r2.translate_jobs == []


# ---------------------------------------------------------------- translation

def _complete_chunks(conn, job_ids, prefix="EN"):
    from research_lab.ai import queue
    for jid in job_ids:
        job = queue.get(conn, jid)
        params = json.loads(job["request_json"])["params"]
        pages = [{"page_index": i, "text": f"{prefix} translation of page {i}"} for i in params["page_indexes"]]
        pages.append({"page_index": 99, "text": "stray page outside the chunk"})
        queue.complete(conn, jid, {"pages": pages}, provider="claude-code", model="test")


def test_translate_job_chunks_and_apply(conn, cjk_paper, monkeypatch):
    from research_lab.ai import queue
    from research_lab.ai.jobs import paper_translate
    from research_lab.intake import pipeline
    monkeypatch.setattr(paper_translate, "CHUNK_TOKENS", 60)    # force one page per chunk
    pid = cjk_paper.paper_id
    ids = pipeline.enqueue_translation(conn, pid, "en")
    assert len(ids) == 3
    ranges = [json.loads(queue.get(conn, j)["request_json"])["params"]["page_range"] for j in ids]
    assert ranges == [[0, 0], [1, 1], [2, 2]]
    first = queue.get(conn, ids[0])
    assert first["target_id"] == f"{pid}:en" and "=== page_index 0 · p. 1 ===" in json.loads(first["request_json"])["messages"][1]["content"]
    assert pipeline.enqueue_translation(conn, pid, "en") == []   # chunks already open are not queued twice
    st = pipeline.translation_status(conn, pid, "en")
    assert (st["chunks_done"], st["chunks_total"], st["chunks_open"]) == (0, 3, 3)

    _complete_chunks(conn, ids[:2])
    st = pipeline.translation_status(conn, pid, "en")
    assert (st["chunks_done"], st["chunks_total"]) == (2, 3) and not st["complete"]
    _complete_chunks(conn, ids[2:])
    rows = conn.execute("SELECT page_index, lang_from, lang_to, text, ai_run_id FROM paper_translations WHERE paper_id = ? "
                        "ORDER BY page_index", (pid,)).fetchall()
    assert [r["page_index"] for r in rows] == [0, 1, 2]          # the stray page 99 was dropped
    assert all(r["lang_from"] == "zh" and r["lang_to"] == "en" and r["ai_run_id"] for r in rows)
    assert pipeline.translation_status(conn, pid, "en")["complete"]
    assert pipeline.enqueue_translation(conn, pid, "en") == []   # nothing left to translate
    assert len(pipeline.enqueue_translation(conn, pid, "en", force=True)) == 3
    with pytest.raises(ValueError):
        pipeline.enqueue_translation(conn, pid, "zh")            # already Chinese


def test_translate_apply_rejects_out_of_range(conn, cjk_paper, monkeypatch):
    from research_lab.ai import queue
    from research_lab.ai.jobs import paper_translate
    from research_lab.intake import pipeline
    monkeypatch.setattr(paper_translate, "CHUNK_TOKENS", 60)
    ids = pipeline.enqueue_translation(conn, cjk_paper.paper_id, "en")
    with pytest.raises(ValueError):
        queue.complete(conn, ids[0], {"pages": [{"page_index": 2, "text": "wrong chunk"}]}, provider="claude-code", model="test")
    assert conn.execute("SELECT COUNT(*) FROM paper_translations").fetchone()[0] == 0


def test_reader_views_and_translation_panel(client, cjk_pdf, monkeypatch):
    from research_lab.ai.jobs import paper_translate
    from research_lab.db.connection import connect
    from research_lab.intake import pipeline
    monkeypatch.setattr(paper_translate, "CHUNK_TOKENS", 60)
    conn = connect()
    with _failing() as http:
        pid = pipeline.ingest(conn, cjk_pdf, http=http, ai=False).paper_id
    detail = client.get(f"/papers/{pid}")
    assert "翻译为英文" in detail.text and "翻译为中文" not in detail.text
    panel = client.post(f"/papers/{pid}/translate", data={"lang": "en"})
    assert panel.status_code == 200 and "已排入 3 个翻译任务" in panel.text and "0/3 段" in panel.text
    ids = [r[0] for r in conn.execute("SELECT id FROM ai_jobs WHERE kind = 'paper.translate' ORDER BY id")]
    _complete_chunks(conn, ids)
    panel = client.get(f"/papers/{pid}/translation")
    assert "已完成" in panel.text and "对照阅读" in panel.text

    both = client.get(f"/papers/{pid}/read?view=both&lang=en")
    assert both.status_code == 200
    assert 'class="reading-stage view-both"' in both.text and 'id="pdf-viewer"' in both.text
    assert "EN translation of page 1" in both.text
    assert 'lang="en"' in both.text and both.text.count('data-page-index="') == 3
    trans = client.get(f"/papers/{pid}/read?view=trans")
    assert "EN translation of page 0" in trans.text and 'id="pdf-viewer"' not in trans.text
    orig = client.get(f"/papers/{pid}/read")
    assert 'id="pdf-viewer"' in orig.text and "EN translation" not in orig.text and "对照" in orig.text
    none = client.get(f"/papers/{pid}/read?view=both&lang=zh")
    assert 'class="reading-stage view-both"' not in none.text and "还没有中文译文" in none.text
    conn.close()


# ---------------------------------------------------------------- Chinese summary + direction

def chinese_summary(direction: str = "sea-asean") -> dict:
    return {
        "research_question": {"text": "东盟中心地位是如何形成的？", "pages": ["1"]},
        "argument": {"text": "大国相互否决使东盟成为可接受的召集者。", "pages": ["1", "3"]},
        "mechanism": {"text": "相互否决", "pages": ["1"]},
        "method": {"text": "峰会出席编码与过程追踪", "pages": ["2"]},
        "relevance_to_project": {"text": "直接回应项目关于东盟中心地位来源的问题。", "pages": ["3"]},
        "key_terms": ["中心地位", "召集权"],
        "direction": {"slug": direction, "why": "研究对象是东盟主导的地区制度。"},
    }


def test_summary_prompt_is_chinese_with_directions(conn, cjk_pdf, sample_pdf, full_summaries):
    from research_lab.intake import pipeline
    with _failing() as http:
        r = pipeline.ingest(conn, cjk_pdf, http=http)
    req = json.loads(conn.execute("SELECT request_json FROM ai_jobs WHERE id = ?", (r.job_id,)).fetchone()[0])
    system, user = req["messages"][0]["content"], req["messages"][1]["content"]
    assert "Simplified Chinese" in system and "Write English." not in system
    assert "sea-asean" in user and "alliance-us" in user and "Simplified Chinese" in user
    assert "中文译名 (original term)" in user and "Language: zh" in user


def test_chinese_summary_direction_suggestion_and_adopt(client, cjk_pdf, full_summaries):
    from research_lab.ai import queue
    from research_lab.db.connection import connect
    from research_lab.intake import pipeline
    conn = connect()
    with _failing() as http:
        r = pipeline.ingest(conn, cjk_pdf, http=http)
    pipeline.set_direction(conn, r.paper_id, None)          # imports now take now.project's direction; start unfiled
    queue.complete(conn, r.job_id, chinese_summary(), provider="claude-code", model="test")
    p = pipeline.get_paper(conn, r.paper_id)
    data = json.loads(p["ai_summary_json"])
    assert data["argument"]["text"].startswith("大国") and data["direction"] == {"slug": "sea-asean", "why": "研究对象是东盟主导的地区制度。"}
    assert p["direction"] is None                               # a suggestion until the user adopts it
    detail = client.get(f"/papers/{r.paper_id}")
    assert "方向建议" in detail.text and "东南亚与东盟研究" in detail.text and f"/papers/{r.paper_id}/adopt/direction" in detail.text
    adopted = client.post(f"/papers/{r.paper_id}/adopt/direction")
    assert adopted.status_code == 200 and adopted.headers.get("hx-trigger") == "paper-direction-changed"
    assert "已采纳" in adopted.text
    assert pipeline.get_paper(conn, r.paper_id)["direction"] == "sea-asean"
    card = client.post(f"/papers/{r.paper_id}/direction", data={"direction": "alliance-us"})
    assert card.status_code == 200 and "已保存" in card.text
    assert pipeline.get_paper(conn, r.paper_id)["direction"] == "alliance-us"
    bad = client.post(f"/papers/{r.paper_id}/direction", data={"direction": "made-up"})
    assert "未知方向" in bad.text and pipeline.get_paper(conn, r.paper_id)["direction"] == "alliance-us"
    conn.close()


def test_invented_direction_is_dropped(conn, cjk_pdf):
    from research_lab.ai import queue
    from research_lab.intake import pipeline
    with _failing() as http:
        r = pipeline.ingest(conn, cjk_pdf, http=http)
    queue.complete(conn, r.job_id, chinese_summary("martian-studies"), provider="claude-code", model="test")
    data = json.loads(pipeline.get_paper(conn, r.paper_id)["ai_summary_json"])
    assert data["direction"] == {"slug": None, "why": None}
    with pytest.raises(LookupError):
        pipeline.adopt(conn, r.paper_id, "direction")


# ---------------------------------------------------------------- library

def test_library_is_an_archive_rail_and_shelf(client, cjk_pdf, sample_pdf):
    """Card 0031: 档案栏 (全部 · 未归档 · 方向 › 课题), the shelf with a reading-status filter, 待补 PDF for
    bibliography-only records, and no verification noise in the list."""
    from research_lab.db.connection import connect, now_iso
    from research_lab.intake import pipeline
    conn = connect()
    with _failing() as http:
        zh = pipeline.ingest(conn, cjk_pdf, http=http, ai=False).paper_id
        en = pipeline.ingest(conn, sample_pdf, http=http, ai=False).paper_id
    pipeline.set_direction(conn, zh, "sea-asean")
    conn.execute("INSERT OR IGNORE INTO paper_projects(paper_id, project_slug) VALUES (?, 'asean-centrality')", (zh,))
    conn.execute("DELETE FROM paper_projects WHERE paper_id = ?", (en,))          # ingest links to now.project; keep one out
    pipeline.set_direction(conn, en, None)                                         # …and gives it that project's direction
    conn.execute("UPDATE papers SET read_depth = 'read' WHERE id = ?", (zh,))
    ts = now_iso()
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, direction, metadata_status, created_at, updated_at) "
                 "VALUES ('goh2013-struggle', 'The Struggle for Order', 'h-goh', '[\"Goh, Evelyn\"]', 2013, 'strategy-signaling', 'unverified', ?, ?)", (ts, ts))

    page = client.get("/papers?view=all").text
    assert "东南亚与东盟研究" in page and "课题\nASEAN Centrality" in page          # the 课题 mark's tooltip (card 0128)
    assert CJK_TITLE in page and "Who Convenes Asia?" in page
    assert "引用信息待核验" not in page and "<th>" not in page            # verification lives on the paper page now
    # card 0057: a bibliography-only record stays off the list, behind 待补原文
    assert "The Struggle for Order" not in page and 'data-tip="待补原文"' not in page and "待补原文 1</a>" in page
    missing = client.get("/papers?src=missing").text
    assert "The Struggle for Order" in missing and missing.count('data-tip="待补原文"') == 1 and "Who Convenes Asia?" not in missing
    assert 'href="/excerpts"' in page and 'href="/papers?view=folders"' in page and 'data-drop-project=' not in page
    assert 'data-tip="读完' in page and 'draggable="true"' in page and "/static/papers.js" in page   # the drop handler is loaded

    by_dir = client.get("/papers?direction=sea-asean").text
    assert CJK_TITLE in by_dir and "Who Convenes Asia?" not in by_dir and '<span class="current">东南亚与东盟研究</span>' in by_dir
    client.get("/papers?direction=")                                # 全部 forgets the remembered pick (card 0066)
    unsorted = client.get("/papers?direction=none").text
    assert "Who Convenes Asia?" in unsorted and CJK_TITLE in unsorted and "The Struggle for Order" not in unsorted
    by_project = client.get("/papers?project=asean-centrality").text
    assert CJK_TITLE in by_project and "Who Convenes Asia?" not in by_project
    unread = client.get("/papers?status=unread").text
    assert "Who Convenes Asia?" in unread and CJK_TITLE not in unread
    assert CJK_TITLE in client.get("/papers?status=done").text
    assert CJK_TITLE in client.get("/papers?q=中心地位").text         # Chinese title search without segmentation
    assert "这个课题还没有文献" in client.get("/papers?project=us-rok-reassurance").text
    assert pipeline.get_paper(conn, en)["language"] == "en"


def test_archive_post_files_a_paper(client, sample_pdf):
    """Dropping a row on the rail: 方向 sets the direction, 课题 links it and fills a missing direction."""
    from research_lab.db.connection import connect
    from research_lab.intake import pipeline
    conn = connect()
    with _failing() as http:
        en = pipeline.ingest(conn, sample_pdf, http=http, ai=False).paper_id
    r = client.post(f"/papers/{en}/archive", data={"project": "asean-centrality"}, headers={"HX-Target": "library"})
    assert r.status_code == 204
    assert pipeline.get_paper(conn, en)["direction"] == "sea-asean"           # taken from the 课题
    assert conn.execute("SELECT 1 FROM paper_projects WHERE paper_id = ? AND project_slug = 'asean-centrality'", (en,)).fetchone()
    r = client.post(f"/papers/{en}/archive", data={"direction": "alliance-us"}, headers={"HX-Target": "library"})
    assert r.status_code == 204 and pipeline.get_paper(conn, en)["direction"] == "alliance-us"
    r = client.post(f"/papers/{en}/archive", data={"direction": ""}, follow_redirects=False)
    assert r.status_code in (204, 303) and pipeline.get_paper(conn, en)["direction"] is None
    assert client.post(f"/papers/{en}/archive", data={"direction": "nope"}).status_code == 400
    assert client.post(f"/papers/{en}/archive", data={"project": "nope"}).status_code == 400
    assert client.post("/papers/no-such/archive", data={"direction": ""}).status_code == 404
    assert "Who Convenes Asia?" in client.get("/papers?direction=none").text
    en_detail = client.get(f"/papers/{en}")
    assert "翻译为中文" in en_detail.text and "翻译为英文" not in en_detail.text
    conn.close()


# ---------------------------------------------------------------- radar

DIGEST_MD = """# 核心池周报 2026-09-28

时间窗：2026-09-20 起（近 8 天）。共 2 篇；🎯 雷达命中 1 篇；跳过疑似书评 0 篇。

| 期刊 | 新文 | 命中 |
|---|---|---|
| IA | 2 | 1 |

## IA ｜ International Affairs ｜ 综合

- **🎯 ASEAN centrality and hedging in the Indo-Pacific**
  Lin Tan ｜ 2026-09-25 ｜ https://doi.org/10.9999/ia.2026.1
  摘要：How ASEAN regionalism survives great-power rivalry.

- **Costly signals and crisis bargaining revisited**
  K. Lee ｜ 2026-09-26 ｜ https://doi.org/10.9999/ia.2026.2
  摘要：Resolve, credibility and deterrence in coercive diplomacy.
"""

NEWS_MD = """# 时评精选 2026-09-28

近 2 天 ｜ 扫描 3 条，入选 2 条（相关性 ≥3 且为分析文）

## 1. 东盟峰会上的南海议题

Fulcrum · ISEAS ｜ 2026-09-27 ｜ [原文](https://fulcrum.sg/asean-summit-scs/)

原题：The South China Sea at the ASEAN Summit

文章分析东盟峰会如何处理南海争端，以及东盟中心地位面临的压力。

> ✍️ 可从东盟中心地位与议程设置切入。

## 2. 美韩同盟的延伸威慑再保证

War on the Rocks ｜ 2026-09-27 ｜ [原文](https://warontherocks.com/us-rok-extended-deterrence/)

原题：Reassuring Seoul: Extended Deterrence after the Summit

文章讨论美国如何向韩国提供延伸威慑再保证。

> 📎 事件×理论：[《Reassurance and alliance politics》](https://doi.org/10.1080/abc.2026.1)（TPR）— 可用于解释再保证机制。
"""


@pytest.fixture()
def digest_repo(repo: Path, conn) -> Path:
    from research_lab.db.connection import set_setting
    root = repo.parent / "digest-clone"
    (root / "digests" / "news").mkdir(parents=True)
    (root / "digests" / "digest-2026-09-28.md").write_text(DIGEST_MD, encoding="utf-8")
    (root / "digests" / "news" / "news-2026-09-28.md").write_text(NEWS_MD, encoding="utf-8")
    set_setting(conn, "digest.repo_path", str(root))
    return root


def test_radar_papers_direction_chips_and_save(client, digest_repo, conn):
    from research_lab import digest
    d = digest.latest(conn)
    assert [i.direction for i in d.items] == ["sea-asean", "strategy-signaling"]
    page = client.get("/digest")
    assert page.status_code == 200
    assert 'data-direction="sea-asean"' in page.text and "东南亚与东盟研究" in page.text and "战略研究与信号分析" in page.text
    assert "Save to library" in page.text and '"direction": "sea-asean"' in page.text
    client.app.state.intake_http = _failing()
    saved = client.post("/digest/save/0", data={"direction": "sea-asean"})
    assert saved.status_code == 200 and "已保存" in saved.text
    row = conn.execute("SELECT direction, language FROM papers").fetchone()
    assert row["direction"] == "sea-asean" and row["language"] == "en"


def test_news_tab_and_arrange(client, digest_repo, conn):
    from research_lab.ai import queue
    page = client.get("/digest?tab=news")
    assert page.status_code == 200
    assert "东盟峰会上的南海议题" in page.text and "Fulcrum · ISEAS" in page.text and "https://fulcrum.sg/asean-summit-scs/" in page.text
    assert "文章分析东盟峰会如何处理南海争端" in page.text and "Reassurance and alliance politics" in page.text
    assert 'data-direction="sea-asean"' in page.text and 'data-direction="alliance-us"' in page.text
    assert "Organize with AI" in page.text

    r = client.post("/digest/news/arrange", data={"date": "2026-09-28"}, headers={"HX-Current-URL": "http://127.0.0.1:4100/digest"})
    assert r.status_code == 204 and "tab=news" in r.headers["HX-Redirect"]
    job = queue.latest_for(conn, "news.arrange", "2026-09-28")
    assert job["status"] == "queued"
    user = json.loads(job["request_json"])["messages"][1]["content"]
    assert "n1" in user and "n2" in user and "us-rok-reassurance" in user
    queue.complete(conn, job["id"], {
        "clusters": [
            {"theme": "东盟与南海", "item_ids": ["n1", "n9"], "why_it_matters": "东盟中心地位受压。", "direction": "sea-asean"},
            {"theme": "同盟再保证", "item_ids": ["n2"], "why_it_matters": "延伸威慑。", "direction": "alliance-us"},
            {"theme": "编造的主题", "item_ids": ["n7"], "direction": "sea-asean"},
        ],
        "project_links": [{"item_id": "n2", "project_slug": "us-rok-reassurance", "note": "再保证背景"},
                          {"item_id": "n1", "project_slug": "no-such-project"}],
    }, provider="claude-code", model="test")
    from research_lab import digest
    arr = digest.arrangement(conn, "2026-09-28")
    assert [c["item_ids"] for c in arr["clusters"]] == [["n1"], ["n2"]]
    assert [(l["item_id"], l["project_slug"]) for l in arr["project_links"]] == [("n2", "us-rok-reassurance")]
    page = client.get("/digest?tab=news&date=2026-09-28")
    assert "东盟与南海" in page.text and "同盟再保证" in page.text and "编造的主题" not in page.text
    assert "Reorganize" in page.text and "与项目的关联" in page.text


def test_news_tab_reads_real_clone(client, conn):
    from research_lab import digest
    if not digest.news_files(conn):
        pytest.skip("no local clone of the digest repo")
    news = digest.news_for(conn)
    assert news.items and all(i.url.startswith("http") and i.source and i.date for i in news.items)
    page = client.get("/digest?tab=news")
    assert page.status_code == 200 and news.items[0].zh_title in page.text


# ---------------------------------------------------------------- card 0032: the paper is its reader

def test_paper_page_opens_pdf_with_summary_and_secondary_information(client, sample_pdf):
    from research_lab.db.connection import connect
    from research_lab.intake import pipeline
    conn = connect()
    with _failing() as http:
        pid = pipeline.ingest(conn, sample_pdf, http=http, ai=False).paper_id
    pipeline.set_direction(conn, pid, "sea-asean")
    page = client.get(f"/papers/{pid}").text
    assert 'id="reader"' in page and 'id="pdf-viewer"' in page
    assert '<section class="pdf-page"' not in page
    assert 'id="side-summary"' in page and 'id="side-summary" hidden' not in page
    # card 0129: no 文献信息与归属 fold; citation and translation live in the side panel's 信息 tab
    assert 'id="reading-information"' not in page and 'id="summary-card"' in page and 'id="paper-props"' in page
    info = page.split('id="side-info"')[1]
    assert 'id="paper-card"' in info and 'id="translation-card"' in info
    assert 'id="excerpt-drawer"' not in page and "按 E" not in page
    assert 'href="/papers?direction=sea-asean"' in page and 'name="direction"' in page and 'name="project"' in page
    assert 'id="paper-find"' in page
    alias = client.get(f"/papers/{pid}/read?page=245").text
    assert 'id="pdf-viewer"' in alias and 'data-initial-page="1"' in alias
    conn.close()


def test_bibliography_only_record_opens_the_same_page(client):
    from research_lab.db.connection import connect, now_iso
    conn = connect()
    ts = now_iso()
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, abstract, created_at, updated_at) "
                 "VALUES ('goh2013-struggle', 'The Struggle for Order', 'h-goh', '[\"Goh, Evelyn\"]', 2013, "
                 "'Hegemony, hierarchy and transition in East Asia.', ?, ?)", (ts, ts))
    page = client.get("/papers/goh2013-struggle").text
    assert "待补原文" in page and "添加 PDF" in page and 'action="/papers/goh2013-struggle/files"' in page and "Hegemony, hierarchy" in page
    assert 'id="pdf-viewer"' not in page and '补上 PDF 后生成概要' in page
    assert 'id="paper-card"' in page and 'href="/papers"' in page          # no direction: back to 全部
    assert 'id="excerpt-drawer"' not in page and 'id="paper-find"' not in page
    conn.close()


def test_page_links_match_prefixed_labels():
    from research_lab.web.routes.papers import _bare_label
    assert _bare_label("p. 381") == _bare_label("381") == _bare_label("p. [381]") == _bare_label("pp. 381") == "381"
    assert _bare_label("Cover Page") == "cover page" and _bare_label("xii") == "xii"


def test_reading_states_first_open_and_neighbours(client, sample_pdf, cjk_pdf):
    """Card 0034: 未读 · 在读 · 读完 in the reader; the first open of an unread paper sets 在读; 上一篇 / 下一篇
    walk the paper's shelf; the redirects stay inside the app."""
    from research_lab.db.connection import connect
    from research_lab.intake import pipeline
    conn = connect()
    with _failing() as http:
        en = pipeline.ingest(conn, sample_pdf, http=http, ai=False, direction="sea-asean").paper_id
        zh = pipeline.ingest(conn, cjk_pdf, http=http, ai=False, direction="sea-asean").paper_id
    assert pipeline.get_paper(conn, en)["read_depth"] is None
    # both imports can land in the same second: fix the shelf order (newest first: zh, then en)
    conn.execute("UPDATE papers SET created_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (en,))
    conn.execute("UPDATE papers SET created_at = '2026-01-02T00:00:00+00:00' WHERE id = ?", (zh,))
    first = client.get(f"/papers/{en}").text
    p = pipeline.get_paper(conn, en)
    assert p["read_depth"] == "skimmed" and p["last_opened_at"]
    assert 'name="state" value="reading" aria-current="true"' in first
    assert "第一次打开" not in client.get(f"/papers/{en}").text                    # only the first time
    r = client.post(f"/papers/{en}/status", data={"state": "done"}, follow_redirects=False)
    assert r.status_code in (204, 303) and pipeline.get_paper(conn, en)["read_depth"] == "read"
    client.post(f"/papers/{en}/status", data={"state": "unread"})
    assert pipeline.get_paper(conn, en)["read_depth"] is None
    conn.execute("UPDATE papers SET read_depth = 'reread' WHERE id = ?", (en,))
    client.post(f"/papers/{en}/status", data={"state": "done"})
    assert pipeline.get_paper(conn, en)["read_depth"] == "reread"                 # 读完 keeps 重读
    assert client.post(f"/papers/{en}/status", data={"state": "maybe"}).status_code == 400
    page_zh, page_en = client.get(f"/papers/{zh}").text, client.get(f"/papers/{en}").text
    assert f'href="/papers/{en}" rel="next"' in page_zh and f'href="/papers/{zh}" rel="prev"' in page_en
    r = client.post(f"/papers/{en}/archive", data={"direction": "sea-asean", "next": "/\\evil.example"}, follow_redirects=False)
    loc = r.headers.get("location") or r.headers.get("HX-Redirect")
    assert loc == "/papers"
    conn.close()


def test_library_page_has_no_explanatory_paragraph(client):
    """Card 0057 (Leo, 0.5.4): the page header is the title and its buttons; how to file a paper is a hover hint."""
    page = client.get("/papers").text
    header = page.split('<header class="page-header">', 1)[1].split("</header>", 1)[0]
    # card 0128: the one line under the title is counts, not an explanation
    desc = header.split('class="page-desc muted">', 1)[1].split("</p>", 1)[0]
    assert "篇有原文" in desc and "篇还没有概要" in desc and "把一行拖到左边" not in page
    assert 'class="tabs dir-filter"' in page and 'class="library-head rail-head"' not in page


def test_library_tabs_by_publication_type(client, cjk_pdf, sample_pdf):
    """Card 0057 (Leo, 10-02): 分栏 by publication type over the papers that have their original."""
    from research_lab.db.connection import connect
    from research_lab.intake import pipeline
    conn = connect()
    with _failing() as http:
        zh = pipeline.ingest(conn, cjk_pdf, http=http, ai=False).paper_id
        en = pipeline.ingest(conn, sample_pdf, http=http, ai=False).paper_id
    conn.execute("UPDATE papers SET kind = 'report' WHERE id = ?", (zh,))
    conn.execute("UPDATE papers SET kind = 'chapter' WHERE id = ?", (en,))
    page = client.get("/papers?view=all").text
    tabs = page.split('aria-label="出版物类型"', 1)[1].split("</div>", 1)[0]
    assert ">全部 <span class=\"count\">2</span>" in tabs and ">论文 <span class=\"count\">0</span>" in tabs
    assert ">书籍 <span class=\"count\">1</span>" in tabs and ">报告 <span class=\"count\">1</span>" in tabs
    books = client.get("/papers?kind=book").text
    assert "Who Convenes Asia?" in books and CJK_TITLE not in books           # a chapter files under 书籍
    reports = client.get("/papers?kind=report").text
    assert CJK_TITLE in reports and "Who Convenes Asia?" not in reports



def test_empty_type_tab_and_empty_missing_list_say_so(client, sample_pdf):
    """Card 0057 review: an empty 书籍 tab or an empty 待补原文 list says so instead of offering a first import."""
    from research_lab.db.connection import connect
    from research_lab.intake import pipeline
    conn = connect()
    with _failing() as http:
        pipeline.ingest(conn, sample_pdf, http=http, ai=False)
    books = client.get("/papers?kind=book").text
    assert "导入第一篇文献" not in books and "这一栏还没有书籍" in books
    missing = client.get("/papers?src=missing").text
    assert "导入第一篇文献" not in missing and "没有待补原文的文献" in missing
    assert "导入第一篇文献" not in client.get("/papers?src=missing&kind=report").text


def test_replace_and_remove_pdf_from_the_paper_page(client, sample_pdf, tmp_path):
    """Card 0175: the wrong PDF attached by hand is swapped or dropped from the 文件 section."""
    import pymupdf
    from research_lab.db.connection import connect
    from research_lab.intake import pipeline
    conn = connect()
    with _failing() as http:
        pid = pipeline.ingest(conn, sample_pdf, http=http, ai=False).paper_id
    page = client.get(f"/papers/{pid}").text
    assert f'action="/papers/{pid}/files/replace"' in page and "替换 PDF" in page and "移除" in page
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Right paper\n", fontsize=11)
    right = tmp_path / "right.pdf"
    doc.save(right)
    doc.close()
    r = client.post(f"/papers/{pid}/files/replace", files={"files": ("right.pdf", right.read_bytes(), "application/pdf")},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?note=pdf-replaced")
    f = pipeline.primary_file(conn, pid)
    assert f["original_name"] == "right.pdf" and "已换成新的 PDF" in client.get(f"/papers/{pid}?note=pdf-replaced").text
    r = client.post(f"/papers/{pid}/files/{f['id']}/remove", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?note=pdf-removed")
    assert pipeline.primary_file(conn, pid) is None and "还没有 PDF" in client.get(f"/papers/{pid}").text
