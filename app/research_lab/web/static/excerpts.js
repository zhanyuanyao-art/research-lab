// Excerpt capture: selection popover in the reader, the `e` drawer, ⌘↩ save, 1–7 use toggles.
// Contract: pages include partials/_excerpt_form.html (drawer + [data-hotkey=excerpt] trigger); the reader
// renders pages as <section class="pdf-page" data-page-index data-page-label [data-file-id]>.
(function () {
  if (window.__rlExcerpts) return;
  window.__rlExcerpts = true;

  var last = null;       // {text, pageIndex, pageLabel, fileId} of the latest selection inside a page
  var popover = null;

  function drawer() { return document.getElementById('excerpt-drawer'); }
  // The drawer is rendered inside sticky page columns, which trap z-index; lift it to <body>.
  (function () { var d = drawer(); if (d && d.parentElement !== document.body) document.body.appendChild(d); })();

  function pageOf(node) {
    var el = node && (node.nodeType === 1 ? node : node.parentElement);
    return el ? el.closest('section.pdf-page') : null;
  }

  function readSelection() {
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    var text = sel.toString().trim();
    var page = pageOf(sel.anchorNode);
    if (!text || !page) return null;
    return { text: text, pageIndex: page.dataset.pageIndex, pageLabel: page.dataset.pageLabel, fileId: page.dataset.fileId,
             rect: sel.getRangeAt(0).getBoundingClientRect() };
  }

  function hidePopover() { if (popover) { popover.remove(); popover = null; } }

  function showPopover(s) {
    hidePopover();
    popover = document.createElement('button');
    popover.type = 'button';
    popover.className = 'excerpt-popover';
    popover.textContent = 'Highlight E';
    popover.style.top = (window.scrollY + s.rect.bottom + 6) + 'px';
    popover.style.left = (window.scrollX + s.rect.left) + 'px';
    popover.addEventListener('mousedown', function (e) { e.preventDefault(); });   // keep the selection
    popover.addEventListener('click', function () { openForm(); });
    document.body.appendChild(popover);
  }

  document.addEventListener('mouseup', function (e) {
    if (e.target.closest && e.target.closest('.excerpt-popover, .excerpt-drawer')) return;
    setTimeout(function () {
      var s = readSelection();
      if (s) { last = s; showPopover(s); } else { hidePopover(); }
    }, 0);
  });

  function openForm() {
    var d = drawer();
    if (!d || !window.htmx) return;
    var s = readSelection() || last;
    var params = new URLSearchParams();
    if (d.dataset.paperId) params.set('paper_id', d.dataset.paperId);
    var fileId = (s && s.fileId) || d.dataset.fileId;
    if (fileId) params.set('file_id', fileId);
    if (s) {
      if (s.pageIndex !== undefined && s.pageIndex !== '') params.set('page_index', s.pageIndex);
      if (s.pageLabel) params.set('page_label', s.pageLabel);
      params.set('quote', s.text.slice(0, 6000));
    } else {
      // No selection: the 页码 defaults to the page the reader is on (papers.js keeps it on #reader).
      var reader = document.getElementById('reader');
      var cur = reader && reader.dataset;
      if (cur && cur.currentPageIndex) params.set('page_index', cur.currentPageIndex);
      if (cur && cur.currentPageLabel) params.set('page_label', cur.currentPageLabel);
    }
    hidePopover();
    d.hidden = false;
    htmx.ajax('GET', '/_p/excerpt-form?' + params.toString(), { target: '#excerpt-drawer-body', swap: 'innerHTML' });
  }

  function closeForm() { var d = drawer(); if (d) d.hidden = true; }

  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-hotkey=excerpt]')) { e.preventDefault(); openForm(); }
    if (e.target.closest('[data-drawer-close]')) { closeForm(); }
  });

  document.body.addEventListener('htmx:afterSwap', function (e) {
    if (e.target && e.target.id === 'excerpt-drawer-body') {
      var note = e.target.querySelector('[data-autofocus]');
      if (note) note.focus();
    }
  });

  // ---- reader: marks in the text and the 划线 tab point at each other (docs/design/PAPERS.md §C).
  function flash(el) { if (!el) return; el.classList.remove('is-flash'); void el.offsetWidth; el.classList.add('is-flash'); }
  document.addEventListener('click', function (e) {
    var m = e.target.closest && e.target.closest('#reader mark.hl[data-excerpt]');
    if (m && !(window.getSelection() && !window.getSelection().isCollapsed)) {
      var tab = document.querySelector('[data-side-tab="marks"]'); if (tab) tab.click();
      var item = document.getElementById('ex-' + m.dataset.excerpt);
      if (item) { item.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); flash(item); }
      return;
    }
    var li = e.target.closest && e.target.closest('#paper-excerpts li[data-excerpt]');
    if (li && !e.target.closest('a')) {
      var mark = document.querySelector('#reader mark.hl[data-excerpt="' + li.dataset.excerpt + '"]');
      if (mark) { mark.scrollIntoView({ block: 'center', behavior: 'smooth' }); document.querySelectorAll('#reader mark.hl[data-excerpt="' + li.dataset.excerpt + '"]').forEach(flash); }
    }
  });

  // After a save in the reader: fetch the page again and swap in the saved page's text (now marked) and
  // the 划线 tab (now listing it), without reloading or moving the scroll position.
  function refreshReader(detail) {
    var reader = document.getElementById('reader');
    if (!reader) return;
    fetch(location.pathname + location.search, { credentials: 'same-origin' }).then(function (r) { return r.text(); }).then(function (html) {
      var doc = new DOMParser().parseFromString(html, 'text/html');
      var list = doc.getElementById('paper-excerpts'), here = document.getElementById('paper-excerpts');
      if (list && here) here.replaceWith(list);
      var count = doc.querySelector('[data-side-tab="marks"] .count'), cHere = document.querySelector('[data-side-tab="marks"] .count');
      if (count && cHere) cHere.textContent = count.textContent;
      var idx = last && last.pageIndex;
      var pages = idx !== undefined && idx !== null && idx !== '' ? ['p-' + idx] : [];
      if (!pages.length) document.querySelectorAll('#reader section.pdf-page').forEach(function (s) { pages.push(s.id); });
      pages.forEach(function (id) {
        var fresh = doc.querySelector('#' + id + ' .page-text'), old = document.querySelector('#' + id + ' .page-text');
        if (fresh && old) old.replaceWith(fresh);
      });
      if (detail && detail.id) {
        document.querySelectorAll('#reader mark.hl[data-excerpt="' + detail.id + '"]').forEach(flash);
        flash(document.getElementById('ex-' + detail.id));
      }
    }).catch(function () {});
  }

  document.body.addEventListener('excerpt-saved', function (e) {
    refreshReader(e.detail && (e.detail.value || e.detail));
  });

  document.body.addEventListener('excerpt-saved', function () {
    var form = document.querySelector('#excerpt-drawer form.excerpt-form');
    if (!form) return;
    ['quote', 'my_note'].forEach(function (n) { var f = form.elements[n]; if (f) f.value = ''; });
    var errs = form.querySelector('#excerpt-form-errors'); if (errs) errs.innerHTML = '';
    last = null;
    var sel = window.getSelection(); if (sel) sel.removeAllRanges();
    if (form.elements.quote) form.elements.quote.focus();
  });

  document.addEventListener('keydown', function (e) {
    var form = e.target.closest && e.target.closest('form.excerpt-form');
    if (form && e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      form.requestSubmit ? form.requestSubmit() : form.submit();
      return;
    }
    var d = drawer();
    if (!d || d.hidden) return;
    if (e.key === 'Escape') { closeForm(); return; }
    if (e.target.matches('input[type=text], textarea, select, [contenteditable]') || e.metaKey || e.ctrlKey || e.altKey) return;
    var f = d.querySelector('form.excerpt-form');
    if (!f) return;
    if (/^[1-7]$/.test(e.key)) {
      var boxes = f.querySelectorAll('.use-pills input[type=checkbox]');
      var box = boxes[parseInt(e.key, 10) - 1];
      if (box) { e.preventDefault(); box.checked = !box.checked; }
    } else if (e.key === 'c') {
      var cases = f.querySelector('[name=cases]'); if (cases) { e.preventDefault(); cases.focus(); }
    }
  });
})();
