"""Attachments and linked papers / datasets under an issue's description (card 0099, ISSUES-LINEAR §2.9): uploads go to
the 课题's private/ folder with a cleaned name and an allowed type, can move to drafts/ and be detached (the file
stays); papers and datasets of the library are linked with a search picker; the 课题 page counts them as 间接关联."""
from datetime import datetime

import pytest

from research_lab.config import get_paths
from research_lab.files import deliverables, issues as I

SLUG = "us-rok-reassurance"


@pytest.fixture(autouse=True)
def no_open(monkeypatch):
    monkeypatch.setenv("RL_OPEN_FILES", "0")


def _issue():
    return I.create("材料验收", SLUG, "", I.revision())


def _meta(ref, key):
    return list(I._entry(I._metadata(), ref).get(key) or [])


def _paper(conn, pid, title, year=2024, venue="Security Studies"):
    now = datetime.now().isoformat()
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, venue, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                 (pid, title, pid, '["Lee, Min"]', year, venue, now, now))


def _dataset(conn, did, name):
    conn.execute("INSERT INTO datasets(id, name, source_kind, original_name, rows, cols, created_at) VALUES (?,?,?,?,?,?,?)",
                 (did, name, "csv", name + ".csv", 48, 12, datetime.now().isoformat()))


def _post(client, url, files=None, **data):
    return client.post(url, data=data, files=files, headers={"HX-Request": "true"}, follow_redirects=False)


# ------------------------------------------------------------------------------------------------ attachments

def test_an_upload_goes_to_the_private_folder_and_onto_the_issue(client, repo):
    ref = _issue()
    r = _post(client, f"/issues/{ref}/attachments", files={"file": ("导师批注.pdf", b"%PDF-1.4 x", "application/pdf")},
              revision=I.revision(ref))
    assert r.status_code == 303
    assert _meta(ref, "attachments") == ["private/导师批注.pdf"]
    saved = get_paths().root / "private" / SLUG / "导师批注.pdf"
    assert saved.read_bytes() == b"%PDF-1.4 x"
    page = client.get(f"/issues/{ref}").text
    strip = page.split('class="issue-mats-strip"', 1)[1].split("</div>\n</div>", 1)[0]
    assert "导师批注.pdf" in strip and ">私有</button>" in strip


def test_names_are_cleaned_and_never_leave_the_folder(repo):
    ref = _issue()
    for evil in ("../../evil.pdf", "..\\..\\evil.pdf", "C:\\Users\\x\\evil.pdf", "a/b/evil.pdf", ".hidden.pdf"):
        I.attach_upload(ref, evil, b"x", I.revision(ref))
    folder = (get_paths().root / "private" / SLUG).resolve()
    rels = _meta(ref, "attachments")
    assert all(r.startswith("private/") and "/" not in r[len("private/"):] and ".." not in r for r in rels)
    for r in rels:
        assert (folder / r.split("/", 1)[1]).resolve().parent == folder
    assert "private/evil.pdf" in rels and "private/evil-2.pdf" in rels       # never overwrites
    assert "private/hidden.pdf" in rels                                       # no dot files
    assert not (get_paths().root / "evil.pdf").exists() and not (get_paths().root.parent / "evil.pdf").exists()


@pytest.mark.parametrize("name", ["run.sh", "image.svg", "tool.exe", "macro.command", "page.xhtml", "page.shtml", "noext"])
def test_only_documents_and_images_are_accepted(repo, name):
    ref = _issue()
    with pytest.raises(I.IssueError, match="只能附上"):
        I.attach_upload(ref, name, b"x", I.revision(ref))
    assert not _meta(ref, "attachments")


def test_uploads_have_a_size_cap(repo, monkeypatch):
    ref = _issue()
    monkeypatch.setattr(I, "ATTACH_MAX", 10)
    with pytest.raises(I.IssueError, match="不能超过"):
        I.attach_upload(ref, "big.pdf", b"x" * 11, I.revision(ref))


def test_an_attachment_moves_between_private_and_drafts_and_detaching_keeps_the_file(repo):
    ref = _issue()
    I.attach_upload(ref, "稿子.docx", b"doc", I.revision(ref))
    I.move_attachment(ref, "private/稿子.docx", "drafts", I.revision(ref))
    assert _meta(ref, "attachments") == ["drafts/稿子.docx"]
    drafts = get_paths().projects / SLUG / "drafts" / "稿子.docx"
    assert drafts.exists() and not (get_paths().root / "private" / SLUG / "稿子.docx").exists()
    I.detach(ref, "drafts/稿子.docx", I.revision(ref))
    assert _meta(ref, "attachments") == [] and drafts.exists()


