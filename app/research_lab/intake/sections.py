"""Heading detection (abstract, introduction, conclusion, references, numbered headings, ALL-CAPS headings) and
the text slices the summary prompt uses. References never enter a prompt.

ALL-CAPS headings (APSR, IO and other journals set section heads in capitals or small caps: "OUR EMPIRICAL
DESIGN", "DATA") are found per column: layout-preserving extraction puts both columns of a page on one line,
so a heading usually shares its line with the other column's text (card 0170).

Chinese headings (摘要 / 引言 / 导论 / 研究设计 / 方法 / 数据 / 结论 / 参考文献, with or without
一、/（一）/1. numbering and the spaced "摘 要" typesetting) map onto the same kinds, plus `methods`
and `data`, which the English heuristic does not emit."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .extract import CJK_ANY_RE, PageText

KEY_HEADING_RE = re.compile(
    r"^(?:\d+\.?|[IVX]+\.)?\s*(abstract|introduction|conclusions?|concluding remarks|discussion|references|"
    r"bibliography|works cited|notes)\b", re.I)
NUMBERED_HEADING_RE = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[IVX]+\.)\s+[A-Z][^.!?]{2,}$")
KIND_OF = {"abstract": "abstract", "introduction": "introduction", "conclusion": "conclusion",
           "conclusions": "conclusion", "concluding remarks": "conclusion", "discussion": "discussion",
           "references": "references", "bibliography": "references", "works cited": "references", "notes": "notes"}
MAX_HEADING_LEN = 60

_CJK_NUM = r"(?:[一二三四五六七八九十]+\s*[、．.]|[（(]\s*[一二三四五六七八九十]+\s*[)）]|\d+(?:\.\d+)*\s*[、．.]?|第[一二三四五六七八九十]+[章节部分])"
CJK_KEY_RE = re.compile(
    r"^[【\[〔]?\s*(?:" + _CJK_NUM + r")?\s*"
    r"(摘\s*要|内容提要|提\s*要|引\s*言|导\s*论|绪\s*论|前\s*言|问题的提出|研究设计|研究方法|方\s*法|"
    r"数据(?:来源|与方法|说明)?|资料来源|结\s*论|结\s*语|余\s*论|讨\s*论|参考文献|注\s*释)\s*[】\]〕]?")
CJK_NUMBERED_RE = re.compile(r"^(?:[一二三四五六七八九十]+\s*[、．]|第[一二三四五六七八九十]+[章节部分])\s*\S")
CJK_KIND_OF = {"摘要": "abstract", "内容提要": "abstract", "提要": "abstract", "引言": "introduction",
               "导论": "introduction", "绪论": "introduction", "前言": "introduction", "问题的提出": "introduction",
               "研究设计": "methods", "研究方法": "methods", "方法": "methods", "数据": "data", "数据来源": "data",
               "数据与方法": "methods", "数据说明": "data", "资料来源": "data", "结论": "conclusion", "结语": "conclusion",
               "余论": "conclusion", "讨论": "discussion", "参考文献": "references", "注释": "notes"}
CJK_SENTENCE_PUNCT = re.compile(r"[，。；？！,;]")
CJK_MAX_REST = 16

COLUMN_GUTTER_RE = re.compile(r"\s{3,}")      # the gap between two columns (or table cells) on one extracted line
CAPS_MAX_WORDS = 6
# A one-word capitalized line is a heading only when it is a usual section name: "YES", "OLS", "TABLE" are not.
CAPS_ONE_WORD = {"INTRODUCTION", "BACKGROUND", "THEORY", "HYPOTHESES", "DATA", "METHOD", "METHODS", "METHODOLOGY",
                 "DESIGN", "MEASUREMENT", "MEASURES", "ESTIMATION", "RESULTS", "FINDINGS", "ANALYSIS", "DISCUSSION",
                 "CONCLUSION", "CONCLUSIONS", "REFERENCES", "BIBLIOGRAPHY", "APPENDIX", "NOTES"}
CAPS_PAGE_MAX = 3        # more capitalized lines than this on a page are a figure's or table's labels
CAPS_RUNNING_PAGES = 3   # the same capitalized line on this many pages is a running header (journal, title)


@dataclass
class Section:
    kind: str                 # abstract | introduction | methods | data | conclusion | discussion | references | notes | heading
    heading: str | None
    page_start: int           # 0-based physical page index
    page_end: int
    line_no: int = -1         # line within page_start where the heading sits; -1 when inferred
    inline: bool = False      # the heading shares its line with another column's text


def cjk_heading(line: str) -> tuple[str, str] | None:
    """(kind, heading) for a Chinese section heading line, else None.

    "摘要：本文……" counts as the abstract heading even with text after the colon; other kinds allow a
    short tail ("结论与讨论", "三、研究设计与数据来源") but no sentence punctuation, so body sentences
    that open with 方法 or 数据 are not taken for headings."""
    m = CJK_KEY_RE.match(line)
    if m:
        key = re.sub(r"\s+", "", m.group(1))
        kind = CJK_KIND_OF.get(key)
        rest = line[m.end():].strip()
        if kind == "abstract" and (not rest or rest[0] in ":：" or len(rest) <= CJK_MAX_REST):
            return kind, line if len(line) < MAX_HEADING_LEN else key
        if kind and len(rest) <= CJK_MAX_REST and not CJK_SENTENCE_PUNCT.search(rest) and not rest.startswith((":", "：")):
            return kind, line
    if CJK_NUMBERED_RE.match(line) and len(line) <= 30 and not CJK_SENTENCE_PUNCT.search(line[2:]):
        return "heading", line
    return None


def caps_heading(segment: str) -> bool:
    """A capitalized section heading: 1–6 words, no lowercase letters, no digits, no closing period; one word only
    when it is a usual section name (CAPS_ONE_WORD)."""
    s = segment.strip()
    if not s or len(s) >= MAX_HEADING_LEN or s.endswith((".", ",", ";")) or re.search(r"\d", s):
        return False
    words = s.split()
    letters = [c for c in s if c.isalpha()]
    if not 1 <= len(words) <= CAPS_MAX_WORDS or len(letters) < 4 or any(c.islower() for c in letters):
        return False
    if len(words) == 1:
        return s.strip(":") in CAPS_ONE_WORD
    return max(len(re.sub(r"[^A-Z]", "", w)) for w in words) >= 4


def _numeric(segment: str) -> bool:
    chars = [c for c in segment if not c.isspace()]
    return bool(chars) and sum(c.isdigit() or c in ".,()−-–*∗%" for c in chars) / len(chars) >= 0.5


def _caps_sections(pages: Sequence[PageText]) -> list[Section]:
    """ALL-CAPS headings, one per column segment. The second line of a two-line heading is skipped, a page with
    many capitalized lines (figure labels) gives none, and a line repeated on several pages is a running header."""
    per_page: list[list[tuple[int, int, str, bool]]] = []
    for p in pages:
        found: list[tuple[int, int, str, bool]] = []
        caps_lines, prev = 0, -2
        for n, raw in enumerate(p.text.splitlines()):
            line = raw.strip()
            if not line or CJK_ANY_RE.search(line[:8]):
                continue
            segs = [s.strip() for s in COLUMN_GUTTER_RE.split(line) if s.strip()]
            hits = [s for s in segs if caps_heading(s)]
            if not hits or any(_numeric(s) for s in segs if s not in hits):   # a table's header row
                continue
            caps_lines += 1
            if prev != n - 1:                                                     # else it continues the heading above
                found.append((p.index, n, hits[0], len(segs) > 1))
            prev = n
        per_page.append(found if caps_lines <= CAPS_PAGE_MAX else [])
    seen: dict[str, set[int]] = {}
    for found in per_page:
        for idx, _, text, _ in found:
            seen.setdefault(re.sub(r"\s+", " ", text), set()).add(idx)
    out = []
    for found in per_page:
        for idx, n, text, inline in found:
            if len(seen[re.sub(r"\s+", " ", text)]) >= CAPS_RUNNING_PAGES:
                continue
            m = KEY_HEADING_RE.match(text)
            kind = KIND_OF[m.group(1).lower()] if m and len(text) - m.end() < 3 else "heading"
            out.append(Section(kind, text, idx, idx, n, inline))
    return out


def detect(pages: Sequence[PageText]) -> list[Section]:
    found: list[Section] = []
    for p in pages:
        for n, raw in enumerate(p.text.splitlines()):
            line = raw.strip()
            if not line:
                continue
            if CJK_ANY_RE.search(line[:8]):
                hit = cjk_heading(line)
                if hit:
                    found.append(Section(hit[0], hit[1], p.index, p.index, n))
                    continue
            if len(line) >= MAX_HEADING_LEN:
                continue
            m = KEY_HEADING_RE.match(line)
            if m and len(line) - m.end() < 25:
                found.append(Section(KIND_OF[m.group(1).lower()], line, p.index, p.index, n))
            elif NUMBERED_HEADING_RE.match(line) and len(line.split()) <= 8:
                found.append(Section("heading", line, p.index, p.index, n))
    taken = {(s.page_start, s.line_no) for s in found}
    found += [s for s in _caps_sections(pages) if (s.page_start, s.line_no) not in taken]
    found.sort(key=lambda s: (s.page_start, s.line_no))
    # A running header repeats "References" or "Notes" on every page; keep the first of each key kind
    # except conclusion/discussion, where the last one is the real closing section.
    kept: list[Section] = []
    seen: set[str] = set()
    for s in found:
        if s.kind in ("abstract", "introduction", "methods", "data", "references") and s.kind in seen:
            continue
        seen.add(s.kind)
        kept.append(s)
    last = pages[-1].index if pages else 0
    for i, s in enumerate(kept):
        s.page_end = kept[i + 1].page_start if i + 1 < len(kept) else last
    refs = next((s for s in kept if s.kind == "references"), None)
    conclusions = [s for s in kept if s.kind == "conclusion" and (refs is None or s.page_start <= refs.page_start)]
    if conclusions:
        for s in kept:
            if s.kind == "conclusion" and s is not conclusions[-1]:
                s.kind = "heading"
    elif refs is not None and refs.page_start > 0:
        kept.append(Section("conclusion", None, max(0, refs.page_start - 2), refs.page_start, -1))
    kept.sort(key=lambda s: (s.page_start, s.line_no))
    return kept


def first(sections: Sequence[Section], kind: str) -> Section | None:
    return next((s for s in sections if s.kind == kind), None)


def _slice(pages: Sequence[PageText], start: Section, end: Section | None) -> list[tuple[str, str]]:
    """(label, text) pieces from the start heading line up to the end heading line."""
    by_index = {p.index: p for p in pages}
    out: list[tuple[str, str]] = []
    stop_page = end.page_start if end else max(by_index)
    for idx in range(start.page_start, stop_page + 1):
        p = by_index.get(idx)
        if p is None:
            continue
        lines = p.text.splitlines()
        lo = start.line_no if idx == start.page_start and start.line_no >= 0 else 0
        hi = end.line_no if end and idx == end.page_start and end.line_no >= 0 else len(lines)
        chunk = "\n".join(lines[lo:hi]).strip()
        if chunk:
            out.append((p.label, chunk))
    return out


def section_text(pages: Sequence[PageText], sections: Sequence[Section], kind: str) -> list[tuple[str, str]]:
    ordered = sorted(sections, key=lambda s: (s.page_start, s.line_no))
    for i, s in enumerate(ordered):
        if s.kind == kind:
            nxt = next((t for t in ordered[i + 1:] if t.line_no >= 0), None)
            return _slice(pages, s, nxt)
    return []


def body_without_references(pages: Sequence[PageText], sections: Sequence[Section]) -> list[tuple[str, str]]:
    refs = first(sections, "references")
    out: list[tuple[str, str]] = []
    for p in pages:
        if refs is None or p.index < refs.page_start:
            if p.text.strip():
                out.append((p.label, p.text.strip()))
        elif p.index == refs.page_start and refs.line_no > 0:
            head = "\n".join(p.text.splitlines()[:refs.line_no]).strip()
            if head:
                out.append((p.label, head))
    return out


def truncate(pieces: list[tuple[str, str]], max_chars: int) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    used = 0
    for label, text in pieces:
        if used >= max_chars:
            break
        room = max_chars - used
        out.append((label, text if len(text) <= room else text[:room].rsplit(" ", 1)[0] + " […]"))
        used += min(len(text), room)
    return out
