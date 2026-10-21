// 产出页 as an issue (docs/design/ARCHIVE-ISSUES.md §B): the title renames in place (click or Enter on it,
// Enter saves, Esc cancels, leaving the field saves when it changed; the field is a one-row textarea set in
// the heading's type, so a long title wraps exactly as the heading did), ⌘/Ctrl+Enter sends a 记录 line,
// and after the 待办 / 记录 block comes back from HTMX the input that was used keeps the focus so several
// lines can be typed in a row. Without this script the rename form sits in the 更多 menu and every form
// still posts on its own.
(function () {
  var h1 = document.querySelector('[data-rename]');
  var form = document.querySelector('.issue-title-form');
  function openRename() {
    if (!h1 || !form) return;
    var input = form.querySelector('[name=title]');
    h1.hidden = true; form.hidden = false; input.focus(); input.select();
    var original = input.value, done = false;
    function close(save) {
      if (done) return; done = true;
      if (save && input.value.trim() && input.value !== original) { form.requestSubmit ? form.requestSubmit() : form.submit(); return; }
      input.value = original; form.hidden = true; h1.hidden = false; h1.focus();
    }
    input.onkeydown = function (e) { if (e.key === 'Enter') { e.preventDefault(); close(true); } if (e.key === 'Escape') { e.preventDefault(); close(false); } };
    input.oninput = function () { if (/[\r\n]/.test(input.value)) input.value = input.value.replace(/\s*[\r\n]+\s*/g, ' '); };
    input.onblur = function () { close(true); };
  }
  if (h1) {
    h1.addEventListener('click', openRename);
    h1.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); openRename(); } });
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-rename-open]');
    if (b) { var d = b.closest('details'); if (d) d.open = false; openRename(); }
  });

  // 描述: show the whole text, or open the document editor right here (its scripts load on first use, so a
  // page that is only read never pays for them). 完成 saves first and reloads only once the save landed.
  function loadAssets(list, done) {
    var i = 0;
    (function next() {
      if (i >= list.length) { done(); return; }
      var url = list[i++];
      if (/\.css(\?|$)/.test(url)) { var l = document.createElement('link'); l.rel = 'stylesheet'; l.href = url; document.head.appendChild(l); next(); return; }
      var s = document.createElement('script'); s.src = url; s.onload = next; s.onerror = next; document.body.appendChild(s);
    })();
  }
  document.addEventListener('click', function (e) {
    var more = e.target.closest && e.target.closest('[data-desc-more]');
    if (more) {
      var body = document.querySelector('[data-desc-body]');
      if (body) {
        if (!more.dataset.label) more.dataset.label = more.textContent;
        more.textContent = body.classList.toggle('open') ? 'Show less' : more.dataset.label;
      }
      return;
    }
    var edit = e.target.closest && e.target.closest('[data-edit-desc]');
    if (edit) {
      var form = document.getElementById('body-form'), view = document.querySelector('[data-desc-view]');
      if (!form || !form.hidden) return;
      form.hidden = false; if (view) view.hidden = true; edit.hidden = true;
      if (!window.rlEditor) loadAssets((form.dataset.assets || '').split(/\s+/).filter(Boolean), function () {});
      form.scrollIntoView({ block: 'start', behavior: 'smooth' });
      return;
    }
    var done = e.target.closest && e.target.closest('[data-desc-done]');
    if (done) {
      if (!window.rlEditor) { window.location.reload(); return; }
      done.disabled = true;
      window.rlEditor.save().then(function (ok) { if (ok) window.location.reload(); else done.disabled = false; });
      return;
    }
    var add = e.target.closest && e.target.closest('[data-add-sub]');
    if (add) { var input = document.querySelector('.issue-add input[name=title]'); if (input) input.focus(); }
  });

  // ⌘/Ctrl+Enter in the 评论 composer sends it.
  document.addEventListener('keydown', function (e) {
    var t = e.target;
    if (!(t && t.closest && t.closest('.tl-compose')) || e.key !== 'Enter' || !(e.metaKey || e.ctrlKey)) return;
    e.preventDefault();
    var f = t.closest('form'); if (f.requestSubmit) f.requestSubmit(); else f.submit();
  });

  // Keep the focus where Leo was typing once #issue-activity is swapped back.
  var again = null;
  document.addEventListener('submit', function (e) {
    var f = e.target;
    if (!f.closest || !f.closest('#issue-activity')) return;
    again = f.classList.contains('issue-add') ? '.issue-add input[name=title]' : (f.classList.contains('tl-compose') ? '.tl-compose textarea' : null);
  }, true);
  document.addEventListener('htmx:afterSwap', function (e) {
    if (!e.detail || !e.detail.target || e.detail.target.id !== 'issue-activity' || !again) return;
    var el = document.querySelector('#issue-activity ' + again); again = null;
    if (el) el.focus();
  });
})();
