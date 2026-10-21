"""R route for a replication plan (card 0186, replication/rrun.py): the package list read out of R text, the install script
generated from validated tokens only, the sandbox that runs the authors' scripts, the stargazer parser behind results.json,
and the run / repair loop with a stand-in for R (the real macOS sandbox, a shell script as the interpreter) plus real R
tests that are skipped when Rscript is absent."""
from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from research_lab.replication import compare, datasets, plans, rrun, rtext, workspace as W

HAS_SANDBOX = os.access(W.SANDBOX_EXEC, os.X_OK)
needs_sandbox = pytest.mark.skipif(not HAS_SANDBOX, reason="macOS sandbox-exec only")
REAL_R = rrun.find_rscript() if os.environ.get("RL_RSCRIPT") is None else None
needs_r = pytest.mark.skipif(REAL_R is None or not HAS_SANDBOX, reason="Rscript or sandbox-exec not available")


# ---------------------------------------------------------------- the package list

LOAD_PACKAGES = '''# All relevant packages
packages <- c("quanteda", "readtext", "plyr", "dplyr",
              "ggplot2", "lmtest", "data.table", "MASS", "stargazer", "lme4")
installedPackages <- packages %in% rownames(installed.packages())
if (any(installedPackages == FALSE)) {
  install.packages(packages[!installedPackages])
}
invisible(lapply(packages, library, character.only = TRUE))
'''

SETUP = '''# Set working directory
setwd("C:/Users/Eric/Dropbox/NSC/")
detach("package:stargazer",unload=T)
remove.packages("stargazer")
download.file("https://cran.r-project.org/src/contrib/stargazer_5.2.3.tar.gz", destfile = "stargazer_5.2.3.tar.gz")
untar("stargazer_5.2.3.tar.gz")
stargazer_src <- readLines("stargazer/R/stargazer-internal.R")
# Move the length check 5 lines up so it precedes is.na(.)
stargazer_src[1990] <- stargazer_src[1995]
stargazer_src[1995] <- ""
writeLines(stargazer_src, con="stargazer/R/stargazer-internal.R")
install.packages("stargazer", repos = NULL, type="source")
'''


def test_the_package_list_reads_vectors_loops_and_pacman_without_running_anything():
    got = rrun.parse_packages({"code/loadPackages.R": LOAD_PACKAGES})
    assert list(got.packages)[:4] == ["quanteda", "readtext", "plyr", "dplyr"]
    assert "lme4" in got.packages and "stargazer" in got.packages
    assert "MASS" in got.packages and "MASS" not in got.to_install            # R's own recommended package
    assert got.packages["quanteda"] == ["loadPackages.R"]
    more = rrun.parse_packages({"a.R": 'pacman::p_load(data.table, "haven", char = c("sf"))\nfor (p in c("zoo", "xts")) library(p, character.only = TRUE)\n'
                                       'suppressPackageStartupMessages(library(tidyr))\nx <- broom::tidy(m)\nrequire("lubridate")\n'
                                       '# library(notloaded)\nlibrary(survival)\n'})
    assert {"data.table", "haven", "sf", "zoo", "xts", "tidyr", "broom", "lubridate"} <= set(more.packages)
    assert "notloaded" not in more.packages and "survival" not in more.to_install


def test_the_package_list_infers_a_package_from_the_functions_a_script_uses():
    got = rrun.parse_packages({"code/m.R": 'd <- fread("./data/x.csv")\nstargazer(fit)\nggsave("a.pdf")\nm <- glmer(y ~ x + (1|g), d)\n'})
    assert {"data.table", "stargazer", "ggplot2", "lme4"} <= set(got.packages)
    assert any("fread" in w for w in got.packages["data.table"])


def test_a_pinned_version_a_patch_recipe_and_anything_unusual_are_recorded():
    got = rrun.parse_packages({"code/setup.R": SETUP + 'remotes::install_github("a/b")\nremotes::install_version("zoo", version = "1.8-9")\n'
                                                 'download.file("http://evil.example/x_1.0.tar.gz", "x.tar.gz")\n'})
    pin = got.pins["stargazer"]
    assert pin.version == "5.2.3" and pin.urls[0].startswith("https://cloud.r-project.org/src/contrib/stargazer_5.2.3")
    patch = got.patches["stargazer"]
    assert patch.file == "R/stargazer-internal.R" and patch.edits == [(1990, 1995), (1995, "")]
    assert got.pins["zoo"].version == "1.8-9"
    text = "\n".join(got.notes)
    assert "install_github(a/b)" in text and "download.file(http://evil.example" in text
    assert "evil.example" not in json.dumps([p.urls for p in got.pins.values()])
    assert "x" not in got.pins


def test_stargazer_gets_the_published_fix_when_the_authors_setup_script_was_not_saved():
    got = rrun.parse_packages({"code/m.R": "stargazer(fit)\n"})
    patch = got.patches["stargazer"]
    assert got.pins["stargazer"].version == "5.2.3" and patch.guard == [(1995, "if (length(s) > 1)")]
    assert any("setup.R" in n for n in got.notes)


def test_the_install_script_is_built_from_validated_tokens_only():
    hostile = {"a.R": 'library("x\\"); system(\\"touch /tmp/pwned\\"); (\\"")\nlibrary(good)\ninstall.packages("bad name")\n'
                      'p <- c("evil\\"); q()")\nlapply(p, library, character.only = TRUE)\n'}
    got = rrun.parse_packages(hostile)
    script = rrun.install_script(got)
    assert 'one("good")' in script and "system(" not in script and "pwned" not in script and "q()" not in script
    full = rrun.install_script(rrun.parse_packages({"code/setup.R": SETUP}))
    assert 'pinned("stargazer", "5.2.3"' in full and "list(1990L, 1995L)" in full and 'list(1995L, "")' in full
    assert 'repos = c(CRAN = "https://cloud.r-project.org")' in full
    assert "github" not in full.lower() and "http://" not in full


def test_install_orders_what_a_dependent_needs_first_and_reads_the_log():
    script = rrun.install_script(rrun.parse_packages({"a.R": "library(lme4)\nlibrary(zoo)\n"}))
    assert script.index('one("nloptr")') < script.index('one("lme4")') < script.index('one("zoo")')
    log = "[rr] ok zoo 4\n[rr] have stargazer 5.2.3\n[rr] error nloptr cmake is required\n[rr] fail nloptr 9\n[rr] fail lme4 1\n[rr] finished\n"
    parsed = rrun.parse_install_log(log)
    assert parsed["installed"] == ["zoo"] and parsed["had"] == ["stargazer"] and set(parsed["failed"]) == {"nloptr", "lme4"}
    assert parsed["finished"]
    log2 = ("* installing *source* package ‘pdftools’ ...\n<stdin>:1:10: fatal error: 'poppler-document.h' file not found\n"
            "ERROR: configuration failed for package ‘pdftools’\n* installing *source* package ‘nloptr’ ...\n"
            "checking for cmake... no\nERROR: configuration failed for package ‘nloptr’\n"
            "Warning in install.packages(pkg) :\n  installation of 4 packages failed:  ‘nloptr’, ‘lme4’, ‘pbkrtest’, ‘glmmTMB’\n"
            "Warning in install.packages(pkg) :\n  installation of 2 packages failed:  ‘pdftools’, ‘readtext’\n"
            "Warning: package ‘crfsuite’ is not available for this version of R\n")
    assert rrun.failure_hint(log2, "nloptr") == "需要 cmake"
    assert "poppler-document.h" in rrun.failure_hint(log2, "pdftools")
    assert rrun.failure_hint(log2, "glmmTMB") == "依赖 nloptr 没装上：需要 cmake"
    assert rrun.failure_hint(log2, "readtext").startswith("依赖 pdftools 没装上：缺系统库的头文件 poppler-document.h")
    assert rrun.failure_hint(log2, "crfsuite").startswith("CRAN 上没有这个包") and rrun.failure_hint(log2, "zzz") == ""
    assert not any("（" in rrun.failure_hint(log2, p) for p in ("nloptr", "pdftools", "glmmTMB", "readtext", "crfsuite"))


