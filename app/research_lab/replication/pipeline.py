"""一键跑完 (card 0195, Leo 10-19): every step of a replication in one background run, so that only two kinds of things
reach Leo: a data file the lab cannot get by itself (he obtains it and uploads it) and a system or R dependency (he
installs it with the command shown). Everything else the run solves itself or works around, and a step that does not
depend on what is stuck still runs.

The steps, in order (STEPS); each is the existing function the plan page's buttons call:

  fetch    取复现包        each public replication link of the plan is listed (fetch.list_source, as the fetch panel's List
                           files). With no package saved yet, the default pick is downloaded as fetch_auto would (the data
                           the main scripts read, within the size caps); in every run, every author script and README /
                           codebook the package lists and the workspace lacks is saved too, as text only, so that coverage can
                           map each table to a script that is present
  scan     扫描表图        coverage.rescan, when the plan has no 全书复现清单 yet or new scripts just arrived
  missing  待补数据        what the main scripts read and the lab lacks (missing.report). A file the package lists and the lab
                           can import (never downloaded, or over the default pick's size but under the cap) is fetched first
                           (fetch.fetch); every file still missing becomes a data need
  install  准备环境        rrun.install, in this thread under rrun's own lock (rrun.Step); a package that will not build
                           becomes a dependency need named by its root cause (rrun.InstallLog: lme4 → nloptr → cmake: brew
                           install cmake), one need per root cause, the packages that only depend on it listed with it; a
                           package CRAN no longer lists is installed from the CRAN archive by the install script itself.
                           No R on this Mac is a dependency need too
  run      运行作者代码    rrun.run, same lock; rrun drops what did not install by rule and may call its repair session
  compare  对照            workspace.compare_run on the run just made, without the targets review (Leo 10-19: a number R
                           reproduces exactly confirms its own extraction; a mismatch is reported, not held for approval)
  report   报告            the AI job replication.report_text (card 0196) when it is registered: queued, dispatched, waited
                           for up to REPORT_WAIT_S; its apply() writes <workspace>/exports/ai_text.json
  deck     展示稿          deck.make, which builds the report and the slides
  export   导出数据文件夹  export.export_folder

A step that raises is recorded as failed and the next one starts. Leo's Stop on the R card ends the run: the steps after
it are skipped and the run's state is `stopped`. A command shown with a need is always one of COMMANDS or a `brew install`
of renv.FORMULAS only (renv.brew_command), never text lifted from an install log; the page gets at most MAX_DEP_NEEDS +
MAX_DATA_NEEDS needs, names cut to a line, the rest counted.

Needs name the tables and figures they hold up: the scripts a need stops are mapped to 全书复现清单 items (coverage item
scripts[].path, through the scripts each main script source()s, one walk over a graph built with one parse per script),
minus the items the run produced anyway (a table printed before the script stopped). After the run a need that stopped
nothing is dropped: a missing file only the scripts that ran fine read, a package nothing failed for.

State: <plan_root>/logs/pipeline.json, written atomically after every change, read by state():

  {"plan_id", "state": running | done | needs | partial | stopped, "started", "ended", "current": step key or null,
   "steps": [{"key", "label", "state": waiting | running | done | blocked | failed | skipped, "detail", "started", "ended"}],
   "needs": [{"kind": "dependency", "title", "why", "affects": [labels], "affects_more", "command", "packages",
              "packages_more"},
             {"kind": "data", "key", "uid", "title", "why", "where", "affects": [labels], "affects_more", "upload": true,
              "scripts"}],
   "needs_more": needs not listed, "run_id": the R run's record or null}

pipeline.json is never larger than state() reads (STATE_CHARS): details, then the needs, are dropped first. The
run's state `needs` means at least one need is left; `partial`, no need but a step failed or was blocked (R busy
elsewhere, a run that cannot be compared, a script that failed for its own reasons). state() adds `running` and reports a file that
says running while nobody holds the lock as `interrupted`.

One run per plan: an exclusive flock on <plan_root>/logs/pipeline.lock, held by the run's thread and released by the
system if the process dies.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import posixpath
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from . import coverage, deck, export, fetch, missing, package, plans, renv, rrun
from . import workspace as W
from .rtext import MAX_VISIT, walk_sources

log = logging.getLogger("research_lab.replication.pipeline")

STEPS = (("fetch", "取复现包"), ("scan", "扫描表图"), ("missing", "待补数据"), ("install", "准备环境"),
         ("run", "运行作者代码"), ("compare", "对照"), ("report", "报告"), ("deck", "展示稿"), ("export", "导出数据文件夹"))
STEP_STATES = ("waiting", "running", "done", "blocked", "failed", "skipped")
STATE_FILE = "logs/pipeline.json"
LOCK_FILE = "logs/pipeline.lock"
REPORT_JOB = "replication.report_text"
REPORT_FILE = "exports/ai_text.json"
REPORT_WAIT_S = 5 * 60                 # how long the report step waits for its AI job
REPORT_POLL_S = 2.0
MAX_LINKS = 3                          # replication links listed per run
FETCH_CODE_FILES = 300                 # author scripts saved per run (what coverage and rrun read at most) …
FETCH_CODE_BYTES = W.AUTHOR_BYTES      # … and their bytes, a script listed without a size counted as the largest read
FETCH_DOCS = 8                         # README / codebook files saved per run …
FETCH_DOC_BYTES = fetch.DOC_BYTES      # … and their bytes, unsized ones counted at fetch's cap for their kind
R_WAIT_S = 10 * 60                     # how long the run waits for an R step started elsewhere (a click on 复现工作区)
R_POLL_S = 2.0
MAX_DATA_NEEDS = 30                    # data needs listed; the rest are counted
MAX_DEP_NEEDS = 20                     # dependency needs listed; the rest are counted
MAX_PACKAGES = 12                      # package names shown per dependency need; the rest are counted
NAME_CHARS = 60                        # a package, header or file name as the page shows it
MAX_AFFECTS = 12                       # labels listed per need; the rest are counted
DETAIL_CHARS = 140
FETCH_FILES = 20                       # listed files the missing step fetches by itself in one run …
FETCH_BYTES = 1024 * fetch.MB          # … and the most bytes they may add up to (an unsized file counts as the cap)
STATE_CHARS = 2_000_000
INSTALL_LOG_CHARS = 200_000            # of rinstall.log read to tell "no network" from "not on CRAN"
R_INSTALL = "brew install r"

# A system header a package's build could not find → the Homebrew formula that ships it.
HEADER_FORMULA = {
    "gdal.h": "gdal", "gdal_priv.h": "gdal", "cpl_conv.h": "gdal", "proj.h": "proj", "proj_api.h": "proj",
    "geos_c.h": "geos", "ft2build.h": "freetype", "png.h": "libpng", "jpeglib.h": "jpeg-turbo", "tiffio.h": "libtiff",
    "gmp.h": "gmp", "mpfr.h": "mpfr", "gsl": "gsl", "fftw3.h": "fftw", "hdf5.h": "hdf5", "udunits2.h": "udunits",
    "glpk.h": "glpk", "sodium.h": "libsodium", "uv.h": "libuv", "zmq.h": "zeromq", "Magick++.h": "imagemagick",
    "cairo.h": "cairo", "hb.h": "harfbuzz", "harfbuzz": "harfbuzz", "fribidi.h": "fribidi", "fribidi": "fribidi",
    "libxml": "libxml2", "openssl": "openssl@3", "curl": "curl", "git2.h": "libgit2", "netcdf.h": "netcdf",
    "unicode": "icu4c", "poppler": "poppler", "sqlite3.h": "sqlite", "zstd.h": "zstd", "lzma.h": "xz",
    "hb-ft.h": "harfbuzz", "poppler-document.h": "poppler", "fontconfig.h": "fontconfig", "webp": "webp",
}
# Every command the page may show. Each one is a constant of this module: nothing of an install log or of R's output is
# ever copied into a command, since that text comes from third-party builds.
COMMANDS = frozenset({"brew install cmake", "brew install gcc", "brew install pkgconf", "xcode-select --install", R_INSTALL,
                      *(f"brew install {f}" for f in HEADER_FORMULA.values())})
_ROOT = re.compile(r"依赖 ([A-Za-z][A-Za-z0-9.]{0,59}) 没装上：")
_PAREN = re.compile(r"（[^（）\n]{0,200}）")
_IDS = re.compile(r"\b(?:ART|EXC|J|DS|RUN|RP)-\d[\w-]{0,30}")
_SPACE = re.compile(r"\s+")
_HEADER = re.compile(r"头文件 ([A-Za-z0-9_./+-]{1,80})")
_NO_PKG = re.compile(r"there is no package called ‘([^’\n]{1,60})’")
_NO_FN = re.compile(r'could not find function "([^"\n]{1,60})"')
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9._]*")         # an R name as it can appear in an error: lme4, data.table
_CJK = re.compile(r"[一-鿿]")                 # the lab's own refusals are Chinese; anything else is not shown as is


class PipelineError(ValueError):
    """Refused, in Chinese, as the page shows it."""


def plain(text: Any, limit: int = DETAIL_CHARS) -> str:
    """One short line for the page: no full-width parenthetical asides, no record ids, whitespace folded, capped."""
    s = _IDS.sub("", _PAREN.sub("", str(text or "")[:2000]))
    s = _SPACE.sub(" ", s).strip(" ：，；")
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- one run per plan, and its state file

def _root(plan_id: str) -> Path:
    root = W.plan_root(plan_id)                     # refuses a malformed id
    (root / "logs").mkdir(parents=True, exist_ok=True)
    return W._real(root)


class _Hold:
    """The right to run a plan's pipeline: an exclusive flock on <plan_root>/logs/pipeline.lock."""

    def __init__(self, root: Path):
        self.root, self.fd = root, None

    def take(self) -> bool:
        path = W._checked(self.root, self.root / LOCK_FILE)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def running(plan_id: str) -> bool:
    """A pipeline run of this plan holds its lock, in this process or another."""
    path = W.plan_root(plan_id) / LOCK_FILE
    if not path.is_file() or path.is_symlink():
        return False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def _blank_steps() -> list[dict[str, Any]]:
    return [{"key": k, "label": label, "state": "waiting", "detail": "", "started": None, "ended": None} for k, label in STEPS]


