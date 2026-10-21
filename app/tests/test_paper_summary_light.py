"""Light summaries (card 0122, Leo 10-11: 「导入后默认只读第一页，消耗 token 少些」): by default a summary reads the
metadata, the abstract and the first page with real text (cover pages skipped), about 1,500 tokens, and fills only
研究问题 and 核心论点; Full summary reruns it on the full rules; papers.summary_mode picks the default."""
from __future__ import annotations

import json

import httpx
import pytest

from research_lab.ai import queue, registry
from research_lab.ai.jobs import paper_summarize as PS
from research_lab.intake import pipeline

TS = "2026-10-11T07:00:00+00:00"
PROSE = ("Great powers keep attending ASEAN-led forums because none accepts a rival as convener, which leaves "
         "ASEAN as the host every side can live with. ")
JSTOR_FOOTER = "This content downloaded from 10.0.0.1 on Sat, 11 Oct 2026 07:00:00 UTC\nAll use subject to https://about.jstor.org/terms"
JSTOR_COVER = ("Who Convenes Asia?\nAuthor(s): Lin Tan\nSource: The Pacific Review, Vol. 36 (2023), pp. 244-270\n"
               "Published by: Taylor & Francis\nStable URL: https://www.jstor.org/stable/123456\nAccessed: 11-10-2026\n"
               "JSTOR is a not-for-profit service that helps scholars, researchers, and students discover, use, and build "
               "upon a wide range of content in a trusted digital archive. Your use of the JSTOR archive indicates your "
               "acceptance of the Terms & Conditions of Use.\n" + JSTOR_FOOTER)
REAL_FIRST = "Who Convenes Asia?\nLin Tan\n" + "\n".join([PROSE * 3] * 6) + "\n© 2023 Taylor & Francis. All rights reserved.\n" + JSTOR_FOOTER
SECOND = "\n".join([("The second page reports attendance coding across three forums from 2005 to 2022. " * 3)] * 8)


def add_paper(conn, pid, pages, *, abstract=None, kind="article", title="Who Convenes Asia?"):
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, venue, kind, abstract, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)", (pid, title, pid, json.dumps(["Tan, Lin"]), 2023, "The Pacific Review",
                                                  kind, abstract, TS, TS))
    fid = conn.execute("INSERT INTO paper_files(paper_id, sha256, rel_path, bytes, page_count, has_text_layer, extractor, "
                       "is_primary, created_at) VALUES (?, ?, 'x.pdf', 10, ?, 1, 'pymupdf', 1, ?)",
                       (pid, pid, len(pages), TS)).lastrowid
    for i, text in enumerate(pages):
        conn.execute("INSERT INTO paper_pages(file_id, page_index, page_label, text) VALUES (?, ?, ?, ?)",
                     (fid, i, str(244 + i), text))
    return pid


def light(conn, pid):
    return registry.get("paper.summarize").material(conn, pid, "light")


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    from research_lab.ai import dispatch
    monkeypatch.setattr(dispatch, "dispatch", lambda job_id: "queued")


# ---------------------------------------------------------------- the light material

def test_light_skips_a_jstor_cover_and_reads_metadata_abstract_and_the_first_page(conn):
    pid = add_paper(conn, "tan2023", [JSTOR_COVER, REAL_FIRST, SECOND], abstract="Great powers keep attending.")
    mode, text = light(conn, pid)
    assert mode.startswith("light")
    assert "Title: Who Convenes Asia?" in text and "Authors: Tan, Lin" in text and "Year: 2023" in text
    assert "Journal: The Pacific Review" in text and "## Abstract\nGreat powers keep attending." in text
    assert "=== p. 245 ===" in text and "=== p. 244 ===" not in text and "=== p. 246 ===" not in text
    assert "JSTOR is a not-for-profit" not in text and "This content downloaded" not in text      # footers go too
    assert "second page" not in text


@pytest.mark.parametrize("front", [
    ["Who Convenes Asia?\nLin Tan"],                                                        # a title page alone
    ["Copyright © 2023 Lin Tan\nAll rights reserved.\nISBN 978-0-00-000000-0\nLibrary of Congress Cataloging-in-Publication Data"],
    ["Contents\n1 Introduction ........ 1\n2 Attendance ........ 12\n3 Veto ........ 30\n4 Hosts ........ 48\n5 Conclusion ........ 70"],
    ["To cite this article: Lin Tan (2023) Who Convenes Asia?\nFull Terms & Conditions of access and use can be found at\n"
     "https://www.tandfonline.com/action/journalInformation"],
])
def test_light_skips_pages_without_real_text(conn, front):
    pid = add_paper(conn, "front", [*front, REAL_FIRST])
    _, text = light(conn, pid)
    assert f"=== p. {244 + len(front)} ===" in text and "=== p. 244 ===" not in text


def test_a_first_page_with_a_copyright_line_still_counts(conn):
    pid = add_paper(conn, "plain", [REAL_FIRST, SECOND])
    assert "=== p. 244 ===" in light(conn, pid)[1]


