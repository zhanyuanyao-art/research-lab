"""Card 0204: 复现环境. The lab's shared library (one per minor version and build of R) filled by Preinstall through the same
install script and install sandbox as a plan's install step; a plan's install skipping what it holds; the 复现环境
section of 设置 with R's flavor, the build tools and the exact commands, and the preinstall's .jobline. R is a shell
stand-in here: nothing installs anything, nothing touches the network."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest

from research_lab.replication import pipeline, plans, renv, rrun, workspace as W

from .test_replication_rrun import REAL_LOG

HAS_SANDBOX = os.access(W.SANDBOX_EXEC, os.X_OK)
needs_sandbox = pytest.mark.skipif(not HAS_SANDBOX, reason="macOS sandbox-exec only")

# Rscript, as far as the install step sees it: the probe answers like R does; given the generated install script it walks
# its one("…") calls in order, says `have` for what the shared library (R_LIBS_SITE) holds, and "installs" the rest into
# R_LIBS_USER by writing a DESCRIPTION, except `sf`, whose build stops at a missing header the way a configure script does.
FAKE_INSTALL = r"""#!/bin/sh
case "$1" in
  --version) echo "Rscript (R) version 4.6.1 (2026-06-24)" >&2; exit 0;;
esac
if [ "$2" = "-e" ]; then
  case "$3" in *'"RL_HOME=", R.home()'*) printf 'RL_HOME=%s\n' '@HOME@';; esac
  case "$3" in *'RL_PKGTYPE=", .Platform$pkgType'*) echo 'RL_PKGTYPE=@PKGTYPE@';; esac
  case "$3" in *'RL_PKGOPT=", getOption("pkgType")'*) echo 'RL_PKGOPT=@PKGOPT@';; esac
  exit 0
fi
echo "R_LIBS_SITE=$R_LIBS_SITE"
for p in $(sed -n 's/^ok <- one("\([^"]*\)")$/\1/p' "$2"); do
  if [ -d "$R_LIBS_SITE/$p" ] || [ -d "$R_LIBS_USER/$p" ]; then echo "[rr] have $p"; continue; fi
  echo "[rr] start $p"
  if [ "$p" = "sf" ]; then
    printf '%s\n' "* installing *source* package ‘sf’ ..." "configure: error: gdal-config not found or not executable." \
      "<stdin>:1:10: fatal error: 'gdal.h' file not found" "ERROR: configuration failed for package ‘sf’"
    echo "[rr] fail sf 2"
    continue
  fi
  mkdir -p "$R_LIBS_USER/$p"
  printf 'Package: %s\nVersion: 1.0\nBuilt: R 4.6.1; ; 2026-10-21; unix\n' "$p" > "$R_LIBS_USER/$p/DESCRIPTION"
  echo "[rr] ok $p 1"
done
echo "[rr] finished"
"""


def _fake_r(tmp_path: Path, monkeypatch, *, home_parts=("fakeR",), official: bool = False) -> Path:
    """An R stand-in under tmp_path; `official` answers the probe as CRAN's build does (.Platform$pkgType a mac binary
    type, getOption("pkgType") "both"), else as Homebrew's ("source" for both)."""
    home = tmp_path.joinpath(*home_parts)
    (home / "bin" / "exec").mkdir(parents=True)
    (home / "bin" / "exec" / "R").write_text("#!/bin/sh\nexit 0\n")
    (home / "bin" / "exec" / "R").chmod(0o755)
    rscript = home.parent / "Rscript"                     # under the prefix the install sandbox may run
    types = ("mac.binary.big-sur-arm64", "both") if official else ("source", "source")
    rscript.write_text(FAKE_INSTALL.replace("@HOME@", str(home)).replace("@PKGTYPE@", types[0]).replace("@PKGOPT@", types[1]))
    rscript.chmod(0o755)
    monkeypatch.setenv("RL_RSCRIPT", str(rscript))
    return home


@pytest.fixture()
def fake_r(tmp_path, monkeypatch, repo):
    return _fake_r(tmp_path, monkeypatch)


def _package(lib: Path, name: str, version: str = "1.0") -> None:
    (lib / name).mkdir(parents=True, exist_ok=True)
    (lib / name / "DESCRIPTION").write_text(f"Package: {name}\nVersion: {version}\nBuilt: R 4.6.1; ; 2026-10-01; unix\n")


