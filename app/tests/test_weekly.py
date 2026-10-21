"""Weekly module: This week, basket, draft job, Evidence insert, stale saves, Artifacts pages,
front-matter edits, harvests, harvest review, Now, and the explicit commit button."""
from __future__ import annotations

import difflib
import json
import subprocess
import time as system_time
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from research_lab import weekly
from research_lab.ai import queue
from research_lab.files import artifacts as A
from research_lab.files import harvests as H
from research_lab.files.weeks import week_of, week_start

LANDSCAPE = "_artifacts/2026-W40-research-landscape.md"


def midweek_noon(week: str) -> str:
    """Keep general weekly fixtures away from boundaries; dedicated tests cover those."""
    noon = datetime.combine(week_start(week) + timedelta(days=2), time(12)).astimezone()
    return noon.astimezone(timezone.utc).isoformat()


def seed_excerpts(conn, week: str | None = None):
    ts = midweek_noon(week or week_of())
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, created_at, updated_at) "
                 "VALUES ('tan2023-convenes', 'Who convenes Asia?', 'h1', '[\"Tan, L.\"]', 2023, ?, ?)", (ts, ts))
    rows = [
        ("EXC-2026-0001", "11", "US leader-level attendance at the EAS lapsed in 5 of 17 summits.", "chair rule never questioned", ts),
        ("EXC-2026-0002", "4", "Convening power is a residual of mutual veto.", "definition", ts),
        ("EXC-2025-0009", "17", "An old excerpt from last year.", "old", "2025-03-01T10:00:00+00:00"),
    ]
    for eid, loc, quote, note, created in rows:
        conn.execute("INSERT INTO excerpts(id, paper_id, locator, kind, quote, my_note, created_at, updated_at) "
                     "VALUES (?, 'tan2023-convenes', ?, 'quote', ?, ?, ?, ?)", (eid, loc, quote, note, created, created))


@pytest.mark.skipif(not hasattr(system_time, "tzset"), reason="requires a controllable local timezone")
@pytest.mark.parametrize("zone,week,start,last,before,end", [
    ("Asia/Shanghai", "2026-W42", "2026-10-11T16:00:00+00:00", "2026-10-18T15:59:59+00:00",
     "2026-10-11T15:59:59+00:00", "2026-10-18T16:00:00+00:00"),
    ("America/New_York", "2026-W44", "2026-10-26T04:00:00+00:00", "2026-11-02T04:59:59+00:00",
     "2026-10-26T03:59:59+00:00", "2026-11-02T05:00:00+00:00"),
])
def test_week_excerpts_use_local_week_boundaries(conn, monkeypatch, zone, week, start, last, before, end):
    seed_excerpts(conn)
    conn.executemany("UPDATE excerpts SET created_at=? WHERE id=?",
                     [(start, "EXC-2026-0001"), (last, "EXC-2026-0002"), (before, "EXC-2025-0009")])
    conn.execute("INSERT INTO excerpts(id,paper_id,locator,kind,quote,my_note,created_at,updated_at) "
                 "SELECT 'EXC-2026-0003',paper_id,locator,kind,quote,my_note,?,? FROM excerpts WHERE id='EXC-2026-0001'",
                 (end, end))
    try:
        with monkeypatch.context() as local:
            local.setenv("TZ", zone)
            system_time.tzset()
            assert [r["id"] for r in weekly.week_excerpts(conn, week)] == ["EXC-2026-0001", "EXC-2026-0002"]
    finally:
        system_time.tzset()


def clear_artifacts(repo: Path):
    for f in (repo / "_artifacts").glob("*.md"):
        f.unlink()


def make_counting(path: Path):
    a = A.load(path)
    a.body = a.body.replace(
        "## What did I figure out that I didn't know last week?\n\n<!-- One sentence. If you cannot write this line, the artifact is not done. -->\n\n...",
        "## What did I figure out that I didn't know last week?\n\nCentrality is about a rule, not attendance.")
    a.body = a.body.replace("## Claim\n\n**I currently think:**\n\n> ...", "## Claim\n\n**I currently think:**\n\n> The chair rule survives falling attendance.")
    A.save(a)
    assert A.counts(a.body)


