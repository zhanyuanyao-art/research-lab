// Shell behaviour: theme toggle, keyboard shortcuts, the create menu, flash/toast notices, busy buttons,
// page dropzones and a global PDF drop target.
(function () {
  // ---- Theme. The server renders <html data-theme-pref> (system | light | dark) and pins data-theme for
  // light/dark; "system" leaves it unset so the prefers-color-scheme rules in app.css apply. The toggle
  // cycles system → light → dark, applies it at once, persists through POST /_p/theme and dispatches
  // `rl:theme` {detail: {theme, dark}} so page scripts (the Markdown editor) can re-skin.
  var root = document.documentElement;
  var mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  function resolvedDark(pref) { return pref === 'dark' || (pref !== 'light' && !!(mq && mq.matches)); }
  function applyTheme(pref, persist) {
    root.classList.add('theme-switching');
    if (pref === 'light' || pref === 'dark') root.setAttribute('data-theme', pref); else root.removeAttribute('data-theme');
    root.setAttribute('data-theme-pref', pref);
    var t = document.getElementById('theme-toggle'); if (t) t.setAttribute('data-theme', pref);
    window.dispatchEvent(new CustomEvent('rl:theme', { detail: { theme: pref, dark: resolvedDark(pref) } }));
    setTimeout(function () { root.classList.remove('theme-switching'); }, 320);
    if (persist) {
      var data = new FormData(); data.append('theme', pref);
      fetch('/_p/theme', { method: 'POST', body: data, credentials: 'same-origin', headers: { 'HX-Request': 'true' } }).catch(function () {});
    }
  }
  var toggle = document.getElementById('theme-toggle');
  if (toggle) toggle.addEventListener('click', function () {
    var order = ['system', 'light', 'dark'];
    var next = order[(order.indexOf(root.getAttribute('data-theme-pref') || 'system') + 1) % order.length];
    applyTheme(next, true);
    toast({ system: '外观跟随系统', light: '浅色外观', dark: '深色外观' }[next]);
  });
  if (mq && mq.addEventListener) mq.addEventListener('change', function () {
    if ((root.getAttribute('data-theme-pref') || 'system') === 'system') applyTheme('system', false);
  });
  window.rlTheme = { apply: applyTheme, isDark: function () { return resolvedDark(root.getAttribute('data-theme-pref') || 'system'); } };

  // ---- Notices. Server flashes fade out by themselves; scripts call window.rlToast(text) for transient ones.
  function toast(text, ms) {
    var stack = document.getElementById('toast-stack'); if (!stack) return;
    var el = document.createElement('div'); el.className = 'toast'; el.textContent = text; stack.appendChild(el);
    setTimeout(function () { el.classList.add('leaving'); setTimeout(function () { el.remove(); }, 300); }, ms || 2200);
  }
  window.rlToast = toast;
  document.querySelectorAll('.flash').forEach(function (f) {
    setTimeout(function () { f.classList.add('leaving'); setTimeout(function () { f.remove(); }, 320); }, 4000);
  });

  // ---- Destructive forms ask first. Capture phase, then the attribute is cleared so a page script that
  // also honours data-confirm (weekly.js) does not ask again.
  document.addEventListener('submit', function (e) {
    var f = e.target; if (!f.dataset || !f.dataset.confirm) return;
    if (window.confirm(f.dataset.confirm)) f.removeAttribute('data-confirm'); else { e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);

  // ---- Busy buttons: a submitted form's primary button shows a spinner until the page changes.
  document.addEventListener('submit', function (e) {
    var form = e.target; if (form.hasAttribute('data-editor') || form.hasAttribute('hx-post') || form.hasAttribute('hx-get')) return;
    var b = form.querySelector('button[type=submit].btn:not(.ghost), .btn[type=submit]'); if (b) { b.classList.add('is-busy'); b.style.position = 'relative'; }
  });
  window.addEventListener('pageshow', function () { document.querySelectorAll('.btn.is-busy').forEach(function (b) { b.classList.remove('is-busy'); }); });
  document.addEventListener('htmx:afterSwap', function (e) {
    var t = e.detail && e.detail.target; if (!t) return;
    t.querySelectorAll('.flash').forEach(function (f) { setTimeout(function () { f.classList.add('leaving'); setTimeout(function () { f.remove(); }, 320); }, 4000); });
  });

  var search = document.getElementById('global-search');
  // On window, so it runs after the page scripts' document listeners: a page that handles a key itself
  // (⌘K in the chat composer, the task board's own keys) calls preventDefault and the global binding yields.
  window.addEventListener('keydown', function (e) {
    if (e.defaultPrevented) return;
    var typing = e.target.matches('input, textarea, select, [contenteditable], [contenteditable] *');
    if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === '/' && search) { e.preventDefault(); search.focus(); }
    if (e.key === 'e') { var x = document.querySelector('[data-hotkey=excerpt]'); if (x) { e.preventDefault(); x.click(); } }
    if (e.key === 'Escape') { document.querySelectorAll('details.menu[open]').forEach(function (d) { d.open = false; }); }
  });
  document.addEventListener('click', function (e) {
    document.querySelectorAll('details.menu[open]').forEach(function (d) { if (!d.contains(e.target)) d.open = false; });
  });

  // Page-level dropzones (#dropzone with #dropfile) submit as soon as files are chosen or dropped.
  var dz = document.getElementById('dropzone');
  if (dz) {
    var input = dz.querySelector('input[type=file]');
    ['dragenter', 'dragover'].forEach(function (ev) { dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add('over'); }); });
    ['dragleave', 'drop'].forEach(function (ev) { dz.addEventListener(ev, function () { dz.classList.remove('over'); }); });
    dz.addEventListener('drop', function (e) { e.preventDefault(); e.stopPropagation(); input.files = e.dataTransfer.files; submit(dz); });
    input.addEventListener('change', function () { if (input.files.length) submit(dz); });
  }
  function submit(form) {
    if (form.getAttribute('action') === '/papers') document.body.classList.add('importing');
    if (form.requestSubmit) form.requestSubmit(); else form.submit();
  }
  window.addEventListener('pageshow', function () { document.body.classList.remove('importing'); });

  // A file dropped outside a drop target must never make the window navigate to it (WKWebView and
  // browsers open dropped files by default). File inputs and dropzones keep their own behaviour.
  function isFileDrag(e) { var t = e.dataTransfer && e.dataTransfer.types; return t && Array.prototype.indexOf.call(t, 'Files') >= 0; }
  function inFileTarget(e) { return e.target.closest && e.target.closest('input[type=file], .dropzone, [data-composer], [data-dock]'); }
  ['dragover', 'drop'].forEach(function (ev) {
    window.addEventListener(ev, function (e) { if (isFileDrag(e) && !inFileTarget(e)) e.preventDefault(); });
  });

  // 新建 → 导入文献 is a label for the global form's input, so the file picker opens from any page.
  var gform = document.getElementById('global-drop-form');
  if (gform) gform.querySelector('input').addEventListener('change', function () { if (this.files.length) submit(gform); });

  // Global drop: a PDF dragged anywhere imports into the library, except on pages that take other files and
  // over the floating assistant, which takes them into its composer (it sits above this overlay).
  if (document.body.hasAttribute('data-no-global-drop')) return;
  var depth = 0;
  function hasPdf(e) {
    var items = e.dataTransfer && e.dataTransfer.items;
    if (!items) return false;
    for (var i = 0; i < items.length; i++) { if (items[i].kind === 'file' && (items[i].type === 'application/pdf' || items[i].type === '')) return true; }
    return false;
  }
  function inOwnTarget(e) { return e.target.closest && e.target.closest('.dropzone, [data-composer], [data-dock], input[type=file], [data-issue-drop]'); }
  document.addEventListener('dragenter', function (e) { if (!hasPdf(e) || inOwnTarget(e)) return; depth++; document.body.classList.add('dragging'); });
  document.addEventListener('dragleave', function () { depth = Math.max(0, depth - 1); if (!depth) document.body.classList.remove('dragging'); });
  document.addEventListener('dragover', function (e) { if (document.body.classList.contains('dragging')) e.preventDefault(); });
  document.addEventListener('drop', function (e) {
    if (inOwnTarget(e)) { depth = 0; document.body.classList.remove('dragging'); return; }
    if (!hasPdf(e)) return;
    e.preventDefault(); depth = 0; document.body.classList.remove('dragging');
    if (!gform || !e.dataTransfer.files.length) return;
    gform.querySelector('input').files = e.dataTransfer.files; submit(gform);
  });
})();

// Confirm the chosen file before the Coach imports it.
(function () {
  var form = document.querySelector('[data-coach-upload]');
  if (!form) return;
  var input = form.querySelector('input[type=file]');
  var label = form.querySelector('.dropzone strong');
  input.addEventListener('change', function () {
    label.textContent = input.files.length ? input.files[0].name : '选择或拖入 PDF';
  });
})();

// Return to the saved Coach step without leaving the native reader window.
(function () {
  if (new URLSearchParams(location.search).get('guide') !== '1' || !/^\/papers\/[^/]+(?:\/read)?$/.test(location.pathname)) return;
  var content = document.querySelector('main.content');
  if (!content) return;
  var banner = document.createElement('div');
  banner.className = 'banner';
  banner.setAttribute('aria-label', '上手引导');
  var text = document.createElement('span');
  text.className = 'text';
  text.textContent = '对照原文，找到研究问题和核心论点。';
  var link = document.createElement('a');
  link.className = 'btn secondary small';
  link.href = '/guide?step=3';
  link.textContent = '返回引导';
  banner.append(text, link);
  content.prepend(banner);
})();
