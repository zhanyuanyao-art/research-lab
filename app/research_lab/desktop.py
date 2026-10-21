"""Research Lab as a Mac app: a native window (WKWebView via pywebview) around the local server.

The research content stays in the user's git repository (the one with _artifacts/ and _data/);
the packaged app only carries the code. The repository is found in this order: RL_REPO_ROOT,
the path saved in ~/Library/Application Support/Research Lab/config.json, the source checkout the
code runs from, ~/Projects/research-lab. If none qualifies, the window asks for a folder.
"""
from __future__ import annotations

import fcntl
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .distribution import is_trial

APP_NAME = "Research Lab Trial" if is_trial() else "Research Lab"
# Appended to the web view's user agent so the server can tell the Mac window from a browser on the same
# port (web/context.py): only the window gets the titlebar-less layout.
DESKTOP_UA_TOKEN = "ResearchLabDesktop"
DESKTOP_UA = f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) {DESKTOP_UA_TOKEN}"
SUPPORT_DIR = Path(os.environ.get("RL_DESKTOP_SUPPORT_DIR") or Path.home() / "Library" / "Application Support" / APP_NAME)
LOG_DIR = Path(os.environ.get("RL_DESKTOP_LOG_DIR") or Path.home() / "Library" / "Logs" / APP_NAME)
CONFIG_FILE = SUPPORT_DIR / "config.json"
PREFERRED_PORT = 4180          # stable so per-origin browser storage survives restarts; 4100 is the dev server
HOST = "127.0.0.1"


# ---------------------------------------------------------------- configuration

def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    pending = CONFIG_FILE.with_suffix(".tmp")
    pending.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(CONFIG_FILE)


def looks_like_repo(path: Path | None) -> bool:
    return bool(path) and (path / "_artifacts").is_dir() and (path / "_data").is_dir() and (path / "app").is_dir()


def source_checkout() -> Path | None:
    """The repository this code lives in, when running from source rather than from the .app."""
    if getattr(sys, "frozen", False):
        return None
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists() and looks_like_repo(parent):
            return parent
    return None


def resolve_repo() -> Path | None:
    candidates = [os.environ.get("RL_REPO_ROOT"), load_config().get("repo_root")]
    if not is_trial():
        candidates.extend([source_checkout(), Path.home() / "Projects" / "research-lab"])
    for c in candidates:
        if c and looks_like_repo(Path(c).expanduser()):
            return Path(c).expanduser().resolve()
    return None


def free_port(preferred: int = PREFERRED_PORT) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free local port")


def single_instance_lock():
    """Held for the life of the process; a second launch exits instead of starting a second server."""
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(SUPPORT_DIR / "instance.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return fh


def route_logs_to_file() -> None:
    """A windowed .app has no terminal; keep stdout/stderr somewhere Console.app and the user can find."""
    if not getattr(sys, "frozen", False):
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_DIR / "app.log", "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = log


def build_identity() -> str:
    from .config import APP_VERSION, build_info
    return APP_VERSION + ":" + str(build_info().get("commit", "source"))


def prepare_upgrade(conn, paths) -> Path | None:
    """Keep an immutable SQLite snapshot before any migration or startup recovery."""
    import sqlite3
    from datetime import datetime
    from uuid import uuid4
    marker = paths.data / "desktop-build.json"
    try:
        previous = json.loads(marker.read_text()).get("build")
    except (OSError, ValueError):
        previous = None
    if previous == build_identity():
        return None
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
        return None
    paths.backups.mkdir(parents=True, exist_ok=True)
    target = paths.backups / f"pre-upgrade-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.sqlite"
    try:
        from contextlib import closing
        with closing(sqlite3.connect(target)) as dest:
            conn.backup(dest)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


def record_build(paths) -> None:
    marker = paths.data / "desktop-build.json"
    pending = marker.with_suffix(".tmp")
    pending.write_text(json.dumps({"build": build_identity()}))
    pending.replace(marker)


# ---------------------------------------------------------------- server

class Server:
    def __init__(self, repo: Path, port: int):
        self.repo, self.port = repo, port
        self.url = f"http://{HOST}:{port}"
        self._server = None

    def start(self, timeout: float = 30.0) -> None:
        os.environ["RL_REPO_ROOT"] = str(self.repo)
        if is_trial():
            os.environ["RL_DATA_DIR"] = str(self.repo / "app/data")
        os.environ["RL_DESKTOP"] = "1"
        import uvicorn

        from . import config
        from .db.connection import backup, connect, migrate
        config._paths = None
        paths = config.get_paths()
        paths.ensure_dirs()
        conn = connect()
        try:
            prepare_upgrade(conn, paths)
            migrate(conn)
        except Exception:
            conn.close()
            raise
        last = sorted(paths.backups.glob("lab-*.sqlite"))
        if not last or time.time() - last[-1].stat().st_mtime > 86400:
            backup(conn)
        conn.close()

        from .web.app import create_app
        cfg = uvicorn.Config(create_app(port=self.port), host=HOST, port=self.port, log_level="warning",
                             log_config=None, access_log=False)
        self._server = uvicorn.Server(cfg)
        threading.Thread(target=self._server.run, name="research-lab-server", daemon=True).start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.url + "/", timeout=1) as r:
                    if r.status == 200:
                        record_build(paths)
                        return
            except OSError:
                time.sleep(0.15)
        raise RuntimeError(f"server did not start on {self.url}")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True


