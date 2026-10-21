"""What is where: versions, worktrees, running servers and apps, launchable copies, the integration lock.

Read-only; used by `rl dev status` and the 开发 page. `git`, `dirty` and `_counts` keep no timeout by default,
because flow.py and autofix.py also use them for merges and pushes. The overview's own reads are bounded
(card 0164): each subprocess has a short timeout, and a worktree whose git does not answer reads as unknown,
never as clean. The per-worktree calls run in parallel, and the overview is cached for OVERVIEW_TTL
seconds, keyed by the git metadata mtimes. `lsregister -dump` takes about 8 s, so the app-copies list is
refreshed in the background and kept for COPIES_TTL.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from ..config import APP_VERSION, get_paths

INSTALLED = Path("/Applications/Research Lab.app")
LSREGISTER = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
CONTENT_PREFIXES = ("_artifacts/", "_data/", "_harvests/", "projects/", "assets/research/", "dev/")
GIT_TIMEOUT = 5.0         # per git call in the overview
PROC_TIMEOUT = 3.0        # per lsof / ps call
COPIES_TIMEOUT = 20.0     # lsregister -dump
MAX_WORKERS = 8           # parallel git calls; more only adds contention on a machine already running checks
MAX_SERVERS = 40          # listeners looked up in detail
OVERVIEW_TTL = 30.0
COPIES_TTL = 600.0


def _proc(cmd: list[str], timeout: float | None, **kw: Any) -> subprocess.CompletedProcess | None:
    """None when the command is missing or ran past its timeout."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_out(args: tuple[str, ...], cwd: Path | None, timeout: float | None) -> subprocess.CompletedProcess | None:
    # GIT_OPTIONAL_LOCKS=0: `git status` would otherwise take index.lock to refresh the index, and could
    # collide with a commit an agent is making in that worktree.
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"} if timeout is not None else None
    return _proc(["git", "-C", str(cwd or main_root()), *args], timeout, env=env)


def git(*args: str, cwd: Path | None = None, check: bool = False, raw: bool = False,
        timeout: float | None = None) -> str:
    r = _git_out(args, cwd, timeout)
    if r is None:
        if check:
            raise RuntimeError(f"git {' '.join(args)}: 没有在 {timeout} 秒内结束")
        return ""
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    if r.returncode != 0:
        return ""
    return r.stdout.rstrip("\n") if raw else r.stdout.strip()  # porcelain lines start with a space


def main_root() -> Path:
    """The main checkout: content root and integration branch."""
    return get_paths().root


def code_root() -> Path:
    """The checkout this code runs from (a worktree, or the main checkout)."""
    return Path(__file__).resolve().parents[3]


def lock_path() -> Path:
    common = git("rev-parse", "--git-common-dir") or ".git"
    p = Path(common)
    return (p if p.is_absolute() else main_root() / p) / "rl-integration.lock"


def read_lock() -> dict[str, Any] | None:
    try:
        return json.loads(lock_path().read_text())
    except (OSError, ValueError):
        return None


RESERVED_PORTS = (4100, 4180)      # the main checkout's `rl serve` and the installed Mac app


def _crc_table() -> list[int]:
    table = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7 if c & 0x80000000 else c << 1) & 0xFFFFFFFF
        table.append(c)
    return table


_CRC = _crc_table()


def cksum(data: bytes) -> int:
    """POSIX `cksum`'s CRC (the octets, then the length, complemented), so ports need no subprocess per name."""
    crc = 0
    for b in data:
        crc = _CRC[((crc >> 24) ^ b) & 0xFF] ^ ((crc << 8) & 0xFFFFFFFF)
    n = len(data)
    while n:
        crc = _CRC[((crc >> 24) ^ n) & 0xFF] ^ ((crc << 8) & 0xFFFFFFFF)
        n >>= 8
    return ~crc & 0xFFFFFFFF