# The shape of a real install log of Homebrew's R on this Mac (card 0204): a parallel install prints each package's
# build after `make`, a long list of what failed goes on the line after "failed:", RcppParallel says "requires cmake",
# configure scripts that cannot find pkg-config fall back to guessing and stop at a header, and the script's own [rr]
# lines follow each call (archive routing: crfsuite from the archive fails to build, NPC is in no archive, ri installs).
REAL_LOG = """[rr] have stargazer 5.2.3
[rr] start readtext
also installing the dependency ‘pdftools’

begin installing package ‘pdftools’
make: *** [pdftools.ts] Error 1
* installing *source* package ‘pdftools’ ...
** using staged installation
Homebrew 7.0.9
Using PKG_CFLAGS=-I/opt/homebrew/opt/poppler/include/cpp
--------------------------- [ANTICONF] --------------------------------
Configuration failed to find 'poppler-cpp' system library. Try installing:
 * brew: poppler (MacOS)
If poppler-cpp is already installed, check that 'pkg-config' is in your
PATH and PKG_CONFIG_PATH contains a poppler-cpp.pc file. If pkg-config
-------------------------- [ERROR MESSAGE] ---------------------------
<stdin>:1:10: fatal error: 'poppler-document.h' file not found
    1 | #include <poppler-document.h>
1 error generated.
--------------------------------------------------------------------
ERROR: configuration failed for package ‘pdftools’
Warning in install.packages(pkg, lib = lib, type = "source", dependencies = NA,  :
  installation of 2 packages failed:  ‘pdftools’, ‘readtext’
[rr] fail readtext 6
[rr] start tidyverse
* installing *source* package ‘textshaping’ ...
Configuration failed to find the --static harfbuzz freetype2 fribidi library. Try installing:
<stdin>:1:10: fatal error: 'hb-ft.h' file not found
ERROR: configuration failed for package ‘textshaping’
Warning in install.packages(pkg, lib = lib, type = "source", dependencies = NA,  :
  installation of 3 packages failed:  ‘textshaping’, ‘ragg’, ‘tidyverse’
[rr] fail tidyverse 3
[rr] start nloptr
* installing *source* package ‘nloptr’ ...
checking whether the C++ compiler works... yes
checking for pkg-config... no
checking for cmake... no

------------------ CMAKE NOT FOUND --------------------
 - brew install --cask cmake       (MacOS; inside a terminal with Homebrew)
ERROR: configuration failed for package ‘nloptr’
Warning in install.packages(pkg, lib = lib, type = "source", dependencies = NA,  :
  installation of package ‘nloptr’ had non-zero exit status
[rr] fail nloptr 4
[rr] start rstan
* installing *source* package ‘RcppParallel’ ...
** preparing to configure package 'RcppParallel' ...
Error in eval(parse(text = contents), envir = envir) :
  error: RcppParallel requires cmake (>= 3.5); cmake was not found
ERROR: configuration failed for package ‘RcppParallel’
Warning in install.packages(pkg, lib = lib, type = "source", dependencies = NA,  :
  installation of 3 packages failed:  ‘RcppParallel’, ‘StanHeaders’, ‘rstan’
[rr] fail rstan 3
[rr] start rstanarm
* installing *source* package ‘nloptr’ ...
checking for cmake... no
ERROR: configuration failed for package ‘nloptr’
* installing *source* package ‘RcppParallel’ ...
  error: RcppParallel requires cmake (>= 3.5); cmake was not found
ERROR: configuration failed for package ‘RcppParallel’
Warning in install.packages(pkg, lib = lib, type = "source", dependencies = NA,  :
  installation of 8 packages failed:
  ‘nloptr’, ‘RcppParallel’, ‘lme4’, ‘rstantools’, ‘StanHeaders’, ‘rstan’, ‘shinystan’, ‘rstanarm’
[rr] fail rstanarm 9
[rr] start crfsuite
[rr] archive crfsuite 0.4.2
* installing *source* package ‘crfsuite’ ...
crfsuite.cpp:12:5: error: no matching function for call to 'crf1d_create'
1 error generated.
ERROR: compilation failed for package ‘crfsuite’
[rr] fail crfsuite 7 0.4.2
[rr] start NPC
[rr] fail NPC 0 not-on-cran
[rr] start ri
[rr] archive ri 0.9
[rr] ok ri 2 0.9
[rr] finished
"""


def test_a_real_logs_failures_come_back_to_their_root_cause():
    parsed = rrun.parse_install_log(REAL_LOG)
    assert parsed["archived"] == {"crfsuite": "0.4.2", "ri": "0.9"} and parsed["finished"]
    assert set(parsed["failed"]) == {"readtext", "tidyverse", "nloptr", "rstan", "rstanarm", "crfsuite", "NPC"}
    log = rrun.InstallLog(REAL_LOG, pkg_config=False)          # no pkg-config on the install step's PATH
    assert log.lists[-1] == ["nloptr", "RcppParallel", "lme4", "rstantools", "StanHeaders", "rstan", "shinystan", "rstanarm"]
    hint = {p: rrun.hint_of(log.cause(p), p) for p in parsed["failed"]}
    assert hint["nloptr"] == "需要 cmake"
    assert hint["rstan"] == "依赖 RcppParallel 没装上：需要 cmake"         # its own call's list, not a later one
    assert hint["rstanarm"] == "依赖 nloptr 没装上：需要 cmake"           # read off the list on the next line
    assert hint["readtext"] == "依赖 pdftools 没装上：需要 pkg-config，还缺头文件 poppler-document.h"
    assert hint["tidyverse"] == "依赖 textshaping 没装上：需要 pkg-config，还缺头文件 hb-ft.h"
    assert hint["crfsuite"].startswith("已从 CRAN 下架，存档里的 0.4.2 版也没装上：error: no matching function")
    assert hint["NPC"] == "CRAN 上没有这个包，存档里也没有"
    assert log.cause("rstan").as_dict() == {"kind": "cmake", "root": "RcppParallel", "detail": "", "archived": ""}
    with_pc = rrun.InstallLog(REAL_LOG)                       # pkg-config there: the header is the reason
    assert rrun.hint_of(with_pc.cause("readtext"), "readtext") == "依赖 pdftools 没装上：缺系统库的头文件 poppler-document.h"
    assert all("（" not in h for h in hint.values())


SOURCE_ONLY_LOG = """[rr] start lme4
Packages which are only available in source form, and may need compilation of C/C++/Fortran: ‘nloptr’
  ‘RcppParallel’
  These will not be installed
trying URL 'https://cloud.r-project.org/bin/macosx/big-sur-arm64/contrib/4.6/lme4_2.0-6.tgz'
[rr] fail lme4 30 lacking nloptr
[rr] start rstan
[rr] fail rstan 12 lacking RcppParallel,StanHeaders
[rr] finished
"""


def test_what_r_left_out_for_having_no_binary_fails_the_package_that_loads_it():
    log = rrun.InstallLog(SOURCE_ONLY_LOG)
    assert log.source_only == {"nloptr", "RcppParallel"}
    hint = {p: rrun.hint_of(log.cause(p), p) for p in ("lme4", "rstan")}
    assert hint["lme4"] == "依赖 nloptr 没装上：没有二进制包，要编译才能装，先装 Xcode 命令行工具"
    assert hint["rstan"] == "依赖 RcppParallel 没装上：没有二进制包，要编译才能装，先装 Xcode 命令行工具"
    from research_lab.replication import pipeline
    need = pipeline._dep_needs(hint)[0]
    assert need["command"] == "xcode-select --install" and set(need["packages"]) == {"lme4", "rstan"}
    # R said nothing about it: the lacking note itself names what is missing
    bare = rrun.InstallLog("[rr] fail car 3 lacking pbkrtest\n")
    assert rrun.hint_of(bare.cause("car"), "car") == "依赖的包 pbkrtest 没装上"


def test_a_package_installed_without_what_it_loads_is_not_counted_installed(conn, jost, fake_r, monkeypatch):
    info = rrun.detect()
    ws = jost["ws"]
    for d in ("logs", "tmp", "rlib"):
        (ws / d).mkdir(exist_ok=True)

    def fake_launch(cmd, env, cwd, stdout, tag, **k):
        stdout.write(SOURCE_ONLY_LOG)
        stdout.flush()
        _package(ws / "rlib", "lme4", "2.0-6")                # the binary went in, its dependency did not
        return subprocess.Popen(["/usr/bin/true"])

    monkeypatch.setattr(rrun, "launch", fake_launch)
    with rrun.Step(ws, "install") as step:
        got = rrun.run_install(ws, info, ["lme4"], "x <- 1\n", timeout=60, step=step, shared=None)
    assert not got["ok"] and "lme4" in got["installed"] and got["failed"]["lme4"].startswith("依赖 nloptr 没装上")
    assert got["causes"]["lme4"]["kind"] == "binary"


