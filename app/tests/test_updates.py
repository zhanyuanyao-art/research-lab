"""记录 (_data/updates.yml) and the archive action behind the issue view, with the system lines written
when maturity, version or archive state change (docs/design/ARCHIVE-ISSUES.md §B, §H)."""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from research_lab.files import updates as U


def test_updates_file_round_trip(repo):
    assert U.read() == [] and U.for_target("ART-2026-4001") == []
    a, _ = U.add("ART-2026-4001", "user", "读完 Goh 第三章，\n中心性的定义要改", now=datetime(2026, 10, 1, 9, 5))
    b, sha = U.add("ART-2026-4001", "assistant", "建议补一个反例", now=datetime(2026, 10, 1, 10, 0))
    U.add("ART-2026-4002", "system", "完成待办「一节」", now=datetime(2026, 10, 2, 8, 0))
    assert (a, b) == ("U-2026-0001", "U-2026-0002")
    rows = U.for_target("ART-2026-4001")
    # Leo's line is a Markdown comment now and keeps its line break (card 0097); the others stay one sentence
    assert [r["id"] for r in rows] == [b, a] and rows[1]["text"] == "读完 Goh 第三章，\n中心性的定义要改"
    assert rows[1]["at"] == "2026-10-01 09:05" and "at: '2026-10-01 09:05'" in U.path().read_text(encoding="utf-8")
    assert U.latest_at()["ART-2026-4001"] == "2026-10-01 10:00"
    with pytest.raises(U.UpdateError, match="写一句"):
        U.add("ART-2026-4001", "user", "   ")
    with pytest.raises(U.UpdateError, match="一份产出"):
        U.add("projects/x", "user", "x")
    with pytest.raises(U.UpdateError, match="一份产出"):
        U.add("T-2026-0001", "user", "x")                                  # tasks keep their own note
    with pytest.raises(U.UpdateError, match="作者"):
        U.add("ART-2026-4001", "leo", "x")
    with pytest.raises(U.UpdateError, match="不要超过"):
        U.add("ART-2026-4001", "user", "长" * 4001)                         # as long as an issue comment
    from research_lab.files.yamlio import StaleFileError
    with pytest.raises(StaleFileError):
        U.add("ART-2026-4001", "user", "x", expected_sha=sha)


def _first_output(client):
    from research_lab.db.connection import connect
    from research_lab.files import artifacts
    c = connect()
    artifacts.index(c)
    row = c.execute("SELECT ref, path FROM artifacts WHERE status IN ('draft', 'active') ORDER BY ref LIMIT 1").fetchone()
    c.close()
    return row["ref"], row["path"]


def _sha(client, ref):
    page = client.get(f"/artifacts/{ref}?tab=edit").text
    return re.search(r'name="sha" value="([0-9a-f]{64})"', page).group(1)


