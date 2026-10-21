"""待补数据 (card 0188, docs/design/REPLICATION-v4-DCL.md §3C): the data files the author's scripts read that the lab does not have.

The agent gets the replication package first (fetch.py picks the data the main scripts read). What the scripts still
read and nobody holds is listed here, one row per file: the scripts that need it, why it is not here, where it may
come from, and an upload. Once a file is uploaded the row turns 已补; until then the scripts that need it are
"waiting for data" (waiting_scripts) and nothing else is blocked. Pure and rule-based: no AI, no network.

What is missing. package.py is run over the author's texts in the plan's workspace (author/), exactly as
workspace.author_code does, with the names of what is present as the "repository": the attached datasets by original
name, the files elsewhere in the workspace, and the files uploaded here. A data read that matches none of them is
missing. Reads the package's own scripts produce (write.csv, saveRDS, Stata save …) are not: they are intermediate
files, not data to fetch. Which scripts are counted: the main scripts (what the session translates) by default; the
rest by `include`.

Why. In this order, from what fetch.py recorded when it listed the source (<plan>/listing.json):
  restricted     the source marks the file restricted (Dataverse), or the plan's link is marked 需申请
  oversize       listed, but larger than the download cap (500 MB) or the default pick's (50 MB)
  unsupported    listed, but a format the lab cannot import (.rds needs R, .xls)
  unfetched      listed, small, never downloaded (a script that was not main read it)
  other paper    not listed, and the path points at somebody else's package (otherdata/CarterSmith/…, HorowitzStam…)
  unknown        everything else: 来源不明
Folder names are matched by shape (see _other_paper), so a hint says "looks like", never more.

Upload. upload() takes a file for one missing row, imports it through fetch.import_upload (the same size cap, formats,
zip rules and safe names as a fetch) as a dataset attached to the plan, remembers the pairing in <plan>/fills.json,
and puts the dataset's copies into the workspace's data/ when the workspace is already prepared. workspace.prepare
rebuilds data/ from the plan's datasets at every run, so the dataset is what lasts.
"""
from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..db.connection import now_iso
from . import fetch, package, store, workspace

log = logging.getLogger("research_lab.replication.missing")
ALL = "all"                                 # include=ALL counts every script, not only the main ones
FILLS_FILE = "fills.json"
MAX_ROWS = 300                              # rows of missing files the panel draws; the rest are counted
MAX_FILES = 5000                            # distinct missing paths one scan keeps; a package that reads more is not a real one
WRITE_SCAN_CHARS = 1_000_000                # characters of write-call text written_names reads in all
STATA_WINDOW = 400                          # what package._stata_path looks at after a Stata command
DATA_LIKE = frozenset(fetch.DATA_EXTS) | {".rdata", ".rda", ".txt", ".dat", ".xlsm", ".zsav", ".pq", ".sas7bdat", ".xpt",
                                          ".tab", ".gz", ".feather", ".pkl", ".pickle", ".mat"}
NEEDS_R = {".rdata", ".rda", ".rds"}
SKIP_DIRS = {"author", "outputs", "logs", "tmp", "scripts"}      # not data: the authors' text, the AI's own files
OTHER_DIRS = {"otherdata", "other_data", "other-data", "external", "externaldata", "external_data", "thirdparty",
              "third_party", "third-party", "outside", "otherpapers", "other_papers"}
# Words that make a CamelCase folder a topic, not a pair of surnames (RawData, ReplicationFiles …)
GENERIC = {"data", "raw", "clean", "cleaned", "replication", "original", "final", "output", "input", "analysis", "results",
           "tables", "figures", "code", "scripts", "files", "file", "merged", "processed", "source", "sources", "archive",
           "temp", "new", "old", "main", "extra", "extras", "supplement", "supplementary", "appendix", "survey", "panel",
           "meeting", "monthly", "agenda", "country", "province", "commune", "district", "county", "city", "sample",
           "master", "base", "full", "all", "test", "train", "backup", "copy", "version", "dataset", "datasets", "wave",
           "round", "year", "month", "week", "day", "event", "events", "text", "texts", "speech", "speeches", "election",
           "elections", "vote", "votes", "voting", "trade", "war", "wars", "conflict", "conflicts", "alliance", "alliances",
           "international", "global", "world", "regional", "national", "local", "public", "private", "state", "states"}
