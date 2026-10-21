"""全书复现清单 (card 0187, docs/design/REPLICATION-v4-DCL.md §3B): every table and figure of a paper and of its online
appendix, and the author's script that makes each one. Rule based: no AI, nothing is run, nothing is downloaded.

Where the text comes from
  main      the paper's 原文 (the primary file), its pages read upright (plans.upright_pages: card 0182 turns landscape
            tables, so a caption inside one is found)
  appendix  every other file of the same paper (文献页 补 PDF keeps a second file beside the first), then the page texts of
            a PDF given by path from the command line (stored once in replication_coverage_sources, never edited), then
            any extra path passed to scan()
  scripts   the author's code and README / codebook text that card 0183 saved under <workspace>/author/

Items. A caption is a line that starts with "TABLE 3.", "Table A41:", "Figure 5", "Fig. 2", "表 3" or "Appendix Table 2",
followed by punctuation, a capitalised title or the end of the line; "Table A1 displays …" is a sentence about a table,
not a caption. A caption that wraps joins its next line; a table of contents entry (dots, a page number) is skipped;
the first good caption of a label wins. Tables of the main text are 正文结果 (ticked at first); the appendix's go to
附录稳健性 when the caption, or the heading they sit under, speaks of robustness, an alternative, removing or excluding
something, "using X instead" …, and to 附录其他 otherwise.

Scripts, strongest evidence first
  comment    a comment that opens with the label ("#### Table 3: Effect … ####", "* Figure A7", "// Table 2"): the script
             and the output calls under it (stargazer, esttab, outreg2, ggsave, pdf/png, write.csv, savefig, to_latex …)
  filename   an output file whose name carries the label (Table_A41.tex, fig7.pdf)
  doc        the README / codebook: a label that starts a line and the file named after it (or on the same line), and for
             the appendix a section number ("5.2: Disaggregating Results …") against the headings the items sit under;
             "coefPlotMain.pdf" in a README counts through the script that writes that file
  words      two or more words shared by an output file's name and the caption, no tie (weakest, last resort)
A script the README names but the workspace does not hold is listed all the same, marked as not saved."""
from __future__ import annotations

import bisect
import itertools
import json
import os
import posixpath
import re
import sqlite3
import stat
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..config import get_paths
from ..db.connection import now_iso
from . import package

GROUPS = ("main", "robust", "other")
GROUP_LABELS = {"main": "正文结果", "robust": "附录稳健性", "other": "附录其他"}
KIND_WORDS = {"table": "Table", "figure": "Figure"}
CAPTION_CHARS = 240
OWNER_CHARS = 40
AUTHOR_DIR = "author"
AUTHOR_SCRIPTS = 300                  # scripts read; the same ceilings as the workspace brief
AUTHOR_FILE = 2 * 1024 * 1024
AUTHOR_BYTES = 8 * 1024 * 1024
DOC_CHARS = 200_000
DOC_FILES = 8
# Ceilings. A paper, a script or a README is somebody else's text, so every rule reads a bounded slice of it and every
# pass over it is linear: a long line is cut before any pattern sees it, items and labels are counted and capped, lookups
# go through dicts and bisect, never nested scans, and a scan that still runs past SCAN_SECONDS stops with a message.
MAX_LINE = 500                        # characters of a page line the caption rules read
MAX_PAGE_LINES = 3_000
MAX_SCAN_LINES = 200_000              # page lines over all sources
MAX_ITEMS = 500                       # items listed; the rest are counted (「另有 N 项未列出」)
MAX_OVERFLOW = 100_000                # distinct extra labels counted before counting stops
MAX_CODE_LINE = 1_000
MAX_CODE_LINES = 20_000               # lines of one script
MAX_COMMENT = 300
MAX_LABELS = 2_000                    # label comments / output calls kept per script …
MAX_CALLS = 1_000
MAX_LABELS_TOTAL = 10_000             # … and over all scripts
MAX_CALLS_TOTAL = 5_000
MAX_ORPHANS = 1_000                   # output calls no label covers, tried against captions by words
MAX_DOC_LINE = 500
MAX_MARKS = 5_000                     # label mentions and file names read per README
MAX_PRODUCED = 10                    # scripts remembered per output file name
MAX_WORD_ITEMS = 60                   # a caption word shared by more items than this is not evidence
MAX_KEY_OUTPUTS = 20                  # files listed for one item
MAX_KEYS = 50                         # labels in one "Tables 1, 2, 3 …" mention
SCAN_SECONDS = 20.0
RANKS = {"comment": 0, "filename": 1, "doc": 2, "words": 3}
HOW_NOTES = {"comment": "脚本里的注释", "filename": "输出文件名带编号", "doc": "说明书里写着", "words": "文件名和标题用词相近"}


class CoverageError(ValueError):
    """Refused, in Chinese, as the page shows it."""


