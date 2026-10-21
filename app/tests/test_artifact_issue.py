"""产出页 as an issue (docs/design/ARCHIVE-ISSUES.md §B, §H; card 0027): click-to-rename title, the 待办 block
of tasks hung under the output with x/y and a ghost row, the 记录 feed oldest-first with a composer, the
properties rail (derived 截止, 类别 posting back to the read tab), archive and restore from the 更多 menu."""
from __future__ import annotations

import re

from research_lab.files import artifacts as A
from research_lab.files import tasks as T
from research_lab.files import updates as U
from research_lab.files.weeks import week_of

REF = "ART-2026-4001"
SLUG = "asean-centrality"


def back(r) -> str:
    """Where a form post lands: 303 Location for a plain post, HX-Redirect for HTMX without a target."""
    assert r.status_code in (204, 303), r.status_code
    return r.headers.get("location") or r.headers.get("hx-redirect") or ""


def visible(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S))


def test_issue_page_blocks_and_properties(client, repo):
    page = client.get(f"/artifacts/{REF}").text
    assert 'class="issue"' in page and 'class="issue-props"' in page and 'id="issue-activity"' in page
    assert "data-rename" in page and 'action="/artifacts/' + REF + '/rename"' in page and "archive.js" in page
    assert "跟随子任务" in page                                                     # no sub-issues yet: derived 截止
    assert 'placeholder="把这份产出要做的事拆在这里，比如：补一个反例"' in page and 'name="source" value="' + REF + '"' in page
    assert "还没有评论" in page and 'action="/artifacts/' + REF + '/updates"' in page
    assert 'aria-label="添加子任务"' in page and 'name="reply" value="page"' in page  # sub-issues are added in place


def test_tasks_block_progress_and_derived_due(client, repo):
    a, _ = T.add({"title": "补一个反例", "project": SLUG, "source": REF, "due": "2099-03-01"})
    b, _ = T.add({"title": "核对 EAS 名单", "project": SLUG, "source": REF, "due": "2099-05-02"})
    T.set_status(b, "done")
    page = client.get(f"/artifacts/{REF}").text
    assert '<span class="count">1/2</span>' in page and "补一个反例" in page and "核对 EAS 名单" in page
    assert "3 月 1 日" in page or "3月1日" in page                                   # latest due among open tasks
    r = client.post(f"/tasks/{a}/status", data={"status": "done", "next": f"/artifacts/{REF}"}, follow_redirects=False)
    assert back(r) == f"/artifacts/{REF}"                                            # back to the output, not /tasks
    page = client.get(f"/artifacts/{REF}").text
    assert '<span class="count">2/2</span>' in page and "跟随子任务" in page
    assert "完成待办「补一个反例」" in page                                          # the system line in 记录


def test_ghost_row_adds_a_task_under_the_output(client, repo):
    r = client.post("/tasks", data={"title": "读 Goh 2013 第三章", "project": SLUG, "source": REF, "next": f"/artifacts/{REF}", "reply": "page"}, follow_redirects=False)
    assert back(r) == f"/artifacts/{REF}"
    [t] = T.for_source(REF)
    assert t["title"] == "读 Goh 2013 第三章" and t["due"] is None
    assert "读 Goh 2013 第三章" in client.get(f"/artifacts/{REF}").text


def test_updates_feed_oldest_first_and_composer(client, repo):
    for text in ("第一句", "第二句"):
        r = client.post(f"/artifacts/{REF}/updates", data={"text": text}, follow_redirects=False)
        assert back(r).startswith(f"/artifacts/{REF}")
    page = client.get(f"/artifacts/{REF}").text
    feed = page.split('class="activity" data-thread')[1].split("</ol>")[0]           # the issue page's thread (card 0097)
    assert feed.index("第一句") < feed.index("第二句")                                 # activity order, oldest at the top
    assert 'class="activity-comment"' in feed and "<strong>Zhanyuan Yao</strong>" in feed and "data-comment-edit=" in feed
    assert len(U.for_target(REF)) == 2


def test_type_select_posts_back_to_the_read_tab(client, repo):
    a = A.load(next(p for p in A.list_paths() if A.load(p).ref == REF))
    r = client.post(f"/artifacts/{REF}/meta", data={"sha": a.sha, "type": "puzzle", "next": f"/artifacts/{REF}"}, follow_redirects=False)
    assert back(r).startswith(f"/artifacts/{REF}?saved=")
    assert A.load(a.path).get("type") == "puzzle"
    assert "类别" in visible(client.get(back(r)).text)


