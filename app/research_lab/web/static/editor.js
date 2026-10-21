// Document editor for any sha-guarded Markdown file. Toast UI in WYSIWYG ("文档") mode by default, the
// Markdown source ("源码") one click away; autosave after 5 s idle and on Cmd/Ctrl+S, POST form.action
// with {<field>, sha}. On 409 the server's diff is shown and the user's text stays in the editor.
// Without the vendored script the plain textarea and the 保存 button still post the same form.
//
// Contract (form[data-editor]):
//   action            save URL; the server answers 200 → status fragment + X-RL-Sha, 409 → conflict fragment
//   data-field        form field name for the text (default "body")
//   data-source       GET URL returning the on-disk text + X-RL-Sha (the conflict view's 载入磁盘版本)
//   data-ref          artifact ref (legacy default for data-source: /artifacts/<ref>/body)
//   textarea[name=<field>], #tui-editor, #editor-status, #editor-conflict, optional #editor-count, #meta-form
//
// Managed content. The file on disk is Markdown and stays the source of truth; the server
// (files.artifacts.merge_protected) restores every `<!-- rl:begin excerpts -->…<!-- rl:end -->` block
// and the `## AI notes` section from disk on save, whatever the editor sent back. Block-level HTML
// comments survive Toast UI 3.2.2's WYSIWYG conversion as `div[data-html-comment]` (app.css styles the
// excerpt block that sits between two of them as a read-only box). Inline comments
// (`- quote <!-- rl:EXC-… -->`) make the converter throw, so the rl markers are stripped before the
// text reaches the editor; any other inline comment keeps the file in 源码 mode.
//
// Template hints (`<!-- One sentence… -->` on a line of its own) would show as raw comment text in a
// document, so they travel through the editor as an italic line `*〔提示〕One sentence…*` and are turned
// back into the comment when the text leaves the editor. Toast UI serialises emphasis with `*`, so the
// round trip is exact; a hint the user edits simply becomes an edited comment. (Toast's widgetRules were
// tried first and lost the matched text on the way back to Markdown.)
(function () {
  var form = document.querySelector('form[data-editor]');
  if (!form) return;
  var field = form.dataset.field || 'body';
  var src = form.querySelector('textarea[name="' + field + '"]') || document.getElementById('body-src');
  var host = document.getElementById('tui-editor');
  var statusEl = document.getElementById('editor-status');
  var conflictEl = document.getElementById('editor-conflict');
  var countEl = document.getElementById('editor-count');
  var ref = form.dataset.ref;
  var sourceUrl = form.dataset.source || (ref ? '/artifacts/' + ref + '/body' : null);
  var editor = null, timer = null, saving = null, dirty = false, edits = 0, blocked = false;
  var switching = false;
  var MODE_KEY = 'rl.editor.mode';
  var INLINE_MARKER_RE = /[ \t]*<!--\s*rl:EXC-\d{4}-\d{4}\s*-->/g;
  // `\S` right after the spaces stops the regex backtracking into `<!-- rl:begin … -->` (a shorter
  // whitespace match would put a space in front of `rl:` and slip past the lookahead).
  var HINT_IN_RE = /^<!--[ \t]*(?!rl:)(\S[^\n]*?)[ \t]*-->[ \t]*$/gm;
  var HINT_OUT_RE = /^[ \t]*\*〔提示〕([^\n]*?)\*[ \t]*$/gm;
  var INLINE_COMMENT_RE = /\S[ \t]*<!--|-->[ \t]*\S/;
  var TOOLBAR = [['heading', 'bold', 'italic', 'strike'], ['quote', 'ul', 'ol', 'indent', 'outdent'], ['table', 'link', 'hr']];
  // An output's file keeps templates/artifact.md's English scaffolding; inside the editor those exact lines
  // read in Chinese (the headings match describe()'s DESC_HEADINGS in routes/artifacts.py) and go back to
  // English on save, so an untouched line round-trips byte for byte. Whole lines only, bullets compared as
  // `- ` because Toast writes `*`.
  var ZH_LINES = sourceUrl && sourceUrl.indexOf('/artifacts/') === 0 ? [
    ["## What did I figure out that I didn't know last week?", '## 想清楚了什么'],
    ['## Claim', '## 主张'], ['## Evidence', '## 证据'], ['## Why it matters', '## 为什么重要'],
    ['## What this challenges', '## 挑战了什么'], ['## Next question', '## 下一个问题'], ['## Sources', '## 来源'],
    ['**I currently think:**', '**我目前认为：**'],
    ['**Why I think so** — cases, works, data:', '**为什么这么认为**：案例、文献、数据'],
    ['*〔提示〕One sentence. If you cannot write this line, the artifact is not done.*', '*〔提示〕一句话。写不出这一句，这份产出就还没完成。*'],
    ['*〔提示〕Which existing explanation, or which of my own earlier artifacts, does this push against?*', '*〔提示〕它挑战的是哪种现有解释，或者我自己早先的哪份产出？*'],
    ['- Author (Year). *Title*.', '- 作者（年份）。*标题*。']
  ] : [];
  var baseTitle = document.title;
  var wysiwygOk = true;

  function swapLines(text, from, to) {
    if (!ZH_LINES.length) return text;
    var map = {};
    ZH_LINES.forEach(function (pair) { map[pair[from]] = pair[to]; });
    return text.split('\n').map(function (line) {
      var key = line.trim().replace(/^[*+] /, '- ');
      return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : line;
    }).join('\n');
  }
  function prepare(text) {
    text = text.replace(INLINE_MARKER_RE, '').replace(HINT_IN_RE, function (m, hint) { return hint ? '*〔提示〕' + hint + '*' : m; });
    text = swapLines(text, 0, 1);
    wysiwygOk = !INLINE_COMMENT_RE.test(text);
    return text;
  }
  function restore(text) {
    text = swapLines(text, 1, 0);
    return text.replace(HINT_OUT_RE, function (m, hint) { return '<!-- ' + hint.replace(/\\([*_\[\]()#>~`.!-])/g, '$1') + ' -->'; });
  }
  function preferredMode() {
    var saved = null;
    try { saved = window.localStorage.getItem(MODE_KEY); } catch (e) { /* private mode */ }
    return (saved === 'markdown' || !wysiwygOk) ? 'markdown' : 'wysiwyg';
  }
  function remember(mode) {
    try { window.localStorage.setItem(MODE_KEY, mode); } catch (e) { /* ignore */ }
  }

  function shaInputs() { return document.querySelectorAll('input[name=sha]'); }
  function currentSha() { return form.querySelector('input[name=sha]').value; }
  function setSha(sha) { shaInputs().forEach(function (i) { i.value = sha; }); }
  function getText() { return restore(editor ? editor.getMarkdown() : src.value); }
  function setText(t) { t = prepare(t); if (editor) editor.setMarkdown(t, false); else src.value = t; }
  function status(html) { statusEl.innerHTML = html; }
  function markDirty(on) { document.title = (on ? '● ' : '') + baseTitle; }
  // 字数: CJK characters count one each, plus Latin words; markup and comments excluded.
  function count() {
    if (!countEl) return;
    var t = getText().replace(/<!--[\s\S]*?-->/g, '').replace(/[#>*_`~\-\[\]()|]/g, ' ');
    var n = (t.match(/[㐀-鿿]/g) || []).length + (t.match(/[A-Za-z0-9À-ɏ][A-Za-z0-9À-ɏ'’-]*/g) || []).length;
    countEl.textContent = '约 ' + n.toLocaleString('zh-CN') + ' 字';
  }

  function editorHeight() { return Math.max(420, window.innerHeight - host.getBoundingClientRect().top - 36) + 'px'; }
  function applyTheme(dark) {
    var ui = host.querySelector('.toastui-editor-defaultUI'); if (!ui) return;
    ui.classList.toggle('toastui-editor-dark', !!dark);
  }

  function markMode(mode) {
    document.querySelectorAll('[data-editor-mode]').forEach(function (b) { b.classList.toggle('on', b.dataset.editorMode === mode); });
  }

  if (window.toastui && window.toastui.Editor && host) {
    src.hidden = true;
    var dark = window.rlTheme ? window.rlTheme.isDark() : (document.documentElement.dataset.theme === 'dark' ||
      (!document.documentElement.dataset.theme && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches));
    var initial = prepare(src.value);
    var mode = preferredMode();
    editor = new window.toastui.Editor({
      el: host, initialValue: initial, initialEditType: mode, previewStyle: 'vertical',
      height: editorHeight(), usageStatistics: false, language: 'zh-CN', theme: dark ? 'dark' : 'light',
      autofocus: false, hideModeSwitch: true, previewHighlight: false, toolbarItems: TOOLBAR
    });
    editor.on('change', changed);
    window.addEventListener('resize', function () { editor.setHeight(editorHeight()); });
    window.addEventListener('rl:theme', function (e) { applyTheme(e.detail.dark); });
    document.querySelectorAll('[data-editor-mode]').forEach(function (b) {
      b.hidden = false;
      b.addEventListener('click', function () { switchMode(b.dataset.editorMode); });
    });
    markMode(mode);
    if (!wysiwygOk) status('<span class="muted">正文里有行内 HTML 注释，这份文件只能用源码模式编辑。</span>');
  } else {
    src.addEventListener('input', changed);
  }
  count();

  function switchMode(mode) {
    if (!editor || (mode === 'wysiwyg') === editor.isWysiwygMode()) return;
    if (mode === 'wysiwyg' && !wysiwygOk) {
      status('<span class="warn">▲ 正文里有行内 HTML 注释，文档模式无法安全处理，请继续使用源码模式。</span>');
      return;
    }
    switching = true;                       // changeMode emits 'change'; a view switch is not an edit
    editor.changeMode(mode, true);
    switching = false;
    markMode(mode);
    remember(mode);
  }

  function changed() {
    if (switching) return;
    dirty = true; edits++;
    markDirty(true);
    count();
    if (blocked) return;
    status('<span class="muted">有未保存的修改…</span>');
    clearTimeout(timer);
    timer = setTimeout(save, 5000);
  }

  function save() {
    clearTimeout(timer);
    if (saving) return saving;
    var at = edits;
    var data = new FormData();
    data.append(field, getText());
    data.append('sha', currentSha());
    status('<span class="muted saving">保存中…</span>');
    saving = fetch(form.action, {
      method: 'POST', body: data, credentials: 'same-origin',
      headers: { 'HX-Request': 'true', 'X-RL-Editor': '1' }
    }).then(function (r) {
      return r.text().then(function (html) {
        if (r.ok) {
          setSha(r.headers.get('X-RL-Sha'));
          blocked = false; conflictEl.innerHTML = '';
          if (edits === at) { dirty = false; markDirty(false); }
          status(html);
          if (dirty) { timer = setTimeout(save, 5000); }
        } else if (r.status === 409) {
          blocked = true;
          conflictEl.innerHTML = html;
          conflictEl.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
          status('<span class="crit">■ 有冲突，未保存。你的文本仍在编辑器里。</span>');
        } else {
          status(html || '<span class="crit">■ 保存失败（' + r.status + '）</span>');
        }
      });
    }).catch(function () {
      status('<span class="crit">■ 无法连接应用，未保存。</span>');
    }).then(function () { saving = null; });
    return saving;
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    blocked = false;
    save();
  });
  // For pages that open this editor in place (the 产出页 描述): save now, then tell whether all is saved.
  window.rlEditor = { save: function () { blocked = false; return (save() || Promise.resolve()).then(function () { return !dirty && !blocked; }); } };

  // Capture phase on window, and stop it there: Toast UI binds ⌘S to strikethrough and would
  // otherwise insert "~~~~" into the document on every save.
  window.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && !e.altKey && (e.key === 's' || e.key === 'S')) {
      e.preventDefault();
      e.stopPropagation();
      blocked = false;
      save();
    }
  }, true);

  conflictEl.addEventListener('click', function (e) {
    var b = e.target.closest('[data-conflict]');
    if (!b) return;
    if (b.dataset.conflict === 'overwrite') {
      setSha(b.dataset.sha);
      blocked = false;
      save();
    } else if (b.dataset.conflict === 'reload' && sourceUrl) {
      fetch(sourceUrl, { credentials: 'same-origin' }).then(function (r) {
        var sha = r.headers.get('X-RL-Sha');
        return r.text().then(function (t) {
          setText(t); setSha(sha);
          dirty = false; blocked = false; conflictEl.innerHTML = ''; markDirty(false); count();
          status('<span class="muted">已载入磁盘上的版本。</span>');
        });
      });
    }
  });

  // The front-matter form shares the sha: flush pending body edits first so neither write is stale.
  var meta = document.getElementById('meta-form');
  if (meta) {
    meta.addEventListener('submit', function (e) {
      if (!dirty || meta.dataset.flushed) return;
      e.preventDefault();
      Promise.resolve(save()).then(function () {
        if (blocked) return;
        meta.dataset.flushed = '1';
        meta.submit();
      });
    });
  }

  window.addEventListener('beforeunload', function (e) {
    if (dirty && !(meta && meta.dataset.flushed)) { e.preventDefault(); e.returnValue = ''; }
  });
})();
