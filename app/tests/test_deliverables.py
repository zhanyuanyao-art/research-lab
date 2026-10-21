"""课题 deliverables: listing drafts/ and private/, uploads, open with the system, export to .docx."""
from __future__ import annotations

import shutil

import pytest

from research_lab.files import deliverables as DL

SLUG = "asean-centrality"


@pytest.fixture(autouse=True)
def no_open(monkeypatch):
    monkeypatch.setenv("RL_OPEN_FILES", "0")


def test_list_resolve_and_traversal(repo):
    drafts = repo / "projects" / SLUG / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / "chapter-2.docx").write_bytes(b"PK")
    (drafts / ".DS_Store").write_bytes(b"")
    priv = repo / "private" / SLUG
    priv.mkdir(parents=True)
    (priv / "full-draft.pdf").write_bytes(b"%PDF")
    rows = DL.list_files(SLUG)
    assert {(r["rel"], r["kind"], r["folder_label"]) for r in rows} == {("drafts/chapter-2.docx", "Word", "草稿"), ("private/full-draft.pdf", "PDF", "私有")}
    assert DL.resolve(SLUG, "drafts/chapter-2.docx") == (drafts / "chapter-2.docx").resolve()
    for bad in ("drafts/../index.md", "../_data/now.yml", "drafts/", "index.md", "drafts/missing.docx", "etc/passwd"):
        with pytest.raises(DL.DeliverableError):
            DL.resolve(SLUG, bad)
    with pytest.raises(DL.DeliverableError, match="200 MB"):
        DL.save_upload(SLUG, "big.bin", b"x" * (DL.MAX_UPLOAD + 1))
    assert DL.save_upload(SLUG, "run.command", b"#!/bin/sh").suffix == ".command"   # stored, but never `open`ed


def test_project_page_lists_opens_and_uploads(client, repo):
    drafts = repo / "projects" / SLUG / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / "chapter-2.docx").write_bytes(b"PK")
    page = client.get(f"/projects/{SLUG}").text
    assert "交付稿" in page and "chapter-2.docx" in page and 'name="rel" value="drafts/chapter-2.docx"' in page
    r = client.post(f"/projects/{SLUG}/deliverables/open", data={"rel": "drafts/chapter-2.docx"}, follow_redirects=False)
    assert r.status_code in (204, 303)
    bad = client.post(f"/projects/{SLUG}/deliverables/open", data={"rel": "drafts/../index.md"}, follow_redirects=False)
    assert "error=" in bad.headers.get("hx-redirect", bad.headers.get("location", ""))
    up = client.post(f"/projects/{SLUG}/deliverables/upload", files={"file": ("notes.md", b"# notes\n", "text/markdown")}, follow_redirects=False)
    assert up.status_code in (204, 303)
    assert (drafts / "notes.md").read_bytes() == b"# notes\n"
    client.post(f"/projects/{SLUG}/deliverables/upload", files={"file": ("notes.md", b"again", "text/markdown")})
    assert (drafts / "notes-2.md").exists() and (drafts / "notes.md").read_bytes() == b"# notes\n"   # never overwrite


@pytest.mark.skipif(not shutil.which("textutil"), reason="textutil is macOS only")
def test_export_output_to_docx(client, repo):
    from research_lab import weekly
    from research_lab.db.connection import connect
    conn = connect()
    try:
        ref = conn.execute("SELECT ref FROM artifacts WHERE project = ? LIMIT 1", (SLUG,)).fetchone()
        if ref is None:
            weekly.load_ref(conn, "ART-2026-4001")
            ref = conn.execute("SELECT ref FROM artifacts WHERE project = ? LIMIT 1", (SLUG,)).fetchone()
        ref = ref[0]
        other = client.post(f"/projects/us-rok-reassurance/deliverables/export", data={"ref": ref}, follow_redirects=False)
        assert "error=" in other.headers.get("hx-redirect", other.headers.get("location", ""))   # not that 课题's output
        r = client.post(f"/projects/{SLUG}/deliverables/export", data={"ref": ref}, follow_redirects=False)
        assert "saved=export" in r.headers.get("hx-redirect", r.headers.get("location", ""))
        files = list((repo / "projects" / SLUG / "drafts").glob("*.docx"))
        assert len(files) == 1 and files[0].stat().st_size > 1000
        client.post(f"/projects/{SLUG}/deliverables/export", data={"ref": ref})
        assert len(list((repo / "projects" / SLUG / "drafts").glob("*.docx"))) == 2   # -2 suffix, no overwrite
        assert files[0].name in client.get(f"/projects/{SLUG}").text
    finally:
        conn.close()
