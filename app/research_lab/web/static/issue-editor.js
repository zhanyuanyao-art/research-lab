// Rich editor for issue descriptions and comments (card 0096; docs/design/ISSUES-LINEAR.md §2.2, §8).
// Needs vendor/tiptap-editor.min.js (window.RLEditor) and vendor/issue-editor.css. Without them the
// textarea it would have been mounted on stays a working plain field.
//
//   var ed = RLIssueEditor.mount(el, {
//     markdown: '…',             // initial text; default: el.value (textarea) or el.textContent
//     placeholder: '添加描述…',   // default: el.placeholder or el.dataset.placeholder
//     onSave: function (md) {},   // after saveDelay of no typing, on blur, ⌘↩ and ⌘S; only when the
//                                 // text changed. May return a promise; a rejection marks it unsaved.
//     onChange: function (md) {}, // every change
//     saveDelay: 800,
//   });
//   ed.getMarkdown()  ed.setMarkdown(md)  ed.flush()  ed.focus()  ed.destroy()  ed.editor (TipTap)
//
// Mounted on a <textarea>, the textarea is hidden and kept in sync, so its form still posts Markdown;
// `rl:editor-change` and `rl:editor-save` (detail.markdown) fire on it. The slash menu's 附件 and 关联文献 fire
// `rl:editor-command` (detail.command 'attach' | 'link-material') on the same element for the page to handle. Loading a text never saves it:
// a stored `* item` stays as it is until the user edits that field.
//
// Storage is Markdown (§8.2). Parsing uses markdown-it with the same rules as files/markdown.py, so the
// page and the editor read a text the same way; keep both sides in step. Blocks the editor cannot
// represent (tables, images) are kept verbatim as a read-only box rather than lost.
//
// Which formats may combine (Leo 10-10) is enforced by the schema and a normaliser, not by the toolbar:
//   blocks    one kind per block: paragraph (or heading), bullet list, ordered list, task list, quote,
//             code block. List items hold one paragraph plus an optional nested list of the same kind;
//             quotes hold paragraphs; a collapse holds paragraphs and lists and only sits at top level.
//   marks     code excludes bold/italic/strike/underline and they exclude it; a link excludes code and
//             underline; strike and underline exclude each other; no underline inside list items;
//             code blocks and collapse titles take no marks; bold, italic and link stack freely.
// The toolbar greys out what does not apply (buttons keep their place) and says why in the tooltip.
(function () {
  'use strict';
  var R = window.RLEditor;
  if (!R || window.RLIssueEditor) return;
  var core = R.core;
  var PMState = R.state, PMModel = R.model;
  var TextSelection = PMState.TextSelection, NodeSelection = PMState.NodeSelection;

  var MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');
  var MOD = MAC ? '⌘' : 'Ctrl+', SHIFT = MAC ? '⇧' : 'Shift+', ALT = MAC ? '⌥' : 'Alt+';

  // Lucide icons (ISC licence, same set as icons.svg), inlined so this file needs no sprite edits.
  var ICONS = {
    bold: '<path d="M6 12h9a4 4 0 0 1 0 8H7a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h7a4 4 0 0 1 0 8"/>',
    italic: '<line x1="19" x2="10" y1="4" y2="4"/><line x1="14" x2="5" y1="20" y2="20"/><line x1="15" x2="9" y1="4" y2="20"/>',
    strike: '<path d="M16 4H9a3 3 0 0 0-2.83 4"/><path d="M14 12a4 4 0 0 1 0 8H6"/><line x1="4" x2="20" y1="12" y2="12"/>',
    underline: '<path d="M6 4v6a6 6 0 0 0 12 0V4"/><line x1="4" x2="20" y1="20" y2="20"/>',
    link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    unlink: '<path d="m18.84 12.25 1.72-1.71h-.02a5.004 5.004 0 0 0-.12-7.07 5.006 5.006 0 0 0-6.95 0l-1.72 1.71"/><path d="m5.17 11.75-1.71 1.71a5.004 5.004 0 0 0 .12 7.07 5.006 5.006 0 0 0 6.95 0l1.71-1.71"/><line x1="8" x2="8" y1="2" y2="5"/><line x1="2" x2="5" y1="8" y2="8"/><line x1="16" x2="16" y1="19" y2="22"/><line x1="19" x2="22" y1="16" y2="16"/>',
    code: '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
    chevron: '<path d="m6 9 6 6 6-6"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    paragraph: '<path d="M13 4v16"/><path d="M17 4v16"/><path d="M19 4H9.5a4.5 4.5 0 0 0 0 9H13"/>',
    h1: '<path d="M4 12h8"/><path d="M4 18V6"/><path d="M12 18V6"/><path d="m17 12 3-2v8"/>',
    h2: '<path d="M4 12h8"/><path d="M4 18V6"/><path d="M12 18V6"/><path d="M21 18h-4c0-4 4-3 4-6 0-1.5-2-2.5-4-1"/>',
    h3: '<path d="M4 12h8"/><path d="M4 18V6"/><path d="M12 18V6"/><path d="M17.5 10.5c1.7-1 3.5 0 3.5 1.5a2 2 0 0 1-2 2"/><path d="M17 17.5c2 1.5 4 .3 4-1.5a2 2 0 0 0-2-2"/>',
    bullet: '<path d="M3 12h.01"/><path d="M3 18h.01"/><path d="M3 6h.01"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M8 6h13"/>',
    ordered: '<path d="M10 12h11"/><path d="M10 18h11"/><path d="M10 6h11"/><path d="M4 10h2"/><path d="M4 6h1v4"/><path d="M6 18H4c0-1 2-2 2-3s-1-1.5-2-1"/>',
    task: '<rect x="3" y="5" width="6" height="6" rx="1"/><path d="m3 17 2 2 4-4"/><path d="M13 6h8"/><path d="M13 12h8"/><path d="M13 18h8"/>',
    quote: '<path d="M17 6H3"/><path d="M21 12H8"/><path d="M21 18H8"/><path d="M3 12v6"/>',
    codeblock: '<path d="M10 9.5 8 12l2 2.5"/><path d="m14 9.5 2 2.5-2 2.5"/><rect width="18" height="18" x="3" y="3" rx="2"/>',
    details: '<path d="m7 20 5-5 5 5"/><path d="m7 4 5 5 5-5"/>'
  };
  function svg(name) {
    return '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">' + ICONS[name] + '</svg>';
  }

  // ------------------------------------------------------------------------------- links

  // Same policy as files/markdown.py is_safe_link: http(s) and paths inside the app.
  function isSafeLink(url) {
    var u = String(url || '').trim();
    if (!u || /^(\/\/|\\|\/\\)/.test(u)) return false;
    var head = u.split(/[/?#]/, 1)[0];
    if (head.indexOf(':') < 0) return true;
    var m = /^([A-Za-z][A-Za-z0-9+.\-]*):/.exec(head);
    return !!m && /^https?$/i.test(m[1]);
  }

  // What the link field accepts: a full URL, an in-app path, or a bare domain (gets https://).
  function normaliseHref(text) {
    var u = String(text || '').trim();
    if (!u) return '';
    if (!/^[A-Za-z][A-Za-z0-9+.\-]*:/.test(u) && !/^[/#?]/.test(u) && /^[^\s/]+\.[^\s/]{2,}/.test(u)) u = 'https://' + u;
    u = u.replace(/\s/g, '%20');
    return isSafeLink(u) ? u : null;
  }

  // ------------------------------------------------------------------- Markdown → HTML

  var CJK_RANGES = [[0x1100, 0x11FF], [0x2E80, 0x2FFF], [0x3000, 0x303F], [0x3040, 0x30FF], [0x3100, 0x31FF],
    [0x3200, 0x33FF], [0x3400, 0x4DBF], [0x4E00, 0x9FFF], [0xA960, 0xA97F], [0xAC00, 0xD7FF],
    [0xF900, 0xFAFF], [0xFE30, 0xFE4F], [0xFF00, 0xFFEF], [0x20000, 0x3FFFF]];
  function isCJK(cp) {
    for (var i = 0; i < CJK_RANGES.length; i++) if (cp >= CJK_RANGES[i][0] && cp <= CJK_RANGES[i][1]) return true;
    return false;
  }

  function buildMarkdown(forEditor, linkify) {
    var md = new R.MarkdownIt('commonmark', { html: false, breaks: true, linkify: !!linkify, typographer: false });
    md.enable(['strikethrough', 'table']);
    if (linkify) md.enable('linkify');
    md.validateLink = isSafeLink;
    var u = md.utils;

    // CJK-aware delimiters, mirrored from files/markdown.py. Code points, not UTF-16 units, so astral
    // characters classify as they do in Python.
    var State = class extends md.inline.State {};
    State.prototype.scanDelims = function (start, canSplitWord) {
      var src = this.src, max = this.posMax, marker = src.charCodeAt(start), pos = start;
      var last = start > 0 ? src.codePointAt(start - 1) : 0x20;
      if (start > 1 && last >= 0xDC00 && last <= 0xDFFF) last = src.codePointAt(start - 2);
      while (pos < max && src.charCodeAt(pos) === marker) pos++;
      var next = pos < max ? src.codePointAt(pos) : 0x20;
      var lastP = u.isMdAsciiPunct(last) || u.isPunctChar(String.fromCodePoint(last));
      var nextP = u.isMdAsciiPunct(next) || u.isPunctChar(String.fromCodePoint(next));
      var lastW = u.isWhiteSpace(last), nextW = u.isWhiteSpace(next);
      var left = !nextW && (!nextP || lastW || lastP || isCJK(last));
      var right = !lastW && (!lastP || nextW || nextP || isCJK(next));
      return {
        can_open: left && (canSplitWord || !right || lastP),
        can_close: right && (canSplitWord || !left || nextP),
        length: pos - start
      };
    };
    md.inline.State = State;

    md.inline.ruler.before('emphasis', 'underline', underlineTokenize);
    md.inline.ruler2.before('emphasis', 'underline', underlinePost);
    md.block.ruler.before('fence', 'details', detailsRule, { alt: ['paragraph', 'reference', 'blockquote', 'list'] });
    md.core.ruler.push('rl_tasks', tasksRule);
    md.core.ruler.push('rl_formats', formatsRule);
    if (forEditor) md.core.ruler.push('rl_raw', rawRule);

    var rules = md.renderer.rules;
    rules.details_open = function (tokens, idx) {
      return '<details><summary>' + u.escapeHtml(tokens[idx].info) + '</summary><div data-type="detailsContent">\n';
    };
    rules.details_close = function () { return '</div></details>\n'; };
    rules.list_item_open = function (tokens, idx, options, env, self) {
      var meta = tokens[idx].meta;
      if (!meta || !('task' in meta)) return self.renderToken(tokens, idx, options);
      return '<li data-type="taskItem" data-checked="' + (meta.task ? 'true' : 'false') + '"><label><input type="checkbox" disabled' +
        (meta.task ? ' checked' : '') + '></label><div>';
    };
    rules.list_item_close = function (tokens, idx, options, env, self) {
      var meta = tokens[idx].meta;
      return meta && 'task' in meta ? '</div></li>\n' : self.renderToken(tokens, idx, options);
    };
    if (!forEditor) {
      rules.link_open = function (tokens, idx, options, env, self) {
        if (/^https?:/i.test(tokens[idx].attrGet('href') || '')) {
          tokens[idx].attrSet('target', '_blank');
          tokens[idx].attrSet('rel', 'noopener noreferrer');
        }
        return self.renderToken(tokens, idx, options);
      };
    }
    if (forEditor) {
      // The code block keeps no trailing newline, or every round trip would add an empty line.
      rules.fence = rules.code_block = function (tokens, idx) {
        var t = tokens[idx], lang = (t.info || '').trim().split(/\s+/)[0];
        return '<pre><code' + (lang ? ' class="language-' + u.escapeHtml(lang) + '"' : '') + '>' +
          u.escapeHtml(t.content.replace(/\n$/, '')) + '</code></pre>\n';
      };
      rules.rl_raw = function (tokens, idx) {
        var s = u.escapeHtml(tokens[idx].content);
        return '<div data-rl-raw="" data-source="' + s + '"><pre>' + s + '</pre></div>\n';
      };
    }
    return md;
  }

  // ++underline++, a port of strikethrough with `+` (markdown-it-ins does the same as <ins>).
  function underlineTokenize(state, silent) {
    var start = state.pos;
    if (silent || state.src.charCodeAt(start) !== 0x2B) return false;
    var scanned = state.scanDelims(start, true), len = scanned.length;
    if (len < 2) return false;
    if (len % 2) { state.push('text', '', 0).content = '+'; len--; }
    for (var i = 0; i < len; i += 2) {
      state.push('text', '', 0).content = '++';
      state.delimiters.push({ marker: 0x2B, length: 0, token: state.tokens.length - 1, end: -1, open: scanned.can_open, close: scanned.can_close });
    }
    state.pos += scanned.length;
    return true;
  }
  function underlinePairs(state, delimiters) {
    var lone = [];
    for (var i = 0; i < delimiters.length; i++) {
      var d = delimiters[i];
      if (d.marker !== 0x2B || d.end === -1) continue;
      var end = delimiters[d.end];
      [[d.token, 'underline_open', 1], [end.token, 'underline_close', -1]].forEach(function (x) {
        var t = state.tokens[x[0]];
        t.type = x[1]; t.tag = 'u'; t.nesting = x[2]; t.markup = '++'; t.content = '';
      });
      var before = state.tokens[end.token - 1];
      if (before.type === 'text' && before.content === '+') lone.push(end.token - 1);
    }
    while (lone.length) {
      var a = lone.pop(), j = a + 1;
      while (j < state.tokens.length && state.tokens[j].type === 'underline_close') j++;
      j--;
      if (a !== j) { var tmp = state.tokens[j]; state.tokens[j] = state.tokens[a]; state.tokens[a] = tmp; }
    }
  }
  function underlinePost(state) {
    underlinePairs(state, state.delimiters);
    (state.tokens_meta || []).forEach(function (m) { if (m && m.delimiters) underlinePairs(state, m.delimiters); });
  }

  // +++ title … +++ (collapse), top level only.
  function detailsRule(state, startLine, endLine, silent) {
    if (state.level > 0 || state.sCount[startLine] - state.blkIndent >= 4) return false;
    var pos = state.bMarks[startLine] + state.tShift[startLine];
    var line = state.src.slice(pos, state.eMarks[startLine]);
    if (line.slice(0, 3) !== '+++' || (line.length > 3 && line[3] !== ' ' && line[3] !== '\t')) return false;
    if (silent) return true;
    var next = startLine, closed = false;
    for (;;) {
      next++;
      if (next >= endLine) break;
      var p = state.bMarks[next] + state.tShift[next], m = state.eMarks[next];
      if (p < m && state.sCount[next] < state.blkIndent) break;
      if (state.sCount[next] - state.blkIndent < 4 && state.src.slice(p, m).replace(/\s+$/, '') === '+++') { closed = true; break; }
    }
    var oldParent = state.parentType, oldMax = state.lineMax;
    state.parentType = 'details'; state.lineMax = next;
    var tok = state.push('details_open', 'details', 1);
    tok.markup = '+++'; tok.block = true; tok.info = line.slice(3).trim(); tok.map = [startLine, next];
    state.md.block.tokenize(state, startLine + 1, next);
    tok = state.push('details_close', 'details', -1);
    tok.markup = '+++'; tok.block = true;
    state.parentType = oldParent; state.lineMax = oldMax;
    state.line = next + (closed ? 1 : 0);
    return true;
  }

  // A bullet list is a task list when every item starts with [ ] or [x].
  var TASK = /^\[([ xX])\](?=\s|$)/;
  function tasksRule(state) {
    var tokens = state.tokens;
    for (var i = 0; i < tokens.length; i++) {
      if (tokens[i].type !== 'bullet_list_open') continue;
      var level = tokens[i].level, items = [], k = i + 1;
      for (; k < tokens.length && !(tokens[k].level === level && tokens[k].nesting === -1); k++) {
        if (tokens[k].type === 'list_item_open' && tokens[k].level === level + 1) items.push(k);
      }
      var found = [];
      for (var n = 0; n < items.length; n++) {
        var it = items[n], para = tokens[it + 1], inline = tokens[it + 2];
        var m = para && para.type === 'paragraph_open' && inline && inline.type === 'inline' ? TASK.exec(inline.content) : null;
        if (!m) break;
        found.push([it, inline, m]);
      }
      if (!items.length || found.length !== items.length) continue;
      tokens[i].attrSet('data-type', 'taskList');
      found.forEach(function (f) {
        var open = tokens[f[0]], close = f[0] + 1;
        while (!(tokens[close].type === 'list_item_close' && tokens[close].level === open.level)) close++;
        open.meta = open.meta || {}; tokens[close].meta = tokens[close].meta || {};
        open.meta.task = tokens[close].meta.task = f[2][1] !== ' ';
        stripPrefix(f[1], f[2][0].length);
      });
    }
  }
  function stripPrefix(inline, n) {
    inline.content = inline.content.slice(n).replace(/^[ \t]+/, '');
    var rest = n, kids = inline.children || [];
    for (var i = 0; i < kids.length && rest > 0 && kids[i].type === 'text'; i++) {
      var take = Math.min(rest, kids[i].content.length);
      kids[i].content = kids[i].content.slice(take);
      rest -= take;
    }
    for (var j = 0; j < kids.length; j++) {
      if (kids[j].type === 'text' && kids[j].content) { kids[j].content = kids[j].content.replace(/^[ \t]+/, ''); break; }
    }
  }

  // Overlapping formats resolve as ProseMirror's Mark.addToSet does with the schema's `excludes`.
  var MARK_OF = { strong: 'bold', em: 'italic', s: 'strike', underline: 'underline', link: 'link' };
  var EXCLUDES = { code: ['bold', 'italic', 'strike', 'underline'], link: ['code', 'underline'], strike: ['underline'] };
  function excludes(a, b) { return a === b || (EXCLUDES[a] || []).indexOf(b) >= 0; }
  function addMark(marks, mark) {
    var kept = [];
    for (var i = 0; i < marks.length; i++) {
      if (excludes(mark[0], marks[i][0])) continue;
      if (excludes(marks[i][0], mark[0])) return marks;
      kept.push(marks[i]);
    }
    kept.push(mark);
    return kept;
  }
  function normalizeInline(children, inItem, Token) {
    var stack = [], leaves = [], changed = false;
    children.forEach(function (t) {
      var base = /_open$/.test(t.type) ? t.type.slice(0, -5) : /_close$/.test(t.type) ? t.type.slice(0, -6) : null;
      if (base && MARK_OF[base] && t.nesting === 1) { stack.push([MARK_OF[base], t]); return; }
      if (base && MARK_OF[base] && t.nesting === -1) {
        for (var k = stack.length - 1; k >= 0; k--) if (stack[k][0] === MARK_OF[base]) { stack.splice(k, 1); break; }
        return;
      }
      var marks = [];
      stack.forEach(function (m) { if (!(inItem && m[0] === 'underline')) marks = addMark(marks, m); });
      if (t.type === 'code_inline') {
        var code = ['code', null];
        marks = addMark(marks, code);
        if (marks.indexOf(code) < 0) {
          var plain = new Token('text', '', 0);
          plain.content = t.content;
          t = plain; changed = true;
        }
      }
      marks = marks.filter(function (m) { return m[0] !== 'code'; });
      if (marks.length !== stack.length) changed = true;
      leaves.push([t, marks]);
    });
    if (!changed) return children;
    var out = [], open = [];
    leaves.concat([[null, []]]).forEach(function (pair) {
      var leaf = pair[0], marks = pair[1], keep = 0;
      while (keep < Math.min(open.length, marks.length) && open[keep][1] === marks[keep][1]) keep++;
      for (var i = open.length - 1; i >= keep; i--) {
        var src = open[i][1], close = new Token(src.type.slice(0, -5) + '_close', src.tag, -1);
        close.markup = src.markup;
        out.push(close);
      }
      for (var j = keep; j < marks.length; j++) {
        var s = marks[j][1], again = new Token(s.type, s.tag, 1);
        again.attrs = s.attrs ? s.attrs.map(function (a) { return a.slice(); }) : null;
        again.markup = s.markup; again.info = s.info;
        out.push(again);
      }
      open = marks.slice();
      if (leaf) out.push(leaf);
    });
    return out;
  }
  function formatsRule(state) {
    var depth = 0;
    state.tokens.forEach(function (tok) {
      if (tok.type === 'list_item_open') depth++;
      else if (tok.type === 'list_item_close') depth--;
      else if (tok.type === 'inline' && tok.children) tok.children = normalizeInline(tok.children, depth > 0, state.Token);
    });
  }

  // Editor only: top-level blocks with tables or images travel as verbatim source.
  function rawRule(state) {
    var tokens = state.tokens, out = [], lines = null;
    for (var i = 0; i < tokens.length; i++) {
      var t = tokens[i];
      if (t.level !== 0 || t.nesting === -1 || !t.map) { out.push(t); continue; }
      var end = i;
      if (t.nesting === 1) { while (end < tokens.length && !(tokens[end].level === 0 && tokens[end].nesting === -1)) end++; }
      var needsRaw = false;
      for (var k = i; k <= end && !needsRaw; k++) {
        var x = tokens[k];
        if (x.type === 'table_open') needsRaw = true;
        if (x.type === 'inline' && (x.children || []).some(function (c) { return c.type === 'image'; })) needsRaw = true;
      }
      if (!needsRaw) { for (var c = i; c <= end; c++) out.push(tokens[c]); i = end; continue; }
      lines = lines || state.src.split('\n');
      var raw = new state.Token('rl_raw', 'div', 0);
      raw.block = true;
      raw.content = lines.slice(t.map[0], t.map[1]).join('\n').replace(/\s+$/, '');
      out.push(raw);
      i = end;
    }
    state.tokens = out;
  }

  var mdView = buildMarkdown(false), mdEditor = buildMarkdown(true), mdPaste = buildMarkdown(true, true);
  function toHTML(markdown) { return mdEditor.render(String(markdown || '')); }

  // ------------------------------------------------------------------- doc → Markdown

  function backticksFor(node, side) {
    var ticks = /`+/g, m, len = 0;
    if (node.isText) while ((m = ticks.exec(node.text))) len = Math.max(len, m[0].length);
    var result = len > 0 && side > 0 ? ' `' : '`';
    for (var i = 0; i < len; i++) result += '`';
    if (len > 0 && side < 0) result += ' ';
    return result;
  }
  function isPlainURL(link, parent, index) {
    if (link.attrs.title || !/^https?:/i.test(link.attrs.href)) return false;
    var content = parent.child(index);
    if (!content.isText || content.text !== link.attrs.href || content.marks[content.marks.length - 1] !== link) return false;
    return index === parent.childCount - 1 || !link.isInSet(parent.child(index + 1).marks);
  }
  // Lists sitting side by side alternate their marker, or CommonMark would read them as one list.
  function prevIsList(parent, index, names) {
    var prev = index > 0 ? parent.child(index - 1) : null;
    return !!prev && names.indexOf(prev.type.name) >= 0;
  }

  // prosemirror-markdown ships ES classes, so subclass with `class` rather than a constructor function.
  var MdState = class extends R.MarkdownSerializerState {};
  MdState.prototype.esc = function (str, startOfLine) {
    // Also at the start of a line after a single-newline break, which prosemirror-markdown misses.
    var atLine = startOfLine || this.out === this.delim || this.out.slice(-(this.delim.length + 1)) === '\n' + this.delim;
    str = str.replace(/[`*\\~\[\]_]/g, function (m, i) {
      return m === '_' && i > 0 && i + 1 < str.length && /\w/.test(str[i - 1]) && /\w/.test(str[i + 1]) ? m : '\\' + m;
    });
    str = str.replace(/\+(?=\+)|^\+|\+$/g, '\\+')          // ++ is underline
      .replace(/&(?=#?[A-Za-z0-9]+;)/g, '\\&')               // entities
      .replace(/<(?=[A-Za-z/!?])/g, '\\<');                  // autolinks
    if (atLine) {
      str = str.replace(/^[ \t]+/, '')
        .replace(/^([\-*>]|\+ )/, '\\$1')
        .replace(/^(#{1,6})(\s|$)/, '\\$1$2')
        .replace(/^(\d+)([.)])(\s|$)/, '$1\\$2$3')
        .replace(/^=+\s*$/, function (m) { return '\\' + m; });
    }
    return str;
  };

  var NODES = {
    paragraph: function (state, node) { state.renderInline(node); state.closeBlock(node); },
    heading: function (state, node) {
      state.write(state.repeat('#', node.attrs.level) + ' ');
      state.renderInline(node, false);
      if (/#$/.test(state.out) && !/`$/.test(state.out)) state.out = state.out.slice(0, -1) + '\\#';
      state.closeBlock(node);
    },
    blockquote: function (state, node) { state.wrapBlock('> ', null, node, function () { state.renderContent(node); }); },
    codeBlock: function (state, node) {
      var backticks = node.textContent.match(/`{3,}/gm);
      var fence = backticks ? backticks.sort().slice(-1)[0] + '`' : '```';
      state.write(fence + (node.attrs.language || '') + '\n');
      state.text(node.textContent, false);
      state.write('\n');
      state.write(fence);
      state.closeBlock(node);
    },
    horizontalRule: function (state, node) { state.write('---'); state.closeBlock(node); },
    bulletList: function (state, node, parent, index) {
      var ch = prevIsList(parent, index, ['bulletList', 'taskList']) ? '*' : '-';
      state.renderList(node, '  ', function () { return ch + ' '; });
    },
    taskList: function (state, node, parent, index) {
      var ch = prevIsList(parent, index, ['bulletList', 'taskList']) ? '*' : '-';
      state.renderList(node, '  ', function (i) { return ch + (node.child(i).attrs.checked ? ' [x] ' : ' [ ] '); });
    },
    orderedList: function (state, node, parent, index) {
      var start = node.attrs.start || 1, maxW = String(start + node.childCount - 1).length;
      var delim = prevIsList(parent, index, ['orderedList']) ? ') ' : '. ';
      state.renderList(node, state.repeat(' ', maxW + 2), function (i) {
        var n = String(start + i);
        return state.repeat(' ', maxW - n.length) + n + delim;
      });
    },
    listItem: function (state, node) { state.renderContent(node); },
    taskItem: function (state, node) { state.renderContent(node); },
    details: function (state, node) {
      var title = node.firstChild.textContent.replace(/\s+/g, ' ').trim();
      state.write('+++' + (title ? ' ' + title : ''));
      state.ensureNewLine();
      state.renderContent(node.lastChild);
      state.flushClose(1);
      state.write('+++');
      state.closeBlock(node);
    },
    rawBlock: function (state, node) { state.text(node.attrs.source, false); state.closeBlock(node); },
    hardBreak: function (state, node, parent, index) {
      for (var i = index + 1; i < parent.childCount; i++) {
        if (parent.child(i).type !== node.type) {
          // A single newline is a break here (`breaks`); a backslash keeps a leading or repeated one.
          var prev = index > 0 ? parent.child(index - 1) : null;
          state.write(!prev || prev.type === node.type ? '\\\n' : '\n');
          return;
        }
      }
    },
    text: function (state, node) { state.text(node.text, !state.inAutolink); }
  };
  var MARKS = {
    bold: { open: '**', close: '**', mixable: true, expelEnclosingWhitespace: true },
    italic: { open: '*', close: '*', mixable: true, expelEnclosingWhitespace: true },
    strike: { open: '~~', close: '~~', mixable: true, expelEnclosingWhitespace: true },
    underline: { open: '++', close: '++', mixable: true, expelEnclosingWhitespace: true },
    link: {
      open: function (state, mark, parent, index) {
        state.inAutolink = isPlainURL(mark, parent, index);
        return state.inAutolink ? '<' : '[';
      },
      close: function (state, mark) {
        var auto = state.inAutolink;
        state.inAutolink = undefined;
        if (auto) return '>';
        var href = String(mark.attrs.href || '').replace(/[\s<>]/g, encodeURIComponent).replace(/[()\\]/g, '\\$&');
        return '](' + href + ')';
      },
      mixable: true
    },
    code: {
      open: function (state, mark, parent, index) { return backticksFor(parent.child(index), -1); },
      close: function (state, mark, parent, index) { return backticksFor(parent.child(index - 1), 1); },
      escape: false
    }
  };
  function toMarkdown(doc) {
    var state = new MdState(NODES, MARKS, { tightLists: true, hardBreakNodeName: 'hardBreak', strict: true });
    state.renderContent(doc);
    return state.out.replace(/\s+$/, '');
  }

  // --------------------------------------------------------------------------- schema

  function textblocksIn(state) {
    var sel = state.selection, out = [];
    if (sel.empty) return [{ node: sel.$from.parent, $pos: sel.$from }];
    state.doc.nodesBetween(sel.from, sel.to, function (node, pos) {
      if (node.isTextblock) { out.push({ node: node, $pos: state.doc.resolve(pos + 1) }); return false; }
      return true;
    });
    return out;
  }
  function inListItem($pos) {
    for (var d = $pos.depth; d > 0; d--) {
      var n = $pos.node(d).type.name;
      if (n === 'listItem' || n === 'taskItem') return true;
    }
    return false;
  }
  function marksInSelection(state) {
    var names = {}, sel = state.selection;
    if (sel.empty) {
      (state.storedMarks || sel.$from.marks()).forEach(function (m) { names[m.type.name] = true; });
    } else {
      state.doc.nodesBetween(sel.from, sel.to, function (node) {
        if (node.isInline) node.marks.forEach(function (m) { names[m.type.name] = true; });
      });
    }
    return names;
  }

  var CONFLICTS = {
    bold: ['code'], italic: ['code'], strike: ['code', 'underline'], underline: ['code', 'link', 'strike'],
    link: ['code', 'underline'], code: ['bold', 'italic', 'strike', 'underline', 'link']
  };
  function conflictReason(a, b) {
    if (a === 'code' || b === 'code') return '行内代码不和其他格式叠加';
    if (a === 'link' || b === 'link') return '链接不再加下划线';
    return '下划线和删除线不叠加';
  }
  // Whether mark `name` may be toggled on the current selection, and if not, why.
  function markCheck(state, name) {
    var sel = state.selection;
    if (sel instanceof NodeSelection) return { allowed: false, reason: '这一块不能加格式' };
    var type = state.schema.marks[name], blocks = textblocksIn(state);
    if (!blocks.length) return { allowed: false, reason: '' };
    for (var i = 0; i < blocks.length; i++) {
      var b = blocks[i];
      if (!b.node.type.allowsMarkType(type)) {
        return { allowed: false, reason: b.node.type.name === 'codeBlock' ? '代码块里不加格式' : '折叠标题不加格式' };
      }
      if (name === 'underline' && inListItem(b.$pos)) return { allowed: false, reason: '列表里不用下划线' };
    }
    var present = marksInSelection(state);
    for (var j = 0; j < CONFLICTS[name].length; j++) {
      if (present[CONFLICTS[name][j]]) return { allowed: false, reason: conflictReason(name, CONFLICTS[name][j]) };
    }
    return { allowed: true, reason: '' };
  }
  // The same check for an input rule, at the text being converted.
  function markAllowedAt(state, name, range) {
    var $pos = state.doc.resolve(range.from), type = state.schema.marks[name];
    if (!$pos.parent.type.allowsMarkType(type)) return false;
    if (name === 'underline' && inListItem($pos)) return false;
    var present = {};
    $pos.marks().forEach(function (m) { present[m.type.name] = true; });
    state.doc.nodesBetween(range.from, range.to, function (node) {
      if (node.isInline) node.marks.forEach(function (m) { present[m.type.name] = true; });
    });
    return !CONFLICTS[name].some(function (c) { return present[c]; });
  }
  function guardRule(rule, name) {
    return new core.InputRule({
      find: rule.find,
      undoable: rule.undoable,
      handler: function (props) { return markAllowedAt(props.state, name, props.range) ? rule.handler(props) : null; }
    });
  }
  // Delimiters convert after whitespace, CJK text or CJK punctuation (TipTap's rules want whitespace).
  var BEFORE = '(?:^|(?<=[\\s\\u3000-\\u303f\\u3040-\\u30ff\\u3400-\\u4dbf\\u4e00-\\u9fff\\uac00-\\ud7af\\uf900-\\ufaff\\uff00-\\uffef]))';
  function delimRule(type, d, name) {
    var e = d.replace(/[*+~]/g, '\\$&'), c = d[0] === '+' ? '+' : d[0] === '~' ? '~' : d[0] === '_' ? '_' : '*';
    var find = new RegExp(BEFORE + '(' + e + '(?!\\s+' + e + ')((?:[^' + (c === '*' ? '*' : '\\' + c) + ']+))' + e + '(?!\\s+' + e + '))$');
    return guardRule(core.markInputRule({ find: find, type: type }), name);
  }
  function noKeys() { return {}; }

  var Bold = R.Bold.extend({
    addKeyboardShortcuts: noKeys,
    addInputRules: function () { return [delimRule(this.type, '**', 'bold')]; }
  });
  var Italic = R.Italic.extend({
    addKeyboardShortcuts: noKeys,
    addInputRules: function () { return [delimRule(this.type, '*', 'italic'), delimRule(this.type, '_', 'italic')]; }
  });
  var Strike = R.Strike.extend({
    excludes: 'strike underline',
    addKeyboardShortcuts: noKeys,
    addInputRules: function () { return [delimRule(this.type, '~~', 'strike')]; }
  });
  var Underline = R.Underline.extend({
    addKeyboardShortcuts: noKeys,
    addInputRules: function () { return [delimRule(this.type, '++', 'underline')]; }
  });
  var Code = R.Code.extend({
    excludes: 'code bold italic strike underline',
    addKeyboardShortcuts: noKeys,
    addInputRules: function () {
      return (this.parent ? this.parent() : []).map(function (r) { return guardRule(r, 'code'); });
    }
  });
  var Link = R.Link.extend({ excludes: 'link code underline', addKeyboardShortcuts: noKeys }).configure({
    openOnClick: false,
    autolink: true,
    linkOnPaste: false,          // handled in handlePaste so the table's rules apply
    defaultProtocol: 'https',
    isAllowedUri: function (url) { return isSafeLink(url); },
    shouldAutoLink: function (url) { return /^https?:\/\//i.test(url) || /^www\./i.test(url); },
    HTMLAttributes: { target: null, rel: null, class: null }
  });

  var Blockquote = R.Blockquote.extend({ content: 'paragraph+', addKeyboardShortcuts: noKeys });
  var Heading = R.Heading.extend({ addKeyboardShortcuts: noKeys }).configure({ levels: [1, 2, 3, 4, 5, 6] });
  var Paragraph = R.Paragraph.extend({ addKeyboardShortcuts: noKeys });
  // Kept so old `---` lines survive; not one of the formats, so no input rule (it could split a list).
  var HorizontalRule = R.HorizontalRule.extend({ addInputRules: function () { return []; } });
  var CodeBlock = R.CodeBlock.configure({ languageClassPrefix: 'language-', exitOnTripleEnter: true, exitOnArrowDown: true });
  var BulletList = R.BulletList.extend({ addKeyboardShortcuts: noKeys });
  var OrderedList = R.OrderedList.extend({ addKeyboardShortcuts: noKeys });
  var ListItem = R.ListItem.extend({ content: 'paragraph (bulletList | orderedList)?' });
  var TaskList = R.TaskList.extend({ addKeyboardShortcuts: noKeys });
  var TaskItem = R.TaskItem.configure({
    nested: true,
    a11y: { checkboxLabel: function (node, checked) { return (checked ? '已完成：' : '待办：') + (node.textContent || '空白待办'); } }
  }).extend({ content: 'paragraph taskList?' });
  var Details = R.Details.configure({
    persist: true,
    HTMLAttributes: { class: 'rl-details' },
    renderToggleButton: function (o) { o.element.setAttribute('aria-label', o.isOpen ? '收起' : '展开'); }
  });
  var DetailsSummary = R.DetailsSummary.extend({ marks: '' });
  var DetailsContent = R.DetailsContent.extend({ content: '(paragraph | bulletList | orderedList | taskList)+' });

  // A block kept as Markdown source (tables, images): shown read-only, written back unchanged.
  var RawBlock = core.Node.create({
    name: 'rawBlock',
    group: 'block',
    atom: true,
    selectable: true,
    draggable: false,
    addAttributes: function () { return { source: { default: '' } }; },
    parseHTML: function () {
      return [{ tag: 'div[data-rl-raw]', getAttrs: function (el) { return { source: el.getAttribute('data-source') || '' }; } }];
    },
    renderHTML: function (props) {
      return ['div', { 'data-rl-raw': '', class: 'rl-raw', title: '表格和图片按原文保存，这里只读' }, ['pre', {}, props.node.attrs.source]];
    }
  });

  // Keeps the invariants that content expressions cannot state: nested lists match their parent's
  // kind, no underline inside list items, neighbouring lists of one kind become one list (Markdown
  // could not keep them apart anyway).
  var Normalise = core.Extension.create({
    name: 'rlNormalise',
    addProseMirrorPlugins: function () {
      return [new PMState.Plugin({
        key: new PMState.PluginKey('rlNormalise'),
        appendTransaction: function (trs, oldState, state) {
          if (!trs.some(function (tr) { return tr.docChanged; })) return null;
          var s = state.schema, tr = state.tr, under = s.marks.underline;
          state.doc.descendants(function (node, pos) {
            var name = node.type.name;
            if (name === 'listItem' || name === 'taskItem') {
              var from = pos + 1, to = pos + node.nodeSize - 1;
              if (state.doc.rangeHasMark(from, to, under)) tr.removeMark(from, to, under);
            }
            if ((name === 'bulletList' || name === 'orderedList') && pos > 0) {
              var $p = state.doc.resolve(pos);
              if ($p.parent.type.name === 'listItem') {
                var outer = $p.node($p.depth - 1);
                if (outer.type !== node.type) tr.setNodeMarkup(pos, outer.type, outer.type.name === 'orderedList' ? { start: 1 } : null);
              }
            }
            return true;
          });
          var joins = [];
          tr.doc.descendants(function (node, pos) {
            if (node.isTextblock) return false;
            var prev = null;
            node.forEach(function (child, offset) {
              if (prev && prev.type === child.type && /^(bulletList|orderedList|taskList)$/.test(child.type.name)) joins.push(pos + 1 + offset);
              prev = child;
            });
            return true;
          });
          // doc's own children: descendants() starts below the doc
          var prevTop = null;
          tr.doc.forEach(function (child, offset) {
            if (prevTop && prevTop.type === child.type && /^(bulletList|orderedList|taskList)$/.test(child.type.name)) joins.push(offset);
            prevTop = child;
          });
          joins.sort(function (a, b) { return b - a; }).forEach(function (p) {
            if (R.transform.canJoin(tr.doc, p)) tr.join(p);
          });
          return tr.docChanged ? tr : null;
        }
      })];
    }
  });

  // Old Markdown can nest what the schema does not allow (a list inside a quote). The parser then moves
  // the list out and leaves the quote empty; drop such leftovers once, right after loading.
  function tidy(editor) {
    var spans = [];
    editor.state.doc.descendants(function (node, pos) {
      if (node.type.name === 'blockquote' && node.childCount === 1 && node.firstChild.content.size === 0) spans.push([pos, pos + node.nodeSize]);
      return !node.isTextblock;
    });
    if (!spans.length) return;
    var tr = editor.state.tr;
    spans.reverse().forEach(function (r) { tr.delete(r[0], r[1]); });
    editor.view.dispatch(tr.setMeta('addToHistory', false));
  }

  // ---------------------------------------------------------------- block conversions

  var BLOCK_KINDS = ['paragraph', 'h1', 'h2', 'h3', 'bullet', 'ordered', 'task', 'quote', 'code'];
  var IN_DETAILS = ['paragraph', 'bullet', 'ordered', 'task'];
  var LIST_OF = { bulletList: 'bullet', orderedList: 'ordered', taskList: 'task' };
  function isListKind(k) { return k === 'bullet' || k === 'ordered' || k === 'task'; }

  // The container the selection sits in (the doc, or one collapse's content) and the children touched.
  function blockScope(state) {
    var sel = state.selection, $from = sel.$from, $to = sel.$to, depth = 0;
    for (var d = Math.min($from.depth, $to.depth); d > 0; d--) {
      if ($from.node(d).type.name === 'detailsContent' && $to.node(d) === $from.node(d)) { depth = d; break; }
    }
    var container = $from.node(depth), start = depth === 0 ? 0 : $from.start(depth);
    var i0 = $from.index(depth), i1 = Math.max(i0, $to.index(depth));
    if (i1 >= container.childCount) i1 = container.childCount - 1;
    if (i1 > i0 && !sel.empty && $to.depth > depth && $to.pos === $to.start(depth + 1)) i1--;
    var from = 0, to = 0;
    container.forEach(function (child, offset, index) {
      if (index === i0) from = start + offset;
      if (index === i1) to = start + offset + child.nodeSize;
    });
    var inSummary = false;
    for (var e = $from.depth; e >= 0; e--) if ($from.node(e).type.name === 'detailsSummary') inSummary = true;
    return { container: container, depth: depth, start: start, i0: i0, i1: i1, from: from, to: to, inDetails: depth > 0, inSummary: inSummary };
  }

  function linesOf(state, scope) {
    var lines = [], block = 0, sel = state.selection;
    function hit(l) { return sel.empty ? sel.from >= l.start && sel.from <= l.end : l.end >= sel.from && l.start <= sel.to; }
    function push(l) { l.block = block; l.selected = hit(l); lines.push(l); }
    function list(node, pos, level) {
      var kind = LIST_OF[node.type.name];
      node.forEach(function (item, io) {
        var ip = pos + 1 + io;
        item.forEach(function (child, co) {
          var cp = ip + 1 + co;
          if (child.type.name === 'paragraph') {
            push({ kind: kind, level: level, content: child.content, checked: !!item.attrs.checked, start: cp + 1,
              end: cp + 1 + child.content.size, listStart: node.attrs.start });
          } else if (LIST_OF[child.type.name]) list(child, cp, level + 1);
        });
      });
    }
    scope.container.forEach(function (node, offset, index) {
      if (index < scope.i0 || index > scope.i1) return;
      var pos = scope.start + offset, name = node.type.name;
      if (name === 'paragraph' || name === 'heading') {
        push({ kind: name === 'heading' ? 'h' + node.attrs.level : 'paragraph', level: 0, content: node.content, start: pos + 1, end: pos + 1 + node.content.size });
      } else if (name === 'blockquote') {
        node.forEach(function (p, o) { push({ kind: 'quote', level: 0, content: p.content, start: pos + 2 + o, end: pos + 2 + o + p.content.size }); });
      } else if (name === 'codeBlock') {
        var off = 0;
        node.textContent.split('\n').forEach(function (t) {
          push({ kind: 'code', level: 0, text: t, lang: node.attrs.language, start: pos + 1 + off, end: pos + 1 + off + t.length });
          off += t.length + 1;
        });
      } else if (LIST_OF[name]) {
        list(node, pos, 0);
      } else {
        push({ kind: 'atom', node: node, start: pos, end: pos + node.nodeSize });
      }
      block++;
    });
    return lines;
  }

  function plainText(line) {
    if (line.text != null) return line.text;
    var s = '';
    line.content.forEach(function (n) { s += n.isText ? n.text : n.type.name === 'hardBreak' ? '\n' : ''; });
    return s;
  }
  function inlineOf(schema, line, inList) {
    if (line.text != null) return line.text ? [schema.text(line.text)] : [];
    var out = [];
    line.content.forEach(function (n) {
      if (inList && n.isText) n = n.mark(n.marks.filter(function (m) { return m.type.name !== 'underline'; }));
      out.push(n);
    });
    return out;
  }

  // Builds blocks from lines (each with .to, the kind it becomes). Returns nodes and, per line, where
  // its text lands: [node index, textblock order inside it, offset added within that textblock].
  function buildBlocks(schema, lines) {
    var N = schema.nodes, out = [], where = [], i = 0;
    function para(l, inList) { return N.paragraph.create(null, inlineOf(schema, l, inList)); }
    while (i < lines.length) {
      var l = lines[i], k = l.to;
      if (k === 'atom') { out.push(l.node); where.push(null); i++; continue; }
      if (k === 'paragraph') { where.push([out.length, 0, 0]); out.push(para(l, false)); i++; continue; }
      if (k[0] === 'h') { where.push([out.length, 0, 0]); out.push(N.heading.create({ level: +k[1] }, inlineOf(schema, l, false))); i++; continue; }
      if (k === 'quote') {
        var ps = [];
        while (i < lines.length && lines[i].to === 'quote') { where.push([out.length, ps.length, 0]); ps.push(para(lines[i], false)); i++; }
        out.push(N.blockquote.create(null, ps));
        continue;
      }
      if (k === 'code') {
        var texts = [], lang = null, off = 0;
        while (i < lines.length && lines[i].to === 'code') {
          var t = plainText(lines[i]);
          where.push([out.length, 0, off]);
          texts.push(t); off += t.length + 1; lang = lang || lines[i].lang || null; i++;
        }
        var body = texts.join('\n');
        out.push(N.codeBlock.create({ language: lang }, body ? schema.text(body) : null));
        continue;
      }
      var group = [];
      while (i < lines.length && lines[i].to === k) { group.push(lines[i]); i++; }
      var prevLevel = -1;
      group.forEach(function (g) {
        var lv = isListKind(g.kind) ? g.level : 0;
        g.lv = Math.max(0, Math.min(lv, prevLevel + 1));
        prevLevel = g.lv;
      });
      var idx = out.length;
      group.forEach(function (g, gi) { where.push([idx, gi, 0]); });
      out.push(buildList(schema, k, group, group[0].kind === 'ordered' ? group[0].listStart || 1 : 1));
    }
    return { nodes: out, where: where };
  }
  function buildList(schema, kind, items, start) {
    var N = schema.nodes, listType = { bullet: N.bulletList, ordered: N.orderedList, task: N.taskList }[kind];
    var itemType = kind === 'task' ? N.taskItem : N.listItem, children = [], j = 0;
    while (j < items.length) {
      var it = items[j++], sub = [];
      while (j < items.length && items[j].lv > it.lv) sub.push(items[j++]);
      var content = [N.paragraph.create(null, inlineOf(schema, it, true))];
      if (sub.length) content.push(buildList(schema, kind, sub, 1));
      children.push(itemType.create(kind === 'task' ? { checked: !!it.checked } : null, content));
    }
    return listType.create(kind === 'ordered' ? { start: start } : null, children);
  }

  // Position of each output textblock's content start, in document order, skipping kept atoms.
  function textblockStarts(node, nodePos) {
    var starts = [];
    if (node.isTextblock) return [nodePos + 1];
    node.descendants(function (n, p) {
      if (n.isTextblock) { starts.push(nodePos + 1 + p + 1); return false; }
      return true;
    });
    return starts;
  }

  function replaceLines(state, scope, lines) {
    var built = buildBlocks(state.schema, lines), tr = state.tr;
    tr.replaceWith(scope.from, scope.to, built.nodes);
    var nodePos = [], p = scope.from;
    built.nodes.forEach(function (n) { nodePos.push(p); p += n.nodeSize; });
    var sel = state.selection;
    function map(pos) {
      for (var i = 0; i < lines.length; i++) {
        var l = lines[i], w = built.where[i];
        if (!w || pos < l.start || pos > l.end) continue;
        var starts = textblockStarts(built.nodes[w[0]], nodePos[w[0]]), tb = starts[w[1]];
        if (tb == null) break;
        var size = tr.doc.resolve(tb).parent.content.size;
        return Math.min(tb + w[2] + (pos - l.start), tb + size);
      }
      return null;
    }
    var a = map(sel.from), b = map(sel.to);
    if (a != null && b != null) tr.setSelection(TextSelection.create(tr.doc, a, b));
    else tr.setSelection(TextSelection.near(tr.doc.resolve(Math.min(scope.from + 1, tr.doc.content.size))));
    return tr;
  }

  function currentBlock(state) {
    var $from = state.selection.$from;
    for (var d = $from.depth; d > 0; d--) {
      var n = $from.node(d), name = n.type.name;
      if (name === 'codeBlock') return 'code';
      if (name === 'heading') return n.attrs.level <= 3 ? 'h' + n.attrs.level : 'paragraph';
      if (name === 'taskItem') return 'task';
      if (name === 'listItem') return LIST_OF[$from.node(d - 1).type.name] || 'bullet';
      if (name === 'blockquote') return 'quote';
      if (name === 'detailsSummary') return 'summary';
    }
    return 'paragraph';
  }

  function blockCheck(state, kind) {
    if (state.selection instanceof NodeSelection) return { allowed: false, reason: '先把光标放进文字里' };
    var scope = blockScope(state);
    if (kind === 'details') {
      if (scope.inDetails || scope.inSummary) return { allowed: true, reason: '', active: true };
      var lines = linesOf(state, scope);
      if (lines.some(function (l) { return l.kind === 'atom'; })) return { allowed: false, reason: '表格、分隔线和折叠不能放进折叠' };
      return { allowed: true, reason: '' };
    }
    if (scope.inSummary) return { allowed: false, reason: '折叠标题只是一行文字' };
    if (scope.inDetails && IN_DETAILS.indexOf(kind) < 0) return { allowed: false, reason: '折叠里只放段落和列表' };
    return { allowed: true, reason: '' };
  }

  function setBlock(editor, kind) {
    var state = editor.state;
    if (!blockCheck(state, kind).allowed) return false;
    if (kind === 'details') return toggleDetails(editor);
    var scope = blockScope(state), lines = linesOf(state, scope);
    var chosen = lines.filter(function (l) { return l.selected && l.kind !== 'atom'; });
    if (!chosen.length) return false;
    var off = chosen.every(function (l) { return l.kind === kind; });
    var oneList = isListKind(chosen[0].kind) && chosen.every(function (l) { return l.block === chosen[0].block; });
    lines.forEach(function (l) {
      if (l.kind === 'atom') l.to = 'atom';
      else if (isListKind(kind) && oneList && !off) l.to = l.block === chosen[0].block ? kind : l.kind;
      else l.to = l.selected ? (off ? 'paragraph' : kind) : l.kind;
    });
    editor.view.dispatch(replaceLines(state, scope, lines).scrollIntoView());
    return true;
  }

  function toggleDetails(editor) {
    var state = editor.state, schema = state.schema, N = schema.nodes, scope = blockScope(state), tr = state.tr;
    if (scope.inDetails || scope.inSummary) {
      var $from = state.selection.$from, d = 1;
      var details = $from.node(d), pos = $from.before(d);
      var title = details.firstChild.textContent, body = [];
      details.lastChild.forEach(function (n) { body.push(n); });
      var head = title ? [N.paragraph.create(null, schema.text(title))] : [];
      tr.replaceWith(pos, pos + details.nodeSize, head.concat(body));
      var shift = scope.inSummary ? 1 : title ? 2 : 4;
      var target = Math.max(1, Math.min(state.selection.from - shift, tr.doc.content.size - 1));
      tr.setSelection(TextSelection.near(tr.doc.resolve(target)));
      editor.view.dispatch(tr.scrollIntoView());
      return true;
    }
    var lines = linesOf(state, scope), first = scope.container.child(scope.i0), titleText = '', rest = lines;
    if (first.type.name === 'paragraph' || first.type.name === 'heading') {
      titleText = plainText(lines[0]).replace(/\s+/g, ' ').trim();
      rest = lines.slice(1);
    }
    rest.forEach(function (l) { l.to = isListKind(l.kind) ? l.kind : 'paragraph'; });
    var content = buildBlocks(schema, rest).nodes;
    if (!content.length) content = [N.paragraph.create()];
    var node = N.details.create({ open: true }, [N.detailsSummary.create(null, titleText ? schema.text(titleText) : null), N.detailsContent.create(null, content)]);
    tr.replaceWith(scope.from, scope.to, node);
    var caret = !titleText ? scope.from + 2 : rest.length ? scope.from + 2 + titleText.length : scope.from + 2 + titleText.length + 3;
    tr.setSelection(TextSelection.near(tr.doc.resolve(caret)));
    editor.view.dispatch(tr.scrollIntoView());
    return true;
  }

  // `+++ ` or `>>> ` on an empty top-level line starts a collapse with the caret in its title.
  function detailsInputRule() {
    return new core.InputRule({
      find: /^(?:\+\+\+|>>>)\s$/,
      handler: function (props) {
        var state = props.state, $from = state.doc.resolve(props.range.from);
        if ($from.depth !== 1 || $from.parent.type.name !== 'paragraph') return null;
        var N = state.schema.nodes, tr = state.tr, start = $from.before(1);
        var title = $from.parent.textContent.slice(props.range.to - $from.start()).replace(/\s+/g, ' ').trim();
        var node = N.details.create({ open: true }, [N.detailsSummary.create(null, title ? state.schema.text(title) : null),
          N.detailsContent.create(null, [N.paragraph.create()])]);
        tr.replaceWith(start, start + $from.parent.nodeSize, node);
        tr.setSelection(TextSelection.create(tr.doc, title ? start + 2 + title.length : start + 2));
      }
    });
  }

  // ------------------------------------------------------------------------- toolbar

  var MARK_BUTTONS = [
    { name: 'bold', icon: 'bold', label: '粗体', key: MOD + 'B' },
    { name: 'italic', icon: 'italic', label: '斜体', key: MOD + 'I' },
    { name: 'strike', icon: 'strike', label: '删除线', key: MOD + SHIFT + 'S' },
    { name: 'underline', icon: 'underline', label: '下划线', key: MOD + 'U' }
  ];
  var LINK_BUTTONS = [
    { name: 'link', icon: 'link', label: '链接', key: MOD + 'K' },
    { name: 'code', icon: 'code', label: '行内代码', key: MOD + 'E' }
  ];
  var BLOCK_ITEMS = [
    { kind: 'paragraph', label: '正文', key: MOD + ALT + '0' },
    { kind: 'h1', label: '标题 1', key: MOD + ALT + '1' },
    { kind: 'h2', label: '标题 2', key: MOD + ALT + '2' },
    { kind: 'h3', label: '标题 3', key: MOD + ALT + '3' },
    null,
    { kind: 'bullet', label: '列表', key: MOD + SHIFT + '8' },
    { kind: 'ordered', label: '编号列表', key: MOD + SHIFT + '9' },
    { kind: 'task', label: '待办清单', key: MOD + SHIFT + '7' },
    null,
    { kind: 'quote', label: '引用', key: MOD + SHIFT + 'B' },
    { kind: 'code', label: '代码块', key: MOD + ALT + 'C', icon: 'codeblock' },
    null,
    { kind: 'details', label: '折叠', key: '+++' }
  ];

  function Toolbar(ctl) {
    this.ctl = ctl;
    var el = this.el = document.createElement('div');
    el.className = 'rl-fmt';
    el.setAttribute('role', 'toolbar');
    el.setAttribute('aria-label', '文字格式');
    el.hidden = true;
    var html = '<div class="rl-fmt-row">';
    function button(b, extra) {
      return '<button type="button" class="rl-fmt-btn' + (extra || '') + '" data-cmd="' + b.name + '" aria-label="' + b.label +
        '" data-tip="' + b.label + '  ' + b.key + '" aria-pressed="false">' + svg(b.icon) + '</button>';
    }
    html += MARK_BUTTONS.map(function (b) { return button(b); }).join('');
    html += '<span class="rl-fmt-sep" role="separator"></span>';
    html += LINK_BUTTONS.map(function (b) { return button(b); }).join('');
    html += '<span class="rl-fmt-sep" role="separator"></span>';
    html += '<button type="button" class="rl-fmt-btn rl-fmt-wide" data-cmd="block" aria-haspopup="menu" aria-expanded="false" aria-label="段落格式" data-tip="段落格式">' +
      '<span class="rl-fmt-current">' + svg('paragraph') + '</span>' + svg('chevron') + '</button>';
    html += '</div><div class="rl-fmt-link" hidden><input type="text" inputmode="url" spellcheck="false" autocomplete="off" placeholder="粘贴或输入链接，回车确认" aria-label="链接地址">' +
      '<button type="button" class="rl-fmt-btn" data-cmd="unlink" aria-label="取消链接" data-tip="取消链接">' + svg('unlink') + '</button></div>';
    html += '<div class="rl-fmt-menu" role="menu" hidden>' + BLOCK_ITEMS.map(function (it) {
      if (!it) return '<div class="rl-fmt-menu-sep" role="separator"></div>';
      return '<button type="button" role="menuitemradio" class="rl-fmt-item" data-kind="' + it.kind + '" aria-checked="false">' +
        svg(it.icon || it.kind) + '<span class="rl-fmt-label">' + it.label + '</span><span class="rl-fmt-key">' + it.key + '</span>' +
        '<span class="rl-fmt-why"></span></button>';
    }).join('') + '</div>';
    el.innerHTML = html;
    document.body.appendChild(el);
    this.menu = el.querySelector('.rl-fmt-menu');
    this.linkBox = el.querySelector('.rl-fmt-link');
    this.linkInput = this.linkBox.querySelector('input');
    this.row = el.querySelector('.rl-fmt-row');
    var self = this;
    // Clicks must not take focus from the editor (the selection would collapse); the link field is the exception.
    el.addEventListener('mousedown', function (e) { if (e.target !== self.linkInput) e.preventDefault(); });
    el.addEventListener('click', function (e) { self.onClick(e); });
    this.linkInput.addEventListener('keydown', function (e) { self.onLinkKey(e); });
    this.linkInput.addEventListener('blur', function (e) {
      if (!el.contains(e.relatedTarget)) self.closeLink(false);
    });
  }
  Toolbar.prototype.onClick = function (e) {
    var b = e.target.closest('button');
    if (!b || !this.el.contains(b)) return;
    var ctl = this.ctl;
    if (b.getAttribute('aria-disabled') === 'true') return;
    var item = b.dataset.kind;
    if (item) { this.closeMenu(); ctl.block(item); return; }
    var cmd = b.dataset.cmd;
    if (cmd === 'block') { this.menu.hidden ? this.openMenu() : this.closeMenu(); return; }
    if (cmd === 'link') { this.openLink(); return; }
    if (cmd === 'unlink') { ctl.unlink(); this.closeLink(true); return; }
    ctl.toggle(cmd);
  };
  Toolbar.prototype.refresh = function () {
    var ctl = this.ctl, state = ctl.editor.state, ed = ctl.editor;
    this.el.querySelectorAll('[data-cmd]').forEach(function (b) {
      var cmd = b.dataset.cmd;
      if (!CONFLICTS[cmd]) return;
      var c = markCheck(state, cmd), on = ed.isActive(cmd);
      // An active mark can always be removed, even when it could not be added here.
      var allowed = c.allowed || on;
      b.setAttribute('aria-disabled', allowed ? 'false' : 'true');
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      b.classList.toggle('is-on', on);
      var base = b.getAttribute('aria-label') + '  ' + ([].concat(MARK_BUTTONS, LINK_BUTTONS).filter(function (x) { return x.name === cmd; })[0] || {}).key;
      b.dataset.tip = allowed ? base : base + ' · ' + c.reason;
    });
    var kind = currentBlock(state), scope = blockScope(state);
    var inDetails = scope.inDetails || scope.inSummary;
    var icon = kind === 'summary' ? 'details' : kind === 'code' ? 'codeblock' : kind;
    this.el.querySelector('.rl-fmt-current').innerHTML = svg(ICONS[icon] ? icon : 'paragraph');
    this.menu.querySelectorAll('[data-kind]').forEach(function (it) {
      var k = it.dataset.kind, c = blockCheck(state, k);
      var checked = k === 'details' ? inDetails : k === kind;
      it.setAttribute('aria-checked', checked ? 'true' : 'false');
      it.setAttribute('aria-disabled', c.allowed ? 'false' : 'true');
      it.querySelector('.rl-fmt-why').textContent = c.allowed ? '' : c.reason;
      if (k === 'details') it.querySelector('.rl-fmt-label').textContent = inDetails ? '取消折叠' : '折叠';
    });
  };
  Toolbar.prototype.show = function () {
    if (this.el.hidden) this.el.hidden = false;
    this.refresh();
    this.place();
  };
  Toolbar.prototype.hide = function () {
    if (this.linking) return;
    this.closeMenu();
    this.el.hidden = true;
  };
  Toolbar.prototype.place = function () {
    if (this.el.hidden) return;
    var el = this.el;
    if (window.matchMedia('(max-width: 640px)').matches) { el.style.left = ''; el.style.top = ''; return; }
    var view = this.ctl.editor.view, sel = this.ctl.linkRange || view.state.selection;
    var a = view.coordsAtPos(sel.from), b = view.coordsAtPos(sel.to, -1);
    var w = el.offsetWidth, h = el.offsetHeight, gap = 8;
    var left = Math.min(a.left, b.left);
    if (a.top !== b.top) left = view.dom.getBoundingClientRect().left;
    left = Math.max(8, Math.min(left - 6, window.innerWidth - w - 8));
    var top = Math.min(a.top, b.top) - h - gap;
    if (top < 60) top = Math.max(a.bottom, b.bottom) + gap;   // under the 52px top bar, flip below
    el.style.left = Math.round(left) + 'px';
    el.style.top = Math.round(top) + 'px';
  };
  Toolbar.prototype.openMenu = function () {
    this.refresh();
    this.menu.hidden = false;
    this.el.querySelector('[data-cmd=block]').setAttribute('aria-expanded', 'true');
    this.focusItem(this.menu.querySelector('[aria-checked=true]:not([aria-disabled=true])') || this.items()[0]);
  };
  Toolbar.prototype.closeMenu = function () {
    if (this.menu.hidden) return;
    this.menu.hidden = true;
    this.el.querySelector('[data-cmd=block]').setAttribute('aria-expanded', 'false');
  };
  Toolbar.prototype.items = function () {
    return [].slice.call(this.menu.querySelectorAll('[data-kind]:not([aria-disabled=true])'));
  };
  Toolbar.prototype.focusItem = function (it) {
    this.menu.querySelectorAll('.is-active').forEach(function (x) { x.classList.remove('is-active'); });
    if (it) it.classList.add('is-active');
  };
  // Keys while the menu is open; the editor keeps focus, so they arrive through handleKeyDown.
  Toolbar.prototype.menuKey = function (e) {
    if (this.menu.hidden) return false;
    var items = this.items(), cur = this.menu.querySelector('.is-active'), i = items.indexOf(cur);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      var n = items.length;
      this.focusItem(items[(i + (e.key === 'ArrowDown' ? 1 : -1) + n) % n]);
      return true;
    }
    if (e.key === 'Enter') { if (cur) { this.closeMenu(); this.ctl.block(cur.dataset.kind); } return true; }
    if (e.key === 'Escape') { this.closeMenu(); return true; }
    return false;
  };
  Toolbar.prototype.openLink = function () {
    var ed = this.ctl.editor, state = ed.state;
    if (!markCheck(state, 'link').allowed && !ed.isActive('link')) return;
    if (ed.isActive('link')) ed.chain().extendMarkRange('link').run();
    var sel = ed.state.selection;
    if (sel.empty) return;
    this.ctl.linkRange = { from: sel.from, to: sel.to };
    this.linking = true;
    this.closeMenu();
    this.row.hidden = true;
    this.linkBox.hidden = false;
    this.linkInput.value = ed.getAttributes('link').href || '';
    this.linkInput.removeAttribute('aria-invalid');
    this.el.hidden = false;
    this.place();
    this.linkInput.focus();
    this.linkInput.select();
  };
  Toolbar.prototype.closeLink = function (refocus) {
    if (!this.linking) return;
    this.linking = false;
    this.linkBox.hidden = true;
    this.row.hidden = false;
    var range = this.ctl.linkRange;
    this.ctl.linkRange = null;
    if (refocus && range) this.ctl.editor.chain().focus().setTextSelection(range).run();
    else this.hide();
  };
  Toolbar.prototype.onLinkKey = function (e) {
    if (e.key === 'Escape') { e.preventDefault(); this.closeLink(true); return; }
    if (e.key !== 'Enter') return;
    e.preventDefault();
    var raw = this.linkInput.value, href = normaliseHref(raw), range = this.ctl.linkRange;
    if (!raw.trim()) { this.ctl.editor.chain().focus().setTextSelection(range).unsetLink().run(); this.closeLink(true); return; }
    if (!href) { this.linkInput.setAttribute('aria-invalid', 'true'); return; }
    this.ctl.editor.chain().focus().setTextSelection(range).setLink({ href: href }).run();
    this.closeLink(true);
  };
  Toolbar.prototype.destroy = function () { this.el.remove(); };

  // ------------------------------------------------------------------------- slash menu
  // Typing / at the start of a line or after a space opens a menu of blocks (card 0118, ISSUES-LINEAR §2.2): type to
  // filter, ↑↓ to choose, ↵ to insert, Esc to close. It follows the compatibility table: no menu in a code block or
  // a collapse title, and blocks a list item, a quote or a collapse cannot hold are greyed out with the reason.
  // 附件 and 关联文献 belong to the page, not the editor: they fire `rl:editor-command` on the element the editor
  // was mounted on, detail.command 'attach' | 'link-material', and the page opens its own picker.
  var SLASH_ITEMS = [
    { kind: 'h1', label: '标题 1', icon: 'h1', words: 'biaoti heading title h1 1' },
    { kind: 'h2', label: '标题 2', icon: 'h2', words: 'biaoti heading title h2 2' },
    { kind: 'h3', label: '标题 3', icon: 'h3', words: 'biaoti heading title h3 3' },
    { kind: 'bullet', label: '列表', icon: 'bullet', words: 'liebiao list bullet ul wuxu 无序' },
    { kind: 'ordered', label: '编号列表', icon: 'ordered', words: 'bianhao liebiao ordered number ol 有序' },
    { kind: 'task', label: '待办清单', icon: 'task', words: 'daiban qingdan todo task check checklist' },
    { kind: 'quote', label: '引用', icon: 'quote', words: 'yinyong quote blockquote' },
    { kind: 'details', label: '折叠', icon: 'details', words: 'zhedie collapse toggle details' },
    { kind: 'code', label: '代码块', icon: 'codeblock', words: 'daima code codeblock' },
    { kind: 'attach', label: '附件', icon: 'attach', words: 'fujian attach attachment file wenjian 文件', command: 'attach' },
    { kind: 'material', label: '关联文献', icon: 'material', words: 'guanlian wenxian shuju link paper dataset cite 数据', command: 'link-material' }
  ];
  ICONS.attach = '<path d="M13.234 20.252 21 12.3"/><path d="m16 6-8.414 8.586a2 2 0 0 0 0 2.828 2 2 0 0 0 2.828 0l8.414-8.586a4 4 0 0 0 0-5.656 4 4 0 0 0-5.656 0l-8.415 8.585a6 6 0 1 0 8.486 8.486"/>';
  ICONS.material = '<path d="M12 7v6"/><path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H19a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H6.5a1 1 0 0 1 0-5H20"/><path d="M9 10h6"/>';

  // The open slash, if any: where the / is and what follows it, typed in a block that may hold the menu.
  function slashAt(state) {
    var sel = state.selection;
    if (!sel.empty || !(sel instanceof TextSelection)) return null;
    var $from = sel.$from, parent = $from.parent;
    if (parent.type.spec.code || parent.type.name === 'detailsSummary' || !parent.isTextblock) return null;
    var before = parent.textBetween(0, $from.parentOffset, '\n', '￼');
    var m = /(^|\s)\/([^\s/]{0,24})$/.exec(before);
    if (!m) return null;
    var from = $from.pos - m[2].length - 1;
    return { from: from, to: $from.pos, query: m[2].toLowerCase() };
  }
  function slashCheck(state, item) {
    if (item.command) return { allowed: true, reason: '' };
    var $from = state.selection.$from;
    for (var d = $from.depth; d > 0; d--) {
      var name = $from.node(d).type.name;
      if ((name === 'listItem' || name === 'taskItem') && !isListKind(item.kind)) return { allowed: false, reason: '列表里只放段落' };
      if (name === 'blockquote') return { allowed: false, reason: '引用里只放段落' };
    }
    return blockCheck(state, item.kind);
  }

  function Slash(ctl) {
    this.ctl = ctl;
    var el = this.el = document.createElement('div');
    el.className = 'rl-slash';
    el.setAttribute('role', 'listbox');
    el.setAttribute('aria-label', '插入');
    el.hidden = true;
    document.body.appendChild(el);
    this.active = null; this.index = 0; this.dismissed = null; this.shown = [];
    var self = this;
    el.addEventListener('mousedown', function (e) { e.preventDefault(); });   // keep the caret in the editor
    el.addEventListener('click', function (e) {
      var b = e.target.closest('[data-slash]');
      if (b && b.getAttribute('aria-disabled') !== 'true') self.run(self.shown[+b.dataset.slash]);
    });
  }
  Slash.prototype.update = function () {
    var state = this.ctl.editor.state, at = slashAt(state);
    if (!at || !this.ctl.editor.isFocused || (this.dismissed !== null && this.dismissed === at.from)) { this.hide(); return; }
    if (this.dismissed !== null && this.dismissed !== at.from) this.dismissed = null;
    var q = at.query;
    // The label anywhere, or the start of a keyword (pinyin or English): "biao" finds 标题, not 列表 (liebiao).
    var shown = SLASH_ITEMS.filter(function (it) {
      return !q || it.label.toLowerCase().indexOf(q) >= 0 || it.words.split(' ').some(function (w) { return w.indexOf(q) === 0; });
    });
    if (!shown.length) { this.hide(); return; }
    if (!this.active || this.active.from !== at.from || this.active.query !== at.query) this.index = 0;
    this.active = at;
    this.shown = shown;
    var checks = shown.map(function (it) { return slashCheck(state, it); });
    if (!checks[this.index] || !checks[this.index].allowed) {
      var first = checks.findIndex(function (c) { return c.allowed; });
      this.index = first < 0 ? 0 : first;
    }
    var self = this;
    this.el.innerHTML = shown.map(function (it, i) {
      var c = checks[i];
      return '<button type="button" role="option" class="rl-slash-item' + (i === self.index ? ' is-active' : '') + '" data-slash="' + i +
        '" aria-selected="' + (i === self.index) + '" aria-disabled="' + (!c.allowed) + '">' + svg(it.icon) +
        '<span class="rl-slash-label">' + it.label + '</span>' + (c.allowed ? '' : '<span class="rl-slash-why">' + c.reason + '</span>') + '</button>';
    }).join('');
    this.checks = checks;
    this.el.hidden = false;
    this.place();
  };
  Slash.prototype.place = function () {
    if (this.el.hidden || !this.active) return;
    var view = this.ctl.editor.view, at = view.coordsAtPos(this.active.from), el = this.el;
    var h = el.offsetHeight, w = el.offsetWidth;
    var top = at.bottom + 6;
    if (top + h > window.innerHeight - 8 && at.top - h - 6 > 8) top = at.top - h - 6;   // flip above near the bottom
    el.style.left = Math.round(Math.max(8, Math.min(at.left - 8, window.innerWidth - w - 8))) + 'px';
    el.style.top = Math.round(top) + 'px';
  };
  Slash.prototype.hide = function () {
    this.active = null;
    if (!this.el.hidden) this.el.hidden = true;
  };
  Slash.prototype.key = function (e) {
    if (this.el.hidden || !this.active || e.isComposing) return false;
    var n = this.shown.length, checks = this.checks;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      var step = e.key === 'ArrowDown' ? 1 : -1, i = this.index;
      for (var k = 0; k < n; k++) { i = (i + step + n) % n; if (checks[i].allowed) break; }
      this.index = i;
      var active = this.active;
      this.el.querySelectorAll('[data-slash]').forEach(function (b, j) {
        b.classList.toggle('is-active', j === i); b.setAttribute('aria-selected', String(j === i));
        if (j === i) b.scrollIntoView({ block: 'nearest' });
      });
      this.active = active;
      return true;
    }
    if (e.key === 'Enter' || e.key === 'Tab') {
      if (checks[this.index] && checks[this.index].allowed) this.run(this.shown[this.index]);
      return true;
    }
    if (e.key === 'Escape') { this.dismissed = this.active.from; this.hide(); return true; }
    return false;
  };
  Slash.prototype.run = function (item) {
    var ed = this.ctl.editor, at = this.active;
    if (!item || !at) return;
    this.hide();
    ed.view.dispatch(ed.state.tr.delete(at.from, at.to));
    if (item.command) {
      this.ctl.command(item.command);
      return;
    }
    if (item.kind === 'details') toggleDetails(ed);
    else setBlock(ed, item.kind);
    ed.commands.focus();
  };
  Slash.prototype.destroy = function () { this.el.remove(); };

  // --------------------------------------------------------------------------- mount

  function mount(target, options) {
    options = options || {};
    var isField = target.tagName === 'TEXTAREA';
    var initial = options.markdown != null ? options.markdown : isField ? target.value : target.textContent;
    var placeholder = options.placeholder || target.getAttribute('placeholder') || target.dataset.placeholder || '';
    var host = target;
    if (isField) {
      host = document.createElement('div');
      target.insertAdjacentElement('afterend', host);
      target.hidden = true;
    } else {
      target.textContent = '';
    }
    host.classList.add('rl-editor');
    var delay = options.saveDelay == null ? 800 : options.saveDelay;
    var ctl = { linkRange: null }, timer = null, lastSaved = '', loaded = '', loading = true, pointerDown = false, toolbar, slash;

    function emit(name, md) {
      if (isField) target.dispatchEvent(new CustomEvent(name, { bubbles: true, detail: { markdown: md } }));
    }
    function current() { return toMarkdown(ctl.editor.state.doc); }
    function flush() {
      clearTimeout(timer); timer = null;
      var md = current();
      if (md === lastSaved) return;
      var before = lastSaved;
      lastSaved = md;
      if (isField) target.value = md;
      emit('rl:editor-save', md);
      if (options.onSave) {
        var r = options.onSave(md);
        if (r && typeof r.then === 'function') r.then(null, function () { if (lastSaved === md) lastSaved = before; });
      }
    }
    function changed() {
      if (loading) return;
      var md = current();
      if (isField) target.value = md === loaded ? initial : md;
      emit('rl:editor-change', md);
      if (options.onChange) options.onChange(md);
      if (ctl.editor.view.composing) return;   // IME: wait for the composed text
      clearTimeout(timer);
      if (md !== lastSaved) timer = setTimeout(flush, delay);
    }
    function selectionShown() {
      var sel = ctl.editor.state.selection;
      return ctl.editor.isFocused && !sel.empty && sel instanceof TextSelection && !pointerDown;
    }
    function sync() {
      if (!toolbar) return;
      if (toolbar.linking) return;
      if (selectionShown()) toolbar.show(); else toolbar.hide();
    }

    var Keys = core.Extension.create({
      name: 'rlKeys',
      priority: 1000,
      addKeyboardShortcuts: function () {
        var map = {};
        function mark(name) { return function () { ctl.toggle(name); return true; }; }
        [['b', 'bold'], ['i', 'italic'], ['u', 'underline'], ['e', 'code']].forEach(function (p) {
          map['Mod-' + p[0]] = map['Mod-' + p[0].toUpperCase()] = mark(p[1]);
        });
        map['Mod-Shift-s'] = map['Mod-Shift-S'] = mark('strike');
        map['Mod-k'] = map['Mod-K'] = function () { toolbar.openLink(); return true; };
        function blk(kind) { return function () { ctl.block(kind); return true; }; }
        map['Mod-Alt-0'] = blk('paragraph');
        map['Mod-Alt-1'] = blk('h1'); map['Mod-Alt-2'] = blk('h2'); map['Mod-Alt-3'] = blk('h3');
        map['Mod-Shift-8'] = blk('bullet'); map['Mod-Shift-9'] = blk('ordered'); map['Mod-Shift-7'] = blk('task');
        map['Mod-Shift-b'] = map['Mod-Shift-B'] = blk('quote');
        map['Mod-Alt-c'] = map['Mod-Alt-C'] = map['Mod-Shift-\\'] = blk('code');
        map['Mod-Enter'] = map['Mod-s'] = function () { flush(); return true; };
        // Enter in an open collapse's title goes to its first line when that line is still empty
        // (TipTap would add a second empty line above it); otherwise TipTap's own handling applies.
        map.Enter = function () {
          var st = ctl.editor.state, $h = st.selection.$head;
          if (!st.selection.empty || $h.parent.type.name !== 'detailsSummary' || !$h.node(-1).attrs.open) return false;
          var first = $h.node(-1).lastChild.firstChild;
          if (!first || first.type.name !== 'paragraph' || first.content.size) return false;
          ctl.editor.commands.setTextSelection($h.after() + 2);
          return true;
        };
        map.Escape = function () {
          if (toolbar && !toolbar.el.hidden) { toolbar.hide(); return true; }
          return false;
        };
        return map;
      },
      addInputRules: function () { return [detailsInputRule()]; }
    });

    ctl.editor = new core.Editor({
      element: host,
      extensions: [
        R.Document, Paragraph, R.Text, Heading, Blockquote, CodeBlock, HorizontalRule, R.HardBreak,
        BulletList, OrderedList, ListItem, TaskList, TaskItem, R.ListKeymap,
        Details, DetailsSummary, DetailsContent, RawBlock,
        Bold, Italic, Strike, Underline, Code, Link,
        R.UndoRedo, R.Dropcursor, R.Gapcursor, R.TrailingNode,
        R.Placeholder.configure({
          includeChildren: true,
          showOnlyCurrent: false,
          placeholder: function (p) {
            if (p.node.type.name === 'detailsSummary') return '折叠标题';
            return p.editor.isEmpty ? placeholder : '';
          }
        }),
        Normalise, Keys
      ],
      content: toHTML(initial),
      enablePasteRules: false,
      editorProps: {
        attributes: { class: 'rl-md', 'aria-label': target.getAttribute('aria-label') || placeholder || '编辑', role: 'textbox', 'aria-multiline': 'true' },
        handleKeyDown: function (view, e) { return (slash && slash.key(e)) || (toolbar ? toolbar.menuKey(e) : false); },
        // Pasted plain text is read as Markdown, URLs become links; ⇧ paste and code blocks stay plain.
        clipboardTextParser: function (text, $context, plain) {
          if (plain || $context.parent.type.spec.code) return null;
          var dom = document.createElement('div');
          dom.innerHTML = mdPaste.render(text);
          return PMModel.DOMParser.fromSchema(ctl.editor.schema).parseSlice(dom, { preserveWhitespace: false, context: $context });
        },
        handlePaste: function (view, e) {
          var text = e.clipboardData && e.clipboardData.getData('text/plain');
          var href = text && !/\s/.test(text.trim()) ? normaliseHref(text) : null;
          if (!href || view.state.selection.empty || !/^(https?:|\/)/i.test(text.trim())) return false;
          if (!markCheck(view.state, 'link').allowed) return false;
          ctl.editor.chain().setLink({ href: href }).run();
          return true;
        }
      },
      onUpdate: changed,
      onSelectionUpdate: sync,
      onTransaction: function () {
        if (toolbar && !toolbar.el.hidden && !toolbar.linking) { toolbar.refresh(); toolbar.place(); }
        if (slash) slash.update();
      },
      onFocus: sync,
      onBlur: function (p) {
        if (slash) slash.hide();
        flush();
        var to = p.event && p.event.relatedTarget;
        if (toolbar && to && toolbar.el.contains(to)) return;
        if (toolbar) toolbar.hide();
      }
    });
    tidy(ctl.editor);
    loading = false;
    lastSaved = loaded = current();

    ctl.toggle = function (name) {
      var ed = ctl.editor, on = ed.isActive(name);
      if (name === 'link') { toolbar.openLink(); return; }
      if (!on && !markCheck(ed.state, name).allowed) return;
      ed.chain().focus().toggleMark(name).run();
    };
    ctl.unlink = function () {
      var range = ctl.linkRange;
      var chain = ctl.editor.chain().focus();
      if (range) chain = chain.setTextSelection(range);
      chain.extendMarkRange('link').unsetLink().run();
    };
    ctl.block = function (kind) { setBlock(ctl.editor, kind); ctl.editor.commands.focus(); };
    toolbar = new Toolbar(ctl);
    slash = new Slash(ctl);
    ctl.command = function (name) {
      target.dispatchEvent(new CustomEvent('rl:editor-command', { bubbles: true, detail: { command: name } }));
    };

    var dom = ctl.editor.view.dom;
    function down() { pointerDown = true; if (!toolbar.linking) toolbar.hide(); }
    function up() { if (!pointerDown) return; pointerDown = false; setTimeout(sync, 0); }
    function reflow() { if (toolbar && !toolbar.el.hidden) toolbar.place(); if (slash) slash.place(); }
    dom.addEventListener('mousedown', down);
    window.addEventListener('mouseup', up);
    window.addEventListener('scroll', reflow, true);
    window.addEventListener('resize', reflow);

    return {
      editor: ctl.editor,
      element: host,
      getMarkdown: current,
      setMarkdown: function (md) {
        loading = true;
        ctl.editor.commands.setContent(toHTML(md), { emitUpdate: false });
        tidy(ctl.editor);
        loading = false;
        initial = md;
        lastSaved = loaded = current();
        if (isField) target.value = md;
      },
      flush: flush,
      focus: function () { ctl.editor.commands.focus(); },
      destroy: function () {
        clearTimeout(timer);
        dom.removeEventListener('mousedown', down);
        window.removeEventListener('mouseup', up);
        window.removeEventListener('scroll', reflow, true);
        window.removeEventListener('resize', reflow);
        toolbar.destroy();
        slash.destroy();
        ctl.editor.destroy();
        if (isField) { host.remove(); target.hidden = false; }
      }
    };
  }

  window.RLIssueEditor = {
    mount: mount,
    toHTML: toHTML,
    render: function (markdown) { return mdView.render(String(markdown || '')); },
    toMarkdown: toMarkdown,
    isSafeLink: isSafeLink,
    versions: R.versions
  };
})();