_CAMEL = re.compile(r"[A-Z][a-z]{2,}")
_R_WRITE = re.compile(r"\b(?:write\.csv2?|write_csv2?|write_tsv|write_delim|write\.table|write\.dta|write_dta|write_sav|"
                      r"write_rds|write_feather|write_parquet|write_xlsx|write\.xlsx|fwrite|saveRDS|save\.image|save)\s*\(")
_PY_WRITE = re.compile(r"\.to_(?:csv|parquet|pickle|stata|excel|feather|json)\s*\(")
_ST_WRITE = re.compile(r"(?:^|\n)[ \t]*(save|saveold|outsheet|export[ \t]+(?:delimited|excel))\b([^\n]*)", re.I)
_USING = re.compile(r"\busing\b", re.I)


class MissingError(ValueError):
    """Refused, in Chinese, as the page shows it."""


# ---------------------------------------------------------------- the rows

@dataclass
class Item:
    key: str                                   # the path as the scripts write it, tidied: "otherdata/CarterSmith/x.dta"
    name: str                                  # its file name
    state: str                                 # missing | filled
    reason: str = ""                           # why the lab does not have it
    hint: str = ""                             # where it may come from
    why: str = ""                              # machine-readable reason: restricted | oversize | unsupported | unfetched | other-paper | unknown
    size: int | None = None                    # as listed, when the listing has it
    scripts: list[str] = field(default_factory=list)   # the counted scripts that need it, as the package names them (code/x.R)
    dataset: str | None = None                 # the dataset an upload became (filled)
    dataset_name: str = ""
    filled_with: str = ""                      # the file Leo uploaded (filled)

    @property
    def filled(self) -> bool:
        return self.state == "filled"


@dataclass
class Report:
    plan_id: str
    items: list[Item] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)       # the counted scripts (code/x.R)
    all_scripts: list[str] = field(default_factory=list)   # every script the package reads, helpers apart
    unknown: list[str] = field(default_factory=list)       # names given to `include` that match no script
    note: str = ""                                         # why there is nothing to list (no author code saved yet)
    total_missing: int = 0                                 # missing files in all, of which `items` holds the first rows
    hidden: int = 0                                        # missing files left out of `items` (past the row limit)
    truncated: bool = False                                # the scan stopped at MAX_FILES paths: total_missing is a lower bound
    waiting: dict[str, list[str]] = field(default_factory=dict)   # script → file names it waits for, from every missing file

    @property
    def missing(self) -> list[Item]:
        return [i for i in self.items if not i.filled]

    @property
    def filled(self) -> list[Item]:
        return [i for i in self.items if i.filled]

    @property
    def more_scripts(self) -> bool:
        return len(self.all_scripts) > len(self.scripts)


@dataclass
class Uploaded:
    item: Item
    datasets: list[str]                        # every dataset the upload became (a zip can hold several files)
    staged: bool = False                       # the workspace's data/ got the copies now


# ---------------------------------------------------------------- paths a script reads

def _key(raw: str, lang: str) -> str | None:
    """A path a script reads, tidied for a row: separators and the author's own `$globals` as package.py reads them,
    leading ./ and ../ dropped (they say where the author's folder was, which is not ours), a Stata `use x` read as
    x.dta. None for what is not a file name: a built path (`{`, `%`), a folder, a name without a data extension."""
    s = package._norm(raw).strip()
    s = re.sub(r"^(?:\.{1,2}/)+", "", s)
    if not s or s.endswith("/") or any(c in s for c in "{}%*?<>|\x00") or len(s) > 240:
        return None
    s = posixpath.normpath(s)
    ext = posixpath.splitext(s)[1].lower()
    if not ext and lang == "stata":
        s, ext = s + ".dta", ".dta"
    if ext not in DATA_LIKE or posixpath.basename(s).startswith("."):
        return None
    return s


