"""Shared Markdown for issue descriptions and comments (docs/design/ISSUES-LINEAR.md §8.2).

Storage stays plain Markdown. `render(text)` turns it into safe HTML for display; the browser editor
(`static/issue-editor.js`) parses the same text with markdown-it (JS) and the same four extensions, so
what the page shows and what the editor loads agree. Keep the two sides in step when changing a rule.

On top of CommonMark (html off, so raw HTML is shown as text):

- strikethrough `~~字~~`; single newlines become line breaks (`breaks`), so old plain-text
  descriptions and comments display as written;
- underline `++字++`;
- collapse: a line `+++ 标题`, the content, then a line `+++`. Top level only;
- task lists: a bullet list whose items all start with `[ ]` / `[x]`. A list mixing tasks and plain
  items keeps the brackets as text, matching the editor, where a list is one kind or the other;
- CJK-aware emphasis: `按**「承诺可信度」**归档` is bold. CommonMark's flanking rules reject a delimiter
  between a CJK letter and CJK punctuation, which the editor produces whenever bold text starts or ends
  with 「」（）;
- links (and images) only to http, https or paths inside the app; anything else stays literal text.

Formats that may not overlap (Leo 10-10) are resolved exactly as the editor's schema resolves them, so
old hand-written Markdown that breaks a rule renders the way the editor would load it, without errors:
code inside code shows literally; inline code drops bold/italic/strike/underline around it; a link
drops inline code and underline inside it; strikethrough drops underline; underline is ignored inside
list items. The markers of a dropped format are not shown, only its text.
"""
from __future__ import annotations

import re
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml, isMdAsciiPunct, isPunctChar, isWhiteSpace
from markdown_it.parser_inline import ParserInline
from markdown_it.rules_inline.state_inline import Delimiter, Scanned, StateInline
from markdown_it.token import Token
from markupsafe import Markup

__all__ = ["render", "is_safe_link", "md"]

# ------------------------------------------------------------------------------------------- links

_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")


def is_safe_link(url: str) -> bool:
    """http(s) URLs and paths inside the app; no other scheme, no protocol-relative `//host`."""
    u = (url or "").strip()
    if not u or u.startswith(("//", "\\", "/\\")):
        return False
    head = re.split(r"[/?#]", u, maxsplit=1)[0]
    if ":" in head:
        m = _SCHEME.match(head)
        return bool(m) and m.group(1).lower() in ("http", "https")
    return True


# ------------------------------------------------------------------------- CJK-aware delimiters

_CJK_RANGES = (
    (0x1100, 0x11FF), (0x2E80, 0x2FFF), (0x3000, 0x303F), (0x3040, 0x30FF), (0x3100, 0x31FF),
    (0x3200, 0x33FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xA960, 0xA97F), (0xAC00, 0xD7FF),
    (0xF900, 0xFAFF), (0xFE30, 0xFE4F), (0xFF00, 0xFFEF), (0x20000, 0x3FFFF),
)


def _is_cjk(ch: str) -> bool:
    c = ord(ch)
    return any(lo <= c <= hi for lo, hi in _CJK_RANGES)


class _CJKStateInline(StateInline):
    def scanDelims(self, start: int, canSplitWord: bool) -> Scanned:
        # CommonMark's rule plus one relaxation: a CJK character on the outer side of a delimiter
        # counts like whitespace when the inner side is punctuation. Mirrored in issue-editor.js.
        pos, maximum = start, self.posMax
        marker = self.src[start]
        last = self.src[start - 1] if start > 0 else " "
        while pos < maximum and self.src[pos] == marker:
            pos += 1
        nxt = self.src[pos] if pos < maximum else " "
        last_p = isMdAsciiPunct(ord(last)) or isPunctChar(last)
        next_p = isMdAsciiPunct(ord(nxt)) or isPunctChar(nxt)
        last_w, next_w = isWhiteSpace(ord(last)), isWhiteSpace(ord(nxt))
        left = not next_w and (not next_p or last_w or last_p or _is_cjk(last))
        right = not last_w and (not last_p or next_w or next_p or _is_cjk(nxt))
        can_open = left and (canSplitWord or not right or last_p)
        can_close = right and (canSplitWord or not left or next_p)
        return Scanned(can_open, can_close, pos - start)


