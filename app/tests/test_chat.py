"""Chat layer: threads, the deterministic router, the chat.reply job and the composer endpoint."""
from __future__ import annotations

import json
import re

import httpx
import pytest

DOI = "10.1234/rl.2023.001"
TITLE = "Who Convenes Asia? Forum attendance and the durability of ASEAN-led institutions"
QUESTION = "What explains the persistence of ASEAN institutional centrality"


def crossref_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.crossref.org" and request.url.path == f"/works/{DOI}":
            return httpx.Response(200, json={"status": "ok", "message": {
                "DOI": DOI, "title": [TITLE], "author": [{"family": "Tan", "given": "Lin"}],
                "issued": {"date-parts": [[2023]]}, "container-title": ["The Pacific Review"], "type": "journal-article"}})
        return httpx.Response(404, json={"status": "error"})
    return httpx.MockTransport(handler)


@pytest.fixture()
def db(client):
    from research_lab.db.connection import connect
    c = connect()
    yield c
    c.close()


def send(client, text: str = "", page: str = "/chat", thread_id: str = "", files=None):
    data = {"text": text, "page": page, "thread_id": thread_id}
    return client.post("/chat/send", data=data, files=files, follow_redirects=False)


def thread_of(resp) -> str:
    tid = resp.headers.get("X-Chat-Thread") or resp.headers["HX-Redirect"].rsplit("/", 1)[-1]
    assert re.fullmatch(r"T-\d{4}-\d{4}", tid)
    return tid


def msgs(db, tid):
    from research_lab.chat import service
    return service.messages(db, tid)


# ---------------------------------------------------------------- pages and guard

def test_chat_page_renders_with_composer(client):
    r = client.get("/chat")
    assert r.status_code == 200
    assert "新对话" in r.text and "提问、粘贴 DOI，或拖入 PDF" in r.text
    assert r.text.count("data-composer") - r.text.count("data-composer-here") == 1   # the global copy is suppressed
    assert "/static/chat.js" in r.text and "/static/chat.css" in r.text


def test_composer_partial_renders_inside_a_page(client):
    from research_lab.web.context import templates
    html = templates().env.get_template("partials/_composer.html").render({"request": None})
    assert 'action="/chat/send"' in html and 'name="files"' in html and "输入 / 使用命令" in html
    page = client.get("/papers")
    assert page.status_code == 200
    if "_composer.html" in (templates().env.loader.get_source(templates().env, "base.html")[0]):
        assert "data-composer" in page.text and 'value="/papers"' in page.text


def test_post_without_hx_request_is_forbidden(client):
    r = client.post("/chat/send", data={"text": "/help"}, headers={"HX-Request": "false"})
    assert r.status_code == 403


def test_empty_message_is_rejected(client):
    r = send(client, "  ")
    assert r.status_code == 400 and "请输入内容" in r.text


# ---------------------------------------------------------------- deterministic commands

def test_hunt_command_creates_thread_and_hunt_card(client, db):
    from research_lab.method.seed import seed
    seed(db, "asean-centrality")
    r = send(client, "/hunt")
    assert r.status_code == 204 and r.headers["HX-Redirect"].startswith("/chat/T-")
    tid = thread_of(r)
    ms = msgs(db, tid)
    assert [m.role for m in ms] == ["user", "assistant"]
    assert ms[1].card_kind == "hunt" and ms[1].card_ref == "asean-centrality"
    from research_lab.chat import service
    assert service.get_thread(db, tid)["title"] == "证据线索 · ASEAN Centrality"
    page = client.get(f"/chat/{tid}")
    assert page.status_code == 200
    assert "待查清单" in page.text and "覆盖率" in page.text and "/projects/asean-centrality/matrix" in page.text
    assert tid in client.get("/chat").text


def test_follow_up_in_same_thread_returns_fragment(client, db):
    tid = thread_of(send(client, "/help"))
    r = send(client, "/now", page=f"/chat/{tid}", thread_id=tid)
    assert r.status_code == 200 and r.headers["X-Chat-Thread"] == tid
    assert 'class="chat-msg chat-msg-user' in r.text and QUESTION in r.text
    assert len(msgs(db, tid)) == 4