_TOKEN = re.compile(r"\"((?:[^\"\\\n]|\\.)*)\"|'((?:[^'\\\n]|\\.)*)'|([()\[\]{}])")


def _call_strings(clean: str, pos: int) -> list[str]:
    """The string literals of the call whose "(" ends just before `pos`, up to its closing bracket and never past
    package.MAX_ARG characters (the reach package._first_arg has). One regex pass finds the literals and brackets, so the
    cost is the tokens in that window, not its characters."""
    depth, out = 0, []
    for m in _TOKEN.finditer(clean, pos, pos + package.MAX_ARG):
        a, b, bracket = m.groups()
        if bracket is None:
            out.append(a if a is not None else b)
        elif bracket in "([{":
            depth += 1
        elif depth == 0:
            break
        else:
            depth -= 1
    return out


def written_names(code: dict[str, str], budget: int = WRITE_SCAN_CHARS) -> set[str]:
    """The lower-case file names the package's own scripts write: write.csv, saveRDS, save(…, file=), to_csv, Stata
    save / export / outsheet. A later script that reads one of them reads an intermediate file, not data to fetch.
    `budget` is the most characters of call text read in all (each call is charged its window): a text made of nothing
    but write calls cannot cost more than that, it just stops being read, and the files it names show as missing."""
    out: set[str] = set()
    left = budget

    def add(raw: str, lang: str) -> None:
        k = _key(raw, lang)
        if k:
            out.add(posixpath.basename(k).lower())

    for path, text in code.items():
        if left <= 0:
            break
        lang = package.lang_of(path) or ""
        if lang == "r" and package.ext_of(path) == ".rmd":
            text = package._rmd_code(text)
        clean, masked = package._mask(text, lang)
        if lang in ("r", "python"):
            for m in (_R_WRITE if lang == "r" else _PY_WRITE).finditer(masked):
                left -= package.MAX_ARG
                if left <= 0:
                    break
                for lit in _call_strings(clean, m.end()):
                    add(lit, lang)
        elif lang == "stata":
            for m in _ST_WRITE.finditer(masked):
                left -= STATA_WINDOW
                if left <= 0:
                    break
                start = m.start(2)
                using = _USING.search(masked, start, m.end(2))
                raw = package._stata_path(clean, using.end() if using else start)
                add(raw + (".dta" if m.group(1).lower().startswith("save") and not posixpath.splitext(raw)[1] else ""), lang)
    return out


def _other_paper(key: str) -> str | None:
    """The folder (or file) a path names when it looks like another paper's replication package: below a folder called
    otherdata / external / …, or a folder, or a file name with a year or suffix after it, that starts with two
    capitalised surnames glued together (CarterSmith/, HorowitzStam2015.dta). A guess by shape, so the page words it
    as a clue. A path of the author's own machine (/Users/…) only counts under an otherdata-style folder: its
    folders are the author's, not a colleague's."""
    parts = key.split("/")
    dirs, stem = [d for d in parts[:-1] if d], posixpath.splitext(parts[-1])[0]
    for i, d in enumerate(dirs):
        if d.lower() in OTHER_DIRS:
            return dirs[i + 1] if i + 1 < len(dirs) else stem
    if key.startswith("/"):
        return None
    for d in dirs:
        if _surnames(d):
            return d
    tokens = re.split(r"[_\-. ]+", stem)
    if (len(tokens) > 1 or re.search(r"\d$", stem)) and _surnames(tokens[0]):
        return re.sub(r"[\d_\-]+$", "", tokens[0])
    return None


def _surnames(word: str) -> bool:
    """CarterSmith, HorowitzStam, MalletBeauchamp2019: two or more capitalised words, none of them a generic one."""
    base = re.sub(r"[\d_\-]+$", "", word)
    if not base or "".join(_CAMEL.findall(base)) != base:
        return False
    pieces = _CAMEL.findall(base)
    return len(pieces) >= 2 and not any(p.lower() in GENERIC for p in pieces)