# ---------------------------------------------------------------- window chrome

def unify_titlebar(window) -> None:
    """No titlebar of our own: the title is hidden, the titlebar is transparent and the page runs to the top
    of the window (full-size content view), so the traffic lights float over the sidebar (Leo, 0.5.3: 「整个
    都改成透明的」). The web view receives clicks under the titlebar too, so the page marks its own drag
    regions (`data-window-drag`, base.html → WindowApi.window_drag_by) and keeps the traffic lights' corner
    clear (app.css, body.desktop). The background colour is what shows before the first page paints."""
    try:
        import AppKit
        from PyObjCTools import AppHelper
    except ImportError:
        return

    def apply() -> None:
        w = window.native
        if w is None:
            return
        w.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        w.setTitlebarAppearsTransparent_(True)
        w.setStyleMask_(w.styleMask() | AppKit.NSWindowStyleMaskFullSizeContentView)
        # pywebview paints the titlebar container with windowBackgroundColor for every non-frameless window
        # (platforms/cocoa.py: "Set the titlebar color"), which left an opaque grey strip across the top over
        # the page (Leo, 10-02). Clear it; the traffic lights live in a subview and stay.
        close = w.standardWindowButton_(AppKit.NSWindowCloseButton)
        container = close.superview().superview() if close is not None and close.superview() is not None else None
        # only that container: on a macOS that hangs the button elsewhere, superview² could be the window frame
        if container is not None and container.className() == "NSTitlebarContainerView" \
                and container.respondsToSelector_("setBackgroundColor:"):
            container.setBackgroundColor_(AppKit.NSColor.clearColor())
        light = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0xfa / 255, 0xfa / 255, 0xfa / 255, 1.0)
        dark = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0x11 / 255, 0x11 / 255, 0x13 / 255, 1.0)
        try:
            def provider(appearance):
                name = appearance.bestMatchFromAppearancesWithNames_([AppKit.NSAppearanceNameAqua, AppKit.NSAppearanceNameDarkAqua])
                return dark if name == AppKit.NSAppearanceNameDarkAqua else light
            color = AppKit.NSColor.colorWithName_dynamicProvider_("ResearchLabBackground", provider)
        except Exception:  # noqa: BLE001 - older PyObjC metadata: fall back to the light background
            color = light
        w.setBackgroundColor_(color)
        w.setTabbingMode_(AppKit.NSWindowTabbingModeDisallowed)

    AppHelper.callAfter(apply)


def upload_pdfs(base_url: str, paths: list[str], limit: int = 10) -> str:
    """POST PDFs to the running server as the page's import form would; returns the path it redirects to."""
    import httpx
    handles = [open(p, "rb") for p in paths[:limit]]
    try:
        files = [("files", (Path(p).name, fh, "application/pdf")) for p, fh in zip(paths, handles)]
        # The mutation guard accepts POSTs whose Origin is the app itself.
        r = httpx.post(base_url + "/papers", files=files, headers={"Origin": base_url}, follow_redirects=False, timeout=300)
    except httpx.HTTPError:
        return "/papers?" + urllib.parse.urlencode({"rejected": "导入失败，请重试"})
    finally:
        for fh in handles:
            fh.close()
    return r.headers.get("location") or "/papers"