def test_archive_and_restore_from_the_menu(client, repo):
    a = A.load(next(p for p in A.list_paths() if A.load(p).ref == REF))
    page = client.get(f"/artifacts/{REF}").text
    assert f'action="/artifacts/{REF}/archive"' in page and ">Archive<" in page
    client.post(f"/artifacts/{REF}/archive", data={"sha": a.sha})
    page = client.get(f"/artifacts/{REF}").text
    assert "这份产出已归档" in page and "Unarchive" in page and "已归档 · 已被取代" in page
    a = A.load(a.path)
    client.post(f"/artifacts/{REF}/archive", data={"sha": a.sha, "action": "restore"})
    assert "这份产出已归档" not in client.get(f"/artifacts/{REF}").text


def test_htmx_ghost_row_comes_back_to_the_page(client, repo):
    """An HTMX post that targets the block and asks for the page (reply=page) gets a redirect to `next`, not
    the chip the one-click sources get, so hx-select can pick #issue-activity out of it."""
    r = client.post("/tasks", data={"title": "HTMX 加的一条", "project": SLUG, "source": REF, "next": f"/artifacts/{REF}", "reply": "page"},
                    headers={"HX-Target": "issue-activity"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/artifacts/{REF}"
    chip = client.post("/tasks", data={"title": "一键来源", "project": SLUG, "source": "M3", "next": "/projects/" + SLUG}, headers={"HX-Target": "x"})
    assert chip.status_code == 200 and "已加入待办" in chip.text                   # the one-click path is unchanged


def _link(client, ref: str, other: str) -> None:
    a = A.load(next(p for p in A.list_paths() if A.load(p).ref == ref))
    client.post(f"/artifacts/{ref}/front", data={"sha": a.sha, "related_add": other})


def test_ref_lives_in_the_collapsed_info_panel(client, repo):
    """Card 0042 (P2-8): the ART- id is reachable for copying but never part of the visible rail."""
    page = client.get(f"/artifacts/{REF}").text
    info = page.split('<details class="props-info">')[1].split("</details>")[0]
    assert "<summary>信息</summary>" in info and f'value="{REF}"' in info and "readonly" in info
    outside = page.replace(info, "")
    assert REF not in visible(outside).replace(REF + "/", "")                     # only hrefs / actions carry it
    assert "编号 " + REF not in visible(page)


def test_related_rows_are_merged_and_marked(client, repo):
    """Card 0042 (P2-9): one row per related output; two-way links say 互相引用, backlink-only 引用了本篇."""
    both = A.load(A.create("memo", "two-way-memo", SLUG, title="双向的备忘")).ref
    back_only = A.load(A.create("memo", "back-only-memo", SLUG, title="只引用本篇的备忘")).ref
    client.get(f"/artifacts/{REF}")                                                 # index the new files
    _link(client, REF, both)
    _link(client, both, REF)
    _link(client, back_only, REF)
    page = client.get(f"/artifacts/{REF}").text
    rail = page.split('class="list prop-links"')[1].split("</ul>")[0]
    assert rail.count(f'data-related="{both}"') == 1 and rail.count(f'data-related="{back_only}"') == 1
    assert rail.count("<li") == 2
    row = lambda ref: rail.split(f'data-related="{ref}"')[1].split("</li>")[0]
    assert "互相引用" in row(both) and "引用了本篇" not in row(both)
    assert "引用了本篇" in row(back_only) and "互相引用" not in row(back_only)


def test_one_overflow_menu_holds_every_action(client, repo):
    """Card 0042 (P2-12): the header ⋯ is gone; the rail's 更多 has 课题的待办, 改名, 归档 and 回收站."""
    page = client.get(f"/artifacts/{REF}").text
    assert page.count('<details class="menu">') == 1
    header = page.split('<header class="page-header">')[1].split("</header>")[0]
    assert "details" not in header and "Project tasks" not in header
    menu = page.split('<details class="menu">')[1].split("</details>")[0]
    assert f'href="/projects/{SLUG}?tab=tasks"' in menu and "Project tasks" in menu
    assert "data-rename-open" in menu and f'action="/artifacts/{REF}/archive"' in menu and f'action="/artifacts/{REF}/delete"' in menu
    edit = client.get(f"/artifacts/{REF}?tab=edit").text                            # no rail there: the link stays reachable
    assert f'href="/projects/{SLUG}?tab=tasks"' in edit


def test_issue_page_order_and_description(client, repo):
    """Leo's 10-01 redesign: 描述 → 子任务 → 评论 on the first screen; the 描述 shows only what Leo wrote
    (template placeholders and hints left out, headings in Chinese); an output with only the assistant's
    notes shows those read-only in the 描述 and offers 开始写; editing happens in place."""
    from research_lab.db.connection import connect
    from research_lab.files import artifacts
    c = connect()
    artifacts.index(c)
    path = repo / c.execute("SELECT path FROM artifacts WHERE ref = ?", (REF,)).fetchone()[0]
    c.close()
    a = artifacts.load(path)
    a.body = ("\n## AI notes\n\n助手整理的三派解释。\n\n## What did I figure out that I didn't know last week?\n\n"
              "<!-- One sentence. -->\n\n...\n\n## Claim\n\n**I currently think:**\n\n> ...\n")
    artifacts.save(a)
    page = client.get(f"/artifacts/{REF}").text
    i_desc, i_sub, i_com = page.index('id="describe"'), page.index('id="tasks"'), page.index('id="updates"')
    assert i_desc < i_sub < i_com
    assert "你还没写自己的部分" in page and "助手整理的三派解释" in page and ">Start writing<" in page
    assert "One sentence" not in page.split('id="body-form"')[0]                   # hints stay out of the 描述
    a = artifacts.load(path)
    a.body = a.body.replace("**I currently think:**\n\n> ...", "**I currently think:**\n\n> 东盟中心性靠程序而不是实力维持。")
    artifacts.save(a)
    page = client.get(f"/artifacts/{REF}").text
    view = page.split('data-desc-view')[1].split('id="body-form"')[0]
    assert "<h2>主张</h2>" in view and "东盟中心性靠程序" in view and "想清楚了什么" not in view
    assert "助手笔记" in page and ">Edit<" in page                                    # notes fold away once Leo wrote


def test_describe_drops_placeholders_inside_written_sections():
    """A section Leo started keeps his lines and loses the template's leftovers next to them."""
    from research_lab.web.routes.artifacts import describe
    body = ("## Claim\n\n**I currently think:**\n\n> 东盟中心性靠程序维持。\n\n> ...\n\n"
            "## Evidence\n\n**Why I think so** — cases, works, data:\n\n1. 2019 年 AOIP 的起草过程\n2. ...\n")
    md = describe(body)["desc_md"]
    assert "东盟中心性靠程序维持" in md and "2019 年 AOIP" in md
    assert "> ..." not in md and "2. ..." not in md and "Why I think so" not in md and "I currently think" not in md
    assert md.startswith("## 主张") and "## 证据" in md


def test_describe_reads_a_toast_serialised_template_as_empty():
    """After one in-place save Toast UI rewrites the untouched template (`*` bullets, escaped dots, renumbered
    lists, dropped comments); none of it is Leo's, so the 描述 stays empty and no fake 来源 appears."""
    from pathlib import Path
    from research_lab.web.routes.artifacts import describe
    template = (Path(__file__).resolve().parents[2] / "templates" / "artifact.md").read_text(encoding="utf-8")
    body = template.split("\n---\n", 1)[1]
    assert describe(body)["desc_md"] == ""
    toast = (body.replace("- Author (Year). *Title*.", "* Author \\(Year\\). *Title*.")
                 .replace("\n...\n", "\n\\.\\.\\.\n").replace("1. ...\n2. ...", "1. ...\n1. ..."))
    toast = re.sub(r"<!--.*?-->\n*", "", toast, flags=re.S)
    assert "* Author" in toast and "\\.\\.\\." in toast
    assert describe(toast)["desc_md"] == ""
    zh = toast.replace("**I currently think:**", "**我目前认为：**").replace("* Author \\(Year\\). *Title*.", "* 作者（年份）。*标题*。")
    assert describe(zh)["desc_md"] == ""
    written = toast.replace("* Author \\(Year\\). *Title*.", "* Acharya (2001). *Constructing a Security Community*.")
    assert describe(written)["desc_md"] == "## 来源\n* Acharya (2001). *Constructing a Security Community*."



def test_describe_drops_the_template_lead_in_before_leos_words():
    """Leo's own claim starts with the template label and no space ("**I currently think:**IPE…"): the label
    goes (the heading says it), his words stay, and no stray asterisks reach the page."""
    from research_lab.web.routes.artifacts import describe
    md = describe("## Claim\n\n**I currently think:**IPE 视角下，东南亚国家处于什么位置？\n\n"
                  "## Evidence\n\n> **Why I think so** — cases, works, data: AOIP 起草记录\n")["desc_md"]
    assert "IPE 视角下，东南亚国家处于什么位置？" in md and "> AOIP 起草记录" in md
    assert "I currently think" not in md and "Why I think so" not in md and "**" not in md
    assert describe("## Evidence\n\n> **Why I think so** — cases, works, data:\n")["desc_md"] == ""
    assert describe("## Evidence\n\n**为什么这么认为**：两份档案\n")["desc_md"] == "## 证据\n两份档案"


def test_other_pages_name_the_sections_in_chinese():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "research_lab"
    for rel in ("weekly.py", "dashboard.py", "chat/cards.py", "web/templates/partials/_basket.html",
                "web/templates/partials/_draft_result.html", "web/templates/pages/artifact.html"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "“What did I figure out”" not in text and "“Claim”" not in text, rel
