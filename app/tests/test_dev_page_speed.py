"""Card 0164: the 开发 page took 12–34 s and failed the page check (30 s) under load. The overview's git, lsof
and lsregister calls are now bounded and parallel, the overview is cached for 30 s keyed by git metadata
mtimes, the app-copies lookup runs in the background, and the page check retries a timed-out page once."""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest

from research_lab.devflow import flow, status


@pytest.fixture(autouse=True)
def fresh_state():
    from research_lab.web.routes import dev
    status.clear_cache()
    status._copies.update(at=0.0, key=None, value=None, thread=None)
    dev._board_memo.clear()
    yield
    t = status._copies["thread"]
    if t is not None:
        t.join(5)
    status.clear_cache()
    status._copies.update(at=0.0, key=None, value=None, thread=None)
    dev._board_memo.clear()


class FakeRun:
    """Stands in for subprocess.run: records every call and answers by the command's first words."""

    def __init__(self, answers=None, slow=None, hang=()):
        self.calls, self.answers, self.slow, self.hang = [], answers or {}, slow or {}, hang
        self.lock = threading.Lock()

    def __call__(self, cmd, **kw):
        with self.lock:
            self.calls.append((cmd, kw))
        words = [c for c in cmd if c not in ("-C",) and not c.startswith("/")]
        key = next((k for k in self.answers if " ".join(words).startswith(k)), None)
        if any(" ".join(words).startswith(h) for h in self.hang):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        for k, secs in self.slow.items():
            if " ".join(words).startswith(k):
                time.sleep(secs)
        return subprocess.CompletedProcess(cmd, 0, self.answers.get(key, ""), "")


def _porcelain(root: Path, names: list[str]) -> str:
    blocks = [f"worktree {root}\nHEAD {'a' * 40}\nbranch refs/heads/main\n"]
    blocks += [f"worktree {root.parent}/research-lab.{n}\nHEAD {'b' * 40}\nbranch refs/heads/agent/{n}\n" for n in names]
    return "\n".join(blocks)


# ── the cache ────────────────────────────────────────────────────────────────────────────────────────────────

def test_cached_computes_once_per_key_and_ttl(monkeypatch):
    calls = []
    compute = lambda: calls.append(1) or len(calls)  # noqa: E731
    assert status.cached("x", "k1", 30, compute) == 1
    assert status.cached("x", "k1", 30, compute) == 1                     # same key, inside the ttl
    assert status.cached("x", "k2", 30, compute) == 2                     # a git ref moved: recompute
    now = time.monotonic()
    monkeypatch.setattr(status.time, "monotonic", lambda: now + 31)
    assert status.cached("x", "k2", 30, compute) == 3                     # older than the ttl
    assert len(calls) == 3