def test_opening_only_reaches_the_issues_own_files(client, repo, monkeypatch):
    ref = _issue()
    I.attach_upload(ref, "a.pdf", b"x", I.revision(ref))
    (get_paths().root / "private" / SLUG / "other.pdf").write_bytes(b"y")
    opened = []
    monkeypatch.setattr(deliverables, "open_with_system", lambda path, reveal=False: opened.append(path.name))
    assert _post(client, f"/issues/{ref}/attachments/open", rel="private/a.pdf").status_code == 200
    assert _post(client, f"/issues/{ref}/attachments/open", rel="private/other.pdf").status_code == 400
    assert _post(client, f"/issues/{ref}/attachments/open", rel="private/../../etc/passwd").status_code == 400
    assert opened == ["a.pdf"]


@pytest.mark.parametrize("name", ["导出的网页.html", "page.HTM"])
def test_a_web_page_attaches_and_is_labelled_as_one(client, repo, name):
    """Leo 10-16: saved web pages are attachments too, listed in the 课题's files as 网页."""
    ref = _issue()
    r = _post(client, f"/issues/{ref}/attachments", files={"file": (name, b"<html><body>saved</body></html>", "text/html")},
              revision=I.revision(ref))
    assert r.status_code == 303
    assert _meta(ref, "attachments") == ["private/" + name]
    assert (get_paths().root / "private" / SLUG / name).read_bytes() == b"<html><body>saved</body></html>"
    assert [f["kind"] for f in deliverables.list_files(SLUG) if f["name"] == name] == ["网页"]


def test_a_web_page_only_ever_opens_with_the_system_never_in_the_app(client, repo, monkeypatch):
    """The safety property behind allowing .html: 打开 hands the file to the system's program (outside the app's
    origin) and the response carries nothing of it, so a script in an attached page never runs in the app."""
    ref = _issue()
    body = b"<html><script>fetch('/settings')</script>RL-MARKER</html>"
    I.attach_upload(ref, "page.html", body, I.revision(ref))
    saved = get_paths().root / "private" / SLUG / "page.html"
    opened = []
    monkeypatch.setattr(deliverables, "open_with_system", lambda path, reveal=False: opened.append((path, reveal)))
    r = _post(client, f"/issues/{ref}/attachments/open", rel="private/page.html")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.json() == {"ok": True} and b"RL-MARKER" not in r.content and "location" not in r.headers
    assert opened == [(saved, False)]
    page = client.get(f"/issues/{ref}").text
    assert "page.html" in page and "RL-MARKER" not in page and "fetch('/settings')" not in page
    for url in (f"/static/../private/{SLUG}/page.html", f"/private/{SLUG}/page.html",
                f"/projects/{SLUG}/private/page.html", f"/projects/{SLUG}/files/page.html",
                f"/site-assets/../private/{SLUG}/page.html"):
        assert b"RL-MARKER" not in client.get(url).content


def test_the_files_are_not_served_by_the_app(client, repo):
    ref = _issue()
    I.attach_upload(ref, "a.pdf", b"x", I.revision(ref))
    for url in (f"/static/../private/{SLUG}/a.pdf", f"/private/{SLUG}/a.pdf", f"/projects/{SLUG}/private/a.pdf"):
        r = client.get(url)
        assert r.status_code in (404, 405) or b"x" != r.content


def test_a_stale_revision_refuses_an_upload(client, repo):
    ref = _issue()
    r = _post(client, f"/issues/{ref}/attachments", files={"file": ("a.pdf", b"x", "application/pdf")}, revision="old")
    assert r.status_code == 409 and not _meta(ref, "attachments")


def test_an_issue_without_a_project_cannot_take_files(repo, monkeypatch):
    ref = _issue()
    real = I.get
    monkeypatch.setattr(I, "get", lambda r: {**real(r), "project": ""} if r == ref else real(r))
    with pytest.raises(I.IssueError, match="课题"):
        I.attach_upload(ref, "a.pdf", b"x", I.revision(ref))