def build_menu(get_window, base_url: str):
    from webview.menu import Menu, MenuAction, MenuSeparator

    def go(path: str):
        def action():
            w = get_window()
            if w is not None:
                w.load_url(base_url + path)
        return action

    def reload_page():
        w = get_window()
        if w is not None:
            w.evaluate_js("location.reload()")

    def focus_search():
        w = get_window()
        if w is not None:
            w.evaluate_js("var s=document.getElementById('global-search'); if (s) { s.focus(); s.select(); }")

    def import_papers():
        # A native menu click is not a web user gesture, so a file input cannot be opened from JS here.
        w = get_window()
        if w is None:
            return
        import webview
        paths = w.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True, file_types=("PDF (*.pdf)",))
        if paths:
            w.evaluate_js("document.body.classList.add('importing')")
            w.load_url(base_url + upload_pdfs(base_url, list(paths)))

    def reveal_repo():
        w = get_window()
        repo = os.environ.get("RL_REPO_ROOT")
        if repo:
            import subprocess
            subprocess.run(["open", repo], check=False)

    return [
        Menu("文件", [
            MenuAction("导入文献…", import_papers),
            MenuAction("导入数据…", go("/data#import")),
            MenuAction("新建产出", go("/artifacts/new")),
            MenuAction("新建课题", go("/projects/new")),
            MenuSeparator(),
            MenuAction("在访达中显示研究仓库", reveal_repo),
        ]),
        Menu("前往", [
            MenuAction("首页", go("/")),
            MenuAction("文献", go("/papers")),
            MenuAction("数据", go("/data")),
            MenuAction("研究档案", go("/artifacts")),
            MenuSeparator(),
            MenuAction("助手", go("/chat")),
            MenuAction("设置", go("/settings")),
            MenuSeparator(),
            MenuAction("搜索", focus_search),
            MenuAction("重新载入", reload_page),
        ]),
    ]


def snapshot_to(window, path: str, quit_after: bool = False) -> None:
    """Self-check hook (RL_DESKTOP_SNAPSHOT=/path.png): the app writes what its web view shows, since
    screen capture from outside needs a permission a build machine may not have."""
    import AppKit
    from PyObjCTools import AppHelper

    import WebKit

    def find_webview(view):
        if view.isKindOfClass_(WebKit.WKWebView):
            return view
        for sub in view.subviews():
            hit = find_webview(sub)
            if hit is not None:
                return hit
        return None

    def done(image, error):
        if image is not None:
            rep = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
            data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
            data.writeToFile_atomically_(path, True)
        if quit_after:
            AppHelper.callLater(0.2, lambda: [w.destroy() for w in __import__("webview").windows])

    def take():
        wv = find_webview(window.native.contentView())
        if wv is not None:
            wv.takeSnapshotWithConfiguration_completionHandler_(None, done)

    AppHelper.callAfter(take)


# ---------------------------------------------------------------- first run

