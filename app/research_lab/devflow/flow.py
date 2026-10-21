"""The four steps as commands: claim (coder), check (tester), review (reviewer), integrate and release
(integrator). Each step writes its result into the card, so the next session picks up from the file.

Rules enforced here, not left to memory:
- a card can only be claimed when no active card declared the same files;
- a reviewer is never the session that owns (wrote) the card;
- integrate needs a passing check and a passing review, and only fast-forwards main;
- release builds from a tag and is the only path into /Applications.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx

from . import cards
from .status import RESERVED_PORTS, code_root, dirty, git, main_root, port_of, read_lock, lock_path

SMOKE_PAGES = ("/", "/papers", "/data", "/artifacts", "/chat", "/chat/jobs", "/feed", "/settings", "/dev")
PAGE_TIMEOUT = 30.0

# Test and review scale with the change. L0 小改: styling, copy, docs, config, small → page smoke only, no
# review. L1 普通: everything else → full tests, no review (Leo 10-10). L2 重要: data, AI guardrails,
# research-content writers, packaging, security, or large → full tests, a review with checklist notes.
LEVELS = cards.LEVELS
LEVEL_LABEL = {"L0": "小改", "L1": "普通", "L2": "重要"}
L0_SUFFIXES = (".css", ".md", ".txt", ".yml", ".yaml", ".html", ".svg")
L0_MAX_LINES = 60
TEST_NET_LOSS = 5
L2_MAX_LINES = 400
L2_PATHS = ("app/research_lab/db/**", "app/research_lab/ai/**", "app/research_lab/agent/**",
            "app/research_lab/files/**", "app/research_lab/devflow/**", "app/research_lab/web/routes/mcp.py",
            "app/research_lab/web/middleware.py", "app/research_lab/config.py", "app/research_lab/desktop.py",
            "packaging/**", "scripts/**", "pyproject.toml", "uv.lock")
GIT_USER = os.environ.get("RL_GIT_USER", "zhanyuanyao-art")


class FlowError(RuntimeError):
    pass


# A jail turns (command, extra env) into the (command, full env) actually run. check() takes one so the small-bug
# autofix can run the tests and the page-check server, which execute model-written code, in a sandbox with a
# whitelisted environment (devflow/autofix.py). Without one, a command runs as is with this process's environment.
Jail = Callable[[list[str], dict[str, str]], tuple[list[str], dict[str, str]]]


def _jailed(cmd: list[str], env: dict | None, jail: Jail | None) -> tuple[list[str], dict[str, str]]:
    return jail(cmd, dict(env or {})) if jail else (cmd, {**os.environ, **(env or {})})


def _run(cmd: list[str], cwd: Path, env: dict | None = None, timeout: int = 900,
         jail: Jail | None = None) -> subprocess.CompletedProcess:
    cmd, full_env = _jailed(cmd, env, jail)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=full_env)


def classify(changed: list[tuple[str, int, int]]) -> tuple[str, list[str]]:
    """(level, reasons) for a change given (path, added, deleted) per file."""
    total = sum(a + d for _, a, d in changed)
    risky = [p for p, _, _ in changed if cards.overlaps([p], list(L2_PATHS))]
    # a test file that loses more than it gains (a deleted file, or assertions cut) counts as removing tests
    deleted_tests = [p for p, a, d in changed if p.startswith("app/tests/") and d - a >= TEST_NET_LOSS]
    reasons = []
    if risky:
        reasons.append("涉及高风险文件：" + "、".join(risky[:6]))
    if total > L2_MAX_LINES:
        reasons.append(f"改动 {total} 行，超过 {L2_MAX_LINES}")
    if deleted_tests:
        reasons.append("删除了测试：" + "、".join(deleted_tests))
    if reasons:
        return "L2", reasons
    if changed and all(p.endswith(L0_SUFFIXES) for p, _, _ in changed) and total <= L0_MAX_LINES:
        return "L0", [f"只改样式、文案或配置，共 {total} 行"]
    return "L1", [f"{len(changed)} 个文件、{total} 行"]


def higher(a: str | None, b: str | None) -> str:
    return max((x for x in (a, b) if x in LEVELS), key=LEVELS.index, default="L1")


def changes_of(wt: Path) -> list[tuple[str, int, int]]:
    """What the worktree changes since it forked from main: committed and uncommitted edits, plus new
    files. Comparing with main's tip instead would count main's own newer commits, in reverse."""
    out: dict[str, tuple[int, int]] = {}
    base = git("merge-base", "main", "HEAD", cwd=wt) or "main"
    # -z and quotePath=false: by default git quotes non-ASCII paths ("_data/\346\226\260.yml"), which no
    # pattern matches; renames off, so both sides of a move are listed
    raw = git("-c", "core.quotePath=false", "diff", "--numstat", "-z", "--no-renames", base, cwd=wt, raw=True)
    for entry in raw.split("\0"):
        parts = entry.split("\t", 2)
        if len(parts) == 3:
            a, d, path = parts
            out[path] = (int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0)
    others = git("-c", "core.quotePath=false", "ls-files", "-z", "--others", "--exclude-standard", cwd=wt, raw=True)
    for path in others.split("\0"):
        if path and path != ".rl-content-root" and path not in out:
            try:
                out[path] = (sum(1 for _ in open(wt / path, encoding="utf-8", errors="ignore")), 0)
            except OSError:
                out[path] = (0, 0)
    return [(p, a, d) for p, (a, d) in sorted(out.items())]


def worktree_name(card: cards.Card) -> str:
    return f"c{card.id}-{cards.slugify(str(card.get('title')))[:24]}".rstrip("-")


def push(*refs: str) -> str:
    helper = f'!f() {{ echo username={GIT_USER}; echo "password=$(gh auth token --user {GIT_USER})"; }}; f'
    r = subprocess.run(["git", "-C", str(main_root()), "-c", "credential.helper=", "-c", f"credential.helper={helper}",
                        "push", "origin", *refs], capture_output=True, text=True)
    if r.returncode != 0:
        raise FlowError(f"push failed: {r.stderr.strip()[-400:]}")
    return r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "pushed"


# ---------------------------------------------------------------- integration lock

def _alive(pid: Any) -> bool:
    """False only when no process has that pid; a lock without a readable pid counts as live."""
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, TypeError, ValueError, OverflowError):
        return True
    return True


def acquire_lock(holder: str, purpose: str) -> None:
    current = read_lock()
    # a holder whose process is gone (killed mid-integrate) left the file behind: treat it as unlocked
    if current and current.get("holder") != holder and _alive(current.get("pid")):
        raise FlowError(f"集成锁在 {current.get('holder')} 手里（{current.get('purpose')}，{current.get('since')}）")
    lock_path().write_text(json.dumps({"holder": holder, "purpose": purpose, "pid": os.getpid(),
                                       "since": datetime.now().strftime("%Y-%m-%d %H:%M")}, ensure_ascii=False))


def release_lock(holder: str) -> None:
    current = read_lock()
    if current and current.get("holder") == holder:
        lock_path().unlink(missing_ok=True)


# ---------------------------------------------------------------- coder

def claim(card_id: str, session: str) -> cards.Card:
    card = cards.get(card_id)
    if card.status not in ("planned", "blocked", "coding"):
        raise FlowError(f"卡片 {card.id} 现在是「{cards.LABEL[card.status]}」，不能领取")
    if card.status == "coding" and card.get("owner") not in (None, session):
        raise FlowError(f"卡片 {card.id} 已由 {card.get('owner')} 领取")
    if not card.files:
        raise FlowError("先在卡片里声明要改的文件（files），再领取")
    clash = cards.conflicts(card)
    if clash:
        raise FlowError("这些文件已被其他卡片占用：" + "；".join(f"{c.id} {c.get('owner')}: {', '.join(f)}" for c, f in clash))
    name = worktree_name(card)
    wt = main_root().parent / f"research-lab.{name}"
    if not wt.exists():
        r = _run([str(main_root() / "scripts/agent-worktree.sh"), "new", name], cwd=main_root(), timeout=600)
        if r.returncode != 0:
            raise FlowError(r.stderr.strip() or r.stdout.strip())
    return cards.update(card, status="coding", owner=session, branch=f"agent/{name}", worktree=str(wt))


# ---------------------------------------------------------------- tester

def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def free_port(preferred: int) -> int:
    """The worktree's own port when nothing listens there, else the next free one up to 4199 (never the
    main checkout's or the Mac app's): a page check must hit the server it started, not someone else's."""
    for port in [preferred, *range(preferred + 1, 4200), *range(4101, preferred)]:
        if port not in RESERVED_PORTS and _port_free(port):
            return port
    raise FlowError("4101–4199 没有空闲端口，先关掉一些 rl serve")