def state(plan_id: str) -> dict[str, Any]:
    """What the page shows: the last run's state file, plus `running`. A run whose file says running but whose lock
    nobody holds died with its process: it is reported as interrupted."""
    root = W.plan_root(plan_id)
    try:
        st = json.loads(W.safe_read(root, STATE_FILE, STATE_CHARS) or "{}") if root.is_dir() else {}
    except (ValueError, OSError, W.WorkspaceError):
        st = {}
    if not isinstance(st, dict):
        st = {}
    st.setdefault("steps", [])
    st.setdefault("needs", [])
    st.setdefault("needs_more", 0)
    alive = running(plan_id)                       # the lock alone says whether a run is going
    if alive and st.get("state") != "running":      # the file is unreadable, or the last run's: a new run starts blank
        st = {"state": "running", "steps": _blank_steps(), "needs": [], "needs_more": 0, "current": None}
    elif alive:
        st["steps"] = st["steps"] or _blank_steps()
    elif st.get("state") == "running":
        st["state"] = "interrupted"
        st["steps"] = [dict(s, state="failed", detail="应用关闭时中断了") if s.get("state") == "running" else s
                       for s in st["steps"]]
    st["running"] = alive
    return st


class _Run:
    """The state of the run in progress, saved to pipeline.json after every change."""

    def __init__(self, plan_id: str, root: Path):
        self.root = root
        self.lock = threading.Lock()
        self.st: dict[str, Any] = {"plan_id": plan_id, "state": "running", "started": _stamp(), "ended": None,
                                   "current": None, "steps": _blank_steps(), "needs": [], "needs_more": 0, "run_id": None}

    def step(self, key: str) -> dict[str, Any]:
        return next(s for s in self.st["steps"] if s["key"] == key)

    def save(self) -> None:
        with self.lock:
            text = json.dumps(self.st, ensure_ascii=False, indent=1)
            if len(text) > STATE_CHARS:                   # state() reads at most this much: slim it rather than cut it
                text = json.dumps(_slim(self.st, keep_needs=True), ensure_ascii=False)
            if len(text) > STATE_CHARS:
                text = json.dumps(_slim(self.st, keep_needs=False), ensure_ascii=False)
            tmp = f"logs/.pipeline.{os.getpid()}.{threading.get_ident()}.tmp"
            W.safe_write(self.root, tmp, text)
            os.replace(self.root / tmp, self.root / STATE_FILE)

    def begin(self, key: str) -> None:
        self.st["current"] = key
        self.step(key).update(state="running", started=_stamp(), detail="")
        self.save()

    def say(self, key: str, text: str) -> None:
        self.step(key)["detail"] = plain(text)
        self.save()

    def end(self, key: str, state_: str, detail: str = "") -> None:
        self.step(key).update(state=state_ if state_ in STEP_STATES else "failed", detail=plain(detail), ended=_stamp())
        self.st["current"] = None
        self.save()

    def needs(self, needs: list[dict[str, Any]], more: int = 0) -> None:
        self.st["needs"], self.st["needs_more"] = needs, more
        self.save()

    def finish(self, stopped: bool = False) -> None:
        steps = self.st["steps"]
        for s in steps:
            if s["state"] in ("waiting", "running"):
                s.update(state="failed", detail=s["detail"] or "没有跑完", ended=_stamp())
        self.st["state"] = ("stopped" if stopped else "needs" if self.st["needs"] else
                            "partial" if any(s["state"] in ("failed", "blocked") for s in steps) else "done")
        self.st.update(ended=_stamp(), current=None)
        self.save()


