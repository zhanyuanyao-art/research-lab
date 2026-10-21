// Reader: j/k page navigation, "g p" page-image toggle, find-in-paper, current-page tracking,
// the excerpt guard (translated text, [data-no-excerpt], never reaches the excerpt capture), the
// 标注区 tabs on the paper page; on the library page the list (filter, J/K, preview, row menu) and drag-to-file.
(function () {
  // Runs in the capture phase on document, before excerpts.js reads the selection on mouseup.
  document.addEventListener('mouseup', function () {
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return;
    function inTrans(node) {
      var el = node && (node.nodeType === 1 ? node : node.parentElement);
      return !!(el && el.closest('[data-no-excerpt]'));
    }
    if (inTrans(sel.anchorNode) || inTrans(sel.focusNode)) sel.removeAllRanges();
  }, true);

  var reader = document.getElementById('reader');
  if (!reader) return;
  var pages = Array.prototype.slice.call(reader.querySelectorAll('section.pdf-page'));
  var label = document.getElementById('current-page');
  var bar = document.getElementById('reader-toolbar');
  var current = null;

  function setCurrent(sec) {
    if (!sec || sec === current) return;
    if (current) current.classList.remove('current');
    current = sec;
    sec.classList.add('current');
    reader.dataset.currentPageIndex = sec.dataset.pageIndex;
    reader.dataset.currentPageLabel = sec.dataset.pageLabel;
    if (label) label.textContent = '第 ' + sec.dataset.pageLabel + ' 页';
  }
  function byIndex(i) { return reader.querySelector('section.pdf-page[data-page-index="' + i + '"]'); }

  // Pages scroll under the sticky top bar and reader toolbar, so what the reader sees starts at the
  // toolbar's bottom edge. It is measured on every call: the toolbar wraps to more rows in narrow windows.
  function pageAtScroll() {
    var top = bar ? Math.max(0, bar.getBoundingClientRect().bottom) : 0;
    var line = top + 0.25 * Math.max(0, window.innerHeight - top);
    var hit = pages[0] || null;
    for (var n = 0; n < pages.length; n++) {
      if (pages[n].getBoundingClientRect().top <= line) hit = pages[n]; else break;
    }
    return hit;
  }
  // The page a jump scrolled to stays current until the window moves: a short page leaves the next one
  // above the line, and the last pages cannot scroll up to the toolbar at all.
  var landed = null, landedY = 0;
  function visiblePage() {
    if (landed && Math.abs(window.scrollY - landedY) < 2) return landed;
    landed = null;
    return pageAtScroll();
  }
  function go(sec) {
    if (!sec) return;
    sec.scrollIntoView({ block: 'start' });
    landed = sec;
    landedY = window.scrollY;
    setCurrent(sec);
  }

  var ticking = false;
  window.addEventListener('scroll', function () {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(function () { ticking = false; setCurrent(visiblePage()); });
  }, { passive: true });

  var showImages = false;
  function toggleImages() {
    showImages = !showImages;
    pages.forEach(function (p) {
      var img = p.querySelector('img.page-img');
      if (!img) return;
      if (showImages && !img.getAttribute('src')) img.setAttribute('src', img.dataset.src);
      img.hidden = !showImages;
    });
    if (current) go(current);
  }

  var gPressed = 0;
  document.addEventListener('keydown', function (e) {
    if (e.target.matches('input, textarea, select, [contenteditable]')) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === 'j' || e.key === 'k') {
      e.preventDefault();
      var i = pages.indexOf(visiblePage());
      go(pages[e.key === 'j' ? Math.min(pages.length - 1, i + 1) : Math.max(0, i - 1)]);
    }
    else if (e.key === 'g') { gPressed = Date.now(); }
    else if (e.key === 'p' && Date.now() - gPressed < 1000) { e.preventDefault(); gPressed = 0; toggleImages(); }
  });

  var find = document.getElementById('paper-find');
  if (find) {
    find.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { find.blur(); return; }
      if (e.key !== 'Enter') return;
      e.preventDefault();
      var q = find.value.trim().toLowerCase();
      if (!q) return;
      var start = current ? pages.indexOf(current) + 1 : 0;
      for (var n = 0; n < pages.length; n++) {
        var sec = pages[(start + n) % pages.length];
        if (sec.textContent.toLowerCase().indexOf(q) !== -1) {
          pages.forEach(function (p) { p.classList.remove('found'); });
          sec.classList.add('found');
          go(sec);
          return;
        }
      }
      find.classList.add('miss');
      setTimeout(function () { find.classList.remove('miss'); }, 600);
    });
  }

  reader.addEventListener('click', function (e) {
    var a = e.target.closest('a[data-jump]');
    if (a && !(e.metaKey || e.ctrlKey)) { var sec = byIndex(a.dataset.jump); if (sec) { e.preventDefault(); go(sec); } }
  });

  var initial = reader.dataset.initialPage;
  if (initial !== undefined) { var s = byIndex(initial); if (s) setTimeout(function () { go(s); }, 0); }
  else setCurrent(visiblePage());
})();