def test_non_chat_page_redirects_to_thread(client, db):
    r = send(client, "/week", page="/")
    assert r.status_code == 204 and r.headers["HX-Redirect"].startswith("/chat/T-")
    tid = thread_of(r)
    page = client.get(f"/chat/{tid}")
    assert "条划线待整理" in page.text and "回到首页" in page.text
    # a second message from another page continues the recent thread
    r2 = send(client, "/status", page="/papers")
    assert thread_of(r2) == tid


def test_plain_form_post_redirects(client, db):
    r = client.post("/chat/send", data={"text": "/help", "page": "/"}, follow_redirects=False,
                    headers={"HX-Request": "false", "Origin": "http://127.0.0.1:4100"})
    assert r.status_code == 303 and r.headers["location"].startswith("/chat/T-")


def test_seed_command_creates_artifact(client, db, repo):
    from research_lab.files.weeks import week_of
    tid = thread_of(send(client, "/seed puzzle test-slug"))
    path = repo / "_artifacts" / f"{week_of()}-test-slug.md"
    assert path.exists()
    ms = msgs(db, tid)
    assert ms[1].card_kind == "artifacts" and ms[1].card_ref.startswith("ART-")
    assert "test slug" in client.get(f"/chat/{tid}").text.lower()
    again = msgs(db, thread_of(send(client, "/seed puzzle test-slug")))
    assert "已存在" in again[1].content


def test_search_milestones_papers_and_help_cards(client, db):
    tid = thread_of(send(client, "find ASEAN centrality"))
    m = msgs(db, tid)[1]
    assert m.card_kind == "search" and m.card_ref == "ASEAN centrality"
    assert "查看全部结果" in client.get(f"/chat/{tid}").text
    tid = thread_of(send(client, "/milestones"))
    assert msgs(db, tid)[1].card_kind == "milestones"
    page = client.get(f"/chat/{tid}").text
    assert "摸清研究版图" in page and "可入稿成果包" in page
    tid = thread_of(send(client, "/nonsense"))
    assert "不认识的命令" in msgs(db, tid)[1].content and "/hunt" in msgs(db, tid)[1].content


def test_doi_is_answered_without_fabricating(client, db):
    tid = thread_of(send(client, "https://doi.org/10.5555/abc.123"))
    m = msgs(db, tid)[1]
    assert "需要 PDF" in m.content and m.card_kind is None
    assert db.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 0


def test_match_natural_forms():
    from research_lab.chat import router
    assert router.match("How is this week going?")[0] is router.cmd_week
    assert router.match("create a seed puzzle chair-rule")[0] is router.cmd_seed
    assert router.match("Search for chair rule") == (router.cmd_search, "for chair rule")
    assert router.match("/HUNT asean")[0] is router.cmd_hunt
    assert router.match("10.1234/rl.2023.001")[0] is router.cmd_doi
    assert router.match("Why does the chair rule persist?") is None


# ---------------------------------------------------------------- free text → chat.reply

def test_free_text_queues_reply_job_and_completion_fills_message(client, db):
    from research_lab.ai import queue
    r = send(client, "Which rival explanation is weakest so far?")
    tid = thread_of(r)
    ms = msgs(db, tid)
    assert [m.role for m in ms] == ["user", "assistant"]
    ph = ms[1]
    assert ph.card_kind == "job_wait" and ph.job_id and ph.card_ref == ph.job_id and ph.content == ""
    job = queue.get(db, ph.job_id)
    assert job["kind"] == "chat.reply" and job["status"] == "queued" and job["target_type"] == "message"
    assert job["target_id"] == str(ph.id)
    req = json.loads(job["request_json"])
    user = req["messages"][1]["content"]
    assert QUESTION in user and "Which rival explanation is weakest" in user and "/hunt" in user
    assert "Never invent sources, quotes or case facts" in req["messages"][0]["content"]
    from research_lab.chat import service
    assert service.get_thread(db, tid)["title"] == "Which rival explanation is weakest so far?"

    frag = client.get(f"/chat/message/{ph.id}").text
    assert "已交给助手" in frag and "rl agent claim" not in frag and 'hx-trigger="every' in frag

    queue.complete(db, ph.job_id, {"reply": "The context holds no evidence on rivals yet. Run `/hunt`.",
                                   "card": {"kind": "hunt", "ref": "asean-centrality"},
                                   "suggested_commands": ["/hunt", "not a command", "/week", "/status", "/papers"]},
                   provider="test", model="test")
    filled = service.get_message(db, ph.id)
    assert filled.content.startswith("The context holds no evidence") and filled.card_kind == "hunt"
    frag = client.get(f"/chat/message/{ph.id}").text
    assert 'hx-trigger="every' not in frag and "待查清单" in frag
    assert frag.count("data-chat-command") == 3


