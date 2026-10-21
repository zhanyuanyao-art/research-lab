// Quick navigation stays local; stale replies cannot replace a newer query.
(function () {
  var dialog = document.getElementById('command-palette');
  if (!dialog) return;
  var input = dialog.querySelector('input'), results = dialog.querySelector('[role=listbox]');
  var timer, controller, generation = 0, index = -1, previous;
  function options() { return Array.from(results.querySelectorAll('[role=option]')); }
  function select(next) {
    var rows = options(); index = rows.length ? (next + rows.length) % rows.length : -1;
    rows.forEach(function (row, i) { row.id = 'palette-option-' + i; row.setAttribute('aria-selected', String(i === index)); });
    if (index >= 0) { input.setAttribute('aria-activedescendant', rows[index].id); rows[index].scrollIntoView({block:'nearest'}); }
    else input.removeAttribute('aria-activedescendant');
  }
  function invalidate() {
    clearTimeout(timer); generation++;
    if (controller) controller.abort();
    index = -1; input.removeAttribute('aria-activedescendant'); results.replaceChildren();
  }
  function note(message) { var p = document.createElement('p'); p.className = 'palette-note'; p.setAttribute('role','status'); p.textContent = message; results.replaceChildren(p); }
  async function load() {
    var turn = generation; controller = new AbortController(); results.setAttribute('aria-busy','true');
    try {
      var response = await fetch('/search/quick?q=' + encodeURIComponent(input.value), {signal:controller.signal});
      if (!response.ok) throw new Error('搜索暂不可用，请重试。');
      var html = await response.text();
      if (!dialog.open || turn !== generation) return;
      results.innerHTML = html; select(0);
    } catch (e) { if (dialog.open && turn === generation && e.name !== 'AbortError') note('搜索暂不可用；按 ⌘↵ 打开搜索页。'); }
    finally { if (turn === generation) results.setAttribute('aria-busy','false'); }
  }
  function open(value) {
    previous = document.activeElement;
    if (!dialog.open) dialog.showModal();
    input.value = value || ''; input.setAttribute('aria-expanded','true'); input.focus(); input.select(); invalidate(); load();
  }
  function close() { if (dialog.open) dialog.close(); }
  dialog.addEventListener('close', function () {
    // Native close events may arrive after the palette has already reopened.
    if (dialog.open) return;
    invalidate(); input.setAttribute('aria-expanded','false');
    if (previous && previous.isConnected) previous.focus();
  });
  dialog.querySelector('[data-palette-close]').addEventListener('click', close);
  dialog.addEventListener('click', function (e) { if (e.target === dialog) close(); });
  results.addEventListener('click', function (e) { if (e.target.closest('[role=option]')) close(); });
  results.addEventListener('mousemove', function (e) { var row = e.target.closest('[role=option]'); if (row) select(options().indexOf(row)); });
  function changed() { invalidate(); note('正在搜索…'); if (!input.composing) timer = setTimeout(load, 150); }
  input.addEventListener('input', changed);
  input.addEventListener('compositionstart', function () { input.composing = true; invalidate(); });
  input.addEventListener('compositionend', function () { input.composing = false; changed(); });
  function allResults() { location.href = '/search?q=' + encodeURIComponent(input.value); }
  dialog.querySelector('form').addEventListener('submit', function (e) {
    e.preventDefault(); if (input.composing) return;
    var row = options()[index]; if (row) row.click(); else allResults();
  });
  window.addEventListener('keydown', function (e) {
    if (e.isComposing) return;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault(); e.stopPropagation();
      if (dialog.open) close(); else open(document.getElementById('global-search')?.value);
      return;
    }
    if (!dialog.open) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); e.stopPropagation(); select(index + (e.key === 'ArrowDown' ? 1 : -1)); }
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); allResults(); }
  }, true);
  // The visible search field remains a normal search form when scripts are unavailable.
  var global = document.getElementById('global-search');
  if (global) global.addEventListener('click', function () { if (!dialog.open) open(global.value); });
})();
