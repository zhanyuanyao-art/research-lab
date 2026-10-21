import socket
from pathlib import Path

from research_lab import desktop


def test_looks_like_repo(repo):
    assert desktop.looks_like_repo(repo)
    assert not desktop.looks_like_repo(repo / "_artifacts")
    assert not desktop.looks_like_repo(None)


def test_resolve_repo_prefers_env_then_config(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(desktop, "SUPPORT_DIR", tmp_path)
    monkeypatch.setenv("RL_REPO_ROOT", str(repo))
    assert desktop.resolve_repo() == repo.resolve()
    monkeypatch.delenv("RL_REPO_ROOT")
    desktop.save_config({"repo_root": str(repo)})
    assert desktop.resolve_repo() == repo.resolve()
    desktop.save_config({"repo_root": str(tmp_path / "nowhere")})
    assert desktop.resolve_repo() != tmp_path / "nowhere"


def test_free_port_falls_back_when_preferred_is_taken():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        taken = s.getsockname()[1]
        s.listen()
        port = desktop.free_port(taken)
    assert port != taken and port > 0


def test_single_instance_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "SUPPORT_DIR", tmp_path)
    first = desktop.single_instance_lock()
    assert first is not None
    assert desktop.single_instance_lock() is None
    first.close()


def test_desktop_flag_marks_body(client, monkeypatch):
    monkeypatch.setenv("RL_DESKTOP", "1")
    page = client.get("/settings", headers={"User-Agent": desktop.DESKTOP_UA}).text
    assert 'class="app desktop"' in page or ' desktop"' in page


def test_static_assets_are_versioned(client):
    html = client.get("/").text
    assert "/static/app.css?v=" in html and "/static/icons.svg?v=" in html


def test_upload_pdfs_posts_files_and_returns_redirect(tmp_path, monkeypatch):
    import httpx
    from research_lab import desktop

    pdf = tmp_path / "tan2023.pdf"
    pdf.write_bytes(b"%PDF-1.7 test")
    seen = {}

    def fake_post(url, files, headers, follow_redirects, timeout):
        seen["url"], seen["names"], seen["origin"] = url, [f[1][0] for f in files], headers.get("Origin")
        return httpx.Response(303, headers={"location": "/papers/tan2023-convenes"})

    monkeypatch.setattr(httpx, "post", fake_post)
    assert desktop.upload_pdfs("http://127.0.0.1:4180", [str(pdf)]) == "/papers/tan2023-convenes"
    assert seen == {"url": "http://127.0.0.1:4180/papers", "names": ["tan2023.pdf"], "origin": "http://127.0.0.1:4180"}

    def down(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", down)
    assert desktop.upload_pdfs("http://127.0.0.1:4180", [str(pdf)]).startswith("/papers?rejected=")


def test_titlebar_less_layout_only_in_the_mac_window(client, monkeypatch):
    """Card 0048: the Mac window draws no titlebar, so its pages leave the traffic lights' corner clear and mark
    drag regions; a browser pointed at the same server (same RL_DESKTOP process) sees the page unchanged."""
    monkeypatch.setenv("RL_DESKTOP", "1")
    window = client.get("/", headers={"User-Agent": desktop.DESKTOP_UA}).text
    assert " desktop" in window.split("<body", 1)[1].split(">", 1)[0]
    assert 'class="titlebar-drag" data-window-drag' in window and '<header class="topbar" data-window-drag>' in window
    browser = client.get("/", headers={"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15"}).text
    assert "data-window-drag" not in browser and " desktop" not in browser.split("<body", 1)[1].split(">", 1)[0]
