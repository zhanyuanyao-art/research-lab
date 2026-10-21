"""Leo's own writing (Word, Markdown, plain text) → Markdown, so the assistant can file it into outputs.

`.docx` is read with the standard library only (it is a zip of XML): headings, paragraphs, lists,
tables and footnotes survive; images, comments and tracked-change history do not. Older formats
(.doc, .rtf, .odt) go through macOS `textutil` when it exists. pandoc is not required.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
TEXT_SUFFIXES = (".md", ".markdown", ".txt")
TEXTUTIL_SUFFIXES = (".doc", ".rtf", ".odt")
SUFFIXES = (".docx", *TEXT_SUFFIXES, *TEXTUTIL_SUFFIXES)
_HEADING_NAME = re.compile(r"^(?:heading|标题)\s*([1-6])$", re.I)


class ConvertError(ValueError):
    """The file cannot be read as text; the message is shown to Leo."""


def supported(name: str) -> bool:
    return Path(name).suffix.lower() in SUFFIXES


def to_markdown(path: Path, name: str | None = None) -> str:
    suffix = Path(name or path.name).suffix.lower()
    if suffix == ".docx":
        return docx_to_markdown(path)
    if suffix in TEXT_SUFFIXES:
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "gb18030"):
            try:
                return _tidy(raw.decode(enc))
            except UnicodeDecodeError:
                continue
        raise ConvertError("文件不是 UTF-8 或 GBK 编码的文本")
    if suffix in TEXTUTIL_SUFFIXES:
        return _via_textutil(path, suffix)
    raise ConvertError(f"不支持 {suffix or '这种'} 文件；请用 .docx、.md 或 .txt")


def _via_textutil(path: Path, suffix: str) -> str:
    tool = shutil.which("textutil")
    if not tool:
        raise ConvertError(f"本机读不了 {suffix}；请在 Word 里另存为 .docx")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"in{suffix}"
        shutil.copyfile(path, src)
        out = Path(tmp) / "out.txt"
        r = subprocess.run([tool, "-convert", "txt", "-encoding", "UTF-8", "-output", str(out), str(src)],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not out.exists():
            raise ConvertError(f"读不了这个 {suffix} 文件；请在 Word 里另存为 .docx")
        return _tidy(out.read_text(encoding="utf-8", errors="replace"))


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


# ---------------------------------------------------------------- docx

def docx_to_markdown(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "word/document.xml" not in names:
                raise ConvertError("这不是一个 Word 文档")
            doc = ET.fromstring(z.read("word/document.xml"))
            styles = _styles(ET.fromstring(z.read("word/styles.xml"))) if "word/styles.xml" in names else {}
            bullets = _bullet_lists(ET.fromstring(z.read("word/numbering.xml"))) if "word/numbering.xml" in names else set()
            notes = _footnotes(ET.fromstring(z.read("word/footnotes.xml"))) if "word/footnotes.xml" in names else {}
    except (zipfile.BadZipFile, ET.ParseError, KeyError) as e:
        raise ConvertError("Word 文件已损坏或加密，读不出内容") from e
    body = doc.find(f"{W}body")
    if body is None:
        return ""
    blocks: list[str] = []
    used: list[str] = []
    counters: dict[tuple[str, int], int] = {}
    for el in body:
        if el.tag == f"{W}p":
            line = _paragraph(el, styles, bullets, counters, used)
            if line is not None:
                blocks.append(line)
        elif el.tag == f"{W}tbl":
            blocks.append(_table(el, used))
    out = "\n\n".join(b for b in blocks if b.strip())
    # consecutive list items read better without blank lines between them
    out = re.sub(r"(?m)^((?:\s*)(?:- |\d+\. ).*)\n\n(?=\s*(?:- |\d+\. ))", r"\1\n", out)
    refs = [f"[^{n}]: {notes[n]}" for n in used if n in notes]
    if refs:
        out += "\n\n" + "\n".join(refs)
    return _tidy(out)


def _styles(root: ET.Element) -> dict[str, int]:
    """styleId → heading level, from the style's name or its outline level."""
    out: dict[str, int] = {}
    for s in root.iter(f"{W}style"):
        sid = s.get(f"{W}styleId") or ""
        name_el = s.find(f"{W}name")
        name = (name_el.get(f"{W}val") if name_el is not None else "") or ""
        m = _HEADING_NAME.match(name.strip()) or _HEADING_NAME.match(sid.strip())
        lvl = s.find(f"{W}pPr/{W}outlineLvl")
        if m:
            out[sid] = int(m.group(1))
        elif lvl is not None and (lvl.get(f"{W}val") or "").isdigit() and int(lvl.get(f"{W}val")) < 6:
            out[sid] = int(lvl.get(f"{W}val")) + 1
        elif name.strip().lower() == "title":
            out[sid] = 1
    return out


