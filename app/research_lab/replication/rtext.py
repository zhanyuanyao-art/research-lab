"""Reading R text without running it (card 0186): which packages an R script set loads or installs, which files it
sources, reads and writes, and the folder it expects to run from.

The text is the authors' (a replication package: untrusted, and it can be huge), and this runs on every load of the
replication page, so everything here is bounded (AGENTS.md, "Scanning outside text"):
  - one masking pass per text (comments blanked, strings blanked), done by a regex over the whole text, never a
    character loop, and remembered per text;
  - every call window is cut at WINDOW characters, found by jumping between the few characters that matter;
  - every file spends from a step budget and a time budget; when either is gone the rest of the file is not scanned and
    the result says so;
  - per file, a regex contributes at most MATCHES hits; names, notes and paths are kept in dicts and capped;
  - file indexes are dicts built once; nothing compares two growing lists pairwise.

Nothing in this module executes, imports or evaluates the text it reads.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import islice
from typing import Iterable, Iterator

from . import package
from .package import STR

CRAN = "https://cloud.r-project.org"
WINDOW = 600                           # the most of a call's arguments that is read
MATCHES = 400                          # hits one pattern contributes per file
CALLS = 20_000                         # function-call names looked at per file (the hint table, the write calls)
STEPS = 250_000                        # bracket / quote steps one file may spend
SCAN_CHARS = 512 * 1024                # the most of one file that is scanned
SCAN_SECONDS = 0.8                     # the most time all files together may take (parse_packages)
MAX_NAMES = 2_000                      # packages / paths kept
MAX_NOTES = 40
MAX_EDITS = 50

# Shipped with R itself (base and recommended): never installed into the plan's library.
BASE_PACKAGES = frozenset({
    "base", "compiler", "datasets", "graphics", "grDevices", "grid", "methods", "parallel", "splines", "stats", "stats4",
    "tcltk", "tools", "utils", "boot", "class", "cluster", "codetools", "foreign", "KernSmooth", "lattice", "MASS",
    "Matrix", "mgcv", "nlme", "nnet", "rpart", "spatial", "survival"})

# What a call means when the script never says library(): the commonest functions of the packages replication code
# leans on. A hint only: it makes the install step list a package the scripts use but load through a helper that was
# not saved. Unqualified names that several packages define (select, filter, icc) are left to a `pkg::` mention or
# to the helper itself.
FUNCTION_PACKAGES = {
    "fread": "data.table", "fwrite": "data.table", "setDT": "data.table", "stargazer": "stargazer",
    "ggplot": "ggplot2", "ggsave": "ggplot2", "glmer": "lme4", "lmer": "lme4", "glmer.nb": "lme4",
    "xtable": "xtable", "coeftest": "lmtest", "lrtest": "lmtest", "vcovHC": "sandwich", "vcovCL": "sandwich",
    "felm": "lfe", "feols": "fixest", "fepois": "fixest", "lm_robust": "estimatr", "read_dta": "haven",
    "read_excel": "readxl", "read_csv": "readr", "texreg": "texreg", "modelsummary": "modelsummary", "plm": "plm",
    "glmmTMB": "glmmTMB", "keyATM": "keyATM", "dfm": "quanteda", "str_detect": "stringr", "ymd": "lubridate",
    "tidy": "broom"}

# Packages that must be tried first, because a dependent of theirs needs them and a failed dependent hides the cause.
PRE_INSTALL = {"lme4": ["nloptr"]}

# A published one-line fix every user of the package needs on R >= 4.2, replayed when the authors' own patch script
# was not saved: stargazer 5.2.3 stops with "the condition has length > 1" in .inside.bracket (a model named by a long
# formula), which is what Jost's setup.R patches. A patch is a list of line edits, each `line = another line` or
# `line = ""`, and nothing else: it can move or drop a line of the CRAN source, never write new text.
KNOWN_PATCHES = {
    "stargazer": {"version": "5.2.3", "file": "R/stargazer-internal.R", "edits": [(1990, 1995), (1995, "")],
                  "guard": [(1995, "if (length(s) > 1)")],
                  "why": "同作者 setup.R 里的补丁（把长度检查提到 is.na 之前）；没有取到 setup.R，用公开的同一处修正"},
}

NAME = re.compile(r"[A-Za-z][A-Za-z0-9.]*[A-Za-z0-9]")            # used with fullmatch
VERSION = re.compile(r"[0-9][0-9A-Za-z._-]{0,30}")                # used with fullmatch
CRAN_TARBALL = re.compile(r"https?://(?:cran|cloud)\.r-project\.org/src/contrib/(?:Archive/([A-Za-z][A-Za-z0-9.]*)/)?"
                          r"([A-Za-z][A-Za-z0-9.]*)_([0-9][0-9A-Za-z._-]*)\.tar\.gz")
CODE_DIRS = frozenset({"code", "codes", "script", "scripts", "src", "r", "rcode", "analysis", "programs", "prog", "do"})
SETUP_NAME = re.compile(r"(?:setup|install\w*|load_?packages|packages|requirements|dependencies)\.r", re.I)


def valid_name(s: object) -> bool:
    return isinstance(s, str) and len(s) <= 60 and NAME.fullmatch(s) is not None


def valid_version(s: object) -> bool:
    return isinstance(s, str) and VERSION.fullmatch(s) is not None


# ---------------------------------------------------------------- masking: one regex pass per text

_TOKEN = re.compile(r"\"(?:[^\"\\\n]|\\.)*\"?|'(?:[^'\\\n]|\\.)*'?|#[^\n]*")


def _mask_pass(text: str) -> tuple[str, str]:
    """(clean, masked): the text with comments blanked, and the same with every string's content blanked too. Both keep
    the length and the line breaks, so an offset found in `masked` reads back from `clean`. A string ends at its line's
    end. One regex pass over the text: the cost is the number of strings and comments, not of characters."""
    clean: list[str] = []
    masked: list[str] = []
    last = 0
    for m in _TOKEN.finditer(text):
        start, end = m.span()
        if start > last:
            clean.append(text[last:start])
            masked.append(text[last:start])
        tok = m.group()
        if tok[0] == "#":
            blank = " " * (end - start)
            clean.append(blank)
            masked.append(blank)
        else:
            clean.append(tok)
            closed = len(tok) >= 2 and tok[-1] == tok[0]
            masked.append(tok[0] + " " * (len(tok) - (2 if closed else 1)) + (tok[0] if closed else ""))
        last = end
    clean.append(text[last:])
    masked.append(text[last:])
    return "".join(clean), "".join(masked)


@lru_cache(maxsize=24)
def mask(text: str) -> tuple[str, str]:
    """_mask_pass of the first SCAN_CHARS characters of `text`, remembered (the page asks for the same files again every
    few seconds while a step runs)."""
    return _mask_pass(text[:SCAN_CHARS])


class _Over(Exception):
    """The step or time budget of a file is spent."""


class Budget:
    def __init__(self, steps: int = STEPS, deadline: float | None = None):
        self.steps, self.deadline, self.over = steps, deadline, False

    def spend(self, n: int = 1) -> None:
        self.steps -= n
        if self.steps < 0 or (self.deadline is not None and (self.steps & 255) == 0 and time.monotonic() > self.deadline):
            self.over = True
            raise _Over


_SPECIAL = re.compile(r"[\"'\\()\[\]{}]")
_SPECIAL_COMMA = re.compile(r"[\"'\\()\[\]{},]")


def inner(clean: str, pos: int, budget: Budget | None = None, limit: int = WINDOW) -> str:
    """The text between the "(" just before `pos` and its closing ")", at most `limit` characters (the rest of a longer
    call is not read). Jumps from one bracket or quote to the next with a regex."""
    end = min(len(clean), pos + limit)
    depth, i, quote = 0, pos, ""
    while i < end:
        m = _SPECIAL.search(clean, i, end)
        if m is None:
            return clean[pos:end]
        if budget is not None:
            budget.spend()
        i = m.start()
        c = clean[i]
        if c == "\\":
            i += 2
            continue
        if quote:
            if c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c in "([{":
            depth += 1
        else:
            if depth == 0:
                return clean[pos:i]
            depth -= 1
        i += 1
    return clean[pos:end]


def split_args(text: str, budget: Budget | None = None, limit: int = 60) -> list[str]:
    """Top-level comma split of an argument list (at most `limit` arguments)."""
    out: list[str] = []
    depth, quote, start, i = 0, "", 0, 0
    while i < len(text) and len(out) < limit:
        m = _SPECIAL_COMMA.search(text, i)
        if m is None:
            break
        if budget is not None:
            budget.spend()
        i = m.start()
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if quote:
            if c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            out.append(text[start:i].strip())
            start = i + 1
        i += 1
    tail = text[start:].strip()
    if (tail or out) and len(out) < limit:
        out.append(tail)
    return out


def literals(expr: str, limit: int = 200) -> list[str]:
    return [a or b for a, b in islice(STR.findall(expr), limit)]


def _only_strings(expr: str) -> bool:
    return not re.sub(r"[\s,]", "", STR.sub("", expr))


_NAMED = re.compile(r"([A-Za-z.][\w.]*)\s*=(?!=)\s*(.*)", re.S)


def _named(arg: str) -> tuple[str, str]:
    m = _NAMED.fullmatch(arg[:WINDOW])
    return (m.group(1), m.group(2).strip()) if m else ("", arg.strip())


def hits(regex: re.Pattern[str], text: str, limit: int = MATCHES) -> Iterator[re.Match[str]]:
    return islice(regex.finditer(text), limit)


# ---------------------------------------------------------------- the package list

@dataclass
class SourcePatch:
    pkg: str
    version: str
    file: str                                   # inside the tarball's folder: R/stargazer-internal.R
    edits: list[tuple[int, int | str]]          # line = another line's text, or line = "" ; applied in order
    guard: list[tuple[int, str]] = field(default_factory=list)   # (line, text it must contain) before editing
    why: str = ""

    def signature(self) -> str:
        return hashlib.sha1(json.dumps([self.pkg, self.version, self.file, self.edits, self.guard]).encode()).hexdigest()[:12]


@dataclass
class Pin:
    pkg: str
    version: str
    urls: list[str]
    origin: str


@dataclass
class PackageList:
    packages: dict[str, list[str]] = field(default_factory=dict)    # name → where it was asked for
    pins: dict[str, Pin] = field(default_factory=dict)
    patches: dict[str, SourcePatch] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)                  # anything unusual, in Chinese, for the log
    truncated: bool = False                                         # some text was not scanned to its end

    def add(self, name: str, where: str) -> None:
        if valid_name(name) and (name in self.packages or len(self.packages) < MAX_NAMES):
            places = self.packages.setdefault(name, [])
            if where not in places and len(places) < 4:
                places.append(where)

    def note(self, text: str) -> None:
        if len(self.notes) < MAX_NOTES and text not in self.notes:
            self.notes.append(text[:300])

    @property
    def to_install(self) -> list[str]:
        return [p for p in self.packages if p not in BASE_PACKAGES]


_VECTOR = re.compile(r"(?<![\w.$@])([A-Za-z.][\w.]*)\s*(?:<-|=)(?!=)\s*(c\s*\(|(?=[\"']))")
_FOR = re.compile(r"\bfor\s*\(\s*([A-Za-z.][\w.]*)\s+in\s+")
_LIBRARY = re.compile(r"(?<![\w.$@])(?:base::)?(library|require|requireNamespace|loadNamespace)\s*\(")
_PLOAD = re.compile(r"(?<![\w.$@])(?:pacman::)?p_load\s*\(")
_APPLY = re.compile(r"(?<![\w.$@])(?:purrr::)?(?:lapply|sapply|vapply|Map|mapply|walk)\s*\(")
_INSTALL = re.compile(r"(?<![\w.$@])install\.packages\s*\(")
_NS = re.compile(r"(?<![\w.])([A-Za-z][A-Za-z0-9.]*):::?[A-Za-z.]")
_OTHER_INSTALL = re.compile(r"(?<![\w.$@])(?:\w+::)?(install_github|install_gitlab|install_bitbucket|install_git|install_url|"
                            r"install_local)\s*\(")
_VERSION_INSTALL = re.compile(r"(?<![\w.$@])(?:\w+::)?install_version\s*\(")
_DOWNLOAD = re.compile(r"(?<![\w.$@])download\.file\s*\(")
_IDENT = re.compile(r"[A-Za-z.][\w.]*")
_IDENT_IN_EXPR = re.compile(r"(?<![\w.$@\"'])([A-Za-z.][\w.]*)(?![\w.(\"'])")


def _resolve(expr: str, vectors: dict[str, list[str]]) -> list[str]:
    """The strings an expression stands for: its literals, and the vectors its identifiers name (at most MAX_NAMES)."""
    found = literals(expr)
    for ident in islice(_IDENT_IN_EXPR.findall(STR.sub('""', expr[:WINDOW * 4])), 200):
        found += vectors.get(ident, [])
        if len(found) > MAX_NAMES:
            break
    return list(dict.fromkeys(found[:MAX_NAMES]))


def _scan_packages(rel: str, text: str, out: PackageList, deadline: float | None) -> None:
    """One R file: the packages it loads or installs, a pinned version, a patch recipe, anything unusual."""
    if rel.lower().endswith(".rmd"):
        text = package._rmd_code(text[:SCAN_CHARS])
    if len(text) > SCAN_CHARS:
        out.truncated = True
        out.note(f"{posixpath.basename(rel)} 太长，只读了前 {SCAN_CHARS // 1024} KB")
    clean, masked = mask(text)
    where = posixpath.basename(rel)
    budget = Budget(deadline=deadline)
    try:
        _scan(text, clean, masked, where, out, budget)
    except _Over:
        out.truncated = True
        out.note(f"{where} 的写法太密或太长，后面的没有扫完")


def _scan(text: str, clean: str, masked: str, where: str, out: PackageList, budget: Budget) -> None:
    vectors: dict[str, list[str]] = {}
    for m in hits(_VECTOR, masked):
        if m.group(2).startswith("c"):
            body = inner(clean, m.end(), budget)
            if body.strip() and _only_strings(body):
                vectors[m.group(1)] = literals(body)
        else:
            lit = STR.match(clean, m.end())
            if lit:
                vectors[m.group(1)] = [lit.group(1) or lit.group(2) or ""]
    for m in hits(_FOR, masked):
        vectors[m.group(1)] = _resolve(inner(clean, m.end(), budget), vectors)

    def names_of(expr: str) -> list[str]:
        return [n for n in _resolve(expr, vectors) if valid_name(n)]

    for m in hits(_LIBRARY, masked):
        args = split_args(inner(clean, m.end(), budget), budget)
        if not args:
            continue
        key, first = _named(args[0])
        if key and key not in ("package", "pkg"):
            first = args[0]
        char_only = any(re.match(r"character\.only\s*=\s*(?:TRUE|T)\b", a) for a in args[1:])
        if STR.fullmatch(first):
            out.add(literals(first)[0], where)
        elif _IDENT.fullmatch(first):
            for n in (names_of(first) if char_only else [first]):
                out.add(n, where)
    for m in hits(_PLOAD, masked):
        for a in split_args(inner(clean, m.end(), budget), budget):
            key, val = _named(a)
            if key == "char":
                for n in names_of(val):
                    out.add(n, where)
            elif key:
                continue
            elif STR.fullmatch(val):
                out.add(literals(val)[0], where)
            elif _IDENT.fullmatch(val):
                out.add(val, where)
    for m in hits(_APPLY, masked):
        args = split_args(inner(clean, m.end(), budget), budget)
        if len(args) >= 2 and any(re.fullmatch(r"(?:base::)?(?:library|require)", _named(a)[1]) for a in args[1:]):
            for n in names_of(args[0]):
                out.add(n, where)
    for m in hits(_INSTALL, masked):
        args = split_args(inner(clean, m.end(), budget), budget)
        if not args:
            continue
        if any(re.match(r"repos\s*=\s*NULL\b", a) for a in args[1:]):
            out.note(f"{where} 里有 install.packages(..., repos = NULL)：装的是本地文件，这一步只从 CRAN 装，不运行它")
            continue
        key, first = _named(args[0])
        for n in names_of(first if key in ("", "pkgs") else args[0]):
            out.add(n, where)
    for m in hits(_NS, masked, MATCHES * 5):
        out.add(m.group(1), where)
    for fn in call_index(text)["hint"]:
        out.add(FUNCTION_PACKAGES[fn], f"{where}（用到 {fn}()）")

    for m in hits(_OTHER_INSTALL, masked):
        arg = literals(inner(clean, m.end(), budget), 1)
        out.note(f"{where} 里有 {m.group(1)}({arg[0][:60] if arg else '…'})：不是 CRAN 的包，没有安装")
    for m in hits(_VERSION_INSTALL, masked):
        name = ver = ""
        for a in split_args(inner(clean, m.end(), budget), budget, 6):
            key, val = _named(a)
            if key in ("package", "pkg") or (not key and not name and STR.fullmatch(val)):
                name = (literals(val, 1) or [""])[0]
            elif key == "version" or (not key and name and STR.fullmatch(val)):
                ver = (literals(val, 1) or [""])[0]
        if valid_name(name) and valid_version(ver):
            out.pins[name] = Pin(name, ver, tarball_urls(name, ver), f"{where}: install_version")
            out.add(name, where)
        else:
            out.note(f"{where} 里的 install_version(...) 看不懂包名或版本，没有固定版本")
    for m in hits(_DOWNLOAD, masked):
        url = (literals(inner(clean, m.end(), budget), 1) or [""])[0][:300]
        t = CRAN_TARBALL.fullmatch(url)
        if t and t.group(2) == (t.group(1) or t.group(2)) and valid_name(t.group(2)) and valid_version(t.group(3)):
            pkg, ver = t.group(2), t.group(3)
            out.pins[pkg] = Pin(pkg, ver, tarball_urls(pkg, ver), f"{where}: download.file")
            out.add(pkg, where)
            patch, why = patch_recipe(clean, masked, pkg, ver, budget)
            if patch:
                out.patches[pkg] = patch
                out.note(f"{where} 从 CRAN 下载 {pkg} {ver} 的源码并挪动了 {len(patch.edits)} 行，装的是改过的版本")
            elif why:
                out.note(f"{where} 对 {pkg} 的补丁没有照做（{why}），装的是 CRAN 原版")
        elif url:
            out.note(f"{where} 里 download.file({url[:80]})：不是 CRAN 的源码包，没有下载")


def tarball_urls(name: str, ver: str) -> list[str]:
    return [f"{CRAN}/src/contrib/{name}_{ver}.tar.gz", f"{CRAN}/src/contrib/Archive/{name}/{name}_{ver}.tar.gz"]


_READLINES = re.compile(r"([A-Za-z.][\w.]*)\s*(?:<-|=)\s*readLines\s*\(")


def patch_recipe(clean: str, masked: str, pkg: str, ver: str, budget: Budget | None = None) -> tuple[SourcePatch | None, str]:
    """`x <- readLines("pkg/R/file.R"); x[a] <- x[b]; x[b] <- ""; writeLines(x, "pkg/R/file.R")` as data, and (patch,
    reason it was refused). The recipe is accepted only when it can do nothing but move or drop lines of the CRAN source:
    every edit is `x[a] <- x[b]` or `x[a] <- ""`. Any other literal is refused whole, because the package's R files run
    when it is installed, and so would whatever text a script managed to write into them. The file must lie inside the
    package's own folder, the line numbers be plain, and the variable the same throughout."""
    m = _READLINES.search(masked)
    if not m:
        return None, ""
    var = m.group(1)
    path = (literals(inner(clean, m.end(), budget), 1) or [""])[0]
    if not re.fullmatch(rf"{re.escape(pkg)}/[A-Za-z0-9_./-]{{1,120}}", path) or ".." in path:
        return None, "文件不在这个包自己的目录里"
    pat = re.compile(rf"(?<![\w.$@]){re.escape(var)}\[(\d{{1,6}})\]\s*(?:<-|=)(?!=)\s*"
                     rf"(?:{re.escape(var)}\[(\d{{1,6}})\]|(\"\"|'')|\"[^\"\n]{{0,200}}\"|'[^'\n]{{0,200}}')")
    edits: list[tuple[int, int | str]] = []
    for e in hits(pat, clean, MAX_EDITS + 1):
        if e.group(2):
            edits.append((int(e.group(1)), int(e.group(2))))
        elif e.group(3) is not None:
            edits.append((int(e.group(1)), ""))
        else:
            return None, "里面有写入新文字的一行，只认「挪一行」和「清空一行」"
    wrote = re.search(rf"writeLines\s*\(\s*{re.escape(var)}\s*,\s*(?:con\s*=\s*)?[\"']{re.escape(path)}[\"']", clean)
    if not edits or not wrote:
        return None, "没有看懂"
    if len(edits) > MAX_EDITS:
        return None, "改动太多"
    patch = SourcePatch(pkg, ver, path.split("/", 1)[1], edits, why="作者的脚本自己的补丁")
    known = KNOWN_PATCHES.get(pkg)
    if known and known["version"] == ver and known["file"] == patch.file and [tuple(x) for x in known["edits"]] == edits:
        patch.guard = [tuple(g) for g in known["guard"]]
    return patch, ""