// 标注区 tabs on the paper page (pages/paper.html): 划线 · 概要 · 信息. #marks / #summary / #info in the
// URL open a tab (older links point at #marks); any [data-side-tab] control elsewhere, such as the
// toolbar's 引用信息与核验, switches too.
(function () {
  // The sticky toolbar wraps to two or three rows depending on the window; keep the rail and the page
  // anchors just below whatever height it has.
  var bar = document.getElementById('reader-toolbar');
  if (bar) {
    var place = function () { document.documentElement.style.setProperty('--reader-top', Math.round(bar.getBoundingClientRect().height + 52) + 'px'); };
    place();
    window.addEventListener('resize', place);
    if (window.ResizeObserver) new ResizeObserver(place).observe(bar);
  }
  var nav = document.getElementById('side-tabs');
  if (!nav) return;
  var names = ['marks', 'summary', 'info'];
  function show(name, push) {
    if (names.indexOf(name) < 0) return;
    nav.querySelectorAll('[data-side-tab]').forEach(function (a) { a.classList.toggle('on', a.dataset.sideTab === name); });
    names.forEach(function (n) { var el = document.getElementById('side-' + n); if (el) el.hidden = n !== name; });
    if (push) history.replaceState(null, '', location.pathname + location.search + '#' + name);
  }
  document.addEventListener('click', function (e) {
    var a = e.target.closest('[data-side-tab]'); if (!a) return;
    e.preventDefault();
    show(a.dataset.sideTab, true);
    var target = a.dataset.sideFocus && document.getElementById(a.dataset.sideFocus);
    if (target) target.scrollIntoView({ block: 'nearest' });
    var menu = a.closest('details.menu'); if (menu) menu.open = false;
  });
  var h = (location.hash || '').slice(1);
  if (names.indexOf(h) >= 0) show(h, false);
})();


