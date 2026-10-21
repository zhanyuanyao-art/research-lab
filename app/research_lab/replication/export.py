"""Export a reviewed replication plan to a `research-design` artifact and an analysis run to an
`analysis` artifact. Tables go into managed blocks of non-earned sections ("Design", "Evidence",
"Reproduction"); the Claim and "What did I figure out" sections stay the user's (method-layer §7
rule 6). The AI reading, if any, goes only into `## AI notes`.

The second half of this file (card 0191) exports a plan as a data folder: see "the data folder" below."""
from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import zipfile
from collections import Counter
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

from ..db.connection import now_iso
from . import plans, runs


class ExportError(ValueError):
    pass


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        s = f"{v:.4g}" if (abs(v) < 1e-3 and v != 0) or abs(v) >= 1e5 else f"{v:.4f}".rstrip("0").rstrip(".")
        return s or "0"
    return str(v).replace("|", "\\|").replace("\n", " ")


def md_table(columns: list[str], rows: list[list[Any]]) -> str:
    if not columns:
        return ""
    head = "| " + " | ".join(_cell(c) or " " for c in columns) + " |"
    sep = "|" + "|".join("---" for _ in columns) + "|"
    body = ["| " + " | ".join(_cell(v) for v in r) + " |" for r in rows]
    return "\n".join([head, sep, *body])


def result_markdown(result: dict[str, Any]) -> str:
    parts = []
    for t in result.get("tables", []):
        parts.append(f"**{t['title']}**（n = {t.get('n')}，缺失/删除 = {t.get('missing') if t.get('missing') is not None else 0}）")
        parts.append("")
        parts.append(md_table(t["columns"], t["rows"]))
        if t.get("note"):
            parts.append("")
            parts.append(f"*{t['note']}*")
        parts.append("")
    for n in result.get("notes", []):
        parts.append(f"> 注：{n}")
    return "\n".join(parts).strip()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def default_project() -> str:
    from ..files import datafiles
    now = datafiles.read("now") or {}
    if now.get("project"):
        return str(now["project"])
    projects = datafiles.read("projects") or []
    return str(projects[0]["slug"]) if projects else "general"


def _create(type_: str, slug: str, project: str, title: str):
    from ..files import artifacts
    from ..files.weeks import week_of
    base, n = slug, 1
    week = week_of()
    while True:
        try:
            path = artifacts.create(type_, slug, project, title=title, week=week)
            break
        except FileExistsError:
            n += 1
            slug = f"{base}-{n}"
            if n > 50:
                raise
    return artifacts.load(path)


def export_plan(conn: sqlite3.Connection, plan_id: str, project: str | None = None) -> str:
    """Reviewed plan → research-design artifact. Returns the artifact ref."""
    from ..files import artifacts
    p = plans.get(conn, plan_id)
    if not p:
        raise ExportError(f"没有这个复现方案：{plan_id}")
    if p["origin"] != "user":
        raise ExportError("AI 建议的方案需要先“标记为已审阅”才能导出")
    pl = p["plan"]
    title = f"复现方案：{p.get('paper_title') or plan_id}"
    a = _create("research-design", f"design-{_slug(plan_id)}", project or default_project(), title)
    a.front["replication_plan"] = plan_id
    if p.get("paper_id"):
        a.front["paper"] = p["paper_id"]
    if p.get("direction"):
        a.front["direction"] = p["direction"]
    fam = pl.get("model", {}).get("family", "")
    lines = [
        f"复现方案 {plan_id}（{plans.STATUS_LABELS.get(p['status'], p['status'])}）"
        + (f"，论文 {p['paper_id']}《{p.get('paper_title') or ''}》" if p.get("paper_id") else ""),
        "",
        md_table(["项目", "内容"], [["分析单位", pl.get("unit_of_analysis", "")], ["时期", pl.get("period", "")],
                                   ["样本规则", pl.get("sample_rule", "")], ["模型", f"{fam}：{pl.get('model', {}).get('specification', '')}"],
                                   ["可行性", f"{plans.LEVEL_LABELS.get(pl.get('feasibility', {}).get('level'), '')}："
                                              f"{pl.get('feasibility', {}).get('why', '')}"]]),
        "",
        "**变量**",
        "",
        md_table(["角色", "变量", "测量", "来源"],
                 [[plans.ROLE_LABELS.get(v["role"], v["role"]), v["name"], v.get("measure", ""), v.get("source", "")] for v in pl.get("variables", [])]),
        "",
        "**数据来源**",
        "",
        md_table(["名称", "获取方式", "位置"], [[s["name"], s.get("access", ""), s.get("url_or_where", "")] for s in pl.get("data_sources", [])]),
        "",
        "**步骤**",
        "",
        *[f"- [{'x' if s.get('done') else ' '}] {s['text']}" + (f" — {s['note']}" if s.get("note") else "") for s in pl.get("steps", [])],
    ]
    for key, label in (("robustness", "稳健性检验"), ("to_obtain", "待获取"), ("caveats", "注意事项")):
        if pl.get(key):
            lines += ["", f"**{label}**", "", *[f"- {x}" for x in pl[key]]]
    if pl.get("datasets"):
        lines += ["", "**已关联数据集**：" + "、".join(pl["datasets"])]
    artifacts.append_to_section(a, "Design", "\n".join(lines))
    artifacts.save(a)
    artifacts.index(conn)
    conn.execute("UPDATE replication_plans SET artifact_ref = ?, updated_at = ? WHERE id = ?", (a.ref, now_iso(), plan_id))
    return a.ref


def export_run(conn: sqlite3.Connection, run_id: str, project: str | None = None) -> str:
    """Run → analysis artifact: result tables (with n and missing), figure link, reproduction code."""
    from ..files import artifacts
    run = runs.get(conn, run_id)
    if not run:
        raise ExportError(f"没有这个运行：{run_id}")
    if run["status"] != "done" or not run.get("result"):
        raise ExportError("运行出错，不能导出")
    res = run["result"]
    title = f"分析：{run['method_label']} · {res.get('dataset', {}).get('name') or run.get('dataset_id')}"
    a = _create("analysis", f"analysis-{_slug(run_id)}", project or default_project(), title)
    a.front["script"] = run_id
    a.front["dataset"] = run.get("dataset_id")
    if run.get("plan_id"):
        a.front["replication_plan"] = run["plan_id"]
    head = (f"{run_id} · {run['method_label']} · 数据集 {run.get('dataset_id')}（{res.get('dataset', {}).get('name', '')}）\n\n"
            f"设置：{res.get('params_label') or '—'}\n\n")
    body = head + result_markdown(res)
    if (run.get("reading") or "").strip():
        body += "\n\n**我的解读**\n\n" + run["reading"].strip()
    if runs.figure_file(run):
        body += f"\n\n![{run_id} 图](/data/runs/{run_id}/figure.png)\n\n图文件：`app/data/{run['figure_path']}`"
    artifacts.append_to_section(a, "Evidence", body)
    artifacts.append_to_section(a, "Reproduction", "```python\n" + (run.get("code") or "").rstrip() + "\n```")
    if run.get("ai"):
        ai = run["ai"]
        notes = ["analysis.read 建议（仅供参考，未经核实，不计入 Claim）：", "", ai.get("reading", "")]
        if ai.get("caveats"):
            notes += ["", "注意：", *[f"- {c}" for c in ai["caveats"]]]
        if ai.get("next_checks"):
            notes += ["", "可做的后续检验：", *[f"- {c}" for c in ai["next_checks"]]]
        artifacts.set_ai_notes(a, "\n".join(notes))
    artifacts.save(a)
    artifacts.index(conn)
    conn.execute("UPDATE analysis_runs SET artifact_ref = ?, updated_at = ? WHERE id = ?", (a.ref, now_iso(), run_id))
    return a.ref