def parse_packages(texts: dict[str, str], extra: Iterable[str] = ()) -> PackageList:
    """The packages the given R files (path → text) load or install, with pins, patch recipes and notes. Reads text;
    nothing here executes it. `extra` adds names by hand (rl data rrun --package). The files together get SCAN_SECONDS."""
    out = PackageList()
    deadline = time.monotonic() + SCAN_SECONDS
    for rel in sorted(texts, key=lambda p: (not SETUP_NAME.fullmatch(posixpath.basename(p)), p)):
        if time.monotonic() > deadline:
            out.truncated = True
            out.note("文件太多或太长，后面的 R 文件没有扫")
            break
        _scan_packages(rel, texts[rel], out, deadline)
    for name in extra:
        out.add(name.strip(), "手动指定")
    for name, rec in KNOWN_PATCHES.items():
        if name in out.packages and name not in out.patches:
            pin = out.pins.get(name)
            if pin is None:
                out.pins[name] = Pin(name, rec["version"], tarball_urls(name, rec["version"]), "内置补丁")
            if out.pins[name].version == rec["version"]:
                out.patches[name] = SourcePatch(name, rec["version"], rec["file"], [tuple(e) for e in rec["edits"]],
                                                [tuple(g) for g in rec["guard"]], rec["why"])
                out.note(f"{name} {rec['version']}：{rec['why']}")
    if any("repos = NULL" in n for n in out.notes) and out.patches:
        out.notes = [n for n in out.notes if "repos = NULL" not in n]       # that line is the patch's own last step
    return out