def port_of(name: str) -> int:
    """Same port as scripts/agent-worktree.sh: 4101 + POSIX cksum(name) mod 80, with 4180 (the Mac
    app's) moved to 4181."""
    port = 4101 + cksum(name.encode()) % 80
    return 4181 if port in RESERVED_PORTS else port


def _counts(left: str, right: str, timeout: float | None = None) -> tuple[int, int]:
    out = git("rev-list", "--left-right", "--count", f"{left}...{right}", timeout=timeout)
    try:
        a, b = out.split()
        return int(a), int(b)
    except ValueError:
        return 0, 0


def _branch_counts(timeout: float | None = GIT_TIMEOUT) -> dict[str, tuple[int, int]]:
    """(behind, ahead) of main for every local branch, in one call (%(ahead-behind), git 2.41+). Empty when
    the git is older or main is missing; callers then count per worktree."""
    out = git("for-each-ref", "--format=%(refname)%09%(ahead-behind:main)", "refs/heads", timeout=timeout)
    res = {}
    for line in out.splitlines():
        ref, _, ab = line.partition("\t")
        try:
            ahead, behind = (int(x) for x in ab.split())
        except ValueError:
            continue
        res[ref.removeprefix("refs/heads/")] = (behind, ahead)
    return res


def dirty(cwd: Path, timeout: float | None = None) -> dict[str, Any]:
    """Uncommitted paths, split into code and research content. A git status that errors (a broken worktree)
    or, with a timeout, does not finish adds unknown=True: the worktree was not checked, which is not the same
    as clean. Callers that act on cleanliness must treat unknown as dirty; flow's `any(dirty(wt).values())`
    does."""
    r = _git_out(("status", "--porcelain", "--untracked-files=normal"), cwd, timeout)
    if r is None or r.returncode != 0:
        return {"code": [], "content": [], "unknown": True}
    code, content = [], []
    for line in r.stdout.rstrip("\n").splitlines():
        path = line[3:].strip().strip('"')
        if path == ".rl-content-root":
            continue
        (content if path.startswith(CONTENT_PREFIXES) else code).append(path)
    return {"code": code, "content": content}


def _parse_worktrees(text: str) -> list[dict[str, Any]]:
    out, cur = [], {}
    for line in text.splitlines() + [""]:
        if not line:
            if cur:
                out.append(cur)
            cur = {}
        elif line.startswith("worktree "):
            cur["path"] = line[9:]
        elif line.startswith("HEAD "):
            cur["head"] = line[5:12]
        elif line.startswith("branch "):
            cur["branch"] = line[7:].removeprefix("refs/heads/")
    return out


def worktrees(timeout: float | None = GIT_TIMEOUT, workers: int = MAX_WORKERS) -> list[dict[str, Any]]:
    """One `git worktree list`, one for-each-ref for every branch's distance from main, then a git status
    per worktree, `workers` at a time."""
    out = _parse_worktrees(git("worktree", "list", "--porcelain", timeout=timeout))
    counts = _branch_counts(timeout) if len(out) > 1 else {}
    main = str(main_root())

    def fill(w: dict[str, Any]) -> None:
        p = Path(w["path"])
        w["is_main"] = w["path"] == main
        name = p.name.removeprefix("research-lab.")
        w["name"] = "main" if w["is_main"] else name
        w["port"] = 4100 if w["is_main"] else port_of(name)
        if w["is_main"]:
            w["behind"], w["ahead"] = 0, 0
        elif w.get("branch") in counts:
            w["behind"], w["ahead"] = counts[w["branch"]]
        else:                                                    # detached, or a git without ahead-behind
            w["behind"], w["ahead"] = _counts("main", w.get("branch") or w.get("head", ""), timeout)
        w["dirty"] = dirty(p, timeout) if p.exists() else {"code": [], "content": []}

    if out:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(out))), thread_name_prefix="rl-worktree") as ex:
            list(ex.map(fill, out))
    return out


