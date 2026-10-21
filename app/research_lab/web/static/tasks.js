// 待办 page behaviour (docs/design/TASKS.md §D): keyboard shortcuts on rows, add-a-sub-task inline,
// due date edited in place, drag / Alt+arrow reordering, expand / collapse of a parent's sub-tasks.
// Everything posts the same forms the page already has and swaps #task-board back; without this
// script the forms still work one click at a time.
(function () {
  var board = document.getElementById('task-board');
  if (!board) return;

  function rows() { return Array.prototype.slice.call(document.querySelectorAll('#task-board .task-row')); }
  function focused() { var el = document.activeElement; return el && el.closest ? el.closest('.task-row') : null; }
  function childrenOf(row) { return document.querySelector('#task-board .task-children[data-for="' + row.dataset.id + '"]'); }
  function reload(url) {
    var b = document.getElementById('task-board');
    var q = url || ('/tasks?view=' + encodeURIComponent(b.dataset.view) + (b.dataset.filter ? '&project=' + encodeURIComponent(b.dataset.filter) : ''));
    if (window.htmx) window.htmx.ajax('GET', q, { target: '#task-board', select: '#task-board', swap: 'outerHTML' });
    else window.location.href = q;
  }
  // The segmented control and the URL sit outside #task-board, so a keyboard view switch updates them here.
  function switchView(view) {
    var b = document.getElementById('task-board');
    var q = '/tasks?view=' + view + (b.dataset.filter ? '&project=' + encodeURIComponent(b.dataset.filter) : '');
    document.querySelectorAll('.task-toolbar .segmented a').forEach(function (a) {
      if (new URL(a.href, window.location.href).searchParams.get('view') === view) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
    var hidden = document.querySelector('.task-toolbar input[name=view]'); if (hidden) hidden.value = view;
    if (window.history && window.history.replaceState) window.history.replaceState(null, '', q);
    reload(q);
  }
  function post(url, data) {
    var fd = new FormData();
    Object.keys(data).forEach(function (k) { fd.append(k, data[k]); });
    var b = document.getElementById('task-board');
    fd.append('view', b.dataset.view); fd.append('filter', b.dataset.filter);
    return fetch(url, { method: 'POST', body: fd, credentials: 'same-origin', headers: { 'HX-Request': 'true' } })
      .then(function (r) { if (!r.ok && r.status !== 204) { window.rlToast && window.rlToast('没有保存，请刷新后再试'); } reload(); });
  }

  // ---- expand / collapse. Parents whose sub-tasks are not due this week start collapsed (data-collapsed).
  function applyCollapsed() {
    document.querySelectorAll('#task-board .task-children').forEach(function (c) {
      var row = document.querySelector('#task-board .task-row[data-id="' + c.dataset.for + '"]');
      var btn = row && row.querySelector('[data-toggle]');
      var collapsed = c.dataset.collapsed === '1';
      c.hidden = collapsed;
      if (btn) btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    });
  }
  function toggle(row) {
    var c = childrenOf(row); if (!c) return;
    c.dataset.collapsed = c.hidden ? '' : '1';
    applyCollapsed();
  }

  // ---- add a sub-task: focus the ghost input under the row (expanding it first); Enter submits, the
  // board comes back and the same input is focused again so several can be typed in a row.
  var refocus = null;
  function addSub(row) {
    var c = childrenOf(row); if (!c) return;
    c.dataset.collapsed = ''; applyCollapsed();
    var input = c.querySelector('.task-add-form input[name=title]');
    if (input) { input.focus(); refocus = row.dataset.id; }
  }

  // ---- due date in place: click the label, pick a date, blur or Enter saves; clearing removes it.
  function editDue(row, span) {
    var input = document.createElement('input');
    input.type = 'date'; input.value = span.dataset.due || ''; input.className = 'task-due-input';
    span.replaceWith(input); input.focus();
    var done = false;
    function save() {
      if (done) return; done = true;
      if (input.value === (span.dataset.due || '')) { input.replaceWith(span); return; }
      post('/tasks/' + row.dataset.id + '/edit', { due: input.value });
    }
    input.addEventListener('change', save);
    input.addEventListener('blur', save);
    input.addEventListener('keydown', function (e) { if (e.key === 'Escape') { done = true; input.replaceWith(span); } if (e.key === 'Enter') save(); });
  }

  // ---- reorder within one list (siblings only): drag the handle, or Alt+↑ / Alt+↓ on the focused row.
  function siblings(row) { return Array.prototype.slice.call(row.parentElement.querySelectorAll(':scope > .task-row')); }
  function moveRow(row, delta) {
    var list = siblings(row), i = list.indexOf(row), j = i + delta;
    if (j < 0 || j >= list.length) return;
    var before = delta < 0 ? list[j] : list[j + 1];
    post('/tasks/' + row.dataset.id + '/move', { before: before ? before.dataset.id : '' }).then(function () {
      var again = document.querySelector('#task-board .task-row[data-id="' + row.dataset.id + '"]'); if (again) again.focus();
    });
  }
  var dragging = null;
  board.addEventListener('dragstart', function (e) {
    var h = e.target.closest && e.target.closest('.task-handle'); if (!h) return;
    dragging = h.closest('.task-row'); dragging.classList.add('is-dragging');
    e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', dragging.dataset.id);
  });
  board.addEventListener('dragover', function (e) {
    if (!dragging) return;
    var over = e.target.closest && e.target.closest('.task-row');
    if (!over || over === dragging || over.parentElement !== dragging.parentElement) return;
    e.preventDefault();
    var r = over.getBoundingClientRect();
    over.classList.toggle('drop-before', e.clientY < r.top + r.height / 2);
    over.classList.toggle('drop-after', e.clientY >= r.top + r.height / 2);
  });
  board.addEventListener('dragleave', function (e) { var t = e.target.closest && e.target.closest('.task-row'); if (t) t.classList.remove('drop-before', 'drop-after'); });
  board.addEventListener('drop', function (e) {
    if (!dragging) return;
    var over = e.target.closest && e.target.closest('.task-row');
    if (!over || over === dragging || over.parentElement !== dragging.parentElement) return;
    e.preventDefault();
    var after = over.classList.contains('drop-after');
    var list = siblings(over), before = after ? list[list.indexOf(over) + 1] : over;
    over.classList.remove('drop-before', 'drop-after');
    post('/tasks/' + dragging.dataset.id + '/move', { before: before ? before.dataset.id : '' });
  });
  board.addEventListener('dragend', function () { if (dragging) dragging.classList.remove('is-dragging'); dragging = null; document.querySelectorAll('.drop-before, .drop-after').forEach(function (x) { x.classList.remove('drop-before', 'drop-after'); }); });

  // Submitting the ghost input (Enter) should bring the focus back to it after the board returns.
  board.addEventListener('submit', function (e) {
    var f = e.target.closest && e.target.closest('.task-add-form'); if (!f) return;
    var c = f.closest('.task-children'); refocus = c ? c.dataset.for : null;
  }, true);

  // ---- clicks: toggle, due label
  document.addEventListener('click', function (e) {
    var t = e.target.closest && e.target.closest('[data-toggle]'); if (t) { toggle(t.closest('.task-row')); return; }
    var d = e.target.closest && e.target.closest('.task-due'); if (d) { editDue(d.closest('.task-row'), d); return; }
    var h = e.target.closest && e.target.closest('[data-help-close]'); if (h) { document.getElementById('task-help').hidden = true; }
  });

  // ---- keyboard
  var VIEWS = ['week', 'overdue', 'all'];
  document.addEventListener('keydown', function (e) {
    if (e.metaKey || e.ctrlKey) return;
    var typing = !!(e.target && e.target.matches && e.target.matches('input, textarea, select, [contenteditable]'));
    if (typing) {
      if (e.key === 'Escape' && e.target.closest('.task-add-form')) { e.target.blur(); var r = e.target.closest('.task-children'); var p = r && document.querySelector('#task-board .task-row[data-id="' + r.dataset.for + '"]'); if (p) p.focus(); }
      return;
    }
    if (e.key === '?') { var help = document.getElementById('task-help'); if (help) help.hidden = !help.hidden; return; }
    var board = document.getElementById('task-board');
    if (e.key === 'v' && board && document.querySelector('.task-toolbar .segmented')) { e.preventDefault(); switchView(VIEWS[(VIEWS.indexOf(board.dataset.view) + 1) % VIEWS.length]); return; }
    var row = focused(), all = rows();
    if (e.key === 'j' || e.key === 'k') {
      e.preventDefault();
      var i = row ? all.indexOf(row) : -1;
      var next = all[e.key === 'j' ? Math.min(i + 1, all.length - 1) : Math.max(i - 1, 0)];
      if (next) next.focus();
      return;
    }
    if (!row) return;
    var clickIn = function (sel) { var el = row.querySelector(sel); if (el) { e.preventDefault(); el.click(); } };
    if (e.key === 'x' || e.key === ' ') clickIn('.task-check button');
    else if (e.key === 's' && row.dataset.canSub) { e.preventDefault(); addSub(row); }
    else if (e.key === 'n') clickIn('[data-carry="next"]');
    else if (e.key === 'w') clickIn('[data-carry="this"]');
    else if (e.key === 'p') clickIn('[data-status-btn="parked"]');
    else if (e.key === 'Enter') clickIn('a[href$="/edit"]');
    else if (e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) { e.preventDefault(); moveRow(row, e.key === 'ArrowUp' ? -1 : 1); }
    else if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') { var c = childrenOf(row); if (c) { c.dataset.collapsed = e.key === 'ArrowLeft' ? '1' : ''; applyCollapsed(); } }
  });

  // ---- after any swap: re-apply collapse state, put focus back where it was.
  document.addEventListener('htmx:afterSwap', function (e) {
    if (!e.detail || !e.detail.target || e.detail.target.id !== 'task-board') return;
    applyCollapsed();
    if (refocus) { var r = document.querySelector('#task-board .task-row[data-id="' + refocus + '"]'); refocus = null; if (r) addSub(r); }
  });
  applyCollapsed();
})();
