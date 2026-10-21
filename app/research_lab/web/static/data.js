// 数据复现 page helpers: auto-submit the method/dataset picker, add rows to editable tables,
// confirm destructive forms, copy the run's code. Everything degrades to plain forms without JS.
(function () {
  document.querySelectorAll('form[data-autosubmit]').forEach(function (f) {
    f.addEventListener('change', function () { f.requestSubmit ? f.requestSubmit() : f.submit(); });
  });
  document.querySelectorAll('[data-add-row]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var table = btn.closest('form').querySelector('table[data-grow] tbody');
      var blank = table && table.querySelector('tr.ds-blank');
      if (!blank) return;
      var row = blank.cloneNode(true);
      row.querySelectorAll('input').forEach(function (i) { i.value = ''; });
      table.appendChild(row);
      var first = row.querySelector('input'); if (first) first.focus();
    });
  });
  document.querySelectorAll('form[data-confirm]').forEach(function (f) {
    f.addEventListener('submit', function (e) { if (!window.confirm(f.dataset.confirm)) e.preventDefault(); });
  });
  document.querySelectorAll('[data-copy]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var el = document.querySelector(btn.dataset.copy);
      if (!el || !navigator.clipboard) return;
      navigator.clipboard.writeText(el.textContent).then(function () {
        var old = btn.textContent; btn.textContent = 'Copied'; setTimeout(function () { btn.textContent = old; }, 1400);
      });
    });
  });
})();