// Folder browser in the content area, with breadcrumb navigation. Native drag and keyboard/menu alternatives share the same writes.
(function () {
  if (!document.getElementById('library')) return;
  var dragging = null, busy = false;
  function notice(text) { if (window.rlToast) window.rlToast(text); }
  async function mutate(url, data, form) {
    if (busy) return;
    busy = true;
    var library = document.getElementById('library');
    library.setAttribute('aria-busy', 'true');
    var controls = form ? Array.from(form.querySelectorAll('input,button')).filter(function (c) { return !c.disabled; }) : [];
    controls.forEach(function (c) { c.disabled = true; });
    if (form) { var old = form.querySelector('[role=alert]'); if (old) old.remove(); }
    try {
      var response = await fetch(url, {method:'POST',body:data,headers:{'X-Library-Form':'1'}});
      var result = await response.json();
      if (!response.ok) throw new Error(result.error || '保存失败，请重试。');
      if (form && form.hasAttribute('data-new-folder-form')) { location.assign(result.url); return; }
      var refreshed = await fetch(location.href);
      if (!refreshed.ok) throw new Error('已保存，刷新失败，请重新打开文献页。');
      var doc = new DOMParser().parseFromString(await refreshed.text(),'text/html');
      if (!doc.getElementById('library')) throw new Error('已保存，请刷新核对。');
      ['#library','.page-header .titles','.topbar-crumbs'].forEach(function (selector) {
        var old = document.querySelector(selector), fresh = doc.querySelector(selector);
        if (old && fresh) old.replaceWith(fresh);
      });
    } catch (error) {
      if (form) {
        var p = document.createElement('p'); p.className = 'form-error'; p.setAttribute('role','alert'); p.textContent = error.message; form.appendChild(p);
      } else notice(error.message);
    } finally {
      busy = false; library.removeAttribute('aria-busy');
      controls.forEach(function (c) { c.disabled = false; c.classList.remove('is-busy'); });
    }
  }
  function rename(row) {
    if (!row || busy) return;
    var menu = row.querySelector('details'); if (menu) menu.open = false;
    var form = row.querySelector('form'); form.hidden = false;
    form.elements.name.focus(); form.elements.name.select();
  }
  function order(ids) {
    var data = new FormData();
    data.append('version', document.querySelector('[data-folder-order]').dataset.folderOrder);
    ids.forEach(function (id) { data.append('ids', id); });
    return mutate('/paper-collections/order', data);
  }
  function folderIds() { return Array.from(document.querySelectorAll('[data-folder-id]')).map(function (r) { return r.dataset.folderId; }); }
  document.addEventListener('click', function (e) {
    var add = e.target.closest('[data-new-folder]');
    if (add && !busy) { var form = document.querySelector('[data-new-folder-form]'); form.hidden = false; form.elements.name.focus(); }
    var edit = e.target.closest('[data-rename-collection]');
    if (edit) rename(edit.closest('[data-folder-id],[data-current-folder]'));
    var cancel = e.target.closest('[data-cancel-folder]');
    if (cancel && !busy) cancelEdit(cancel.closest('.folder-name-form'));
    var step = e.target.closest('[data-folder-step]');
    if (step && !busy) {
      var ids = folderIds(), i = ids.indexOf(step.closest('[data-folder-id]').dataset.folderId), j = i + Number(step.dataset.folderStep);
      if (j >= 0 && j < ids.length) { var tmp = ids[i]; ids[i] = ids[j]; ids[j] = tmp; order(ids); }
    }
  });
  function cancelEdit(form) {
    form.reset(); form.hidden = true;
    var row = form.closest('[data-folder-id],[data-current-folder]');
    var focus = row ? row.querySelector('a,[data-rename-collection]') : document.querySelector('[data-new-folder]');
    if (focus) focus.focus();
  }
  document.addEventListener('keydown', function (e) {
    if (e.key === 'F2' && !e.target.closest('input,textarea') && e.target.closest('[data-folder-id]')) { e.preventDefault(); rename(e.target.closest('[data-folder-id]')); }
    var form = e.target.closest('.folder-name-form');
    if (form && e.key === 'Escape' && !busy) {
      e.preventDefault(); cancelEdit(form);
    }
  });
  document.addEventListener('submit', function (e) {
    var form = e.target.closest('[data-library-form]'); if (!form) return;
    e.preventDefault();
    if (busy) { form.querySelectorAll('.is-busy').forEach(function (b) { b.classList.remove('is-busy'); }); return; }
    mutate(form.action, new FormData(form), form);
  });
  document.addEventListener('dragstart', function (e) {
    if (busy || e.target.closest('input,button,summary')) { e.preventDefault(); return; }
    var row = e.target.closest('[data-folder-id],.paper-line'); if (!row) return;
    dragging = {kind:row.hasAttribute('data-folder-id') ? 'folder' : 'paper', id:row.dataset.folderId || row.dataset.paperId};
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('application/x-research-lab-' + dragging.kind, dragging.id);
    row.classList.add('dragging');
  });
  function target(e) { return e.target.closest(dragging && dragging.kind === 'folder' ? '[data-folder-id]' : '[data-drop-collection],[data-drop-unfiled]'); }
  function clearDragMarks() { document.querySelectorAll('.drop-target,.drop-before,.drop-after').forEach(function (n) { n.classList.remove('drop-target','drop-before','drop-after'); }); }
  document.addEventListener('dragover', function (e) {
    if (!dragging || busy) return;
    var row = target(e); if (!row) return;
    e.preventDefault(); e.dataTransfer.dropEffect = 'move'; clearDragMarks();
    row.classList.add(dragging.kind === 'paper' ? 'drop-target' : (e.clientY < row.getBoundingClientRect().top + row.offsetHeight / 2 ? 'drop-before' : 'drop-after'));
  });
  document.addEventListener('drop', function (e) {
    if (!dragging || busy) return;
    var row = target(e); if (!row) return;
    e.preventDefault();
    if (dragging.kind === 'folder') {
      if (row.dataset.folderId !== dragging.id) {
        var ids = folderIds().filter(function (id) { return id !== dragging.id; });
        ids.splice(ids.indexOf(row.dataset.folderId) + (row.classList.contains('drop-after') ? 1 : 0), 0, dragging.id);
        order(ids);
      }
    } else {
      var data = new FormData(); data.append('move','1'); data.append('collection',row.dataset.dropCollection || '');
      mutate('/papers/' + encodeURIComponent(dragging.id) + '/archive', data);
    }
    clearDragMarks();
  });
  document.addEventListener('dragend', function () {
    dragging = null; clearDragMarks(); document.querySelectorAll('.dragging').forEach(function (r) { r.classList.remove('dragging'); });
  });
})();


