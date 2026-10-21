"""Fetching public replication data (card 0159): Dataverse, OSF, GitHub and direct links are listed without downloading,
only picked files are fetched and imported, and the size cap, zip-slip and non-public hosts are refused. HTTP is stubbed."""
from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest

from research_lab.replication import fetch

CSV = b"country,year,treated,y\nA,2010,0,1.5\nA,2011,1,2.5\nB,2010,0,0.5\nB,2011,0,0.7\n"


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


DATAVERSE = {"status": "OK", "data": {"latestVersion": {
    "metadataBlocks": {"citation": {"fields": [{"typeName": "title", "value": "Replication Data for: Rebels and States"}]}},
    "files": [
        {"label": "panel.tab", "directoryLabel": "data", "dataFile": {"id": 101, "filename": "panel.tab", "filesize": 900,
                                                                       "originalFileName": "panel.dta", "originalFileSize": 4096}},
        {"label": "codebook.pdf", "dataFile": {"id": 102, "filename": "codebook.pdf", "filesize": 50000}},
        {"label": "models.rds", "dataFile": {"id": 103, "filename": "models.rds", "filesize": 700}},
        {"label": "raw.csv", "dataFile": {"id": 104, "filename": "raw.csv", "filesize": len(CSV)}},
    ]}}}


def _handler(calls: list[str], extra=None):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if extra:
            hit = extra(request)
            if hit is not None:
                return hit
        url, host, path = str(request.url), request.url.host, request.url.path
        if host == "dataverse.harvard.edu" and path == "/api/datasets/:persistentId/":
            assert request.url.params["persistentId"] == "doi:10.7910/DVN/ABC123"
            return httpx.Response(200, json=DATAVERSE)
        if host == "dataverse.harvard.edu" and path == "/api/access/datafile/104":
            return httpx.Response(200, content=CSV, headers={"content-type": "text/csv"})
        if host == "api.osf.io" and path == "/v2/nodes/ab3cd/":
            return httpx.Response(200, json={"data": {"attributes": {"title": "OSF replication"}}})
        if host == "api.osf.io" and path == "/v2/nodes/ab3cd/files/osfstorage/":
            return httpx.Response(200, json={"data": [
                {"attributes": {"kind": "file", "name": "survey.csv", "size": len(CSV), "materialized_path": "/survey.csv"},
                 "links": {"download": "https://osf.io/download/xyz/"}},
                {"attributes": {"kind": "file", "name": "paper.pdf", "size": 10}, "links": {"download": "https://osf.io/download/pdf/"}},
                {"attributes": {"kind": "folder", "name": "more"},
                 "relationships": {"files": {"links": {"related": {"href": "https://api.osf.io/v2/nodes/ab3cd/files/osfstorage/more/"}}}}},
            ], "links": {"next": None}})
        if host == "api.osf.io" and path == "/v2/nodes/ab3cd/files/osfstorage/more/":
            return httpx.Response(200, json={"data": [
                {"attributes": {"kind": "file", "name": "wave2.json", "size": 40, "materialized_path": "/more/wave2.json"},
                 "links": {"download": "https://osf.io/download/w2/"}}], "links": {}})
        if url == "https://osf.io/download/w2/":
            return httpx.Response(200, json=[{"id": 1, "score": 0.5}, {"id": 2, "score": 0.9}])
        if host == "api.github.com" and path == "/repos/lab/rep/contents/":
            return httpx.Response(200, json=[
                {"type": "file", "name": "README.md", "path": "README.md", "size": 5, "download_url": "https://raw.githubusercontent.com/lab/rep/main/README.md"},
                {"type": "dir", "name": "data", "path": "data"}])
        if host == "api.github.com" and path == "/repos/lab/rep/contents/data":
            return httpx.Response(200, json=[
                {"type": "file", "name": "panel.csv", "path": "data/panel.csv", "size": len(CSV),
                 "download_url": "https://raw.githubusercontent.com/lab/rep/main/data/panel.csv"},
                {"type": "file", "name": "bundle.zip", "path": "data/bundle.zip", "size": 300,
                 "download_url": "https://raw.githubusercontent.com/lab/rep/main/data/bundle.zip"}])
        if url.endswith("/data/panel.csv"):
            return httpx.Response(200, content=CSV)
        if url.endswith("/data/bundle.zip"):
            return httpx.Response(200, content=_zip({"inner/a.csv": CSV, "inner/notes.txt": b"x", "b.csv": CSV}))
        if host == "files.example.org":
            return httpx.Response(200, content=CSV)
        return httpx.Response(404, json={})
    return handler


@pytest.fixture()
def web():
    calls: list[str] = []

    def make(extra=None):
        return httpx.Client(transport=httpx.MockTransport(_handler(calls, extra)), follow_redirects=True), calls
    return make


@pytest.mark.parametrize("src", ["10.7910/DVN/ABC123", "doi:10.7910/DVN/ABC123", "https://doi.org/10.7910/DVN/ABC123",
                                 "https://dataverse.harvard.edu/dataset.xhtml?persistentId=doi:10.7910/DVN/ABC123"])