class _CJKParserInline(ParserInline):
    def parse(self, src, md, env, tokens):
        state = _CJKStateInline(src, md, env, tokens)
        self.tokenize(state)
        for rule in self.ruler2.getRules(""):
            rule(state)
        return state.tokens


# ---------------------------------------------------------------------------- underline ++字++

def _underline_tokenize(state: StateInline, silent: bool) -> bool:
    start = state.pos
    if silent or state.src[start] != "+":
        return False
    scanned = state.scanDelims(start, True)
    length = scanned.length
    if length < 2:
        return False
    if length % 2:
        state.push("text", "", 0).content = "+"
        length -= 1
    for _ in range(0, length, 2):
        state.push("text", "", 0).content = "++"
        state.delimiters.append(Delimiter(marker=ord("+"), length=0, token=len(state.tokens) - 1,
                                          end=-1, open=scanned.can_open, close=scanned.can_close))
    state.pos += scanned.length
    return True


def _underline_pairs(state: StateInline, delimiters: list[Delimiter]) -> None:
    lone = []
    for d in delimiters:
        if d.marker != ord("+") or d.end == -1:
            continue
        end = delimiters[d.end]
        for idx, kind, nesting in ((d.token, "underline_open", 1), (end.token, "underline_close", -1)):
            t = state.tokens[idx]
            t.type, t.tag, t.nesting, t.markup, t.content = kind, "u", nesting, "++", ""
        before = state.tokens[end.token - 1]
        if before.type == "text" and before.content == "+":
            lone.append(end.token - 1)
    # An odd run splits as `+` `++` …; move the lone `+` after the closing tags (as strikethrough does).
    while lone:
        i = lone.pop()
        j = i + 1
        while j < len(state.tokens) and state.tokens[j].type == "underline_close":
            j += 1
        j -= 1
        if i != j:
            state.tokens[i], state.tokens[j] = state.tokens[j], state.tokens[i]


def _underline_post(state: StateInline) -> None:
    _underline_pairs(state, state.delimiters)
    for meta in state.tokens_meta:
        if meta and "delimiters" in meta:
            _underline_pairs(state, meta["delimiters"])


# ------------------------------------------------------------------------- collapse +++ 标题

def _details(state, start_line: int, end_line: int, silent: bool) -> bool:
    if state.level > 0 or state.sCount[start_line] - state.blkIndent >= 4:
        return False   # never inside a list, quote or another collapse
    pos = state.bMarks[start_line] + state.tShift[start_line]
    line = state.src[pos:state.eMarks[start_line]]
    if not line.startswith("+++") or (len(line) > 3 and line[3] not in " \t"):
        return False
    if silent:
        return True
    title = line[3:].strip()
    next_line, closed = start_line, False
    while True:
        next_line += 1
        if next_line >= end_line:
            break   # an unclosed collapse runs to the end, like an unclosed code fence
        p, m = state.bMarks[next_line] + state.tShift[next_line], state.eMarks[next_line]
        if p < m and state.sCount[next_line] < state.blkIndent:
            break
        if state.sCount[next_line] - state.blkIndent < 4 and state.src[p:m].rstrip() == "+++":
            closed = True
            break
    old_parent, old_max = state.parentType, state.lineMax
    state.parentType, state.lineMax = "details", next_line
    tok = state.push("details_open", "details", 1)
    tok.markup, tok.block, tok.info, tok.map = "+++", True, title, [start_line, next_line]
    state.md.block.tokenize(state, start_line + 1, next_line)
    tok = state.push("details_close", "details", -1)
    tok.markup, tok.block = "+++", True
    state.parentType, state.lineMax = old_parent, old_max
    state.line = next_line + (1 if closed else 0)
    return True