def _slim(st: dict[str, Any], keep_needs: bool) -> dict[str, Any]:
    """The state without what can grow: step details, and the needs' explanations (or the needs themselves, counted)."""
    keys = ("kind", "key", "uid", "title", "command", "upload")
    needs = [{k: n[k] for k in keys if k in n} | {"affects": [], "affects_more": 0} for n in st["needs"]] if keep_needs else []
    return {**st, "steps": [dict(s, detail="") for s in st["steps"]], "needs": needs,
            "needs_more": st.get("needs_more", 0) + (0 if keep_needs else len(st["needs"]))}


# ---------------------------------------------------------------- scripts → the tables and figures they make

@dataclass
class _Ctx:
    plan_id: str
    items: list[dict[str, Any]] = field(default_factory=list)
    ws: Path | None = None
    r_scripts: list[str] | None = None            # the main R scripts rrun runs; None: not known
    r_off: str = ""                               # why the R steps do not run ('' when they do)
    r_off_state: str = "skipped"
    data_needs: list[dict[str, Any]] = field(default_factory=list)
    data_more: int = 0
    fetched: int = 0                              # datasets the missing step fetched by itself
    new_code: int = 0                             # author scripts the fetch step saved in this run
    dep_needs: list[dict[str, Any]] = field(default_factory=list)
    loaders: dict[str, list[str]] = field(default_factory=dict)     # package → basenames of the R files that load it
    outcome: dict[str, Any] | None = None
    run_id: str | None = None
    stopped: bool = False                         # Leo stopped an R step: the run ends there
    _by_path: dict[str, list[int]] | None = None
    _by_name: dict[str, list[int]] | None = None
    _edges: dict[str, list[str]] | None = None
    _back: dict[str, list[str]] | None = None

    def index(self) -> None:
        self._by_path, self._by_name = {}, {}
        for i, it in enumerate(self.items):
            for s in it.get("scripts") or []:
                p = str(s.get("path") or "")
                if p:
                    self._by_path.setdefault(p, []).append(i)
                    self._by_name.setdefault(posixpath.basename(p).lower(), []).append(i)

    def items_of(self, script: str) -> list[int]:
        if self._by_path is None:
            self.index()
        return self._by_path.get(script) or self._by_name.get(posixpath.basename(script).lower(), [])

    def graph(self) -> dict[str, list[str]]:
        """script → the saved R scripts it source()s directly, each text parsed once. rtext.walk_sources resolves the
        paths (cwd, ../, a bare file name), given every other script as empty text so that it stops one level down."""
        if self._edges is None:
            self._edges, self._back = {}, {}
            try:
                ws = self.ws or rrun.ws_of(self.plan_id)
                index = rrun.FileIndex(rrun._author_files(ws))
                texts = {p: t for p, t in rrun._r_texts(ws).items() if p in index.exact}
                one = dict.fromkeys(texts, "")
                for p, text in texts.items():
                    one[p] = text
                    self._edges[p] = walk_sources(index, p, one)[0][1:]
                    one[p] = ""
            except Exception:  # noqa: BLE001 - the graph is a refinement: each script still names its own items
                log.warning("source graph of %s", self.plan_id, exc_info=True)
            for p, kids in self._edges.items():
                for k in kids:
                    self._back.setdefault(k, []).append(p)
        return self._edges

    @staticmethod
    def _walk(starts: Iterable[str], edges: dict[str, list[str]]) -> dict[str, None]:
        seen = dict.fromkeys(starts)
        queue, i = list(seen), 0
        while i < len(queue) and len(seen) < 50 * MAX_VISIT:
            for k in edges.get(queue[i], ()):
                if k not in seen:
                    seen[k] = None
                    queue.append(k)
            i += 1
        return seen

    def reach(self, scripts: Iterable[str]) -> list[str]:
        """The scripts and every saved R script they load through source(), in one walk over the graph."""
        return list(self._walk(scripts, self.graph()))

    def affects(self, scripts: Iterable[str], produced: set[int] = frozenset()) -> tuple[list[str], int]:
        """The labels of the items the scripts make (through what they source), in the list's order, minus `produced`.
        Without a 全书复现清单 the scripts themselves are named."""
        scripts = list(dict.fromkeys(scripts))
        if not self.items:
            names = [plain(x, 80) for x in scripts]
        else:
            hit: set[int] = set()
            for p in self.reach(scripts):
                hit.update(i for i in self.items_of(p) if i not in produced)
            names = list(dict.fromkeys(plain(self.items[i].get("label") or "", 80) for i in sorted(hit)))
            names = [n for n in names if n]
        return names[:MAX_AFFECTS], max(0, len(names) - MAX_AFFECTS)

    def loading(self, packages: Iterable[str]) -> list[str]:
        """The main R scripts that load one of the packages, themselves or through what they source: one walk back from
        the files that load them."""
        names = {n.lower() for p in packages for n in self.loaders.get(p, [])}
        if not names or not self.r_scripts:
            return []
        self.graph()
        starts = [p for p in {*self._edges, *self.r_scripts} if posixpath.basename(p).lower() in names]
        up = self._walk(starts, self._back or {})
        return [s for s in self.r_scripts if s in up]


