"""Deliverables of a 课题: the Word / PDF / other files Leo edits outside the app.

Two folders per 课题: `projects/<slug>/drafts/` (in git, exported and uploaded drafts) and
`private/<slug>/` (never leaves this machine: full manuscripts, supervisor feedback). The app lists
them, opens them with the system's default application, accepts uploads into drafts/, and exports an
output's Markdown to .docx with macOS's textutil (no pandoc needed). Every path the web layer touches
goes through resolve(), which refuses anything outside those two folders.
"""
from __future__ import annotations

import html
import os
import re
import zipfile
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt

from ..config import Paths, get_paths
from . import artifacts

FOLDERS = (("drafts", "草稿"), ("private", "私有"))
KIND = {".docx": "Word", ".doc": "Word", ".pages": "Pages", ".pdf": "PDF", ".pptx": "幻灯片", ".key": "幻灯片",
        ".xlsx": "表格", ".csv": "表格", ".md": "Markdown", ".txt": "文本", ".tex": "LaTeX", ".rtf": "RTF", ".odt": "文档",
        ".html": "网页", ".htm": "网页"}
_SAFE_NAME = re.compile(r"[\\/:*?\"<>|\x00-\x1f]+")
# Direction overrides and zero-width characters: a right-to-left override can make "evil<RLO>fdp.exe" read as
# "evilexe.pdf" in Finder.
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
# `open` would execute these; they only get 在访达中显示.
EXECUTABLE = {".app", ".command", ".sh", ".zsh", ".bash", ".py", ".pl", ".rb", ".scpt", ".workflow", ".pkg", ".dmg", ".jar", ".exe", ".bat"}
MAX_UPLOAD = 200 * 1024 * 1024


class DeliverableError(ValueError):
    pass


def folder(slug: str, key: str, paths: Paths | None = None) -> Path:
    p = paths or get_paths()
    return p.projects / slug / "drafts" if key == "drafts" else p.root / "private" / slug


def list_files(slug: str, paths: Paths | None = None) -> list[dict[str, Any]]:
    """Newest first; hidden files and .gitkeep skipped. `rel` is "<folder>/<name>", the handle the routes use."""
    out = []
    for key, label in FOLDERS:
        d = folder(slug, key, paths)
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if not f.is_file() or f.name.startswith(".") or f.name == "README.md":
                continue
            st = f.stat()
            out.append({"name": f.name, "rel": f"{key}/{f.name}", "folder": key, "folder_label": label,
                        "kind": KIND.get(f.suffix.lower(), f.suffix.lstrip(".").upper() or "文件"),
                        "size": st.st_size, "mtime": datetime.fromtimestamp(st.st_mtime)})
    return sorted(out, key=lambda x: x["mtime"], reverse=True)


def resolve(slug: str, rel: str, paths: Paths | None = None) -> Path:
    """The file behind a "<folder>/<name>" handle, or DeliverableError for anything outside the two folders."""
    key, _, name = (rel or "").partition("/")
    if key not in dict(FOLDERS) or not name or name != _SAFE_NAME.sub("", name) or name in (".", ".."):
        raise DeliverableError("不是课题文件夹里的文件")
    base = folder(slug, key, paths).resolve()
    target = (base / name).resolve()
    if target.parent != base or not target.is_file():
        raise DeliverableError("不是课题文件夹里的文件")
    return target


def open_with_system(path: Path, reveal: bool = False) -> None:
    """`open` / `open -R`; executables are only ever revealed; RL_OPEN_FILES=0 (tests) makes it a no-op."""
    if path.suffix.lower() in EXECUTABLE:
        reveal = True
    if os.environ.get("RL_OPEN_FILES", "1") == "0":
        return
    subprocess.run(["open", "-R", str(path)] if reveal else ["open", str(path)], check=False)


def unique(path: Path) -> Path:
    """name.docx, name-2.docx, name-3.docx … never overwrite what Leo may have edited."""
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def clean_name(filename: str) -> str:
    """The bare file name of an upload: no invisible direction or zero-width characters, no folders (a browser may send "C:\\x\\a.pdf" or "../a.pdf"), no
    characters a path or Finder trips over, no leading dot, at most 120 characters before the suffix."""
    raw = _INVISIBLE.sub("", str(filename or "")).replace("\\", "/")
    name = _SAFE_NAME.sub("", Path(raw).name).strip().lstrip(".").strip()
    stem, suffix = os.path.splitext(name)
    if len(stem) > 120:
        name = stem[:120].rstrip() + suffix
    if not name or name in (".", ".."):
        raise DeliverableError("文件名不能为空")
    return name