FIRST_RUN_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>
:root{color-scheme:light dark;--bg:#fafafa;--fg:#18181b;--muted:#71717a;--card:#fff;--border:#e7e7ea;--btn:#18181b;--btn-ink:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#111113;--fg:#f4f4f5;--muted:#a1a1aa;--card:#18181b;--border:#28282d;--btn:#f4f4f5;--btn-ink:#18181b}}
body{margin:0;height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:14px -apple-system,"PingFang SC",sans-serif}
.card{width:440px;padding:32px;background:var(--card);border:1px solid var(--border);border-radius:14px}
h1{font-size:20px;margin:0 0 8px}p{color:var(--muted);line-height:1.6;margin:0 0 20px}
button{height:36px;padding:0 16px;border:0;border-radius:8px;background:var(--btn);color:var(--btn-ink);font:inherit;font-weight:500;cursor:pointer}
#err{color:#b91c1c;margin-top:12px;min-height:1em}</style></head><body><div class="card">
<h1>开始你的研究工作区</h1><p>先选择保存位置，再跟着引导建立课题、放入文献。会在所选位置新建一个 Research Lab 文件夹。</p>
<button onclick="pick(this,'create')">新建工作区…</button>
<button onclick="pick(this,'choose')">打开已有工作区…</button>
<div id="err" role="status"></div></div><script>
function pick(button, method) {
  document.querySelectorAll('button').forEach(function(b){b.disabled=true});
  pywebview.api[method]().then(function(m){document.getElementById('err').textContent=m||''})
    .catch(function(){document.getElementById('err').textContent='无法打开工作区，请重试。'})
    .finally(function(){document.querySelectorAll('button').forEach(function(b){b.disabled=false})});
}
</script></body></html>"""


class FirstRunApi:
    """The window's JS API: the first-run folder picker, and moving the window from the page's drag regions."""

    def __init__(self, on_chosen):
        self._on_chosen = on_chosen
        self.window = None

    def window_drag_by(self, dx: float, dy: float) -> None:
        """Move the window by a mouse delta in screen points (y down). Deltas, not positions: pywebview's own
        drag regions place the window from screen coordinates measured against the main display, which throws
        a window on a second display across the screen."""
        def move() -> None:
            w = self.window.native if self.window else None
            if w is not None:
                f = w.frame()
                w.setFrameOrigin_((f.origin.x + float(dx), f.origin.y - float(dy)))
        _on_main(move)

    def window_zoom(self) -> None:
        """Double-click on a drag region, as on a titlebar."""
        _on_main(lambda: self.window.native.performZoom_(None) if self.window and self.window.native else None)

    def choose(self) -> str:
        import webview
        picked = self.window.create_file_dialog(webview.FileDialog.FOLDER) if self.window else None
        if not picked:
            return ""
        path = Path(picked[0])
        if not looks_like_repo(path):
            return "这里还不是 Research Lab 工作区。首次使用请选择「新建工作区」。"
        return self._open(path)

    def create(self) -> str:
        import webview
        from .workspace import initialize
        picked = self.window.create_file_dialog(webview.FileDialog.FOLDER) if self.window else None
        if not picked:
            return ""
        try:
            path = initialize(Path(picked[0]) / "Research Lab")
        except FileExistsError:
            return "这个位置已有 Research Lab 文件夹。请选择「打开已有工作区」，或换一个位置。"
        except OSError:
            return "无法创建工作区，请换一个可写入的位置。"
        return self._open(path)

    def _open(self, path: Path) -> str:
        cfg = load_config()
        cfg["repo_root"] = str(path)
        save_config(cfg)
        # A first-run folder choice must not inherit another workspace's database override.
        os.environ["RL_DATA_DIR"] = str(path / "app/data")
        threading.Thread(target=self._on_chosen, args=(path,), daemon=True).start()
        return ""


def _on_main(fn) -> None:
    try:
        from PyObjCTools import AppHelper
    except ImportError:
        return
    AppHelper.callAfter(fn)


# ---------------------------------------------------------------- entry point

def main() -> None:
    route_logs_to_file()
    # Apps started from Finder get a minimal PATH; git (versions, commit) and gh (digest fallback) live here.
    os.environ["PATH"] = ":".join(dict.fromkeys(["/opt/homebrew/bin", "/usr/local/bin", *os.environ.get("PATH", "/usr/bin:/bin").split(":")]))
    lock = single_instance_lock()
    if lock is None:
        print("Research Lab is already running.", file=sys.stderr)
        return
    import webview
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True

    port = free_port()
    state: dict = {"window": None, "server": None}

    def boot(repo: Path) -> None:
        server = Server(repo, port)
        try:
            server.start()
        except Exception:
            server.stop()
            import traceback
            traceback.print_exc()
            state["window"].load_html(FIRST_RUN_HTML.replace('<div id="err" role="status"></div>',
                '<div id="err" role="status">启动失败。请检查文件夹权限，或选择其他工作区。</div>'))
            return
        state["server"] = server
        state["window"].load_url(server.url + "/")

    repo = resolve_repo()
    api = FirstRunApi(boot)
    window = webview.create_window(
        APP_NAME, html=FIRST_RUN_HTML if repo is None else "<html><body style='background:#fafafa'></body></html>",
        js_api=api, width=1360, height=880, min_size=(960, 640), text_select=True, zoomable=True,
    )
    api.window = window
    state["window"] = window

    def on_shown() -> None:
        unify_titlebar(window)
        if repo is not None:
            try:
                boot(repo)
            except Exception as exc:  # noqa: BLE001 - show the failure in the window instead of dying silently
                window.load_html(f"<body style='font:14px -apple-system;padding:40px'><h2>启动失败</h2><pre>{exc}</pre>"
                                 f"<p>日志：{LOG_DIR / 'app.log'}</p></body>")

    snap = os.environ.get("RL_DESKTOP_SNAPSHOT")

    def on_loaded() -> None:
        if snap:
            target = os.environ.get("RL_DESKTOP_SNAPSHOT_PATH", "/") if state["server"] else None
            if target and urllib.parse.urlparse(window.get_current_url() or "").path != urllib.parse.urlparse(target).path:
                window.load_url(state["server"].url + target)
                return
            time.sleep(3)
            snapshot_to(window, snap, quit_after=os.environ.get("RL_DESKTOP_SNAPSHOT_QUIT") == "1")

    def on_closed() -> None:
        if state["server"] is not None:
            state["server"].stop()

    window.events.shown += on_shown
    window.events.closed += on_closed
    window.events.loaded += on_loaded
    base = f"http://{HOST}:{port}"
    webview.start(menu=build_menu(lambda: state["window"], base), private_mode=False,
                  storage_path=str(SUPPORT_DIR / "webview"), user_agent=DESKTOP_UA)


if __name__ == "__main__":
    main()