def _item_key(label: str) -> str | None:
    found = coverage.mentions(str(label or "")[:120])
    return found[0][2][0] if found and found[0][2] else None


def _produced(conn, ctx: _Ctx) -> set[int]:
    """The items the run made after all: a table of results.json under the item's label, or an output file the item names."""
    if not ctx.run_id or not ctx.items:
        return set()
    rec = W.get_run(conn, ctx.run_id) or {}
    tables = ((rec.get("result") or {}).get("results") or {}).get("tables") or []
    keys: set[str] = set()
    for t in tables[:2_000]:
        for _s, _e, ks in coverage.mentions(str(t.get("label") or "")[:300])[:3]:
            keys.update(ks)
    files = {posixpath.basename(str(o)).lower() for o in ((ctx.outcome or {}).get("outputs") or [])[:2_000]}
    out = set()
    for i, it in enumerate(ctx.items):
        k = _item_key(it.get("label"))
        if (k and (k in keys or (coverage.parent_key(k) or "") in keys)) or \
                any(posixpath.basename(str(o)).lower() in files for o in (it.get("outputs") or [])[:20]):
            out.add(i)
    return out


# ---------------------------------------------------------------- the steps

def _saved(ws: Path, remote: fetch.Remote) -> bool:
    try:
        rel = fetch._author_path(remote.name, remote.name.lower().endswith(".pdf"))
    except fetch.FetchError:
        return True                                   # a name fetch would refuse: nothing to try
    return os.path.isfile(ws / rel)


def _size(listed: Any, unknown: int) -> int:
    """A listed size, or `unknown` (the most fetch would download of that kind) when the source gave none."""
    return listed if isinstance(listed, int) and listed >= 0 else unknown


def _has_code(ws: Path) -> bool:
    return any(package.lang_of(f.name) for f in W.regular_files(ws / W.AUTHOR)[:20_000]) \
        if (ws / W.AUTHOR).is_dir() and not (ws / W.AUTHOR).is_symlink() else False


def _fetch_source(conn, plan_id: str, src: str, ws: Path, first: bool) -> tuple[int, int, int]:
    """One link: (scripts saved, documents saved, datasets imported). `first`: no package is saved yet, so the default
    pick's data comes too. The download itself is fetch's: size caps, safe names, zip rules, text-only code."""
    client = fetch._client(None)
    try:
        listing = fetch.list_source(src, client)
        fetch.record_listing(plan_id, src, listing)
        chosen: list[fetch.Remote] = []
        texts: dict[str, str] = {}
        if first:
            try:
                auto = fetch.plan_auto(listing, client)
                chosen, texts = [*auto.data, *auto.code, *auto.docs], dict(auto.texts)
            except fetch.FetchError:             # the code could not be read for a pick: the scripts still come, no data
                log.warning("default pick of %s for %s", src, plan_id, exc_info=True)
        picked = {r.name for r in chosen}
        budget, n_code = FETCH_CODE_BYTES, 0
        for r in listing.code:
            if n_code >= FETCH_CODE_FILES or r.name in picked or not r.fetchable or _saved(ws, r):
                continue
            size = _size(r.size, W.AUTHOR_FILE)
            if size > W.AUTHOR_FILE or size > budget:
                continue
            chosen.append(r)
            budget, n_code = budget - size, n_code + 1
        budget, n_docs = FETCH_DOC_BYTES, 0
        for r in listing.docs:
            if n_docs >= FETCH_DOCS or r.name in picked or not r.fetchable or package.is_side(r.name) or _saved(ws, r):
                continue
            size = _size(r.size, fetch.PDF_MAX if r.name.lower().endswith(".pdf") else fetch.CODE_MAX)
            if size > budget:
                continue
            chosen.append(r)
            budget, n_docs = budget - size, n_docs + 1
        if not chosen:
            return 0, 0, 0
        results = fetch._download_all(conn, src, chosen, plan_id, client, fetch.MAX_MB * fetch.MB, texts)
    finally:
        client.close()
    code = sum(1 for r in results if r.saved and package.is_code(r.name))
    return code, sum(1 for r in results if r.saved) - code, sum(1 for r in results if r.dataset)


def _fetch(conn, ctx: _Ctx, run: _Run) -> tuple[str, str]:
    plan = plans.get(conn, ctx.plan_id) or {}
    links = [str(link.get("url") or "") for link in (plan.get("plan") or {}).get("links") or []
             if link.get("url") and link.get("access") != "request"][:MAX_LINKS]
    if not links:
        return "skipped", "方案里没有复现数据链接"
    W.workspace_dir(ctx.plan_id).mkdir(parents=True, exist_ok=True)
    ws = rrun.ws_of(ctx.plan_id)
    first = not _has_code(ws)
    code = docs = data = 0
    errors: list[str] = []
    for src in links:
        run.say("fetch", "列出复现包里的文件")
        try:
            c, d, n = _fetch_source(conn, ctx.plan_id, src, ws, first)
        except fetch.FetchError as e:
            errors.append(str(e))
            continue
        code, docs, data = code + c, docs + d, data + n
    ctx.new_code = code
    got = "、".join(x for x in (f"{code} 个脚本" if code else "", f"{docs} 份说明" if docs else "",
                                f"{data} 个数据集" if data else "") if x)
    if got:
        return "done", f"取回 {got}"
    if errors and first:
        return "failed", errors[0]
    if first:
        return "failed", "复现包里没有可以取的文件"
    return "skipped", "复现包已取回" + ("，这次没连上来源" if errors else "")


def _scan(conn, ctx: _Ctx, run: _Run) -> tuple[str, str]:
    ctx.items = coverage.items(conn, ctx.plan_id)
    if ctx.items and not ctx.new_code:
        return "skipped", f"清单已有 {len(ctx.items)} 项"
    try:
        coverage.rescan(conn, ctx.plan_id)
    finally:
        ctx.items = coverage.items(conn, ctx.plan_id)
        ctx.index()
    return "done", f"找到 {len(ctx.items)} 项表图"