def test_concurrent_callers_share_one_computation():
    started, calls = threading.Event(), []

    def compute():
        calls.append(1)
        started.set()
        time.sleep(0.3)
        return "overview"

    out = []
    threads = [threading.Thread(target=lambda: out.append(status.cached("y", "k", 30, compute))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert out == ["overview"] * 4 and len(calls) == 1


def test_overview_is_cached_but_the_lock_is_read_fresh(monkeypatch):
    computed, locks = [], iter([None, {"holder": "集成会话", "purpose": "合并", "since": "12:00"}])
    monkeypatch.setattr(status, "_compute_overview", lambda: computed.append(1) or {"versions": {}, "worktrees": [],
                                                                                    "servers": [], "code_root": "", "pid": 1})
    monkeypatch.setattr(status, "overview_key", lambda: ("same",))
    monkeypatch.setattr(status, "read_lock", lambda: next(locks))
    first = status.overview(include_copies=False)
    second = status.overview(include_copies=False)
    assert len(computed) == 1 and first["lock"] is None and second["lock"]["holder"] == "集成会话"
    assert second["copies"] is None


def test_the_cache_key_follows_branch_tips_and_new_worktrees(tmp_path, monkeypatch):
    common = tmp_path / ".git"
    (common / "refs/heads/agent").mkdir(parents=True)
    (common / "worktrees").mkdir()
    monkeypatch.setattr(status, "_common_dir", lambda: common)
    before = status.overview_key()
    assert status.overview_key() == before
    time.sleep(0.01)
    (common / "refs/heads/agent/c0164").write_text("abc\n")                # a commit on a nested branch ref
    after_commit = status.overview_key()
    assert after_commit != before
    time.sleep(0.01)
    (common / "worktrees/research-lab.c0165").mkdir()                       # a worktree added
    assert status.overview_key() != after_commit


# ── bounded subprocesses ─────────────────────────────────────────────────────────────────────────────────────

def test_every_overview_subprocess_has_a_short_timeout(tmp_path, monkeypatch):
    root = tmp_path / "research-lab"
    monkeypatch.setattr(status, "main_root", lambda: root)
    fake = FakeRun({"git worktree list": _porcelain(root, ["c1", "c2"]),
                    "git for-each-ref": "refs/heads/main\t0 0\nrefs/heads/agent/c1\t2 5\nrefs/heads/agent/c2\t0 1\n",
                    "lsof -nP": "p77\nn*:4120\n", "ps": "  77 uv run rl serve\n", "lsof -a": "p77\nfcwd\nn/tmp/wt\n"})
    monkeypatch.setattr(status.subprocess, "run", fake)
    o = status._compute_overview()
    assert fake.calls and all(kw.get("timeout") is not None and kw["timeout"] <= status.GIT_TIMEOUT for _, kw in fake.calls)
    assert all((kw.get("env") or {}).get("GIT_OPTIONAL_LOCKS") == "0" for cmd, kw in fake.calls if cmd[0] == "git")
    w = {x["name"]: x for x in o["worktrees"]}
    assert (w["c1"]["ahead"], w["c1"]["behind"]) == (2, 5) and (w["c2"]["ahead"], w["c2"]["behind"]) == (0, 1)
    assert not any(cmd[1:4] == ["-C", str(root), "rev-list"] for cmd, _ in fake.calls if "agent/c1" in " ".join(cmd))
    assert o["servers"] == [{"pid": 77, "port": 4120, "kind": "serve", "cwd": "/tmp/wt", "command": "uv run rl serve"}]


def test_a_git_status_that_hangs_reads_as_unknown_not_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(status.subprocess, "run", FakeRun(hang=("git status",)))
    assert status.dirty(tmp_path, timeout=1) == {"code": [], "content": [], "unknown": True}
    assert status.git("log", "-1", timeout=1) == ""
    with pytest.raises(RuntimeError, match="秒内"):
        status.git("status", check=True, timeout=1)


def test_a_worktree_git_cannot_read_is_unknown_too(tmp_path):
    broken = tmp_path / "research-lab.broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /nowhere/.git/worktrees/broken\n")     # a worktree whose admin dir is gone
    assert status.dirty(broken) == {"code": [], "content": [], "unknown": True}
    assert status.dirty(broken, timeout=5)["unknown"] is True
    assert any(status.dirty(broken).values())                                    # flow's cleanliness test says dirty


def _overview_with(dirty):
    return {"versions": {"version": "1", "head": "abc x", "installed": None, "installed_current": False,
                         "ahead_of_origin": 0, "behind_origin": 0, "tag": ""},
            "worktrees": [{"name": "c1", "branch": "agent/c1", "port": 4120, "is_main": False, "ahead": 1, "behind": 0,
                           "dirty": dirty}],
            "servers": [], "lock": None, "copies": [], "code_root": "", "pid": 1}


@pytest.mark.parametrize("dirty,shown,hidden", [
    ({"code": [], "content": [], "unknown": True}, "未提交：没查到", "未提交代码"),
    ({"code": ["a.py"], "content": []}, "未提交代码 1", "没查到"),
])
def test_the_cli_never_shows_an_unchecked_worktree_as_clean(monkeypatch, dirty, shown, hidden):
    from typer.testing import CliRunner
    from research_lab.cli import app
    monkeypatch.setattr(status, "overview", lambda **kw: _overview_with(dirty))
    monkeypatch.setattr("research_lab.devflow.cards.board", lambda: {})
    r = CliRunner().invoke(app, ["dev", "status", "--no-copies"])
    line = next(x for x in r.output.splitlines() if x.strip().startswith("c1"))
    assert shown in line and hidden not in line, r.output


def test_flow_keeps_untimed_git(tmp_path, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(status.subprocess, "run", fake)
    status.git("merge", "--no-ff", "agent/x", cwd=tmp_path)
    status.dirty(tmp_path)
    assert all(kw["timeout"] is None and kw.get("env") is None for _, kw in fake.calls)


def test_worktree_git_calls_run_in_parallel(tmp_path, monkeypatch):
    root = tmp_path / "research-lab"
    names = [f"c{i}" for i in range(8)]
    for n in names:
        (tmp_path / f"research-lab.{n}").mkdir()
    root.mkdir()
    monkeypatch.setattr(status, "main_root", lambda: root)
    monkeypatch.setattr(status.subprocess, "run", FakeRun({"git worktree list": _porcelain(root, names)},
                                                          slow={"git status": 0.25}))
    t = time.perf_counter()
    out = status.worktrees(workers=8)
    assert len(out) == 9 and time.perf_counter() - t < 9 * 0.25 / 2         # serial would be 2.25 s


def test_a_detached_worktree_falls_back_to_rev_list(tmp_path, monkeypatch):
    root = tmp_path / "research-lab"
    text = f"worktree {root}\nHEAD {'a' * 40}\nbranch refs/heads/main\n\nworktree {tmp_path}/research-lab.x\nHEAD {'c' * 40}\ndetached\n"
    fake = FakeRun({"git worktree list": text, "git rev-list": "4\t1\n"})
    monkeypatch.setattr(status, "main_root", lambda: root)
    monkeypatch.setattr(status.subprocess, "run", fake)
    x = status.worktrees()[1]
    assert (x["behind"], x["ahead"]) == (4, 1) and x["dirty"] == {"code": [], "content": []}


def test_server_discovery_is_three_bounded_calls(monkeypatch):
    listeners = "".join(f"p{1000 + i}\nn*:{4101 + i}\n" for i in range(60))
    fake = FakeRun({"lsof -nP": listeners})
    monkeypatch.setattr(status.subprocess, "run", fake)
    out = status.servers()
    assert len(out) == status.MAX_SERVERS and len(fake.calls) == 3
    assert fake.calls[1][0][-1].count(",") == status.MAX_SERVERS - 1


def test_lsof_that_hangs_means_no_servers(monkeypatch):
    monkeypatch.setattr(status.subprocess, "run", FakeRun(hang=("lsof",)))
    assert status.servers() == []


# ── app copies in the background ─────────────────────────────────────────────────────────────────────────────

def test_app_copies_never_hold_up_the_page(monkeypatch):
    release, scans = threading.Event(), []

    def scan():
        scans.append(1)
        release.wait(5)
        return ["/Applications/Research Lab.app"]

    monkeypatch.setattr(status, "_scan_copies", scan)
    t = time.perf_counter()
    assert status.launchable_copies(wait=0.05) is None                  # first lookup still running
    assert time.perf_counter() - t < 1
    assert status.launchable_copies(wait=0) is None and len(scans) == 1  # no second lookup while one runs
    release.set()
    status._copies["thread"].join(5)
    assert status.launchable_copies(wait=0) == ["/Applications/Research Lab.app"] and len(scans) == 1


def test_app_copies_refresh_after_the_ttl_and_show_the_old_list_meanwhile(monkeypatch):
    lists = iter([["/a/Research Lab.app"], ["/a/Research Lab.app", "/b/Research Lab.app"]])
    gate = threading.Event()
    gate.set()
    monkeypatch.setattr(status, "_scan_copies", lambda: gate.wait(5) and next(lists))
    assert status.launchable_copies(wait=None) == ["/a/Research Lab.app"]
    gate.clear()
    now = time.monotonic()
    monkeypatch.setattr(status.time, "monotonic", lambda: now + status.COPIES_TTL + 1)
    assert status.launchable_copies(wait=0) == ["/a/Research Lab.app"]     # stale, refresh under way
    gate.set()
    status._copies["thread"].join(5)
    assert len(status.launchable_copies(wait=0)) == 2


# ── ports without a subprocess ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(shutil.which("cksum") is None, reason="needs POSIX cksum")
@pytest.mark.parametrize("name", ["", "a", "devops", "c0164-dev-12-34-30", "c0020-tasks-md-3", "研究"])
def test_cksum_matches_the_posix_tool(name):
    out = subprocess.run(["cksum"], input=name.encode(), capture_output=True).stdout.split()[0]
    assert status.cksum(name.encode()) == int(out)


# ── the page check ───────────────────────────────────────────────────────────────────────────────────────────

class FakeGet:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def __call__(self, url, timeout):
        self.calls += 1
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return httpx.Response(o)


@pytest.mark.parametrize("outcomes,expected,calls", [
    ([httpx.ReadTimeout("slow"), 200], 200, 2),                         # one retry on a timeout
    ([httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")], "ReadTimeout", 2),
    ([httpx.ConnectError("refused")], "ConnectError", 1),               # not a timeout: no retry
    ([500], 500, 1),
])
def test_the_page_check_retries_a_timeout_once(monkeypatch, outcomes, expected, calls):
    fake = FakeGet(*outcomes)
    monkeypatch.setattr(flow.httpx, "get", fake)
    assert flow.page_status("http://127.0.0.1:4199/dev") == expected and fake.calls == calls


# ── the page itself ──────────────────────────────────────────────────────────────────────────────────────────

def test_the_page_says_what_it_has_not_checked_yet(client, repo, monkeypatch):
    o = {**_overview_with({"code": [], "content": [], "unknown": True}), "copies": None}
    monkeypatch.setattr(status, "overview", lambda **kw: o)
    page = client.get("/dev").text
    assert "正在查找" in page and "没查到" in page


def test_the_board_is_reparsed_only_when_a_card_changes(client, repo, monkeypatch):
    from research_lab.devflow import cards
    monkeypatch.setattr(status, "overview", lambda **kw: {"versions": {}, "worktrees": [], "servers": [], "lock": None,
                                                         "copies": [], "code_root": "", "pid": 1})
    real, calls = cards.board, []
    monkeypatch.setattr(cards, "board", lambda: calls.append(1) or real())
    cards.new("第一张", files=["a.py"])
    client.get("/dev")
    client.get("/dev")
    assert len(calls) == 1
    cards.new("第二张", files=["b.py"])
    assert "第二张" in client.get("/dev").text and len(calls) == 2
