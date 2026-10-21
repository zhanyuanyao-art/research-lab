"""PDF acceptance checks, per-page text extraction, identifiers and the title guess.

All PyMuPDF use sits in this module so another extractor can replace it (architecture §9.8).
"""
from __future__ import annotations

import hashlib
import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

MAX_BYTES = 100 * 1024 * 1024
EXTRACTOR = f"pymupdf {pymupdf.VersionBind}"
TEXT_FLAGS = pymupdf.TEXT_DEHYPHENATE | pymupdf.TEXT_PRESERVE_WHITESPACE
TEXT_LAYER_MIN_CHARS = 200

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)
ISBN_RE = re.compile(r"\bISBN(?:-1[03])?[:\s]*((?:97[89][-\s]?)?(?:\d[-\s]?){9}[\dXx])\b")
YEAR_RE = re.compile(r"\b(1[89]\d\d|20\d\d)\b")
# CJK papers print years as "2021年" with no word boundary between the digits and 年.
CJK_YEAR_RE = re.compile(r"(?<!\d)(1[89]\d\d|20\d\d)(?=年)")

HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
KANA_RE = re.compile(r"[\u3040-\u30ff]")
HANGUL_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff]")
CJK_ANY_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
CJK_LANGS = ("zh", "ja", "ko")
CJK_SHARE = 0.3            # CJK characters / all letters at or above this → a CJK paper
LANG_SAMPLE_PAGES = 3
CJK_TEXT_LAYER_MIN_CHARS = 80   # CJK text is ~3x denser than Latin, so the "has a text layer" bar is lower
EN_STOPWORDS = frozenset("the of and to in a is that for on with as by are this be from or which an it at".split())


class RejectedFile(ValueError):
    """The upload is not an acceptable PDF; the message is shown to the user."""


@dataclass
class PageText:
    index: int
    label: str
    text: str


@dataclass
class Extracted:
    page_count: int
    pages: list[PageText]
    has_text_layer: bool
    metadata: dict[str, str]
    title_guess: str | None
    author_guess: str | None
    dois: list[str] = field(default_factory=list)
    isbns: list[str] = field(default_factory=list)
    years: list[int] = field(default_factory=list)
    extractor: str = EXTRACTOR
    language: str | None = None

    @property
    def head_text(self) -> str:
        """First two pages, where the title, DOI and year live."""
        return "\n".join(p.text for p in self.pages[:2])


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_pdf(path: Path) -> None:
    """Raise RejectedFile unless the file is a readable, unencrypted PDF under the size cap."""
    size = path.stat().st_size
    if size == 0:
        raise RejectedFile("文件是空的")
    if size > MAX_BYTES:
        raise RejectedFile(f"文件超过 {MAX_BYTES // (1024 * 1024)} MB")
    with open(path, "rb") as fh:
        if fh.read(5) != b"%PDF-":
            raise RejectedFile("不是 PDF 文件")
    try:
        doc = pymupdf.open(path)
    except Exception as e:  # noqa: BLE001 - PyMuPDF raises several unrelated types
        raise RejectedFile(f"PDF 打不开，可能已损坏：{e}") from e
    try:
        if doc.is_encrypted or doc.needs_pass:
            raise RejectedFile("PDF 已加密，请先解除密码")
        if doc.page_count == 0:
            raise RejectedFile("PDF 没有页面")
    finally:
        doc.close()


def clean_doi(raw: str) -> str:
    return raw.strip().rstrip(".,;:)]}>'\"").lower()


def find_dois(text: str) -> list[str]:
    out: list[str] = []
    for m in DOI_RE.finditer(text):
        d = clean_doi(m.group(1))
        if d not in out:
            out.append(d)
    return out


def _isbn_ok(digits: str) -> bool:
    if len(digits) == 10:
        total = sum((10 - i) * (10 if c in "Xx" else int(c)) for i, c in enumerate(digits))
        return total % 11 == 0
    if len(digits) == 13 and digits.isdigit():
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(digits))
        return total % 10 == 0
    return False


def find_isbns(text: str) -> list[str]:
    out: list[str] = []
    for m in ISBN_RE.finditer(text):
        digits = re.sub(r"[-\s]", "", m.group(1)).upper()
        if _isbn_ok(digits) and digits not in out:
            out.append(digits)
    return out