def test_reply_result_guardrails():
    from pydantic import ValidationError
    from research_lab.ai.jobs.chat_reply import ChatReplyResult
    with pytest.raises(ValidationError):
        ChatReplyResult.model_validate({"reply": "x", "quote": "invented"})
    with pytest.raises(ValidationError):
        ChatReplyResult.model_validate({"reply": "x", "card": {"kind": "job_wait", "ref": "J-1"}})
    long = ChatReplyResult.model_validate({"reply": "word " * 400})
    assert len(long.reply.split()) <= 251


def test_reply_with_unknown_card_ref_is_dropped(client, db):
    from research_lab.ai import queue
    from research_lab.chat import service
    ph = msgs(db, thread_of(send(client, "Tell me about Tan 2023")))[1]
    queue.complete(db, ph.job_id, {"reply": "Not in the library.", "card": {"kind": "paper_summary", "ref": "nobody2020-x"}},
                   provider="test", model="test")
    m = service.get_message(db, ph.id)
    assert m.card_kind is None and m.content == "Not in the library."


# ---------------------------------------------------------------- files

def test_pdf_upload_ingests_paper(client, db, sample_pdf):
    with httpx.Client(transport=crossref_transport()) as http:
        client.app.state.intake_http = http
        with open(sample_pdf, "rb") as fh:
            r = send(client, "", files=[("files", ("tan2023.pdf", fh, "application/pdf"))])
        client.app.state.intake_http = None
    tid = thread_of(r)
    ms = msgs(db, tid)
    assert ms[0].content == "附件：tan2023.pdf"
    assert ms[1].card_kind in ("paper_summary", "job_wait")
    paper_id = ms[1].card_ref if ms[1].card_kind == "paper_summary" else None
    assert paper_id and db.execute("SELECT title FROM papers WHERE id = ?", (paper_id,)).fetchone()["title"] == TITLE
    assert "已导入" in ms[1].content and f"](/papers/{paper_id})" in ms[1].content
    assert f"`{paper_id}`" not in ms[1].content and "引用信息已核验" in ms[1].content
    page = client.get(f"/chat/{tid}").text
    assert f"/papers/{paper_id}/read" in page and "summary-card" in page

    from research_lab.chat import service
    assert service.latest_paper_in_thread(db, tid) == paper_id
    r2 = send(client, "/excerpt", page=f"/chat/{tid}", thread_id=tid)
    assert f"/papers/{paper_id}/read" in r2.text

    with open(sample_pdf, "rb") as fh:
        dup = send(client, "", page=f"/chat/{tid}", thread_id=tid, files=[("files", ("again.pdf", fh, "application/pdf"))])
    assert "已在文献库里" in dup.text and f"`{paper_id}`" not in dup.text

    listing = msgs(db, thread_of(send(client, "/papers", page="/papers")))[-1].content
    assert TITLE in listing and f"/papers/{paper_id}" in listing and f"`{paper_id}`" not in listing
    assert "引用信息已核验" in listing and "verified" not in listing


def test_rejected_file_is_reported(client, db, tmp_path):
    bogus = tmp_path / "x.pdf"
    bogus.write_text("hello")
    with open(bogus, "rb") as fh:
        tid = thread_of(send(client, "", files=[("files", ("x.pdf", fh, "application/pdf"))]))
    assert "无法导入 **x.pdf**" in msgs(db, tid)[1].content


# ---------------------------------------------------------------- service