def test_the_install_script_goes_binary_first_only_for_the_official_r_and_routes_archived_packages():
    plist = rrun.parse_packages({"a.R": "library(lme4)\nlibrary(crfsuite)\n"})
    src = rrun.install_script(plist)
    assert 'ptype <- "source"' in src and '"both"' not in src and "compile.from.source" not in src
    binary = rrun.install_script(plist, binary=True)
    assert 'options(install.packages.compile.from.source = "always")' in binary         # what has no binary compiles
    assert 'startsWith(.Platform$pkgType, "mac.binary")) "both" else "source"' in binary
    assert 'getOption("pkgType")' not in binary                                      # "both" on the official build
    no_tools = rrun.install_script(plist, binary=True, compile=False)
    assert 'options(install.packages.compile.from.source = "never")' in no_tools
    for script in (src, binary, no_tools):
        # installed means: with every package it loads; one without them is a failure that names them
        assert 'tools::package_dependencies(pkg, db = d, which = c("Depends", "Imports"), recursive = TRUE)' in script
        assert 'if (pkg %in% have() && !length(lacking(pkg))) { msg("have", pkg)' in script
        assert 'msg("fail", pkg, secs(t0), "lacking", paste(head(gap, 20), collapse = ","))' in script
        assert "type = ptype" in script and ".libPaths(lib)" in script
        assert "lib.loc = c(lib, .Library.site)" in script                 # what the shared library holds is not installed again
        assert '"https://cloud.r-project.org/src/contrib/Meta/archive.rds"' in script
        assert "if (!on_cran(pkg)) return(invisible(archived(pkg, t0)))" in script
        assert 'paste0("https://cloud.r-project.org/src/contrib/Archive/", pkg, "/", f)' in script
        assert "packageVersion(pkg, lib.loc = lib)" in script and "pkg %in% here()" in script
        assert "github" not in script.lower() and "http://" not in script


def official_probe(home: Path) -> str:
    """Shell that answers the probe the way CRAN's official R does, expression by expression: .Platform$pkgType is
    "mac.binary.big-sur-arm64", getOption("pkgType") is "both" (utils sets it when it loads)."""
    return f"""case "$1" in --version) echo "Rscript (R) version 4.6.1 (2026-06-24)" >&2; exit 0;; esac
case "$3" in *'"RL_HOME=", R.home()'*) printf 'RL_HOME=%s\\n' '{home}';; esac
case "$3" in *'RL_PKGTYPE=", .Platform$pkgType'*) echo RL_PKGTYPE=mac.binary.big-sur-arm64;;
  *'RL_PKGTYPE=", getOption("pkgType")'*) echo RL_PKGTYPE=both;; esac
case "$3" in *'RL_PKGOPT=", getOption("pkgType")'*) echo RL_PKGOPT=both;; esac
"""


@pytest.fixture()
def official_r(tmp_path, monkeypatch):
    """A stand-in for CRAN's official R: its probe says the package type is a macOS binary one."""
    home = tmp_path / "offR"
    (home / "bin" / "exec").mkdir(parents=True)
    (home / "bin" / "exec" / "R").write_text("#!/bin/sh\nexit 0\n")
    (home / "bin" / "exec" / "R").chmod(0o755)
    rscript = tmp_path / "RscriptOfficial"
    rscript.write_text("#!/bin/sh\n" + official_probe(home))
    rscript.chmod(0o755)
    monkeypatch.setenv("RL_RSCRIPT", str(rscript))
    return home


def test_the_official_r_is_found_first_and_takes_binaries(official_r):
    assert rrun.RSCRIPT_DIRS[0] == "/Library/Frameworks/R.framework/Resources/bin"
    info = rrun.detect()
    assert 'RL_PKGTYPE=", .Platform$pkgType' in rrun.PROBE_CODE
    assert info.pkg_type == "mac.binary.big-sur-arm64" and info.flavor == "official" and info.binaries
    assert info.key == "4.6-official"
    assert rrun.flavor_of("/Library/Frameworks/R.framework/Versions/4.6-arm64/Resources") == "official"
    assert rrun.flavor_of("/opt/homebrew/Cellar/r/4.6.1_1/lib/R") == "homebrew"
    plain = rrun.RInfo("/x/Rscript", "4.6.1", "/opt/homebrew/Cellar/r/4.6.1_1/lib/R", "")
    assert plain.flavor == "homebrew" and not plain.binaries and plain.key == "4.6-homebrew"


def test_detect_still_reads_an_r_that_prints_only_its_home(fake_r):
    info = rrun.detect()
    assert info.home == str(fake_r) and info.pkg_type == "" and info.flavor == "other" and not info.binaries


def _package(lib: Path, name: str, version: str = "1.0", built: str = "R 4.6.1; aarch64-apple-darwin27.0.0; 2026-10-01; unix"):
    (lib / name).mkdir(parents=True, exist_ok=True)
    (lib / name / "DESCRIPTION").write_text(f"Package: {name}\nVersion: {version}\nBuilt: {built}\n")


def test_the_shared_library_comes_after_the_plans_own_and_counts_as_installed(conn, jost, fake_r):
    info = rrun.detect()
    assert rrun.shared_lib(info) is None and rrun.installed_versions(jost["ws"]) == {}
    shared = rrun.shared_dir(info) / "rlib"
    _package(shared, "lme4", "2.0-6")
    _package(shared, "zoo", "1.8")
    _package(jost["ws"] / "rlib", "zoo", "1.9")
    assert rrun.shared_lib(info) == W._real(shared)
    assert rrun.installed_versions(jost["ws"]) == {"lme4": "2.0-6", "zoo": "1.9"}      # the plan's own wins
    assert rrun.installed_versions(jost["ws"], shared=False) == {"zoo": "1.9"}
    cmd, env, _ = rrun.sandbox_command(jost["ws"], "code/a.R", "", info)
    assert env["R_LIBS_SITE"] == str(W._real(shared)) and env["R_LIBS_USER"] == str(jost["ws"] / "rlib")
    assert f"SHARED_LIB={W._real(shared)}" in cmd
    icmd, ienv = rrun.install_command(jost["ws"], info, jost["ws"] / "tmp" / "x.R", shared=rrun.shared_lib(info))
    assert ienv["R_LIBS_SITE"] == str(W._real(shared)) and f"SHARED_LIB={W._real(shared)}" in icmd
    alone, _ = rrun.install_command(jost["ws"], info, jost["ws"] / "tmp" / "x.R")
    assert "SHARED_LIB=/nonexistent/rlib-shared" in alone
    # a folder above the workspace, the home folder or / is never handed to the sandbox as the shared library
    for bad in (jost["ws"].parent, Path.home(), Path("/")):
        assert "SHARED_LIB=/nonexistent/rlib-shared" in rrun.sandbox_prefix(jost["ws"], info.home, shared=bad)


@needs_sandbox
def test_a_run_says_when_the_plans_library_was_built_by_another_r(conn, jost, fake_r, monkeypatch):
    (jost["ws"] / "author" / "code" / "loadPackages.R").write_text("library(data.table)\n")
    no_ai(monkeypatch)
    _package(jost["ws"] / "rlib", "zoo")
    official = rrun.RInfo("/y", "4.6.1", "/Library/Frameworks/R.framework/Resources", "", "mac.binary.big-sur-arm64")
    rrun._write_library_key(jost["ws"], official)
    assert rrun.built_by_other_r(jost["ws"], rrun.detect()) == "4.6-official"
    rrun.run(conn, jost["id"])
    log = (jost["ws"] / "logs" / "rrun.log").read_text()
    assert "这份方案的包是 4.6-official 的 R 装的，现在用的是 4.6-other" in log and "先准备环境" in log


def test_a_library_built_by_another_r_is_emptied_before_the_install(conn, jost, fake_r):
    ws = jost["ws"]
    lib = ws / "rlib"
    homebrew = rrun.RInfo("/x", "4.6.1", "/opt/homebrew/Cellar/r/4.6.1_1/lib/R", "", "source")
    official = rrun.RInfo("/y", "4.6.1", "/Library/Frameworks/R.framework/Resources", "", "mac.binary.big-sur-arm64")
    assert rrun.rebuild_if_other_r(ws, official) == ""                     # nothing there
    _package(lib, "zoo")                                                   # built before card 0204: no key, Homebrew's
    assert rrun.rebuild_if_other_r(ws, homebrew) == "" and (lib / "zoo").exists()
    assert rrun.rebuild_if_other_r(ws, official) == "4.6-homebrew" and lib.is_dir() and not any(lib.iterdir())
    _package(lib, "zoo")
    rrun._write_library_key(ws, official)
    assert rrun.rebuild_if_other_r(ws, official) == "" and (lib / "zoo").exists()
    newer = rrun.RInfo("/y", "4.7.0", "/Library/Frameworks/R.framework/Resources", "", "mac.binary.big-sur-arm64")
    assert rrun.rebuild_if_other_r(ws, newer) == "4.6-official" and not (lib / "zoo").exists()
    _package(lib, "zoo", built="R 4.5.2; ; 2026-01-01; unix")              # an old minor version, no key
    (ws / "logs" / "rlib.json").unlink()
    assert rrun.rebuild_if_other_r(ws, homebrew) == "4.5-homebrew"


# ---------------------------------------------------------------- the stargazer parser

