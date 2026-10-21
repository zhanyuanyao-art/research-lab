"""V2 workspace UI: Chinese navigation, the Artifacts board by research direction, direction folders and
their management, the artifact editor's sha-guarded body save (managed blocks preserved), front-matter
edits, and editable projects."""
from __future__ import annotations

import difflib
import re
from pathlib import Path

import pytest

from research_lab.files import artifacts as A
from research_lab.files import datafiles as D
from research_lab.files.weeks import week_of
from research_lab.i18n import Labels, week_label

LANDSCAPE = "_artifacts/2026-W40-research-landscape.md"
REF = "ART-2026-4001"
BLOCK = "<!-- rl:begin excerpts -->\n- Veto keeps the chair [EXC-2026-0001] <!-- rl:EXC-2026-0001 -->\n<!-- rl:end -->"


@pytest.fixture()
def client(client):
    client.follow_redirects = False      # assert on the 303s themselves
    return client


def _shelf(html: str, slug: str) -> str:
    m = re.search(rf'<a class="card folder[^"]*" href="/artifacts/d/{re.escape(slug)}".*?</a>', html, re.S)
    assert m, f"no shelf {slug}"
    return m.group(0)


def _changed_lines(before: str, after: str) -> list[str]:
    return [l for l in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
            if l[:1] in "+-" and not l.startswith(("+++", "---"))]


def _with_block(repo: Path) -> A.Artifact:
    a = A.load(repo / LANDSCAPE)
    A.append_to_section(a, "Evidence", "- Veto keeps the chair [EXC-2026-0001] <!-- rl:EXC-2026-0001 -->")
    A.set_ai_notes(a, "- Outline: rule before attendance.")
    A.save(a)
    return A.load(repo / LANDSCAPE)


# ---------------------------------------------------------------- navigation

def test_chinese_nav_and_week_label(client):
    html = client.get("/").text
    primary = re.search(r'<nav class="nav" aria-label="主导航">.*?</nav>', html, re.S).group(0)
    labels = re.findall(r'<a class="nav-item[^"]*" href="([^"]+)"[^>]*>.*?<span>([^<]+)</span></a>', primary, re.S)
    assert labels == [("/", "首页"), ("/papers", "文献"), ("/data", "数据"), ("/issues", "研究档案")]
    foot = re.search(r'<div class="sidebar-foot">.*?</div>', html, re.S).group(0)
    assert 'href="/chat"' in foot and "Theo" in foot and 'href="/settings"' in foot
    assert week_label(week_of()) in html and "年 第" in week_label(week_of())
    # the only composer outside /chat is the floating assistant's (card 0074), not a docked page composer
    assert "partials/_composer" not in html and "data-composer-here" not in html
    assert html.count('<form class="composer"') == 1 and html.index('id="assistant-dock"') < html.index('data-composer-dock')
    week = client.get("/week").text
    assert "本周工作台" in week and 'href="/artifacts"' in week
    assert re.search(r'<a class="nav-item active" href="/issues"', week)   # Archive section lit on /week


def test_labels_fall_back_to_english():
    L = Labels({"types": [{"key": "new-type", "label": "New Type"}]})
    assert L.stage["exploring"] == "探索" and L.type["landscape"] == "研究版图"
    assert L.type["new-type"] == "New Type" and L.level["weird"] == "weird" and L.milestone["M3"] == "文献地图 v1"


# ---------------------------------------------------------------- board and folders