def _stop(proc: subprocess.Popen) -> None:
    """End the test server's process group; one that already exited (or was never ours to signal) is fine."""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(timeout=5)


def copy_db(src: Path, dest: Path) -> Path:
    """A consistent snapshot of the database (the WAL included) through SQLite's backup API, safe while
    the app is writing to it."""
    import sqlite3
    from contextlib import closing
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        return dest
    # closing(): `with sqlite3.connect()` only ends the transaction; the handle on the real file must not
    # stay open while the check runs
    with closing(sqlite3.connect(f"file:{src}?mode=ro", uri=True)) as a, closing(sqlite3.connect(dest)) as b:
        a.backup(b)
    return dest


def _smoke(wt: Path, port: int, jail: Jail | None = None) -> list[tuple[str, int | str]]:
    # The test server reads Leo's real content but a copy of his database: a branch's migrations run on the
    # copy and vanish with it, so a migration renamed or dropped before merge never lingers in the real
    # schema_migrations (card 0037). It never runs AI or feed refreshes either.
    # Only SQLite is copied; the stored PDFs are linked in read-only use so a reader page still finds
    # them. Other data (datasets' DuckDB, caches) is absent: a page that needs it belongs in a test, not here.
    from ..config import get_paths
    real = get_paths().data
    with tempfile.TemporaryDirectory(prefix="rl-check-") as tmp:
        copy_db(get_paths().db, Path(tmp) / "lab.sqlite")
        if (real / "papers").is_dir():
            (Path(tmp) / "papers").symlink_to(real / "papers", target_is_directory=True)
        return _smoke_on(wt, port, {"RL_CLAUDE_BIN": "none", "RL_FEEDS_AUTO": "0", "RL_AI_WORKER": "0", "RL_AUTOFIX": "0",
                                    "RL_DATA_DIR": tmp}, jail)