def _wait_idle(ws: Path, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while rrun.busy_in(ws) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not rrun.busy_in(ws)


# ---------------------------------------------------------------- commands

def test_commands_are_built_from_the_formula_list_only():
    assert renv.brew_command(["fribidi", "cmake", "evil; rm -rf /", "pkgconf", "cmake"]) == "brew install cmake pkgconf fribidi"
    assert renv.brew_command([]) == "" and renv.brew_command(["--cask"]) == ""
    assert renv.command_ok("brew install cmake pkgconf harfbuzz fribidi")
    for bad in ("brew install fribidi cmake", "brew install cmake;", "brew install", "brew  install cmake", "sudo brew install cmake",
                "brew install cmake --force"):
        assert not renv.command_ok(bad), bad
    formulas = set(renv.FORMULAS)
    assert set(pipeline.HEADER_FORMULA.values()) <= formulas
    assert {f for fs in renv.PACKAGE_FORMULAS.values() for f in fs} <= formulas
    assert {t[0] for t in renv.TOOLS} <= formulas and len(renv.FORMULAS) == len(formulas)
    assert len(renv.PRESET) == 19 and {"tidyverse", "lme4", "rstan", "sf", "readtext"} <= set(renv.PRESET)


def test_tools_are_found_by_which_the_prefix_and_pkg_config_with_a_time_limit_and_kept_briefly(tmp_path, monkeypatch):
    prefix = tmp_path / "brew"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "opt" / "harfbuzz").mkdir(parents=True)
    for name, body in (("brew", "exit 0"), ("cmake", "exit 0"),
                       ("pkg-config", 'case "$2" in fribidi) exit 0;; gdal) sleep 5; exit 0;; *) exit 1;; esac')):
        (prefix / "bin" / name).write_text(f"#!/bin/sh\n{body}\n")
        (prefix / "bin" / name).chmod(0o755)
    monkeypatch.setattr(renv, "brew_prefix", lambda info: prefix)
    monkeypatch.setattr(renv, "_search_path", lambda p: str(prefix / "bin"))
    monkeypatch.setattr(renv, "PROBE_TIMEOUT_S", 0.5)
    t0 = time.monotonic()
    got = {t["formula"]: t["present"] for t in renv.check_tools(None, fresh=True)}
    assert time.monotonic() - t0 < 5                                  # a hanging pkg-config costs its time limit, no more
    assert got["cmake"] and got["pkgconf"] and got["harfbuzz"] and got["fribidi"]
    assert not got["freetype"] and not got["gdal"] and not got["udunits"]
    (prefix / "bin" / "cmake").unlink()
    assert {t["formula"]: t["present"] for t in renv.check_tools(None)}["cmake"]          # kept CHECK_TTL seconds
    assert not {t["formula"]: t["present"] for t in renv.check_tools(None, fresh=True)}["cmake"]
    monkeypatch.setattr(rrun, "detect", lambda: None)
    assert renv.missing_formulas(["harfbuzz", "fribidi", "cmake", "zstd"]) == ["cmake", "zstd"]


# ---------------------------------------------------------------- Preinstall and the plan's install

@needs_sandbox
def test_preinstall_fills_the_shared_library_through_the_install_sandbox_and_shows_progress(fake_r, client):
    info = rrun.detect()
    assert info.flavor == "other" and renv.start(info) is True
    ws = W._real(rrun.shared_dir(info))
    assert ws == W._real(rrun.shared_root() / "4.6-other")
    _wait_idle(ws)
    lib = ws / "rlib"
    assert sorted(p.name for p in lib.iterdir()) == sorted({*renv.PRESET, "nloptr"} - {"sf"})
    script = (ws / "tmp" / "rinstall.R").read_text()
    assert 'ptype <- "source"' in script and script.index('one("nloptr")') < script.index('one("lme4")')
    assert "R_LIBS_SITE=/nonexistent" in (ws / "logs" / "rinstall.log").read_text()       # no shared library of its own
    result = json.loads((ws / "logs" / "preinstall.json").read_text())
    assert result["key"] == "4.6-other" and set(result["failed"]) == {"sf"}
    assert result["failed"]["sf"] == "缺系统库的头文件 gdal.h" or "gdal.h" in result["failed"]["sf"]
    job = renv.job(info)
    assert job["state"] == "error" and job["done"] == 19 and not job["running"] and job["took"]
    rows = {r["name"]: r for r in job["steps"]}
    assert rows["sf"]["state"] == "failed" and "gdal.h" in rows["sf"]["detail"] and rows["lme4"]["state"] == "done"
    assert rows["tidyverse"]["took"] == "1 秒"
    assert rrun.read_state(ws)["note"].startswith("1 个包没装上：sf")
    view = renv.view()
    assert view["preset_have"] == 18 and view["shared_count"] == 19
    page = client.get("/settings/r-env").text
    assert "常用的 19 个里装好了 18 个" in page and "装完了，1 个没装上" in page and 'data-pkg="sf" data-state="failed"' in page
    assert 'hx-trigger="every 3s"' not in page and ">Preinstall</button>" in page
    assert renv.start(info) is True                                    # again: what is there is not installed twice
    _wait_idle(ws)
    log = (ws / "logs" / "rinstall.log").read_text()
    assert "[rr] have tidyverse" in log and "[rr] start tidyverse" not in log and "[rr] start sf" in log