# ---------------------------------------------------------------- what a script sources, reads, writes, and where it runs

_FN = re.compile(r"(?<![\w.$@])([A-Za-z_.][\w.]*)\s*\(")
_SOURCE_NAMES = frozenset({"source", "sys.source"})
_READ_NAMES = frozenset(package.R_READS)
_WRITE_NAMES = frozenset({
    "ggsave", "pdf", "png", "jpeg", "svg", "tiff", "bmp", "write.csv", "write.csv2", "write.table", "write_csv",
    "write_delim", "write_tsv", "fwrite", "saveRDS", "write_rds", "save", "save.image", "writeLines", "sink", "cat",
    "stargazer", "write_dta", "write.dta", "write_parquet", "dev.copy", "write_xlsx", "xtable", "print", "texreg",
    "write.xlsx"})
_DATA_FILE = re.compile(r"\.(?:rdata|rda|rds)$", re.I)


@lru_cache(maxsize=48)
def call_index(text: str) -> dict[str, tuple]:
    """One pass over the function calls of a text (the first CALLS of them): where the source(), data-reading and writing
    calls end (the offset just after their "("), and which of the package-hint functions appear. Shared by everything
    below, so a text is walked once however many questions are asked of it."""
    _, masked = mask(text)
    source: list[int] = []
    read: list[tuple[int, str]] = []
    write: list[int] = []
    hint: dict[str, None] = {}
    for m in islice(_FN.finditer(masked), CALLS):
        fn = m.group(1)
        if fn in _SOURCE_NAMES:
            source.append(m.end())
        elif fn in _READ_NAMES:
            read.append((m.end(), fn))
        elif fn in _WRITE_NAMES:
            write.append(m.end())
        if fn in FUNCTION_PACKAGES:
            hint[fn] = None
    return {"source": tuple(source), "read": tuple(read), "write": tuple(write), "hint": tuple(hint)}