def _render_details_open(self, tokens, idx, options, env):
    return f'<details><summary>{escapeHtml(tokens[idx].info)}</summary><div data-type="detailsContent">\n'


def _render_details_close(self, tokens, idx, options, env):
    return "</div></details>\n"


# ---------------------------------------------------------------------------- task lists

_TASK = re.compile(r"^\[([ xX])\](?=\s|$)")


def _list_items(tokens: list[Token], open_idx: int) -> tuple[list[int], int]:
    """Indices of a list's own items and of its closing token."""
    level, items, i = tokens[open_idx].level, [], open_idx + 1
    while i < len(tokens) and not (tokens[i].level == level and tokens[i].nesting == -1):
        if tokens[i].type == "list_item_open" and tokens[i].level == level + 1:
            items.append(i)
        i += 1
    return items, i


def _strip_prefix(inline: Token, n: int) -> None:
    inline.content = inline.content[n:].lstrip(" \t")
    rest = n
    for child in inline.children or []:
        if rest <= 0 or child.type != "text":
            break
        take = min(rest, len(child.content))
        child.content, rest = child.content[take:], rest - take
    first = next((c for c in inline.children or [] if c.type == "text" and c.content), None)
    if first is not None:
        first.content = first.content.lstrip(" \t")


def _tasks(state) -> None:
    tokens = state.tokens
    for i, tok in enumerate(tokens):
        if tok.type != "bullet_list_open":
            continue
        items, _ = _list_items(tokens, i)
        marks = []
        for it in items:
            para, inline = tokens[it + 1], tokens[it + 2] if it + 2 < len(tokens) else None
            m = _TASK.match(inline.content) if para.type == "paragraph_open" and inline is not None \
                and inline.type == "inline" else None
            if not m:
                break
            marks.append((it, inline, m))
        if not items or len(marks) != len(items):
            continue
        tok.attrSet("data-type", "taskList")
        for it, inline, m in marks:
            close = it + 1
            while not (tokens[close].type == "list_item_close" and tokens[close].level == tokens[it].level):
                close += 1
            tokens[it].meta["task"] = tokens[close].meta["task"] = m.group(1) != " "
            _strip_prefix(inline, m.end())


def _render_item_open(self, tokens, idx, options, env):
    if "task" not in tokens[idx].meta:
        return self.renderToken(tokens, idx, options, env)
    on = tokens[idx].meta["task"]
    return (f'<li data-type="taskItem" data-checked="{"true" if on else "false"}">'
            f'<label><input type="checkbox" disabled{" checked" if on else ""}></label><div>')


def _render_item_close(self, tokens, idx, options, env):
    if "task" in tokens[idx].meta:
        return "</div></li>\n"
    return self.renderToken(tokens, idx, options, env)


# -------------------------------------------------------------- formats that may not overlap

_MARK_OF = {"strong": "bold", "em": "italic", "s": "strike", "underline": "underline", "link": "link"}
# The editor's ProseMirror `excludes`: the key, when present, keeps the listed marks off the same text.
EXCLUDES = {
    "code": {"bold", "italic", "strike", "underline"},
    "link": {"code", "underline"},
    "strike": {"underline"},
}


def _excludes(a: str, b: str) -> bool:
    return a == b or b in EXCLUDES.get(a, ())


def _add_mark(marks: list[tuple[str, Any]], mark: tuple[str, Any]) -> list[tuple[str, Any]]:
    """ProseMirror's Mark.addToSet, on (name, source token) pairs."""
    kept = []
    for other in marks:
        if _excludes(mark[0], other[0]):
            continue
        if _excludes(other[0], mark[0]):
            return marks
        kept.append(other)
    return kept + [mark]