# ---------------------------------------------------------------- what the plan holds

def _fills_path(plan_id: str) -> Path:
    return workspace.plan_root(plan_id) / FILLS_FILE


_fills_lock = threading.Lock()


def read_fills(plan_id: str) -> dict[str, dict[str, Any]]:
    """key → {dataset, file, at, scripts}: the rows an upload filled."""
    try:
        raw = json.loads(_fills_path(plan_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict) and v.get("dataset")} if isinstance(raw, dict) else {}


def _write_fill(plan_id: str, key: str, entry: dict[str, Any]) -> None:
    path = _fills_path(plan_id)
    with _fills_lock:
        fills = read_fills(plan_id)
        fills[key] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(fills, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)


def _author_texts(ws: Path) -> tuple[dict[str, str], dict[str, str]]:
    """(scripts, READMEs) saved under author/, read with the limits workspace.author_code uses, never through a link."""
    root = ws / workspace.AUTHOR
    code: dict[str, str] = {}
    readmes: dict[str, str] = {}
    if os.path.islink(root) or not root.is_dir():
        return code, readmes
    budget = workspace.AUTHOR_BYTES
    for f in workspace.regular_files(root):
        rel, size = f.relative_to(ws).as_posix(), f.stat().st_size
        try:
            if package.lang_of(f.name):
                if len(code) < workspace.AUTHOR_SCRIPTS and size <= min(workspace.AUTHOR_FILE, budget):
                    code[rel], budget = workspace.safe_read(ws, rel, workspace.AUTHOR_FILE), budget - size
            elif package.is_doc(f.name) and re.search(r"readme", f.name, re.I) and len(readmes) < workspace.README_FILES:
                readmes[rel] = workspace.safe_read(ws, rel, workspace.README_CHARS)
        except workspace.WorkspaceError:
            continue
    return code, readmes


def _workspace_names(ws: Path) -> list[str]:
    """The data files elsewhere in the workspace (data/, and whatever a run staged), by file name. The authors' text,
    the AI's scripts, outputs, logs and scratch files are not data the scripts' reads could mean."""
    out: list[str] = []
    try:
        entries = sorted(os.scandir(ws), key=lambda e: e.name)
    except OSError:
        return out
    for e in entries:
        if e.name in SKIP_DIRS or e.is_symlink() or not e.is_dir():
            continue
        out += [f.name for f in workspace.regular_files(Path(e.path)) if posixpath.splitext(f.name)[1].lower() in DATA_LIKE]
    return out


def _loaded(start: str, scripts: dict[str, package.Script]) -> list[package.Script]:
    """`start` and every script it loads, in order."""
    seen: list[str] = []
    queue = [start]
    while queue:
        p = queue.pop(0)
        if p not in seen and p in scripts:
            seen.append(p)
            queue.extend(scripts[p].sources)
    return [scripts[p] for p in seen]


def _label(path: str) -> str:
    """A script as the package names it: code/x.R, without the workspace's author/ in front."""
    return path.removeprefix(workspace.AUTHOR + "/")


def _chosen(include: Iterable[str] | str | None, scripts: dict[str, package.Script], entry: list[str],
            main: list[str]) -> tuple[list[str], list[str]]:
    """(the counted scripts, the names that matched none). None: the main scripts. ALL: every script. Otherwise the
    scripts named, by workspace path, package path or file name (a name two scripts share counts both)."""
    if include is None:
        return list(main), []
    if include == ALL:
        return list(entry), []
    names = [include] if isinstance(include, str) else list(include)
    chosen: list[str] = []
    unknown: list[str] = []
    for n in names:
        hits = [p for p in scripts if p == n or p == f"{workspace.AUTHOR}/{n}" or posixpath.basename(p) == n]
        if not hits:
            unknown.append(n)
        chosen += [p for p in hits if p not in chosen]
    return chosen, unknown


# ---------------------------------------------------------------- why a file is not here