def page_status(url: str, timeout: float = PAGE_TIMEOUT) -> int | str:
    """The page's status code, or the error's name. A timeout is retried once: on a machine running several
    checks at once a slow first render is load, not a broken page (card 0164)."""
    for attempt in (1, 2):
        try:
            return httpx.get(url, timeout=timeout).status_code
        except httpx.TimeoutException as e:
            if attempt == 2:
                return type(e).__name__
        except httpx.HTTPError as e:
            return type(e).__name__
    return "TimeoutException"                                   # not reached


def _smoke_on(wt: Path, port: int, env: dict[str, str], jail: Jail | None = None) -> list[tuple[str, int | str]]:
    port = free_port(port)
    cmd, full_env = _jailed(["uv", "run", "rl", "serve", "--no-open", "--port", str(port)], env, jail)
    proc = subprocess.Popen(cmd, cwd=wt, env=full_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            start_new_session=True)
    results: list[tuple[str, int | str]] = []
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(80):
            if proc.poll() is not None:
                return [("/", "服务没有起来")]
            try:
                if httpx.get(base + "/", timeout=2).status_code:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        for page in SMOKE_PAGES:
            results.append((page, page_status(base + page)))
    finally:
        _stop(proc)
    return results


def _parallel(wt: Path, jail: Jail | None = None) -> list[str]:
    """`-n auto` when the worktree's environment has pytest-xdist: the suite drops from about 4.5 minutes to about
    2 (Leo 10-11: changes must not wait that long). A branch that predates the dependency runs serially. Asked
    through the jail when there is one: starting the worktree's python runs whatever .pth files its .venv holds."""
    r = _run(["uv", "run", "python", "-c", "import xdist"], cwd=wt, timeout=120, **({"jail": jail} if jail else {}))
    return ["-n", "auto"] if r.returncode == 0 else []


TRACEBACK_LINES = 20
TRACEBACK_CHARS = 4000          # and at most this much: twenty lines of a long repr can still be huge
JUNIT_MAX_BYTES = 20_000_000