def _plain_data(d):
    return json.loads(json.dumps(d, default=str))


def _plain(text):
    from research_lab.files.yamlio import yaml_rt
    return _plain_data(yaml_rt().load(text))


def complete(conn, job_id, result):
    return queue.complete(conn, job_id, result, provider="test", model="test")


# ------------------------------------------------------------------ This week

def test_week_without_artifact_shows_seed_form(repo, client):
    clear_artifacts(repo)
    r = client.get("/week")
    assert r.status_code == 200
    assert "Create seed" in r.text and 'name="slug"' in r.text and 'action="/artifacts"' in r.text
    assert "谜题" in r.text  # type select comes from _data/types.yml (label via i18n)


def test_create_seed_via_post(repo, client):
    clear_artifacts(repo)
    r = client.post("/artifacts", data={"type": "puzzle", "slug": "chair-rule", "title": "Puzzle: the chair rule",
                                         "level": "seed", "project": "asean-centrality", "next": "/week"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/week"
    week = week_of()
    path = repo / "_artifacts" / f"{week}-chair-rule.md"
    a = A.load(path)
    y, w = week.split("-W")
    assert a.ref == f"ART-{y}-{w}01" and a.get("week") == week and a.get("type") == "puzzle"
    page = client.get("/week")
    assert "Puzzle: the chair rule" in page.text and 'name="text"' in page.text and a.sha in page.text
    bad = client.post("/artifacts", data={"type": "nope", "slug": "x", "title": "x", "level": "seed", "project": "asean-centrality"})
    assert bad.status_code == 400


def test_basket_lists_this_weeks_excerpts(repo, conn, client, monkeypatch):
    from research_lab.web.routes import week as week_route
    week = week_of()
    monkeypatch.setattr(week_route, "week_of", lambda d=None: week if d is None else week_of(d))   # /week shows it too
    seed_excerpts(conn, week)
    conn.execute("INSERT INTO claims(project_slug, kind, statement, status, source_excerpts_json, created_at) "
                 "VALUES ('asean-centrality', 'claim', 'Centrality is a rule.', 'accepted', '[\"EXC-2026-0001\"]', ?)",
                 (midweek_noon(week),))
    groups = weekly.basket(conn, week)
    ids = [e["id"] for g in groups for e in g["excerpts"]]
    assert ids == ["EXC-2026-0001", "EXC-2026-0002"]
    assert groups[0]["claim"]["statement"] == "Centrality is a rule." and groups[1]["claim"] is None
    assert groups[0]["excerpts"][0]["citation"] == "Tan (2023, p. 11)"
    # a marker in the file pulls an older excerpt into the basket
    marked = weekly.basket(conn, week, "x\n- old <!-- rl:EXC-2025-0009 -->\n")
    assert "EXC-2025-0009" in [e["id"] for g in marked for e in g["excerpts"]]
    r = client.get("/week")
    assert r.status_code == 200
    assert "EXC-2026-0001" in r.text and "Centrality is a rule." in r.text and "未归组" in r.text
    assert "EXC-2025-0009" not in r.text


def _landscape_sections(repo):
    a = A.load(repo / LANDSCAPE)
    return a, {k: A.section_body(a.body, k) for k in ("What did I figure out", "Claim", "Evidence")}


def test_draft_job_writes_ai_notes_and_never_claim(repo, conn, client):
    seed_excerpts(conn)
    other = A.create("puzzle", "chair-rule", "asean-centrality", week="2026-W41")
    other_ref = A.load(other).ref
    before, secs_before = _landscape_sections(repo)
    r = client.post("/week/draft", data={"ref": "ART-2026-4001", "excerpt_ids": ["EXC-2026-0001", "EXC-2026-0002"]})
    assert r.status_code == 200 and "artifact.draft" in r.text and 'hx-trigger="every 2s"' in r.text
    job = conn.execute("SELECT * FROM ai_jobs WHERE kind = 'artifact.draft'").fetchone()
    assert job["target_type"] == "artifact" and job["target_id"] == "ART-2026-4001"
    params = json.loads(job["request_json"])["params"]
    assert params["excerpt_ids"] == ["EXC-2026-0001", "EXC-2026-0002"]
    assert "US leader-level attendance" in json.loads(job["request_json"])["messages"][1]["content"]

    complete(conn, job["id"], {
        "evidence_bullets": [
            {"excerpt_id": "EXC-2026-0001", "text": "Tan (2023, p. 11): US attendance lapsed in 5 of 17 EAS summits [EXC-2026-0001]"},
            {"excerpt_id": "EXC-2026-0077", "text": "Invented [EXC-2026-0077]"},
            {"excerpt_id": "EXC-2026-0002", "text": "Mixes in an unselected one [EXC-2026-0077]"},
        ],
        "what_i_figured_out_candidates": ["Attendance can fall without the rule being questioned."],
        "outline": ["Pattern: attendance drops", "Rule persists"],
        "questions": ["Has a non-ASEAN chair ever been proposed?"],
        "related_artifacts": [other_ref, "ART-2026-9999", "ART-2026-4001"],
    })
    after, secs_after = _landscape_sections(repo)
    assert "## AI notes" in after.body and "Pattern: attendance drops" in after.body
    assert "Has a non-ASEAN chair ever been proposed?" in after.body
    assert secs_after["What did I figure out"] == secs_before["What did I figure out"]
    assert secs_after["Claim"] == secs_before["Claim"]
    assert secs_after["Evidence"] == secs_before["Evidence"]
    assert "EXC-2026-0001" not in after.body.split("## AI notes")[0]
    assert not A.counts(after.body)

    done = queue.get(conn, job["id"])
    kept = json.loads(done["result_json"])
    assert [b["excerpt_id"] for b in kept["evidence_bullets"]] == ["EXC-2026-0001"]
    assoc = conn.execute("SELECT * FROM associations").fetchall()
    assert [(a["src_id"], a["dst_id"], a["origin"], a["status"]) for a in assoc] == [
        ("ART-2026-4001", other_ref, "ai-suggested", "proposed")]

    panel = client.get(f"/week/draft/{job['id']}")
    assert panel.status_code == 200
    assert "Insert into Evidence" in panel.text and "Claim 始终由你来写" in panel.text
    assert "every 2s" not in panel.text


def test_insert_selected_appends_marker_lines(repo, conn, client):
    seed_excerpts(conn)
    client.post("/week/draft", data={"ref": "ART-2026-4001", "excerpt_ids": ["EXC-2026-0001"]})
    job_id = conn.execute("SELECT id FROM ai_jobs WHERE kind = 'artifact.draft'").fetchone()[0]
    complete(conn, job_id, {"evidence_bullets": [
        {"excerpt_id": "EXC-2026-0001", "text": "Tan (2023, p. 11): attendance lapsed in 5 of 17 summits [EXC-2026-0001]"}]})
    old_sha = A.load(repo / LANDSCAPE).sha
    stale = client.post("/week/insert", data={"job_id": job_id, "sha": "0" * 64, "bullets": ["0"]})
    assert stale.status_code == 409 and A.load(repo / LANDSCAPE).sha == old_sha

    r = client.post("/week/insert", data={"job_id": job_id, "sha": old_sha, "bullets": ["0"]})
    assert r.status_code == 204 and r.headers["HX-Redirect"].startswith("/week?inserted=1")
    a = A.load(repo / LANDSCAPE)
    evidence = A.section_body(a.body, "Evidence")
    assert "<!-- rl:begin excerpts -->" in evidence
    assert "- Tan (2023, p. 11): attendance lapsed in 5 of 17 summits [EXC-2026-0001] <!-- rl:EXC-2026-0001 -->" in evidence
    assert "EXC-2026-0001" not in (A.section_body(a.body, "Claim") or "")
    assert conn.execute("SELECT status FROM excerpts WHERE id = 'EXC-2026-0001'").fetchone()[0] == "used"
    # the inserted excerpt now shows as "in file" in the basket
    assert any(e["in_file"] for g in weekly.basket(conn, week_of(), a.body) for e in g["excerpts"])


def test_stale_save_returns_diff_and_keeps_file(repo, conn, client):
    path = repo / LANDSCAPE
    loaded = A.load(path)
    my_edit = loaded.text().replace("## What seems insufficient\n\n...", "## What seems insufficient\n\nMy edit.")
    path.write_text(path.read_text() + "\nEdited elsewhere.\n", encoding="utf-8")
    on_disk = path.read_text()
    r = client.post("/week/save", data={"ref": "ART-2026-4001", "sha": loaded.sha, "text": my_edit})
    assert r.status_code == 409
    assert "@@" in r.text and "磁盘上的当前版本" in r.text and "你的编辑" in r.text and "My edit." in r.text
    assert path.read_text() == on_disk

    ok = client.post("/week/save", data={"ref": "ART-2026-4001", "sha": A.load(path).sha, "text": my_edit},
                     follow_redirects=False)
    assert ok.status_code == 303 and "My edit." in path.read_text()


def test_week_now_fields_are_sha_guarded(repo, client):
    from research_lab.files import datafiles
    sha = datafiles.file_sha("now")
    before = (repo / "_data/now.yml").read_text()
    r = client.post("/week/now", data={"sha": sha, "this_week": "Write the chair-rule puzzle",
                                        "next_week": "Literature Map v0.1 for ASEAN centrality",
                                        "puzzle": "ASEAN centrality survives despite wide variation in individual members' commitment."})
    assert r.status_code == 200 and "已保存到 _data/now.yml。" in r.text
    after = (repo / "_data/now.yml").read_text()
    old, new = _plain(before), datafiles.read("now")
    assert _plain_data(new) == {**old, "this_week": "Write the chair-rule puzzle"}
    stale = client.post("/week/now", data={"sha": sha, "this_week": "Other"})
    assert stale.status_code == 409 and (repo / "_data/now.yml").read_text() == after


# ------------------------------------------------------------------ Artifacts

def test_artifact_pages(repo, client):
    r = client.get("/artifacts")
    assert r.status_code == 200
    assert 'href="/issues?direction=sea-asean"' in r.text and 'href="/artifacts/new"' in r.text  # directions open the issue workspace
    assert "Research Landscape" in client.get("/artifacts/d/sea-asean").text
    d = client.get("/artifacts/ART-2026-4001")
    assert d.status_code == 200
    assert "New version (v0.1 → v0.2)" in d.text and "Mark paper-ready" in d.text and "版本历史" in d.text
    assert client.get("/artifacts/ART-2099-0000").status_code == 404


def test_version_bump_changes_only_the_version_line(repo, client):
    path = repo / LANDSCAPE
    before = path.read_text()
    sha = A.load(path).sha
    refused = client.post("/artifacts/ART-2026-4001/front", data={"sha": sha, "bump": "minor"})
    assert refused.status_code == 400 and path.read_text() == before
    r = client.post("/artifacts/ART-2026-4001/front", data={"sha": sha, "bump": "minor", "confirm": "yes"},
                    follow_redirects=False)
    assert r.status_code == 303
    changed = [l for l in difflib.ndiff(before.splitlines(), path.read_text().splitlines()) if l[:1] in "+-"]
    assert changed == ["- version: v0.1", "+ version: v0.2"]


def test_front_matter_edits(repo, conn, client):
    path = repo / LANDSCAPE
    other = A.load(A.create("puzzle", "chair-rule", "asean-centrality", week="2026-W41")).ref
    # promotion is locked while Claim is empty, even when confirmed
    r = client.post("/artifacts/ART-2026-4001/front", data={"sha": A.load(path).sha, "level": "paper-ready",
                                                           "version": "v1.0", "confirm": "yes"})
    assert r.status_code == 400 and A.load(path).get("level") == "seed"
    make_counting(path)
    r = client.post("/artifacts/ART-2026-4001/front", data={"sha": A.load(path).sha, "level": "paper-ready",
                                                           "version": "v1.0", "confirm": "yes"}, follow_redirects=False)
    assert r.status_code == 303
    a = A.load(path)
    assert a.get("level") == "paper-ready" and a.get("version") == "v1.0"
    before = path.read_text()
    r = client.post("/artifacts/ART-2026-4001/front", data={"sha": a.sha, "related_add": other}, follow_redirects=False)
    assert r.status_code == 303
    changed = [l for l in difflib.ndiff(before.splitlines(), path.read_text().splitlines()) if l[:1] in "+-"]
    assert changed == ["- related: []", f"+ related: [{other}]"]
    assert client.post("/artifacts/ART-2026-4001/front", data={"sha": A.load(path).sha, "related_add": "ART-2026-9999"}).status_code == 400
    stale = client.post("/artifacts/ART-2026-4001/front", data={"sha": "0" * 64, "status": "active"})
    assert stale.status_code == 409 and A.load(path).get("status") == "draft"
    back = client.get(f"/artifacts/{other}")
    assert "ART-2026-4001" in back.text  # backlink


# ------------------------------------------------------------------ Harvest

def test_harvest_create_and_pages(repo, client):
    r = client.get("/harvest/new?kind=monthly&period=2026-10")
    assert r.status_code == 200 and "2026-W40" in r.text and "2026-W44" in r.text
    r = client.post("/harvest", data={"kind": "monthly", "period": "2026-10"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/harvest/2026-10"
    h = H.load(repo / "_harvests" / "2026-10.md")
    assert (h.kind, h.period, h.from_week, h.to_week) == ("monthly", "2026-10", "2026-W40", "2026-W44")
    assert h.title == "Harvest: October 2026" and "Which three artifacts started to connect?" in h.doc.body
    assert "What am I actually researching?" not in h.doc.body
    page = client.get("/harvest/2026-10")
    assert page.status_code == 200 and "ART-2026-4001" in page.text and "/projects/asean-centrality" in page.text
    listing = client.get("/harvest")
    assert listing.status_code == 200 and "/harvest/2026-10" in listing.text
    q = H.load(H.create("quarterly", "2026-Q4"))
    assert q.to_week == "2026-W53" and "What am I actually researching?" in q.doc.body


def test_harvest_save_is_sha_guarded(repo, client):
    client.post("/harvest", data={"kind": "monthly", "period": "2026-10"})
    path = repo / "_harvests" / "2026-10.md"
    h = H.load(path)
    path.write_text(path.read_text() + "\nelsewhere\n")
    disk = path.read_text()
    r = client.post("/harvest/2026-10/save", data={"sha": h.sha, "text": h.text().replace("## What I made this month", "## Made")})
    assert r.status_code == 409 and "@@" in r.text and path.read_text() == disk


def test_harvest_due_indicator(repo, conn):
    from research_lab.config import get_paths
    make_counting(repo / LANDSCAPE)
    for i in range(3):
        make_counting(A.create("puzzle", f"p{i}", "asean-centrality", week=f"2026-W4{i + 1}"))
    A.index(conn)
    assert H.due(conn, get_paths()) == {"due": True, "count": 4, "threshold": 4, "since": None}
    H.create("monthly", "2026-10")
    assert H.due(conn, get_paths())["due"] is False


def test_harvest_review_apply_proposes_only_known_refs(repo, conn, client):
    make_counting(repo / LANDSCAPE)
    other_path = A.create("puzzle", "chair-rule", "asean-centrality", week="2026-W42")
    make_counting(other_path)
    other = A.load(other_path).ref
    files_before = {p: p.read_text() for p in (repo / "_artifacts").glob("*.md")}
    r = client.post("/harvest/2026-10/review")
    assert r.status_code == 200 and "harvest.review" in r.text
    job = conn.execute("SELECT * FROM ai_jobs WHERE kind = 'harvest.review'").fetchone()
    assert job["target_id"] == "2026-10"
    assert sorted(json.loads(job["request_json"])["params"]["refs"]) == sorted(["ART-2026-4001", other])
    complete(conn, job["id"], {
        "connections": [{"a": "ART-2026-4001", "b": other, "why": "Both locate centrality in a rule."},
                        {"a": "ART-2026-4001", "b": "ART-2026-9999", "why": "Unknown ref."},
                        {"a": other, "b": other, "why": "Self."}],
        "pipeline": [{"project": "asean-centrality", "rule": "exploring -> puzzle", "met": False, "missing": "A puzzle artifact"},
                     {"project": "made-up", "rule": "x", "met": True, "missing": ""}],
        "suggested_next": ["counterargument: great-power acceptance"],
    })
    assoc = conn.execute("SELECT src_type, src_id, dst_type, dst_id, origin, status FROM associations").fetchall()
    assert [tuple(a) for a in assoc] == [("artifact", "ART-2026-4001", "artifact", other, "ai-suggested", "proposed")]
    assert {p: p.read_text() for p in (repo / "_artifacts").glob("*.md")} == files_before
    result = json.loads(queue.get(conn, job["id"])["result_json"])
    assert [p["project"] for p in result["pipeline"]] == ["asean-centrality"]
    panel = client.get(f"/harvest/review/{job['id']}")
    assert "Both locate centrality in a rule." in panel.text and "A puzzle artifact" in panel.text


# ------------------------------------------------------------------ Now

def test_now_form_round_trip(repo, client):
    from research_lab.files import datafiles
    page = client.get("/now")
    assert page.status_code == 200 and 'name="f_need_to_find_out"' in page.text and "What explains the persistence" in page.text
    before = (repo / "_data/now.yml").read_text()
    reading = "\n".join(datafiles.read("now")["reading"]) + "\nAcharya — Whose Ideas Matter?"
    r = client.post("/now", data={"sha": datafiles.file_sha("now"), "f_reading": reading,
                                  "f_this_week": "Produce the W40 Research Landscape"}, follow_redirects=False)
    assert r.status_code == 303 and "saved=reading" in r.headers["location"]
    after = (repo / "_data/now.yml").read_text()
    old = _plain(before)
    assert _plain_data(datafiles.read("now")) == {**old, "reading": [*old["reading"], "Acharya — Whose Ideas Matter?"]}
    assert after.startswith("# What the dashboard shows")
    stale = client.post("/now", data={"sha": "0" * 64, "f_this_week": "Other"})
    assert stale.status_code == 409 and (repo / "_data/now.yml").read_text() == after


# ------------------------------------------------------------------ Commit

def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def test_commit_button_touches_only_artifact_and_now(repo, conn, client):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    art = repo / LANDSCAPE
    art.write_text(art.read_text().replace("## What seems insufficient\n\n...", "## What seems insufficient\n\nA sentence."))
    now = repo / "_data/now.yml"
    now.write_text(now.read_text().replace("this_week: Produce", "this_week: Finish"))
    year = repo / "_data/year.yml"
    year.write_text(year.read_text() + "\n# unrelated edit\n")
    page = client.get("/week")
    assert "M _data/year.yml" in page.text or " M _data/year.yml" in page.text
    msg = weekly.commit_message({"week": "2026-W40", "type": "landscape", "title": "Research Landscape"})
    assert msg == "W40: landscape — Research Landscape"
    r = client.post("/week/commit", data={"ref": "ART-2026-4001", "message": msg})
    assert r.status_code == 200 and "已提交" in r.text
    files = _git(repo, "show", "--name-only", "--format=%s", "HEAD").split()
    assert files[0] == "W40:" and sorted(files[-2:]) == sorted([LANDSCAPE, "_data/now.yml"])
    assert "year.yml" not in " ".join(files)
    assert _git(repo, "status", "--porcelain").strip() == "M _data/year.yml"
    assert "origin" not in _git(repo, "remote")
    again = client.post("/week/commit", data={"ref": "ART-2026-4001", "message": msg})
    assert again.status_code == 400 and "没有可提交的内容" in again.text
    versions = weekly.versions(LANDSCAPE)
    assert [v["subject"] for v in versions] == [msg, "initial"]
    assert msg in client.get("/artifacts/ART-2026-4001").text


def test_versions_empty_without_git(repo):
    assert weekly.versions(LANDSCAPE) == []
    assert weekly.git_status() is None