def _first_paths(clean: str, ends: Iterable[int], keep=None, limit: int = 200) -> tuple[str, ...]:
    """For each call (the offset after its "("), the path its first argument means: package._paths_from over a
    WINDOW-sized look at it, the first reading of it only."""
    out: dict[str, None] = {}
    for end, fn in ends:
        for p in package._paths_from(package._first_arg(clean, end))[:1]:
            if p and len(p) < 400 and (keep is None or keep(fn, p)):
                out[p] = None
        if len(out) >= limit:
            break
    return tuple(out)


@lru_cache(maxsize=48)
def sourced_files(text: str) -> tuple[str, ...]:
    """The literal paths of the source() / sys.source() calls of one R text (at most 200)."""
    clean, _ = mask(text)
    return _first_paths(clean, ((e, "source") for e in call_index(text)["source"][:MATCHES]))


@lru_cache(maxsize=48)
def read_refs(text: str) -> tuple[str, ...]:
    """The literal paths an R text reads data from (fread, read.csv, load, readRDS …), at most 200."""
    clean, _ = mask(text)
    return _first_paths(clean, call_index(text)["read"][:MATCHES * 2], lambda fn, p: fn != "load" or bool(_DATA_FILE.search(p)))


@lru_cache(maxsize=48)
def write_dirs(text: str) -> tuple[str, ...]:
    """The folders (relative to the working folder) a text writes into, from the path literals of its save / plot / write
    calls: R does not create a folder, so a script that saves into ./figures/ fails when it is not there."""
    clean, _ = mask(text)
    budget = Budget()
    out: dict[str, None] = {}
    try:
        for end in call_index(text)["write"][:MATCHES * 2]:
            for lit in literals(inner(clean, end, budget), 4):
                p = lit[:200].replace("\\", "/")
                if "/" in p and "://" not in p and not p.startswith("/"):
                    d = posixpath.dirname(posixpath.normpath(p))
                    if d and d != "." and ".." not in d.split("/"):
                        out[d] = None
            if len(out) >= 100:
                break
    except _Over:
        pass
    return tuple(out)


