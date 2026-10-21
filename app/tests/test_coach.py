import re
from contextlib import closing

import httpx
import pymupdf
import pytest
from fastapi.testclient import TestClient

from research_lab import coach, config
from research_lab.db.connection import connect, get_setting, set_setting
from research_lab.files import datafiles, issues, tasks
from research_lab.workspace import initialize


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    root = initialize(tmp_path / "workspace")
    monkeypatch.setenv("RL_REPO_ROOT", str(root))
    monkeypatch.delenv("RL_DATA_DIR", raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    for key in ("RL_AI_WORKER", "RL_FEEDS_AUTO", "RL_PM_AUTO", "RL_AUTOFIX"):
        monkeypatch.setenv(key, "0")
    monkeypatch.setenv("RL_CLAUDE_BIN", "none")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config._paths = None
    from research_lab.web.app import create_app
    app = create_app()
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as http:
        app.state.intake_http = http
        with TestClient(app, base_url="http://127.0.0.1:4100") as client, closing(connect()) as conn:
            client.headers["Origin"] = "http://127.0.0.1:4100"
            yield client, conn, root
    config._paths = None


def project_form():
    return dict(name="高温与城市治理", direction_name="城市研究", direction_key="CITY", question="预警怎样变成行动？",
                projects_sha=datafiles.file_sha("projects"), directions_sha=datafiles.file_sha("directions"))


def upload(client, conn, title="Heat and cities"):
    project = coach.state(conn)["project"]
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((60, 60), title, fontsize=20)
        page.insert_textbox(pymupdf.Rect(60, 100, 550, 700),
                            "This is a synthetic paper for testing. The city uses warnings to prepare for heat. " * 12)
        content = doc.tobytes()
    response = client.post("/guide/import", data={"project": project["slug"]},
                           files={"files": ("example.pdf", content, "application/pdf")}, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/guide"
    return coach.state(conn)["paper"]["id"]


def test_complete_loop_without_model_and_retry_is_idempotent(fresh):
    client, conn, root = fresh
    assert "你想研究什么" in client.get("/").text
    assert coach.state(conn)["step"] == 1
    response = client.post("/guide/project", data=project_form())
    assert response.status_code == 200
    project = coach.state(conn)["project"]
    assert project["name"] == "高温与城市治理"
    assert "放入第一篇文献" in response.text
    paper = upload(client, conn)
    assert coach.state(conn)["step"] == 3
    assert "先看原文" in client.get("/guide").text
    assert client.get(f"/papers/{paper}").status_code == 200
    assert coach.state(conn)["step"] == 3  # Viewing is not acknowledgement.
    assert client.post("/guide/review", data=dict(project=project["slug"], paper=paper)).status_code == 200
    assert coach.state(conn)["step"] == 4
    form = dict(project=project["slug"], paper=paper, revision=issues.revision(),
                title="找一篇不同解释的论文", note="还不确定协调机制是否足够。", due="2027-01-15")
    assert client.post("/guide/next", data=form).status_code == 200
    assert client.post("/guide/next", data=form).status_code == 200
    state = coach.state(conn)
    assert state["finished"] and len(tasks.read()) == 1
    assert state["task"]["due"] == "2027-01-15"
    assert f"/papers/{paper}" in state["task"]["note"]
    assert "还不确定" in state["task"]["note"]
    assert not list((root / "_artifacts").glob("*.md"))
    assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM ai_runs").fetchone()[0] == 0
    assert get_setting(conn, "ai.paused") is True
    assert client.post("/guide/finish").status_code == 200
    assert 'aria-label="上手引导"' not in client.get("/").text


def test_skip_resume_and_existing_users_are_not_forced(fresh):
    client, conn, root = fresh
    client.post("/guide/project", data=project_form())
    client.post("/guide/dismiss")
    assert coach.state(conn)["hidden"]
    assert 'aria-label="上手引导"' not in client.get("/").text
    response = client.post("/guide/start")
    assert "放入第一篇文献" in response.text
    with closing(connect()) as reopened:
        assert coach.state(reopened)["step"] == 2
    conn.execute("DELETE FROM settings WHERE key LIKE 'coach.%'")
    assert "你想研究什么" not in client.get("/").text
    assert "打开上手引导" in client.get("/settings").text


def test_validation_and_stale_forms_preserve_user_input(fresh):
    client, conn, root = fresh
    form = project_form()
    response = client.post("/guide/project?step=1", data={**form, "name": ""})
    assert response.status_code == 400
    assert datafiles.read("directions") == []
    datafiles.add_direction({"slug": "another", "name": "已有方向", "key": "OTHER"})
    response = client.post("/guide/project?step=1", data=form)
    assert response.status_code == 409
    assert "高温与城市治理" in response.text
    assert len(datafiles.read("directions")) == 1
    assert datafiles.read("projects") == []
    assert client.post("/guide/not-an-action").status_code == 404
    assert client.post("/guide/review", data={}).status_code == 400
    assert client.post("/guide/next", data={}).status_code == 400


def test_import_errors_and_switching_papers_do_not_fake_completion(fresh):
    client, conn, root = fresh
    client.post("/guide/project", data=project_form())
    project = coach.state(conn)["project"]["slug"]
    response = client.post("/guide/import", data={"project": project},
                           files={"files": ("bad.pdf", b"not a PDF", "application/pdf")})
    assert response.status_code == 200
    assert coach.state(conn)["step"] == 2
    paper = upload(client, conn)
    client.post("/guide/review", data=dict(project=project, paper=paper))
    form = dict(project=project, paper=paper, revision=issues.revision(), title="下一步")
    client.post("/guide/next", data=form)
    assert coach.state(conn)["finished"]
    upload(client, conn, "Different synthetic document")
    assert coach.state(conn)["step"] == 3
    assert not coach.state(conn)["finished"]
    assert client.post("/guide/review", data=dict(project=project, paper=paper)).status_code == 400


def test_step_urls_cannot_skip_requirements_and_markup_is_escaped(fresh):
    client, conn, root = fresh
    assert "你想研究什么" in client.get("/guide?step=4").text
    form = project_form()
    form["name"] = '<script>alert("x")</script>'
    client.post("/guide/project", data=form)
    response = client.get("/guide")
    assert '<script>alert("x")</script>' not in response.text
    assert "&lt;script&gt;" in response.text
    sprite = (config.get_paths().web_static / "icons.svg").read_text()
    icons = re.findall(r'<symbol id="([^"]+)"', sprite)
    guide = (config.get_paths().web_templates / "pages/guide.html").read_text()
    assert all(name in icons for name in re.findall(r"ui.icon\('([^']+)'", guide))


def test_removed_paper_cannot_reuse_another_papers_completed_task(fresh):
    client, conn, root = fresh
    client.post("/guide/project", data=project_form())
    project = coach.state(conn)["project"]["slug"]
    fallback = upload(client, conn, "Other paper available")
    chosen = upload(client, conn, "Paper with a saved next step")
    client.post("/guide/review", data=dict(project=project, paper=chosen))
    client.post("/guide/next", data=dict(project=project, paper=chosen, revision=issues.revision(), title="旧文献的下一步"))
    assert coach.state(conn)["finished"]
    conn.execute("DELETE FROM paper_projects WHERE paper_id=?", (chosen,))
    assert coach.state(conn)["paper"]["id"] == fallback
    client.post("/guide/review", data=dict(project=project, paper=fallback))
    assert coach.state(conn)["step"] == 4
    assert not coach.state(conn)["finished"]


def test_changed_project_rejects_import_before_any_paper_is_written(fresh):
    client, conn, root = fresh
    client.post("/guide/project", data=project_form())
    response = client.post("/guide/import", data={"project": "deleted"},
                           files={"files": ("bad.pdf", b"not a PDF", "application/pdf")})
    assert response.status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 0


def test_coach_import_applies_limits_before_parsing(fresh):
    client, conn, root = fresh
    client.post("/guide/project", data=project_form())
    project = coach.state(conn)["project"]["slug"]
    files = [("files", ("a.pdf", b"a", "application/pdf")),
             ("files", ("b.pdf", b"b", "application/pdf"))]
    assert client.post("/guide/import", data={"project": project}, files=files).status_code == 400
    assert client.post("/guide/import", headers={"content-length": str(10**10)}, content=b"").status_code == 413
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 0


def test_next_step_stale_form_and_deleted_task_are_not_complete(fresh):
    client, conn, root = fresh
    client.post("/guide/project", data=project_form())
    project = coach.state(conn)["project"]["slug"]
    paper = upload(client, conn)
    client.post("/guide/review", data=dict(project=project, paper=paper))
    form = dict(project=project, paper=paper, revision="stale", title="保留我的下一步", note="保留我的笔记")
    response = client.post("/guide/next", data=form)
    assert response.status_code == 409
    assert "保留我的下一步" in response.text and "保留我的笔记" in response.text
    assert tasks.read() == []
    form["revision"] = issues.revision()
    client.post("/guide/next", data=form)
    task = coach.state(conn)["task"]
    assert task
    (root / "_data/tasks.yml").write_text("[]\n")
    assert coach.state(conn)["step"] == 4


def test_direction_prefix_validation_and_existing_direction_reuse(fresh):
    client, conn, root = fresh
    form = project_form()
    for key in ("", "city", "TOOLONG", "LAB"):
        response = client.post("/guide/project?step=1", data={**form, "direction_key": key})
        assert response.status_code == 400
        assert datafiles.read("directions") == []
        assert datafiles.read("projects") == []
    assert client.post("/guide/project", data=form).status_code == 200
    direction = datafiles.read("directions")[0]
    assert direction["key"] == "CITY"
    response = client.post("/guide/project", data={**project_form(), "name": "第二个问题", "direction_key": ""})
    assert response.status_code == 200
    assert len(datafiles.read("directions")) == 1
    assert coach.state(conn)["project"]["direction"] == direction["slug"]