def test_dataverse_lists_data_files_with_originals_and_sizes(web, src):
    http, calls = web()
    listing = fetch.list_source(src, http)
    assert listing.source == "dataverse" and listing.title == "Replication Data for: Rebels and States"
    assert [(f.name, f.size, f.note) for f in listing.files] == [("data/panel.dta", 4096, ""), ("models.rds", 700, "需要 R"),
                                                                  ("raw.csv", len(CSV), "")]
    assert listing.files[0].url == "https://dataverse.harvard.edu/api/access/datafile/101?format=original"
    assert not any("datafile" in c for c in calls)                                       # listing downloads nothing


def test_osf_lists_through_folders_and_github_lists_data_only(web):
    http, _ = web()
    osf = fetch.list_source("https://osf.io/ab3cd/", http)
    assert osf.title == "OSF replication" and [f.name for f in osf.files] == ["survey.csv", "more/wave2.json"]
    gh = fetch.list_source("https://github.com/lab/rep", http)
    assert gh.source == "github" and [f.name for f in gh.files] == ["data/panel.csv", "data/bundle.zip"]
    direct = fetch.list_source("https://files.example.org/replication/data.csv", http)
    assert direct.source == "direct" and [f.name for f in direct.files] == ["data.csv"]
    with pytest.raises(fetch.FetchError, match="认不出"):
        fetch.list_source("https://example.org/paper.html", http)


def test_picked_files_are_downloaded_imported_tagged_and_attached(web, conn):
    from research_lab.replication import datasets, plans
    plan = plans.create_blank(conn, direction="sea-asean")
    http, calls = web()
    out = fetch.fetch(conn, "10.7910/DVN/ABC123", ["raw.csv"], plan_id=plan, http=http)
    assert [r.error for r in out] == [None]
    ds = datasets.get(conn, out[0].dataset)
    assert ds["rows"] == 4 and ds["cols"] == 4 and ds["direction"] == "sea-asean" and "10.7910/DVN/ABC123" in ds["note"]
    assert out[0].dataset in plans.get(conn, plan)["plan"]["datasets"]
    assert sum("datafile/104" in c for c in calls) == 1 and not any("datafile/101" in c for c in calls)   # only the pick
    with pytest.raises(fetch.FetchError, match="需要 R"):
        fetch.fetch(conn, "10.7910/DVN/ABC123", ["models.rds"], http=http)
    with pytest.raises(fetch.FetchError, match="没有这个文件"):
        fetch.fetch(conn, "10.7910/DVN/ABC123", ["nope.csv"], http=http)
    with pytest.raises(fetch.FetchError, match="没有这份复现方案"):
        fetch.fetch(conn, "10.7910/DVN/ABC123", ["raw.csv"], plan_id="RP-2099-0001", http=http)


def test_a_zip_is_unpacked_and_json_becomes_a_table(web, conn):
    from research_lab.replication import datasets
    http, _ = web()
    zipped = fetch.fetch(conn, "https://github.com/lab/rep", ["2"], http=http)       # by number: data/bundle.zip
    assert sorted(r.name for r in zipped) == ["data/bundle.zip › a.csv", "data/bundle.zip › b.csv"]
    assert all(r.dataset for r in zipped)
    js = fetch.fetch(conn, "https://osf.io/ab3cd", ["wave2.json"], http=http)
    assert js[0].dataset and datasets.get(conn, js[0].dataset)["rows"] == 2


def test_the_size_cap_refuses_big_files(web, conn):
    http, _ = web()
    small = 64
    listed = fetch.fetch(conn, "https://github.com/lab/rep", ["data/panel.csv"], http=http, max_bytes=small)
    assert listed[0].dataset is None and "超过上限" in listed[0].error                    # the listing said how big
    # a server that does not say: the stream is cut off at the cap
    http2, _ = web(lambda r: httpx.Response(200, content=b"x" * 5000) if r.url.host == "files.example.org" else None)
    cut = fetch.fetch(conn, "https://files.example.org/big.csv", ["big.csv"], http=http2, max_bytes=1000)
    assert cut[0].dataset is None and "超过上限" in cut[0].error


@pytest.mark.parametrize("member", ["../evil.csv", "/etc/evil.csv", "data/../../evil.csv", "C:/evil.csv"])
def test_a_zip_slip_is_refused(web, conn, member, tmp_path):
    bad = _zip({member: CSV, "ok.csv": CSV})
    http, _ = web(lambda r: httpx.Response(200, content=bad) if r.url.path.endswith("/evil.zip") else None)
    out = fetch.fetch(conn, "https://files.example.org/evil.zip", ["evil.zip"], http=http)
    assert out[0].dataset is None and "越界路径" in out[0].error
    assert not (tmp_path.parent / "evil.csv").exists()
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("huge.csv", b"0" * 200_000)
    http2, _ = web(lambda r: httpx.Response(200, content=bomb.getvalue()) if r.url.path.endswith("/bomb.zip") else None)
    out = fetch.fetch(conn, "https://files.example.org/bomb.zip", ["bomb.zip"], http=http2, max_bytes=50_000)
    assert out[0].dataset is None and "超过上限" in out[0].error