# -------------------------------------------------------------------------------------------------- materials

def test_papers_and_datasets_are_linked_and_unlinked(client, repo, conn):
    _paper(conn, "lee2024double", "Double Reassurance and Alliance Autonomy")
    _dataset(conn, "DS-2026-0003", "核咨询小组会议纪要编码表")
    conn.commit()
    ref = _issue()
    for item in ("paper:lee2024double", "dataset:DS-2026-0003"):
        assert _post(client, f"/issues/{ref}/materials", toggle=item, revision=I.revision(ref)).status_code == 303
    assert _meta(ref, "materials") == ["paper:lee2024double", "dataset:DS-2026-0003"]
    page = client.get(f"/issues/{ref}").text
    assert 'href="/papers/lee2024double"' in page and "Lee 2024 · Double Reassurance" in page
    assert 'href="/data/datasets/DS-2026-0003"' in page and "48 行" in page
    # the picker's second press on a linked item unlinks it; a chip's × too
    _post(client, f"/issues/{ref}/materials", toggle="paper:lee2024double", revision=I.revision(ref))
    _post(client, f"/issues/{ref}/materials", item="dataset:DS-2026-0003", on="0", revision=I.revision(ref))
    assert _meta(ref, "materials") == []


def test_only_rows_of_the_library_can_be_linked(conn, repo):
    ref = _issue()
    for bad in ("paper:nope", "dataset:DS-2026-9999", "javascript:alert(1)", "paper:../x", "artifact:ART-2026-0001"):
        with pytest.raises(I.IssueError):
            I.set_material(conn, ref, bad, True, I.revision(ref))


def test_the_picker_groups_this_projects_papers_others_and_datasets(client, repo, conn):
    _paper(conn, "cha2023sharing", "Nuclear Sharing Without Nuclear Weapons", 2023, "International Security")
    _paper(conn, "narang2025", "Nuclear Strategies of Emerging Powers", 2025)
    _dataset(conn, "DS-2026-0004", "Nuclear consultative group coding")
    conn.execute("INSERT INTO paper_projects(paper_id, project_slug) VALUES ('cha2023sharing', ?)", (SLUG,))
    conn.commit()
    ref = _issue()
    I.set_material(conn, ref, "paper:cha2023sharing", True, I.revision(ref))
    html = client.get(f"/issues/{ref}/materials/search?q=nuclear").text
    here, rest = html.split("文献库里的其他文献", 1)
    assert "本课题的文献" in here and "Nuclear Sharing" in here and "已关联" in here and 'aria-selected="true"' in here
    others, data = rest.split("数据集", 1)
    assert "Nuclear Strategies" in others and "Nuclear consultative group coding" in data and "48 行 · 12 列" in data
    assert 'name="toggle" value="dataset:DS-2026-0004"' in data
    assert "没有找到「zzz」" in client.get(f"/issues/{ref}/materials/search?q=zzz").text


def test_the_project_page_counts_issue_links_as_indirect(client, repo, conn):
    _paper(conn, "only2026issue", "Only Linked From An Issue", 2026)
    conn.commit()
    ref = _issue()
    I.set_material(conn, ref, "paper:only2026issue", True, I.revision(ref))
    page = client.get(f"/projects/{SLUG}?tab=papers").text
    assert "Only Linked From An Issue" in page
    row = page.split("Only Linked From An Issue", 1)[1].split("</li>", 1)[0]
    assert "间接关联" in row
    assert conn.execute("SELECT COUNT(*) FROM paper_projects WHERE paper_id = 'only2026issue'").fetchone()[0] == 0


def test_the_page_offers_the_two_icon_entries(client, repo):
    page = client.get(f"/issues/{_issue()}").text
    tools = page.split('class="issue-mats-tools"', 1)[1].split('class="issue-pick-foot"', 1)[0]
    assert 'aria-label="添加附件"' in tools and "#paperclip" in tools and 'type="file"' in tools
    assert 'aria-label="关联文献或数据"' in tools and "#book-plus" in tools
    assert 'accept=".pdf,' in tools