# ================================================================ the data folder (card 0191)
# docs/design/REPLICATION-v4-DCL.md §3E. A plan's ticked 复现清单 items (coverage.py) become a folder, one directory per
# group that has ticked items (正文结果 / 附录稳健性 / 附录其他), the way the DCL lab's package is laid out:
#
#   <plan>-replication/
#     README.md                  every ticked item → script → output file → verdict, the environment, how to rerun,
#                                what is still 「尚未复现」 or 「等数据」, what was left out
#     R-packages.txt             name==version of the plan's R library (when R was used)
#     正文结果/ code/            the authors' scripts as saved (author/), code/modified/ the copies that actually ran
#                                where they differ, code/changes.md, code/python/ the AI's Python scripts
#               data/            the datasets the group's scripts read, as imported (original format), else CSV
#               output/          the group's tables (results.json slice, the stargazer text) and figures
#
# Everything comes from the plan's live workspace and records; nothing is run, fetched or asked of an AI. Names in the
# author's text (script paths, captions, file names) are untrusted: a file is only ever read from the workspace's own
# listing, never from a path the text names, and every name that is written (a file, a README cell) is cleaned first.
# Only regular single-link files are copied, each opened O_NOFOLLOW (the primitives workspace.copy_regular uses), under a
# per-file, per-folder, file-count and time cap; what does not fit is listed in the README, not silently dropped.
#
# The zip is made in a scratch folder, stored under the 课题's drafts/ by the deliverables rules (a cleaned name that never
# overwrites) and attached to the issue that names the paper as material. All of that happens only on Leo's click
# (POST /replicate/<plan>/export-folder); build_folder() alone writes nothing but its destination, which the caller picks.

log = logging.getLogger("research_lab.replication.export")

FOLDER_SUFFIX = "-replication"
MAX_FILE = 100 * 1024 * 1024            # one copied file
MAX_FOLDER = 500 * 1024 * 1024          # the folder before zipping
MAX_FILES = 3000
ZIP_CAP = 200 * 1024 * 1024             # deliverables.MAX_UPLOAD: the largest file the 课题 folders take
EXPORT_SECONDS = 120.0
INDEX_FILES = 20_000                    # files listed per workspace folder
MAX_SCRIPTS = 300                       # scripts copied into one group
PY_SCRIPTS = 50
MAX_OUTPUTS = 600                       # output files placed in all
README_ROWS = 500                       # items listed in the README tables (the coverage list itself stops at 500)
README_SCRIPTS = 4                      # scripts / outputs named on one item's line
README_OUTPUTS = 6
README_MISSING = 40                     # missing data files listed
README_SKIPPED = 30                     # files left out, listed one by one
README_STATUS = 40                      # items listed per status in the 「还没有复现」 section
ENV_LINES = 2000
TEXT_BLOB = 2_000_000                   # characters of a group's scripts searched for dataset names
CHANGES_CHARS = 1_000_000
CSV_PARQUET_CAP = 50 * 1024 * 1024      # a dataset with no original file is re-exported as CSV only when its parquet is smaller
PY_PACKAGES = ("pandas", "numpy", "scipy", "statsmodels", "pyfixest", "differences", "linearmodels", "matplotlib", "pyreadstat")
SKIP_OUTPUT_NAMES = ("results.json", "mapping.json", "changes.md")
_LOCK = threading.Lock()

_CTRL = re.compile("[\x00-\x1f\x7f-\x9f  ​-‏‪-‮⁦-⁩﻿]")
_PATH_BAD = re.compile(r'[\\/:*?"<>|]')
_NAME_OK = re.compile(r"[A-Za-z0-9_./ -]+")              # a folder name the README may name
_CMD_OK = re.compile(r"[A-Za-z0-9_./-]+")                # a path a copy-paste command may carry: no space, quote, $ ` ; & ( )
NO_CMD = "（文件名含特殊字符，请手动打开）"


def _cmd_path(path: str) -> str | None:
    """`path` when it is safe to put in a line Leo copies into a terminal (letters, digits, _ . / - only, no option-like start,
    no .. step), else None. Names come from the authors' text, so a command never quotes them: it leaves them out."""
    return path if _CMD_OK.fullmatch(path) and not path.startswith("-") and ".." not in path.split("/") else None


def _text(value: Any, limit: int = 160) -> str:
    """Text from outside, as one clean line: control and direction characters gone, whitespace collapsed, cut."""
    s = " ".join(_CTRL.sub(" ", str(value if value is not None else "")[: limit * 4]).replace("`", "'").split())
    return s[: limit - 1] + "…" if len(s) > limit else s


def _td(value: Any, limit: int = 120) -> str:
    return _text(value, limit).replace("|", "¦")


def _part(name: Any) -> str:
    """One path component made safe: no separators, control or direction characters, no leading dot, a bounded length."""
    s = _PATH_BAD.sub("_", _CTRL.sub("_", str(name if name is not None else "")[:400])).strip().strip(".").strip()
    if not s:
        return "_"
    stem, ext = os.path.splitext(s)
    return stem[:100] + ext[:16] if len(stem) > 100 else s


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.0f} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


class _Skip(Exception):
    """One file that is not copied; the message is the reason the README gives."""