def build_stamp(app: Path) -> dict[str, Any] | None:
    try:
        return json.loads((app / "Contents/Resources/research_lab/web/static/build.json").read_text())
    except (OSError, ValueError):
        return None


def versions(timeout: float | None = GIT_TIMEOUT) -> dict[str, Any]:
    root = main_root()
    head = git("log", "-1", "--format=%h %s", timeout=timeout)
    behind, ahead = _counts("origin/main", "main", timeout)
    installed = build_stamp(INSTALLED) if INSTALLED.exists() else None
    main_sha = git("rev-parse", "--short", "main", timeout=timeout)
    version_file = (root / "pyproject.toml").read_text() if (root / "pyproject.toml").exists() else ""
    ver = next((l.split('"')[1] for l in version_file.splitlines() if l.startswith("version = ")), APP_VERSION)
    return {"version": ver, "head": head, "main_sha": main_sha, "ahead_of_origin": ahead, "behind_origin": behind,
            "tag": git("describe", "--tags", "--abbrev=0", timeout=timeout), "installed": installed,
            "installed_current": bool(installed and installed.get("commit") == main_sha and not installed.get("dirty"))}


def servers(timeout: float = PROC_TIMEOUT, limit: int = MAX_SERVERS) -> list[dict[str, Any]]:
    """Processes listening on 4100–4199 (rl serve / desktop app) and the checkout they run from: one lsof for
    the listeners, then one ps and one lsof for all of them, each bounded by `timeout`."""
    r = _proc(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"], timeout)
    found, pid = {}, None
    for line in (r.stdout if r else "").splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("n") and pid:
            port = line.rsplit(":", 1)[-1]
            if port.isdigit() and 4100 <= int(port) <= 4199:
                found[pid] = int(port)
    listeners = sorted(found.items(), key=lambda x: x[1])[:limit]
    if not listeners:
        return []
    pids = ",".join(p for p, _ in listeners)
    ps = _proc(["ps", "-o", "pid=,command=", "-p", pids], timeout)
    cmds = {}
    for line in (ps.stdout if ps else "").splitlines():
        p, _, cmd = line.strip().partition(" ")
        cmds[p] = cmd.strip()
    lc = _proc(["lsof", "-a", "-p", pids, "-d", "cwd", "-Fpn"], timeout)
    cwds, cur = {}, None
    for line in (lc.stdout if lc else "").splitlines():
        if line.startswith("p"):
            cur = line[1:]
        elif line.startswith("n") and cur:
            cwds.setdefault(cur, line[1:])
    out = []
    for p, port in listeners:
        cmd = cmds.get(p, "")
        kind = "app" if ".app/Contents/MacOS" in cmd else "serve"
        out.append({"pid": int(p), "port": port, "kind": kind, "cwd": cwds.get(p, ""), "command": cmd[:160]})
    return out


# ── app copies: slow, so refreshed in the background ─────────────────────────────────────────────────────────

def _scan_copies() -> list[str]:
    r = _proc([LSREGISTER, "-dump"], COPIES_TIMEOUT)
    if r is None:
        return []
    paths = set()
    for line in r.stdout.splitlines():
        if line.startswith("path:") and "Research Lab" in line and ".app" in line:
            paths.add(line[5:].strip().rsplit(" (0x", 1)[0])
    return sorted(paths)


_copies: dict[str, Any] = {"at": 0.0, "key": None, "value": None, "thread": None}
_copies_lock = threading.Lock()


def _refresh_copies(key: Any) -> None:
    value = _scan_copies()
    with _copies_lock:
        _copies.update(at=time.monotonic(), key=key, value=value)


def launchable_copies(wait: float | None = None) -> list[str] | None:
    """Research Lab copies LaunchServices can start. A fresh list (under COPIES_TTL, and the installed app
    unchanged) comes back at once. Otherwise a background refresh starts and this waits up to `wait` seconds
    (None: until it is done). It then returns the newest list it has: the previous one, or None while the very
    first lookup is still running."""
    key = _mtime(INSTALLED)
    with _copies_lock:
        if _copies["value"] is not None and _copies["key"] == key and time.monotonic() - _copies["at"] < COPIES_TTL:
            return _copies["value"]
        t = _copies["thread"]
        if t is None or not t.is_alive():
            t = threading.Thread(target=_refresh_copies, args=(key,), name="rl-app-copies", daemon=True)
            _copies["thread"] = t
            t.start()
    t.join(wait)
    with _copies_lock:
        return _copies["value"]


# ── the overview and its cache ───────────────────────────────────────────────────────────────────────────────

def _mtime(p: Path) -> int | None:
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return None


def _tree_mtime(p: Path) -> int | None:
    """The newest mtime under a directory: refs live in nested folders (refs/heads/agent/…)."""
    best = _mtime(p)
    if best is None:
        return None
    for dirpath, _, files in os.walk(p):
        for name in [dirpath, *(os.path.join(dirpath, f) for f in files)]:
            m = _mtime(Path(name))
            if m is not None and m > best:
                best = m
    return best


_common_dirs: dict[str, Path] = {}


def _common_dir() -> Path:
    root = str(main_root())
    if root not in _common_dirs:
        _common_dirs[root] = lock_path().parent
    return _common_dirs[root]


def overview_key() -> tuple:
    """What the overview depends on that a stat can see: worktrees added or removed, branch tips and tags,
    main's HEAD, fetches, the integration lock, the installed build. Uncommitted edits are deliberately left
    out: the indexes change on every `git add` or `git status` some agent runs, which would defeat the cache.
    OVERVIEW_TTL bounds how stale the 未提交 column gets."""
    c = _common_dir()
    return (str(main_root()), str(code_root()), _mtime(c / "worktrees"), _tree_mtime(c / "refs"),
            *(_mtime(c / n) for n in ("HEAD", "packed-refs", "FETCH_HEAD", "rl-integration.lock")),
            _mtime(INSTALLED / "Contents/Resources/research_lab/web/static/build.json"))


_MISS = object()
_memo: dict[str, tuple[float, Any, Any]] = {}
_memo_lock = threading.Lock()
_compute_lock = threading.Lock()


def cached(name: str, key: Any, ttl: float, compute: Callable[[], Any]) -> Any:
    """compute() at most once per `ttl` seconds while `key` stays the same. Concurrent callers wait for the one
    computing instead of each running it (a page check and Leo opening 开发 at once)."""
    def hit() -> Any:
        with _memo_lock:
            m = _memo.get(name)
            return m[2] if m and m[1] == key and time.monotonic() - m[0] < ttl else _MISS
    if (v := hit()) is not _MISS:
        return v
    with _compute_lock:
        if (v := hit()) is not _MISS:
            return v
        value = compute()
        with _memo_lock:
            _memo[name] = (time.monotonic(), key, value)
        return value


def clear_cache() -> None:
    with _memo_lock:
        _memo.clear()
    _common_dirs.clear()


def _compute_overview() -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="rl-overview") as ex:
        v, w, s = ex.submit(versions), ex.submit(worktrees), ex.submit(servers)
        return {"versions": v.result(), "worktrees": w.result(), "servers": s.result(),
                "code_root": str(code_root()), "pid": os.getpid()}


def overview(include_copies: bool = True, *, copies_wait: float | None = None) -> dict[str, Any]:
    """versions, worktrees, servers (cached, see `cached`), plus the lock read fresh and the app copies (see
    `launchable_copies`; copies_wait=None waits for the list, as `rl dev status` does)."""
    base = cached("overview", overview_key(), OVERVIEW_TTL, _compute_overview)
    return {**base, "lock": read_lock(), "copies": launchable_copies(copies_wait) if include_copies else None}