def test_board_groups_by_direction(client, repo, conn):
    A.create("question", "alliance-q", "us-rok-reassurance", title="Why reassure a treaty ally?", direction="alliance-us")
    A.create("memo", "signal-memo", "asean-centrality", title="Costly signals at ASEAN", direction="strategy-signaling")
    A.create("memo", "loose-memo", "asean-centrality", title="Stray memo", direction="no-such-direction")
    html = client.get("/artifacts?group=direction").text                          # 按方向 (the default is 按状态)
    alliance, sea = _shelf(html, "alliance-us"), _shelf(html, "sea-asean")
    assert "<b>1</b><span>问题</span>" in alliance
    assert "Stray memo" in html and "1 个未归类" in html      # recent list shows everything, unsorted are flagged
    folder = client.get("/artifacts/d/alliance-us").text
    assert "Why reassure a treaty ally?" in folder and "Research Landscape" not in folder
    sea_folder = client.get("/artifacts/d/sea-asean").text
    assert "Research Landscape" in sea_folder                # no front-matter direction → project's direction
    assert "Costly signals at ASEAN" in client.get("/artifacts/d/strategy-signaling").text
    filtered = client.get("/artifacts/d/strategy-signaling?type=memo").text if False else client.get("/artifacts?group=direction&type=memo").text
    assert "<b>1</b><span>结论</span>" in _shelf(filtered, "strategy-signaling")
    row = conn.execute("SELECT direction, direction_source FROM artifacts WHERE ref = ?", (REF,)).fetchone()
    assert tuple(row) == ("sea-asean", "project")


def test_direction_folder(client, repo):
    A.create("literature-map", "sea-map", "asean-centrality", title="Map of centrality camps")
    A.create("analysis", "sea-run", "asean-centrality", title="Attendance regression")
    page = client.get("/artifacts/d/sea-asean")
    assert page.status_code == 200
    html = page.text
    for k in ("question", "literature", "data", "conclusion"):
        assert f'href="?kind={k}"' in html
    assert "Map of centrality camps" in html and "Attendance regression" in html and "ASEAN Centrality" in html
    lit = client.get("/artifacts/d/sea-asean?kind=literature").text
    assert "Map of centrality camps" in lit and "Attendance regression" not in lit
    data_tab = client.get("/artifacts/d/sea-asean?kind=data").text
    assert "Attendance regression" in data_tab and "Map of centrality camps" not in data_tab
    assert 'href="/artifacts/new?direction=sea-asean"' in html
    assert client.get("/artifacts/d/nope").status_code == 404


def test_direction_management(client, repo):
    p = repo / "_data" / "directions.yml"
    before = p.read_text()
    r = client.post("/artifacts/directions", data={"slug": "maritime", "key": "msec", "name": "海洋安全", "keywords": "sea lanes, piracy",
                                                   "sha": D.file_sha("directions")})
    assert r.status_code == 303
    added = p.read_text()
    assert added.startswith(before) and "- slug: maritime\n  key: MSEC\n" in added and "keywords: [sea lanes, piracy]" in added
    r = client.post("/artifacts/directions/maritime", data={"name": "海洋安全研究", "name_en": "", "keywords": "sea lanes, piracy",
                                                           "description": "", "sha": D.file_sha("directions")})
    assert r.status_code == 303 and _changed_lines(added, p.read_text()) == ["-  name: 海洋安全", "+  name: 海洋安全研究"]
    in_use = client.post("/artifacts/directions/sea-asean/delete", data={"sha": D.file_sha("directions")})
    assert in_use.status_code == 400 and "sea-asean" in p.read_text()
    stale = client.post("/artifacts/directions/maritime/delete", data={"sha": "0" * 64})
    assert stale.status_code == 409 and "maritime" in p.read_text()
    assert client.post("/artifacts/directions/maritime/delete", data={"sha": D.file_sha("directions")}).status_code == 303
    assert p.read_text() == before
    bad = client.post("/artifacts/directions", data={"slug": "Bad Slug", "name": "x"})
    assert bad.status_code == 400


def test_new_artifact_with_direction(client, repo):
    form = client.get("/artifacts/new?direction=alliance-us").text
    assert 'name="direction"' in form and 'value="alliance-us" selected' in form
    r = client.post("/artifacts", data={"type": "question", "level": "seed", "project": "asean-centrality",
                                        "direction": "periphery-nts", "title": "Border floods and ASEAN", "slug": "border-floods"})
    assert r.status_code == 303 and "?tab=edit" in r.headers["location"]
    a = A.load(repo / "_artifacts" / f"{week_of()}-border-floods.md")
    assert a.get("direction") == "periphery-nts" and list(a.front).index("direction") == list(a.front).index("project") + 1