def _fetchable(item: missing.Item) -> bool:
    if item.why == "unfetched":
        return True
    return item.why == "oversize" and item.size is not None and item.size <= fetch.MAX_MB * fetch.MB


def _fetch_listed(conn, plan_id: str, items: list[missing.Item]) -> int:
    """Download the missing files the package lists and the lab can import, as the plan page's Import would. Returns how
    many datasets came in; a source that fails leaves its files as needs."""
    listing = fetch.read_listing(plan_id)
    if not listing:
        return 0
    by_base = missing._by_base(listing)
    picks: dict[str, list[str]] = {}
    total = n = 0
    for it in items:
        if not _fetchable(it) or n >= FETCH_FILES:
            continue
        hit = missing._listed(it.key, by_base)
        if not hit or not hit[1].get("src"):
            continue
        size = _size(it.size, fetch.MAX_MB * fetch.MB)
        if total + size > FETCH_BYTES:
            continue
        picks.setdefault(str(hit[1]["src"]), []).append(hit[0])
        total, n = total + size, n + 1
    got = 0
    for src, names in picks.items():
        try:
            got += sum(1 for r in fetch.fetch(conn, src, names, plan_id=plan_id) if r.dataset)
        except Exception:  # noqa: BLE001 - not reachable now: the files stay needs, and the next run tries again
            log.warning("fetching %s for %s", names, plan_id, exc_info=True)
    return got


def _missing(conn, ctx: _Ctx, run: _Run) -> tuple[str, str]:
    rep = missing.report(conn, ctx.plan_id, limit=None)
    if rep.note:
        return "blocked", "还没有取回作者的复现包"
    got = ctx.fetched = _fetch_listed(conn, ctx.plan_id, rep.missing) if any(_fetchable(i) for i in rep.missing) else 0
    if got:
        rep = missing.report(conn, ctx.plan_id, limit=None)
    gaps = rep.missing
    needs = []
    for it in gaps[:MAX_DATA_NEEDS]:
        labels, more = ctx.affects(it.scripts)
        needs.append({"kind": "data", "key": it.key, "uid": hashlib.sha1(it.key.encode()).hexdigest()[:12],
                      "title": it.name, "why": plain(it.reason),
                      "where": plain(it.hint) if it.why in ("restricted", "other-paper", "unsupported") else "",
                      "affects": labels, "affects_more": more, "upload": True, "scripts": list(it.scripts)})
    ctx.data_needs, ctx.data_more = needs, max(0, len(gaps) - len(needs))
    _publish(run, ctx.dep_needs, ctx.data_needs, ctx.data_more)
    took = f"，自动取回 {got} 个" if got else ""
    if gaps:
        return "blocked", f"还缺 {len(gaps)} 个数据文件{took}"
    return "done", f"数据都齐了{took}"


def command_ok(cmd: str) -> bool:
    """One of COMMANDS, or a `brew install` of renv.FORMULAS only (built by renv.brew_command)."""
    return cmd in COMMANDS or renv.command_ok(cmd)


def _root_of(pkg: str, hint: str) -> tuple[str, str]:
    """(the package whose own install failed, its reason) of an install hint: 「依赖 X 没装上：…」 names X (rrun.hint_of
    always names the root, never a chain)."""
    m = _ROOT.match(hint)
    return (m.group(1), hint[m.end():]) if m else (pkg, hint)


def _dependency(pkg: str, hint: str) -> tuple[str, str]:
    """(command, title) of what Leo installs so that `pkg` builds, read off rrun.hint_of's own phrases. The command is one
    of COMMANDS or a `brew install` of renv.FORMULAS ('' when none would do it): a package known to need several system
    libraries gets them all (renv.PACKAGE_FORMULAS), a missing pkg-config adds pkgconf. The hint may quote a third-party
    build's error line, so nothing of it is ever copied into a command."""
    hint = str(hint or "")[:2000]
    root, why = _root_of(pkg, hint)
    if "需要 cmake" in why:
        return "brew install cmake", "安装 cmake"
    if re.search(r"invalid active developer path|xcrun: error|Xcode 命令行工具", why):
        return "xcode-select --install", "安装 Xcode 命令行工具"
    if "gfortran" in why:
        return "brew install gcc", "安装 gfortran"
    formulas = ["pkgconf"] if "需要 pkg-config" in why else []
    m = _HEADER.search(why)
    table = renv.PACKAGE_FORMULAS.get(root)
    if table and (m or formulas or "缺系统库" in why):
        formulas += table
    elif m:
        header = m.group(1)
        formula = HEADER_FORMULA.get(posixpath.basename(header)) or HEADER_FORMULA.get(header.split("/")[0])
        if formula:
            formulas.append(formula)
        elif not formulas:
            return "", plain(f"安装提供 {header} 的系统库", NAME_CHARS)
    cmd = renv.brew_command(formulas)
    if cmd:
        return cmd, plain("安装 " + "、".join(cmd.split(" ")[2:]), NAME_CHARS)
    return "", "有 R 包装不上"


def _only_missing(cmd: str) -> str:
    """A `brew install` narrowed to the formulas this Mac lacks (renv's checks); unchanged when it lacks none of them,
    or for any other command."""
    if not cmd.startswith("brew install ") or not renv.command_ok(cmd):
        return cmd
    try:
        lacking = renv.missing_formulas(cmd.split(" ")[2:])
    except Exception:  # noqa: BLE001 - a failed check shows the whole command, which is still right
        return cmd
    return renv.brew_command(lacking) or cmd


def _names(names: list[str], n: int = 4) -> str:
    return "、".join(plain(p, NAME_CHARS) for p in names[:n]) + (f" 等 {len(names)} 个包" if len(names) > n else "")