def _normalize_inline(children: list[Token], in_item: bool) -> list[Token]:
    stack: list[tuple[str, Token]] = []
    leaves: list[tuple[Token, list[tuple[str, Any]]]] = []
    changed = False
    for t in children:
        base = t.type[:-5] if t.type.endswith("_open") else t.type[:-6] if t.type.endswith("_close") else None
        if base in _MARK_OF and t.nesting == 1:
            stack.append((_MARK_OF[base], t))
        elif base in _MARK_OF and t.nesting == -1:
            for k in range(len(stack) - 1, -1, -1):
                if stack[k][0] == _MARK_OF[base]:
                    del stack[k]
                    break
        else:
            marks: list[tuple[str, Any]] = []
            wanted = [m for m in stack if not (in_item and m[0] == "underline")]
            for m in wanted:
                marks = _add_mark(marks, m)
            if t.type == "code_inline":
                marks = _add_mark(marks, ("code", None))
                if ("code", None) not in marks:
                    plain = Token("text", "", 0)
                    plain.content = t.content
                    t, changed = plain, True
            marks = [m for m in marks if m[0] != "code"]
            if len(marks) != len(stack):
                changed = True
            leaves.append((t, marks))
    if not changed:
        return children
    out: list[Token] = []
    open_: list[tuple[str, Any]] = []
    for leaf, marks in leaves + [(None, [])]:
        keep = 0
        while keep < min(len(open_), len(marks)) and open_[keep][1] is marks[keep][1]:
            keep += 1
        for name, src in reversed(open_[keep:]):
            close = Token(src.type[:-5] + "_close", src.tag, -1)
            close.markup = src.markup
            out.append(close)
        for name, src in marks[keep:]:
            again = Token(src.type, src.tag, 1)
            again.attrs, again.markup, again.info = dict(src.attrs), src.markup, src.info
            out.append(again)
        open_ = list(marks)
        if leaf is not None:
            out.append(leaf)
    return out


def _formats(state) -> None:
    depth = 0
    for tok in state.tokens:
        if tok.type == "list_item_open":
            depth += 1
        elif tok.type == "list_item_close":
            depth -= 1
        elif tok.type == "inline" and tok.children:
            tok.children = _normalize_inline(tok.children, depth > 0)


# ------------------------------------------------------------------------------------ links

def _render_link_open(self, tokens, idx, options, env):
    tok = tokens[idx]
    if re.match(r"^https?:", tok.attrGet("href") or "", re.I):
        tok.attrSet("target", "_blank")
        tok.attrSet("rel", "noopener noreferrer")
    return self.renderToken(tokens, idx, options, env)


# ------------------------------------------------------------------------------- the parser

def _build() -> MarkdownIt:
    m = MarkdownIt("commonmark", {"html": False, "breaks": True, "linkify": False, "typographer": False})
    m.enable(["strikethrough", "table"])
    m.inline.__class__ = _CJKParserInline
    m.validateLink = is_safe_link
    m.inline.ruler.before("emphasis", "underline", _underline_tokenize)
    m.inline.ruler2.before("emphasis", "underline", _underline_post)
    m.block.ruler.before("fence", "details", _details, {"alt": ["paragraph", "reference", "blockquote", "list"]})
    m.core.ruler.push("rl_tasks", _tasks)
    m.core.ruler.push("rl_formats", _formats)
    m.add_render_rule("details_open", _render_details_open)
    m.add_render_rule("details_close", _render_details_close)
    m.add_render_rule("list_item_open", _render_item_open)
    m.add_render_rule("list_item_close", _render_item_close)
    m.add_render_rule("link_open", _render_link_open)
    return m


md = _build()


def render(text: str | None) -> Markup:
    """Safe HTML for an issue description or comment; wrap it in `<div class="rl-md">`."""
    if not text or not text.strip():
        return Markup("")
    return Markup(md.render(text))