LATEX = r"""% Table created by stargazer v.5.2.3 by Marek Hlavac, Social Policy Institute. E-mail: marek.hlavac at gmail.com
\begin{table}[!htbp] \centering
  \caption{}
  \label{}
\begin{tabular}{@{\extracolsep{0pt}}lD{.}{.}{-3} D{.}{.}{-3} D{.}{.}{-3} D{.}{.}{-3} }
\\[-1.8ex]\hline
\hline \\[-1.8ex]
 & \multicolumn{4}{c}{\textit{Dependent variable:}} \\
\cline{2-5}
\\[-1.8ex] & \multicolumn{1}{c}{outcome} & \multicolumn{1}{c}{outDiff} & \multicolumn{2}{c}{outcome} \\
\\[-1.8ex] & \multicolumn{1}{c}{\textit{Poisson}} & \multicolumn{1}{c}{\textit{OLS}} & \multicolumn{1}{c}{\textit{Poisson}} & \multicolumn{1}{c}{\textit{OLS}} \\
\\[-1.8ex] & \multicolumn{1}{c}{(1)} & \multicolumn{1}{c}{(2)} & \multicolumn{1}{c}{(3)} & \multicolumn{1}{c}{(4)} \\
\hline \\[-1.8ex]
 Mean Hawkishness & 5.101^{***} & 0.881^{**} &  &  \\
  & (1.450) & (0.443) &  &  \\
  President's Hawkishness &  &  & 0.218 & -0.020 \\
  &  &  & (0.522) & (0.178) \\
  No. of Attendees & 0.011 & 0.022^{***} & 0.032 & 0.025^{***} \\
  & (0.021) & (0.008) & (0.020) & (0.007) \\
  Constant & -7.056^{***} & -1.180^{***} & -4.339^{***} & -0.215 \\
  & (1.205) & (0.410) & (0.650) & (0.187) \\
 \hline \\[-1.8ex]
Observations & \multicolumn{1}{c}{2,685} & \multicolumn{1}{c}{2,685} & \multicolumn{1}{c}{2,650} & \multicolumn{1}{c}{2,650} \\
\hline
\hline \\[-1.8ex]
\textit{Note:}  & \multicolumn{4}{r}{$^{*}$p$<$0.1; $^{**}$p$<$0.05; $^{***}$p$<$0.01} \\
\end{tabular}
\end{table}
"""

TEXT = """
===================================================
                    Dependent variable:
             ---------------------------------------
                       y1             y2
                     Poisson          OLS
                       (1)            (2)
---------------------------------------------------
Treated post         -0.231**         0.140
                     (0.110)         (0.090)
Share of women                       -1.250***
                                     (0.301)
Constant              2.000           0.5
                     (0.400)         (0.2)
---------------------------------------------------
Observations         1,204           1,198
R2                                    0.31
===================================================
Note:                *p<0.1; **p<0.05; ***p<0.01
"""

SCRIPT_WITH_TABLE = '''source("./code/loadPackages.R")
nscd_all = fread("./data/meetingData.csv")
fit1 = glm(y ~ x, data = nscd_all, family = "poisson")

#### Table 3: Effect of Participant Hawkishness on Foreign Policy Decisions ####
stargazer(fit1, fit2, no.space = T)

#### Table A9: Something in the appendix ####
stargazer(fit3)
ggsave(filename = "./figures/coefPlotMain.pdf")
'''


def test_the_latex_table_becomes_one_results_table_per_model_column():
    blocks = rrun.parse_stargazer("some R output\n" + LATEX + "\nmore output\n")
    assert len(blocks) == 1
    b = blocks[0]
    assert b.models == ["(1)", "(2)", "(3)", "(4)"] and b.types == ["Poisson", "OLS", "Poisson", "OLS"]
    assert b.dep == ["outcome", "outDiff", "outcome", "outcome"]
    assert [t for t, _ in b.rows] == ["Mean Hawkishness", "President's Hawkishness", "No. of Attendees", "Constant"]
    assert b.stats["Observations"] == ["2,685", "2,685", "2,650", "2,650"]
    assert b.raw.startswith("% Table created by stargazer") and b.raw.rstrip().endswith("\\end{table}") and "Mean Hawkishness" in b.raw
    calls = rrun.stargazer_calls(SCRIPT_WITH_TABLE)
    assert [c["label"] for c in calls] == ["Table 3", "Table A9"]
    res = rrun.results_from_blocks([("code/meetingAnalysis.R", blocks, calls)])
    assert W.validate_results(res) == []
    by = {(t["label"], t["model"]): t for t in res["tables"]}
    t1 = by[("Table 3", "(1)")]
    assert t1["estimator"] == "Poisson" and t1["needs_r"] is False
    row = next(r for r in t1["rows"] if r["term"] == "Mean Hawkishness")
    assert row == {"term": "Mean Hawkishness", "estimate": 5.101, "se": 1.45, "p": None, "ci": None, "n": 2685}
    assert {r["term"] for r in by[("Table 3", "(3)")]["rows"]} == {"President's Hawkishness", "No. of Attendees", "Constant"}
    assert next(r for r in by[("Table 3", "(4)")]["rows"] if r["term"] == "President's Hawkishness")["estimate"] == -0.02
    assert by[("Table 3", "(3)")]["rows"][0]["n"] == 2650


def test_the_text_table_is_read_by_column_position_so_blank_cells_do_not_shift_values():
    b = rrun.parse_stargazer(TEXT)[0]
    assert b.kind == "text" and b.models == ["(1)", "(2)"] and b.types == ["Poisson", "OLS"]
    assert b.raw.startswith("=====") and b.raw.rstrip().endswith("***p<0.01") and "Share of women" in b.raw
    rows = {t: c for t, c in b.rows}
    assert rows["Treated post"][0].estimate == -0.231 and rows["Treated post"][0].se == 0.11 and rows["Treated post"][0].stars == 2
    assert rows["Share of women"][0] is None and rows["Share of women"][1].estimate == -1.25
    assert rows["Share of women"][1].se == 0.301 and rows["Share of women"][1].stars == 3
    assert rows["Constant"][1].estimate == 0.5
    assert b.stats["Observations"] == ["1,204", "1,198"]
    res = rrun.results_from_blocks([("s.R", [b], [])])
    assert res["tables"][0]["label"] == "s.R 第 1 个表" and W.validate_results(res) == []


def test_a_confidence_interval_cell_and_a_table_without_model_numbers():
    ci = "\n".join(["\\begin{tabular}{lc}", " & (1) \\\\", "x & 1.5 \\\\", " & (0.5, 2.5) \\\\", "\\end{tabular}"])
    b = rrun.parse_stargazer(ci)[0]
    assert b.rows[0][1][0].ci == (0.5, 2.5) and b.rows[0][1][0].se is None
    desc = "\n".join(["\\begin{tabular}{lcc}", "Statistic & Mean & Max \\\\", "x & 1.2 & 3 \\\\", "\\end{tabular}"])
    assert rrun.parse_stargazer(desc) == []


def test_a_term_that_differs_from_the_target_only_in_case_and_punctuation_takes_the_targets_spelling():
    blocks = rrun.parse_stargazer(LATEX)
    calls = rrun.stargazer_calls(SCRIPT_WITH_TABLE)
    targets = [{"id": "T1", "table": "Table 3", "model": "(1)", "term": "MEAN HAWKISHNESS", "value": 5.101, "value_text": "5.101",
                "se": 1.45, "se_text": "(1.450)", "sig": "***", "n": 2685},
               {"id": "T2", "table": "Table 3", "model": "(3)", "term": "PRESIDENT’S HAWKISHNESS", "value": 0.218,
                "value_text": "0.218", "se": 0.522, "se_text": "(0.522)", "sig": "", "n": 2650},
               {"id": "T3", "table": "Table 3", "model": "(4)", "term": "PRESIDENT’S HAWKISHNESS", "value": -0.02,
                "value_text": "−0.020", "se": 0.178, "se_text": "(0.178)", "sig": "", "n": 2650}]
    res = rrun.results_from_blocks([("code/meetingAnalysis.R", blocks, calls)], targets)
    out = compare.compare(targets, res)
    assert [i["verdict"] for i in out["items"]] == ["exact", "exact", "exact"], out["items"]


# ---------------------------------------------------------------- the command

def test_the_command_starts_the_interpreter_directly_under_the_profile(tmp_path, repo):
    from research_lab.config import get_paths
    ws = get_paths().data / "replication" / "RP-2026-0098" / "workspace"
    (ws / "rrun").mkdir(parents=True)
    home = tmp_path / "R"
    (home / "bin" / "exec").mkdir(parents=True)
    info = rrun.RInfo("/x/Rscript", "4.6.1", str(home), str(home / "bin" / "exec" / "R"))
    cmd, env, cwd = rrun.sandbox_command(ws, "code/a.R", "", info)
    assert cmd[0] == W.SANDBOX_EXEC and cmd[1:3] == ["-f", str(W.app_file("sandbox_r.sb"))]
    assert cmd[-3:] == [str(home / "bin" / "exec" / "R"), "--vanilla", "--no-echo"][:0] + ["--vanilla", "--no-echo", "--file=code/a.R"] \
        or cmd[-4:] == [info.binary, "--vanilla", "--no-echo", "--file=code/a.R"]
    assert cwd == W._real(ws) / "rrun" and env["R_LIBS_USER"] == str(W._real(ws) / "rlib") and env["R_LIBS_SITE"] == "/nonexistent"
    assert "/opt/homebrew/bin" not in env["PATH"] and "HOME" in env and env["HOME"].startswith(str(W._real(ws) / "tmp"))
    cmd2, _, cwd2 = rrun.sandbox_command(ws, "code/a.R", "code", info)
    assert cmd2[-1] == "--file=a.R" and cwd2 == W._real(ws) / "rrun" / "code"
    with pytest.raises(rrun.RRunError):
        rrun.sandbox_command(ws, "a.R", "", rrun.RInfo("/x", "4", str(Path.home()), "/bin/sh"))