# ---------------------------------------------------------------- editor

def test_edit_tab_renders_editor(client, repo):
    a = _with_block(repo)
    html = client.get(f"/artifacts/{REF}?tab=edit").text
    assert "Back to output" in html and "/static/vendor/toastui-editor-all.min.js" in html
    assert 'class="on" data-editor-mode="wysiwyg"' in html and ">Source<" in html   # 文档 mode first, Markdown one click away
    assert 'id="body-src"' in html and "&lt;!-- rl:begin excerpts --&gt;" in html
    src = re.search(r'<textarea name="body" id="body-src"[^>]*>(.*?)</textarea>', html, re.S).group(1)
    assert "## AI notes" not in src                          # AI notes are shown read-only, not edited
    assert f'value="{a.sha}"' in html and 'action="/artifacts/ART-2026-4001/meta"' in html
    read = client.get(f"/artifacts/{REF}").text
    assert '<script src="/static/vendor/toastui-editor-all.min.js' not in read     # loaded only when 描述 is opened
    assert 'data-edit-desc' in read and 'id="body-form" data-editor' in read


def test_body_save_preserves_managed_block(client, repo):
    a = _with_block(repo)
    path = repo / LANDSCAPE
    edited = A.editable_body(a.body).replace("## Next question", "## Next question\n\nA new paragraph typed in the editor.")
    # a WYSIWYG round trip may reflow the managed block: the server restores it from disk
    edited = edited.replace("- Veto keeps the chair [EXC-2026-0001] <!-- rl:EXC-2026-0001 -->", "* Veto keeps the chair")
    r = client.post(f"/artifacts/{REF}/body", data={"body": edited, "sha": a.sha}, headers={"X-RL-Editor": "1"})
    assert r.status_code == 200 and "已保存" in r.text
    text = path.read_text()
    assert "A new paragraph typed in the editor." in text
    assert BLOCK in text and text.count("Veto keeps the chair") == 1
    assert "## AI notes\n\n- Outline: rule before attendance.\n" in text
    assert r.headers["X-RL-Sha"] == A.load(path).sha
    # the editor dropped the markers entirely: block re-inserted in its section, loose line removed
    b = A.load(path)
    stripped = A.editable_body(b.body).replace(BLOCK, "- Veto keeps the chair [EXC-2026-0001]")
    r2 = client.post(f"/artifacts/{REF}/body", data={"body": stripped, "sha": b.sha}, headers={"X-RL-Editor": "1"})
    assert r2.status_code == 200
    text2 = path.read_text()
    assert text2.count("Veto keeps the chair") == 1 and BLOCK in text2
    ev = A.section_body(A.load(path).body, "Evidence")
    assert BLOCK in ev
    # unchanged round trip is byte-identical
    c = A.load(path)
    client.post(f"/artifacts/{REF}/body", data={"body": A.editable_body(c.body), "sha": c.sha}, headers={"X-RL-Editor": "1"})
    assert path.read_text() == text2


def test_body_save_stale_sha_409(client, repo):
    a = _with_block(repo)
    path = repo / LANDSCAPE
    disk = path.read_text()
    r = client.post(f"/artifacts/{REF}/body", data={"body": "My edit.\n", "sha": "0" * 64}, headers={"X-RL-Editor": "1"})
    assert r.status_code == 409 and path.read_text() == disk
    assert "保存冲突" in r.text and "@@" in r.text and "+My edit." in r.text
    assert r.headers["X-RL-Current-Sha"] == a.sha
    plain = client.post(f"/artifacts/{REF}/body", data={"body": "My edit.\n", "sha": "0" * 64})
    assert plain.status_code == 409 and re.search(r"<textarea[^>]*>My edit\.\s*</textarea>", plain.text)