def fence(text: str) -> str:
    """A Markdown code fence the text cannot close: one backtick more than its longest run, at least four."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(4, longest + 1)


def short_traceback(text: str, wt: Path) -> str:
    """What a report shows of one traceback: its last TRACEBACK_LINES lines, scrubbed, then at most
    TRACEBACK_CHARS characters. The order matters: a cut by characters can land inside a key, and the half left
    over no longer looks like one, so the cut comes after the scrub. A cut by lines cannot split a token."""
    text = scrub("\n".join(text.splitlines()[-TRACEBACK_LINES:]), wt)
    return text if len(text) <= TRACEBACK_CHARS else "…" + text[-(TRACEBACK_CHARS - 1):]


def one_line(text: str, limit: int = 300) -> str:
    """A test's node id as a heading: it comes from a JUnit file the code under test can rewrite, so no line
    breaks or control characters, and not too long."""
    return " ".join("".join(c if c.isprintable() else " " for c in text).split())[:limit]


def _nodeid(classname: str, name: str, wt: Path) -> str:
    """pytest's node id from a JUnit testcase: app.tests.test_x(.TestClass) + test_y[p] → app/tests/test_x.py::…"""
    if not classname:                                   # a collection error names the module only
        return name.replace(".", "/") + ".py"
    parts = classname.split(".")
    for k in range(len(parts), 0, -1):
        path = "/".join(parts[:k]) + ".py"
        if (wt / path).is_file():
            return "::".join([path, *parts[k:], name])
    return f"{classname}::{name}"


def failure_tracebacks(junit: Path, wt: Path) -> dict[str, str]:
    """Node id → each failure's or error's short traceback as pytest wrote it (short_traceback trims and scrubs
    it), read from pytest's JUnit XML: unlike the console, it comes out the same with -n auto (xdist merges the workers' results). The file
    is written by a run of code under test, so it is size-capped; expat ≥ 2.4 refuses entity-amplification
    documents."""
    import xml.etree.ElementTree as ET
    try:
        if junit.stat().st_size > JUNIT_MAX_BYTES:
            return {}
        root = ET.parse(junit).getroot()
    except (OSError, ET.ParseError):
        return {}
    out: dict[str, str] = {}
    for case in root.iter("testcase"):
        for kind in ("failure", "error"):
            for el in case.findall(kind):
                node = _nodeid(case.get("classname") or "", case.get("name") or "", wt)
                text = (el.text or el.get("message") or "").rstrip()
                out[node if node not in out else f"{node} ({kind})"] = text
    return out


def scrub(text: str, wt: Path) -> str:
    """What may go into a report that is pushed to the (private) repository: secret-shaped strings and the exact
    app/.env values out (devflow/autofix.redact), the worktree's and the home folder's paths shortened."""
    from .autofix import redact, secret_values
    for root in sorted({str(wt), str(wt.resolve())}, key=len, reverse=True):
        text = text.replace(root + "/", "")
    return redact(text.replace(str(Path.home()), "~"), secret_values())