def _bullet_lists(root: ET.Element) -> set[tuple[str, int]]:
    """(numId, level) pairs that are bullets rather than numbered."""
    abstract: dict[str, dict[int, str]] = {}
    for a in root.iter(f"{W}abstractNum"):
        levels = {}
        for lvl in a.iter(f"{W}lvl"):
            fmt = lvl.find(f"{W}numFmt")
            levels[int(lvl.get(f"{W}ilvl") or 0)] = (fmt.get(f"{W}val") if fmt is not None else "") or ""
        abstract[a.get(f"{W}abstractNumId") or ""] = levels
    out = set()
    for n in root.iter(f"{W}num"):
        ref = n.find(f"{W}abstractNumId")
        levels = abstract.get(ref.get(f"{W}val") if ref is not None else "", {})
        for ilvl, fmt in levels.items():
            if fmt in ("bullet", "none", ""):
                out.add((n.get(f"{W}numId") or "", ilvl))
    return out


def _footnotes(root: ET.Element) -> dict[str, str]:
    out = {}
    for fn in root.iter(f"{W}footnote"):
        fid = fn.get(f"{W}id") or ""
        if fn.get(f"{W}type") in ("separator", "continuationSeparator") or not fid:
            continue
        text = " ".join(_runs_text(p, []) for p in fn.iter(f"{W}p")).strip()
        if text:
            out[fid] = " ".join(text.split())
    return out


def _runs_text(el: ET.Element, used: list[str]) -> str:
    parts: list[str] = []
    for node in el.iter():
        tag = node.tag
        if tag == f"{W}t":
            parts.append(node.text or "")
        elif tag == f"{W}tab":
            parts.append("\t")
        elif tag in (f"{W}br", f"{W}cr"):
            parts.append("  \n")
        elif tag == f"{W}footnoteReference":
            fid = node.get(f"{W}id") or ""
            if fid:
                used.append(fid)
                parts.append(f"[^{fid}]")
    return "".join(parts)


def _paragraph(p: ET.Element, styles: dict[str, int], bullets: set[tuple[str, int]],
               counters: dict[tuple[str, int], int], used: list[str]) -> str | None:
    text = _runs_text(p, used).strip()   # tracked deletions are w:delText, which _runs_text skips
    if not text:
        return None
    ppr = p.find(f"{W}pPr")
    level = 0
    num = None
    if ppr is not None:
        style = ppr.find(f"{W}pStyle")
        if style is not None:
            level = styles.get(style.get(f"{W}val") or "", 0)
        lvl = ppr.find(f"{W}outlineLvl")
        if not level and lvl is not None and (lvl.get(f"{W}val") or "").isdigit() and int(lvl.get(f"{W}val")) < 6:
            level = int(lvl.get(f"{W}val")) + 1
        num = ppr.find(f"{W}numPr")
    if level:
        return "#" * level + " " + " ".join(text.split())
    if num is not None:
        nid = num.find(f"{W}numId")
        ilvl = num.find(f"{W}ilvl")
        key = (nid.get(f"{W}val") if nid is not None else "") or "", int((ilvl.get(f"{W}val") if ilvl is not None else 0) or 0)
        if key[0] and key[0] != "0":
            indent = "  " * key[1]
            if key in bullets:
                return f"{indent}- {text}"
            counters[key] = counters.get(key, 0) + 1
            return f"{indent}{counters[key]}. {text}"
    return text


def _table(tbl: ET.Element, used: list[str]) -> str:
    rows = []
    for tr in tbl.iter(f"{W}tr"):
        cells = [" ".join(_runs_text(tc, used).split()).replace("|", "\\|") for tc in tr.findall(f"{W}tc")]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)