def test_title_inference_and_thread_order(db):
    from research_lab.chat import service
    assert service.infer_title("/hunt", "Hunt list · x") == "Hunt list · x"
    long = service.infer_title("a" * 100)
    assert len(long) == 60 and long.endswith("…")
    t1 = service.create_thread(db, "one")
    t2 = service.create_thread(db, "two")
    service.add_message(db, t1, "user", "hello")
    assert [t["id"] for t in service.list_threads(db)][0] == t1
    assert t1 != t2 and t2.endswith("0002")
    with pytest.raises(ValueError):
        service.add_message(db, t1, "robot", "x")


# ---------------------------------------------------------------- reply copy

def visible(text: str) -> str:
    """Message text with markdown link targets removed, so ids that live only in an href do not count."""
    return re.sub(r"\]\([^)]*\)", "]", text)


def test_command_replies_are_chinese_and_hide_ids(client, db):
    from research_lab.chat import router
    from research_lab.method.seed import seed
    seed(db, "asean-centrality")

    def run(text: str):
        fn, arg = router.match(text)
        return fn(db, arg, {"thread_id": None, "page": ""})

    status = run("/status").content
    assert "今天该做的事" in status and "概览" in status and "(/agent)" not in status
    assert "This morning" not in status and "ART-" not in visible(status)

    now = run("/now").content
    assert "研究问题" in now and "课题：** ASEAN Centrality" in now and "(/now)" not in now and "asean-centrality" not in now

    unknown = run("/hunt nope").content
    assert "找不到课题" in unknown and "ASEAN Centrality" in unknown

    assert "某条划线" in run("/associate").content and "找不到这条划线" in run("/associate EXC-2099-0001").content
    assert "还没有文献" in run("/excerpt").content and "文献库还是空的" in run("/papers").content

    seeded = run("/seed puzzle copy-check")
    assert seeded.card_kind == "artifacts" and "已新建一份初稿" in seeded.content and "类型为问题" in seeded.content
    body = visible(seeded.content)
    assert "puzzle" not in body and "_artifacts" not in body and "ART-" not in body
    assert "可选类型" in run("/seed").content


def test_chinese_phrasings_route_to_commands():
    from research_lab.chat.router import cmd_milestones, cmd_related, cmd_search, cmd_week, match

    assert match("这周进展如何？")[0] is cmd_week
    assert match("搜索 主席规则") == (cmd_search, "主席规则")
    assert match("和出席率下降相关的划线") == (cmd_related, "出席率下降")
    assert match("课题进度")[0] is cmd_milestones
    assert match("中国对东盟的政策怎么看") is None


def test_message_from_an_issue_page_carries_the_page_invisibly(client, db):
    from research_lab.files import issues
    ref = issues.create("第二章", "asean-centrality", "", issues.revision(""))
    tid = thread_of(send(client, "本周进度", page=f"/issues/{ref}"))
    user, reply = msgs(db, tid)
    assert (user.card_kind, user.card_ref) == ("page", f"/issues/{ref}") and "进度" in reply.content
    assert reply.card_kind == "progress"
    page = client.get(f"/chat/{tid}").text
    assert 'data-card-kind="page"' not in page
    other = [m for m in msgs(db, thread_of(send(client, "本周进度", page="/papers"))) if m.role == "user"][-1]
    assert (other.card_kind, other.card_ref) == ("page", "/papers")    # a non-issue page: 这条 is not an issue
    assert "/议题" in send(client, "/help", thread_id=tid).text


def test_old_chat_reply_history_leaves_out_the_hidden_page_cards(db):
    from research_lab.ai.jobs.chat_reply import _history_block
    from research_lab.chat import service
    tid = service.create_thread(db, "x")
    service.add_message(db, tid, "user", "这条怎么样", card_kind="page", card_ref="/issues/T-2026-0001")
    service.add_message(db, tid, "assistant", "见卡片", card_kind="hunt", card_ref="asean-centrality")
    last = service.add_message(db, tid, "assistant", "")
    lines, question = _history_block(db, db.execute("SELECT * FROM chat_messages WHERE id = ?", (last.id,)).fetchone())
    assert question == "这条怎么样" and "page" not in "\n".join(lines) and "T-2026-0001" not in "\n".join(lines)
    assert "[card hunt: asean-centrality]" in "\n".join(lines)
