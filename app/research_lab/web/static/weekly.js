// Weekly module: tabs, confirm dialogs, basket group toggles, artifact filters, slug autofill,
// unsaved-edit guard, and swapping 4xx fragments (htmx skips them by default).
(function () {
  document.body.addEventListener('htmx:beforeSwap', function (e) {
    var s = e.detail.xhr.status;
    if (s === 400 || s === 409) { e.detail.shouldSwap = true; e.detail.isError = false; }
  });

  document.querySelectorAll('[data-tabs]').forEach(function (bar) {
    var scope = bar.parentElement;
    bar.addEventListener('click', function (e) {
      var b = e.target.closest('[data-tab]'); if (!b) return;
      bar.querySelectorAll('[data-tab]').forEach(function (x) { x.classList.toggle('on', x === b); });
      scope.querySelectorAll('[data-pane]').forEach(function (p) { p.hidden = p.dataset.pane !== b.dataset.tab; });
    });
    var visible = scope.querySelector('[data-pane]:not([hidden])');
    if (visible) bar.querySelectorAll('[data-tab]').forEach(function (x) { x.classList.toggle('on', x.dataset.tab === visible.dataset.pane); });
  });

  var LEVELS = { 'seed': 1, 'developed': 2, 'paper-ready': 3 };
  document.addEventListener('submit', function (e) {
    var f = e.target;
    if (f.dataset.confirm && !window.confirm(f.dataset.confirm)) { e.preventDefault(); return; }
    if (f.hasAttribute('data-promote-guard')) {
      var sel = f.querySelector('select[name=level]');
      var confirmInput = f.querySelector('input[name=confirm]');
      if (sel && (LEVELS[sel.value] || 0) > (LEVELS[f.dataset.currentLevel] || 0)) {
        if (!window.confirm('提升到「' + sel.options[sel.selectedIndex].text + '」？成熟度由你决定，应用只检查 Claim 是否已写。')) { e.preventDefault(); return; }
        confirmInput.value = 'yes';
      }
    }
    if (f.hasAttribute('data-dirty-guard')) f.dataset.submitting = '1';
  });

  document.addEventListener('change', function (e) {
    var t = e.target;
    if (t.matches('[data-toggle-group]')) {
      t.closest('fieldset').querySelectorAll('input[name=excerpt_ids]').forEach(function (c) { c.checked = t.checked; });
    }
    if (t.matches('select[data-period-defaults]')) {
      var map = JSON.parse(t.dataset.periodDefaults), p = t.form.querySelector('input[name=period]');
      if (p && map[t.value]) p.value = map[t.value];
    }
  });

  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-copy]'); if (!b) return;
    if (navigator.clipboard) navigator.clipboard.writeText(b.dataset.copy).then(function () { b.textContent = 'Copied'; });
  });

  document.querySelectorAll('[data-slug-source]').forEach(function (title) {
    var slug = document.getElementById(title.dataset.slugSource), touched = false;
    if (!slug) return;
    slug.addEventListener('input', function () { touched = slug.value !== ''; });
    title.addEventListener('input', function () {
      if (touched) return;
      slug.value = title.value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60).replace(/-+$/, '');
    });
  });

  var dirty = false;
  document.querySelectorAll('form[data-dirty-guard]').forEach(function (f) {
    f.addEventListener('input', function () { dirty = true; });
  });
  window.addEventListener('beforeunload', function (e) {
    var submitting = document.querySelector('form[data-dirty-guard][data-submitting]');
    if (dirty && !submitting) { e.preventDefault(); e.returnValue = ''; }
  });

  // Artifacts list: the shared row partial has no data attributes, so facets arrive as JSON in row order.
  var list = document.getElementById('artifact-list'), facetsEl = document.getElementById('artifact-facets');
  var box = document.getElementById('filters'), count = document.getElementById('filter-count');
  if (list && facetsEl && box) {
    var facets = JSON.parse(facetsEl.textContent), items = list.querySelectorAll(':scope > li');
    items.forEach(function (li, i) { var f = facets[i] || {}; Object.keys(f).forEach(function (k) { li.dataset[k] = f[k]; }); });
    var active = {};
    var apply = function () {
      var shown = 0;
      items.forEach(function (li) {
        var ok = Object.keys(active).every(function (k) { return li.dataset[k] === active[k]; });
        li.hidden = !ok; if (ok) shown++;
      });
      box.querySelectorAll('button').forEach(function (b) { b.classList.toggle('on', active[b.dataset.key] === b.dataset.value); });
      count.textContent = Object.keys(active).length ? '显示 ' + shown + ' / ' + items.length : '';
    };
    box.addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      if (b.dataset.key === 'clear') active = {};
      else if (active[b.dataset.key] === b.dataset.value) delete active[b.dataset.key];
      else active[b.dataset.key] = b.dataset.value;
      apply();
    });
  }
})();