@pytest.mark.parametrize("src", ["http://127.0.0.1/data.csv", "http://10.0.0.8/data.csv", "http://localhost/data.csv",
                                 "https://[::1]/data.csv"])
def test_a_non_public_host_is_refused(web, src, conn):
    http, calls = web()
    with pytest.raises(fetch.FetchError):
        fetch.fetch(conn, src, ["data.csv"], http=http)
    assert calls == []


def test_a_redirect_to_a_private_address_is_refused(web, conn):
    http, calls = web(lambda r: httpx.Response(302, headers={"location": "http://192.168.1.5/secret.csv"})
                      if r.url.host == "files.example.org" else None)
    out = fetch.fetch(conn, "https://files.example.org/data.csv", ["data.csv"], http=http)
    assert out[0].dataset is None and "下载失败" in out[0].error and not any("192.168" in c for c in calls)


def test_the_fetch_command_lists_then_picks(monkeypatch):
    from typer.testing import CliRunner
    from research_lab.cli import data as cli
    listing = fetch.Listing("dataverse", "Rebels", [fetch.Remote("data/panel.dta", "https://x/1", 4 * 1024 * 1024),
                                                   fetch.Remote("models.rds", "https://x/2", None, "需要 R")])
    monkeypatch.setattr(fetch, "list_source", lambda src, http=None: listing)
    out = CliRunner().invoke(cli.app, ["fetch", "10.7910/DVN/ABC123"])
    assert out.exit_code == 0 and "1. data/panel.dta  4.0 MB" in out.output and "[需要 R]" in out.output and "--pick" in out.output
    seen = {}

    def fake(conn, src, picks, plan_id=None, max_bytes=0):
        seen.update(src=src, picks=picks, plan=plan_id, max_bytes=max_bytes)
        return [fetch.Imported("data/panel.dta", "DS-2026-0009"), fetch.Imported("x.csv", error="坏了")]
    monkeypatch.setattr(fetch, "fetch", fake)
    out = CliRunner().invoke(cli.app, ["fetch", "10.7910/DVN/ABC123", "--pick", "1", "--pick", "x.csv", "--plan", "RP-2026-0001",
                                       "--max-mb", "50"])
    assert out.exit_code == 0 and seen == {"src": "10.7910/DVN/ABC123", "picks": ["1", "x.csv"], "plan": "RP-2026-0001",
                                           "max_bytes": 50 * 1024 * 1024}
    assert "DS-2026-0009  data/panel.dta" in out.output and "失败  x.csv: 坏了" in out.output and "已挂到方案 RP-2026-0001" in out.output


@pytest.mark.parametrize("name", ["/__private__tmp__/x.csv", "/etc/x.csv", "../../x.csv", "a/./../b.csv", "a\x00b.csv", "..", "/"])
def test_a_source_chosen_file_name_never_lands_outside_the_work_folder(tmp_path, name):
    target = fetch._inside(tmp_path, name)
    assert target.parent == tmp_path and "\x00" not in target.name and target.name not in ("", ".", "..")


def test_a_broken_or_locked_zip_member_fails_that_file_only(tmp_path, monkeypatch):
    archive = tmp_path / "data.zip"
    archive.write_bytes(_zip({"a.csv": b"x,y\n1,2\n"}))
    out = tmp_path / "out"
    out.mkdir()
    real_open = zipfile.ZipFile.open

    def locked(self, member, *a, **k):
        raise RuntimeError("File is encrypted, password required for extraction")
    monkeypatch.setattr(zipfile.ZipFile, "open", locked)
    with pytest.raises(fetch.FetchError, match="加了密码"):
        fetch.unzip(archive, out, 10_000)
    monkeypatch.setattr(zipfile.ZipFile, "open", real_open)
    (tmp_path / "bad.zip").write_bytes(b"PK\x03\x04 not really a zip")
    with pytest.raises(fetch.FetchError, match="坏了"):
        fetch.unzip(tmp_path / "bad.zip", out, 10_000)


def test_the_size_cap_still_reads_as_a_size_error_inside_a_zip(tmp_path):
    archive = tmp_path / "big.zip"
    archive.write_bytes(_zip({"a.csv": b"x" * 5000}))
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(fetch.FetchError, match="上限"):
        fetch.unzip(archive, out, 1000)


def test_corrupt_deflate_data_fails_that_file_only(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("a.csv", b"x,y\n" + b"1,2\n" * 2000)
    raw = bytearray(buf.getvalue())
    start = raw.index(b"a.csv") + len(b"a.csv")           # the local header's name, then the compressed bytes
    for i in range(start + 10, start + 60):
        raw[i] ^= 0xFF
    archive = tmp_path / "broken.zip"
    archive.write_bytes(bytes(raw))
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(fetch.FetchError):
        fetch.unzip(archive, out, 1_000_000)