def test_body_source_endpoint(client, repo):
    a = _with_block(repo)
    r = client.get(f"/artifacts/{REF}/body")
    assert r.status_code == 200 and r.headers["X-RL-Sha"] == a.sha and BLOCK in r.text and "AI notes" not in r.text


def test_direction_change_writes_front_matter(client, repo, conn):
    path = repo / LANDSCAPE
    before = path.read_text()
    a = A.load(path)
    r = client.post(f"/artifacts/{REF}/direction", data={"direction": "strategy-signaling", "sha": a.sha})
    assert r.status_code == 303 and "saved=direction" in r.headers["location"]
    after = path.read_text()
    assert _changed_lines(before, after) == ["+direction: strategy-signaling"]
    assert after.index("direction: strategy-signaling") > after.index("project: asean-centrality")
    row = conn.execute("SELECT direction, direction_source FROM artifacts WHERE ref = ?", (REF,)).fetchone()
    assert tuple(row) == ("strategy-signaling", "front")
    assert "战略研究与信号分析" in client.get(f"/artifacts/{REF}").text
    stale = client.post(f"/artifacts/{REF}/direction", data={"direction": "alliance-us", "sha": a.sha})
    assert stale.status_code == 409 and path.read_text() == after
    bad = client.post(f"/artifacts/{REF}/direction", data={"direction": "nope", "sha": A.load(path).sha})
    assert bad.status_code == 400


def test_meta_form(client, repo):
    path = repo / LANDSCAPE
    a = A.load(path)
    data = {"sha": a.sha, "title": "Landscape, retitled", "type": "landscape", "level": "seed", "status": "active",
            "version": "v0.1", "project": "asean-centrality", "direction": "", "related": ""}
    r = client.post(f"/artifacts/{REF}/meta", data=data)
    assert r.status_code == 303 and "tab=edit" in r.headers["location"]
    b = A.load(path)
    assert b.get("title") == "Landscape, retitled" and b.get("status") == "active" and "direction" not in b.front
    promote = client.post(f"/artifacts/{REF}/meta", data={**data, "sha": b.sha, "level": "developed"})
    assert promote.status_code == 400 and "What did I figure out" in promote.text and A.load(path).get("level") == "seed"
    bad_rel = client.post(f"/artifacts/{REF}/meta", data={**data, "sha": b.sha, "related": "ART-2099-0101"})
    assert bad_rel.status_code == 400
    assert client.post(f"/artifacts/{REF}/meta", data={**data, "sha": "0" * 64}).status_code == 409


# ---------------------------------------------------------------- projects

def test_project_edit_one_line_diff(client, repo):
    p = repo / "_data" / "projects.yml"
    before = p.read_text()
    form = client.get("/projects/asean-centrality/edit").text
    assert 'name="question"' in form and 'name="direction"' in form
    proj = D.project("asean-centrality")
    data = {k: str(proj.get(k) or "") for k in ("name", "question", "stage", "direction", "since", "note")}
    data.update(note="Main project; the chair-rule puzzle is next.", sha=D.file_sha("projects"))
    r = client.post("/projects/asean-centrality/edit", data=data)
    assert r.status_code == 303
    assert _changed_lines(before, p.read_text()) == [
        "-  note: Current main project. Grew out of the ASEAN relational-networks paper.",
        "+  note: Main project; the chair-rule puzzle is next.",
    ]
    stale = client.post("/projects/asean-centrality/edit", data={**data, "note": "Another", "sha": "0" * 64})
    assert stale.status_code == 409 and "@@" in stale.text and "Another" in stale.text
    assert "Another" not in p.read_text()
    bad = client.post("/projects/asean-centrality/edit", data={**data, "stage": "nope", "sha": D.file_sha("projects")})
    assert bad.status_code == 400