def cjk_share(text: str) -> float:
    letters = sum(1 for c in text if c.isalpha())
    return len(CJK_ANY_RE.findall(text)) / letters if letters else 0.0


def detect_language(text: str) -> str | None:
    """ISO-ish guess from character classes: zh | ja | ko | en | other; None when there is no text.
    Kana marks Japanese and Hangul marks Korean even inside Han-heavy text; Latin text counts as
    English only when common English function words are frequent (otherwise 'other')."""
    letters = sum(1 for c in text if c.isalpha())
    if letters < 20:
        return None
    han, kana, hangul = len(HAN_RE.findall(text)), len(KANA_RE.findall(text)), len(HANGUL_RE.findall(text))
    cjk = han + kana + hangul
    if cjk / letters >= CJK_SHARE:
        if kana >= 0.1 * cjk:
            return "ja"
        if hangul >= 0.3 * cjk:
            return "ko"
        return "zh"
    words = re.findall(r"[A-Za-z]+", text.lower())
    if not words:
        return "other"
    return "en" if sum(1 for w in words if w in EN_STOPWORDS) / len(words) >= 0.06 else "other"


def is_cjk(language: str | None) -> bool:
    return language in CJK_LANGS


def _span_title(page: pymupdf.Page, cjk: bool = False) -> str | None:
    """Page 1's largest-font text, when that font is clearly larger than the body. With `cjk`, only
    spans that are mostly CJK compete, so a bilingual first page yields the Chinese title rather than
    the English translation many CNKI-indexed journals print underneath."""
    spans: list[tuple[float, str]] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for s in line.get("spans", []):
                t = s.get("text", "").strip()
                if t:
                    spans.append((round(float(s.get("size", 0)) * 2) / 2, t))
    if not spans:
        return None
    sizes = [sz for sz, t in spans for _ in range(len(t))]
    body = statistics.median(sizes)
    pool = [(sz, t) for sz, t in spans if cjk_share(t) >= 0.5] if cjk else spans
    if not pool:
        return None
    top = max(sz for sz, _ in pool)
    if top < body + 1.0:
        return None
    parts = [t for sz, t in pool if sz == top]
    title = ("" if cjk else " ").join(parts).strip()
    if len(title) < (4 if not cjk else 2) or len(title) > 300:
        return None
    return re.sub(r"\s+", " ", title)


def _line_title(text: str) -> str | None:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    lines = [l for l in lines if len(l) >= 3 and not DOI_RE.search(l) and not l.lower().startswith(("abstract", "http"))]
    if not lines:
        return None
    first = lines[0]
    if len(first) < 30 and len(lines) > 1 and len(lines[1]) < 120 and not re.match(r"^[A-Z]\.\s", lines[1]):
        first = f"{first} {lines[1]}"
    return first[:300]


def _metadata_title(meta: dict[str, str]) -> str | None:
    t = (meta.get("title") or "").strip()
    if len(t) < 8 or re.search(r"\.(docx?|pdf|tex|indd)$|^microsoft word|^untitled", t, re.I):
        return None
    return t


def find_years(text: str) -> list[int]:
    return sorted({int(y) for y in YEAR_RE.findall(text)} | {int(y) for y in CJK_YEAR_RE.findall(text)})


def _label(page) -> str:
    """The printed page label. pymupdf raises IndexError for pages before the first label range,
    which JSTOR downloads have when the cover sheet carries no label."""
    try:
        return page.get_label()
    except IndexError:
        return ""


# A page whose text runs up or down the sheet (a landscape table, set sideways with /Rotate or in the content
# stream) is read in the direction it is displayed. Upright pages never take this path.
SIDEWAYS_SHARE = 0.6        # of the page's characters, at least this share runs the same non-horizontal way
SIDEWAYS_MIN_CHARS = 80     # a margin stamp on an almost empty page is not a sideways page