def check(card_id: str, *, smoke: bool = True, full: bool = False, jail: Jail | None = None) -> dict[str, Any]:
    card = cards.get(card_id)
    wt = Path(card.get("worktree") or "")
    if not wt.exists():
        raise FlowError(f"卡片 {card.id} 没有 worktree（先 rl dev claim）")
    changes = changes_of(wt)
    auto, reasons = classify(changes)
    level = higher(card.get("level"), auto)   # the planner may raise a card's level, never lower it
    tracebacks: dict[str, str] = {}
    if level == "L0" and not full:
        summary, failed, tests_ok = "小改：跳过单元测试，只检查页面", [], True
    else:
        # the JUnit file lives under tempfile's folder: inside the autofix jail that is the run's temp folder,
        # the only place the tests may write
        junit_dir = Path(tempfile.mkdtemp(prefix="rl-junit-"))
        junit = junit_dir / "junit.xml"
        try:
            # RL_AUTOFIX=0: nothing a test does may start the small-bug autofix pipeline (devflow/autofix.py)
            r = _run(["uv", "run", "pytest", "-q", *_parallel(wt, jail), "--tb=short", "-rfE", f"--junitxml={junit}",
                      "app/tests"], cwd=wt, env={"RL_AUTOFIX": "0"}, timeout=1200, jail=jail)
            # the test name too: a parametrised id can carry whatever the test was given. Two ids that read the
            # same once scrubbed keep both entries, numbered.
            for node, raw in list(failure_tracebacks(junit, wt).items())[:20]:
                key = base = one_line(scrub(node, wt))
                n = 1
                while key in tracebacks:
                    n += 1
                    key = f"{base} ({n})"
                tracebacks[key] = short_traceback(raw, wt)
        finally:
            shutil.rmtree(junit_dir, ignore_errors=True)
        summary = next((l for l in reversed(r.stdout.splitlines()) if " passed" in l or " failed" in l or " error" in l), "no result")
        failed = [scrub(l, wt) for l in r.stdout.splitlines() if l.startswith(("FAILED", "ERROR"))][:20]
        tests_ok = r.returncode == 0
    pages = _smoke(wt, port_of(wt.name.removeprefix("research-lab.")), jail) if smoke or level == "L0" else []
    bad_pages = [(p, c) for p, c in pages if c != 200]
    ok = tests_ok and not bad_pages
    changed = [p for p, _, _ in changes]
    outside = [f for f in changed if card.files and not cards.overlaps([f], card.files)]
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    # The commit the tests ran on, so integrate can refuse a branch that moved afterwards; none when the
    # worktree had uncommitted edits, since then the tested code is not any commit.
    uncommitted = [l for l in git("status", "--porcelain", cwd=wt, raw=True).splitlines()
                   if l.strip() and not l.endswith(".rl-content-root")]
    tested = None if uncommitted else git("rev-parse", "HEAD", cwd=wt) or None
    report = cards.dev_dir() / "reports" / f"{card.id}-test-{datetime.now():%Y%m%d-%H%M}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join([
        f"# 卡片 {card.id} 测试报告 · {stamp}", "", f"结果：{'通过' if ok else '未通过'}",
        f"等级：{LEVEL_LABEL[level]}（{'；'.join(reasons)}）", f"测试：{summary}",
        *(["失败：", *[f"- {l}" for l in failed]] if failed else []),
        "页面：" + "，".join(f"{p} {c}" for p, c in pages) if pages else "页面：未检查",
        f"改动文件：{len(changed)} 个" + (f"；超出声明范围：{', '.join(outside)}" if outside else ""), "",
        *([f"## 失败的回溯（每条最后 {TRACEBACK_LINES} 行）", ""]
          + [f"### {node}\n\n{fence(tb)}text\n{tb}\n{fence(tb)}\n" for node, tb in tracebacks.items()] if tracebacks else [])]),
        encoding="utf-8")
    cards.append_section(card, "测试（tester 填）",
                         f"- {stamp} {'通过' if ok else '未通过'} · {LEVEL_LABEL[level]} · {summary} · 页面 {len(pages) - len(bad_pages)}/{len(pages)}"
                         + (f" · 超出声明范围：{', '.join(outside)}" if outside else "") + f" · 报告 reports/{report.name}")
    cards.update(card, test={"result": "pass" if ok else "fail", "at": stamp, "summary": summary, "level": level,
                             "commit": tested},
                 status="review" if ok else "coding")
    return {"ok": ok, "summary": summary, "failed": failed, "tracebacks": tracebacks, "pages": pages, "outside": outside,
            "report": str(report),
            "level": level, "reasons": reasons}


def level_of(card: cards.Card) -> str:
    return higher(card.get("level"), (card.get("test") or {}).get("level"))


# ---------------------------------------------------------------- reviewer

def review(card_id: str, verdict: str, by: str, notes: str = "") -> cards.Card:
    card = cards.get(card_id)
    if verdict not in ("pass", "changes"):
        raise FlowError("结论只能是 pass 或 changes")
    if by == card.get("owner"):
        raise FlowError("评审必须由另一个会话来做（不能审自己写的卡）")
    if card.status != "review":
        raise FlowError(f"卡片 {card.id} 还没通过测试（现在是「{cards.LABEL[card.status]}」）")
    if level_of(card) == "L2" and not notes.strip():
        raise FlowError("重要改动的评审要写结论：按 dev/README.md 的评审清单逐条说明（--notes）")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    cards.append_section(card, "评审（reviewer 填）", f"- {stamp} {by}：{'通过' if verdict == 'pass' else '需要修改'}"
                         + (f"\n\n{notes.strip()}" if notes.strip() else ""))
    return cards.update(card, review={"verdict": verdict, "by": by, "at": stamp},
                        status="review" if verdict == "pass" else "coding")


