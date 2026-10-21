"""复现环境 (card 0204): what 设置 shows about R for replication, and Preinstall, which fills the lab's shared library.

The shared library is <data>/replication/rlib-shared/<R x.y>-<flavor>/rlib (rrun.shared_dir): one per minor version and
per build of R, so CRAN's official R and Homebrew's never share compiled packages. Every plan's install and run put it
after the plan's own library (R_LIBS_SITE), read-only in both sandboxes; a plan's install skips what it already holds.

Preinstall installs PRESET into it, in the background, through the same generated install script and the same install
sandbox as a plan's install step (rrun.run_install), with the shared library's folder as the workspace: the network
open, writes only to its rlib/ and tmp/. Progress is read off the script's own [rr] lines and shown in the same
.jobline as 一键跑完 (partials/_r_env.html, replication.js ticks the clock).

The page also lists the build tools and system libraries that Homebrew's R needs to compile the common packages
(found by `which`, the Homebrew prefix and `pkg-config --exists`, each with a time limit, the answer kept CHECK_TTL
seconds), and the exact command for what is missing. Every command shown is built here from the constant FORMULAS:
nothing of a log or of a tool's output is ever copied into one."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from . import rrun
from . import workspace as W
from .rtext import PackageList

PRESET = ("tidyverse", "lme4", "glmmTMB", "car", "AER", "fixest", "plm", "sandwich", "lmtest", "stargazer",
          "modelsummary", "haven", "data.table", "rstan", "rstanarm", "ggpubr", "quanteda", "readtext", "sf")
DOWNLOAD_URL = "https://cloud.r-project.org/bin/macosx/"
CHECK_TTL = 60.0
PROBE_TIMEOUT_S = 5
RESULT_FILE = "logs/preinstall.json"

# What Homebrew's R needs to compile the preset packages: (formula, what `which` or pkg-config looks for, a tool or a
# library, who needs it).
TOOLS = (
    ("cmake", "cmake", "tool", "lme4、rstan"),
    ("pkgconf", "pkg-config", "tool", "找系统库的包"),
    ("freetype", "freetype2", "lib", "tidyverse"),
    ("harfbuzz", "harfbuzz", "lib", "tidyverse"),
    ("fribidi", "fribidi", "lib", "tidyverse"),
    ("libpng", "libpng", "lib", "tidyverse"),
    ("jpeg-turbo", "libjpeg", "lib", "tidyverse"),
    ("libtiff", "libtiff-4", "lib", "tidyverse"),
    ("webp", "libwebp", "lib", "tidyverse"),
    ("poppler", "poppler-cpp", "lib", "readtext"),
    ("gdal", "gdal", "lib", "sf"),
    ("geos", "geos", "lib", "sf"),
    ("proj", "proj", "lib", "sf"),
    ("udunits", "udunits2", "lib", "sf"),
)
# Every Homebrew formula a command on the page may name, in the order a command lists them. pipeline.HEADER_FORMULA's
# formulas are all here (test_replication_renv checks it).
FORMULAS = ("cmake", "pkgconf", "gcc", "freetype", "harfbuzz", "fribidi", "libpng", "jpeg-turbo", "libtiff", "webp",
            "poppler", "gdal", "geos", "proj", "udunits", "gsl", "gmp", "mpfr", "fftw", "hdf5", "glpk", "libsodium", "libuv",
            "zeromq", "imagemagick", "cairo", "fontconfig", "libxml2", "openssl@3", "curl", "libgit2", "netcdf", "icu4c",
            "sqlite", "zstd", "xz")
# A CRAN package whose source build is known to need these formulas (more complete than the one header its configure
# script stops at): the command for it names them all.
PACKAGE_FORMULAS = {
    "textshaping": ("freetype", "harfbuzz", "fribidi"), "systemfonts": ("freetype",),
    "ragg": ("freetype", "libpng", "jpeg-turbo", "libtiff", "webp"), "pdftools": ("poppler",),
    "sf": ("gdal", "geos", "proj", "udunits"), "terra": ("gdal", "geos", "proj"), "units": ("udunits",),
    "lwgeom": ("geos", "proj"), "gsl": ("gsl",), "magick": ("imagemagick",), "nloptr": ("cmake",),
    "RcppParallel": ("cmake",), "openssl": ("openssl@3",), "gmp": ("gmp",), "Rmpfr": ("mpfr",),
}


def brew_command(formulas) -> str:
    """`brew install a b c` for the given formulas, each one of FORMULAS (anything else is left out), in FORMULAS'
    order; '' when none is left."""
    want = set(formulas)
    names = [f for f in FORMULAS if f in want]
    return "brew install " + " ".join(names) if names else ""


def command_ok(cmd: str) -> bool:
    """A `brew install …` command made of FORMULAS only, as brew_command writes it."""
    parts = str(cmd or "").split(" ")
    return len(parts) > 2 and parts[:2] == ["brew", "install"] and brew_command(parts[2:]) == cmd


# ---------------------------------------------------------------- tools and libraries

_CHECK: dict[str, Any] = {}
_CHECK_LOCK = threading.Lock()


def brew_prefix(info: rrun.RInfo | None) -> Path | None:
    """Homebrew's prefix: the one R comes from, else the usual ones that hold a `brew`."""
    if info is not None and info.flavor == "homebrew":
        p = rrun.r_prefix(info.home)
        if (p / "bin" / "brew").exists():
            return p
    for p in (Path("/opt/homebrew"), Path("/usr/local")):
        if (p / "bin" / "brew").exists():
            return p
    return None