def _check(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise CoverageError(f"这份论文或作者代码太大，扫描超过 {SCAN_SECONDS:.0f} 秒，已中止。")


# ---------------------------------------------------------------- labels

_NUM = r"(?:[A-Z]{1,2}[.\-]?\d{1,3}[a-z]?|\d{1,2}\.\d{1,2}[a-z]?(?!\d)|\d{1,3}[a-z]?)"
_NUM_CAP = _NUM[:-1] + r"|[IVX]{1,5}(?![A-Za-z]))"
_WORD_ANY = r"(?:(?i:tables?|figures?|figs?\.?|tab\.?)|表|图)"
_CAP = re.compile(
    r"^[ \t]*(?:(?:Online[ \t]+)?(?P<app>Appendix|Supplementary|Supplemental)[ \t]+)?"
    r"(?P<word>Table|TABLE|Tab\.|TAB\.|Figure|FIGURE|Fig\.|FIG\.|表|图)[ \t]*(?P<num>" + _NUM_CAP + r")"
    r"(?![A-Za-z0-9])(?P<rest>.*)$")
_DOT_RUN = re.compile(r"[.·…]{4,}")
_SPACED_DOTS = re.compile(r"\.[ \t]{1,3}\.[ \t]{1,3}\.")
_DOTS_THEN_END = re.compile(r"\.[ \t]{1,3}\.$")
_SENTENCE_END = (".", "!", "?", "。", "！", "？")
_CONNECTOR = re.compile(r"(?:-|,|\b(?:of|and|the|for|in|on|with|to|using|by|from|a|an|or|between|across|at|as|than|"
                        r"versus|vs\.?|under|over|into|among))[ \t]*$", re.I)
_GLUED_STAMP = re.compile(r"(?<=[a-z])(?:University|Cambridge|Press|Online|Published)$")
_NUMBER_CELL = re.compile(r"^[(\[]?[-−–]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)[)\]]?(?:\*|∗|†)*$")
_CJK = re.compile(r"[㐀-鿿]")
_MENTION = re.compile(
    r"(?<![A-Za-z])(?P<first>" + _WORD_ANY + r")[ \t]*"
    r"(?P<body>" + _NUM + r"(?:[ \t]*(?:–|—|-|to)[ \t]*" + _NUM + r")?"
    r"(?:[ \t]*(?:,[ \t]*(?:and|&)|,|;|and|&|/|、|和)[ \t]*(?:" + _WORD_ANY + r"[ \t]*)?" + _NUM + r"(?:[ \t]*(?:–|—|-|to)[ \t]*" + _NUM + r")?)*)"
    r"(?![A-Za-z0-9])")
_TOKEN = re.compile(r"(?:(?P<w>" + _WORD_ANY + r")[ \t]*)?(?P<n>" + _NUM + r")(?:[ \t]*(?P<r>–|—|-|to)[ \t]*(?P<n2>" + _NUM + r"))?")


def _toc_line(line: str) -> bool:
    """A table of contents entry: dot leaders, or a page number after a wide gap or after spaced dots. Linear: the
    page number is peeled off the end by hand, and the patterns left have bounded repeats."""
    if _DOT_RUN.search(line) or _SPACED_DOTS.search(line):
        return True
    s = line.rstrip()
    k = len(s)
    while k and "0" <= s[k - 1] <= "9":
        k -= 1
    if not 1 <= len(s) - k <= 3:
        return s.endswith("...") or bool(_DOTS_THEN_END.search(s[-8:]))
    head = s[:k]
    return (head.endswith("    ") or head.endswith("\t") or head.rstrip().endswith("...")
            or bool(_DOTS_THEN_END.search(head.rstrip()[-8:])))


def norm_num(num: str) -> str:
    """Table A.1 and Table A1 are one label; 3a stays apart from 3 (parent_key() joins them when a script says "Figure 3")."""
    n = num.replace(" ", "").upper()
    return re.sub(r"^([A-Z]{1,2})[.\-](?=\d)", r"\1", n)


def mkey(kind: str, num: str) -> str:
    return f"{kind}:{norm_num(num)}"


def parent_key(key: str) -> str | None:
    m = re.fullmatch(r"(\w+:.*\d)[A-Z]", key)
    return m.group(1) if m else None


def _kind(word: str) -> str:
    w = word.lower().rstrip(".")
    return "figure" if w.startswith("fig") or w == "图" else "table"


def label_text(kind: str, num: str, cjk: bool = False) -> str:
    return f"{'表' if kind == 'table' else '图'} {num}" if cjk else f"{KIND_WORDS[kind]} {num}"


def is_appendix_num(num: str) -> bool:
    """A letter in front of the digits (A41, S3, B.2) marks the appendix, whichever file the caption is in."""
    return bool(re.match(r"[A-Z]{1,2}[.\-]?\d", num))


def _expand(first: str, body: str) -> list[tuple[str, str]]:
    """(kind, number) of each label in "Tables A9 and A10", "Table 3, Figure 7", "A9–A12" (a range with the same
    letter or plain numbers, at most 12 long; otherwise its two ends)."""
    out: list[tuple[str, str]] = []
    word = first
    for m in _TOKEN.finditer(first + " " + body):
        if m.group("w"):
            word = m.group("w")
        n, n2 = m.group("n"), m.group("n2")
        kind = _kind(word)
        out.append((kind, n))
        if n2:
            a, b = re.fullmatch(r"([A-Z]*)(\d+)(.*)", n), re.fullmatch(r"([A-Z]*)(\d+)(.*)", n2)
            if a and b and a.group(1) == b.group(1) and not a.group(3) and not b.group(3) \
                    and 1 < int(b.group(2)) - int(a.group(2)) + 1 <= 12:
                out += [(kind, f"{a.group(1)}{k}") for k in range(int(a.group(2)) + 1, int(b.group(2)))]
            out.append((kind, n2))
    return list(dict.fromkeys(out))[:MAX_KEYS]


def mentions(text: str) -> list[tuple[int, int, list[str]]]:
    """(start, end, label keys) of each reference to a table or figure in `text`, a list of them counting as one."""
    return [(m.start(), m.end(), [mkey(k, n) for k, n in _expand(m.group("first"), m.group("body"))])
            for m in _MENTION.finditer(text)]


# ---------------------------------------------------------------- captions and headings

@dataclass
class SourceText:
    source: str                           # 'main' | 'appendix'
    name: str
    pages: list[tuple[str, str]]          # (printed page label, text)


@dataclass
class Item:
    source: str
    kind: str
    num: str
    label: str
    caption: str
    page: str
    section: str = ""
    headings: list[str] = field(default_factory=list)      # the headings it sits under, outermost first
    sec_ids: list[str] = field(default_factory=list)       # their numbers: 5, 5.2
    group: str = ""
    scripts: list[dict[str, Any]] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    rank: int = 0                                           # 2 title on the caption line, 1 on the next, 0 none

    @property
    def key(self) -> str:
        return f"{self.source}:{self.label}"

    @property
    def mkey(self) -> str:
        return mkey(self.kind, self.num)


def _wide(line: str) -> bool:
    return bool(re.search(r"\S {3,}\S", line.strip()))


def _numeric_row(line: str) -> bool:
    return sum(1 for t in line.split() if _NUMBER_CELL.match(t)) >= 3


def _title_of(word: str, rest: str) -> str | None:
    """The caption text on the label's line, "" when the line holds the label alone, None when the line is a sentence
    that merely begins with a reference."""
    if not rest.strip():
        return ""
    m = re.match(r"[ \t]*[.:：—–\-][ \t]*(.*)$", rest)
    if m:
        return m.group(1)
    if word in ("表", "图"):
        m = re.match(r"[ \t　]+(\S.*)$", rest)
        return m.group(1) if m else None
    m = re.match(r"[ \t]+([A-Z][a-z].*|[A-Z]{2,}\b.*)$", rest)
    return m.group(1) if m else None


def _join(parts: list[str], more: str) -> list[str]:
    prev = parts[-1]
    if prev.endswith("-") and more[:1].islower() and prev[-2:-1].isalpha():
        parts[-1] = prev[:-1] + more                       # a word the typesetter broke: Deci- / sions
    elif prev.endswith("-"):
        parts[-1] = prev + more
    else:
        parts.append(more)
    return parts


def _caption_text(lines: list[str], i: int, title: str) -> tuple[str, int]:
    """(caption, rank): the title, with up to two wrapped lines. A wrapped line follows with no blank between, is not a
    table row or another caption, and either starts like a continuation or follows a line that stops mid-phrase."""
    j = i + 1
    rank = 2 if title else 0
    parts = [title] if title else []
    if not title:                                          # "Figure 3." then the title on the next line (one blank at most)
        k = j + 1 if j < len(lines) and not lines[j].strip() else j
        if k < len(lines) and lines[k].strip() and not _CAP.match(lines[k]) and not _wide(lines[k]) \
                and not _numeric_row(lines[k]) and len(lines[k].strip()) <= 150:
            parts, rank, j = [lines[k].strip()], 1, k + 1
    while parts and j < len(lines) and len(parts) < 3:
        nxt = lines[j].strip()
        if not nxt or _CAP.match(lines[j]) or _wide(lines[j]) or _numeric_row(nxt) or len(nxt) > 150:
            break
        prev = parts[-1]
        if prev.endswith(_SENTENCE_END):
            break
        cjk = bool(_CJK.search(prev) and _CJK.search(nxt))
        if not (cjk and len(prev) < 60 or nxt[:1].islower() or nxt[:1].isdigit() or nxt[:1] in "(“\"'"
                or _CONNECTOR.search(prev) or (nxt[:1].isupper() and " " not in nxt and nxt.isalpha())):
            break
        parts = _join(parts, nxt)
        j += 1
    text = re.sub(r"\s+", " ", " ".join(parts)).strip()
    return text, rank


_REPEAT = re.compile(r"^(?:Table|TABLE|Tab\.|Figure|FIGURE|Fig\.|表|图)[ \t]*(?P<num>" + _NUM_CAP + r")[ \t]*[.:：—–\-]?[ \t]*", re.I)


def _clean_caption(label_word: str, num: str, text: str) -> str:
    """The caption without a repeated label ("Table A7: Table A7: Out-of-Sample"), a stamp glued to its end, or the
    punctuation around it."""
    m = _REPEAT.match(text)
    if m and norm_num(m.group("num")) == norm_num(num):
        text = text[m.end():]
    text = _GLUED_STAMP.sub("", text).strip(" \t.:—–-")
    return text[:CAPTION_CHARS - 1] + "…" if len(text) > CAPTION_CHARS else text


_HEAD = re.compile(r"^[ \t]{0,3}(?P<num>\d{1,2}(?:\.\d{1,2}){0,3})\.?[ \t]{1,6}(?P<title>[A-Z][^\n]{2,100}?)[ \t]*$")


def _heading(line: str) -> tuple[str, str] | None:
    """('5.2', 'Disaggregating Results by Meeting Type') for a numbered heading line; table-of-contents lines,
    sentences and table rows are not headings."""
    if len(line) > 160:
        return None
    m = _HEAD.match(line)
    if not m:
        return None
    num, title = m.group("num"), m.group("title")
    if _toc_line(line) or title.endswith(".") or re.search(r"\S {3,}\S", title) or sum(c.isdigit() for c in title) > 6:
        return None
    if "." not in num:
        words = [w for w in re.findall(r"[A-Za-z]+", title) if len(w) > 3]
        if not words or sum(w[0].isupper() for w in words) < 0.6 * len(words):
            return None
    return num, title


ROBUST_RE = re.compile(r"robust|alternativ|alternate|remov(?:e|ing)|exclud|without|dropp|instead|placebo|sensitiv|"
                       r"稳健|替代|剔除|排除|敏感性|安慰剂", re.I)
FULL_RE = re.compile(r"full results|main results|descriptive|summary statistic|描述性|完整结果", re.I)


def group_of(item: Item) -> str:
    """正文结果 for the main text; in the appendix 附录稳健性 when the caption or the nearest heading says so, or an
    ancestor heading does and the nearest one is not a "full results" or descriptive part; 附录其他 otherwise."""
    if item.source == "main":
        return "main"
    if ROBUST_RE.search(item.caption):
        return "robust"
    if item.headings:
        if ROBUST_RE.search(item.headings[-1]):
            return "robust"
        if not FULL_RE.search(item.headings[-1]) and any(ROBUST_RE.search(h) for h in item.headings[:-1]):
            return "robust"
    return "other"


def _settle(stack: list[tuple[str, str, int]], earlier: list[list[tuple[str, str, int]]], caption: str) -> list[tuple[str, str, int]]:
    """A float is often set a page after the section it belongs to, past the next heading. When the nearest heading
    shares no word with the caption and one of the last few headings shares two, the item belongs to that one."""
    words = _words(caption)
    if not words or not stack or words & _words(stack[-1][1]):
        return stack
    for chain in reversed(earlier):
        if len(words & _words(chain[-1][1])) >= 2:
            return chain
    return stack


def extract(sources: Iterable[SourceText], *, limit: int | None = None, deadline: float | None = None) -> tuple[list[Item], int]:
    """(items, how many more labels were found than `limit` lists): every captioned table and figure, in the order
    the sources and pages give them. A line is cut to MAX_LINE characters before any pattern reads it."""
    limit = MAX_ITEMS if limit is None else limit
    found: dict[str, Item] = {}
    overflow: set[str] = set()
    budget = MAX_SCAN_LINES
    for src in sources:
        stack: list[tuple[str, str, int]] = []
        history: list[list[tuple[str, str, int]]] = []       # the headings as they stood after each heading
        for page_label, text in src.pages:
            _check(deadline)
            lines = [ln[:MAX_LINE] for ln in text.splitlines()[:min(MAX_PAGE_LINES, budget)]]
            budget -= len(lines)
            for i, line in enumerate(lines):
                h = _heading(line) if src.source == "appendix" else None
                if h:
                    depth = h[0].count(".") + 1
                    while stack and stack[-1][2] >= depth:
                        stack.pop()
                    stack.append((h[0], h[1], depth))
                    history = (history + [list(stack)])[-5:]
                    continue
                m = _CAP.match(line)
                if not m or _toc_line(line):
                    continue
                word, num = m.group("word"), m.group("num")
                title = _title_of(word, m.group("rest"))
                if title is None:
                    continue
                kind = _kind(word)
                source = "appendix" if src.source == "appendix" or m.group("app") or is_appendix_num(num) else "main"
                label = label_text(kind, num.strip(), word in ("表", "图"))
                key = f"{source}:{label}"
                if key not in found and len(found) >= limit:
                    if len(overflow) < MAX_OVERFLOW:
                        overflow.add(key)
                    continue
                caption, rank = _caption_text(lines, i, title.strip())
                caption = _clean_caption(word, num, caption)
                chain = _settle(stack, history[:-1], caption) if source == "appendix" else []
                item = Item(source, kind, num.strip(), label, caption, page_label,
                            headings=[f"{n} {t}" for n, t, _ in chain], sec_ids=[n for n, _, _ in chain], rank=rank)
                if chain:
                    item.section = item.headings[-1]
                old = found.get(key)
                if old is None or item.rank > old.rank:
                    found[key] = item
    items = list(found.values())
    for it in items:
        it.group = group_of(it)
    return items, len(overflow)


def extract_items(sources: Iterable[SourceText], **kw: Any) -> list[Item]:
    return extract(sources, **kw)[0]


# ---------------------------------------------------------------- what a script writes and labels

R_OUT = ("stargazer", "texreg", "htmlreg", "screenreg", "xtable", "print.xtable", "kable", "kbl", "modelsummary",
         "msummary", "etable", "huxreg", "export_summs", "gtsave", "save_kable", "ggsave", "ggexport", "pdf", "png",
         "jpeg", "jpg", "svg", "tiff", "bmp", "postscript", "cairo_pdf", "write.csv", "write.csv2", "write.table",
         "write_csv", "write_tsv", "write_xlsx", "write.xlsx", "fwrite", "sink", "writeLines", "saveWidget")
R_FILE_ONLY = {"print", "cat", "sink", "writeLines"}          # an output call only when it names a file
PY_OUT = ("savefig", "to_latex", "to_csv", "to_excel", "to_html", "to_markdown", "write_image", "write_html")
OUT_EXT = r"(?:tex|pdf|png|jpe?g|svg|eps|tiff?|bmp|csv|tsv|xlsx?|html?|txt|docx?|rtf|md|dta|ps|gif)"
_OUT_FILE = re.compile(rf"\.{OUT_EXT}$", re.I)
CODE_NAME_EXT = ("r", "rmd", "do", "ado", "py", "sas", "m", "jl", "sps")
_R_OUT = re.compile(rf"(?<![\w.$@])(?:[A-Za-z_][\w.]*::)?({package._alt(R_OUT + tuple(R_FILE_ONLY))})\s*\(")
_PY_OUT = re.compile(rf"(?<![\w$@])({package._alt(PY_OUT)})\s*\(")
_ST_LEAD = r"^[ \t]*(?:(?:qui(?:etly)?|noi(?:sily)?|cap(?:ture)?)[ \t]+)*"
_ST_USING_CMD = re.compile(rf"{_ST_LEAD}(esttab|estout|outreg2?|tabout|asdoc|outsheet|export[ \t]+delimited|export[ \t]+excel|"
                           r"latabstat|mat2txt|texdoc[ \t]+\w+)\b", re.M)
_ST_PATH_CMD = re.compile(rf"{_ST_LEAD}(graph[ \t]+export|graph[ \t]+save|putexcel[ \t]+set|putdocx[ \t]+save)\b", re.M)
_COMMENT_LEAD = re.compile(
    r"^(?:(?:create|creates|make|makes|produce|produces|plot|plots|generate|generates|build|save|export|write|output|"
    r"replicate|replicates|code[ \t]+for|data[ \t]+for|for|in|of)[ \t]+){0,2}"
    r"(?:(?:online[ \t]+)?appendix[ \t]+)?(?P<mention>" + _WORD_ANY + r"[ \t]*" + _NUM + r".*)$", re.I)


@dataclass
class LabelEvent:
    line: int
    keys: list[str]
    title: str
    trailing: bool


@dataclass
class OutCall:
    line: int
    name: str
    file: str | None


@dataclass
class ScriptInfo:
    path: str
    labels: list[LabelEvent] = field(default_factory=list)
    calls: list[OutCall] = field(default_factory=list)


def _call_args(clean: str, pos: int, limit: int = 1_500) -> str:
    """The text of the call whose "(" ends just before `pos`, to its closing bracket."""
    depth, i, quote = 0, pos, ""
    end = min(len(clean), pos + limit)
    while i < end:
        c = clean[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
        i += 1
    return clean[pos:i]


def _out_file(args: str) -> str | None:
    for a, b in package.STR.findall(args):
        lit = (a or b).strip()
        if _OUT_FILE.search(lit) and posixpath.splitext(posixpath.basename(lit))[0]:
            return lit.replace("\\", "/").removeprefix("./")
    return None


def _comments(lines: list[str], clean_lines: list[str]) -> Iterator[tuple[int, str, bool]]:
    """(line number, comment text, trailing) per line that holds a comment: package._mask blanks comments, so a comment
    is where the original differs from the masked copy."""
    for n, (raw, cl) in enumerate(zip(lines, clean_lines), 1):
        k = next((x for x in range(min(len(raw), len(cl))) if raw[x] != cl[x]), -1)
        if k >= 0:
            yield n, raw[k:k + MAX_COMMENT], bool(cl[:k].strip())


_DECOR = "#*/-=~%! \t\r\n\f\v"


def _label_of_comment(comment: str) -> tuple[list[str], str] | None:
    body = comment[:MAX_COMMENT].lstrip(_DECOR).rstrip(_DECOR)
    m = _COMMENT_LEAD.match(body)
    if not m:
        return None
    found = mentions(m.group("mention"))
    if not found or found[0][0] != 0:
        return None
    title = m.group("mention")[found[0][1]:].lstrip(" \t:.-–—").strip()
    return found[0][2], title


def scan_script(path: str, text: str, *, labels_left: int = MAX_LABELS, calls_left: int = MAX_CALLS) -> ScriptInfo:
    """The labels a script's comments open with and the output calls it makes, by line. At most MAX_CODE_LINES lines of
    at most MAX_CODE_LINE characters are read, and at most `labels_left` labels and `calls_left` calls kept."""
    lang = package.lang_of(path)
    info = ScriptInfo(path)
    if not lang:
        return info
    if package.ext_of(path) == ".rmd":
        text = package._rmd_code(text)
    lines = [ln[:MAX_CODE_LINE] for ln in text.split("\n")[:MAX_CODE_LINES]]
    text = "\n".join(lines)
    clean, masked = package._mask(text, lang)
    starts = [0]
    for ln in lines:
        starts.append(starts[-1] + len(ln) + 1)

    def line_of(pos: int) -> int:
        return bisect.bisect_right(starts, pos)

    clean_lines = clean.split("\n")
    for n, comment, trailing in _comments(lines, clean_lines):
        got = _label_of_comment(comment)
        if got:
            info.labels.append(LabelEvent(n, got[0], got[1], trailing))
            if len(info.labels) >= labels_left:
                break

    def add(line: int, name: str, file: str | None) -> bool:
        info.calls.append(OutCall(line, name, file))
        return len(info.calls) >= calls_left

    if lang == "r":
        for m in _R_OUT.finditer(masked):
            file = _out_file(_call_args(clean, m.end()))
            if m.group(1) in R_FILE_ONLY and not file:
                continue
            if add(line_of(m.start()), m.group(1), file):
                break
    elif lang == "python":
        for m in _PY_OUT.finditer(masked):
            if add(line_of(m.start()), m.group(1), _out_file(_call_args(clean, m.end()))):
                break
    elif lang == "stata":
        for m in _ST_USING_CMD.finditer(masked):
            ln = line_of(m.start())
            last = ln
            while last < len(lines) and last - ln < 8 and lines[last - 1].rstrip().endswith("///"):
                last += 1
            stmt = "\n".join(clean_lines[ln - 1:last])
            u = re.search(r"\busing[ \t\n]+", stmt)
            file = package._stata_path(stmt, u.end()) if u else ""
            if add(ln, re.sub(r"\s+", " ", m.group(1)), file if file and _OUT_FILE.search(file) else None):
                break
        for m in _ST_PATH_CMD.finditer(masked):
            if len(info.calls) >= calls_left:
                break
            file = package._stata_path(clean, m.end())
            add(line_of(m.start()), re.sub(r"\s+", " ", m.group(1)), file if file and _OUT_FILE.search(file) else None)
    info.calls.sort(key=lambda c: c.line)
    return info


# ---------------------------------------------------------------- the README and the codebook

_FILE = re.compile(r"(?<![\w/.\-])((?:\.{1,2}/)?\w[\w./\-]*\.([A-Za-z]{1,5}))(?![\w])")
_SEC = re.compile(r"^[ \t]*(\d{1,2}(?:\.\d{1,2}){1,3})[ \t]*[:：]", re.M)
_APPENDIX_HEAD = re.compile(r"^[ \t]*(?:Online[ \t]+)?Appendix(?:[ \t]+(?:Section|Tables?|Figures?|Items?)){0,2}[ \t]*$", re.M | re.I)
ANCHOR_WINDOW = 400


def _appendix_start(text: str) -> int:
    """Where the appendix part of a codebook starts: the last line that is just "Appendix" with section numbers after it."""
    for m in reversed(list(_APPENDIX_HEAD.finditer(text))):
        if _SEC.search(text[m.end():m.end() + 800]):
            return m.start()
    return 0


@dataclass
class Anchor:
    start: int
    name: str                             # the file as written
    kind: str                             # 'script' | 'output'


def _opens_line(text: str, line_start: int, pos: int) -> bool:
    """True when at most one word (a bullet, a number, "Run") comes before `pos` on its line: a row of a list or table,
    not a sentence that happens to name both a table and a script."""
    return len(text[line_start:pos].strip(" \t-*•>|#0123456789.)(`\"'").split()) <= 1


def doc_links(text: str) -> tuple[list[tuple[str, Anchor]], list[tuple[str, Anchor]]]:
    """(label key, anchor) and (section number, anchor) pairs of one README or codebook. A label ties to the file named
    on its own line, or, when the label starts a line, to the first file named after it before the next line-start
    label (a codebook table that wraps its rows). A section number counts only after the "Appendix" heading. Lines are
    cut to MAX_DOC_LINE, at most MAX_MARKS file names and mentions are read, and lookups go by bisect."""
    text = "\n".join(ln[:MAX_DOC_LINE] for ln in text.split("\n"))
    anchors: list[Anchor] = []
    for m in _FILE.finditer(text):
        if len(anchors) >= MAX_MARKS:
            break
        ext = m.group(2).lower()
        if ext in CODE_NAME_EXT:
            anchors.append(Anchor(m.start(), m.group(1), "script"))
        elif _OUT_FILE.search("." + ext):
            anchors.append(Anchor(m.start(), m.group(1), "output"))
    starts = [a.start for a in anchors]
    marks: list[tuple[int, int, str, list[str]]] = [(s, e, "label", keys) for s, e, keys in mentions(text)[:MAX_MARKS]]
    start_sec = _appendix_start(text)
    marks += [(m.start(1), m.end(), "sec", [m.group(1)]) for m in itertools.islice(_SEC.finditer(text), MAX_MARKS)
              if m.start() >= start_sec]
    marks.sort()
    opens = [m[0] for m in marks if _opens_line(text, text.rfind("\n", 0, m[0]) + 1, m[0])]
    at_start = set(opens)
    labels: list[tuple[str, Anchor]] = []
    secs: list[tuple[str, Anchor]] = []
    for s, e, typ, keys in marks:
        ls = text.rfind("\n", 0, s) + 1
        le = text.find("\n", e)
        le = len(text) if le < 0 else le
        same = [a for a in anchors[bisect.bisect_left(starts, ls):bisect.bisect_left(starts, le)]
                if s in at_start or _opens_line(text, ls, a.start)]
        pick = min(same, key=lambda a: abs(a.start - s)) if same else None
        if pick is None and s in at_start:
            i = bisect.bisect_right(opens, s)
            limit = min(opens[i] if i < len(opens) else len(text), e + ANCHOR_WINDOW)
            j = bisect.bisect_left(starts, e)
            if j < len(anchors) and anchors[j].start < limit:
                pick = anchors[j]
        if pick is not None:
            (labels if typ == "label" else secs).extend((k, pick) for k in keys)
    return labels, secs


# ---------------------------------------------------------------- tying items to scripts

def _words(s: str) -> set[str]:
    stop = {"effect", "effects", "using", "results", "model", "models", "table", "figure", "summary", "analysis", "data",
            "with", "from", "that", "this", "their", "across", "between", "main", "text", "plot", "plots", "estimated",
            "distribution", "values"}
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    return {w[:5] for w in re.findall(r"[a-z]{4,}", spaced.lower()) if w not in stop}


def _norm_title(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", s.lower()))


@dataclass
class Evidence:
    """label key → script → the best reason found; section number → script; label key → files written for it."""
    code: dict[str, str]                                    # author-relative path → text
    by_name: dict[str, list[str]] = field(default_factory=dict)
    hits: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    secs: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    outs: dict[str, list[str]] = field(default_factory=dict)
    produced: dict[str, list[tuple[str, str]]] = field(default_factory=dict)   # output file name → (script, file as written)
    orphans: list[tuple[str, OutCall]] = field(default_factory=list)           # output calls no label comment covers
    infos: dict[str, ScriptInfo] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for p in self.code:
            self.by_name.setdefault(posixpath.basename(p).lower(), []).append(p)

    def resolve(self, written: str) -> tuple[str, str]:
        """(display name, saved path or '') for a script as a README writes it."""
        name = written.replace("\\", "/")
        hits = self.by_name.get(posixpath.basename(name).lower(), [])
        if len(hits) > 1:
            hits = [h for h in hits if h.lower().endswith(name.lower().removeprefix("./"))] or hits
        return posixpath.basename(name), (hits[0] if hits else "")

    def add(self, store: dict[str, dict[str, dict[str, Any]]], key: str, script: str, how: str, ref: str, via: str = "") -> None:
        name, full = self.resolve(script)
        slot = store.setdefault(key, {})
        cur = slot.get(full or name)
        if cur is None:
            slot[full or name] = {"path": name, "full": full, "how": how, "ref": ref, "via": via, "have": bool(full)}
        elif RANKS[how] < RANKS[cur["how"]]:
            cur.update(how=how, ref=ref, via=via or cur["via"])

    def output(self, key: str, file: str) -> None:
        outs = self.outs.setdefault(key, [])
        if len(outs) < MAX_KEY_OUTPUTS and file not in outs:
            outs.append(file)


def _norm_out(p: str) -> str:
    return p.replace("\\", "/").removeprefix("./")


def _label_in_name(stem: str) -> list[str]:
    out = []
    for m in re.finditer(r"(?<![A-Za-z])(table|tab|tbl|figure|fig)[\s_\-.]*([A-Za-z]?\d{1,3}[a-z]?)(?![0-9])", stem, re.I):
        num = m.group(2)
        if num[0].isalpha():
            num = num[0].upper() + num[1:]
        out.append(mkey("figure" if m.group(1).lower().startswith("fig") else "table", num))
    return out


def gather_evidence(code: dict[str, str], docs: dict[str, str], *, deadline: float | None = None) -> Evidence:
    """Comments, output file names and documents, read once each, into one Evidence. A label meets the calls under it
    through bisect on their line numbers; every label owns its own stretch of lines, so the work is linear."""
    ev = Evidence(code)
    labels_left, calls_left = MAX_LABELS_TOTAL, MAX_CALLS_TOTAL
    for p, t in code.items():
        if not package.lang_of(p):
            continue
        _check(deadline)
        info = scan_script(p, t, labels_left=min(MAX_LABELS, labels_left), calls_left=min(MAX_CALLS, calls_left))
        labels_left -= len(info.labels)
        calls_left -= len(info.calls)
        ev.infos[p] = info
    for path, info in ev.infos.items():
        base = posixpath.basename(path)
        call_lines = [c.line for c in info.calls]
        block = sorted(e.line for e in info.labels if not e.trailing)
        tagged = {e.line for e in info.labels if e.trailing}
        for e in info.labels:
            if e.trailing:
                calls = info.calls[bisect.bisect_left(call_lines, e.line):bisect.bisect_right(call_lines, e.line)]
            else:
                i = bisect.bisect_right(block, e.line)
                nxt = block[i] if i < len(block) else 10 ** 9
                calls = [c for c in info.calls[bisect.bisect_left(call_lines, e.line):bisect.bisect_left(call_lines, nxt)]
                         if c.line not in tagged]
            via = ", ".join(dict.fromkeys(c.name for c in calls))
            for k in e.keys:
                ev.add(ev.hits, k, path, "comment", f"{base}:{e.line}", via)
                for c in calls:
                    if c.file:
                        ev.output(k, _norm_out(c.file))
        first = block[0] if block else None
        for c in info.calls:
            if not c.file:
                continue
            made = ev.produced.setdefault(posixpath.basename(c.file).lower(), [])
            if len(made) < MAX_PRODUCED and (path, _norm_out(c.file)) not in made:
                made.append((path, _norm_out(c.file)))
            for k in _label_in_name(posixpath.splitext(posixpath.basename(c.file))[0]):
                ev.add(ev.hits, k, path, "filename", f"{base}:{c.line}", c.name)
                ev.output(k, _norm_out(c.file))
            if c.line not in tagged and (first is None or c.line < first) and len(ev.orphans) < MAX_ORPHANS:
                ev.orphans.append((path, c))
    for dpath, text in docs.items():
        _check(deadline)
        dname = posixpath.basename(dpath)
        labels, secs = doc_links(text)
        for target, pairs in ((ev.hits, labels), (ev.secs, secs)):
            for key, a in pairs:
                if a.kind == "script":
                    ev.add(target, key, a.name, "doc", dname)
                    continue
                for script, file in ev.produced.get(posixpath.basename(a.name).lower(), []):
                    ev.add(target, key, script, "doc", f"{dname} → {posixpath.basename(a.name)}")
                    if target is ev.hits:
                        ev.output(key, file)
    return ev


def map_scripts(items: list[Item], code: dict[str, str], docs: dict[str, str], *, deadline: float | None = None) -> Evidence:
    """Fill each item's scripts and outputs: the strongest evidence first, one entry per script. Captions are
    normalised once and looked up through dicts, never compared item by item against every label or call."""
    ev = gather_evidence(code, docs, deadline=deadline)
    caps = [_norm_title(it.caption) for it in items]
    by_caption: dict[str, list[int]] = {}
    inverted: dict[str, list[int]] = {}
    for i, (it, cap) in enumerate(zip(items, caps)):
        if cap:
            by_caption.setdefault(cap, []).append(i)
        for w in _words(it.caption):
            inverted.setdefault(w, []).append(i)
    # a comment whose title is an item's caption under a label the paper does not use (a copied header): the caption wins
    for path, info in ev.infos.items():
        for e in info.labels:
            title = _norm_title(e.title)
            for i in by_caption.get(title, []) if len(title) >= 25 else []:
                if items[i].mkey not in e.keys:
                    ev.add(ev.hits, items[i].mkey, path, "comment", f"{posixpath.basename(path)}:{e.line}", "title")
    # words in common, for the output files no label reaches
    labelled = {f for outs in ev.outs.values() for f in outs}
    for path, c in ev.orphans:
        file = _norm_out(c.file or "")
        words = _words(posixpath.splitext(posixpath.basename(file))[0])
        if file in labelled or len(words) < 2:
            continue
        score: Counter[int] = Counter()
        for w in words:
            near = inverted.get(w, ())
            if len(near) <= MAX_WORD_ITEMS:                    # a word in most captions says nothing
                score.update(i for i in near if items[i].mkey not in ev.hits)
        top = score.most_common(2)
        if not top or top[0][1] < 2 or (len(top) > 1 and top[0][1] == top[1][1]):
            continue
        it = items[top[0][0]]
        ev.add(ev.hits, it.mkey, path, "words", f"{posixpath.basename(path)}:{c.line}", c.name)
        ev.output(it.mkey, file)
    for it in items:
        found: dict[str, dict[str, Any]] = {}
        for k in (it.mkey, parent_key(it.mkey)):
            for slot, hit in ev.hits.get(k or "", {}).items():
                found.setdefault(slot, hit)
        if it.source == "appendix":
            for sid in reversed(it.sec_ids):
                if sid in ev.secs:
                    for slot, hit in ev.secs[sid].items():
                        found.setdefault(slot, dict(hit, ref=f"{hit['ref']}，{sid} 节"))
                    break
        ordered = sorted(found.values(), key=lambda h: RANKS[h["how"]])
        it.scripts = [{k: h[k] for k in ("path", "full", "how", "ref", "via", "have")} for h in ordered]
        it.outputs = list(ev.outs.get(it.mkey) or ev.outs.get(parent_key(it.mkey) or "") or [])
    return ev


def hit_note(h: dict[str, Any]) -> str:
    """The sentence under a script's name: what says this script makes the item."""
    text = HOW_NOTES.get(h.get("how", ""), "")
    if h.get("ref"):
        text += f"（{h['ref']}）"
    via = h.get("via")
    if via and via != "title":
        text += f"，用 {via} 输出"
    elif via == "title":
        text += "，注释标题和这张表的标题一致"
    if not h.get("have"):
        text += "；这个脚本没有保存在工作区"
    return text


# ---------------------------------------------------------------- reading the inputs

@dataclass
class Scan:
    items: list[Item]
    sources: list[dict[str, Any]]                           # [{source, name, pages}]
    scripts: int = 0
    docs: int = 0
    notes: list[str] = field(default_factory=list)
    omitted: int = 0                                        # labels found beyond MAX_ITEMS, not listed

    @property
    def sources_read(self) -> set[str]:
        return {s["source"] for s in self.sources if s["pages"]}


def author_texts(plan_id: str) -> tuple[dict[str, str], dict[str, str]]:
    """(scripts, docs) the workspace holds under author/: author-relative path → text. Read-only; links and anything
    that is not a regular file are left alone."""
    code: dict[str, str] = {}
    docs: dict[str, str] = {}
    if not re.fullmatch(r"[A-Za-z0-9_-]+", plan_id or ""):
        return code, docs
    root = get_paths().data / "replication" / plan_id / "workspace" / AUTHOR_DIR
    if os.path.islink(root) or not root.is_dir():
        return code, docs
    budget = AUTHOR_BYTES
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(filenames):
            f = Path(dirpath) / name
            try:
                st = f.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            rel = f.relative_to(root).as_posix()
            try:
                if package.lang_of(name):
                    if len(code) < AUTHOR_SCRIPTS and st.st_size <= min(AUTHOR_FILE, budget):
                        code[rel] = f.read_text(encoding="utf-8", errors="replace")
                        budget -= st.st_size
                elif package.is_doc(name) and len(docs) < DOC_FILES:
                    docs[rel] = f.read_text(encoding="utf-8", errors="replace")[:DOC_CHARS]
            except OSError:
                continue
    return code, docs


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone())


def _pdf_pages(path: Path) -> tuple[str, list[tuple[str, str]]]:
    from ..intake import extract
    extract.check_pdf(path)
    return extract.sha256_file(path), [(p.label, p.text or "") for p in extract.extract(path).pages]


def sources_of(conn: sqlite3.Connection, plan: dict[str, Any], extra_pdfs: Iterable[Path] = ()) -> list[SourceText]:
    """The paper's 原文 and its other files (read upright), the stored appendix texts, then `extra_pdfs`."""
    from . import plans
    out: list[SourceText] = []
    seen: set[str] = set()
    if plan.get("paper_id"):
        for n, f in enumerate(conn.execute("SELECT * FROM paper_files WHERE paper_id = ? ORDER BY is_primary DESC, id",
                                           (plan["paper_id"],)).fetchall()):
            out.append(SourceText("main" if n == 0 else "appendix", str(f["original_name"] or f["rel_path"]),
                                  [(str(p.label), p.text or "") for p in plans.upright_pages(conn, f)]))
            seen.add(str(f["sha256"]))
    if _has_table(conn, "replication_coverage_sources"):
        for r in conn.execute("SELECT name, sha256, pages_json FROM replication_coverage_sources WHERE plan_id = ? ORDER BY id",
                              (plan["id"],)):
            if r["sha256"] not in seen:
                out.append(SourceText("appendix", r["name"], [(a, b) for a, b in json.loads(r["pages_json"])]))
                seen.add(r["sha256"])
    for path in extra_pdfs:
        sha, pages = _pdf_pages(Path(path))
        if sha not in seen:
            out.append(SourceText("appendix", Path(path).name, pages))
            seen.add(sha)
    return out


def scan(conn: sqlite3.Connection, plan_id: str, *, extra_pdfs: Iterable[Path] = (), code: dict[str, str] | None = None,
         docs: dict[str, str] | None = None) -> Scan:
    """The plan's whole list, computed and not stored: items from the paper and its appendix, tied to the author's scripts."""
    from . import plans
    plan = plans.get(conn, plan_id)
    if plan is None:
        raise CoverageError(f"没有这个复现方案：{plan_id}")
    sources = sources_of(conn, plan, extra_pdfs)
    if code is None or docs is None:
        c, d = author_texts(plan_id)
        code, docs = c if code is None else code, d if docs is None else docs
    result = Scan([], [{"source": s.source, "name": s.name, "pages": len(s.pages)} for s in sources],
                  scripts=sum(1 for p in code if package.lang_of(p)), docs=len(docs))
    if not any(s.pages for s in sources):
        result.notes.append("这篇论文没有可读的 PDF 文本，列不出清单。")
        return result
    deadline = time.monotonic() + SCAN_SECONDS
    result.items, result.omitted = extract(sources, deadline=deadline)
    map_scripts(result.items, code, docs, deadline=deadline)
    if result.omitted:
        result.notes.append(f"另有 {result.omitted} 项未列出（清单最多列 {MAX_ITEMS} 项）。")
    if "appendix" not in result.sources_read:
        result.notes.append("没有附录：在文献页给这篇补一份附录 PDF（第二个文件）后再 Scan。")
    if not code:
        result.notes.append("工作区里还没有作者的代码，对不上脚本；取到复现包之后再 Scan。")
    return result


# ---------------------------------------------------------------- stats

def stats(items: Iterable[Item | dict[str, Any]]) -> dict[str, Any]:
    """Counts per group, how many items name a script, and how many of those scripts are saved in the workspace."""
    out: dict[str, Any] = {"total": 0, "mapped": 0, "have": 0, "groups": {g: {"total": 0, "mapped": 0} for g in GROUPS}}
    for it in items:
        d = it if isinstance(it, dict) else {"group": it.group, "scripts": it.scripts}
        g = out["groups"][d["group"]]
        out["total"] += 1
        g["total"] += 1
        if d["scripts"]:
            out["mapped"] += 1
            g["mapped"] += 1
            out["have"] += any(s["have"] for s in d["scripts"])
    return out


# ---------------------------------------------------------------- the stored list

def _must(conn: sqlite3.Connection, plan_id: str) -> None:
    if not conn.execute("SELECT 1 FROM replication_plans WHERE id = ?", (plan_id,)).fetchone():
        raise CoverageError(f"没有这个复现方案：{plan_id}")


def save_scan(conn: sqlite3.Connection, plan_id: str, result: Scan) -> dict[str, int]:
    """Store the list. An item already there keeps its ticked and owner; one new in the main text starts ticked; an
    item of a source that was read this time and is no longer found goes. Sources not read are left alone, so a scan
    without the appendix never drops what Leo wrote beside appendix items."""
    from .plans import _locked
    _must(conn, plan_id)
    now = now_iso()
    added = kept = dropped = 0
    with _locked(conn):
        old = {r["item_key"]: r for r in conn.execute(
            "SELECT id, item_key, source FROM replication_coverage WHERE plan_id = ?", (plan_id,))}
        for pos, it in enumerate(result.items):
            row = (it.kind, it.label, it.caption, it.page, it.section, it.group, pos,
                   json.dumps(it.scripts, ensure_ascii=False), json.dumps(it.outputs, ensure_ascii=False), now)
            if it.key in old:
                conn.execute("UPDATE replication_coverage SET kind=?, label=?, caption=?, page=?, section=?, grp=?, position=?, "
                             "scripts_json=?, outputs_json=?, updated_at=? WHERE id=?", (*row, old[it.key]["id"]))
                kept += 1
            else:
                conn.execute("INSERT INTO replication_coverage(plan_id, item_key, source, kind, label, caption, page, section, "
                             "grp, position, scripts_json, outputs_json, ticked, created_at, updated_at) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (plan_id, it.key, it.source, *row[:-1], 1 if it.group == "main" else 0, now, now))
                added += 1
        found = {it.key for it in result.items}
        for key, r in old.items():
            if key not in found and r["source"] in result.sources_read:
                conn.execute("DELETE FROM replication_coverage WHERE id = ?", (r["id"],))
                dropped += 1
        conn.execute("UPDATE replication_plans SET coverage_omitted = ? WHERE id = ?", (result.omitted, plan_id))
    return {"added": added, "kept": kept, "dropped": dropped}


def add_source(conn: sqlite3.Connection, plan_id: str, path: Path) -> str:
    """Store the page texts of an appendix PDF given by path (the command line). Read-only on the file; the same PDF
    twice is stored once. Returns its name."""
    _must(conn, plan_id)
    sha, pages = _pdf_pages(Path(path))
    conn.execute("INSERT OR IGNORE INTO replication_coverage_sources(plan_id, name, sha256, page_count, pages_json, added_at) "
                 "VALUES (?,?,?,?,?,?)", (plan_id, Path(path).name, sha, len(pages), json.dumps(pages, ensure_ascii=False), now_iso()))
    return Path(path).name


def rescan(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    """Scan and store; the numbers the page flashes."""
    result = scan(conn, plan_id)
    if not result.items:
        raise CoverageError(result.notes[0] if result.notes else "论文里没有找到带标题的表或图。")
    return {**save_scan(conn, plan_id, result), **stats(result.items), "notes": result.notes, "omitted": result.omitted}


def items(conn: sqlite3.Connection, plan_id: str) -> list[dict[str, Any]]:
    out = []
    if not _has_table(conn, "replication_coverage"):
        return out
    for r in conn.execute("SELECT * FROM replication_coverage WHERE plan_id = ? ORDER BY position, id", (plan_id,)):
        d = dict(r)
        d["scripts"] = json.loads(d.pop("scripts_json") or "[]")
        d["outputs"] = json.loads(d.pop("outputs_json") or "[]")
        d["group"] = d.pop("grp")
        d["ticked"] = bool(d["ticked"])
        for s in d["scripts"]:
            s["note"] = hit_note(s)
        out.append(d)
    return out


def overview(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    """What the plan page shows: the non-empty groups in order, each with its items and counts."""
    rows = items(conn, plan_id)
    groups = []
    for g in GROUPS:
        mine = [r for r in rows if r["group"] == g]
        if mine:
            groups.append({"group": g, "label": GROUP_LABELS[g], "rows": mine, "total": len(mine),
                           "ticked": sum(r["ticked"] for r in mine), "mapped": sum(1 for r in mine if r["scripts"])})
    try:
        omitted = int(conn.execute("SELECT coverage_omitted FROM replication_plans WHERE id = ?", (plan_id,)).fetchone()[0])
    except (sqlite3.OperationalError, TypeError):
        omitted = 0
    return {"groups": groups, "total": len(rows), "ticked": sum(r["ticked"] for r in rows),
            "mapped": sum(1 for r in rows if r["scripts"]), "has_appendix": any(r["source"] == "appendix" for r in rows),
            "omitted": omitted, "limit": MAX_ITEMS}


def owner_of(raw: Any) -> str:
    return re.sub(r"[\x00-\x1f\x7f-\x9f  ]", "", str(raw or "")).strip()[:OWNER_CHARS]


def save_marks(conn: sqlite3.Connection, plan_id: str, ids: Iterable[int], ticked: Iterable[int],
               owners: dict[int, str]) -> int:
    """Leo's ticks and 汇报人 for the listed items (ids not in this plan are ignored). Returns how many items changed."""
    from .plans import _locked
    _must(conn, plan_id)
    ids, ticked = list(dict.fromkeys(ids)), set(ticked)
    changed = 0
    with _locked(conn):
        for i in ids:
            row = conn.execute("SELECT ticked, owner FROM replication_coverage WHERE id = ? AND plan_id = ?", (i, plan_id)).fetchone()
            if row is None:
                continue
            tick, owner = int(i in ticked), owner_of(owners.get(i, row["owner"]))
            if (tick, owner) != (row["ticked"], row["owner"]):
                conn.execute("UPDATE replication_coverage SET ticked = ?, owner = ?, updated_at = ? WHERE id = ?",
                             (tick, owner, now_iso(), i))
                changed += 1
    return changed


def delete_for_plan(conn: sqlite3.Connection, plan_id: str) -> None:
    for table in ("replication_coverage", "replication_coverage_sources"):
        if _has_table(conn, table):
            conn.execute(f"DELETE FROM {table} WHERE plan_id = ?", (plan_id,))


# ---------------------------------------------------------------- command line

def _report(result: Scan) -> str:
    s = stats(result.items)
    names = "、".join("{}（{} 页）".format(x["name"], x["pages"]) for x in result.sources) or "无"
    lines = [f"来源：{names}；作者脚本 {result.scripts} 个，说明书 {result.docs} 份"]
    for g in GROUPS:
        c = s["groups"][g]
        lines.append(f"{GROUP_LABELS[g]}：{c['total']} 项，对上脚本 {c['mapped']} 项")
    share = f"{100 * s['mapped'] / s['total']:.0f}%" if s["total"] else "—"
    lines.append(f"合计 {s['total']} 项，对上脚本 {s['mapped']} 项（{share}），其中脚本已保存在工作区的 {s['have']} 项")
    lines += result.notes
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """python -m research_lab.replication.coverage <plan> [--appendix PDF …] [--list] [--save]
    Without --save nothing is written: the list is computed from the library and the workspace and its counts printed.
    With --save the appendix PDFs are stored as sources of the plan and the list is stored (a migration must have run)."""
    import argparse
    from ..db.connection import connect, migrate
    ap = argparse.ArgumentParser(prog="coverage", description=main.__doc__)
    ap.add_argument("plan")
    ap.add_argument("--appendix", action="append", default=[], metavar="PDF")
    ap.add_argument("--list", action="store_true", help="print every item")
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args(argv)
    conn = connect()
    try:
        extra = [Path(p) for p in args.appendix]
        if args.save:
            migrate(conn)
            for p in extra:
                add_source(conn, args.plan, p)
            extra = []
        result = scan(conn, args.plan, extra_pdfs=extra)
        print(_report(result))
        if args.list:
            for it in result.items:
                names = ", ".join(s["path"] for s in it.scripts) or "—"
                print(f"{GROUP_LABELS[it.group]}\t{it.label}\tp.{it.page}\t{names}\t{it.caption[:70]}")
        if args.save:
            print(save_scan(conn, args.plan, result))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