# ---------------------------------------------------------------- integrator

def _releases() -> Path:
    return cards.dev_dir() / "RELEASES.md"


def _note_unreleased(line: str) -> None:
    """Newest first under 「未发布」; `release` turns that block into the version's section."""
    p = _releases()
    text = p.read_text(encoding="utf-8") if p.exists() else "# 版本记录\n"
    marker = "## 未发布\n"
    if marker not in text:
        text = text.rstrip() + "\n\n" + marker
    i = text.index(marker) + len(marker)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = text[:i] + f"\n- {line}" + text[i:]
    p.write_text(re.sub(r"([^\n])\n(## )", r"\1\n\n\2", text), encoding="utf-8")


RECORD_PREFIXES = ("dev/", "docs/")   # cards, reports, release notes, design notes: nothing tests read


def records_only_behind(branch: str, cwd: Path | None = None) -> list[str] | None:
    """What main changed since `branch` forked, when every change is a record (RECORD_PREFIXES);
    None when main moved in code too. An empty list means the branch is already on top of main."""
    base = git("merge-base", "main", branch, cwd=cwd)
    if not base:
        return None
    files = [f for f in git("diff", "--name-only", f"{base}..main", cwd=cwd, raw=True).splitlines() if f]
    return files if all(f.startswith(RECORD_PREFIXES) for f in files) else None


def _hold(holder: str, purpose: str, lock_held: bool) -> None:
    """Take the integration lock, or with lock_held check that the caller already has it (and leave it to them:
    the small-bug autofix holds it from integrate through release, so nobody slips in between)."""
    if not lock_held:
        acquire_lock(holder, purpose)
        return
    current = read_lock()
    if not current or current.get("holder") != holder:
        raise FlowError(f"集成锁不在 {holder} 手里" + (f"（在 {current.get('holder')} 手里）" if current else ""))


def integrate(card_id: str, holder: str, *, force: bool = False, do_push: bool = True, lock_held: bool = False) -> str:
    card = cards.get(card_id)
    branch = str(card.get("branch") or "")
    if not branch:
        raise FlowError(f"卡片 {card.id} 没有分支")
    test, rev = card.get("test") or {}, card.get("review") or {}
    # Leo 10-10: an ordinary change merges once the full tests pass; only L2 (guardrails, migrations, the dev
    # flow, packaging, >400 lines, deleted tests) waits for another session's review. A review that asked for
    # changes still holds any level until a reviewer looks again.
    needs_review = level_of(card) == "L2"
    if not force and test.get("result") != "pass":
        raise FlowError("需要先测试通过（rl dev check）")
    if not force and rev.get("verdict") == "changes":
        raise FlowError("评审要求修改：改完后请评审再看一次（rl dev review）")
    if not force and needs_review and rev.get("verdict") != "pass":
        raise FlowError(f"{LEVEL_LABEL[level_of(card)]}改动需要另一个会话评审通过（rl dev review）后才能合并")
    # With L1 unreviewed, the test record is the only gate: it must be for the commit about to merge.
    if not force and (not test.get("commit") or test["commit"] != git("rev-parse", branch)):
        raise FlowError("合并的代码和测过的不一样（测试后有新提交，或测试时有未提交的改动）：提交后重跑 rl dev check")
    main_dirty = dirty(main_root())
    if main_dirty.get("unknown"):           # git errored on the main checkout: not known to be clean, so not clean
        raise FlowError("读不到主仓库的 git 状态，没法确认有没有未提交的代码改动，不能合并；先在主仓库里跑 git status 查原因")
    if main_dirty["code"]:
        raise FlowError("主仓库里有未提交的代码改动，先处理（研究内容不受影响）：" + ", ".join(main_dirty["code"][:8]))
    _hold(holder, f"integrate {card.id}", lock_held)
    try:
        if subprocess.run(["git", "-C", str(main_root()), "merge-base", "--is-ancestor", "main", branch]).returncode != 0:
            # main moved only in records (often this session's own dev/ commits): the tested code is the
            # same, so rebase the branch here instead of sending its author back for another check
            wt = Path(card.get("worktree") or "")
            if records_only_behind(branch) is None or not wt.exists() or any(dirty(wt).values()):
                raise FlowError(f"{branch} 不是基于最新的 main：在它的 worktree 里 `git rebase main`，再跑一次 rl dev check")
            r = subprocess.run(["git", "-C", str(wt), "rebase", "-q", "main"], capture_output=True, text=True)
            if r.returncode != 0:
                subprocess.run(["git", "-C", str(wt), "rebase", "--abort"], capture_output=True)
                raise FlowError(f"{branch} 自动 rebase 失败，请在它的 worktree 里手动 rebase：{r.stderr.strip()[-200:]}")
            if do_push:   # keep origin's copy in step, or the author's next push of the branch is refused
                try:
                    push("--force-with-lease", branch)
                except FlowError:
                    pass
        git("merge", "--ff-only", branch, check=True)
        msg = push("main") if do_push else "not pushed"
        cards.update(card, status="done")
        _note_unreleased(f"{card.get('title')}（卡片 {card.id}）")
        return msg
    finally:
        if not lock_held:
            release_lock(holder)