def _size_text(n: int) -> str:
    return f"{n / fetch.MB:.0f} MB" if n >= fetch.MB else f"{max(1, n // 1024)} KB"


def _by_base(listing: dict[str, dict[str, Any]]) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """The listing by lower-case file name, built once per report so that finding a file is not a pass over the listing."""
    out: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for n, e in listing.items():
        out.setdefault(posixpath.basename(n).lower(), []).append((n, e))
    return out


def _listed(key: str, by_base: dict[str, list[tuple[str, dict[str, Any]]]]) -> tuple[str, dict[str, Any]] | None:
    """The listed file a script's path means: the same file name, the one sharing the most trailing folders with the path."""
    hits = by_base.get(posixpath.basename(key).lower(), [])
    parts = key.lower().split("/")

    def shared(name: str) -> int:
        n = 0
        for a, b in zip(reversed(name.lower().split("/")), reversed(parts)):
            if a != b:
                break
            n += 1
        return n

    return max(hits, key=lambda h: shared(h[0]), default=None)


def _explain(key: str, by_base: dict[str, list[tuple[str, dict[str, Any]]]], links: list[dict[str, Any]]) -> dict[str, Any]:
    """{why, reason, hint, size} for one missing file. The order is the module doc's. `by_base` is _by_base(listing)."""
    hit = _listed(key, by_base)
    if hit:
        name, e = hit
        size = e.get("size") if isinstance(e.get("size"), int) else None
        ext = posixpath.splitext(name)[1].lower()
        where = f"复现包里的 {name}"
        if e.get("restricted"):
            return dict(why="restricted", size=size, reason="来源标为受限，不能自动下载",
                        hint="需要向作者申请：数据可得性说明里一般写了联系方式")
        note = str(e.get("note") or "")
        if ext in NEEDS_R or ext in fetch.UNSUPPORTED or not fetch.is_data(name) or (note and not note.startswith("超过")):
            why = "需要 R" if ext in NEEDS_R else fetch.UNSUPPORTED.get(ext) or note or "这种格式暂时不能导入"
            return dict(why="unsupported", size=size, reason=f"格式暂时不能导入：{why}",
                        hint=f"{where}；转成 CSV 或 Stata（.dta）后上传")
        if (size is not None and size > fetch.MAX_MB * fetch.MB) or note:
            return dict(why="oversize", size=size, hint=f"{where}；拆分，或只留要用的列转成 Parquet，再上传",
                        reason=(f"文件 {_size_text(size)}，" if size else "文件") + f"超过 {fetch.MAX_MB} MB 上限，不能自动下载")
        if size is not None and size > fetch.AUTO_MAX_MB * fetch.MB:
            return dict(why="oversize", size=size, reason=f"文件 {_size_text(size)}，大于自动下载的 {fetch.AUTO_MAX_MB} MB，没有默认下载",
                        hint=f"{where}；在上面的数据链接点 List files 勾选后 Import，或自己下载后上传")
        return dict(why="unfetched", size=size, reason="复现包里有，还没下载",
                    hint=f"{where}；在上面的数据链接点 List files 勾选后 Import，或直接上传")
    folder = _other_paper(key)
    if folder:
        return dict(why="other-paper", size=None, reason="路径指向别的论文的数据，不在这篇的复现包里",
                    hint=f"来自其他论文的复现包：{folder}")
    request = next((link for link in links if link.get("access") == "request"), None)
    if request:
        return dict(why="restricted", size=None, reason="方案里的数据来源标为需申请",
                    hint=f"来源：{request.get('url') or '方案里的数据链接'}；申请到后上传")
    return dict(why="unknown", size=None, hint="来源不明",
                reason="复现包里没有找到这个文件" if by_base else "还没有记下复现包的文件清单（在数据链接处 List files 并 Import 一次就会记下）")


# ---------------------------------------------------------------- the report

def _tails(key: str) -> list[str]:
    """The key and each trailing run of its folders, lower-cased: a/b/c.csv, b/c.csv, c.csv."""
    parts = key.lower().split("/")
    return ["/".join(parts[i:]) for i in range(len(parts))]