def sideways_turn(page: pymupdf.Page) -> int:
    """How many degrees the page must be turned so that most of its text reads left to right: 0 for an ordinary
    page, else 90, 180 or 270 (the `rotate` of show_pdf_page). Lines are counted by character, by the direction
    they run in the page's own coordinates, so a /Rotate 90 sheet whose text is already drawn along the
    displayed width gives 0, and a landscape table drawn upward gives 270."""
    chars = {0: 0, 90: 0, 180: 0, 270: 0}
    for block in page.get_text("dict", flags=0).get("blocks", []):
        for line in block.get("lines", []):
            dx, dy = line.get("dir", (1.0, 0.0))
            turn = 0 if dx >= 0.95 else 270 if dy <= -0.95 else 180 if dx <= -0.95 else 90 if dy >= 0.95 else None
            if turn is not None:
                chars[turn] += sum(len("".join(s.get("text", "").split())) for s in line.get("spans", []))
    total = sum(chars.values())
    turn = max(chars, key=lambda k: chars[k])
    return turn if turn and total >= SIDEWAYS_MIN_CHARS and chars[turn] >= SIDEWAYS_SHARE * total else 0


def page_text(page: pymupdf.Page, turn: int | None = None) -> str:
    """The page's text in reading order. An upright page: PyMuPDF's sorted text, exactly as always. A sideways
    page: the same extraction on a copy of the page turned upright, so a landscape table keeps its rows, its
    caption and its notes instead of coming out column by column. `turn` is sideways_turn(page) when known."""
    turn = sideways_turn(page) if turn is None else turn
    if turn:
        try:
            w, h = (page.rect.height, page.rect.width) if page.rotation % 180 else (page.rect.width, page.rect.height)
            if turn % 180:
                w, h = h, w
            upright = pymupdf.open()
            try:
                sheet = upright.new_page(width=w, height=h)
                sheet.show_pdf_page(sheet.rect, page.parent, page.number, rotate=turn)
                return sheet.get_text("text", sort=True, flags=TEXT_FLAGS)
            finally:
                upright.close()
        except Exception:  # noqa: BLE001 - a page that cannot be turned is read as it lies
            pass
    return page.get_text("text", sort=True, flags=TEXT_FLAGS)


def sideways_texts(path: Path, also: Iterable[int] = ()) -> dict[int, str]:
    """page index → upright text, for the pages of a stored PDF that lie sideways: every /Rotate 90 or 270 page,
    and the pages in `also` (those whose stored text looks scrambled, e.g. a landscape table set in the content
    stream). Read-only; a PDF that will not open gives {}. Papers imported before sideways pages were turned keep
    their stored text, so the callers that need a table read it from here."""
    want = set(also)
    try:
        doc = pymupdf.open(path)
    except Exception:  # noqa: BLE001 - a missing or damaged file leaves the stored text in charge
        return {}
    try:
        out: dict[int, str] = {}
        for i, page in enumerate(doc):
            if i in want or page.rotation % 180:
                turn = sideways_turn(page)
                if turn:
                    out[i] = page_text(page, turn)
        return out
    finally:
        doc.close()


def extract(path: Path) -> Extracted:
    doc = pymupdf.open(path)
    try:
        pages = [PageText(i, _label(p) or str(i + 1), page_text(p)) for i, p in enumerate(doc)]
        meta = {k: (v or "") for k, v in (doc.metadata or {}).items() if isinstance(v, (str, type(None)))}
        language = detect_language("\n".join(p.text for p in pages[:LANG_SAMPLE_PAGES]))
        span_title = _span_title(doc[0], cjk=is_cjk(language)) if pages else None
    finally:
        doc.close()
    head = "\n".join(p.text for p in pages[:2])
    first3 = sum(len(p.text.strip()) for p in pages[:3])
    meta_text = " ".join(meta.get(k, "") for k in ("title", "subject", "keywords"))
    meta_title = _metadata_title(meta)
    if is_cjk(language):
        # CNKI/Wanfang exports often carry a file name or an English stub as the PDF title.
        meta_title = meta_title if meta_title and cjk_share(meta_title) >= 0.5 else None
        title = span_title or meta_title or _line_title(pages[0].text if pages else "")
    else:
        title = meta_title or span_title or _line_title(pages[0].text if pages else "")
    author = (meta.get("author") or "").strip() or None
    min_chars = CJK_TEXT_LAYER_MIN_CHARS if is_cjk(language) else TEXT_LAYER_MIN_CHARS
    return Extracted(
        page_count=len(pages), pages=pages, has_text_layer=first3 >= min_chars, metadata=meta,
        title_guess=title, author_guess=author,
        dois=find_dois(head + "\n" + meta_text), isbns=find_isbns(head + "\n" + meta_text),
        years=find_years(head), language=language,
    )