# ---------------------------------------------------------------- the run, with a stand-in for R

TABLE_PRINT = " ".join(shlex.quote(ln) for ln in LATEX.splitlines())
FAKE_R = f"""#!/bin/sh
for a in "$@"; do case "$a" in --file=*) f="${{a#--file=}}";; esac; done
echo "R stand-in running $f in $PWD"
case "$f" in
  *crash*) printf 'Error in crash() : always fails\\nExecution halted\\n'; exit 1;;
esac
if [ -f code/loadPackages.R ]; then
  while IFS= read -r line; do
    case "$line" in *lme4*) printf 'Error in library(x, character.only = TRUE) : there is no package called \xe2\x80\x98lme4\xe2\x80\x99\nCalls: lapply -> FUN -> library\nExecution halted\n'; exit 1;; esac
  done < code/loadPackages.R
fi
if [ ! -f code/loadPackages.R ]; then
  printf 'Error in file(filename, "r", encoding = encoding) : cannot open the connection\\nCalls: source -> file\\n'
  exit 1
fi
[ -f data/meetingData.csv ] || {{ echo "no data"; exit 1; }}
case "$f" in
  *meeting*) printf '%s\\n' {TABLE_PRINT}; echo pdf > figures/coefPlotMain.pdf; echo rows > summary.txt;;
esac
exit 0
"""


@pytest.fixture()
def fake_r(tmp_path, monkeypatch):
    home = tmp_path / "fakeR"
    (home / "bin" / "exec").mkdir(parents=True)
    exe = home / "bin" / "exec" / "R"
    exe.write_text(FAKE_R)
    exe.chmod(0o755)
    rscript = tmp_path / "Rscript"
    rscript.write_text(f"""#!/bin/sh
case "$1" in --version) echo "Rscript (R) version 4.6.1 (2026-06-24)" >&2;; *) printf '%s' '{home}';; esac
""")
    rscript.chmod(0o755)
    monkeypatch.setenv("RL_RSCRIPT", str(rscript))
    return home


@pytest.fixture()
def jost(conn, tmp_path):
    """A plan with the data under its authors' name and an R script that sources a helper nobody saved."""
    src = tmp_path / "meetingData.csv"
    src.write_text("y,x\n1,2\n3,4\n5,6\n")
    ds = datasets.import_file(conn, src, name="meetingData", original_name="meetingData.csv")
    plan_id = plans.create_blank(conn)
    plans.attach_dataset(conn, plan_id, ds)
    plans.save_targets(conn, plan_id, [
        {"table": "Table 3", "model": "(1)", "term": "MEAN HAWKISHNESS", "value": "5.101", "se": "(1.450)", "sig": "***", "n": "2,685", "page": "17"},
        {"table": "Table 3", "model": "(4)", "term": "PRESIDENT’S HAWKISHNESS", "value": "−0.020", "se": "(0.178)", "n": "2,650", "page": "17"}])
    ws = W.workspace_dir(plan_id)
    (ws / "author" / "code").mkdir(parents=True)
    (ws / "author" / "code" / "meetingAnalysis.R").write_text(SCRIPT_WITH_TABLE)
    (ws / "author" / "README.md").write_text("readme\n")
    return {"id": plan_id, "ds": ds, "ws": W._real(ws)}


def no_ai(monkeypatch):
    calls = []
    monkeypatch.setattr(rrun, "repair_session", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("AI called")))
    monkeypatch.setattr(rrun, "can_repair", lambda conn: True)
    return calls


def fix_by_writing_the_helper(calls, text="Calls the helper"):
    def session(conn, ws, prompt, step=None):
        calls.append(prompt)
        helper = Path(ws) / "rrun" / "code" / "loadPackages.R"
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text("# helper the authors' package did not save\nlibrary(data.table)\n")
        return {"ok": True, "text": "code/loadPackages.R：补写了没有保存的辅助脚本，因为脚本 source() 了它", "tokens": 4321, "turns": 3, "seconds": 1}
    return session


def test_detect_reads_the_version_and_home_of_the_interpreter(fake_r):
    info = rrun.detect()
    assert info and info.version == "4.6.1" and info.home == str(fake_r) and info.binary.endswith("bin/exec/R")


def test_without_r_nothing_runs_and_the_python_route_stays(monkeypatch, conn, jost):
    monkeypatch.setenv("RL_RSCRIPT", "none")
    assert rrun.detect() is None
    for fn in (rrun.install, rrun.run):
        with pytest.raises(rrun.RRunError, match="没有找到 R"):
            fn(conn, jost["id"])
    o = rrun.overview(conn, jost["id"])
    assert o["r"]["found"] is False and o["scripts"][0]["path"] == "code/meetingAnalysis.R"
    assert o["scripts"][0]["missing"] == ["code/loadPackages.R"]


def test_the_scripts_to_run_and_what_each_needs_that_was_not_saved(conn, jost):
    ws = jost["ws"]
    assert rrun.r_main_scripts(conn, jost["id"], ws) == ["code/meetingAnalysis.R"]
    assert rrun.r_main_scripts(conn, jost["id"], ws, ["meetingAnalysis.R"]) == ["code/meetingAnalysis.R"]
    with pytest.raises(rrun.RRunError):
        rrun.r_main_scripts(conn, jost["id"], ws, ["nothing.R"])
    texts = rrun._r_texts(ws)
    assert rrun.cwd_of("code/meetingAnalysis.R", texts["code/meetingAnalysis.R"]) == ""
    assert rrun.cwd_of("code/a.R", 'x <- fread("./data/x.csv")') == "" and rrun.cwd_of("model/a.R", 'x <- fread("./data/x.csv")') == "model"
    assert rrun.cwd_of("a.R", 'source("./code/b.R")') == ""
    assert rrun.missing_sourced(rrun._author_files(ws), "code/meetingAnalysis.R", texts) == ["code/loadPackages.R"]
    (ws / "author" / "code" / "loadPackages.R").write_text("library(x)\n")
    assert rrun.missing_sourced(rrun._author_files(ws), "code/meetingAnalysis.R", rrun._r_texts(ws)) == []


@needs_sandbox
def test_a_script_that_succeeds_costs_no_token_and_leaves_author_untouched(conn, jost, fake_r, monkeypatch):
    (jost["ws"] / "author" / "code" / "loadPackages.R").write_text("library(data.table)\n")
    calls = no_ai(monkeypatch)
    before = {p: p.read_bytes() for p in (jost["ws"] / "author").rglob("*") if p.is_file()}
    out = rrun.run(conn, jost["id"])
    assert out["state"] == "done" and out["rounds"] == 0 and out["tokens"] == 0 and calls == []
    assert {p: p.read_bytes() for p in (jost["ws"] / "author").rglob("*") if p.is_file()} == before
    assert out["scripts"][0]["exit"] == 0 and out["tables"] == 8 or out["tables"] == 4
    ws = jost["ws"]
    assert (ws / "rrun" / "data" / "meetingData.csv").read_text().startswith("y,x")                # under the authors' name
    assert not os.access(ws / "rrun" / "data" / "meetingData.csv", os.W_OK)
    assert (ws / "outputs" / "figures" / "coefPlotMain.pdf").read_text() == "pdf\n" and (ws / "outputs" / "summary.txt").exists()
    raw = ws / "outputs" / "tables" / "meetingAnalysis-1-Table_3.tex"
    assert raw.is_file() and "\\begin{tabular}" in raw.read_text()
    changes = (ws / "outputs" / "changes.md").read_text()
    assert "（没有改动）" in changes and "没有花 AI 的 token" in changes
    results = json.loads((ws / "outputs" / "results.json").read_text())
    assert W.validate_results(results) == [] and {t["label"] for t in results["tables"]} == {"Table 3"}
    run = W.get_run(conn, out["run_id"])
    assert run["method"] == "workspace" and run["status"] == "done" and run["tokens"] == 0 and run["workspace"]["mode"] == "rrun"
    assert run["result"]["valid"] is True and "scripts/code/meetingAnalysis.R" in run["workspace"]["code"]
    assert W.run_file(jost["id"], out["run_id"], "outputs/changes.md").is_file()
    assert W.run_file(jost["id"], out["run_id"], "logs/rrun-code_meetingAnalysis.R.log").is_file()
    cmp = compare.compare(plans.target_records(conn, jost["id"]), run["result"]["results"])
    assert [i["verdict"] for i in cmp["items"]] == ["exact", "exact"], cmp["items"]
    assert rrun.read_state(ws)["state"] == "done" and not rrun.busy(jost["id"])


