"""The import sheet (card 0130, docs/design/LIBRARY-UI.md §3.3、§八 3): one file or one identifier per request with a
result per queue row, possible duplicates answered in place, bibliography records for DOIs without an open PDF,
放到 defaults from the page, and imports that no longer join the current project by themselves."""
from __future__ import annotations

import re

import httpx
import pytest

from research_lab.intake import identifiers

DOI = "10.1234/rl.2023.001"
TITLE = "Who Convenes Asia? Forum attendance and the durability of ASEAN-led institutions"


def _crossref(request: httpx.Request) -> httpx.Response | None:
    if request.url.host == "api.crossref.org" and request.url.path == f"/works/{DOI}":
        return httpx.Response(200, json={"status": "ok", "message": {
            "DOI": DOI, "title": [TITLE], "author": [{"family": "Tan", "given": "Lin"}],
            "issued": {"date-parts": [[2023]]}, "container-title": ["The Pacific Review"], "type": "journal-article"}})
    return None


@pytest.fixture()
def web(client, sample_pdf):
    """The client with the network mocked: Crossref knows DOI, arXiv serves the sample PDF, the rest is 404."""
    pdf = sample_pdf.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        hit = _crossref(request)
        if hit is not None:
            return hit
        if request.url.host == "arxiv.org" and request.url.path.startswith("/pdf/"):
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        return httpx.Response(404, json={"status": "error"})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client.app.state.intake_http = http
    yield client
    http.close()


def upload(client, path, **picks):
    with open(path, "rb") as fh:
        return client.post("/imports/files", data=picks, files={"file": (path.name, fh, "application/pdf")}).json()


@pytest.mark.parametrize("line, kind, value", [
    ("10.1017/S0003055419000731", "doi", "10.1017/s0003055419000731"),
    ("https://doi.org/10.1017/S0003055419000731", "doi", "10.1017/s0003055419000731"),
    ("doi:10.1093/isq/sqab001.", "doi", "10.1093/isq/sqab001"),
    ("https://www.tandfonline.com/doi/full/10.1080/09512748.2020.1234567", "doi", "10.1080/09512748.2020.1234567"),
    ("https://www.cambridge.org/core/services/aop/10.1017%2FS0020818300000001.pdf", "doi", "10.1017/s0020818300000001"),
    ("2310.01234", "arxiv", "2310.01234"),
    ("https://arxiv.org/abs/2310.01234v2", "arxiv", "2310.01234"),
    ("arXiv:2310.01234", "arxiv", "2310.01234"),
    ("Who Convenes Asia?", "unknown", ""),
])
def test_identifiers_are_read_from_dois_arxiv_ids_and_urls(line, kind, value):
    ident = identifiers.parse_one(line)
    assert (ident.kind, ident.value) == (kind, value)


def test_a_paste_is_one_identifier_per_line_without_repeats():
    got = identifiers.parse("10.1234/a\n\n https://doi.org/10.1234/A \n2310.01234\n" + "\n".join(f"10.9999/{i}" for i in range(80)))
    assert [i.raw for i in got[:2]] == ["10.1234/a", "2310.01234"] and len(got) == identifiers.MAX_LINES
    assert identifiers.parse_one("2310.01234").doi == "10.48550/arxiv.2310.01234"


def test_the_sheet_preselects_where_papers_go_from_the_page(client, conn):
    from research_lab import paper_collections as C
    ref = C.create(conn, "第二章材料")
    conn.commit()

    def picked(page):
        html = client.get("/imports/sheet", params={"page": page}).text
        sel = {}
        for name in ("direction", "project", "collection"):
            box = html.split(f'name="{name}"', 1)[1].split("</select>", 1)[0]
            m = re.search(r'<option value="([^"]*)" selected>', box)
            sel[name] = m.group(1) if m else ""
        return sel, html
    sel, html = picked("/")
    assert sel == {"direction": "", "project": "", "collection": ""}       # nothing guessed from the current 课题 (card 0202)
    assert "导入文献" in html and "Choose files" in html and ">Add</button>" in html and ">Done</button>" in html
    assert "/static/import.css" in html and "<html" not in html
    assert picked("/papers?direction=strategy-signaling")[0]["direction"] == "strategy-signaling"
    assert picked("/projects/us-rok-reassurance")[0] == {"direction": "alliance-us", "project": "us-rok-reassurance", "collection": ""}
    assert picked(f"/papers?collection={ref}")[0]["collection"] == ref
    client.cookies.set("rl_direction", "periphery-nts")                                # the direction bar's pick
    assert picked("/papers")[0]["direction"] == "periphery-nts"


