"""研究档案 as an issue list (docs/design/ARCHIVE-ISSUES.md §C, §H; card 0028): grouped by 方向 only, a
table row per output with 待办 x/y, the 截止 derived from its tasks and its latest 记录; filters 本周 ·
全部 · 已归档, 类别 and 课题; archived outputs only under 已归档; rows sort by latest record."""
from __future__ import annotations

import re

from research_lab.files import artifacts as A
from research_lab.files import tasks as T
from research_lab.files import updates as U
from research_lab.files.weeks import week_add, week_of

REF = "ART-2026-4001"
SLUG = "asean-centrality"


def group(html: str, slug: str) -> str:
    m = re.search(rf'<section class="arch-group"[^>]*>\s*<a class="card folder arch-head[^"]*" href="/artifacts/d/{re.escape(slug)}".*?</section>', html, re.S)
    assert m, f"no group {slug}"
    return m.group(0)


def test_rows_carry_progress_derived_due_and_latest_record(client, repo):
    a, _ = T.add({"title": "补一个反例", "project": SLUG, "source": REF, "due": "2099-03-01"})
    b, _ = T.add({"title": "核对名单", "project": SLUG, "source": REF})
    T.set_status(b, "done")
    U.add(REF, "user", "今天写完了研究版图的三派。")
    html = client.get("/artifacts?group=direction").text
    sea = group(html, "sea-asean")
    assert 'class="data arch-table"' in sea and f'href="/artifacts/{REF}"' in sea
    assert ">1/2<" in sea and ("3 月 1 日" in sea or "3月1日" in sea)
    assert "最近更新" in html and 'class="menu row-menu hover-actions"' in sea
    assert "<b>1</b><span>文献地图</span>" in sea                                       # the old folder counts live on in the head


def test_views_and_archived_rows(client, repo):
    page = client.get("/artifacts?group=direction").text
    assert f'href="/artifacts/{REF}"' in page
    a = A.load(next(p for p in A.list_paths() if A.load(p).ref == REF))
    client.post(f"/artifacts/{REF}/archive", data={"sha": a.sha})
    assert f'href="/artifacts/{REF}"' not in client.get("/artifacts?group=direction").text              # hidden from 全部
    arch = client.get("/artifacts?group=direction&view=archived").text
    assert f'href="/artifacts/{REF}"' in arch and "已被取代" in arch
    assert 'aria-current="page">已归档 <span class="count">1</span>' in arch


def test_week_kind_and_project_filters(client, repo):
    A.create("question", "this-week-q", SLUG, title="本周的问题")
    old = A.create("memo", "old-memo", "us-rok-reassurance", title="旧备忘", week=week_add(week_of(), -3))
    week = client.get("/artifacts?group=direction&view=week").text
    assert "本周的问题" in week and "旧备忘" not in week
    kinds = client.get("/artifacts?group=direction&kind=conclusion").text
    assert "旧备忘" in kinds and "本周的问题" not in kinds
    proj = client.get("/artifacts?group=direction&project=us-rok-reassurance").text
    assert "旧备忘" in proj and "本周的问题" not in proj
    assert "没有符合条件的产出" in client.get("/artifacts?group=direction&view=week&project=us-rok-reassurance").text


def test_rows_sort_by_latest_record(client, repo):
    A.create("question", "quiet-q", SLUG, title="安静的一份")
    U.add(REF, "user", "最新的一条记录")
    sea = group(client.get("/artifacts?group=direction").text, "sea-asean")
    assert sea.index(f'href="/artifacts/{REF}"') < sea.index("安静的一份")


def test_back_refuses_hosts(client, repo):
    a = A.load(next(p for p in A.list_paths() if A.load(p).ref == REF))
    for bad in ("//evil.example/x", "/\\evil.example/x", "https://evil.example/"):
        r = client.post(f"/artifacts/{REF}/rename", data={"sha": A.load(a.path).sha, "title": "x", "next": bad}, follow_redirects=False)
        loc = r.headers.get("location") or r.headers.get("hx-redirect") or ""
        assert loc.startswith(f"/artifacts/{REF}"), (bad, loc)


def test_default_list_groups_by_status_with_sub_issue_and_comment_counts(client, repo):
    """Leo's 10-01 redesign: the list reads like Linear's issues, grouped by status (初稿 / 成形 / 可入文),
    each row with its sub-issues x/y and its comment count; 按方向 is one click away."""
    a, _ = T.add({"title": "补一个反例", "project": SLUG, "source": REF})
    U.add(REF, "user", "和导师谈过")
    U.add(REF, "user", "收窄范围")
    html = client.get("/artifacts").text
    assert 'class="arch-group arch-status"' in html and ">按状态<" in html and ">按方向<" in html
    assert "<th class=\"num\">子任务</th>" in html and "<th class=\"num\">评论</th>" in html
    row = re.search(rf'<tr class="arch-row[^"]*">\s*<td class="c-title">\s*<a class="title serif" href="/artifacts/{REF}">.*?</tr>', html, re.S).group(0)
    assert ">0/1<" in row and ">2<" in row


def test_status_groups_keep_outputs_without_a_level(client, repo):
    """全部 N counts every unarchived output, so one with no level (or an unknown one) still shows, in its own
    「未标成熟度」 group, instead of falling out of the three status groups."""
    path = next(p for p in A.list_paths() if A.load(p).ref == REF)
    a = A.load(path)
    a.front.pop("level", None)
    A.save(a)
    html = client.get("/artifacts").text
    assert "未标成熟度" in html and f'href="/artifacts/{REF}"' in html.split("未标成熟度")[1]
    shown = len(re.findall(r'<tr class="arch-row', html))
    assert f">全部 <span class=\"count\">{shown}</span>" in html