@needs_sandbox
def test_a_failing_script_gets_one_ai_repair_of_the_copy_and_every_edit_is_a_diff(conn, jost, fake_r, monkeypatch):
    calls = []
    monkeypatch.setattr(rrun, "repair_session", fix_by_writing_the_helper(calls))
    monkeypatch.setattr(rrun, "can_repair", lambda conn: True)
    out = rrun.run(conn, jost["id"])
    ws = jost["ws"]
    assert out["state"] == "done" and out["rounds"] == 1 and out["tokens"] == 4321 and len(calls) == 1
    assert "cannot open the connection" in calls[0] and "code/loadPackages.R" in calls[0]       # the error and the missing helper
    assert not (ws / "author" / "code" / "loadPackages.R").exists()                              # author/ untouched
    assert (ws / "rrun" / "code" / "loadPackages.R").exists()
    changes = (ws / "outputs" / "changes.md").read_text()
    assert "### code/loadPackages.R（新增" in changes and "library(data.table)" in changes and "补写了没有保存的辅助脚本" in changes
    assert "4321 token" in changes
    run = W.get_run(conn, out["run_id"])
    assert run["tokens"] == 4321 and run["workspace"]["rounds"][0]["files"] == ["code/loadPackages.R"]
    led = plans.ledger(conn, jost["id"])                                                         # the 0185 ledger counts it
    assert any(s["key"] == "workspace" and s["tokens"] == 4321 for s in led["steps"])
    # a second run lays the kept repair over the fresh copies and asks nobody
    calls2 = no_ai(monkeypatch)
    again = rrun.run(conn, jost["id"])
    assert again["state"] == "done" and again["rounds"] == 0 and again["tokens"] == 0 and calls2 == []
    assert "补写" not in "".join(r["text"] for r in again["repairs"]) and "### code/loadPackages.R" in (ws / "outputs" / "changes.md").read_text()
    # --fresh forgets it: the AI is needed again
    third_calls = []
    monkeypatch.setattr(rrun, "repair_session", fix_by_writing_the_helper(third_calls))
    assert rrun.run(conn, jost["id"], fresh=True)["rounds"] == 1 and len(third_calls) == 1


@needs_sandbox
def test_packages_that_did_not_install_are_taken_out_by_rule_before_any_ai(conn, jost, fake_r, monkeypatch, client):
    ws = jost["ws"]
    (ws / "author" / "code" / "loadPackages.R").write_text(
        'packages <- c("zoo", "lme4", "data.table")\nlapply(packages, library, character.only = TRUE)\n')
    for name in ("zoo", "data.table", "stargazer"):                            # installed; lme4 is not
        (ws / "rlib" / name).mkdir(parents=True)
        (ws / "rlib" / name / "DESCRIPTION").write_text("Package: x\nVersion: 1.0\n")
    calls = no_ai(monkeypatch)
    out = rrun.run(conn, jost["id"])
    assert out["state"] == "done" and out["tokens"] == 0 and out["rounds"] == 0 and out["rule_repairs"] == 1 and calls == []
    assert [r["kind"] for r in out["repairs"]] == ["rule"] and out["repairs"][0]["files"] == ["code/loadPackages.R"]
    fixed = (ws / "rrun" / "code" / "loadPackages.R").read_text()
    assert fixed == 'packages <- c("zoo", "data.table")\nlapply(packages, library, character.only = TRUE)\n'
    assert (ws / "author" / "code" / "loadPackages.R").read_text().count("lme4") == 1          # the authors' file is untouched
    changes = (ws / "outputs" / "changes.md").read_text()
    assert "按规则修补（没有请 AI，0 token）" in changes and '-packages <- c("zoo", "lme4", "data.table")' in changes
    assert '+packages <- c("zoo", "data.table")' in changes and "packages 里去掉 lme4" in changes
    run = W.get_run(conn, out["run_id"])
    assert run["tokens"] == 0 and [r["kind"] for r in run["workspace"]["rounds"]] == ["rule"]
    assert "按规则修补 1 次" in re.sub(r"<[^>]+>", " ", client.get(f"/replicate/{jost['id']}").text)
    # the repair was kept: the next run starts from it and has nothing to repair
    again = rrun.run(conn, jost["id"])
    assert again["state"] == "done" and again["rule_repairs"] == 0 and again["tokens"] == 0
    # …and the package list the install step works from is still the authors': lme4 stays on it to be tried again
    assert "lme4" in rrun.package_list(ws).to_install and "lme4" not in rtext.parse_packages(rrun._texts_in(ws / "rrun", ws)).packages


@needs_sandbox
def test_when_the_rule_finds_nothing_or_is_not_enough_the_ai_is_still_called(conn, jost, fake_r, monkeypatch):
    ws = jost["ws"]
    (ws / "author" / "code" / "loadPackages.R").write_text("library(data.table)\n")
    (ws / "author" / "code" / "zcrash.R").write_text('library(lme4)\nx <- fread("./data/meetingData.csv")\nfit <- lm(y ~ x, x)\n')
    (ws / "author" / "code" / "ycrash.R").write_text('x <- fread("./data/meetingData.csv")\nfit <- lm(y ~ x, x)\n')
    calls = []

    def session(conn, ws_, prompt, step=None):
        calls.append(prompt)
        return {"ok": True, "text": "什么也改不了", "tokens": 5, "turns": 1, "seconds": 1}
    monkeypatch.setattr(rrun, "repair_session", session)
    monkeypatch.setattr(rrun, "can_repair", lambda conn: True)
    out = rrun.run(conn, jost["id"], scripts=["ycrash.R", "zcrash.R"])
    # ycrash loads nothing that is missing: straight to the AI. zcrash: the rule takes library(lme4) out, the script still fails
    # (the stand-in crashes by name), the rule is not tried twice, and the AI is called
    kinds = [(r["script"], r["kind"]) for r in out["repairs"]]
    assert kinds == [("code/ycrash.R", "ai"), ("code/zcrash.R", "rule"), ("code/zcrash.R", "ai")]
    assert len(calls) == 2 and out["rounds"] == 2 and out["rule_repairs"] == 1 and out["tokens"] == 10
    assert "invisible(NULL)" in (ws / "rrun" / "code" / "zcrash.R").read_text()


@needs_sandbox
def test_a_repair_only_counts_for_the_author_file_it_was_made_against(conn, jost, fake_r, monkeypatch):
    calls = []
    monkeypatch.setattr(rrun, "repair_session", fix_by_writing_the_helper(calls))
    monkeypatch.setattr(rrun, "can_repair", lambda conn: True)
    rrun.run(conn, jost["id"])
    (jost["ws"] / "author" / "code" / "loadPackages.R").write_text("library(somethingElse)\n")        # the real helper arrives
    again = rrun.run(conn, jost["id"])
    assert again["rounds"] == 0
    assert "somethingElse" in (jost["ws"] / "rrun" / "code" / "loadPackages.R").read_text()


@needs_sandbox
def test_the_round_cap_a_no_edit_answer_and_an_unfixable_error_stop_the_ai(conn, jost, fake_r, monkeypatch):
    sessions = []

    def idle(conn, ws, prompt, step=None):
        sessions.append(prompt)
        return {"ok": True, "text": "什么也改不了：lme4 没装", "tokens": 100, "turns": 1, "seconds": 1}
    monkeypatch.setattr(rrun, "repair_session", idle)
    monkeypatch.setattr(rrun, "can_repair", lambda conn: True)
    out = rrun.run(conn, jost["id"])
    assert len(sessions) == 1 and out["state"] == "error" and out["tokens"] == 100
    assert any("没有改任何文件" in s for s in out["skipped"])
    assert "没有改任何文件" in (jost["ws"] / "outputs" / "changes.md").read_text()
    # an error only an install can fix is not worth a session
    sessions.clear()
    (jost["ws"] / "author" / "code" / "crash.R").write_text('x <- fread("./data/meetingData.csv")\nfit <- lm(y ~ x, x)\n')
    out2 = rrun.run(conn, jost["id"], scripts=["crash.R"], repair=False)
    assert sessions == [] and out2["state"] == "error" and "code/crash.R" in out2["failed"]
    assert rrun._unfixable("Error in loadNamespace(x) : there is no package called ‘lme4’", {"lme4"}).startswith("需要的包 lme4")
    assert rrun._unfixable('Error: could not find function "glmer"', {"lme4"}).startswith("glmer()")
    assert rrun._unfixable("Error in library(x) : there is no package called ‘keyATM’\nCalls: lapply -> FUN -> library", {"keyATM"}) == ""
    assert rrun._unfixable("x\n[超过 30 分钟没有结束，已中止]", set()) == "超时被中止"