@pytest.mark.parametrize("name", ["a.html\\.pdf", "a.html/.pdf", ".pdf", "..pdf", "x/.docx"])
def test_the_type_is_judged_on_the_cleaned_name(repo, name):
    """card 0117: a name that loses its suffix in cleaning ("a.html\\.pdf" → "pdf") is refused."""
    ref = _issue()
    with pytest.raises(I.IssueError, match="只能附上"):
        I.attach_upload(ref, name, b"x", I.revision(ref))
    assert not _meta(ref, "attachments")


def test_invisible_direction_and_zero_width_characters_are_removed(repo):
    rlo, zwsp, bom, lri = "‮", "​", "﻿", "⁦"
    assert deliverables.clean_name(f"evil{rlo}fdp.exe") == "evilfdp.exe"
    assert deliverables.clean_name(f"{bom}{zwsp}批注{lri}稿.pdf") == "批注稿.pdf"
    ref = _issue()
    I.attach_upload(ref, f"导师{rlo}批注.pdf", b"x", I.revision(ref))
    assert _meta(ref, "attachments") == ["private/导师批注.pdf"]
    with pytest.raises(I.IssueError, match="只能附上"):
        I.attach_upload(ref, f"run{rlo}fdp.sh", b"x", I.revision(ref))   # reads as "runhs.pdf" but is a script


def test_the_project_page_explains_indirect_links_through_issues(client, repo, conn):
    from datetime import datetime as _dt
    now = _dt.now().isoformat()
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, venue, created_at, updated_at) "
                 "VALUES ('tip2026', 'Tooltip Paper', 'tip2026', '[]', 2026, '', ?, ?)", (now, now))
    conn.commit()
    ref = _issue()
    I.set_material(conn, ref, "paper:tip2026", True, I.revision(ref))
    row = client.get(f"/projects/{SLUG}?tab=papers").text.split("Tooltip Paper", 1)[1].split("</li>", 1)[0]
    assert 'title="通过划线、文献地图或子议题的关联进入课题">间接关联' in row


@pytest.mark.parametrize("suffix", ["epub", "mobi", "azw3", "azw", "djvu", "fb2", "EPUB"])
def test_ebooks_attach_privately_and_open_with_system(client, repo, monkeypatch, suffix):
    ref = _issue()
    name = "书目." + suffix
    opened = []
    monkeypatch.setattr(deliverables, "open_with_system", lambda path, reveal=False: opened.append(path))
    response = _post(client, f"/issues/{ref}/attachments",
                     files={"file": (name, b"synthetic ebook", "application/octet-stream")},
                     revision=I.revision(ref))
    assert response.status_code == 303
    assert _meta(ref, "attachments") == ["private/" + name]
    assert I.ATTACH_MAX == deliverables.MAX_UPLOAD == 200 * 1024 * 1024
    assert _post(client, f"/issues/{ref}/attachments/open", rel="private/" + name).status_code == 200
    assert opened == [get_paths().root / "private" / SLUG / name]
    assert opened[0].read_bytes() == b"synthetic ebook"
    page = client.get(f"/issues/{ref}").text
    accept = page.split('data-attach-input accept="', 1)[1].split('"', 1)[0].split(',')
    assert "." + suffix.lower() in accept


def test_a_comment_can_carry_an_upload_and_existing_files(client, repo):
    """Card 0176: the comment box takes one file (to drafts/) and `attach` handles; both show on the comment and the issue."""
    ref = _issue()
    drafts = get_paths().projects / SLUG / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / "memo.md").write_text("# memo\n", encoding="utf-8")
    r = _post(client, f"/issues/{ref}/comments", files={"file": ("note.md", b"# note", "text/markdown")},
              text="见附件", revision=I.revision(ref), attach="drafts/memo.md")
    assert r.status_code == 303, r.text
    last = [c for c in I.comments(ref) if c.get("kind") == "comment"][-1]
    assert last["text"] == "见附件" and last["attachments"] == ["drafts/memo.md", "drafts/note.md"]
    assert {a["rel"] for a in I.attachments(ref)} == {"drafts/memo.md", "drafts/note.md"}
    r = _post(client, f"/issues/{ref}/comments", files={"file": ("only.md", b"x", "text/markdown")}, revision=I.revision(ref))
    assert r.status_code == 303
    assert [c for c in I.comments(ref) if c.get("kind") == "comment"][-1]["text"] == "附件：only.md"
    page = client.get(f"/issues/{ref}").text
    assert 'data-rel="drafts/note.md"' in page and "activity-attachments" in page