def test_new_project_creates_files(client, repo):
    r = client.post("/projects/new", data={"slug": "mekong-water", "name": "Mekong Water Politics", "stage": "idea",
                                           "question": "Why does the MRC stay weak?", "direction": "periphery-nts",
                                           "since": "2026-09", "note": "", "sha": D.file_sha("projects")})
    assert r.status_code == 303 and r.headers["location"].startswith("/projects/mekong-water")
    folder = repo / "projects" / "mekong-water"
    for name in ("index.md", "literature.md", "evidence.md"):
        assert (folder / name).exists()
    index = (folder / "index.md").read_text()
    assert "slug: mekong-water" in index and "# Mekong Water Politics" in index and "Why does the MRC stay weak?" in index
    entry = D.project("mekong-water")
    assert entry["direction"] == "periphery-nts" and entry["stage"] == "idea"
    assert client.get("/projects/mekong-water").status_code == 200
    dup = client.post("/projects/new", data={"slug": "mekong-water", "name": "x", "stage": "idea", "question": "q"})
    assert dup.status_code == 400


def test_project_file_edit(client, repo):
    page = client.get("/projects/asean-centrality/files/literature.md")
    assert page.status_code == 200 and 'name="text"' in page.text
    text, sha = D.read_project_file("asean-centrality", "literature.md")
    r = client.post("/projects/asean-centrality/files/literature.md", data={"text": text + "\nNew line.\n", "sha": sha})
    assert r.status_code == 303
    assert (repo / "projects/asean-centrality/literature.md").read_text().endswith("New line.\n")
    stale = client.post("/projects/asean-centrality/files/literature.md", data={"text": "overwrite", "sha": sha})
    assert stale.status_code == 409 and "@@" in stale.text
    assert client.get("/projects/asean-centrality/files/secret.md").status_code == 404


def test_project_file_editor_contract(client, repo):
    """The shared editor script saves project files like artifact bodies: X-RL-Editor → fragment + sha."""
    text, sha = D.read_project_file("asean-centrality", "literature.md")
    r = client.post("/projects/asean-centrality/files/literature.md", data={"text": text + "\nEditor line.\n", "sha": sha},
                    headers={"X-RL-Editor": "1"})
    assert r.status_code == 200 and "已保存" in r.text and len(r.headers["X-RL-Sha"]) == 64
    assert (repo / "projects/asean-centrality/literature.md").read_text().endswith("Editor line.\n")
    stale = client.post("/projects/asean-centrality/files/literature.md", data={"text": "mine", "sha": sha}, headers={"X-RL-Editor": "1"})
    assert stale.status_code == 409 and "@@" in stale.text and 'data-conflict="overwrite"' in stale.text
    assert stale.headers["X-RL-Current-Sha"] == r.headers["X-RL-Sha"]
    src = client.get("/projects/asean-centrality/files/literature.md/source")
    assert src.status_code == 200 and src.headers["X-RL-Sha"] == r.headers["X-RL-Sha"] and src.text.endswith("Editor line.\n")