@needs_sandbox
def test_an_ai_round_never_removes_or_rewrites_what_an_earlier_script_wrote(conn, jost, fake_r, monkeypatch):
    ws = jost["ws"]
    (ws / "author" / "code" / "loadPackages.R").write_text("library(data.table)\n")
    (ws / "author" / "code" / "zcrash.R").write_text('x <- fread("./data/meetingData.csv")\nfit <- lm(y ~ x, x)\n')
    seen = []

    def meddling(conn, ws_, prompt, step=None):
        rr = Path(ws_) / "rrun"
        seen.append(sorted(p.name for p in (rr / "figures").iterdir()))
        (rr / "figures" / "coefPlotMain.pdf").write_text("forged\n")           # a generated file the AI must not edit
        (rr / "summary.txt").unlink()                                              # nor delete
        (rr / "data" / "meetingData.csv").chmod(0o644)
        (rr / "data" / "meetingData.csv").write_text("y,x\n9,9\n")             # nor the data copy
        (rr / "code" / "zcrash.R").write_text('x <- fread("./data/meetingData.csv")\nprint(1)\n')
        return {"ok": True, "text": "code/zcrash.R：改了一行", "tokens": 7, "turns": 1, "seconds": 1}
    monkeypatch.setattr(rrun, "repair_session", meddling)
    monkeypatch.setattr(rrun, "can_repair", lambda conn: True)
    out = rrun.run(conn, jost["id"], rounds=1)
    assert seen == [["coefPlotMain.pdf"]] and out["rounds"] == 1 and out["repairs"][0]["files"] == ["code/zcrash.R"]
    assert (ws / "outputs" / "figures" / "coefPlotMain.pdf").read_text() == "pdf\n"
    assert (ws / "outputs" / "summary.txt").read_text() == "rows\n"
    assert (ws / "rrun" / "data" / "meetingData.csv").read_text().startswith("y,x\n1,2")
    assert not os.access(ws / "rrun" / "data" / "meetingData.csv", os.W_OK)


def test_the_repair_session_can_edit_only_the_copies_and_has_no_shell_or_network(tmp_path):
    cmd = rrun.repair_command("/usr/bin/true", "sonnet", tmp_path)
    denied = cmd[cmd.index("--disallowedTools") + 1:cmd.index("--allowedTools")]
    allowed = cmd[cmd.index("--allowedTools") + 1:]
    real = os.path.realpath(tmp_path)
    for needle in ("Bash", "WebFetch", "WebSearch", f"Write(/{real}/author/**)", f"Edit(/{real}/rrun/data/**)",
                   f"Write(/{real}/outputs/**)", f"Write(/{real}/rlib/**)"):
        assert needle in denied, needle
    assert f"Write(/{real}/rrun/**)" in allowed and f"Edit(/{real}/rrun/**)" in allowed and "Bash" not in allowed
    assert "--restricted" in cmd and cmd[cmd.index("--tools") + 1] == "Read,Write,Edit,Glob,Grep"
    assert json.loads(cmd[cmd.index("--mcp-config") + 1]) == {"mcpServers": {}} and "--strict-mcp-config" in cmd


@needs_sandbox
def test_an_edit_outside_the_scripts_is_put_back_after_a_repair_round(conn, jost, fake_r, monkeypatch):
    def naughty(conn, ws, prompt, step=None):
        rr = Path(ws) / "rrun"
        (rr / "code" / "loadPackages.R").parent.mkdir(parents=True, exist_ok=True)
        (rr / "code" / "loadPackages.R").write_text("library(data.table)\n")
        (rr / "README.md").write_text("tampered\n")
        (rr / "stray.txt").write_text("x")
        return {"ok": True, "text": "x", "tokens": 1, "turns": 1, "seconds": 1}
    monkeypatch.setattr(rrun, "repair_session", naughty)
    monkeypatch.setattr(rrun, "can_repair", lambda conn: True)
    rrun.run(conn, jost["id"])
    assert (jost["ws"] / "rrun" / "README.md").read_text() == "readme\n" and not (jost["ws"] / "rrun" / "stray.txt").exists()


# ---------------------------------------------------------------- preparing rrun/

def test_prepare_copies_author_mirrors_data_by_name_and_makes_the_write_folders(conn, jost):
    ws = jost["ws"]
    (ws / "author" / "code" / "loadPackages.R").write_text("library(x)\n")
    prep = rrun.prepare_run(conn, jost["id"], ws, ["code/meetingAnalysis.R"])
    rr = ws / "rrun"
    assert (rr / "code" / "meetingAnalysis.R").read_text() == SCRIPT_WITH_TABLE and (rr / "README.md").exists()
    assert (rr / "data" / "meetingData.csv").exists() and (rr / "figures").is_dir()
    assert prep["cwds"] == {"code/meetingAnalysis.R": ""} and prep["mirrored"] == ["data/meetingData.csv"]
    assert prep["manifest"]["data/meetingData.csv"] and "code/loadPackages.R" in prep["manifest"]
    (rr / "stale.txt").write_text("old")
    rrun.prepare_run(conn, jost["id"], ws, ["code/meetingAnalysis.R"])
    assert not (rr / "stale.txt").exists()                                       # rrun/ is rebuilt every run


def test_a_script_beside_its_data_runs_from_its_own_folder_and_a_path_reaches_the_dataset(conn, jost):
    ws = jost["ws"]
    (ws / "author" / "analysis").mkdir()
    (ws / "author" / "analysis" / "main.R").write_text('d <- read.csv("../raw/meetingData.csv")\nm <- lm(y ~ x, d)\n')
    prep = rrun.prepare_run(conn, jost["id"], ws, ["analysis/main.R"])
    assert prep["cwds"]["analysis/main.R"] == "analysis"
    assert (ws / "rrun" / "analysis" / "data" / "meetingData.csv").exists() and (ws / "rrun" / "raw" / "meetingData.csv").exists()


def test_an_absolute_or_escaping_path_never_leaves_rrun(conn, jost):
    ws = jost["ws"]
    (ws / "author" / "code" / "evil.R").write_text('d <- fread("/etc/passwd")\nx <- fread("../../../../meetingData.csv")\n'
                                                   'ggsave("../../../../outside/p.pdf")\nsource("../../x.R")\n')
    rrun.prepare_run(conn, jost["id"], ws, ["code/evil.R"])
    assert not (ws.parent.parent.parent / "outside").exists() and not (ws.parent / "meetingData.csv").exists()
    assert rrun.missing_sourced(rrun._author_files(ws), "code/evil.R", rrun._r_texts(ws)) == ["x.R"]


def test_laid_repairs_need_the_same_author_text_and_symlinks_are_never_followed(conn, jost):
    ws = jost["ws"]
    prep = rrun.prepare_run(conn, jost["id"], ws, ["code/meetingAnalysis.R"])
    rr = ws / "rrun"
    (rr / "code" / "meetingAnalysis.R").write_text(SCRIPT_WITH_TABLE.replace("fit1", "fitOne"))
    rrun.save_fixes(ws, rr, ["code/meetingAnalysis.R"])
    again = rrun.prepare_run(conn, jost["id"], ws, ["code/meetingAnalysis.R"])
    assert again["laid"] == ["code/meetingAnalysis.R"] and "fitOne" in (rr / "code" / "meetingAnalysis.R").read_text()
    (ws / "author" / "code" / "meetingAnalysis.R").write_text(SCRIPT_WITH_TABLE + "# new\n")
    assert rrun.prepare_run(conn, jost["id"], ws, ["code/meetingAnalysis.R"])["laid"] == []
    assert prep["manifest"]


# ---------------------------------------------------------------- one R step at a time

def test_one_r_step_at_a_time_and_a_dead_process_never_locks_the_plan(conn, jost, fake_r):
    ws = rrun._prepare_ws(jost["id"])
    assert rrun.busy(jost["id"]) is False
    with rrun.Step(ws, "install") as first:
        assert rrun.busy(jost["id"]) is True and rrun.read_state(ws)["state"] == "running"
        with pytest.raises(rrun.RRunError, match="正在运行 R"):
            rrun.Step(ws, "run").__enter__()
        with pytest.raises(W.WorkspaceError, match="正在运行 R"):
            W.check_start(conn, jost["id"], "ai")
        first.finish("done", "ok")
    assert rrun.busy(jost["id"]) is False and rrun.read_state(ws)["state"] == "done"
    with pytest.raises(rrun.RRunError):                                  # a Python AI job blocks the R step too
        conn.execute("INSERT INTO replication_jobs(plan_id, mode, state, host_pid, started_at) VALUES (?,?,?,?,?)",
                     (jost["id"], "ai", "running", os.getpid(), "2026-10-18 10:00:00"))
        rrun.spawn(conn, jost["id"], "run")