@pytest.mark.parametrize("pages,abstract", [
    ([REAL_FIRST * 6], "Great powers keep attending. " * 400),                              # long English
    (["\n".join(["东盟主导的地区论坛之所以能够持续吸引大国出席，是因为任何一方都不愿让对手充当召集者。" * 4] * 40)],
     "本文讨论东盟中心地位的来源。" * 300),                                                      # long Chinese
])
def test_the_light_material_stays_near_1500_tokens(conn, pages, abstract):
    pid = add_paper(conn, "long", pages, abstract=abstract)
    _, text = light(conn, pid)
    assert PS.light_tokens(text) <= PS.LIGHT_TOKENS + 60
    assert "## Abstract" in text and "=== p. 244 ===" in text


def test_full_is_the_old_rule(conn):
    pid = add_paper(conn, "full", [JSTOR_COVER, REAL_FIRST, SECOND])
    mode, text = registry.get("paper.summarize").material(conn, pid)
    assert mode == "full text minus references" and "=== p. 246 ===" in text


# ---------------------------------------------------------------- the job

def light_reply() -> dict:
    """What a model might send back for a light job: the two fields, and some it was not asked for."""
    return {"research_question": {"text": "大国为何持续出席东盟主导的论坛？", "pages": ["245"]},
            "argument": {"text": "任何一方都不接受对手当召集者，东盟成了都能接受的主办方。", "pages": ["245"]},
            "mechanism": {"text": "相互否决", "pages": []}, "key_terms": ["召集能力"], "mode": "full",
            "direction": {"slug": "sea-asean", "why": "东盟"}}


def test_the_default_job_is_light_and_fills_only_two_fields(conn):
    pid = add_paper(conn, "tan2023", [JSTOR_COVER, REAL_FIRST, SECOND])
    job_id = pipeline.enqueue_summary(conn, pid)
    req = json.loads(queue.get(conn, job_id)["request_json"])
    assert req["params"]["summary_mode"] == "light" and req["max_tokens"] == 800
    user = req["messages"][1]["content"]
    assert "only research_question and argument" in user and "=== p. 245 ===" in user
    assert "Known camps" not in user and "Research directions" not in user
    queue.complete(conn, job_id, light_reply(), provider="claude-code", model="test")
    data = pipeline.summary_of(pipeline.get_paper(conn, pid))
    assert data["mode"] == "light"
    assert data["research_question"]["text"] and data["argument"]["text"]
    assert data["mechanism"] == {"text": None, "pages": []} and data["key_terms"] == [] and data["direction"]["slug"] is None
    assert data["relevance_to_project"]["text"] is None


def test_the_setting_full_brings_back_the_full_job(conn):
    from research_lab import tunables as TU
    TU.set_value(conn, "papers.summary_mode", "full")
    pid = add_paper(conn, "tan2023", [JSTOR_COVER, REAL_FIRST, SECOND])
    req = json.loads(queue.get(conn, pipeline.enqueue_summary(conn, pid))["request_json"])
    assert req["params"]["summary_mode"] == "full" and req["params"]["mode"] == "full text minus references"
    assert "=== p. 246 ===" in req["messages"][1]["content"]


def test_a_full_reply_is_stored_as_full_even_if_it_says_light(conn):
    pid = add_paper(conn, "tan2023", [REAL_FIRST, SECOND])
    job_id = pipeline.enqueue_summary(conn, pid, mode="full")
    queue.complete(conn, job_id, {**light_reply(), "mode": "light"}, provider="claude-code", model="test")
    data = pipeline.summary_of(pipeline.get_paper(conn, pid))
    assert data["mode"] == "full" and data["mechanism"]["text"] == "相互否决"


def test_a_rerun_queued_before_light_reading_keeps_its_full_request(conn):
    pid = add_paper(conn, "rerun", [JSTOR_COVER, REAL_FIRST, SECOND])
    conn.execute("UPDATE papers SET ai_summary_json = ? WHERE id = ?", (json.dumps({"argument": {"text": "旧"}}), pid))
    with PS.forced_mode("full"):
        job_id = queue.enqueue(conn, "paper.summarize", pid)
    req = json.loads(queue.get(conn, job_id)["request_json"])
    del req["params"]["summary_mode"]                                   # Leo's 重新生成, queued before card 0122
    conn.execute("UPDATE ai_jobs SET request_json = ? WHERE id = ?", (json.dumps(req), job_id))
    pipeline.enqueue_summary(conn, add_paper(conn, "new", [REAL_FIRST]))
    kept = json.loads(queue.get(conn, job_id)["request_json"])
    assert kept["params"]["summary_mode"] == "full" and kept["messages"] == req["messages"]


def test_waiting_jobs_built_before_light_reading_follow_the_setting(conn):
    old = add_paper(conn, "old", [JSTOR_COVER, REAL_FIRST, SECOND])
    job_id = queue.enqueue(conn, "paper.summarize", old)
    req = json.loads(queue.get(conn, job_id)["request_json"])
    del req["params"]["summary_mode"]                                   # as a job queued before card 0122
    conn.execute("UPDATE ai_jobs SET request_json = ? WHERE id = ?", (json.dumps(req), job_id))
    pipeline.enqueue_summary(conn, add_paper(conn, "new", [REAL_FIRST]))
    assert json.loads(queue.get(conn, job_id)["request_json"])["params"]["summary_mode"] == "light"


