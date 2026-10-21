"""Card 0168: attaching a dataset and saving a section of the plan both used to read the whole plan and write it back,
so a section saved during an import could drop the new dataset link. Now every edit writes only its own keys of
plan_json under the write lock, and a section form carries that section's version: stale is 409 and writes nothing."""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest

from research_lab.db.connection import connect
from research_lab.replication import datasets, plans

from .test_data import data_client, write_cn_csv  # noqa: F401 - data_client is a fixture


def two_datasets(conn, tmp_path: Path) -> tuple[str, str]:
    csv = write_cn_csv(tmp_path / "attendance.csv", encoding="utf-8")
    return (datasets.import_file(conn, csv, original_name="a.csv"), datasets.import_file(conn, csv, original_name="b.csv"))


def test_an_import_during_a_section_save_loses_no_link(conn, tmp_path, monkeypatch):
    """The save has read the plan and is about to write when the import attaches a dataset on another connection."""
    ds, _ = two_datasets(conn, tmp_path)
    plan_id = plans.create_blank(conn)
    conn.commit()
    read, attaching = threading.Event(), threading.Event()
    real_patch = plans._patch

    def slow_patch(c, pid, values):
        if threading.current_thread().name == "save":
            read.set()                                   # the save holds what it read ...
            attaching.wait(5)
            time.sleep(0.3)                              # ... while the import tries to attach
        real_patch(c, pid, values)

    monkeypatch.setattr(plans, "_patch", slow_patch)
    errors: list[BaseException] = []

    def save():
        c = connect()
        try:
            plans.save_variables(c, plan_id, [{"role": "DV", "name": "出席率"}])
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            c.close()

    def attach():
        read.wait(5)
        c = connect()
        try:
            attaching.set()
            plans.attach_dataset(c, plan_id, ds)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            c.close()

    threads = [threading.Thread(target=save, name="save"), threading.Thread(target=attach, name="attach")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert not errors
    p = plans.get(conn, plan_id)
    assert p["plan"]["datasets"] == [ds] and [v["name"] for v in p["plan"]["variables"]] == ["出席率"]


def test_each_edit_writes_only_its_own_section(conn, tmp_path):
    ds_a, ds_b = two_datasets(conn, tmp_path)
    plan_id = plans.create_blank(conn)
    stale = plans.get(conn, plan_id)                      # a page drawn before the imports
    plans.attach_dataset(conn, plan_id, ds_a)
    plans.attach_dataset(conn, plan_id, ds_b)
    plans.attach_dataset(conn, plan_id, ds_a)             # already there: still one
    plans.save_sources(conn, plan_id, [{"name": "ATOP"}], version=plans.section_version(stale, "sources"))
    plans.save_fields(conn, plan_id, {"unit_of_analysis": "国家—年", "direction": "alliance-us"},
                      version=plans.section_version(stale, "fields"))
    plans.add_step(conn, plan_id, "下载数据")
    plans.set_step(conn, plan_id, 0, done=True)
    plans.detach_dataset(conn, plan_id, ds_a)
    p = plans.get(conn, plan_id)
    assert p["plan"]["datasets"] == [ds_b] and p["plan"]["data_sources"][0]["name"] == "ATOP"
    assert p["plan"]["unit_of_analysis"] == "国家—年" and p["direction"] == "alliance-us"
    assert p["plan"]["steps"] == [{"text": "下载数据", "done": True, "note": ""}]
    with pytest.raises(plans.PlanError):
        plans.set_step(conn, plan_id, 5, done=True)
    with pytest.raises(plans.PlanError):                  # a bad link is refused whole, nothing half-written
        plans.save_links(conn, plan_id, [{"url": "https://osf.io/abcd/"}, {"url": "file:///etc/passwd"}])
    assert plans.get(conn, plan_id)["plan"]["links"] == [] and not conn.in_transaction


def test_a_stale_section_form_gets_409_and_writes_nothing(data_client, conn, tmp_path):
    ds, _ = two_datasets(conn, tmp_path)
    plan_id = plans.create_blank(conn)
    conn.commit()
    page = data_client.get(f"/data/plans/{plan_id}/detail").text
    drawn = dict(re.findall(r'action="/data/plans/[^/"]+/(\w+)">\s*<input type="hidden" name="version" value="([0-9a-f]+)"', page))
    assert set(drawn) == {"fields", "sources", "links", "assumptions", "variables"}
    plans.attach_dataset(conn, plan_id, ds)                # an import lands while the page is open
    conn.commit()
    r = data_client.post(f"/data/plans/{plan_id}/variables", data={"version": drawn["variables"], "var_role": ["DV"],
                                                                  "var_name": ["出席率"], "var_measure": [""], "var_source": [""]},
                         follow_redirects=False)
    assert r.status_code in (204, 303)                     # another section moving on does not make this one stale
    assert plans.get(conn, plan_id)["plan"]["datasets"] == [ds]
    r = data_client.post(f"/data/plans/{plan_id}/variables", data={"version": drawn["variables"], "var_role": ["IV"],
                                                                  "var_name": ["联盟强度"], "var_measure": [""], "var_source": [""]},
                         follow_redirects=False)
    assert r.status_code == 409 and "这一部分在别处改过" in r.text
    p = plans.get(conn, plan_id)
    assert [v["name"] for v in p["plan"]["variables"]] == ["出席率"] and p["plan"]["datasets"] == [ds]
    r = data_client.post(f"/data/plans/{plan_id}/fields", data={"version": "0" * 16, "unit_of_analysis": "国家—年"},
                         follow_redirects=False)
    assert r.status_code == 409 and plans.get(conn, plan_id)["plan"]["unit_of_analysis"] == ""
    with pytest.raises(plans.StalePlan):
        plans.save_assumptions(conn, plan_id, [{"text": "平行趋势"}], version="0" * 16)
    assert plans.get(conn, plan_id)["plan"]["assumptions"] == []