def _dep_needs(failed: dict[str, str]) -> list[dict[str, Any]]:
    """One need per root cause: the packages whose own build failed for the same fix (one command), and the packages
    that only failed because they depend on those, share it; a package CRAN and its archive both lack, and a build error
    no command fixes, each stand alone. Packages keep their full list here; _publish caps what the page gets."""
    groups: dict[str, dict[str, Any]] = {}
    checked: dict[str, str] = {}
    for pkg, hint in failed.items():
        hint = str(hint or "")[:2000]
        if not hint or hint == "没有装完":
            continue
        root, why = _root_of(pkg, hint)
        cmd, title = _dependency(pkg, hint)
        if cmd.startswith("brew install "):
            if cmd not in checked:
                checked[cmd] = _only_missing(cmd)
            if checked[cmd] != cmd:
                cmd = checked[cmd]
                title = plain("安装 " + "、".join(cmd.split(" ")[2:]), NAME_CHARS)
        if cmd:
            key = cmd
        elif "存档里也没有" in why or "CRAN 上没有这个包" in why:
            key, title = "gone", "CRAN 上找不到的包"
        elif "存档没连上" in why:
            key, title = "offline", "连不上 CRAN 的存档"
        else:
            key = f"root:{root}"
            title = title if title != "有 R 包装不上" else plain(f"{root} 装不上", NAME_CHARS)
        g = groups.setdefault(key, {"kind": "dependency", "title": title, "why": "", "affects": [], "affects_more": 0,
                                    "command": cmd if command_ok(cmd) else "", "packages": [], "_roots": {}, "_reason": ""})
        g["packages"].append(pkg)
        if len(g["_roots"]) < 50:
            g["_roots"].setdefault(root, None)
        if not g["_reason"] or root == pkg:
            g["_reason"] = plain(why, 80)
    out = []
    for g in groups.values():
        roots = list(g.pop("_roots"))
        reason = g.pop("_reason")
        held = [p for p in g["packages"] if p not in roots]
        lead = _names(roots)
        why = f"{lead} {reason}" if reason.startswith(("需要", "缺")) else f"{lead}：{reason}"
        if held:
            why += f"；{_names(held)} 依赖{'它们' if len(roots) > 1 else '它'}"
        g["why"] = plain(why)
        out.append(g)
    return sorted(out, key=lambda g: not g["command"])       # what one command fixes first: those are kept when capped


def _publish(run: _Run, dep: list[dict[str, Any]], data: list[dict[str, Any]], data_more: int) -> None:
    """The needs as the page gets them: at most MAX_DEP_NEEDS dependencies and MAX_DATA_NEEDS files, every name cut to a
    line, at most MAX_PACKAGES packages each; what is left out is counted."""
    shown = []
    for n in dep[:MAX_DEP_NEEDS]:
        pk = list(n["packages"])
        shown.append({"kind": "dependency", "title": plain(n["title"], NAME_CHARS), "why": plain(n["why"]),
                      "command": n["command"] if command_ok(n["command"]) else "",
                      "affects": [plain(a, 80) for a in n["affects"][:MAX_AFFECTS]], "affects_more": n["affects_more"],
                      "packages": [plain(p, NAME_CHARS) for p in pk[:MAX_PACKAGES]],
                      "packages_more": max(0, len(pk) - MAX_PACKAGES)})
    for n in data[:MAX_DATA_NEEDS]:
        shown.append({**n, "title": plain(n["title"], 80), "affects": [plain(a, 80) for a in n["affects"][:MAX_AFFECTS]],
                      "scripts": [plain(x, 200) for x in n["scripts"][:20]]})
    run.needs(shown, max(0, len(dep) - MAX_DEP_NEEDS) + max(0, len(data) - MAX_DATA_NEEDS) + data_more)


@contextmanager
def _holding(step: rrun.Step) -> Iterator[rrun.Step]:
    try:
        yield step
    except BaseException as e:
        step.__exit__(type(e), e, e.__traceback__)
        raise
    else:
        step.__exit__(None, None, None)


def _take_r(ws: Path, kind: str, run: _Run, key: str) -> rrun.Step | None:
    """rrun's own lock for this plan, waiting at most R_WAIT_S for an R step that something else started."""
    deadline = time.monotonic() + R_WAIT_S
    told = False
    while True:
        try:
            return rrun.Step(ws, kind).__enter__()
        except rrun.RRunError:
            if time.monotonic() >= deadline:
                return None
            if not told:
                run.say(key, "等另一个 R 步骤结束")
                told = True
            time.sleep(R_POLL_S)


def _r_need(ctx: _Ctx) -> None:
    scripts = ctx.r_scripts or sorted({str(s.get("path")) for it in ctx.items for s in it.get("scripts") or []
                                       if str(s.get("path") or "").lower().endswith((".r", ".rmd"))})
    labels, more = ctx.affects(scripts)
    ctx.dep_needs.append({"kind": "dependency", "title": "安装 R", "why": "这台电脑没有 R，作者的 R 代码跑不了",
                          "affects": labels, "affects_more": more, "command": R_INSTALL, "packages": []})


def _install(conn, ctx: _Ctx, run: _Run) -> tuple[str, str]:
    W.workspace_dir(ctx.plan_id).mkdir(parents=True, exist_ok=True)
    ctx.ws = rrun.ws_of(ctx.plan_id)
    if W.current_job(conn, ctx.plan_id) is not None:
        ctx.r_off, ctx.r_off_state = "助手会话正在用这个工作区", "failed"
        return "failed", ctx.r_off
    try:
        ctx.r_scripts = rrun.r_main_scripts(conn, ctx.plan_id, ctx.ws)
    except rrun.RRunError:
        ctx.r_off = "作者没有可以直接运行的 R 脚本"
        return "skipped", ctx.r_off
    if rrun.detect() is None:
        _r_need(ctx)
        _publish(run, ctx.dep_needs, ctx.data_needs, ctx.data_more)
        ctx.r_off, ctx.r_off_state = "这台电脑没有 R", "blocked"
        return "blocked", ctx.r_off
    step = _take_r(ctx.ws, "install", run, "install")
    if step is None:
        ctx.r_off, ctx.r_off_state = "R 一直被别的操作占用", "failed"
        return "failed", "R 一直被别的操作占用，等它结束后再跑一次"
    with _holding(step):
        result = rrun.install(conn, ctx.plan_id, step=step, progress=lambda s: run.say("install", s))
    ctx.loaders = {k: list(v) for k, v in rrun.package_list(ctx.ws).packages.items()}
    failed = dict(result.get("failed") or {})
    archived = len(result.get("archived") or {})
    if result.get("ok"):
        return "done", f"{len(result.get('installed') or [])} 个 R 包就绪" + (
            f"，{archived} 个已下架的从 CRAN 存档装上" if archived else "")
    if result.get("stopped"):
        ctx.stopped = True
        return "failed", "装包被停止了"
    if result.get("timed_out"):
        return "failed", "装包超时"
    log_text = W.safe_read(ctx.ws, "logs/rinstall.log", INSTALL_LOG_CHARS)
    if "unable to access index for repository" in log_text:
        return "failed", "连不上 CRAN，网络好了再跑一次"
    ctx.dep_needs.extend(_dep_needs(failed))
    for need in ctx.dep_needs[:MAX_DEP_NEEDS]:
        need["affects"], need["affects_more"] = ctx.affects(ctx.loading(need["packages"]))
    _publish(run, ctx.dep_needs, ctx.data_needs, ctx.data_more)
    if not failed:
        return "failed", "装包没有正常结束"
    return ("blocked" if ctx.dep_needs else "failed"), f"{len(failed)} 个包没装上，其余照常运行"