@lru_cache(maxsize=48)
def _first_components(text: str) -> frozenset[str]:
    first = set()
    for p in (*sourced_files(text), *read_refs(text)):
        p = p.replace("\\", "/")
        if p.startswith("./") and "/" in p[2:]:
            first.add(p[2:].split("/", 1)[0])
    return frozenset(first)


def cwd_of(script: str, text: str) -> str:
    """The folder (under the authors' root) a script is meant to run from: its own folder, or the parent when the
    script sits in code/ and refers to "./code/..." and "./data/..." (the usual layout of a replication package)."""
    here = posixpath.dirname(script)
    first = _first_components(text)
    base = posixpath.basename(here)
    if here and (base in first or (base.lower() in CODE_DIRS and first - {base})):
        return posixpath.dirname(here)
    return here


class FileIndex:
    """The saved files, indexed once: exact paths and by base name, so that no lookup walks the whole list."""

    def __init__(self, files: Iterable[str]):
        self.exact: set[str] = set()
        self.by_name: dict[str, list[str]] = {}
        for f in files:
            self.exact.add(f)
            self.by_name.setdefault(posixpath.basename(f), []).append(f)

    def find(self, rel: str) -> str | None:
        """The saved file `rel` means: the exact path, or the one whose path ends with it."""
        if rel in self.exact:
            return rel
        for f in self.by_name.get(posixpath.basename(rel), [])[:50]:
            if f.endswith("/" + rel):
                return f
        return None

    def by_base(self, base: str) -> list[str]:
        return self.by_name.get(base, [])