_CJK_CHAR = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uff00-\uffef]")
_SENTENCE_END = tuple(".!?:;\u3002\uff01\uff1f\uff1a\uff1b\u201d\"')\uff09")
_LIST_START = re.compile(r"^(?:[\(\[]?\d{1,3}[\.\)\]]|[\(\[]?[ivxIVX]{1,5}[\)\]]|[-\u2022\u25cf\u2013\uff0d]|[\(\[]?[a-zA-Z][\)\]])\s")


def _width(ln: str) -> int:
    """Display width: CJK glyphs are about twice as wide as Latin letters."""
    return len(ln) + len(_CJK_CHAR.findall(ln))


def _lines_with_gaps(text: str) -> list[tuple[str, int, int]]:
    """(stripped line, blank lines before it, leading indent) for every non-empty line."""
    out: list[tuple[str, int, int]] = []
    gap = 0
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        ln = raw.strip()
        if not ln:
            gap += 1
            continue
        out.append((ln, gap, len(raw) - len(raw.lstrip(" \t"))))
        gap = 0
    return out


def paragraphs(text: str) -> list[str]:
    """The page's paragraphs, each on one line, for reading and for the translator.

    Two things break a paragraph: a blank-line gap, and a short or sentence-final line (headings,
    bylines, the last line of a paragraph). Extractors differ in what a blank line means: most PDFs
    put one only between paragraphs, but some (RSIS commentaries, several reports) double-space every
    line, so there a single blank line is just a line break and two or more mean a paragraph. The
    page decides which it is from the share of lines that are followed by exactly one blank line.
    Inside a paragraph, a line indented after a sentence end starts a new paragraph (first-line
    indents), a hyphenated word is rejoined ("institu-/tions") while a compound keeps its hyphen
    ("ASEAN-/led"), and CJK lines join without a space. Display only: the stored page text keeps its
    lines, and quote verification normalises whitespace, so a passage selected from reflowed text
    still verifies against the page.
    """
    items = _lines_with_gaps(text)
    if not items:
        return []
    gaps = [g for _ln, g, _i in items[1:]]
    double_spaced = len(gaps) >= 4 and sum(1 for g in gaps if g == 1) / len(gaps) >= 0.6
    para_gap = 2 if double_spaced else 1
    lengths = sorted(_width(ln) for ln, _g, _i in items)
    full = lengths[min(len(lengths) - 1, int(len(lengths) * 0.9))]
    body_indent = min((i for _ln, _g, i in items), default=0)

    out: list[str] = []
    para = ""
    prev_line = ""
    for k, (ln, gap, indent) in enumerate(items):
        starts_new = bool(para) and (
            gap >= para_gap
            or (indent - body_indent >= 2 and prev_line.endswith(_SENTENCE_END))
            or (_LIST_START.match(ln) is not None and prev_line.endswith(_SENTENCE_END)))
        if starts_new:
            out.append(para)
            para = ""
        if not para:
            para = ln
        elif para.endswith("-") and len(para) > 1 and ln[:1].islower():
            para = (para[:-1] if para[-2].islower() else para) + ln
        elif _CJK_CHAR.match(para[-1]) and _CJK_CHAR.match(ln[0]):
            para += ln
        else:
            para += " " + ln
        nxt = items[k + 1][0] if k + 1 < len(items) else None
        short = _width(ln) < full * 0.7
        hyphenated = ln.endswith("-") and nxt is not None and nxt[:1].islower()
        if nxt is not None and not hyphenated and (short or (ln.endswith(_SENTENCE_END) and _width(ln) < full * 0.85)):
            out.append(para)
            para = ""
        prev_line = ln
    if para:
        out.append(para)
    return out


def reflow(text: str) -> str:
    """Join PDF hard line breaks back into paragraphs for reading (see `paragraphs`)."""
    return "\n\n".join(paragraphs(text))


def render_page_png(pdf_path: Path, page_index: int, out_path: Path, dpi: int = 144) -> Path:
    doc = pymupdf.open(pdf_path)
    try:
        if not 0 <= page_index < doc.page_count:
            raise IndexError(page_index)
        pix = doc[page_index].get_pixmap(dpi=dpi)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(".tmp")
        pix.save(str(tmp), output="png")
        tmp.replace(out_path)
    finally:
        doc.close()
    return out_path