def test_a_dropped_pdf_comes_back_as_its_own_row_and_joins_no_project(web, conn, sample_pdf, tmp_path):
    r = upload(web, sample_pdf, direction="strategy-signaling", project="", collection="")
    assert r["state"] == "ok" and r["paper_id"] and "轻读概要排队中" in r["note"] and r["title"]
    pid = r["paper_id"]
    assert conn.execute("SELECT direction FROM papers WHERE id = ?", (pid,)).fetchone()[0] == "strategy-signaling"
    assert conn.execute("SELECT 1 FROM paper_projects WHERE paper_id = ?", (pid,)).fetchone() is None
    again = upload(web, sample_pdf)
    assert again["state"] == "exists" and again["paper_id"] == pid
    junk = tmp_path / "notes.pdf"
    junk.write_bytes(b"not a pdf at all")
    bad = upload(web, junk)
    assert bad["state"] == "error" and bad["note"].startswith("没有导入")
    assert web.post("/imports/files", data={"project": "no-such-project"}, files={"file": ("a.pdf", b"x", "application/pdf")}).status_code == 400


def test_a_possible_duplicate_is_answered_in_the_sheet(web, conn, sample_pdf, tmp_path):
    import pymupdf
    from research_lab import paper_collections as C
    first = upload(web, sample_pdf, project="asean-centrality")
    assert conn.execute("SELECT project_slug FROM paper_projects WHERE paper_id = ?", (first["paper_id"],)).fetchone()[0] == "asean-centrality"
    copy = tmp_path / "tan-copy.pdf"                         # same DOI, different bytes: a held possible duplicate
    doc = pymupdf.open(sample_pdf)
    doc.set_metadata({"title": "another copy"})
    doc.save(copy)
    dup = upload(web, copy)
    assert dup["state"] == "dup" and dup["pending"] and dup["duplicate_of"] == first["paper_id"] and "DOI 相同" in dup["note"]
    ref = C.create(conn, "待引用")
    conn.commit()
    same = web.post(f"/imports/pending/{dup['pending']}/same", data={"paper_id": first["paper_id"], "collection": ref}).json()
    assert same["state"] == "ok" and same["paper_id"] == first["paper_id"]
    assert conn.execute("SELECT COUNT(*) FROM paper_files WHERE paper_id = ?", (first["paper_id"],)).fetchone()[0] == 2
    assert conn.execute("SELECT 1 FROM paper_collection_items WHERE collection_id = ? AND paper_id = ?", (ref, first["paper_id"])).fetchone()
    assert web.post(f"/imports/pending/{dup['pending']}/same", data={"paper_id": first["paper_id"]}).status_code == 404
    assert web.post("/imports/pending/abcdefabcdef/maybe", data={}).status_code == 404


def test_identifiers_become_papers_or_bibliography_records(web, conn):
    biblio = web.post("/imports/identifiers", data={"id": f"https://doi.org/{DOI}", "direction": "sea-asean"}).json()
    assert biblio["state"] == "biblio" and "待补原文" in biblio["note"] and biblio["title"] == TITLE
    pid = biblio["paper_id"]
    row = conn.execute("SELECT doi, direction FROM papers WHERE id = ?", (pid,)).fetchone()
    assert row["doi"] == DOI and row["direction"] == "sea-asean"
    assert conn.execute("SELECT 1 FROM paper_files WHERE paper_id = ?", (pid,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM paper_projects WHERE paper_id = ?", (pid,)).fetchone() is None
    again = web.post("/imports/identifiers", data={"id": DOI}).json()
    assert again["state"] == "exists" and again["paper_id"] == pid and "还没有原文" in again["note"]
    unknown = web.post("/imports/identifiers", data={"id": "a book I read once"}).json()
    assert unknown["state"] == "error" and "认不出编号" in unknown["note"]
    missing = web.post("/imports/identifiers", data={"id": "10.9999/nothing.here"}).json()
    assert missing["state"] == "error" and "没找到" in missing["note"]


def test_an_arxiv_id_brings_its_pdf(web, conn):
    arxiv = web.post("/imports/identifiers", data={"id": "arXiv:2310.01234", "project": "us-rok-reassurance"}).json()
    assert arxiv["state"] == "ok" and arxiv["paper_id"] and "开放获取 PDF" in arxiv["note"]
    assert conn.execute("SELECT 1 FROM paper_files WHERE paper_id = ?", (arxiv["paper_id"],)).fetchone()
    assert conn.execute("SELECT project_slug FROM paper_projects WHERE paper_id = ?", (arxiv["paper_id"],)).fetchone()[0] == "us-rok-reassurance"
    # another id that serves the very same file is recognised, not imported twice
    twin = web.post("/imports/identifiers", data={"id": "2310.05678"}).json()
    assert twin["state"] == "exists" and twin["paper_id"] == arxiv["paper_id"]


def test_the_script_catches_every_way_in(client):
    js = client.get("/static/import.js").text
    assert "global-drop-form|dropzone" in js and 'label[for="dropfile"]' in js and "/imports/files" in js and "/imports/identifiers" in js
    assert client.get("/static/import.css").status_code == 200
    for page in ("/papers", "/", "/data"):                                   # mounted once, in base.html
        assert client.get(page).text.count("/static/import.js") == 1, page