MAX_VISIT = 300


def walk_sources(files: Iterable[str] | FileIndex, script: str, texts: dict[str, str]) -> tuple[list[str], list[str]]:
    """(the saved scripts `script` loads through source(), itself first; the files it sources that are not saved), as
    paths under the authors' root (the folder a script runs from, see cwd_of). At most MAX_VISIT scripts are followed and
    200 paths are read per script."""
    index = files if isinstance(files, FileIndex) else FileIndex(files)
    missing: dict[str, None] = {}
    seen: dict[str, None] = {}
    queue = [script]
    pos = 0
    while pos < len(queue) and len(seen) < MAX_VISIT:
        cur = queue[pos]
        pos += 1
        if cur in seen or cur not in texts:
            continue
        seen[cur] = None
        cwd = cwd_of(cur, texts[cur])
        for ref in sourced_files(texts[cur]):
            ref = ref.replace("\\", "/")
            if re.match(r"^(?:[A-Za-z]:)?/", ref) or ".." in ref.split("/"):
                tail = posixpath.basename(ref)
                hit = index.by_base(tail)
                if hit:
                    queue.append(hit[0])
                else:
                    missing[tail] = None
                continue
            rel = posixpath.normpath(posixpath.join(cwd, ref))
            hit = index.find(rel)
            if hit:
                queue.append(hit)
            else:
                missing[rel] = None
        if len(missing) >= 200:
            break
    return list(seen), list(missing)