// The library list (card 0128, docs/design/LIBRARY-UI.md §3.1): the filter field narrows the rows on screen as
// you type (Enter still searches the whole library on the server); J and K move the selection, Enter opens it,
// a click on a row's blank space selects it; from 1600px the selection fills the preview column. Each row's ⋯
// menu is cloned from #paper-line-menu when it first opens. Elements are looked up on every use: a folder
// change re-renders #library (the folder browser block above).
(function () {
  if (!document.querySelector('[data-line-list]')) return;
  var wide = window.matchMedia('(min-width: 1600px)');
  var loading = null;

  function list() { return document.querySelector('[data-line-list]'); }
  function rows() {
    var l = list();
    return l ? Array.prototype.filter.call(l.querySelectorAll('.paper-line'), function (r) { return !r.hidden; }) : [];
  }
  function current() { var l = list(); return l && l.querySelector('.paper-line.is-selected'); }
  function loadPeek(row) {
    var peek = document.getElementById('lib-peek');
    if (!peek || !wide.matches || !row) return;
    var id = row.dataset.paperId;
    if (peek.dataset.paperId === id) return;
    peek.dataset.paperId = id;
    if (loading) loading.abort();
    loading = new AbortController();
    fetch('/papers/' + encodeURIComponent(id) + '/peek', { signal: loading.signal })
      .then(function (r) { return r.ok ? r.text() : ''; })
      .then(function (html) { if (peek.dataset.paperId === id) peek.innerHTML = html; })
      .catch(function () {});
  }
  function select(row, scroll) {
    if (!row) return;
    var old = current();
    if (old) { old.classList.remove('is-selected'); old.removeAttribute('aria-selected'); }
    row.classList.add('is-selected');
    row.setAttribute('aria-selected', 'true');
    if (scroll) row.scrollIntoView({ block: 'nearest' });
    loadPeek(row);
  }
  function step(n) {
    var all = rows();
    if (!all.length) return;
    var i = all.indexOf(current());
    select(all[i < 0 ? 0 : Math.max(0, Math.min(all.length - 1, i + n))], true);
  }
  function first() {
    if (!wide.matches) return;
    var sel = current();
    if (sel && !sel.hidden) loadPeek(sel); else select(rows()[0], false);
  }

  document.addEventListener('keydown', function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey || e.target.closest('input, textarea, select, [contenteditable], details[open]')) return;
    if (e.key === 'j' || e.key === 'J') { e.preventDefault(); step(1); }
    else if (e.key === 'k' || e.key === 'K') { e.preventDefault(); step(-1); }
    else if (e.key === 'Enter' && current() && !e.target.closest('a, button, summary')) {
      e.preventDefault();
      current().querySelector('.pl-title').click();
    }
  });
  document.addEventListener('click', function (e) {
    var row = e.target.closest('[data-line-list] .paper-line');
    if (row && !e.target.closest('a, button, summary, details, .tip')) select(row, false);
  });
  first();
  if (wide.addEventListener) wide.addEventListener('change', first);
  var lib = document.getElementById('library');
  if (lib && lib.parentNode && window.MutationObserver) {
    new MutationObserver(function () { if (!document.getElementById('lib-peek') || !document.getElementById('lib-peek').dataset.paperId) first(); })
      .observe(lib.parentNode, { childList: true });
  }

  // Filter as you type: title, authors and DOI (data-filter). Empty groups hide; the count follows.
  document.addEventListener('input', function (e) {
    var field = e.target.closest('[data-line-filter]');
    var l = list();
    if (!field || !l) return;
    var total = document.querySelector('[data-line-total]');
    if (total && total.dataset.all === undefined) total.dataset.all = total.textContent;
    var words = field.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    var shown = 0;
    l.querySelectorAll('.paper-line').forEach(function (r) {
      var hay = r.dataset.filter || '';
      r.hidden = !words.every(function (w) { return hay.indexOf(w) !== -1; });
      if (!r.hidden) shown++;
    });
    l.querySelectorAll('[data-line-group]').forEach(function (g) { g.hidden = !g.querySelector('.paper-line:not([hidden])'); });
    if (total) total.textContent = words.length ? shown + ' 篇' : total.dataset.all;
    var none = l.querySelector('[data-line-none]');
    if (none) none.hidden = shown > 0;
    var sel = current();
    if (sel && sel.hidden) { sel.classList.remove('is-selected'); first(); }
  });
  document.addEventListener('keydown', function (e) {
    var field = e.target.closest && e.target.closest('[data-line-filter]');
    if (field && e.key === 'Escape' && field.value) { e.preventDefault(); field.value = ''; field.dispatchEvent(new Event('input', { bubbles: true })); }
  });

  // Row menus: one template for the whole page, the paper's id put in when a menu first opens.
  document.addEventListener('toggle', function (e) {
    var menu = e.target, tpl = document.getElementById('paper-line-menu');
    if (!tpl || !menu.matches || !menu.matches('details.line-menu') || !menu.open) return;
    var panel = menu.querySelector('.menu-panel');
    if (panel.childElementCount) return;
    var row = menu.closest('.paper-line');
    var id = encodeURIComponent(row.dataset.paperId);
    panel.appendChild(tpl.content.cloneNode(true));
    panel.querySelectorAll('[action], [formaction], [href]').forEach(function (el) {
      ['action', 'formaction', 'href'].forEach(function (a) { var v = el.getAttribute(a); if (v) el.setAttribute(a, v.replace('__ID__', id)); });
    });
    panel.querySelectorAll('[data-when]').forEach(function (el) { if (el.dataset.when.split(' ').indexOf(row.dataset.state) < 0) el.remove(); });
    if (row.dataset.file === '0') panel.querySelectorAll('[data-when-file]').forEach(function (el) { el.remove(); });
  }, true);
})();