def test_archive_writes_an_existing_status_and_system_lines(client, repo):
    """ARCHIVE-ISSUES.md §H.2: outputs have no due date; 归档 writes superseded (absorbed-into-paper once
    paper-ready), keeps the old status for 恢复, leaves maturity alone and notes both in the 记录."""
    ref, path = _first_output(client)
    text = (repo / path).read_text(encoding="utf-8")
    status, level = (re.search(rf"^{k}: *(\S+)", text, re.M).group(1) for k in ("status", "level"))
    r = client.post(f"/artifacts/{ref}/meta", data={"sha": _sha(client, ref), "due": "2030-11-15"}, follow_redirects=False)
    assert r.status_code == 303 and "due:" not in (repo / path).read_text(encoding="utf-8")   # not a field any more

    r = client.post(f"/artifacts/{ref}/archive", data={"sha": _sha(client, ref)}, follow_redirects=False)
    assert r.status_code == 303 and "saved=archived" in r.headers["location"]
    text = (repo / path).read_text(encoding="utf-8")
    want = "absorbed-into-paper" if level == "paper-ready" else "superseded"
    assert f"status: {want}" in text and f"archived_from: {status}" in text and f"level: {level}" in text
    assert U.for_target(ref)[0]["text"].startswith("已归档 · ") and U.for_target(ref)[0]["by"] == "system"
    client.post(f"/artifacts/{ref}/archive", data={"sha": _sha(client, ref)}, follow_redirects=False)   # twice: no-op
    assert len([u for u in U.for_target(ref) if u["text"].startswith("已归档")]) == 1
    client.post(f"/artifacts/{ref}/archive", data={"sha": _sha(client, ref), "action": "restore"}, follow_redirects=False)
    text = (repo / path).read_text(encoding="utf-8")
    assert re.search(r"^status: *(\S+)", text, re.M).group(1) == status and "archived_from" not in text
    assert U.for_target(ref)[0]["text"] == "从归档恢复"

    r = client.post(f"/artifacts/{ref}/updates", data={"text": "和导师谈过，先收窄到东盟"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("#updates")
    assert U.for_target(ref)[0]["by"] == "user" and U.for_target(ref)[0]["text"] == "和导师谈过，先收窄到东盟"
    assert client.post(f"/artifacts/{ref}/updates", data={"text": ""}, follow_redirects=False).status_code == 400
    assert client.post("/artifacts/ART-2099-9999/updates", data={"text": "x"}, follow_redirects=False).status_code == 404


def test_task_source_names_the_output():
    from research_lab import i18n
    assert i18n.task_source("ART-2026-4001", "ASEAN 中心性") == "来自产出《ASEAN 中心性》"
    assert i18n.task_source("ART-2026-4001") == "来自产出" and i18n.due_zh("2026-11-05") == "11 月 5 日"


def test_leo_edits_and_deletes_only_his_own_lines(repo):
    a, _ = U.add("ART-2026-4001", "user", "初稿", now=datetime(2026, 10, 1, 9, 5))
    b, _ = U.add("ART-2026-4001", "system", "成熟度：初稿 → 成形", now=datetime(2026, 10, 1, 9, 6))
    U.edit(a, "改过的\n**第二行**", now=datetime(2026, 10, 1, 9, 30))
    row = next(r for r in U.read() if r["id"] == a)
    assert row["text"] == "改过的\n**第二行**" and row["edited"] == "2026-10-01 09:30"
    with pytest.raises(U.UpdateError, match="自己的"):
        U.edit(b, "篡改记录")
    with pytest.raises(U.UpdateError, match="自己的"):
        U.delete(b)
    U.delete(a)
    assert [r["id"] for r in U.read()] == [b]
    with pytest.raises(U.UpdateError, match="找不到"):
        U.delete(a)


def test_the_output_page_refuses_a_stale_edit_or_delete(client, repo):
    """The page names the updates.yml it was drawn from (card 0112); a change made since is refused with 409."""
    from research_lab.files import artifacts as A
    ref = next(A.load(p).ref for p in A.list_paths())
    client.post(f"/artifacts/{ref}/updates", data={"text": "原话"})
    page = client.get(f"/artifacts/{ref}").text
    edit = page.split('data-comment-edit="', 1)[1].split('"', 1)[0]
    delete = page.split('data-comment-delete="', 1)[1].split('"', 1)[0]
    assert f"?sha={U.file_sha()}" in edit and f"?sha={U.file_sha()}" in delete
    U.add(ref, "system", "成熟度：初稿 → 成形")                                  # someone else writes in between
    hx = {"HX-Request": "true", "Accept": "application/json"}
    stale = client.post(edit.replace("&amp;", "&"), data={"text": "改过的"}, headers=hx)
    assert stale.status_code == 409 and "请刷新" in stale.json()["error"]
    assert client.post(delete, headers=hx).status_code == 409
    assert any(u["text"] == "原话" for u in U.read())
    fresh = client.get(f"/artifacts/{ref}").text.split('data-comment-edit="', 1)[1].split('"', 1)[0]
    ok = client.post(fresh, data={"text": "改过的"}, headers=hx, follow_redirects=False)
    assert ok.status_code == 303 and any(u["text"] == "改过的" for u in U.read())