def missing_sourced(files: Iterable[str] | FileIndex, script: str, texts: dict[str, str]) -> list[str]:
    """The files `script` (and the helpers it loads) source() that are not among the saved files."""
    return walk_sources(files, script, texts)[1]


def source_chain(files: Iterable[str] | FileIndex, script: str, texts: dict[str, str]) -> list[str]:
    """`script` and the saved scripts it loads through source(), in the order they are met."""
    return walk_sources(files, script, texts)[0]


# ---------------------------------------------------------------- loading what is not installed: the rule before any AI

@dataclass
class Edit:
    start: int
    end: int
    replacement: str
    why: str


def _vector_edits(clean: str, body_start: int, body: str, absent: set[str], var: str) -> list[Edit]:
    """Take the strings in `absent` out of the all-strings vector c(...) whose body starts at `body_start`: each one with
    the comma after it; a run at the end with the comma before it. Formatting and the other entries are left alone."""
    items = [(m.start(), m.end(), (m.group(1) if m.group(1) is not None else m.group(2))) for m in islice(STR.finditer(body), 500)]
    if not items:
        return []
    keep_flags = [name not in absent for _, _, name in items]
    if all(keep_flags):
        return []
    out: list[Edit] = []
    last_kept = max((i for i, k in enumerate(keep_flags) if k), default=-1)
    first_end = body_start if last_kept < 0 else body_start + items[last_kept][1]
    for i, (a, b, name) in enumerate(items):
        if keep_flags[i] or i > last_kept:
            continue
        a2, b2 = body_start + a, body_start + b
        j = b2
        while j < len(clean) and clean[j] in " \t":
            j += 1
        if j < len(clean) and clean[j] == ",":
            j += 1
            while j < len(clean) and clean[j] in " \t":
                j += 1
            out.append(Edit(a2, j, "", f"{var} 里去掉 {name}"))
    if last_kept < len(items) - 1:                         # a run of absent names at the end, with the comma before it
        out.append(Edit(first_end, body_start + items[-1][1], "", f"{var} 里去掉 " + "、".join(
            n for k, (_, _, n) in zip(keep_flags[last_kept + 1:], items[last_kept + 1:]) if not k)))
    return out