def report(conn, plan_id: str, include: Iterable[str] | str | None = None, *, limit: int | None = MAX_ROWS) -> Report:
    """The rows of 待补数据 for a plan: what the counted scripts read and nobody holds (state missing), then what
    uploads filled (state filled). Reads the saved author texts and the plan's records; changes nothing.

    The author's text is not trusted to be small, and this runs on every plan page load: every pass is linear, at most
    MAX_FILES distinct paths are kept, and only the first `limit` missing files are explained and drawn (None: all, 0:
    none; `total_missing`, `hidden` and `waiting` always cover every file kept)."""
    from . import plans
    plan = plans.get(conn, plan_id)
    if plan is None:
        raise MissingError(f"没有这份复现方案：{plan_id}")
    ws = workspace._real(workspace.workspace_dir(plan_id))
    code, readmes = _author_texts(ws)
    out = Report(plan_id)
    attached = workspace.plan_datasets(conn, plan)
    attached_ids = {d["id"] for d in attached}
    fills = {k: v for k, v in read_fills(plan_id).items() if v["dataset"] in attached_ids}
    if not code:
        out.note = "工作区里还没有作者的脚本（先在数据链接处取回复现包）"
    else:
        names = [d["original_name"] for d in attached] + [posixpath.basename(k) for k in fills] + _workspace_names(ws)
        present = list({n.lower(): n for n in names}.values())      # one entry per name: a repeated name is "ambiguous" to resolve()
        pkg = package.analyze(code, [*code, *readmes, *present], docs=readmes)
        entry = [p for p in pkg.scripts if p not in pkg.helpers] or list(pkg.scripts)
        main = pkg.main or [p for p in entry if pkg.models[p]] or entry
        chosen, out.unknown = _chosen(include, pkg.scripts, entry, main)
        out.scripts, out.all_scripts = [_label(p) for p in chosen], [_label(p) for p in entry]
        written = written_names(code)
        loaders: dict[str, list[str]] = {}                          # script → the counted scripts that run it (itself, or by source())
        for p in chosen:
            for s in _loaded(p, pkg.scripts):
                loaders.setdefault(s.path, []).append(_label(p))
        needs: dict[str, dict[str, None]] = {}
        for path, entries in loaders.items():
            s = pkg.scripts[path]
            for raw in dict.fromkeys(s.unresolved):
                k = _key(raw, s.lang)
                if not k or posixpath.basename(k).lower() in written:
                    continue
                if k not in needs and len(needs) >= MAX_FILES:
                    out.truncated = True
                    break
                needs.setdefault(k, {}).update(dict.fromkeys(entries))
            if out.truncated:
                break
        hosts: dict[str, str] = {}                                  # a trailing path → the longest path that ends in it
        merged: dict[str, dict[str, None]] = {}
        for k in sorted(needs, key=lambda x: (-x.count("/"), x.lower())):
            host = hosts.get(k.lower())
            if host is None:
                host = k
                for t in _tails(k):
                    hosts.setdefault(t, k)
            merged.setdefault(host, {}).update(needs[k])
        ordered = sorted(merged.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))
        out.total_missing = len(ordered)
        for k, scripts in ordered:
            name = posixpath.basename(k)
            for s in scripts:
                out.waiting.setdefault(s, []).append(name)
        by_base, links = _by_base(fetch.read_listing(plan_id)), plan["plan"].get("links") or []
        shown = ordered if limit is None else ordered[:max(0, limit)]
        out.hidden = len(ordered) - len(shown)
        for k, scripts in shown:
            out.items.append(Item(k, posixpath.basename(k), "missing", scripts=list(scripts), **_explain(k, by_base, links)))
    for k, f in sorted(fills.items()):
        ds = next((d for d in attached if d["id"] == f["dataset"]), None)
        out.items.append(Item(k, posixpath.basename(k), "filled", dataset=f["dataset"], scripts=list(f.get("scripts") or []),
                              dataset_name=(ds or {}).get("name") or "", filled_with=str(f.get("file") or "")))
    return out