def save_upload(slug: str, filename: str, data: bytes, paths: Paths | None = None, *, folder_key: str = "drafts") -> Path:
    """Write an upload into the 课题's drafts/ (default) or private/ folder under a cleaned, unused name."""
    name = clean_name(filename)
    if len(data) > MAX_UPLOAD:
        raise DeliverableError("文件超过 200 MB")
    if folder_key not in dict(FOLDERS):
        raise DeliverableError("不是课题的文件夹")
    d = folder(slug, folder_key, paths)
    d.mkdir(parents=True, exist_ok=True)
    # Exclusive creation closes the race between the issue and project upload routes.
    index = 1
    while True:
        target = d / name if index == 1 else d / f'{Path(name).stem}-{index}{Path(name).suffix}'
        try:
            with target.open('xb') as stream:
                stream.write(data)
            return target
        except FileExistsError:
            index += 1


def export_docx(a: artifacts.Artifact, slug: str, paths: Paths | None = None) -> Path:
    """Markdown → HTML → .docx through textutil (ships with macOS). AI notes and app-managed blocks are
    left out (artifacts.editable_body); the title becomes the document heading."""
    if not shutil.which("textutil"):
        raise DeliverableError("导出 Word 需要 macOS 自带的 textutil")
    title = str(a.get("title") or a.ref or "untitled")
    body = artifacts.editable_body(a.body)
    rendered = MarkdownIt("commonmark", {"html": True}).enable("table").render(body)
    safe_title = html.escape(title)
    doc = (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{safe_title}</title></head>'
           f'<body><h1>{safe_title}</h1>{rendered}</body></html>')
    d = folder(slug, "drafts", paths)
    d.mkdir(parents=True, exist_ok=True)
    target = unique(d / f"{_SAFE_NAME.sub('', title).strip()[:80] or a.ref}.docx")
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as tmp:
        tmp.write(doc)
    try:
        r = subprocess.run(["textutil", "-convert", "docx", "-output", str(target), tmp.name], capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(tmp.name)
    if r.returncode != 0 or not target.exists():
        raise DeliverableError("textutil 转换失败：" + (r.stderr.strip() or "未知原因"))
    return target


# ---------------------------------------------------------------- a Word file to write in

def outline_docx(slug: str, title: str, sections: list[str], paths: Paths | None = None) -> Path | None:
    """An empty Word file with Leo's title and section headings, for writing in. An existing file of that
    name is returned as it is (never overwritten). None without textutil."""
    d = folder(slug, "private", paths)
    target = d / f"{_SAFE_NAME.sub('', title).strip()[:80] or slug}.docx"
    if target.exists():
        return target
    if not shutil.which("textutil"):
        return None
    heads = "".join(f"<h2>{html.escape(s)}</h2><p></p>" for s in sections)
    doc = (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{html.escape(title)}</title></head>'
           f'<body><h1>{html.escape(title)}</h1>{heads}</body></html>')
    d.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as tmp:
        tmp.write(doc)
    try:
        r = subprocess.run(["textutil", "-convert", "docx", "-output", str(target), tmp.name], capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(tmp.name)
    return target if r.returncode == 0 and target.exists() else None


# ---- word counts ---------------------------------------------------------------------------------

_CJK = re.compile(r"[\u3400-\u9fff]")
_WORD = re.compile(r"[A-Za-z0-9\u00C0-\u024F][A-Za-z0-9\u00C0-\u024F'\u2019-]*")
_WT = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.S)
_TAG = re.compile(r"<[^>]+>")
_count_cache: dict[str, tuple[float, int]] = {}


def count_text(text: str) -> int:
    """中文字 + 英文词, the same rule as the editor's 约 N 字 (CJK characters count one each)."""
    return len(_CJK.findall(text)) + len(_WORD.findall(text))


def docx_text(path: Path) -> str:
    """The paragraph text of a .docx: every <w:t> run of word/document.xml, paragraphs separated by newlines."""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
    xml = xml.replace("</w:p>", "\n")
    return html.unescape("".join(_WT.findall(xml)) if "<w:t" in xml else _TAG.sub("", xml))


def word_count(path: Path) -> int | None:
    """Actual word count of a deliverable: .docx through its XML, Markdown / text as is, other kinds
    unknown (None). Cached per path by mtime so the home page does not reopen the file every request."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    hit = _count_cache.get(key)
    if hit and hit[0] == st.st_mtime:
        return hit[1]
    suffix = path.suffix.lower()
    try:
        if suffix == ".docx":
            n = count_text(docx_text(path))
        elif suffix in (".md", ".txt", ".tex", ".markdown"):
            n = count_text(path.read_text(encoding="utf-8", errors="ignore"))
        else:
            return None
    except (OSError, zipfile.BadZipFile, KeyError):
        return None
    _count_cache[key] = (st.st_mtime, n)
    return n