def library_edits(text: str, absent: set[str]) -> list[Edit]:
    """Edits that stop an R text from loading packages that are not installed, found by rule and not by a model: a
    library(x) of an absent package becomes invisible(NULL) (require() of one already just returns FALSE), and an absent
    name leaves the all-strings vector a loading loop or lapply(pkgs, library) walks. Only when the text is small enough to
    have been read whole; the call or the vector must lie inside the WINDOW."""
    if not absent or len(text) > SCAN_CHARS:
        return []
    clean, masked = mask(text)
    budget = Budget()
    edits: list[Edit] = []
    try:
        vectors: dict[str, tuple[int, str]] = {}
        for m in hits(_VECTOR, masked):
            if m.group(2).startswith("c"):
                body = inner(clean, m.end(), budget)
                if body.strip() and _only_strings(body) and len(body) < WINDOW:
                    vectors[m.group(1)] = (m.end(), body)
        load_vars: set[str] = set()
        for m in hits(_LIBRARY, masked):
            body = inner(clean, m.end(), budget)
            args = split_args(body, budget)
            if not args or len(body) >= WINDOW:
                continue
            key, first = _named(args[0])
            if key and key not in ("package", "pkg"):
                first = args[0]
            char_only = any(re.match(r"character\.only\s*=\s*(?:TRUE|T)\b", a) for a in args[1:])
            if m.group(1) == "library" and STR.fullmatch(first) and literals(first)[0] in absent:
                edits.append(Edit(m.start(), m.end() + len(body) + 1, "invisible(NULL)", f"library({literals(first)[0]}) 没装上，改成空操作"))
            elif m.group(1) == "library" and _IDENT.fullmatch(first) and not char_only and first in absent:
                edits.append(Edit(m.start(), m.end() + len(body) + 1, "invisible(NULL)", f"library({first}) 没装上，改成空操作"))
            elif char_only and _IDENT.fullmatch(first):
                load_vars.add(first)
        for m in hits(_APPLY, masked):
            body = inner(clean, m.end(), budget)
            args = split_args(body, budget)
            if len(args) >= 2 and any(re.fullmatch(r"(?:base::)?(?:library|require)", _named(a)[1]) for a in args[1:]):
                load_vars.update(_IDENT_IN_EXPR.findall(STR.sub('""', args[0])))
        for m in hits(_FOR, masked):
            loop_expr = inner(clean, m.end(), budget)
            after = masked[m.end() + len(loop_expr) + 1:][:240]
            if re.search(r"\b(?:library|require)\s*\(", after):
                load_vars.update(_IDENT_IN_EXPR.findall(STR.sub('""', loop_expr)))
                load_vars.add(m.group(1))
        for m in hits(_PLOAD, masked):
            for a in split_args(inner(clean, m.end(), budget), budget):
                key, val = _named(a)
                if key == "char":
                    load_vars.update(_IDENT_IN_EXPR.findall(STR.sub('""', val)))
        for var in load_vars:
            if var in vectors:
                start, body = vectors[var]
                edits += _vector_edits(clean, start, body, absent, var)
    except _Over:
        pass
    edits.sort(key=lambda e: e.start)
    kept: list[Edit] = []
    for e in edits:                                        # never two edits over the same characters
        if not kept or e.start >= kept[-1].end:
            kept.append(e)
    return kept


def apply_edits(text: str, edits: list[Edit]) -> str:
    out = text
    for e in sorted(edits, key=lambda e: e.start, reverse=True):
        out = out[:e.start] + e.replacement + out[e.end:]
    return out