def missing_for(conn, plan_id: str, include: Iterable[str] | str | None = None) -> list[Item]:
    """The files still missing for the counted scripts (the main ones unless `include` says otherwise): what coverage
    marks 「等数据」 against."""
    return report(conn, plan_id, include, limit=None).missing


def waiting_scripts(conn, plan_id: str, include: Iterable[str] | str | None = ALL) -> dict[str, list[str]]:
    """script (code/x.R) → the file names it is still waiting for. Every script by default, since a coverage item can
    come from an appendix script no main-analysis rule would count."""
    return report(conn, plan_id, include, limit=0).waiting


# ---------------------------------------------------------------- upload

def stage(conn, plan_id: str, ds_ids: Iterable[str]) -> bool:
    """Put the datasets' copies (data/<id>.parquet, .csv, .columns.json) into an already prepared workspace, as
    workspace.prepare makes them. False when the workspace has no data/ yet (prepare makes it at the next run) or the
    copy could not be made; never raises, since the dataset itself is already safe."""
    from . import datasets
    ws = workspace._real(workspace.workspace_dir(plan_id))
    data = ws / "data"
    try:
        if os.path.islink(ws) or os.path.islink(data) or not data.is_dir():
            return False
        import duckdb
        workspace._chmod_tree(data, 0o644)
        try:
            for ds_id in ds_ids:
                ds = datasets.get(conn, ds_id)
                src = store.parquet_path(ds_id)
                if ds is None or not src.exists():
                    continue
                parquet, csv, cols = (workspace._checked(ws, data / f"{ds_id}{ext}") for ext in (".parquet", ".csv", ".columns.json"))
                shutil.copyfile(src, parquet)
                with duckdb.connect() as con:
                    con.execute(f"COPY (SELECT * FROM read_parquet({store.sql_str(str(parquet))})) TO "
                                f"{store.sql_str(str(csv))} (HEADER, DELIMITER ',')")
                cols.write_text(json.dumps({"id": ds_id, "name": ds["name"], "rows": ds["rows"],
                                            "columns": ds["schema"].get("columns", [])}, ensure_ascii=False, indent=1),
                                encoding="utf-8")
        finally:
            workspace._chmod_tree(data, 0o444)
        return True
    except Exception:  # noqa: BLE001 - the upload already succeeded; the next run's prepare makes the copies
        log.warning("could not stage %s into the workspace of %s", list(ds_ids), plan_id, exc_info=True)
        return False


def upload(conn, plan_id: str, key: str, path: Path, filename: str, *,
           max_bytes: int = fetch.MAX_MB * fetch.MB) -> Uploaded:
    """Fill one missing row with an uploaded file. `key` must be a row the report lists as missing for some script
    (the page cannot name any other file); `path` is the received file, which this call moves. The file is imported by
    fetch.import_upload (same cap, formats and safe names as a fetch) as a dataset attached to the plan; the pairing is
    remembered so the row turns 已补, and the workspace's data/ gets the copies when it exists."""
    item = next((i for i in report(conn, plan_id, ALL, limit=None).missing if i.key == key), None)
    if item is None:
        raise MissingError("这个文件不在待补清单里，可能已经补上了。刷新页面再看。")
    try:
        results = fetch.import_upload(conn, plan_id, path, filename, want=item.key, max_bytes=max_bytes)
    except fetch.FetchError as e:
        raise MissingError(str(e)) from e
    ok = [r for r in results if r.dataset]
    if not ok:
        raise MissingError(next((r.error for r in results if r.error), "没能导入这个文件"))
    base = posixpath.basename(item.key).lower()
    primary = next((r for r in ok if r.name.split("›")[-1].strip().lower() == base), ok[0])
    _write_fill(plan_id, item.key, {"dataset": primary.dataset, "file": posixpath.basename(filename.replace("\\", "/")),
                                    "at": now_iso(), "scripts": item.scripts})
    ids = [r.dataset for r in ok if r.dataset]
    return Uploaded(item, ids, stage(conn, plan_id, ids))