def test_project_hub_lists_materials_and_links_papers(client, repo):
    page = client.get("/projects/asean-centrality").text
    for tab in ("文献", "划线", "产出", "文件", "研究工具"):
        assert tab in page
    assert "/projects/asean-centrality/files/index.md" in page and "/projects/asean-centrality/matrix" in page
    from research_lab.db.connection import connect
    conn = connect()
    try:
        # A fresh test library is empty; the hub test owns its one paper fixture.
        paper = "hub-test-paper"
        conn.execute("INSERT INTO papers(id,title,title_hash,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (paper, "Project hub test paper", "hub-test-title", "2026-01-01", "2026-01-01"))
        r = client.post("/projects/us-rok-reassurance/papers", data={"paper_id": paper}, follow_redirects=False)
        assert r.status_code in (204, 303)
        assert conn.execute("SELECT 1 FROM paper_projects WHERE paper_id = ? AND project_slug = 'us-rok-reassurance'", (paper,)).fetchone()
        assert "移出课题" in client.get("/projects/us-rok-reassurance").text
        r = client.post(f"/projects/us-rok-reassurance/papers/{paper}/detach", follow_redirects=False)
        assert r.status_code in (204, 303)
        assert not conn.execute("SELECT 1 FROM paper_projects WHERE paper_id = ? AND project_slug = 'us-rok-reassurance'", (paper,)).fetchone()
        bad = client.post("/projects/us-rok-reassurance/papers", data={"paper_id": "nope"}, follow_redirects=False)
        assert bad.status_code in (204, 303) and "error=" in bad.headers.get("location", bad.headers.get("hx-redirect", ""))
    finally:
        conn.close()


def test_theme_toggle_persists(client, repo):
    from research_lab.db.connection import connect, get_setting
    r = client.post("/_p/theme", data={"theme": "dark"})
    assert r.status_code == 204
    conn = connect()
    try:
        assert get_setting(conn, "ui.theme") == "dark"
    finally:
        conn.close()
    assert 'data-theme="dark"' in client.get("/projects").text and 'data-theme-pref="dark"' in client.get("/projects").text
    assert client.post("/_p/theme", data={"theme": "neon"}).status_code == 400
    client.post("/_p/theme", data={"theme": "system"})
    html = client.get("/projects").text
    assert 'data-theme-pref="system"' in html and 'data-theme="' not in html.split("<head>")[0]


def test_output_rename_move_and_trash(client, repo):
    """File management from the archive: rename keeps file and ref; move changes 课题; delete goes to app/data/trash."""
    from research_lab import weekly
    from research_lab.db.connection import connect
    conn = connect()
    try:
        a = weekly.load_ref(conn, REF)
        path = a.path
        r = client.post(f"/artifacts/{REF}/rename", data={"title": "改过的标题", "sha": a.sha, "next": "/artifacts"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/artifacts?saved=title"
        assert A.load(path).get("title") == "改过的标题" and path.exists()
        assert "改过的标题" in client.get("/artifacts").text and "已改名" in client.get("/artifacts?saved=title").text
        stale = client.post(f"/artifacts/{REF}/rename", data={"title": "x", "sha": a.sha}, follow_redirects=False)
        assert stale.status_code == 409
        a = weekly.load_ref(conn, REF)
        r = client.post(f"/artifacts/{REF}/project", data={"project": "us-rok-reassurance", "sha": a.sha}, follow_redirects=False)
        assert r.status_code == 303 and A.load(path).get("project") == "us-rok-reassurance"
        assert conn.execute("SELECT project FROM artifacts WHERE ref = ?", (REF,)).fetchone()[0] == "us-rok-reassurance"
        bad = client.post(f"/artifacts/{REF}/project", data={"project": "nope", "sha": weekly.load_ref(conn, REF).sha}, follow_redirects=False)
        assert bad.status_code == 400
        page = client.get(f"/artifacts/{REF}").text
        assert "移到回收站" in page and f"/artifacts/{REF}/rename" in page
        a = weekly.load_ref(conn, REF)
        r = client.post(f"/artifacts/{REF}/delete", data={"sha": a.sha, "next": f"/artifacts/{REF}"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/artifacts/d/") and "deleted=" in r.headers["location"]
        assert not path.exists()
        trashed = list(A.trash_dir().glob("*" + path.name))
        assert len(trashed) == 1 and A.load(trashed[0]).ref == REF
        assert client.get(f"/artifacts/{REF}").status_code == 404
        assert "已把「改过的标题」移到回收站" in client.get(r.headers["location"]).text
    finally:
        conn.close()


def test_row_menu_on_archive_lists(client, repo):
    html = client.get("/artifacts/d/sea-asean").text
    assert 'class="menu row-menu hover-actions"' in html and f"/artifacts/{REF}/delete" in html and 'name="next" value="/artifacts/d/sea-asean"' in html


def test_dashboard_direction_chips(client, repo):
    html = client.get("/").text
    assert "课题" in html and 'href="/issues?project=asean-centrality"' in html and 'id="home-progress"' in html
    assert "东南亚与东盟研究" in html