@needs_sandbox
def test_a_plans_install_skips_what_the_shared_library_holds(conn, fake_r):
    info = rrun.detect()
    shared = rrun.shared_dir(info) / "rlib"
    _package(shared, "lme4", "2.0-6")
    _package(shared, "nloptr", "2.2.1")
    plan_id = plans.create_blank(conn)
    ws = W.workspace_dir(plan_id)
    (ws / "author" / "code").mkdir(parents=True)
    (ws / "author" / "code" / "a.R").write_text("library(lme4)\nlibrary(zoo)\n")
    result = rrun.install(conn, plan_id, fetch=False)
    ws = W._real(ws)
    assert result["ok"] and result["failed"] == {} and {"lme4", "nloptr", "zoo"} <= set(result["installed"])
    assert (ws / "rlib" / "zoo").is_dir() and not (ws / "rlib" / "lme4").exists()
    log = (ws / "logs" / "rinstall.log").read_text()
    assert f"R_LIBS_SITE={W._real(shared)}" in log and "[rr] have lme4" in log and "[rr] have nloptr" in log
    assert json.loads((ws / "logs" / "rlib.json").read_text())["key"] == "4.6-other"
    assert rrun.installed_versions(ws)["lme4"] == "2.0-6" and result["flavor"] == "other"
    assert "共享包库" in (ws / "logs" / "rrun.log").read_text()


def test_a_failed_install_reports_root_causes_and_the_archive(conn, fake_r, monkeypatch):
    """run_install reads the log once: the verdicts from the [rr] lines, each reason from InstallLog."""
    info = rrun.detect()
    plan_id = plans.create_blank(conn)
    ws = W._real(rrun._prepare_ws(plan_id))
    for d in ("logs", "tmp", "rlib"):
        (ws / d).mkdir(exist_ok=True)

    def fake_launch(cmd, env, cwd, stdout, tag, **k):
        stdout.write(REAL_LOG)
        stdout.flush()
        _package(ws / "rlib", "ri", "0.9")
        import subprocess
        return subprocess.Popen(["/usr/bin/true"])

    monkeypatch.setattr(rrun, "launch", fake_launch)
    with rrun.Step(ws, "install") as step:
        got = rrun.run_install(ws, info, ["readtext", "nloptr", "rstan", "ri", "NPC"], "x <- 1\n", timeout=60, step=step,
                               shared=None)
    assert got["archived"] == {"ri": "0.9"} and not got["ok"] and got["finished"]
    assert got["failed"]["rstan"] == "依赖 RcppParallel 没装上：需要 cmake"
    assert got["causes"]["rstan"] == {"kind": "cmake", "root": "RcppParallel", "detail": "", "archived": ""}
    assert got["failed"]["NPC"] == "CRAN 上没有这个包，存档里也没有" and "ri" not in got["failed"]
    needs = pipeline._dep_needs(got["failed"])
    assert any(n["command"] == "brew install cmake" and "RcppParallel" in n["why"] for n in needs)


# ---------------------------------------------------------------- the settings section

TOOLS_FIXED = [{"formula": f, "kind": k, "present": f in ("freetype", "libpng", "poppler"), "needed_by": n}
               for f, _p, k, n in renv.TOOLS]


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_the_settings_page_loads_the_section_after_itself(client):
    page = client.get("/settings").text
    assert 'id="r-env" hx-get="/settings/r-env" hx-trigger="load"' in page and "复现环境" in page
    assert "replication.js" in page


def test_homebrew_r_shows_what_is_missing_the_command_and_the_official_r(client, tmp_path, monkeypatch, repo):
    _fake_r(tmp_path, monkeypatch, home_parts=("Cellar", "r", "4.6.1_1", "lib", "R"))
    monkeypatch.setattr(renv, "check_tools", lambda info, fresh=False: [dict(t) for t in TOOLS_FIXED])
    r = client.get("/settings/r-env")
    assert r.status_code == 200
    page, text = r.text, _text(r.text)
    assert re.findall(r"<h3>([^<]+)</h3>", page) == ["复现环境"]
    assert "R 4.6.1 · Homebrew" in text and "只能从源码编译" in text
    assert "还缺 cmake、pkgconf、harfbuzz、fribidi、jpeg-turbo" in text and "已有 freetype、libpng、poppler" in text
    cmd = re.search(r"data-r-command>([^<]+)<", page).group(1)
    assert cmd == "brew install cmake pkgconf harfbuzz fribidi jpeg-turbo libtiff webp gdal geos proj udunits"
    assert renv.command_ok(cmd)
    assert "data-r-official" in page and renv.DOWNLOAD_URL in page and "下次准备环境时重装" in text
    assert "常用的 19 个里装好了 0 个" in text and "把常用的 19 个 R 包一次装进共享包库" in text
    assert ">Preinstall</button>" in page and ">Check again</button>" in page and 'hx-trigger="every 3s"' not in page
    assert "（" not in text