def _mentions(text: str, packages: set[str]) -> set[str]:
    """The failed packages a script's error or skip note names: by name, `no package called`, or a function of theirs.
    One pass over the text whatever the number of packages."""
    text = str(text or "")[:4000]
    out = {w.rstrip(".") for w in _WORD.findall(text)} & packages
    out.update(m for m in _NO_PKG.findall(text) if m in packages)
    out.update(rrun.FUNCTION_PACKAGES[f] for f in _NO_FN.findall(text) if rrun.FUNCTION_PACKAGES.get(f) in packages)
    return out


def _notes(failed: dict[str, str], skipped: list[str], packages: set[str]) -> dict[str, set[str]]:
    """failed script → the failed packages its error or rrun's skip note names, each text read once."""
    out: dict[str, set[str]] = {}
    for script, text in [*failed.items(), *((x.partition("：")[0], x) for x in skipped[:2 * len(failed) + 10])]:
        if script in failed:
            out.setdefault(script, set()).update(_mentions(text, packages))
    return out


def _explained(ctx: _Ctx, failed: dict[str, str], skipped: list[str]) -> set[str]:
    """The failed scripts a need accounts for: waiting for a missing file, or stopped by a package that did not install."""
    waiting = {s for n in ctx.data_needs for s in n["scripts"]}
    pkgs = {p for n in ctx.dep_needs for p in n["packages"]}
    named = _notes(failed, skipped, pkgs)
    out = {s for s in failed if s in waiting or named.get(s)}
    return out | (set(failed) & set(ctx.loading(pkgs)))


def _run(conn, ctx: _Ctx, run: _Run) -> tuple[str, str]:
    if ctx.r_off:
        return ctx.r_off_state, ctx.r_off
    step = _take_r(ctx.ws, "run", run, "run")
    if step is None:
        return "failed", "R 一直被别的操作占用，等它结束后再跑一次"
    with _holding(step):
        outcome = rrun.run(conn, ctx.plan_id, step=step, progress=lambda s: run.say("run", s))
    ctx.outcome, ctx.run_id = outcome, outcome.get("run_id")
    run.st["run_id"] = ctx.run_id
    todo = list(outcome.get("todo") or [])
    failed = dict(outcome.get("failed") or {})
    tables = int(outcome.get("tables") or 0)
    if outcome.get("state") == "stopped":
        ctx.stopped = True
        return "failed", "运行被停止了"
    if not failed:
        return "done", f"{len(todo)} 个脚本都跑通了，得到 {tables} 张表"
    explained = _explained(ctx, failed, outcome.get("skipped") or [])
    left = [s for s in failed if s not in explained]
    text = f"跑通 {len(todo) - len(failed)}/{len(todo)} 个脚本，得到 {tables} 张表"
    if left:
        text += "；" + "、".join(posixpath.basename(s) for s in left[:3]) + (" 等" if len(left) > 3 else "") + " 没跑通"
    return ("failed" if left else "blocked"), text


def _compare(conn, ctx: _Ctx, run: _Run) -> tuple[str, str]:
    if not ctx.run_id:
        return ("blocked" if ctx.r_off_state == "blocked" else "skipped"), "没有新的运行结果"
    rec = W.get_run(conn, ctx.run_id)
    why = W.not_comparable(rec) if rec else "没有这次运行"
    if why:
        return "blocked", why
    out = W.compare_run(conn, ctx.plan_id, ctx.run_id, require_review=False)
    s = out.get("summary") or {}
    c = s.get("counts") or {}
    if not s.get("total"):
        return "done", "方案里还没有复现目标，结果已存"
    parts = [f"{c.get('exact', 0) + c.get('within', 0)} 项复现出来", f"{c.get('direction', 0)} 项方向一致",
             f"{c.get('mismatch', 0)} 项对不上", f"{c.get('missing', 0)} 项没找到"]
    return "done", f"{s.get('total', 0)} 项目标：" + "，".join(p for p in parts if not p.startswith("0 "))


def _engine_down(why: str) -> str:
    """dispatch.route's reason for not running a job now, as one short Chinese line."""
    if "暂停" in why:
        return "助手已暂停"
    if "limit" in why or "cap" in why:
        return "今天的助手用量到上限了"
    return "助手现在用不了"


def _report(conn, ctx: _Ctx, run: _Run) -> tuple[str, str]:
    from ..ai import dispatch, queue, registry
    began = time.time()
    if REPORT_JOB not in registry.load_all():
        return "skipped", "还不能自动写报告"
    where, why = dispatch.route(conn)
    if where == "queue":
        return "failed", _engine_down(why)
    job_id = queue.enqueue(conn, REPORT_JOB, ctx.plan_id)
    if job_id is None:                                    # an open job for this plan already: wait for that one
        row = queue.latest_for(conn, REPORT_JOB, ctx.plan_id)
        job_id = row["id"] if row is not None else None
    if job_id is None:
        return "failed", "没能排上写报告的任务"
    started = dispatch.dispatch(job_id)
    if started == "failed":
        return "failed", "助手现在用不了"
    run.say("report", "助手在写报告")
    deadline = time.monotonic() + REPORT_WAIT_S
    while True:
        job = queue.get(conn, job_id)
        status = job["status"] if job is not None else "missing"
        if status == "done":
            path = W.workspace_dir(ctx.plan_id) / REPORT_FILE
            fresh = path.is_file() and not path.is_symlink() and path.stat().st_mtime >= began - 1
            return ("done", "报告写好了") if fresh else ("failed", "报告没有写出来")
        if status in ("failed", "cancelled", "missing"):
            return "failed", "写报告时出错了"
        if status == "queued" and job["error"]:           # the engine sent it back: resting, rate-limited, over the cap
            return "failed", "助手暂时用不了，稍后会接着写"
        if time.monotonic() >= deadline:
            return "failed", "五分钟内没写完，助手会接着写"
        time.sleep(REPORT_POLL_S)