def test_manual_import_queues_a_light_summary(conn, sample_pdf):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as http:
        r = pipeline.ingest(conn, sample_pdf, http=http)
    assert json.loads(queue.get(conn, r.job_id)["request_json"])["params"]["summary_mode"] == "light"


# ---------------------------------------------------------------- the reading page

def test_a_light_summary_says_so_and_offers_the_full_reading(conn, client):
    pid = add_paper(conn, "tan2023", [JSTOR_COVER, REAL_FIRST, SECOND])
    queue.complete(conn, pipeline.enqueue_summary(conn, pid), light_reply(), provider="claude-code", model="test")
    card = client.get(f"/papers/{pid}/summary").text
    assert "只读了标题、摘要和正文首页" in card and ">Full summary</button>" in card and 'title="读全文，生成完整概要"' in card
    assert 'hx-vals=\'{"mode": "full"}\'' in card
    assert "展开更多" not in card
    r = client.post(f"/papers/{pid}/summarize", data={"mode": "full"})
    assert r.status_code == 200 and "概要已排队" in r.text
    job = pipeline.latest_summary_job(conn, pid)
    assert json.loads(job["request_json"])["params"]["summary_mode"] == "full"
    queue.complete(conn, job["id"], {**light_reply(), "relevance_to_project": {"text": "与项目直接相关"}},
                   provider="claude-code", model="test")
    card = client.get(f"/papers/{pid}/summary").text
    assert "只读了标题" not in card and "Full summary" not in card and "展开更多" in card
    client.post(f"/papers/{pid}/summarize")                             # 重新生成 keeps a full summary full (0126)
    assert json.loads(pipeline.latest_summary_job(conn, pid)["request_json"])["params"]["summary_mode"] == "full"


def test_an_old_summary_without_a_mode_reads_as_full(conn, client):
    pid = add_paper(conn, "tan2023", [REAL_FIRST])
    conn.execute("UPDATE papers SET ai_summary_json = ? WHERE id = ?",
                 (json.dumps({"research_question": {"text": "旧问题"}, "argument": {"text": "旧论点"}}), pid))
    card = client.get(f"/papers/{pid}/summary").text
    assert "旧论点" in card and "只读了首页" not in card and "Full summary" not in card
    client.post(f"/papers/{pid}/summarize")                             # read in full before 0122: stays full
    assert json.loads(pipeline.latest_summary_job(conn, pid)["request_json"])["params"]["summary_mode"] == "full"


def test_rerunning_a_light_summary_follows_the_setting(conn, client):
    from research_lab import tunables as TU
    pid = add_paper(conn, "tan2023", [JSTOR_COVER, REAL_FIRST, SECOND])
    queue.complete(conn, pipeline.enqueue_summary(conn, pid), light_reply(), provider="claude-code", model="test")
    client.post(f"/papers/{pid}/summarize")
    assert json.loads(pipeline.latest_summary_job(conn, pid)["request_json"])["params"]["summary_mode"] == "light"
    TU.set_value(conn, "papers.summary_mode", "full")
    client.post(f"/papers/{pid}/summarize")
    assert json.loads(pipeline.latest_summary_job(conn, pid)["request_json"])["params"]["summary_mode"] == "full"


# ---------------------------------------------------------------- the setting

def test_the_setting_is_on_the_whitelist():
    from research_lab import tunables as TU
    t = TU.entry("papers.summary_mode")
    assert t.name == "文献概要读多少" and TU.default("papers.summary_mode") == "light"
    assert [c for c, _ in t.choices] == ["light", "full"] and TU.validate("papers.summary_mode", "full") == "full"
    with pytest.raises(TU.TunableError):
        TU.validate("papers.summary_mode", "half")


@pytest.mark.parametrize("text", ["概要读全文", "以后文献概要都读全文", "概要只读首页就行"])
def test_telling_theo_how_much_to_read_changes_the_setting(conn, text):
    from research_lab import tunables as TU
    from research_lab.agent import skills, tasks, tools
    assert skills.pick(text).name == "settings"
    new = "light" if "首页" in text else "full"
    if new == "light":
        TU.set_value(conn, "papers.summary_mode", "full")
    task_id = tasks.create(conn, text, skill="settings")
    out, err = tools.call(conn, "change_settings", {"changes": [{"key": "papers.summary_mode", "new": new}]}, 5000,
                          task_id=task_id)
    assert not err, out
    assert TU.tunable(conn, "papers.summary_mode") == new


@pytest.mark.parametrize("text", ["这篇概要读全文重新生成", "帮我生成这篇论文的概要"])
def test_one_papers_summary_is_not_the_setting(text):
    from research_lab.agent import skills
    assert not skills.is_settings_request(text)