def test_official_r_takes_binaries_and_needs_no_switch(client, tmp_path, monkeypatch, repo):
    _fake_r(tmp_path, monkeypatch, home_parts=("offR",), official=True)
    assert rrun.detect().pkg_type == "mac.binary.big-sur-arm64" and rrun.detect().binaries
    monkeypatch.setattr(renv, "check_tools", lambda info, fresh=False: [dict(t, present=True) for t in TOOLS_FIXED])
    text = _text(page := client.get("/settings/r-env").text)
    assert "R 4.6.1 · 官方" in text and "先装 CRAN 的二进制包" in text and "都有了" in text
    assert "data-r-official" not in page and "data-r-command" not in page and ">Preinstall</button>" in page


def test_without_r_the_section_says_where_to_get_it(client, monkeypatch):
    monkeypatch.setenv("RL_RSCRIPT", "none")
    page = client.get("/settings/r-env").text
    assert "这台电脑没有找到 R" in _text(page) and renv.DOWNLOAD_URL in page and "Preinstall" not in page


def test_a_running_preinstall_is_one_jobline_that_polls_ticks_and_can_be_stopped(client, fake_r, monkeypatch):
    monkeypatch.setattr(renv, "check_tools", lambda info, fresh=False: [dict(t) for t in TOOLS_FIXED])
    info = rrun.detect()
    ws = rrun.shared_dir(info)
    for d in ("logs", "tmp", "rlib"):
        (ws / d).mkdir(parents=True, exist_ok=True)
    with rrun.Step(ws, "preinstall") as step:
        rrun._write_state(ws, since=int(time.time()) - 90)
        (ws / "logs" / "rinstall.log").write_text("[rr] start tidyverse\nnoise\n[rr] ok tidyverse 75\n[rr] start nloptr\n"
                                                   "[rr] ok nloptr 3\n[rr] start lme4\n")
        page = client.get("/settings/r-env").text
        assert 'hx-get="/settings/r-env" hx-trigger="every 3s"' in page
        assert 'class="jobline" id="r-env-box" data-tone="running"' in page and 'data-since="' in page
        assert "正在预装 · 完成 1/19 · 正在装 lme4" in _text(page)
        assert 'data-pkg="tidyverse" data-state="done"' in page and "1 分 15 秒" in page
        assert 'data-pkg="lme4" data-state="running"' in page and 'data-pkg="sf" data-state="waiting"' in page
        assert ">Stop</button>" in page and ">Preinstall</button>" not in page
        r = client.post("/settings/r-env/preinstall", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/settings?note=preinstall-busy#r-env"
        r = client.post("/settings/r-env/stop", follow_redirects=False)
        assert r.status_code == 303 and rrun._stopped(ws)
        step.finish("stopped", "已停止")
    page = client.get("/settings/r-env").text
    assert 'hx-trigger="every 3s"' not in page and "已停止，可以再装一次" in _text(page)
    assert 'data-pkg="lme4" data-state="failed"' in page


def test_the_preinstall_button_starts_the_job_or_says_why_not(client, monkeypatch):
    monkeypatch.setattr(renv, "start", lambda info=None: True)
    r = client.post("/settings/r-env/preinstall", follow_redirects=False)
    assert r.headers["location"] == "/settings?note=preinstall#r-env"
    assert "已开始预装常用的 R 包" in client.get(r.headers["location"]).text

    def refuse(info=None):
        raise rrun.RRunError("这台电脑没有找到 R。")

    monkeypatch.setattr(renv, "start", refuse)
    r = client.post("/settings/r-env/preinstall", follow_redirects=False)
    assert r.headers["location"] == "/settings?note=preinstall-failed#r-env"
    assert 'class="banner danger"' in client.get(r.headers["location"]).text


def test_without_compilers_a_source_only_r_cannot_preinstall(fake_r, monkeypatch, client):
    monkeypatch.setattr(rrun, "dev_dir", lambda: None)
    monkeypatch.setattr(renv, "check_tools", lambda info, fresh=False: [dict(t) for t in TOOLS_FIXED])
    with pytest.raises(rrun.RRunError, match="xcode-select --install"):
        renv.start()
    page = client.get("/settings/r-env").text
    assert ">Preinstall</button>" not in page and "xcode-select --install" in page


def test_the_clock_script_ticks_every_jobline():
    js = (Path(rrun.__file__).parents[1] / "web" / "static" / "replication.js").read_text()
    assert '".jobline [data-since]"' in js and '"details.jobline[id]"' in js and "rl.runall.open." in js