def _search_path(prefix: Path | None) -> str:
    dirs = ([str(prefix / "bin")] if prefix else []) + ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    return os.pathsep.join(dict.fromkeys(dirs))


def _pc_exists(pc: str, module: str) -> bool:
    try:
        return subprocess.run([pc, "--exists", module], capture_output=True, timeout=PROBE_TIMEOUT_S,
                              env={"PATH": "/usr/bin:/bin", "LANG": "C"}).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def check_tools(info: rrun.RInfo | None, fresh: bool = False) -> list[dict[str, Any]]:
    """[{formula, kind, present, needed_by}] for TOOLS. A tool is found with `which` on Homebrew's and the system's bin
    folders; a library by Homebrew's opt/<formula> folder, else `pkg-config --exists` (each probe at most
    PROBE_TIMEOUT_S). Kept CHECK_TTL seconds; `fresh` checks again."""
    now = time.monotonic()
    with _CHECK_LOCK:
        if not fresh and _CHECK.get("tools") is not None and now - _CHECK.get("at", 0) < CHECK_TTL:
            return [dict(t) for t in _CHECK["tools"]]
    prefix = brew_prefix(info)
    path = _search_path(prefix)
    pc = shutil.which("pkg-config", path=path)
    out = []
    for formula, probe, kind, needed_by in TOOLS:
        if kind == "tool":
            present = bool(shutil.which(probe, path=path))
        else:
            present = bool(prefix and (prefix / "opt" / formula).is_dir()) or bool(pc and _pc_exists(pc, probe))
        out.append({"formula": formula, "kind": kind, "present": present, "needed_by": needed_by})
    with _CHECK_LOCK:
        _CHECK.update(at=time.monotonic(), tools=out, prefix=str(prefix or ""))
    return [dict(t) for t in out]


def missing_formulas(formulas) -> list[str]:
    """The given formulas that this Mac does not have, by the same checks (a formula outside TOOLS: Homebrew's
    opt/<formula> folder only)."""
    info = rrun.detect()
    known = {t["formula"]: t["present"] for t in check_tools(info)}
    prefix = brew_prefix(info)
    return [f for f in formulas if not known.get(f, bool(prefix and (prefix / "opt" / f).is_dir()))]


# ---------------------------------------------------------------- Preinstall

