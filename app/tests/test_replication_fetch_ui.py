"""Card 0165: on the replication plan page each saved 复现数据链接 gets List files (lists, never downloads), ticks, and
Import (downloads only the ticked files, imports them and attaches each dataset to the plan). The page carries a
version of the links: a page older than the links gets 409 and nothing is listed or downloaded. fetch is stubbed."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from research_lab.replication import datasets, fetch, plans

from .test_data import data_client, write_cn_csv  # noqa: F401 - data_client is a fixture

DOI = "https://doi.org/10.7910/DVN/ABCDEF"
LISTING = fetch.Listing("dataverse", "Replication Data for: Staggered Reassurance", [
    fetch.Remote("data/panel.csv", "https://dataverse.harvard.edu/api/access/datafile/1", 1_234_567),
    fetch.Remote("data/extra.dta", "https://dataverse.harvard.edu/api/access/datafile/2", 2048),
    fetch.Remote("code/model.rds", "https://dataverse.harvard.edu/api/access/datafile/3", 512, "需要 R"),
    fetch.Remote("data/old.xls", "https://dataverse.harvard.edu/api/access/datafile/4", None, "旧版 Excel，另存为 .xlsx 后拖进来"),
])


@pytest.fixture
def plan(conn):
    plan_id = plans.create_blank(conn)
    plans.save_links(conn, plan_id, [{"url": DOI, "kind": "dataverse"}, {"url": "https://osf.io/abcd/", "kind": "osf"}])
    conn.commit()
    return plan_id


@pytest.fixture
def calls(monkeypatch):
    seen: dict[str, list] = {"list": [], "fetch": []}

    def list_source(src, http=None):
        seen["list"].append(src)
        return LISTING

    monkeypatch.setattr(fetch, "list_source", list_source)
    return seen


def version_of(html: str) -> str:
    return re.search(r"/links/0/files\?v=([0-9a-f]+)", html).group(1)


def test_list_files_shows_the_files_and_marks_the_ones_that_cannot_be_fetched(data_client, conn, plan, calls):
    page = data_client.get(f"/data/plans/{plan}/detail").text
    assert page.count(">List files</button>") == 2 and 'id="plan-fetch"' in page and "正在列出文件" in page
    v = version_of(page)
    r = data_client.get(f"/data/plans/{plan}/links/0/files?v={v}")
    assert r.status_code == 200 and calls["list"] == [DOI] and not calls["fetch"]          # listed, nothing fetched
    html = r.text
    assert "Replication Data for: Staggered Reassurance" in html and "4 个数据文件" in html
    assert 'name="pick" value="data/panel.csv" aria-label="选择 data/panel.csv">' in html and "1.2 MB" in html
    assert re.search(r'value="code/model.rds"[^>]*disabled', html) and "需要 R" in html
    assert re.search(r'value="data/old.xls"[^>]*disabled', html) and "另存为 .xlsx" in html
    assert f'name="sources_version" value="{v}"' in html and ">Import</button>" in html and "正在下载并导入" in html
    assert f'hx-post="/data/plans/{plan}/links/0/import"' in html
    data_client.get(f"/data/plans/{plan}/links/1/files?v={v}")
    assert calls["list"][-1] == "https://osf.io/abcd/"                                     # the row names the link
    assert data_client.get(f"/data/plans/{plan}/links/5/files?v={v}").status_code == 404


def test_import_attaches_the_picked_datasets_and_shows_each_result(data_client, conn, plan, calls, monkeypatch, tmp_path: Path):
    csv = write_cn_csv(tmp_path / "panel.csv", encoding="utf-8")

    def fake_fetch(conn_, src, picks, *, plan_id=None, http=None, max_bytes=0):
        calls["fetch"].append((src, list(picks), plan_id))
        ds_id = datasets.import_file(conn_, csv, original_name="panel.csv")
        plans.attach_dataset(conn_, plan_id, ds_id)
        return [fetch.Imported("data/panel.csv", dataset=ds_id),
                fetch.Imported("data/extra.dta", error="文件超过 500 MB，没有下载")]

    monkeypatch.setattr(fetch, "fetch", fake_fetch)
    v = version_of(data_client.get(f"/data/plans/{plan}/detail").text)
    r = data_client.post(f"/data/plans/{plan}/links/0/import",
                         data={"sources_version": v, "pick": ["data/panel.csv", "data/extra.dta"]})
    assert r.status_code == 200 and calls["fetch"] == [(DOI, ["data/panel.csv", "data/extra.dta"], plan)]
    ds_id = plans.get(conn, plan)["plan"]["datasets"][0]
    html = r.text
    assert "1 个已导入并关联到这份方案，1 个没导入" in html
    assert f'data/panel.csv</span> → <a href="/data/datasets/{ds_id}">{ds_id}</a>' in html
    assert "文件超过 500 MB，没有下载" in html
    oob = html[html.index('id="plan-datasets"'):]
    assert oob.startswith('id="plan-datasets" hx-swap-oob="true"') and f'href="/data/datasets/{ds_id}"' in oob   # the list refreshes
    assert f'href="/data/datasets/{ds_id}"' in data_client.get(f"/data/plans/{plan}/detail").text


def test_errors_are_shown_and_nothing_is_fetched_without_a_pick(data_client, conn, plan, monkeypatch):
    def no_listing(src, http=None):
        raise fetch.FetchError("没找到：链接可能写错了，或者数据还没公开")

    def broken_fetch(conn_, src, picks, **kw):
        raise fetch.FetchError("没有这个文件，或者名字不唯一：data/gone.csv")

    monkeypatch.setattr(fetch, "list_source", no_listing)
    monkeypatch.setattr(fetch, "fetch", broken_fetch)
    v = version_of(data_client.get(f"/data/plans/{plan}/detail").text)
    r = data_client.get(f"/data/plans/{plan}/links/0/files?v={v}")
    assert r.status_code == 200 and "没找到：链接可能写错了" in r.text and ">Import</button>" not in r.text
    r = data_client.post(f"/data/plans/{plan}/links/0/import", data={"sources_version": v})
    assert "先勾选要导入的文件" in r.text
    r = data_client.post(f"/data/plans/{plan}/links/0/import", data={"sources_version": v, "pick": ["data/gone.csv"]})
    assert "没有这个文件，或者名字不唯一：data/gone.csv" in r.text and plans.get(conn, plan)["plan"]["datasets"] == []


def test_a_page_older_than_the_links_gets_409_and_nothing_is_downloaded(data_client, conn, plan, calls, monkeypatch):
    def must_not_run(*a, **kw):
        raise AssertionError("fetch ran on a stale page")

    monkeypatch.setattr(fetch, "fetch", must_not_run)
    v = version_of(data_client.get(f"/data/plans/{plan}/detail").text)
    plans.save_links(conn, plan, [{"url": "https://github.com/kpark/reassurance", "kind": "github"}, {"url": DOI}])
    conn.commit()                                                    # row 0 is a different link now
    r = data_client.post(f"/data/plans/{plan}/links/0/import", data={"sources_version": v, "pick": ["data/panel.csv"]})
    assert r.status_code == 409 and "已经过期" in r.text and ">Reload</a>" in r.text
    r = data_client.get(f"/data/plans/{plan}/links/0/files?v={v}")
    assert r.status_code == 409 and not calls["list"]
    assert plans.get(conn, plan)["plan"]["datasets"] == []
    page = data_client.get(f"/data/plans/{plan}/detail").text                # a fresh page lists again
    assert data_client.get(f"/data/plans/{plan}/links/0/files?v={version_of(page)}").status_code == 200
    assert calls["list"] == ["https://github.com/kpark/reassurance"]
