"""Card 0131 (docs/design/LIBRARY-UI.md §3.6, 卡 F): papers and issues linked both ways. The link stays the issue's own
`materials` (card 0099); the reader's 议题 row lists the issues that use a paper, with their keys, and links or
unlinks one through a picker, version-checked; the library row's 议题 mark says which issues use it."""
import json
from datetime import datetime

import pytest

from research_lab.files import issues as I

SLUG = "us-rok-reassurance"
JSON = {"Accept": "application/json", "HX-Request": "true"}


def _paper(conn, pid, title):
    now = datetime.now().isoformat()
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, venue, direction, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (pid, title, pid, '["Lee, Min"]', 2024, "Security Studies", "alliance-us", now, now))
    conn.commit()


@pytest.fixture
def lab(conn, repo):
    I.sync_keys(create=True)                       # numbered, as the library is since 0121
    _paper(conn, "P-1", "Reassurance after the Washington Declaration")
    _paper(conn, "P-2", "Alliance burden sharing")
    a = I.create("梳理延伸威慑文献", SLUG, "", I.revision())
    b = I.create("写引言", SLUG, "", I.revision())
    I.set_material(conn, a, "paper:P-1", True, I.revision(a))
    return dict(a=a, b=b)


def test_the_index_reads_the_issues_materials_backwards(conn, lab):
    (row,) = I.paper_issues("P-1")
    assert row["id"] == lab["a"] and row["key"] == I.key_of(lab["a"]) and row["label"] == f"{row['key']} · 梳理延伸威慑文献"
    assert I.paper_issues("P-2") == []
    I.set_material(conn, lab["b"], "paper:P-1", True, I.revision(lab["b"]))
    assert [r["id"] for r in I.paper_issues("P-1")] == sorted([lab["a"], lab["b"]], key=I.key_of)   # the cache follows the write
    assert all(x["revision"] == I.revision(x["id"]) for x in I.paper_issues("P-1", with_revisions=True))


def test_the_reader_lists_the_issues_with_keys_and_offers_link_and_unlink(client, lab):
    page = client.get("/papers/P-1").text
    row = page.split("<dt>议题</dt>", 1)[1].split("</dd>", 1)[0]
    key = I.key_of(lab["a"])
    assert f'<span class="issue-key">{key}</span>梳理延伸威慑文献' in row and f'href="/issues/{lab["a"]}"' in row
    assert f'name="revision" value="{I.revision(lab["a"])}"' in row and 'aria-label="Unlink: 梳理延伸威慑文献"' in row
    assert 'aria-label="Link issue"' in row and 'data-url="/papers/P-1/issues/search"' in row
    empty = client.get("/papers/P-2").text.split("<dt>议题</dt>", 1)[1].split("</dd>", 1)[0]
    assert ">Link issue</summary>" in empty and "data-paper-issue-form" in empty


def test_the_picker_finds_issues_by_key_title_or_project_and_marks_the_linked(client, lab):
    key_a, key_b = I.key_of(lab["a"]), I.key_of(lab["b"])
    hits = client.get("/papers/P-1/issues/search", params={"q": key_b.lower()}).text
    assert hits.count('class="issue-pick"') == 1 and f"{key_b}</span> 写引言" in hits
    assert f'value="{lab["b"]} {I.revision(lab["b"])} 1"' in hits
    linked = client.get("/papers/P-1/issues/search", params={"q": "延伸威慑"}).text
    assert f'value="{lab["a"]} {I.revision(lab["a"])} 0"' in linked and "，已关联" in linked
    assert 'aria-selected="true"' not in linked                              # aria-selected follows ↑↓ (card 0161); the label says linked
    everything = client.get("/papers/P-1/issues/search").text
    assert everything.index(key_a) < everything.index(key_b)                 # the linked one first
    assert "没有找到「不存在的议题」" in client.get("/papers/P-1/issues/search", params={"q": "不存在的议题"}).text
    assert client.get("/papers/NOPE/issues/search").status_code == 404


def test_a_pick_links_and_an_unlink_unlinks_with_the_issues_revision(client, conn, lab):
    b = lab["b"]
    r = client.post("/papers/P-2/issues", data={"pick": f"{b} {I.revision(b)} 1"}, headers=JSON)
    assert r.status_code == 200 and r.json()["linked"] is True and r.json()["revision"] == I.revision(b)
    assert "paper:P-2" in I._refs_list(b, "materials") and [x["id"] for x in I.paper_issues("P-2")] == [b]
    stale = client.post("/papers/P-2/issues", data={"ref": b, "revision": "old", "on": "0"}, headers=JSON)
    assert stale.status_code == 409 and "paper:P-2" in I._refs_list(b, "materials")       # a stale page writes nothing
    by_key = client.post("/papers/P-2/issues", data={"ref": I.key_of(b), "revision": I.revision(b), "on": "0"}, headers=JSON)
    assert by_key.status_code == 200 and "paper:P-2" not in I._refs_list(b, "materials")  # keys work as refs
    plain = client.post("/papers/P-1/issues", data={"ref": b, "revision": I.revision(b), "next": "/papers/P-1#summary"},
                        headers={"HX-Request": "true"}, follow_redirects=False)
    assert plain.status_code == 303 and plain.headers["location"] == "/papers/P-1#summary"
    away = client.post("/papers/P-1/issues", data={"ref": b, "revision": I.revision(b), "on": "0", "next": "https://example.org/"},
                       headers={"HX-Request": "true"}, follow_redirects=False)
    assert away.headers["location"] == "/papers/P-1"                                     # never off the app
    assert client.post("/papers/P-1/issues", data={"ref": "T-2099-0001", "revision": "x"}, headers=JSON).status_code == 400
    assert client.post("/papers/NOPE/issues", data={"ref": b, "revision": I.revision(b)}, headers=JSON).status_code == 404


def test_the_library_row_shows_which_issues_use_the_paper(client, lab):
    page = client.get("/papers?src=missing").text      # these test papers have no PDF, so they list under 待补原文
    line = page.split('data-paper-id="P-1"', 1)[1].split("</li>", 1)[0]
    tip = f"议题\n{I.key_of(lab['a'])} · 梳理延伸威慑文献"
    assert f'data-tip="{tip}"' in line and "#list-checks" in line
    other = page.split('data-paper-id="P-2"', 1)[1].split("</li>", 1)[0]
    assert "#list-checks" not in other and other.count('class="sig"') >= 1              # the slot stays, empty