@needs_sandbox
def test_the_page_click_runs_the_step_on_a_thread_and_the_lock_is_taken_before_it_starts(conn, jost, fake_r, monkeypatch, client):
    import time
    (jost["ws"] / "author" / "code" / "loadPackages.R").write_text("library(data.table)\n")
    calls = no_ai(monkeypatch)
    r = client.post(f"/replicate/{jost['id']}/r/run", follow_redirects=False)
    assert "msg=" in r.headers.get("location", r.headers.get("hx-redirect", ""))
    for _ in range(100):
        time.sleep(0.2)
        if not rrun.busy(jost["id"]):
            break
    assert not rrun.busy(jost["id"]) and calls == []
    state = rrun.read_state(jost["ws"])
    assert state["kind"] == "run" and state["state"] == "done" and state["run_id"]
    assert W.get_run(conn, state["run_id"])["workspace"]["mode"] == "rrun"
    page = client.get(f"/replicate/{jost['id']}").text
    assert "R 运行作者代码" in page and "上一次运行：完成" in page


def test_stop_kills_the_running_group_and_marks_the_step_stopped(conn, jost, fake_r):
    ws = rrun._prepare_ws(jost["id"])
    (ws / "logs").mkdir(exist_ok=True)
    with rrun.Step(ws, "run") as step:
        proc = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
        step.started(proc.pid)
        assert rrun.stop(jost["id"]) is True
        assert proc.wait(timeout=10) < 0 and rrun._stopped(ws)
        step.finish("stopped", "已停止")
    assert rrun.stop(jost["id"]) is False
    with rrun.Step(ws, "run"):
        assert rrun._stopped(ws) is False                                 # a new step starts clean


def test_a_rerun_of_an_r_run_is_refused_and_pointed_at_the_r_button(conn, jost):
    from research_lab.db.connection import next_sequence_id
    run_id = next_sequence_id(conn, "analysis_runs", "RUN")
    conn.execute("INSERT INTO analysis_runs(id, plan_id, method, params_json, result_json, status, workspace_json, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?)", (run_id, jost["id"], "workspace", "{}", "{}", "done",
                                              json.dumps({"mode": "rrun", "code": ["scripts/code/a.R"]}), "2026-10-18T10:00:00"))
    with pytest.raises(W.WorkspaceError, match="Run author code"):
        W.check_start(conn, jost["id"], "rerun", run_id)


# ---------------------------------------------------------------- the page

def test_the_page_offers_install_and_run_and_shows_what_is_missing(conn, jost, client, fake_r):
    page = client.get(f"/replicate/{jost['id']}").text
    assert "data-replicate-r-found" in page and "R 4.6.1" in page
    assert "Install R packages" in page and "Run author code (R)" in page
    assert "data-replicate-r-missing-helper" in page and "code/loadPackages.R" in page
    assert "data-replicate-r-install" in page and "联网，只从 CRAN" in page
    assert "stargazer" in page and "补丁" in page


def test_the_page_without_r_says_so_and_keeps_the_python_route(conn, jost, client, monkeypatch):
    monkeypatch.setenv("RL_RSCRIPT", "none")
    page = client.get(f"/replicate/{jost['id']}").text
    assert "data-replicate-r-missing" in page and "data-replicate-start" in page
    for action in ("install", "run"):
        r = client.post(f"/replicate/{jost['id']}/r/{action}", follow_redirects=False)
        assert "err=" in r.headers.get("location", r.headers.get("hx-redirect", ""))
    idle = client.get(f"/replicate/{jost['id']}/r/status")                       # nothing runs: the page is told to reload
    assert idle.status_code == 204 and idle.headers["HX-Refresh"] == "true"
    assert client.post(f"/replicate/{jost['id']}/r/stop", follow_redirects=False).status_code in (204, 303)


@needs_sandbox
def test_an_r_run_shows_in_the_run_list_with_its_changes_and_outputs(conn, jost, fake_r, client, monkeypatch):
    monkeypatch.setattr(rrun, "repair_session", fix_by_writing_the_helper([]))
    monkeypatch.setattr(rrun, "can_repair", lambda conn: True)
    out = rrun.run(conn, jost["id"])
    page = client.get(f"/replicate/{jost['id']}").text
    assert "R 运行作者代码" in page and "data-replicate-rrun-info" in page and "data-replicate-changes" in page
    assert "/rerun" not in page.split("data-replicate-rrun-info")[0].split(out["run_id"])[-1] or "R 运行作者代码" in page
    md = client.get(f"/replicate/{jost['id']}/runs/{out['run_id']}/files/outputs/changes.md")
    assert md.status_code == 200 and "补写了没有保存的辅助脚本" in md.text
    assert client.get(f"/replicate/{jost['id']}/runs/{out['run_id']}/files/scripts/code/meetingAnalysis.R").status_code == 200


# ---------------------------------------------------------------- real R (skipped when Rscript is absent)

@needs_r
def test_real_r_is_found_and_runs_a_script_inside_the_sandbox(conn, jost):
    info = rrun.detect()
    assert info and info.binary and re.fullmatch(r"\d+\.\d+(\.\d+)?", info.version)
    ws = jost["ws"]
    (ws / "author" / "code" / "probe.R").write_text('''
cat("hello", R.version.string, "\\n")
print(.libPaths())
dir.create("figures", showWarnings = FALSE)
writeLines("table", "figures/t.txt")
r <- function(expr) tryCatch({ expr; "allowed" }, error = function(e) "denied", warning = function(w) "denied")
cat("author:", r(writeLines("x", "../../author/code/evil.R")), "\\n")
cat("net:", r(socketConnection("127.0.0.1", 9, open = "r", timeout = 2)), "\\n")
cat("home:", r(readLines(path.expand("~/../../.zsh_history"), n = 1)), "\\n")
cat("child-write:", r(if (system("echo x > ../../author/code/evil2.R 2>/dev/null") != 0) stop("denied")), "\\n")
cat("data-write:", r(writeLines("x", "data/meetingData.csv")), "\\n")
''')
    (ws / "rlib").mkdir()
    prep = rrun.prepare_run(conn, jost["id"], ws, ["code/probe.R"])
    out = rrun.run_script(ws, "code/probe.R", "code", info)
    assert out["exit"] == 0, out["output"]
    text = out["output"]
    assert f"hello R version {info.version}" in text and str(ws / "rlib") in text
    for line in ("author: denied", "net: denied", "child-write: denied", "data-write: denied"):
        assert line in text, text
    assert not (ws / "author" / "code" / "evil.R").exists() and not (ws / "author" / "code" / "evil2.R").exists()
    assert (ws / "rrun" / "code" / "figures" / "t.txt").read_text() == "table\n"
    assert prep["manifest"]


@needs_r
def test_real_r_reads_the_shared_library_after_the_plans_own_and_cannot_change_it(conn, jost):
    info = rrun.detect()
    shared = rrun.shared_dir(info) / "rlib"
    (shared / "zoo").mkdir(parents=True)
    (shared / "zoo" / "DESCRIPTION").write_text("Package: zoo\nVersion: 1.8\n")
    ws = jost["ws"]
    (ws / "rlib").mkdir()
    (ws / "author" / "code" / "probe.R").write_text('''
r <- function(expr) tryCatch({ expr; "allowed" }, error = function(e) "denied", warning = function(w) "denied")
s <- .libPaths()[2]
cat("paths:", .libPaths(), "\\n")
cat("read:", r(readLines(file.path(s, "zoo", "DESCRIPTION"))), "\\n")
cat("write:", r(writeLines("x", file.path(s, "zoo", "DESCRIPTION"))), "\\n")
cat("add:", r(if (!dir.create(file.path(s, "evil"), showWarnings = FALSE)) stop("no")), "\\n")
''')
    rrun.prepare_run(conn, jost["id"], ws, ["code/probe.R"])
    text = rrun.run_script(ws, "code/probe.R", "code", info)["output"]
    paths = re.search(r"paths: (.*)", text).group(1).split()
    assert paths[:2] == [str(ws / "rlib"), str(W._real(shared))], text
    assert not any("site-library" in p for p in paths)
    for line in ("read: allowed", "write: denied", "add: denied"):
        assert line in text, text
    assert (shared / "zoo" / "DESCRIPTION").read_text() == "Package: zoo\nVersion: 1.8\n" and not (shared / "evil").exists()


@needs_r
def test_real_r_timeout_kills_the_process_group(conn, jost):
    info = rrun.detect()
    ws = jost["ws"]
    (ws / "author" / "code" / "loop.R").write_text("while (TRUE) { x <- 1 }\n")
    rrun.prepare_run(conn, jost["id"], ws, ["code/loop.R"])
    out = rrun.run_script(ws, "code/loop.R", "code", info, timeout=3)
    assert out["exit"] == -9 and "已中止" in out["output"] and out["seconds"] < 30
    assert subprocess.run(["/usr/bin/pgrep", "-f", str(ws / "rrun")], capture_output=True).returncode != 0