def _copy_capped(src: Path, dst: Path, cap: int) -> int:
    """src → dst, regular single-link files only, never through a link (O_NOFOLLOW both ways), at most `cap` bytes. A file
    that grows while it is read is cut off and refused. Returns the bytes written."""
    try:
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise _Skip("读不了（可能是链接）") from None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            raise _Skip("不是普通文件")
        if st.st_size > cap:
            raise _Skip(f"超过大小上限（{_mb(st.st_size)}）")
        dst.parent.mkdir(parents=True, exist_ok=True)
        out = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        written = 0
        try:
            with os.fdopen(out, "wb") as o:
                while chunk := fh.read(1 << 20):
                    written += len(chunk)
                    if written > cap:
                        raise _Skip("超过大小上限")
                    o.write(chunk)
        except _Skip:
            dst.unlink(missing_ok=True)
            raise
    return written


def _read_bytes(path: Path, cap: int) -> bytes | None:
    """A regular file's first `cap` bytes, or None (missing, a link, odd, unreadable)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            return None
        return fh.read(cap)


class _Folder:
    """The folder being built: unique clean destination names (a collision gets -2, -3 …; compared case-insensitively, as
    macOS does), the byte, file and time budgets, and what was left out."""

    def __init__(self, root: Path, deadline: float):
        self.root, self.deadline = root, deadline
        self.used: set[str] = set()
        self.bytes = 0
        self.files = 0
        self.skipped: list[tuple[str, str]] = []
        self.skipped_n = 0

    def tick(self) -> None:
        if time.monotonic() > self.deadline:
            raise ExportError(f"导出超过 {int(EXPORT_SECONDS)} 秒，已中止，什么都没有保存。")

    def skip(self, name: Any, why: str) -> None:
        self.skipped_n += 1
        if len(self.skipped) < README_SKIPPED:
            self.skipped.append((_text(name, 100), why))

    def unique(self, parts: list[str]) -> str:
        parts = [_part(p) for p in parts]
        rel = "/".join(parts)
        if rel.lower() in self.used:
            stem, ext = os.path.splitext(parts[-1])
            n = 2
            while "/".join([*parts[:-1], f"{stem}-{n}{ext}"]).lower() in self.used:
                n += 1
            rel = "/".join([*parts[:-1], f"{stem}-{n}{ext}"])
        self.used.add(rel.lower())
        return rel

    def copy(self, src: Path, parts: list[str], label: Any = None) -> str | None:
        """The destination (relative to the folder) or None when the file was left out."""
        self.tick()
        if self.files >= MAX_FILES:
            self.skip(label or parts[-1], "文件太多，放不下")
            return None
        rel = self.unique(parts)
        try:
            n = _copy_capped(src, self.root / rel, min(MAX_FILE, MAX_FOLDER - self.bytes))
        except _Skip as e:
            self.used.discard(rel.lower())
            self.skip(label or parts[-1], str(e))
            return None
        except OSError:
            self.used.discard(rel.lower())
            self.skip(label or parts[-1], "没写成")
            return None
        self.bytes += n
        self.files += 1
        return rel

    def write(self, parts: list[str], data: str | bytes) -> str:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        rel = self.unique(parts)
        dst = self.root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        self.bytes += len(raw)
        self.files += 1
        return rel


# ---------------------------------------------------------------- labels

def _keys_of(text: Any) -> frozenset[str]:
    """The table / figure keys a label or a file name carries ('Table 3', 'Table_A10', 'fig7', '表 3'); coverage's rule for
    output file names, so an item and the file that is named after it meet on the same key."""
    from . import coverage
    s = str(text or "")[:300].replace("表", "Table ").replace("图", "Figure ")
    return frozenset(coverage._label_in_name(s))


def _item_key(row: dict[str, Any]) -> str:
    from . import coverage
    num = str(row.get("label") or "").strip().rsplit(None, 1)[-1] if str(row.get("label") or "").strip() else ""
    return coverage.mkey(str(row.get("kind") or "table"), num)


def _table_label_of(name: str) -> str:
    """outputs/tables/<script>-<k>-<table label>.tex → the label part ('' when the file has none)."""
    m = re.search(r"-\d+(?:-(.+))?$", os.path.splitext(name)[0])
    return (m.group(1) or "").replace("_", " ") if m else ""


def _script_stem_of_table(name: str) -> str:
    m = re.match(r"^(.+?)-\d+(?:-.*)?$", os.path.splitext(name)[0])
    return m.group(1) if m else ""


# ---------------------------------------------------------------- what goes where

@dataclass
class Built:
    """What build_folder made."""
    root: Path
    plan_id: str
    groups: dict[str, dict[str, int]]                  # group label → files under code/, data/, output/
    status: dict[str, int]                             # 已复现 / 等数据 / 尚未复现 → items
    files: int
    bytes: int
    skipped: int
    readme: str


def _index(folder: Path) -> dict[str, Path]:
    """The plain files under folder, by path relative to it (links and odd files left out; at most INDEX_FILES)."""
    from . import workspace as W
    if os.path.islink(folder) or not folder.is_dir():
        return {}
    return {f.relative_to(folder).as_posix(): f for f in islice(W.regular_files(folder), INDEX_FILES)}


def _environment(ws: Path) -> dict[str, Any]:
    """Which R (from the install step's record), the packages the scripts load with the versions the plan's own library
    holds, and the whole library; or, when R never ran for this plan, the Python this app runs on."""
    from . import rrun, rtext, workspace as W
    try:
        info = json.loads(W.safe_read(ws, "logs/rinstall.json", 2_000_000) or "{}")
    except (ValueError, W.WorkspaceError):
        info = {}
    info = info if isinstance(info, dict) else {}
    have = rrun.installed_versions(ws)
    if info.get("r") or have:
        wanted = [n for n in (info.get("wanted") or []) if rtext.valid_name(n)][:ENV_LINES] if isinstance(info.get("wanted"), list) else []
        failed = info.get("failed") if isinstance(info.get("failed"), dict) else {}
        notes = [_text(n, 200) for n in (info.get("notes") or [])[:10]] if isinstance(info.get("notes"), list) else []
        return {"kind": "r", "r": _text(info.get("r"), 20), "wanted": wanted, "have": have,
                "failed": {str(k)[:60]: _text(v, 120) for k, v in islice(failed.items(), ENV_LINES)}, "notes": notes}
    import platform
    from importlib import metadata
    pkgs = {}
    for name in PY_PACKAGES:
        try:
            pkgs[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return {"kind": "python", "python": platform.python_version(), "packages": pkgs}


def _latest_comparison(conn: sqlite3.Connection, plan_id: str) -> dict[str, Counter]:
    """table key → verdict counts, from the newest run that has a comparison with the paper's reported numbers."""
    from . import compare, workspace as W
    for run in W.runs(conn, plan_id):
        cmp = (run.get("workspace") or {}).get("comparison")
        if not isinstance(cmp, dict) or not isinstance(cmp.get("items"), list) or cmp.get("run_id") != run.get("id"):
            continue                                                       # a comparison of another run is not this run's
        out: dict[str, Counter] = {}
        for it in islice(cmp["items"], 5000):
            if not isinstance(it, dict) or it.get("verdict") not in compare.VERDICTS:
                continue
            for k in _keys_of(it.get("table")):
                out.setdefault(k, Counter())[it["verdict"]] += 1
        return out
    return {}


def _verdict_text(counts: Counter | None) -> str:
    from . import compare
    if not counts:
        return "未对照"
    return "、".join(f"{compare.LABELS[v]} {counts[v]}" for v in compare.VERDICTS if counts.get(v))


def _data_matches(blob: str, ds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The datasets whose original file name the scripts mention (one linear search each, in one lowered text)."""
    out = []
    for d in ds:
        name = str(d.get("original_name") or "").lower()
        if name and name in blob:
            out.append(d)
    return out


def _export_dataset(f: _Folder, conn: sqlite3.Connection, d: dict[str, Any], group_dir: str) -> str | None:
    """The dataset as it was imported (its original file), else as a CSV of what the library holds."""
    from . import datasets, store
    src = datasets.original_path(d)
    if src.is_file() and not src.is_symlink():
        return f.copy(src, [group_dir, "data", Path(str(d["original_name"])).name], label=d["original_name"])
    try:
        if store.parquet_path(d["id"]).stat().st_size > CSV_PARQUET_CAP:
            f.skip(d.get("original_name") or d.get("name"), "没有原始文件，转成 CSV 会太大")
            return None
        df = datasets.load(d["id"])
    except Exception:  # noqa: BLE001 - one dataset that cannot be read is listed, the folder is still made
        f.skip(d.get("original_name") or d.get("name"), "原始文件不在，数据也读不出来")
        return None
    rel = f.unique([group_dir, "data", f"{Path(str(d.get('original_name') or d.get('name') or d['id'])).stem}.csv"])
    dst = f.root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(dst, index=False)
    f.bytes += dst.stat().st_size
    f.files += 1
    return rel


def build_folder(conn: sqlite3.Connection, plan_id: str, dest: Path, *, items: list[dict[str, Any]] | None = None,
                 seconds: float | None = None) -> Built:
    """The folder of one plan at `dest` (which must not exist yet or be empty). `items` are coverage rows (coverage.items()
    shape; default: the plan's stored list); the ticked ones are exported. Nothing outside `dest` is written."""
    from . import coverage, missing, rrun, rtext, workspace as W
    from .report import tilde
    plan = plans.get(conn, plan_id)
    if plan is None:
        raise ExportError(f"没有这个复现方案：{plan_id}")
    rows_all = items if items is not None else coverage.items(conn, plan_id)
    rows = [r for r in rows_all if r.get("ticked")]
    if not rows:
        raise ExportError("清单里还没有勾选要复现的表或图：先在方案页扫描清单，勾上要放进数据文件夹的部分。")
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise ExportError("目标文件夹已经存在。")
    f = _Folder(dest, time.monotonic() + (EXPORT_SECONDS if seconds is None else seconds))
    dest.mkdir(parents=True, exist_ok=True)
    ws = rrun.ws_of(plan_id)
    author = _index(ws / "author")
    outputs = _index(ws / "outputs")
    code, _docs = coverage.author_texts(plan_id)
    r_texts = {p: t for p, t in code.items() if p.lower().endswith(".r")}
    index = rtext.FileIndex(author)
    ticked_by_group = {g: [r for r in rows if r.get("group") == g] for g in coverage.GROUPS}
    groups = [g for g in coverage.GROUPS if ticked_by_group[g]]
    dirs = {g: coverage.GROUP_LABELS[g] for g in groups}
    py_files = sorted(p for p in _index(ws / "scripts") if p.lower().endswith(".py"))[:PY_SCRIPTS]
    first = groups[0]

    # --- code
    entry: dict[str, list[str]] = {}            # group → the scripts its items name (the entry points)
    chain: dict[str, list[str]] = {}            # group → those and the scripts they source()
    for g in groups:
        e: dict[str, None] = {}
        c: dict[str, None] = {}
        for r in ticked_by_group[g]:
            for s in (r.get("scripts") or [])[:README_SCRIPTS * 3]:
                full = str(s.get("full") or "")
                if full in author:
                    e.setdefault(full)
                    for p in (rtext.source_chain(index, full, r_texts) if full in r_texts else [full]):
                        if p in author:
                            c.setdefault(p)
        entry[g], chain[g] = list(e), list(c)[:MAX_SCRIPTS]
    placed_code: dict[str, dict[str, str]] = {g: {} for g in groups}      # group → author path → destination
    n_modified: dict[str, int] = {g: 0 for g in groups}
    changes_src = outputs.get("changes.md")
    changes_text = None
    if changes_src is not None:
        raw = _read_bytes(changes_src, CHANGES_CHARS)
        changes_text = tilde(raw.decode("utf-8", errors="replace")) if raw is not None else None
    for g in groups:
        for p in chain[g]:
            text = r_texts.get(p)
            cwd = rtext.cwd_of(p, text) if text is not None else ""
            tail = p[len(cwd) + 1:] if cwd and p.startswith(cwd + "/") else p
            segs = tail.split("/")
            segs = segs[1:] if len(segs) > 1 and segs[0].lower() == "code" else segs
            rel = f.copy(author[p], [dirs[g], "code", *segs], label=p)
            if rel:
                placed_code[g][p] = rel
            ran = ws / "rrun" / p
            a, b = _read_bytes(author[p], W.AUTHOR_FILE), _read_bytes(ran, W.AUTHOR_FILE)
            if rel and b is not None and a != b:                          # what actually ran differs from the author's file
                f.copy(ran, [dirs[g], "code", "modified", *segs], label=p)
                n_modified[g] += 1
        if changes_text is not None:
            f.write([dirs[g], "code", "changes.md"], changes_text)
    py_group = first if py_files else ""
    for p in py_files:
        f.copy(ws / "scripts" / p, [dirs[first], "code", "python", *p.split("/")], label=p)

    # --- data
    ds_all = W.plan_datasets(conn, plan)[:200]
    claimed: set[str] = set()
    placed_data: dict[str, list[str]] = {g: [] for g in groups}
    for g in groups:
        blob = "\n".join(code[p] for p in chain[g] if p in code)[:TEXT_BLOB].lower()
        for d in _data_matches(blob, ds_all):
            claimed.add(d["id"])
            rel = _export_dataset(f, conn, d, dirs[g])
            if rel:
                placed_data[g].append(rel)
    if py_group:
        for d in ds_all:
            if d["id"] not in claimed:
                rel = _export_dataset(f, conn, d, dirs[py_group])
                if rel:
                    placed_data[py_group].append(rel)

    # --- output
    keys_of_group = {g: {_item_key(r) for r in ticked_by_group[g]} for g in groups}
    all_keys = {_item_key(r) for r in rows_all}
    out_bases: dict[str, set[str]] = {}                                    # output file name an item names → its groups
    for r in rows_all:
        for o in (r.get("outputs") or [])[:20]:
            out_bases.setdefault(posixpath.basename(str(o)).lower(), set()).add(r.get("group") if r.get("ticked") else "")
    scripts_of_group = {g: set(chain[g]) for g in groups}
    stems_of_group = {g: {posixpath.splitext(posixpath.basename(p))[0] for p in chain[g]} for g in groups}
    produced: dict[str, list[tuple[str, str]]] | None = None
    placed_out: dict[str, list[tuple[str, frozenset[str]]]] = {g: [] for g in groups}    # group → (dest, keys)
    loose: list[str] = []
    left_out = 0
    for rel, src in sorted(outputs.items()):
        base = posixpath.basename(rel)
        if rel in SKIP_OUTPUT_NAMES or base.startswith(".") or re.fullmatch(r"Rplots.*\.pdf", base, re.I):
            continue
        is_table = rel.startswith("tables/")
        keys = _keys_of(_table_label_of(base) if is_table else posixpath.splitext(base)[0])
        hit = {g for g in groups if keys & keys_of_group[g]} | {g for g in out_bases.get(base.lower(), ()) if g in keys_of_group}
        if not hit and (keys & all_keys or (out_bases.get(base.lower()) and not out_bases[base.lower()] - {""})):
            left_out += 1                                                 # it belongs to an item nobody ticked
            continue
        if not hit and is_table:
            stem = _script_stem_of_table(base)
            hit = {g for g in groups if stem and stem in stems_of_group[g]}
        if not hit:
            if produced is None:
                try:
                    produced = coverage.gather_evidence({p: code[p] for p in sorted(set().union(*scripts_of_group.values())) if p in code}, {},
                                                        deadline=time.monotonic() + 20).produced
                except coverage.CoverageError:
                    produced = {}
            made = {s for s, _file in produced.get(base.lower(), [])}
            hit = {g for g in groups if made & scripts_of_group[g]}
        if not hit and py_group and not entry[py_group] and not any(entry.values()):
            hit = {py_group}
        if not hit:
            loose.append(rel)
            continue
        if sum(len(v) for v in placed_out.values()) >= MAX_OUTPUTS:
            f.skip(rel, "输出文件太多")
            continue
        for g in sorted(hit, key=groups.index):
            placed = f.copy(src, [dirs[g], "output", *rel.split("/")], label=rel)
            if placed:
                placed_out[g].append((placed, keys))
    if loose:
        for rel in loose[:MAX_OUTPUTS]:
            f.copy(outputs[rel], ["其他输出", *rel.split("/")], label=rel)
    # results.json slice per group: the tables whose label is one of the group's ticked items
    results, _why = W._load(ws, W.RESULTS)
    tables = [t for t in (results.get("tables") if isinstance(results, dict) and isinstance(results.get("tables"), list) else [])
              if isinstance(t, dict)]
    slice_keys: dict[str, set[str]] = {}
    for g in groups:
        mine = [t for t in tables if _keys_of(t.get("label")) & keys_of_group[g]]
        if mine:
            rel = f.write([dirs[g], "output", "results.json"], json.dumps({"tables": mine}, ensure_ascii=False, indent=1))
            slice_keys[g] = set().union(*(_keys_of(t.get("label")) for t in mine))
            placed_out[g].append((rel, frozenset(slice_keys[g])))

    # --- what each item stands on
    cmp = _latest_comparison(conn, plan_id)
    try:
        gaps = missing.report(conn, plan_id, missing.ALL, limit=missing.MAX_ROWS)
    except missing.MissingError:
        gaps = None
    waiting = gaps.waiting if gaps else {}
    resolved: dict[str, list[dict[str, Any]]] = {g: [] for g in groups}
    status = Counter()
    for g in groups:
        for r in ticked_by_group[g]:
            key = _item_key(r)
            bases = {posixpath.basename(str(o)).lower() for o in (r.get("outputs") or [])[:20]}
            outs = [dst for dst, keys in placed_out[g] if key in keys or posixpath.basename(dst).lower() in bases]
            saved = [str(s["full"]) for s in (r.get("scripts") or []) if str(s.get("full") or "") in author]
            unsaved = [str(s.get("path") or "") for s in (r.get("scripts") or []) if str(s.get("full") or "") not in author]
            wait_for = list(dict.fromkeys(n for s in saved for n in waiting.get(s, [])))
            state = "done" if outs else ("waiting" if wait_for else "todo")
            if state == "todo":
                why = ("作者的 README 提到的脚本没有保存在工作区" if unsaved and not saved else
                       "脚本在，但还没有跑出这张表或图（没跑、报错或没跑完）" if saved else "没有找到对应的作者脚本")
            else:
                why = ""
            status[state] += 1
            resolved[g].append({"row": r, "key": key, "outs": outs, "state": state, "wait": wait_for, "why": why,
                                "scripts": [placed_code[g].get(s) or s for s in saved], "unsaved": unsaved,
                                "verdict": _verdict_text(cmp.get(key)) if outs else "—"})

    counts = {dirs[g]: {sub: sum(1 for u in f.used if u.startswith(f"{dirs[g].lower()}/{sub}/")) for sub in ("code", "data", "output")}
              for g in groups}
    env = _environment(ws)
    if env["kind"] == "r":
        lines = [f"R {env['r'] or '（版本没有记录）'}", *[f"{n}=={v}" for n, v in islice(sorted(env["have"].items()), ENV_LINES)]]
        f.write(["R-packages.txt"], "\n".join(lines) + "\n")
    readme = tilde(_readme(plan, plan_id, groups, dirs, resolved, entry, placed_code, n_modified, counts, py_files, env, gaps, f,
                           n_unticked=len(rows_all) - len(rows), loose=len(loose), left_out=left_out,
                           changes=changes_text is not None, write_dirs=_write_dirs(chain, r_texts, groups)))
    f.write(["README.md"], readme)
    return Built(dest, plan_id, counts, {"已复现": status["done"], "等数据": status["waiting"], "尚未复现": status["todo"]},
                 f.files, f.bytes, f.skipped_n, readme)


def _write_dirs(chain: dict[str, list[str]], r_texts: dict[str, str], groups: list[str]) -> dict[str, dict[str, Any]]:
    """The folders (relative to the group folder) a group's R scripts save into: R does not create them. The paths are the
    authors' string literals, so each goes through _text and only plain ones (letters, digits, _ . / space -) are kept:
    {"cmd": those a mkdir line may carry, "named": those with a space (named, never put in a command), "odd": how many were
    left out for their characters}."""
    from . import rtext
    out: dict[str, dict[str, Any]] = {}
    for g in groups:
        found: dict[str, None] = {}
        for p in chain[g]:
            if p in r_texts:
                for d in rtext.write_dirs(r_texts[p]):
                    found.setdefault(d.removeprefix("./"))
        cmd: list[str] = []
        named: list[str] = []
        odd = 0
        for d in found:
            if not d or d.split("/")[0].lower() in ("data", "code"):
                continue
            clean = _text(d, 100)
            if clean != d or not _NAME_OK.fullmatch(clean) or ".." in clean.split("/") or clean.startswith("-"):
                odd += 1
            elif _cmd_path(clean):
                cmd.append(clean)
            else:
                named.append(clean)
        out[g] = {"cmd": cmd[:6], "named": named[:6], "odd": odd + max(0, len(cmd) - 6) + max(0, len(named) - 6)}
    return out


def _readme(plan: dict[str, Any], plan_id: str, groups: list[str], dirs: dict[str, str], resolved: dict[str, list[dict[str, Any]]],
            entry: dict[str, list[str]], placed_code: dict[str, dict[str, str]], n_modified: dict[str, int], counts: dict[str, dict[str, int]], py_files: list[str],
            env: dict[str, Any], gaps: Any, f: _Folder, *, n_unticked: int, loose: int, left_out: int, changes: bool,
            write_dirs: dict[str, dict[str, Any]]) -> str:
    from . import coverage
    title = _text(plan.get("paper_title") or plan_id, 200)
    year = f"（{plan['paper_year']}）" if plan.get("paper_year") else ""
    every = [x for g in groups for x in resolved[g]]
    n_done = sum(1 for x in every if x["state"] == "done")
    n_wait = sum(1 for x in every if x["state"] == "waiting")
    n_todo = len(every) - n_done - n_wait
    out = [f"# 复现数据文件夹：{title}", "",
           f"- 论文：{title}{year}",
           f"- 方案：{plan_id}",
           f"- 导出时间：{time.strftime('%Y-%m-%d %H:%M')}，由 Research Lab 生成",
           f"- 勾选了 {len(every)} 张表或图（" + "、".join(f"{coverage.GROUP_LABELS[g]} {len(resolved[g])}" for g in groups)
           + f"）：已复现 {n_done}，等数据 {n_wait}，尚未复现 {n_todo}。"
           + (f"另有 {n_unticked} 项没有勾选，不在这个文件夹里。" if n_unticked else ""), "",
           "## 文件夹里有什么", ""]
    for g in groups:
        c = counts[dirs[g]]
        out.append(f"- `{dirs[g]}/`：`code/` 作者的原脚本（{c['code']} 个文件" + (f"，其中 `code/modified/` 是为了跑通改过的 {n_modified[g]} 个副本" if n_modified[g] else "")
                   + ("，`code/changes.md` 写了改动" if changes else "") + f"）；`data/` 脚本读的数据（{c['data']} 个文件）；`output/` 复现出的表和图（{c['output']} 个文件）")
    out += ["- `README.md`：这份说明"]
    if env["kind"] == "r":
        out.append("- `R-packages.txt`：这份复现用的 R 库里每个包的版本")
    if py_files:
        out.append(f"- `{dirs[groups[0]]}/code/python/`：AI 翻译成 Python 的脚本（{len(py_files)} 个），整份方案共用，所以只放在第一组里")
    if loose:
        out.append(f"- `其他输出/`：{loose} 个跑出来、但对不上任何一组的文件")
    out.append("")
    out += ["## 每张表和图：脚本、输出、判定", "",
            "判定来自这份方案最近一次和论文报告值的逐项对照；没有对照过的写「未对照」。", ""]
    shown = 0
    for g in groups:
        out += [f"### {coverage.GROUP_LABELS[g]}", "", "| 表或图 | 标题 | 脚本 | 输出文件 | 判定 | 状态 |", "|---|---|---|---|---|---|"]
        for x in resolved[g]:
            if shown >= README_ROWS:
                break
            shown += 1
            r = x["row"]
            scripts = [f"`{_td(s, 80)}`" for s in (x["scripts"] + [f"{u}（未保存）" for u in x["unsaved"]])[:README_SCRIPTS]]
            more_s = len(x["scripts"]) + len(x["unsaved"]) - len(scripts)
            outs = [f"`{_td(o, 80)}`" for o in x["outs"][:README_OUTPUTS]]
            more_o = len(x["outs"]) - len(outs)
            label = {"done": "已复现", "waiting": "等数据", "todo": "尚未复现"}[x["state"]]
            out.append(f"| {_td(r.get('label'), 40)} | {_td(r.get('caption'), 90)} | "
                       + (" ".join(scripts) + (f" 等 {len(scripts) + more_s} 个" if more_s > 0 else "") or "—") + " | "
                       + (" ".join(outs) + (f" 等 {len(outs) + more_o} 个" if more_o > 0 else "") or "—")
                       + f" | {_td(x['verdict'], 80)} | {label} |")
        out.append("")
    todo = [x for x in every if x["state"] != "done"]
    out += ["## 还没有复现的", ""]
    if not todo:
        out += ["勾选的都有输出了。", ""]
    for state, head in (("waiting", "等数据"), ("todo", "尚未复现")):
        mine = [x for x in todo if x["state"] == state]
        if not mine:
            continue
        out += [f"### {head}（{len(mine)}）", ""]
        for x in mine[:README_STATUS]:
            r = x["row"]
            detail = ("等 " + "、".join(f"`{_td(n, 60)}`" for n in x["wait"][:4]) + (" 等" if len(x["wait"]) > 4 else "")) if state == "waiting" else x["why"]
            out.append(f"- {_td(r.get('label'), 40)}（{_td(r.get('caption'), 70)}）：{detail}")
        if len(mine) > README_STATUS:
            out.append(f"- 另有 {len(mine) - README_STATUS} 项未列出")
        out.append("")
    if gaps is not None and gaps.items:
        wanted = [i for i in gaps.items if not i.filled]
        if wanted:
            out += [f"## 待补数据文件（{gaps.total_missing}{'+' if gaps.truncated else ''}）", "",
                    "作者的脚本读了、但这个文件夹里没有的数据。补上后，用到它的结果才能跑出来。", "",
                    "| 文件 | 哪些脚本要用 | 为什么没有 | 可能从哪里拿 |", "|---|---|---|---|"]
            for i in wanted[:README_MISSING]:
                out.append(f"| `{_td(i.key, 80)}` | {' '.join(f'`{_td(s, 50)}`' for s in i.scripts[:3]) or '—'} | {_td(i.reason, 90)} | {_td(i.hint, 90)} |")
            rest = gaps.total_missing - min(len(wanted), README_MISSING)
            if rest > 0:
                out.append(f"\n另有 {rest} 个文件未列出。")
            out.append("")
    out += ["## 环境", ""]
    if env["kind"] == "r":
        out += [f"R 版本：{env['r'] or '没有记录'}。脚本里加载的包，和这份复现用的 R 库里装的版本（整个库在 `R-packages.txt`）：", ""]
        if env["wanted"]:
            out += ["| 包 | 版本 |", "|---|---|"]
            for n in env["wanted"][:ENV_LINES]:
                v = env["have"].get(n)
                out.append(f"| {n} | " + (v if v else "没装上" + (f"：{env['failed'][n]}" if n in env["failed"] else "")).replace("|", "¦") + " |")
        else:
            out.append("（安装记录里没有列出脚本要用的包。）")
        for n in env["notes"]:
            out += ["", f"注意：{n}"]
    else:
        out += [f"这份方案没有用 R 跑过，复现用的是 Python {env['python']}。主要的包：" + ("、".join(f"{k} {v}" for k, v in env["packages"].items()) or "（没有读到版本）") + "。"]
    out += ["", "## 怎么重跑", ""]
    runnable = [(g, s, placed_code[g][s]) for g in groups for s in entry[g] if s in placed_code[g]]
    if env["kind"] == "r" and runnable:
        first, _script, dest = runnable[0]
        script_cmd = _cmd_path(dest.removeprefix(dirs[first] + "/"))
        out += ["**用 R，在某一组的文件夹里**（里面有 `code/` 和 `data/`）：", "",
                "1. 装好上面这个版本的 R 和表里的包；",
                "2. 打开终端，进入那一组的文件夹，再运行脚本。作者的脚本读的是 `./data/…`，所以要在组文件夹里运行，不是在 `code/` 里：", "",
                "```", f"cd {dirs[first]}", f"Rscript {script_cmd}" if script_cmd else NO_CMD, "```", ""]
        wd = write_dirs.get(first) or {"cmd": [], "named": [], "odd": 0}
        shown = [*wd["cmd"], *wd["named"]]
        if shown:
            out += ["脚本会往这些文件夹里存图和表，R 不会自己建，要先建好：" + "、".join(f"`{d}/`" for d in shown)
                    + (f"（例如 `mkdir -p {' '.join(wd['cmd'])}`）" if wd["cmd"] else "") + "。", ""]
        if wd["odd"]:
            out += [f"另有 {wd['odd']} 个存图文件夹的名字含特殊字符，没有列出，请看脚本里存图的路径。", ""]
        if wd["named"]:
            out += ["名字里有空格的文件夹没有放进命令，请手动建。", ""]
        if len(runnable) > 1:
            out += ["各组的入口脚本（都在组文件夹里这样运行）：", ""]
            for g in groups:
                mine = [d.removeprefix(dirs[g] + "/") for gg, _s, d in runnable if gg == g]
                if mine:
                    out.append(f"- `{dirs[g]}/`：" + "、".join(f"`{d}`" if _cmd_path(d) else NO_CMD for d in mine[:README_SCRIPTS])
                               + (f" 等 {len(mine)} 个" if len(mine) > README_SCRIPTS else ""))
            out.append("")
        if any(n_modified.values()):
            out += ["`code/modified/` 里是为了在这台电脑上跑通而改过的副本（只动路径、没装上的包这类地方）。要用它，把它覆盖到 `code/` 里同名的文件上再运行；改了什么见 `code/changes.md`。", ""]
        out += ["**在 Research Lab 里**：打开这份方案的复现工作区，先点 Install R packages（联网装包），再点 Run author code (R)。没有报错时不调用 AI。", ""]
    elif py_files:
        out += ["脚本是 AI 在 Research Lab 的复现工作区里写的，读的是工作区自己的 `data/`。要重跑，打开这份方案的复现工作区，点运行记录里的 Re-run（不调用 AI）。", ""]
    else:
        out += ["这份方案还没有可以重跑的脚本。", ""]
    if f.skipped_n or left_out:
        out += ["## 没有放进来的文件", ""]
        for name, why in f.skipped:
            out.append(f"- `{name}`：{why}")
        if f.skipped_n > len(f.skipped):
            out.append(f"- 另有 {f.skipped_n - len(f.skipped)} 个未列出")
        if left_out:
            out.append(f"- 属于没勾选的表或图的输出 {left_out} 个，没有放进来")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- the zip, the 课题 and the issue

@dataclass
class Target:
    """Where the zip goes: a 课题's drafts/ folder, and the issue it is attached to (none: only saved)."""
    slug: str
    project_name: str
    issue_ref: str = ""
    issue_key: str = ""
    why: str = ""                    # why no issue was chosen


@dataclass
class Delivered:
    rel: str                         # drafts/<name>
    project: str
    project_name: str
    issue_key: str
    attached: bool
    why: str
    built: Built
    size: int

    @property
    def message(self) -> str:
        where = f"已导出 {self.rel.split('/', 1)[1]}（{_mb(self.size)}），放进了课题「{self.project_name}」的草稿文件夹"
        if self.attached:
            return f"{where}，并挂到了议题 {self.issue_key}。"
        return f"{where}。没有挂到议题：{self.why}"


def _projects() -> dict[str, dict[str, Any]]:
    from ..files import datafiles
    return {str(p["slug"]): p for p in datafiles.read("projects") or [] if isinstance(p, dict) and p.get("slug")}


def pick_target(conn: sqlite3.Connection, plan: dict[str, Any]) -> Target:
    """The 课题 and the issue for a plan's zip. The issue is the one that names the plan's paper as material (issues.yml
    `paper:<id>`); with several, the one in the plan's research direction, then in the paper's own 课题; if that still leaves
    none or more than one, no issue is chosen and the zip is only saved. The 课题 is the issue's (an attachment lives in
    its issue's 课题), else the paper's; no 课题 at all is refused."""
    from ..files import issues as I
    projects = _projects()
    paper = plan.get("paper_id")
    if not paper:
        raise ExportError("这份方案没有关联论文，不知道该放进哪个课题的草稿文件夹。")
    mine = [r[0] for r in conn.execute("SELECT project_slug FROM paper_projects WHERE paper_id = ? ORDER BY project_slug", (paper,))
            if r[0] in projects]
    by_ref = {r["id"]: r for r in I.read()}
    cands = [by_ref[x["id"]] for x in I.paper_issues(paper) if x["id"] in by_ref and not by_ref[x["id"]].get("archived")
             and by_ref[x["id"]].get("project") in projects]
    direction = str(plan.get("direction") or "")
    why = ""
    chosen: dict[str, Any] | None = None
    if len(cands) == 1:
        chosen = cands[0]
    elif len(cands) > 1:
        in_dir = [c for c in cands if direction and c.get("direction") == direction]
        if len(in_dir) > 1:
            in_dir = [c for c in in_dir if c.get("project") in mine] or in_dir
        if len(in_dir) == 1:
            chosen = in_dir[0]
        elif not in_dir:
            why = f"有 {len(cands)} 个议题把这篇论文列为材料，但没有一个在这份方案的研究方向里。"
        else:
            why = f"有 {len(in_dir)} 个议题把这篇论文列为材料，分不出该挂哪一个。"
    else:
        why = "没有议题把这篇论文列为材料。"
    if chosen is not None:
        slug = str(chosen["project"])
        return Target(slug, str(projects[slug].get("name") or slug), chosen["id"], str(chosen.get("key") or chosen.get("title") or ""))
    if not mine:
        raise ExportError("这篇论文还没有加入任何课题，先把它加入一个课题再导出。")
    in_dir = [s for s in mine if direction and projects[s].get("direction") == direction]
    slug = (in_dir or mine)[0]
    return Target(slug, str(projects[slug].get("name") or slug), why=why)


def preview(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    """What the page's Export folder card says before the click (reads only): the ticked items per group, where the zip
    would go and whether an issue would get it."""
    from . import coverage
    plan = plans.get(conn, plan_id)
    rows = [r for r in coverage.items(conn, plan_id) if r["ticked"]]
    groups = [(coverage.GROUP_LABELS[g], n) for g in coverage.GROUPS if (n := sum(1 for r in rows if r["group"] == g))]
    info: dict[str, Any] = {"ticked": len(rows), "groups": groups, "project": "", "issue_key": "", "note": "", "ok": False}
    try:
        t = pick_target(conn, plan or {})
    except ExportError as e:
        info["note"] = str(e)
        return info
    except Exception:  # noqa: BLE001 - the card still draws, without a destination
        log.exception("export preview for %s", plan_id)
        info["note"] = "暂时读不到这篇论文所在的课题和议题。"
        return info
    info.update(project=t.project_name, issue_key=t.issue_key, note=t.why, ok=bool(rows))
    return info


def zip_folder(folder: Path, zip_path: Path, cap: int | None = None) -> int:
    """folder → zip_path under `<folder name>/…`: plain files only (each opened O_NOFOLLOW), a size cap on the zip itself that is
    checked after every megabyte written, so a member that would burst it stops the zip there, not after it was all written. A
    refused zip is removed. Returns its size."""
    from . import workspace as W
    cap = ZIP_CAP if cap is None else cap
    root = folder.name
    too_big = ExportError(f"压缩后超过 {cap // (1024 * 1024)} MB（课题文件夹收的上限）：勾少一点，或先去掉很大的数据文件。")
    try:
        with zipfile.ZipFile(zip_path, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for f in W.regular_files(folder):
                rel = f.relative_to(folder).as_posix()
                if rel.startswith("/") or ".." in rel.split("/"):
                    continue
                info = zipfile.ZipInfo(f"{root}/{rel}", time.localtime(f.stat().st_mtime)[:6])
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                try:
                    fd = os.open(f, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                except OSError:
                    continue
                with os.fdopen(fd, "rb") as src:
                    if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
                        continue
                    with zf.open(info, "w") as dst:
                        while chunk := src.read(1 << 20):
                            dst.write(chunk)
                            if zf.fp.tell() > cap:
                                raise too_big
                if zf.fp.tell() > cap:
                    raise too_big
        return zip_path.stat().st_size
    except BaseException:
        zip_path.unlink(missing_ok=True)
        raise


def _store(slug: str, zip_path: Path, name: str) -> str:
    """Put the zip into the 课题's drafts/ the way an upload goes there: a cleaned name, never over an existing file, then checked
    to sit inside the folder. It is copied to a hidden temporary name in the same folder and linked into place, which fails if the
    name is taken and leaves no half-written zip under the real name; the temporary name is always removed. Returns drafts/<name>."""
    from ..files import deliverables
    d = deliverables.folder(slug, "drafts")
    d.mkdir(parents=True, exist_ok=True)
    clean = deliverables.clean_name(name)
    stem, ext = os.path.splitext(clean)
    tmp = d / f".{secrets.token_hex(6)}.part"
    try:
        with open(zip_path, "rb") as src, open(tmp, "xb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        n = 1
        while True:
            target = d / (clean if n == 1 else f"{stem}-{n}{ext}")
            try:
                os.link(tmp, target)
                break
            except FileExistsError:
                n += 1
    finally:
        tmp.unlink(missing_ok=True)
    deliverables.resolve(slug, f"drafts/{target.name}")
    return f"drafts/{target.name}"


def export_folder(conn: sqlite3.Connection, plan_id: str, *, seconds: float | None = None) -> Delivered:
    """Leo's click: build the folder in a scratch directory, zip it, save the zip in the paper's 课题 drafts/ and attach it to
    the issue that names the paper. Raises ExportError (in Chinese) before anything is saved when it cannot."""
    from . import rrun, workspace as W
    from ..config import get_paths
    from ..files import issues as I
    from ..files.yamlio import StaleFileError
    plan = plans.get(conn, plan_id)
    if plan is None:
        raise ExportError(f"没有这个复现方案：{plan_id}")
    if W.current_job(conn, plan_id) is not None or rrun.busy(plan_id):
        raise ExportError("这份方案正在运行，等它结束再导出。")
    target = pick_target(conn, plan)
    if not _LOCK.acquire(blocking=False):
        raise ExportError("正在导出，请稍等。")
    scratch = None
    try:
        cache = get_paths().cache
        cache.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="rl-export-", dir=cache))
        name = f"{plan_id}{FOLDER_SUFFIX}"
        built = build_folder(conn, plan_id, scratch / name, seconds=seconds)
        zip_path = scratch / f"{name}.zip"
        size = zip_folder(built.root, zip_path)
        rel = _store(target.slug, zip_path, f"{name}.zip")
        attached, why = False, target.why
        if target.issue_ref:
            try:
                I.attach_existing(target.issue_ref, rel, I.revision(target.issue_ref))
                attached, why = True, ""
            except (*I.ERRORS, StaleFileError) as e:
                why = f"{e}"
        return Delivered(rel, target.slug, target.project_name, target.issue_key, attached, why, built, size)
    finally:
        _LOCK.release()
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