def _took(seconds: float | None) -> str:
    """"12 秒", "3 分 5 秒", "1 小时 4 分": the format of the 一键跑完 clock (routes/run_all.took, replication.js)."""
    if seconds is None or seconds < 0:
        return ""
    s = int(seconds)
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分 {s % 60} 秒" if s % 60 else f"{s // 60} 分"
    return f"{s // 3600} 小时 {s % 3600 // 60} 分"


def _ws(info: rrun.RInfo) -> Path | None:
    try:
        return rrun.shared_dir(info)
    except rrun.RRunError:
        return None


def start(info: rrun.RInfo | None = None) -> bool:
    """Leo's Preinstall: take the shared library's lock now and install PRESET into it on a background thread. False when
    a preinstall is already running; RRunError when it cannot start (no R, no compilers for an R that must compile)."""
    info = info or rrun.detect()
    if info is None:
        raise rrun.RRunError("这台电脑没有找到 R。")
    if rrun.dev_dir() is None and not info.binaries:
        raise rrun.RRunError("这台电脑没有命令行开发工具，R 包要编译，装不了。先在终端运行 xcode-select --install。")
    ws = rrun.shared_dir(info)
    for d in ("logs", "tmp", "rlib"):
        (ws / d).mkdir(parents=True, exist_ok=True)
    ws = W._real(ws)
    try:
        step = rrun.Step(ws, "preinstall").__enter__()
    except rrun.RRunError:
        return False
    rrun._write_state(ws, since=int(time.time()), key=info.key)

    def go() -> None:
        try:
            result = preinstall(ws, info, step)
            failed = result["failed"]
            note = ("已停止" if result["stopped"] else f"超过 {rrun.INSTALL_TIMEOUT_S // 60} 分钟，已中止" if result["timed_out"]
                    else "常用包都装好了" if result["ok"] else f"{len(failed)} 个包没装上：" + "、".join(list(failed)[:8]))
            step.finish("stopped" if result["stopped"] else "done" if result["ok"] else "error", note)
        except BaseException as e:  # noqa: BLE001 - a crashed preinstall must end visibly, not stay "running"
            step.finish("error", f"{type(e).__name__}: {e}")
        finally:
            step.__exit__(None, None, None)

    threading.Thread(target=go, daemon=True, name="rrun-preinstall").start()
    return True


def preinstall(ws: Path, info: rrun.RInfo, step: rrun.Step, timeout: int = rrun.INSTALL_TIMEOUT_S) -> dict[str, Any]:
    """The preinstall itself (the thread's body; tests call it directly with a stand-in for R)."""
    t0 = time.monotonic()
    W._swept(ws, "预装前")
    for d in ("logs", "tmp", "rlib"):
        (ws / d).mkdir(parents=True, exist_ok=True)
    plist = PackageList()
    for p in PRESET:
        plist.add(p, "预装")
    script = rrun.install_script(plist, binary=info.binaries, compile=rrun.dev_dir() is not None)
    got = rrun.run_install(ws, info, list(PRESET), script, timeout=timeout, step=step, shared=None)
    result = {"at": W._stamp(), "r": info.version, "key": info.key, "binaries": info.binaries, **got,
              "seconds": round(time.monotonic() - t0)}
    W.safe_write(ws, RESULT_FILE, json.dumps(result, ensure_ascii=False, indent=1))
    return result


def stop(info: rrun.RInfo | None = None) -> bool:
    info = info or rrun.detect()
    ws = _ws(info) if info else None
    return bool(ws and ws.is_dir() and rrun.stop_in(W._real(ws)))


def job(info: rrun.RInfo | None) -> dict[str, Any]:
    """The preinstall as the .jobline shows it: running, state, tone, the summary line, the clock, and one row per
    PRESET package (waiting, running, done, failed) read off the install script's [rr] lines (rrun._log_lines: line by
    line, capped)."""
    out: dict[str, Any] = {"running": False, "state": "", "note": "", "since": None, "took": "", "steps": [], "done": 0,
                           "total": len(PRESET), "current": "", "failed": {}}
    ws = _ws(info) if info else None
    if ws is None or not (ws / "logs").is_dir() or ws.is_symlink():
        return out
    st = rrun.read_state(ws)
    running = rrun.busy_in(ws)
    state = str(st.get("state") or "")
    if state == "running" and not running:
        state = "interrupted"
    try:
        result = json.loads(W.safe_read(ws, RESULT_FILE) or "{}") if not running else {}
    except (ValueError, W.WorkspaceError):
        result = {}
    rows = {p: {"name": p, "state": "waiting", "detail": "", "took": ""} for p in PRESET}
    current = ""
    if st:
        for line in rrun._log_lines(ws, "logs/rinstall.log", limit=20_000):
            m = rrun._RR_LINE.match(line)
            if not m:
                continue
            kind, name, rest = m.group(1), m.group(2), (m.group(3) or "").strip()
            if kind == "start":
                current = name
            row = rows.get(name)
            if row is None:
                continue
            if kind == "start":
                row["state"] = "running"
            elif kind == "ok":
                secs = rest.split(" ")[0]
                row.update(state="done", took=_took(int(secs)) if secs.isdigit() else "")
            elif kind == "have":
                row.update(state="done", detail="已经有了")
            elif kind in ("fail", "error") and row["state"] != "done":
                row["state"] = "failed"
    for name, why in (result.get("failed") or {}).items():
        if name in rows:
            rows[name].update(state="failed", detail=rrun._shown(str(why), 140))
    if not running:
        for row in rows.values():
            if row["state"] == "running":
                row["state"] = "failed" if state in ("error", "interrupted", "stopped") else row["state"]
    since = st.get("since") if isinstance(st.get("since"), int) else None
    done = sum(1 for r in rows.values() if r["state"] in ("done", "failed"))
    out.update(running=running, state=state, note=str(st.get("note") or "")[:300], since=since if running else None,
               steps=list(rows.values()), done=done, current=current if running else "",
               failed={k: rows[k]["detail"] for k in rows if rows[k]["state"] == "failed"})
    if since:
        end = time.time() if running else _epoch(st.get("finished_at")) or time.time()
        out["took"] = _took(end - since)
    return out


def _epoch(stamp: Any) -> float | None:
    try:
        return time.mktime(time.strptime(str(stamp), "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError, OverflowError):
        return None


# ---------------------------------------------------------------- the settings section

def view(fresh: bool = False) -> dict[str, Any]:
    """Everything the 复现环境 section shows."""
    info = rrun.detect()
    out: dict[str, Any] = {"r": None, "download": DOWNLOAD_URL, "tools": [], "missing": [], "command": "",
                           "preset_total": len(PRESET), "preset_have": 0, "shared_count": 0, "job": job(None)}
    if info is None:
        return out
    tools = check_tools(info, fresh)
    missing = [t["formula"] for t in tools if not t["present"]]
    shared = rrun.shared_versions(info)
    out.update(r={"version": info.version, "flavor": info.flavor,
                  "flavor_label": rrun.FLAVOR_LABEL.get(info.flavor, info.flavor), "binaries": info.binaries},
               tools=tools, missing=missing, command=brew_command(missing),
               preset_have=sum(1 for p in PRESET if p in shared), shared_count=len(shared), job=job(info),
               can_build=rrun.dev_dir() is not None or info.binaries)
    return out