def _deck(conn, ctx: _Ctx, run: _Run) -> tuple[str, str]:
    res = deck.make(conn, ctx.plan_id)
    text = f"{res.slides} 页，{res.replicated} 页放了复现结果"
    return "done", text + (f"，{res.pending} 页待补" if res.pending else "")


def _export(conn, ctx: _Ctx, run: _Run) -> tuple[str, str]:
    d = export.export_folder(conn, ctx.plan_id)
    return "done", f"已放进课题「{d.project_name}」" + ("并挂到议题" if d.attached else "")


STEP_FUNCS: dict[str, Callable[[Any, _Ctx, _Run], tuple[str, str]]] = {
    "fetch": _fetch, "scan": _scan, "missing": _missing, "install": _install, "run": _run, "compare": _compare,
    "report": _report, "deck": _deck, "export": _export}


def _final_needs(conn, ctx: _Ctx) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(dependency, data) needs as the run ends: affected items minus what the run produced, and the needs that stopped
    nothing dropped."""
    if ctx.outcome is None:
        return ctx.dep_needs, ctx.data_needs
    produced = _produced(conn, ctx)
    todo = set(ctx.outcome.get("todo") or [])
    failed = dict(ctx.outcome.get("failed") or {})
    ok = todo - set(failed)
    named = _notes(failed, list(ctx.outcome.get("skipped") or []), {p for n in ctx.dep_needs for p in n["packages"]})
    dep = []
    for need in ctx.dep_needs:
        if not need["packages"]:                          # R itself
            dep.append(need)
            continue
        pk = set(need["packages"])
        stopped = {s for s, pkgs in named.items() if pkgs & pk} | (set(failed) & set(ctx.loading(pk)))
        if not stopped:
            continue
        if len(dep) < MAX_DEP_NEEDS:
            need["affects"], need["affects_more"] = ctx.affects(sorted(stopped), produced)
        dep.append(need)
    data = []
    for need in ctx.data_needs:
        left = [s for s in need["scripts"] if s not in ok]
        if need["scripts"] and not left:
            continue
        need["affects"], need["affects_more"] = ctx.affects(left, produced)
        data.append(need)
    return dep, data


def execute(conn, plan_id: str, run: _Run) -> dict[str, Any]:
    """Every step in order on this thread; a step that raises is recorded and the next one starts."""
    ctx = _Ctx(plan_id)
    for key, _label in STEPS:
        if ctx.stopped:                                   # Leo's Stop on the R card ends the whole run
            run.end(key, "skipped", "已停止")
            continue
        run.begin(key)
        try:
            outcome, detail = STEP_FUNCS[key](conn, ctx, run)
        except Exception as e:  # noqa: BLE001 - one step's failure must not stop the steps that do not need it
            log.warning("pipeline step %s of %s failed", key, plan_id, exc_info=True)
            outcome, detail = "failed", (str(e) if _CJK.search(str(e)) else "这一步出错了")
        run.end(key, outcome, detail)
    try:
        dep, data = _final_needs(conn, ctx)
        _publish(run, dep, data, ctx.data_more)
        step = run.step("missing")
        if step["state"] == "blocked" and ctx.data_needs:     # the count the step showed, after the run dropped some
            left = len(data) + ctx.data_more
            took = f"，自动取回 {ctx.fetched} 个" if ctx.fetched else ""
            step.update(state="blocked" if left else "done",
                        detail=(f"还缺 {left} 个数据文件" if left else "缺的文件不影响跑通的脚本") + took)
    except Exception:  # noqa: BLE001 - the needs as collected during the run still stand
        log.warning("refining the needs of %s", plan_id, exc_info=True)
    run.finish(stopped=ctx.stopped)
    return run.st


# ---------------------------------------------------------------- starting a run

_threads: dict[str, threading.Thread] = {}


def _check(conn, plan_id: str) -> Path:
    if plans.get(conn, plan_id) is None:
        raise PipelineError("没有这个复现方案。")
    if W.current_job(conn, plan_id) is not None:
        raise PipelineError("这份方案的助手会话正在运行，等它结束再一键跑完。")
    return _root(plan_id)


def start(conn, plan_id: str) -> bool:
    """Start the plan's run on a background thread with its own database connection. False when one is already running
    (here or in another process); PipelineError when the plan cannot be run now."""
    root = _check(conn, plan_id)
    hold = _Hold(root)
    if not hold.take():
        return False
    try:
        run = _Run(plan_id, root)
        run.save()

        def go() -> None:
            from ..db.connection import connect
            c = None
            try:
                c = connect()
                execute(c, plan_id, run)
            except BaseException:  # noqa: BLE001 - a crashed run must end visibly, not stay "running"
                log.exception("pipeline of %s crashed", plan_id)
                try:
                    run.finish()
                except Exception:  # noqa: BLE001
                    log.exception("could not record the end of the pipeline of %s", plan_id)
            finally:
                if c is not None:
                    c.close()
                hold.release()

        t = threading.Thread(target=go, daemon=True, name=f"pipeline-{plan_id}")
        _threads[plan_id] = t
        t.start()
    except BaseException:
        hold.release()
        raise
    return True


def run_now(conn, plan_id: str) -> dict[str, Any]:
    """The same run on the calling thread (the command line, tests); PipelineError when one is already running."""
    root = _check(conn, plan_id)
    hold = _Hold(root)
    if not hold.take():
        raise PipelineError("这份方案已经在一键跑完了。")
    try:
        run = _Run(plan_id, root)
        run.save()
        try:
            return execute(conn, plan_id, run)
        except BaseException:
            run.finish()
            raise
    finally:
        hold.release()


def join(plan_id: str, timeout: float | None = None) -> bool:
    """Wait for this process's run of the plan to end; True when none is left running."""
    t = _threads.get(plan_id)
    if t is not None:
        t.join(timeout)
        if not t.is_alive():
            _threads.pop(plan_id, None)
    return t is None or not t.is_alive()