def commit_dev(message: str) -> str:
    """Commit only dev/ in the main checkout (cards, reports, RELEASES.md)."""
    if not git("status", "--porcelain", "--", "dev"):
        return "dev/ 没有改动"
    git("add", "dev", check=True)
    git("commit", "-q", "-m", message, "--", "dev", check=True)
    return git("log", "-1", "--format=%h %s")


def release(version: str, holder: str, *, install: bool = True, do_push: bool = True, lock_held: bool = False) -> list[str]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise FlowError("版本号格式：0.5.0")
    root = main_root()
    main_dirty = dirty(root)
    if main_dirty.get("unknown"):
        raise FlowError("读不到主仓库的 git 状态，没法确认有没有未提交的代码改动，不能发版；先在主仓库里跑 git status 查原因")
    if main_dirty["code"]:
        raise FlowError("主仓库里有未提交的代码改动，不能发版")
    _hold(holder, f"release {version}", lock_held)
    steps = []
    try:
        for rel in ("pyproject.toml", "app/research_lab/config.py"):
            p = root / rel
            text = p.read_text(encoding="utf-8")
            text = re.sub(r'^version = "[^"]+"', f'version = "{version}"', text, count=1, flags=re.M) if rel == "pyproject.toml" \
                else re.sub(r'APP_VERSION = "[^"]+"', f'APP_VERSION = "{version}"', text, count=1)
            p.write_text(text, encoding="utf-8")
        _run(["uv", "lock", "--quiet"], cwd=root)
        notes = _releases()
        if notes.exists():
            text = notes.read_text(encoding="utf-8")
            notes.write_text(text.replace("## 未发布\n", f"## 未发布\n\n## v{version} · {datetime.now():%Y-%m-%d}\n", 1), encoding="utf-8")
        git("add", "pyproject.toml", "uv.lock", "app/research_lab/config.py", "dev", check=True)
        git("commit", "-q", "-m", f"Release v{version}", "--", "pyproject.toml", "uv.lock", "app/research_lab/config.py", "dev", check=True)
        git("tag", f"v{version}", check=True)
        steps.append(f"commit + tag v{version}")
        if do_push:
            steps.append(push("main", f"v{version}"))
        r = _run([str(root / "packaging/macos/build.sh"), "--ref", f"v{version}"], cwd=root, timeout=1800)
        if r.returncode != 0:
            raise FlowError("打包失败：" + (r.stderr or r.stdout)[-600:])
        steps.append(r.stdout.strip().splitlines()[-1][:160])
        if install:
            r = _run([str(root / "packaging/macos/install.sh")], cwd=root, timeout=300)
            if r.returncode != 0:
                raise FlowError("安装失败：" + (r.stderr or r.stdout)[-600:])
            steps.append(r.stdout.strip().splitlines()[-1][:160] if r.stdout.strip() else "installed")
        return steps
    finally:
        if not lock_held:
            release_lock(holder)
