// Issue page: the title, the description and quick sub-issues save in place; other forms save and then
// refresh the workspace. The revision covers issues.yml, tasks.yml and projects.yml together, so every
// save changes it for every form on the page: requests run one at a time through a queue, and each takes
// the revision the previous one returned.
(function () {
  if (window.__rlIssues) return;
  window.__rlIssues = true;
  var queue = Promise.resolve(), waiting = 0;
  var IDLE_MS = 800;
  var CONFLICT = '这项内容已在其他窗口更新。当前草稿仍保留，请先复制草稿，再刷新合并。';

  function enqueue(job) {
    waiting++;
    var run = queue.then(function () { waiting--; return job(); });
    queue = run.catch(function () {});
    return run;
  }
  // Network failures and the guard's plain-text 403 (or a 500) become Chinese; JSON errors pass through.
  async function request(url, data, headers, fallback, keepalive) {
    var response;
    try {
      response = await fetch(url, {method: 'POST', body: data, keepalive: !!keepalive,
                                   headers: Object.assign({'HX-Request': 'true'}, headers || {})});
    } catch (_) { throw new Error('连不上应用，没有保存，请稍后重试。'); }
    if (response.ok) return response;
    var message = '';
    if ((response.headers.get('content-type') || '').indexOf('application/json') === 0) {
      try { message = (await response.json()).error || ''; } catch (_) {}
    }
    if (response.status === 403) message = '这次修改没有送达，请刷新页面后重试。';
    else if (response.status >= 500) message = '服务器出错，没有保存，请稍后重试。';
    throw new Error(message || fallback || '没有保存，请稍后重试。');
  }
  window.rlIssueRequest = request;

  // Textareas with data-autogrow, on any page (this file loads everywhere): the box grows with the text; rows
  // is the least height and data-autogrow="<px>" can raise it; a CSS max-height brings the scrollbar back.
  // Measuring collapses the box to height:auto for a moment. The parent holds its height meanwhile: otherwise
  // the page gets shorter, the browser clamps scrollY and the page jumps on every keystroke (WKWebView has
  // no scroll anchoring to hide it). ISSUES-LINEAR.md §6.3.
  function grow(el) {
    if (!el.getClientRects().length) return;  // hidden: measured again on focus or when its <details> opens
    var holder = el.parentElement, held = holder.style.minHeight, y = window.scrollY;
    holder.style.minHeight = holder.offsetHeight + 'px';
    el.style.height = 'auto';
    // Heights are border-box: add the borders, or the text sits 2px short and scrolls while typing.
    el.style.height = Math.max(parseInt(el.dataset.autogrow, 10) || 0, el.scrollHeight + el.offsetHeight - el.clientHeight) + 'px';
    el.style.overflowY = el.scrollHeight > el.clientHeight + 1 ? 'auto' : 'hidden';
    holder.style.minHeight = held;
    if (window.scrollY !== y) window.scrollTo(window.scrollX, y);
  }
  function growAll(root) { (root || document).querySelectorAll('textarea[data-autogrow]').forEach(grow); }
  window.rlGrow = grow;
  growAll();
  window.addEventListener('resize', function () { growAll(); });
  document.addEventListener('focusin', function (e) { if (e.target.matches && e.target.matches('textarea[data-autogrow]')) grow(e.target); });
  document.addEventListener('toggle', function (e) { if (e.target.open) growAll(e.target); }, true);
  document.addEventListener('htmx:afterSettle', function (e) { growAll(document.contains(e.target) ? e.target : document); });
  function key(form) { return form.getAttribute('action') + ':' + (form.elements.field ? form.elements.field.value : ''); }
  function twinIn(root, form) {
    return Array.from(root.querySelectorAll('[data-issue-form]')).find(function (f) { return key(f) === key(form); });
  }
  // A queued submit may outlive its form when a refresh replaced it; act on the current one.
  function live(form) { return document.contains(form) ? form : (twinIn(document, form) || form); }
  function clearBusy(form) { form.querySelectorAll('.is-busy').forEach(function (button) { button.classList.remove('is-busy'); }); }
  function clearError(form) { form.querySelectorAll('.form-error').forEach(function (p) { p.remove(); }); }
  function setStatus(form, state, text) {
    var status = form.querySelector('[data-save-status]');
    if (!status) return;
    clearTimeout(status.fade);
    if (state) status.dataset.state = state; else delete status.dataset.state;
    status.textContent = text;
    if (state === 'saved') status.fade = setTimeout(function () { setStatus(form, '', ''); }, 2000);
  }
  function showError(form, message) {
    clearError(form);
    if (form.querySelector('[data-save-status]')) { setStatus(form, 'error', message); return; }
    var p = document.createElement('p');
    p.className = 'form-error'; p.setAttribute('role', 'alert'); p.textContent = message;
    form.appendChild(p);
  }
  // A conflicted draft keeps its old revision on purpose, so saving it is still refused.
  function adoptRevision(revision) {
    document.querySelectorAll('#issue-workspace [data-issue-form]:not([data-conflicted]) input[name=revision]').forEach(function (input) { input.value = revision; });
  }
  function markConflict(form, revision) {
    form.dataset.conflicted = '1';
    form.elements.revision.value = revision;
    showError(form, CONFLICT);
  }

  // Inline fields: defaultValue is the value the server holds; while a save is on its way, the text it
  // carries (form.inflight) is the base instead, so "dirty" means neither saved nor being saved.
  // Browsers send textarea line breaks as CRLF and the server trims, hence the normalising.
  function control(form) { return form.elements.value || form.elements.title; }
  function base(form) { return form.inflight != null ? form.inflight : control(form).defaultValue; }
  function norm(text) { return text.replace(/\r\n?/g, '\n').trim(); }
  function dirty(form) { return norm(control(form).value) !== norm(base(form)); }
  // Autosave waits for a pause; never while an IME candidate is being chosen (the text is still pinyin).
  function schedule(form) {
    clearTimeout(form.idle);
    if (!form.composing) form.idle = setTimeout(function () { saveField(form); }, IDLE_MS);
  }
  function saveField(form) {
    if (form.queued) return form.queued;
    if (!dirty(form)) return Promise.resolve();
    // One queued save per field reads the latest text when it runs; typing during a save queues the next.
    var run = form.queued = enqueue(async function () {
      form.queued = null;
      if (!dirty(form)) return;
      var c = control(form), value = c.value;
      setStatus(form, 'saving', '保存中…');
      // keepalive lets a save started by leaving the page finish; browsers cap such bodies at 64 KB.
      form.sending = value.length < 20000;
      form.inflight = value;
      try {
        var response = await request(form.action, new FormData(form), {Accept: 'application/json'}, '', form.sending);
        var result = await response.json();
      } finally { form.sending = false; form.inflight = null; }
      c.defaultValue = result.value;
      if (c.value === value && document.activeElement !== c) { c.value = result.value; grow(c); }
      adoptRevision(result.revision);
      clearError(form);
      if (form.classList.contains('issue-title-form')) retitle(result.value);
      if (!dirty(form)) { setStatus(form, 'saved', '已保存'); delete form.dataset.edited; return; }
      // Changed while this save was out (even back to the old text): that is a new edit to save.
      setStatus(form, '', '');
      if (form.hasAttribute('data-autosave')) schedule(form);
      else if (document.activeElement !== c) saveField(form);
    });
    run.catch(function (e) { showError(form, e.message); });
    return run;
  }
  function retitle(title) {
    document.title = title + ' · Research Lab';
    var root = document.querySelector('[data-project-parent]');
    var crumb = root && document.querySelector('.topbar-crumbs a[href="/issues?project=' + root.dataset.projectParent + '"]');
    if (crumb) crumb.textContent = title;
  }

  function createSub(form, submitter) {
    var input = form.elements.title, title = input.value.trim();
    if (submitter) input.focus();
    form.pending = form.pending || new Set();
    if (!title || form.pending.has(title)) return;  // a second Enter before the reply must not add it twice
    form.pending.add(title);
    enqueue(async function () {
      var target = live(form), data = new FormData(target);
      data.set('title', title);
      var response = await request(target.action, data, {Accept: 'application/json'}, '没有添加，请稍后重试。');
      var result = await response.json();
      adoptRevision(result.revision);
      clearError(target);
      if (result.row) insertRow(target, result); else window.rlReloadIssueWorkspace();
      if (target.elements.title.value.trim() === title) { target.elements.title.value = ''; delete target.dataset.edited; }
      // The open composer stays open for the next one: title and description cleared, properties kept.
      var desc = target.elements.description;
      if (desc && desc.value === data.get('description')) { desc.value = ''; if (desc.rlEditor) desc.rlEditor.setMarkdown(''); grow(desc); }
    }).catch(function (e) { showError(live(form), e.message); })
      .finally(function () { form.pending.delete(title); clearBusy(live(form)); });
  }
  function insertRow(form, result) {
    var section = form.closest('.issue-subissues'), tree = section.querySelector('.issue-subtree');
    if (!tree) { tree = document.createElement('div'); tree.className = 'issue-subtree'; form.before(tree); }
    var t = document.createElement('template');
    t.innerHTML = result.row;
    var item = t.content.querySelector('.issue-tree-item');
    // Rows are ordered by due date, open first and undated last (data-sort); a new row goes after its equals.
    var after = Array.from(tree.children).find(function (el) { return el.dataset.sort > item.dataset.sort; });
    tree.insertBefore(item, after || null);
    section.querySelectorAll('[data-with-rows]').forEach(function (el) { el.hidden = false; });
    var progress = section.querySelector('[data-sub-progress]');
    if (!progress) return;
    progress.hidden = !result.total;
    progress.querySelector('.issue-progress-ring').style.setProperty('--progress', (result.total ? Math.round(result.done / result.total * 100) : 0) + '%');
    progress.querySelector('[data-progress-count]').textContent = result.done + '/' + result.total;
  }

  // Property pickers and comments: save, then re-render the workspace from the server.
  function slot(c) { return c.name + (c.type === 'checkbox' || c.type === 'radio' ? '=' + c.value : ''); }
  function fields(form) {
    var map = new Map();
    form.querySelectorAll('input:not([type=hidden]), textarea, select').forEach(function (c) { map.set(slot(c), c); });
    return map;
  }
  // 撤销 for a property menu (ISSUES-LINEAR.md §2.5): what the field held before this save, read from the
  // menu as the server rendered it (the selected option, the boxes ticked, the date shown).
  function undoOf(form, submitter) {
    var menu = form.closest('[data-prop]');
    if (!menu || !form.elements.field) return null;
    var field = form.elements.field.value, values, text = '';
    var boxes = form.querySelectorAll('input[type=checkbox][name=value]');
    if (field === 'due') values = [menu.dataset.current || ''];
    else if (boxes.length) values = Array.from(boxes).filter(function (b) { return b.defaultChecked; }).map(function (b) { return b.value; });
    else {
      var selected = menu.querySelector('.issue-menu-option.selected');
      values = [selected ? selected.value : ''];
      text = submitter ? submitter.textContent.trim() : '';
    }
    return {action: form.action, field: field, values: values,
            text: text ? menu.dataset.title + '改为「' + text + '」' : menu.dataset.title + '已更新'};
  }
  function revert(undo) {
    enqueue(async function () {
      var data = new FormData(), rev = document.querySelector('#issue-workspace [data-issue-form] input[name=revision]');
      data.set('field', undo.field);
      data.set('revision', rev ? rev.value : '');
      undo.values.forEach(function (v) { data.append('value', v); });
      await request(undo.action, data);
      var response = await fetch(location.href);
      if (!response.ok) throw new Error('已撤销，但页面刷新失败，请刷新核对。');
      refreshWorkspace(await response.text());
      if (window.rlToast) window.rlToast('已撤销');
    }).catch(function (e) { if (window.rlToast) window.rlToast(e.message); });
  }
  function undoToast(undo) {
    var stack = document.getElementById('toast-stack');
    if (!stack) { if (window.rlToast) window.rlToast(undo.text); return; }
    var el = document.createElement('div'), span = document.createElement('span'), button = document.createElement('button');
    el.className = 'toast toast-undo'; el.setAttribute('role', 'status');
    span.textContent = undo.text;
    button.type = 'button'; button.className = 'link-btn'; button.textContent = 'Undo';
    el.append(span, button);
    stack.appendChild(el);
    function leave() { el.classList.add('leaving'); setTimeout(function () { el.remove(); }, 300); }
    var timer = setTimeout(leave, 6000);
    button.addEventListener('click', function () { clearTimeout(timer); leave(); revert(undo); });
  }

  function submitForm(form, submitter) {
    var data = new FormData(form);
    if (submitter && submitter.name) data.append(submitter.name, submitter.value);
    var undo = undoOf(form, submitter);
    var submitted = form.dataset.edited, snapshot = new Map();
    fields(form).forEach(function (c, name) { snapshot.set(name, {value: c.value, checked: c.checked}); });
    enqueue(async function () {
      var target = live(form);
      data.set('revision', target.elements.revision.value);
      clearError(target);
      var response = await request(target.action, data);
      var next = new URL(response.url), here = new URL(location.href);
      if (next.pathname !== here.pathname || (here.pathname === '/issues' && next.searchParams.get('project') !== here.searchParams.get('project'))) {
        location.assign(next.pathname + next.search); return;
      }
      // Saves redirect to the plain issue URL; keep this page's query (for example the archive view).
      if (next.search !== here.search) response = await fetch(here.href);
      if (!response.ok) throw new Error('已保存，但页面刷新失败，请刷新核对。');
      var html = await response.text();
      target = live(target);
      if (target.dataset.edited === submitted) delete target.dataset.edited;
      else if (target.elements.field) {
        // Changed while saving: what was just saved is the draft's base now, not someone else's edit.
        fields(target).forEach(function (c, name) {
          var saved = snapshot.get(name);
          if (saved) { c.defaultValue = saved.value; c.defaultChecked = saved.checked; }
        });
      }
      refreshWorkspace(html);
      if (undo) undoToast(undo); else if (window.rlToast) window.rlToast('已保存');
    }).catch(function (e) { showError(live(form), e.message); })
      .finally(function () { clearBusy(form); clearBusy(live(form)); });
  }

  // Inline fields keep their nodes (focus, caret, IME composition, a pending autosave); they take the
  // server's hidden values and, unless they hold a draft the server changed underneath, its revision.
  function syncInline(form, twin) {
    var c = control(form), t = control(twin);
    twin.querySelectorAll('input[type=hidden]').forEach(function (h) {
      // The sub-issue composer's priority and due date are Leo's choice in progress, not the server's.
      if (form.hasAttribute('data-sub-composer') && ['priority', 'due', 'assignee'].indexOf(h.name) >= 0) return;
      if (h.name !== 'revision' && form.elements[h.name]) form.elements[h.name].value = h.value;
    });
    if (form.dataset.conflicted) return;
    if (dirty(form) && norm(t.defaultValue) !== norm(c.defaultValue)) { markConflict(form, form.elements.revision.value); return; }
    form.elements.revision.value = twin.elements.revision.value;
    if (!dirty(form) && c.value !== t.defaultValue) {
      c.value = t.defaultValue; grow(c);
      if (c.rlEditor) c.rlEditor.setMarkdown(c.value);   // another window changed the description
    }
    c.defaultValue = t.defaultValue;
  }
  // Other drafts (a comment, an unsaved label or relation choice) are copied into the fresh form,
  // matched by name and value: the relation list gains entries when sub-issues are added.
  function carryDraft(draft, twin) {
    var mine = fields(draft), remoteChanged = draft.dataset.conflicted === '1';
    fields(twin).forEach(function (c, name) {
      var d = mine.get(name);
      if (!d) { if (c.defaultChecked) remoteChanged = true; return; }
      if (c.defaultValue !== d.defaultValue || c.defaultChecked !== d.defaultChecked) remoteChanged = true;
      c.value = d.value; c.checked = d.checked;
    });
    // Do not give an old draft a fresh revision if another window changed its server value.
    if (remoteChanged) markConflict(twin, draft.elements.revision.value);
    twin.dataset.edited = draft.dataset.edited;
    var details = twin.closest('details'), was = draft.closest('details');
    if (details && was && was.open) details.open = true;
  }
  // Replace everything around the kept nodes without moving them.
  function morph(current, fresh, kept) {
    var pairs = [];
    var wanted = Array.from(fresh.childNodes).map(function (node) {
      var keep = kept.get(node);
      if (keep && !keep.matches('[data-inline-field], [data-issue-uploads]')) pairs.push([keep, node]);
      return keep || node;
    });
    Array.from(current.childNodes).forEach(function (node) { if (wanted.indexOf(node) < 0) node.remove(); });
    var cursor = current.firstChild;
    wanted.forEach(function (node) {
      if (node === cursor) cursor = cursor.nextSibling;
      else current.insertBefore(node, cursor);
    });
    pairs.forEach(function (pair) { morph(pair[0], pair[1], kept); });
  }
  function refreshWorkspace(html) {
    var doc = new DOMParser().parseFromString(html, 'text/html');
    var fresh = doc.getElementById('issue-workspace'), current = document.getElementById('issue-workspace');
    if (!fresh || !current) throw new Error('页面未能更新，请刷新核对。');
    current.querySelectorAll('[data-issue-form][data-edited]:not([data-inline-field])').forEach(function (draft) {
      var twin = twinIn(fresh, draft);
      if (twin) carryDraft(draft, twin);
    });
    var kept = new Map();
    var uploads = current.querySelector("[data-issue-uploads]"), freshUploads = fresh.querySelector("[data-issue-uploads]");
    if (uploads && freshUploads) {
      for (var u = uploads, v = freshUploads; u && v && u !== current; u = u.parentElement, v = v.parentElement) kept.set(v, u);
    }
    current.querySelectorAll('[data-inline-field]').forEach(function (form) {
      var twin = twinIn(fresh, form);
      if (!twin) return;
      syncInline(form, twin);
      for (var a = form, b = twin; a && b && a !== current; a = a.parentElement, b = b.parentElement) kept.set(b, a);
    });
    // Multi-choice menus (labels, relations) stay open while Leo ticks one box after another.
    var reopen = Array.from(current.querySelectorAll('details[data-stay-open][open]')).map(function (d) {
      return (d.closest('.issue-chips') ? '.issue-chips ' : '.issue-properties ') + 'details[data-prop="' + d.dataset.prop + '"]';
    });
    var y = window.scrollY;
    if (kept.size) { kept.set(fresh, current); morph(current, fresh, kept); } else current.replaceWith(fresh);
    reopen.forEach(function (selector) { var d = document.querySelector(selector); if (d) d.open = true; });
    ['.topbar-crumbs', 'nav[aria-label="研究方向"]', '#create-menu'].forEach(function (selector) {
      var old = document.querySelector(selector), next = doc.querySelector(selector);
      if (old && next) old.replaceWith(next);
    });
    growAll();
    mountEditors();
    relativeTimes();
    mountSplit();
    document.title = doc.title;
    window.scrollTo(0, y);
  }

  // Split with Theo (card 0052) in an output's sub-issue block: on its issue page (the sub-issue composer names the
  // output as parent) and on its 研究材料 page (the 子任务 form names it as source). The block is the fragment
  // /issues/<ref>/split serves; it is loaded here because both page templates are another card's (0127) for now.
  function mountSplit() {
    if (document.querySelector('[data-split-theo], [data-split-slot]')) return;
    var input = document.querySelector('#issue-workspace [data-sub-composer] input[name="parent"], #tasks form.issue-add input[name="source"]');
    if (!input || !/^ART-\d{4}-\d{4}$/.test(input.value)) return;
    var block = input.closest('section'), head = block && block.querySelector('header, .issue-block-head');
    if (!head) return;
    var slot = document.createElement('div'); slot.setAttribute('data-split-slot', '');
    head.insertAdjacentElement('afterend', slot);
    fetch('/issues/' + encodeURIComponent(input.value) + '/split', {headers: {'HX-Request': 'true'}})
      .then(function (r) { return r.ok ? r.text() : ''; })
      .then(function (html) {
        if (!html || !document.contains(slot)) { slot.remove(); return; }
        slot.outerHTML = html;
        var box = document.querySelector('[data-split-theo]');
        if (box && window.htmx) window.htmx.process(box);
      })
      .catch(function () { slot.remove(); });
  }
  mountSplit();
  document.addEventListener('htmx:afterSettle', function () { mountSplit(); });

  // The description in the rich editor (card 0109; static/issue-editor.js, loaded by pages/issue.html). The editor
  // keeps the hidden textarea in sync and reports each change as an input event, so the save path above (queue,
  // revision, conflicts, the 0.8 s idle save, pagehide) stays the textarea's; leaving the editor saves at once.
  // Old descriptions are tidied on their first edit (`* ` becomes `- `, runs of spaces merge): say so once.
  function mountDescription() {
    if (!window.RLIssueEditor) return;
    document.querySelectorAll('#issue-workspace .issue-description-form textarea[data-rich]').forEach(function (ta) {
      if (ta.rlEditor) return;
      var form = ta.form, view = form.querySelector('[data-desc-view]');
      var tidy = false;
      var ed = window.RLIssueEditor.mount(ta, {
        placeholder: ta.getAttribute('placeholder'),
        saveDelay: 864e5,   // the idle save is schedule()'s, driven by the input events below
        onChange: function () {
          ta.dispatchEvent(new Event('input', {bubbles: true}));
          if (tidy) { tidy = false; tidyNote(form); }
        },
        onSave: function () { if (dirty(form)) { clearTimeout(form.idle); saveField(form); } }
      });
      ta.rlEditor = ed;
      if (view) view.remove();
      tidy = !!norm(ta.defaultValue) && norm(ed.getMarkdown()) !== norm(ta.defaultValue);
    });
  }
  // Comment composers (card 0097): the issue page's and an output's use the same editor. A comment is sent with
  // ⌘↩ or the button, never on its own, so the editor only reports changes (data-edited keeps a draft through a
  // workspace refresh). An output's page does not load the editor; data-editor-assets names the two files to load.
  function mountComments() {
    document.querySelectorAll('.issue-comment-form textarea[data-rich], .tl-compose textarea[data-rich]').forEach(function (ta) {
      if (ta.rlEditor || !window.RLIssueEditor) return;
      ta.rlEditor = window.RLIssueEditor.mount(ta, {
        placeholder: ta.getAttribute('placeholder'),
        saveDelay: 864e5,
        onChange: function () { ta.dispatchEvent(new Event('input', {bubbles: true})); }
      });
    });
  }
  var loadingEditor = null;
  function loadEditor() {
    if (window.RLIssueEditor) return Promise.resolve();
    var holder = document.querySelector('textarea[data-rich][data-editor-assets]');
    if (!holder) return Promise.resolve();
    if (!loadingEditor) {
      loadingEditor = holder.dataset.editorAssets.split(' ').reduce(function (chain, src) {
        return chain.then(function () {
          return new Promise(function (resolve, reject) {
            var s = document.createElement('script');
            s.src = src; s.onload = resolve; s.onerror = reject;
            document.head.appendChild(s);
          });
        });
      }, Promise.resolve()).catch(function () { loadingEditor = null; });
    }
    return loadingEditor;
  }
  function mountEditors() {
    loadEditor().then(function () { mountDescription(); mountComments(); });
  }
  document.addEventListener('htmx:afterSettle', function (e) {
    mountEditors();
    relativeTimes();
    // An output's composer swapped back after sending: keep the caret in the (new) editor.
    var elt = e.detail && e.detail.requestConfig && e.detail.requestConfig.elt;
    if (elt && elt.classList && elt.classList.contains('tl-compose')) {
      loadEditor().then(function () {
        var ta = document.querySelector('.tl-compose textarea[data-rich]');
        if (ta && ta.rlEditor) ta.rlEditor.focus();
      });
    }
  });

  // Relative times in the activity (card 0097): 刚刚, N 分钟前, N 小时前 today, 昨天 HH:MM, M月D日; the full time
  // stays the tooltip. Worked out here so a page left open keeps counting (refreshed every minute).
  function relativeTime(iso) {
    var t = new Date(iso);
    if (isNaN(t)) return '';
    var now = new Date(), s = (now - t) / 1000;
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    var today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    if (s < 60) return '刚刚';
    if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
    if (t >= today) return Math.floor(s / 3600) + ' 小时前';
    if (t >= new Date(today - 864e5)) return '昨天 ' + pad(t.getHours()) + ':' + pad(t.getMinutes());
    if (t.getFullYear() === now.getFullYear()) return (t.getMonth() + 1) + '月' + t.getDate() + '日';
    return t.getFullYear() + '年' + (t.getMonth() + 1) + '月' + t.getDate() + '日';
  }
  function relativeTimes() {
    document.querySelectorAll('time[data-rel]').forEach(function (el) {
      var text = relativeTime(el.getAttribute('datetime'));
      if (text) el.textContent = text;
    });
  }
  window.rlRelativeTime = relativeTime;
  relativeTimes();
  setInterval(relativeTimes, 60000);

  // Editing and deleting Leo's own comments (card 0097). An issue comment's request queues behind the page's
  // other saves and carries the latest revision; an output's has no revision. Afterwards the thread is redrawn.
  function threadRedraw() {
    if (document.getElementById('issue-workspace') && window.rlReloadIssueWorkspace) return window.rlReloadIssueWorkspace();
    return fetch(location.href).then(function (r) { return r.text(); }).then(function (html) {
      var fresh = new DOMParser().parseFromString(html, 'text/html').getElementById('issue-activity');
      var current = document.getElementById('issue-activity');
      if (fresh && current) {
        current.replaceWith(fresh);
        if (window.htmx) window.htmx.process(fresh);
        mountEditors();
        relativeTimes();
      }
    });
  }
  function commentRequest(url, fields) {
    var data = new FormData();
    Object.keys(fields).forEach(function (k) { data.set(k, fields[k]); });
    var issue = url.indexOf('/issues/') === 0;
    var send = function () {
      if (issue) {
        var rev = document.querySelector('#issue-workspace [data-issue-form] input[name=revision]');
        data.set('revision', rev ? rev.value : '');
      }
      return request(url, data, {Accept: 'application/json'}, '没有保存，请稍后重试。').then(function (response) {
        if (issue) return response.json().then(function (r) { if (r.revision) adoptRevision(r.revision); });
      });
    };
    return (issue ? enqueue(send) : send()).then(threadRedraw);
  }
  function closeEdit(li) {
    var box = li.querySelector('.activity-edit');
    if (box) { var ta = box.querySelector('textarea'); if (ta && ta.rlEditor) ta.rlEditor.destroy(); box.remove(); }
    var body = li.querySelector('[data-comment-body]');
    if (body) body.hidden = false;
  }
  document.addEventListener('click', function (e) {
    var edit = e.target.closest && e.target.closest('[data-comment-edit]');
    if (edit) {
      var li = edit.closest('.activity-comment');
      if (li.querySelector('.activity-edit')) return;
      loadEditor().then(function () {
        var box = document.createElement('div');
        box.className = 'activity-edit';
        var ta = document.createElement('textarea');
        ta.value = li.querySelector('[data-comment-source]').value;
        ta.setAttribute('aria-label', '修改评论');
        var bar = document.createElement('div');
        bar.className = 'activity-edit-bar';
        bar.innerHTML = '<button type="button" class="btn ghost small" data-edit-cancel>Cancel</button><button type="button" class="btn small" data-edit-save>Save</button>';
        box.append(ta, bar);
        li.querySelector('[data-comment-body]').hidden = true;
        li.querySelector('.activity-card').appendChild(box);
        if (window.RLIssueEditor) {
          ta.rlEditor = window.RLIssueEditor.mount(ta, { saveDelay: 864e5, onSave: function () {} });
          ta.rlEditor.editor.commands.focus('end');
        }
        var save = function () {
          var text = ta.rlEditor ? ta.rlEditor.getMarkdown() : ta.value;
          if (!text.trim()) { if (window.rlToast) window.rlToast('评论不能为空；不要了可以删除。'); return; }
          bar.querySelector('[data-edit-save]').classList.add('is-busy');
          commentRequest(edit.dataset.commentEdit, {text: text})
            .catch(function (err) { bar.querySelector('[data-edit-save]').classList.remove('is-busy'); if (window.rlToast) window.rlToast(err.message); });
        };
        bar.querySelector('[data-edit-save]').addEventListener('click', save);
        bar.querySelector('[data-edit-cancel]').addEventListener('click', function () { closeEdit(li); });
        box.addEventListener('keydown', function (ev) {
          if ((ev.metaKey || ev.ctrlKey) && ev.key === 'Enter') { ev.preventDefault(); ev.stopPropagation(); save(); }
          if (ev.key === 'Escape') { ev.preventDefault(); closeEdit(li); }
        }, true);
      });
      return;
    }
    var del = e.target.closest && e.target.closest('[data-comment-delete]');
    if (del) {
      // Two steps instead of a dialog (the Mac app has none): the first click arms, the second deletes.
      if (!del.dataset.armed) {
        del.dataset.armed = '1'; del.textContent = 'Confirm';
        setTimeout(function () { delete del.dataset.armed; del.textContent = 'Delete'; }, 4000);
        return;
      }
      del.classList.add('is-busy');
      commentRequest(del.dataset.commentDelete, {})
        .catch(function (err) { del.classList.remove('is-busy'); if (window.rlToast) window.rlToast(err.message); });
    }
  });

  // Keep files in this page until the user chooses where a new PDF belongs.
  function fileDrag(e) { return e.dataTransfer && Array.from(e.dataTransfer.types).includes('Files'); }
  function uploadRow(file, url) {
    var list = document.querySelector('[data-issue-uploads]');
    if (!list) return;
    list.hidden = false;
    var row = document.createElement('li'), name = document.createElement('span'), note = document.createElement('span');
    row.className = 'issue-upload-row'; row.dataset.pending = '1';
    name.className = 'issue-upload-name'; name.textContent = file.name;
    note.className = 'issue-upload-note'; note.textContent = '等待上传';
    row.append(name, note); list.append(row);
    function button(label, action) {
      var b = document.createElement('button'); b.type = 'button'; b.className = 'btn secondary small';
      b.textContent = label; b.addEventListener('click', action); row.append(b);
    }
    function dismiss() { file = null; row.remove(); list.hidden = !list.children.length; }
    function send(mode, paper) {
      row.querySelectorAll('button').forEach(function (b) { b.remove(); });
      delete row.dataset.error; row.dataset.pending = '1'; note.textContent = '等待上传';
      flush();
      enqueue(async function () {
        var data = new FormData(), progress = document.createElement('progress');
        data.set('file', file); data.set('mode', mode); data.set('revision', pageRevision());
        if (paper) data.set('paper_id', paper);
        progress.max = 100; progress.value = 0; progress.setAttribute('aria-label', file.name + ' 上传进度'); row.append(progress);
        note.textContent = '上传中';
        var result;
        try {
          result = await new Promise(function (resolve, reject) {
            var xhr = new XMLHttpRequest(); xhr.open('POST', url);
            xhr.setRequestHeader('HX-Request', 'true'); xhr.setRequestHeader('Accept', 'application/json');
            xhr.upload.onprogress = function (e) {
              if (e.lengthComputable) progress.value = e.loaded / e.total * 100;
              if (e.loaded === e.total) note.textContent = '正在处理';
            };
            xhr.onload = function () {
              var out; try { out = JSON.parse(xhr.responseText); } catch (_) {}
              if (xhr.status >= 200 && xhr.status < 300 && out) resolve(out);
              else reject(new Error(out?.error || '上传未完成，请刷新核对。'));
            };
            xhr.onerror = function () { reject(new Error('连不上应用，请稍后重试。')); };
            xhr.send(data);
          });
        } finally { progress.remove(); }
        note.textContent = result.note;
        if (result.state === 'choice') {
          if (result.importable) button('Import to library', function () { send('import'); });
          button('Attach only', function () { send('attach'); });
        } else if (result.state === 'duplicate') {
          button('Link paper', function () { send('link', result.paper_id); });
          button(result.import_action === 'separate' ? 'Keep both' : 'Import to library', function () { send(result.import_action); });
          button('Attach only', function () { send('attach'); });
        } else {
          adoptRevision(result.revision);
          var fragment = new DOMParser().parseFromString(result.materials, 'text/html').querySelector('[data-issue-materials]');
          var old = document.querySelector('[data-issue-materials]');
          if (old && fragment) old.replaceWith(fragment);
          delete row.dataset.pending; file = null;
        }
        button('Dismiss', dismiss);
      }).catch(function (e) {
        row.dataset.error = '1'; note.textContent = e.message;
        button('Dismiss', dismiss);
      });
    }
    send('auto');
  }
  ['dragenter', 'dragover', 'drop'].forEach(function (type) {
    window.addEventListener(type, function (e) {
      var zone = e.target.closest && e.target.closest('[data-issue-drop]');
      if (!zone || !fileDrag(e)) return;
      e.preventDefault(); e.stopPropagation(); document.body.classList.remove('dragging');
      zone.classList.add('is-over');
      if (type === 'drop') {
        zone.classList.remove('is-over');
        Array.from(e.dataTransfer.files).forEach(function (file) { uploadRow(file, zone.dataset.issueDrop); });
      }
    }, true);
  });
  // Run after document handlers so in-place actions can cancel navigation first.
  var confirmedUploadLeave = false;
  window.addEventListener('click', function (e) {
    var link = e.target.closest && e.target.closest('a[href]');
    if (!link || e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || link.target === '_blank' || link.hasAttribute('download')) return;
    var next = new URL(link.href, location.href);
    if (next.pathname === location.pathname && next.search === location.search && next.hash) return;
    if (!document.querySelector('[data-issue-uploads] [data-pending]')) return;
    if (!window.confirm('还有文件未处理，确定离开？')) { e.preventDefault(); e.stopImmediatePropagation(); }
    else { confirmedUploadLeave = true; setTimeout(function () { confirmedUploadLeave = false; }, 0); }
  });
  window.addEventListener('dragleave', function (e) {
    var zone = e.target.closest && e.target.closest('[data-issue-drop]');
    if (zone && !zone.contains(e.relatedTarget)) zone.classList.remove('is-over');
  }, true);

  // Attachments and linked papers / datasets under the description (card 0099). The paperclip opens the system file
  // chooser (a click on a page button counts as a user gesture, also in the Mac app). A PDF then takes the description
  // drop's path (card 0178): a row in the upload queue that links a paper already in the library, or offers Import to
  // library / Attach only; any other file submits the form like any property menu (it lands in private/). The
  // book-plus opens a search picker: type to search, ↑↓ to move, ↵ to link or, on an item already linked, unlink.
  document.addEventListener('click', function (e) {
    var attach = e.target.closest && e.target.closest('[data-attach]');
    if (attach) { var input = attach.form.querySelector('[data-attach-input]'); if (input) input.click(); return; }
    var open = e.target.closest && e.target.closest('[data-open-attachment]');
    if (open) {
      var data = new FormData();
      data.set('rel', open.dataset.rel);
      request(open.dataset.openAttachment, data, {Accept: 'application/json'}, '没能打开这个文件。')
        .then(function () { if (window.rlToast) window.rlToast('已用默认程序打开'); })
        .catch(function (err) { if (window.rlToast) window.rlToast(err.message); });
    }
  });
  document.addEventListener('change', function (e) {
    var input = e.target;
    if (!input.matches || !input.matches('[data-attach-input]') || !input.files.length) return;
    var file = input.files[0], drop = input.form.dataset.libraryDrop;
    // The server tells a PDF by its name's suffix, as here.
    if (drop && /\.pdf$/i.test(file.name)) { uploadRow(file, drop); input.value = ''; return; }
    input.form.requestSubmit();
  });
  // The editor's slash menu (card 0118) asks for these two through rl:editor-command; they are the same entries.
  document.addEventListener('rl:editor-command', function (e) {
    var root = document.getElementById('issue-workspace');
    if (!root) return;
    if (e.detail.command === 'attach') {
      var button = root.querySelector('[data-attach]');
      if (button) button.click();
    } else if (e.detail.command === 'link-material') {
      var picker = root.querySelector('details[data-materials]');
      if (picker) { picker.open = true; picker.scrollIntoView({block: 'nearest'}); }
    }
  });
  var pickTimer = null;
  function pickResults(input) {
    var box = input.form.querySelector('[data-materials-results]');
    fetch(input.dataset.url + '?q=' + encodeURIComponent(input.value.trim())).then(function (r) { return r.text(); }).then(function (html) {
      box.innerHTML = html;
      var first = box.querySelector('.issue-pick');
      if (first) first.classList.add('is-active');
    }).catch(function () { box.innerHTML = '<p class="issue-pick-empty">没能搜索，请稍后再试。</p>'; });
  }
  document.addEventListener('toggle', function (e) {
    var d = e.target;
    if (!d.matches || !d.matches('details[data-materials]') || !d.open) return;
    var input = d.querySelector('[data-materials-search]');
    pickResults(input);
    setTimeout(function () { input.focus(); }, 0);
  }, true);
  document.addEventListener('input', function (e) {
    if (!e.target.matches || !e.target.matches('[data-materials-search]')) return;
    clearTimeout(pickTimer);
    var input = e.target;
    pickTimer = setTimeout(function () { pickResults(input); }, 150);
  });
  document.addEventListener('keydown', function (e) {
    if (!e.target.matches || !e.target.matches('[data-materials-search]') || e.isComposing) return;
    var picks = Array.from(e.target.form.querySelectorAll('.issue-pick')), cur = picks.findIndex(function (b) { return b.classList.contains('is-active'); });
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!picks.length) return;
      if (cur >= 0) picks[cur].classList.remove('is-active');
      var next = picks[(cur + (e.key === 'ArrowDown' ? 1 : -1) + picks.length) % picks.length];
      next.classList.add('is-active'); next.scrollIntoView({block: 'nearest'});
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (cur >= 0) e.target.form.requestSubmit(picks[cur]);
    }
  });

  function tidyNote(form) {
    var note = document.createElement('p');
    note.className = 'issue-tidy-note';
    note.setAttribute('role', 'status');
    note.textContent = '已按新格式整理';
    form.insertBefore(note, form.querySelector('.rl-editor'));
    setTimeout(function () { note.classList.add('leaving'); setTimeout(function () { note.remove(); }, 400); }, 3200);
  }
  mountEditors();
  // The composer reloads the workspace after creating; it waits behind pending saves.
  window.rlReloadIssueWorkspace = function () {
    return enqueue(async function () {
      var response = await fetch(location.href);
      if (!response.ok) throw new Error('页面未能更新，请刷新核对。');
      refreshWorkspace(await response.text());
    }).catch(function (e) { if (window.rlToast) window.rlToast(e.message); });
  };

  document.addEventListener('input', function (e) {
    var form = e.target.closest('[data-issue-form]');
    if (!form) return;
    form.dataset.edited = String(Number(form.dataset.edited || 0) + 1);
    if (form.classList.contains('issue-title-form') && /[\r\n]/.test(e.target.value)) e.target.value = e.target.value.replace(/\s*[\r\n]+\s*/g, ' ');
    if (form.hasAttribute('data-autosave')) {
      var status = form.querySelector('[data-save-status]');
      if (status && status.dataset.state === 'saved') setStatus(form, '', '');
      schedule(form);
    }
  });
  // After the listener above, so a title that just lost a pasted line break is measured without it.
  document.addEventListener('input', function (e) { if (e.target.matches && e.target.matches('textarea[data-autogrow]')) grow(e.target); });
  // Property menus save on the change itself (a date picked, a box ticked); there is no 保存 button.
  document.addEventListener('change', function (e) {
    var form = e.target.closest && e.target.closest('form[data-instant]');
    if (form && e.target.name === 'value' && (e.target.type !== 'date' || e.target.value)) form.requestSubmit();
  });
  // The filter at the top of a menu: options whose words do not contain the text hide; Enter takes the first.
  function options(panel) { return Array.from(panel.querySelectorAll('[data-filter-text]')); }
  document.addEventListener('input', function (e) {
    if (!e.target.matches || !e.target.matches('[data-menu-filter]')) return;
    var q = e.target.value.trim().toLowerCase();
    options(e.target.closest('.issue-popover-panel')).forEach(function (o) { o.hidden = !!q && o.dataset.filterText.toLowerCase().indexOf(q) < 0; });
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' || !e.target.matches || !e.target.matches('[data-menu-filter]') || e.isComposing) return;
    e.preventDefault();
    var first = options(e.target.closest('.issue-popover-panel')).find(function (o) { return !o.hidden; });
    if (first) first.click();
  });
  var finePointer = window.matchMedia && window.matchMedia('(hover: hover) and (pointer: fine)').matches;
  // A menu starts clean: what was typed into it is cleared when it closes (clearing on open would race
  // with keys typed right after a shortcut opened it).
  document.addEventListener('toggle', function (e) {
    var menu = e.target;
    if (!menu.matches || !menu.matches('details.issue-popover')) return;
    var filter = menu.querySelector('[data-menu-filter], [data-due-text]');
    if (menu.open) {
      if (filter && finePointer && document.activeElement !== filter) filter.focus({preventScroll: true});   // no keyboard over a phone's sheet
      return;
    }
    if (filter) { filter.value = ''; options(menu).forEach(function (o) { o.hidden = false; }); }
    var reading = menu.querySelector('[data-due-reading]'), cal = menu.querySelector('[data-due-calendar]');
    if (reading) { reading.textContent = ''; delete reading.dataset.date; }
    if (cal) cal.hidden = true;
  }, true);
  // S, P, A, L, ⇧D open 状态, 优先级, 分配人, 标签, 截止 (the keys the menus show); the visible copy, column or chips.
  document.addEventListener('keydown', function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey || e.isComposing || e.defaultPrevented || typeof e.key !== 'string') return;
    if (!document.getElementById('issue-workspace') || document.querySelector('dialog[open]')) return;
    if (e.target.closest && e.target.closest('input, textarea, select, [contenteditable]')) return;
    var key = e.key.toUpperCase();
    if (key === 'D' && e.shiftKey) key = '⇧D';
    if (['S', 'P', 'A', 'L', '⇧D'].indexOf(key) < 0) return;
    var menu = Array.from(document.querySelectorAll('#issue-workspace details[data-key="' + key + '"]')).find(function (d) { return d.offsetParent !== null; });
    if (!menu) return;
    e.preventDefault();
    document.querySelectorAll('.issue-popover[open]').forEach(function (m) { if (m !== menu) m.open = false; });
    menu.open = true;
    var field = menu.querySelector('[data-menu-filter], [data-due-text]');
    if (field) field.focus({preventScroll: true});
  });
  // Menus by keyboard (card 0114, ISSUES-LINEAR.md §2.7): ↑↓ move a highlight over the visible options and
  // Enter takes it; Esc closes (above). Focus stays in the filter or the due text, so typing goes on
  // filtering. Capture phase, ahead of the filter's and the due text's own Enter, which apply only while
  // nothing is highlighted: typing a date clears the highlight, so Enter saves what was typed.
  var MENU_ITEM = '.issue-menu-option, .issue-check-option';   // single choices, and the ticks of 标签 and 关联
  function keyMenu(target) {
    var menu = target.closest && target.closest('details.issue-popover[open]');
    if (!menu && target === document.body) menu = document.querySelector('#issue-workspace details.issue-popover[open]');
    return menu && !menu.matches('[data-materials]') ? menu : null;   // the materials search has its own arrows
  }
  function menuItems(menu) {
    return Array.from(menu.querySelectorAll(MENU_ITEM)).filter(function (o) { return !o.hidden && !o.disabled && o.offsetParent !== null; });
  }
  function highlight(menu, option) {
    menu.querySelectorAll('.issue-menu-option.is-active, .issue-check-option.is-active').forEach(function (o) { o.classList.remove('is-active'); });
    if (option) { option.classList.add('is-active'); option.scrollIntoView({block: 'nearest'}); }
  }
  document.addEventListener('keydown', function (e) {
    if (['ArrowDown', 'ArrowUp', 'Enter'].indexOf(e.key) < 0 || e.isComposing || e.keyCode === 229 || e.metaKey || e.ctrlKey || e.altKey) return;
    var menu = keyMenu(e.target);
    if (!menu) return;
    var items = menuItems(menu), cur = items.findIndex(function (o) { return o.classList.contains('is-active'); });
    if (e.key === 'Enter') {
      if (cur < 0) return;
      e.preventDefault(); e.stopPropagation();
      // The current value again saves nothing, so it only closes the menu.
      if (items[cur].classList.contains('selected') && items[cur].type === 'submit') { menu.open = false; return; }
      items[cur].click();
      return;
    }
    if (!items.length) return;
    e.preventDefault(); e.stopPropagation();
    var step = e.key === 'ArrowDown' ? 1 : -1;
    highlight(menu, items[cur < 0 ? (step > 0 ? 0 : items.length - 1) : (cur + step + items.length) % items.length]);
  }, true);
  // The pointer and the keys share one highlight; a filter puts it on the first match, a typed date clears it.
  document.addEventListener('mouseover', function (e) {
    var option = e.target.closest && e.target.closest(MENU_ITEM);
    if (option && !option.closest('.issue-popover[open]')) option = null;
    if (option && !option.classList.contains('is-active')) highlight(option.closest('details'), option);
  });
  document.addEventListener('input', function (e) {
    var menu = e.target.closest && e.target.closest('details.issue-popover[open]');
    if (!menu || !e.target.matches('[data-menu-filter], [data-due-text]')) return;
    highlight(menu, e.target.matches('[data-menu-filter]') && e.target.value.trim() ? menuItems(menu)[0] : null);
  });
  document.addEventListener('toggle', function (e) {
    if (e.target.matches && e.target.matches('details.issue-popover') && !e.target.open) highlight(e.target, null);
  }, true);
  // 快捷键速查: ? outside text fields, wherever the sheet is on the page (研究档案 and issue pages).
  document.addEventListener('keydown', function (e) {
    if (e.key !== '?' || e.metaKey || e.ctrlKey || e.altKey || e.isComposing || e.defaultPrevented) return;
    if (e.target.closest && e.target.closest('input, textarea, select, [contenteditable]')) return;
    var sheet = document.querySelector('dialog[data-issue-shortcuts]');
    if (!sheet || document.querySelector('dialog[open]')) return;
    e.preventDefault();
    document.querySelectorAll('.issue-popover[open]').forEach(function (m) { m.open = false; });
    sheet.showModal();
  });
  document.addEventListener('click', function (e) {
    var sheet = e.target.closest && e.target.closest('dialog[data-issue-shortcuts]');
    // A click on the backdrop lands on the dialog itself; its content sits in a padded inner box.
    if (sheet && (e.target === sheet || e.target.closest('[data-shortcuts-close]'))) sheet.close();
  });
  // The due menu (ISSUES-LINEAR.md §4): what Leo types is read by files/dates.py on the server and shown
  // as 「→ 10月20日 周二」; Enter saves that date, nothing when it cannot be read. 自定义… opens a month.
  var dueTimer = null, dueAsked = 0;
  function saveDue(form, iso) {
    var button = form.querySelector('[data-due-submit]');
    button.value = iso;
    form.requestSubmit(button);
  }
  document.addEventListener('input', function (e) {
    if (!e.target.matches || !e.target.matches('[data-due-text]')) return;
    var input = e.target, reading = input.form.querySelector('[data-due-reading]'), ask = ++dueAsked;
    clearTimeout(dueTimer);
    delete reading.dataset.date;
    if (!input.value.trim()) { reading.textContent = ''; return; }
    dueTimer = setTimeout(async function () {
      try {
        var r = await fetch('/issues/due?text=' + encodeURIComponent(input.value), {headers: {Accept: 'application/json'}});
        var got = await r.json();
        if (ask !== dueAsked) return;                       // an older answer arriving late
        if (got.date) { reading.dataset.date = got.date; reading.textContent = '→ ' + got.label + '，回车确认'; }
        else reading.textContent = '读不出这个日期，换个说法：明天、3天、下周三、10月20日';
      } catch (_) { reading.textContent = '连不上应用，稍后再试。'; }
    }, 120);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' || !e.target.matches || !e.target.matches('[data-due-text]') || e.isComposing) return;
    e.preventDefault();                                     // never the implicit submit (it would clear the date)
    var date = e.target.form.querySelector('[data-due-reading]').dataset.date;
    if (date) saveDue(e.target.form, date);
  });
  var CAL_DAYS = '一二三四五六日';
  function iso(d) { return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }
  function drawMonth(box, year, month) {
    // Monday first; weekend columns shaded, today outlined, the current due date filled.
    var first = new Date(year, month, 1), lead = (first.getDay() + 6) % 7, days = new Date(year, month + 1, 0).getDate();
    var today = iso(new Date()), chosen = box.dataset.value, cells = [];
    for (var i = 0; i < lead; i++) cells.push('<span></span>');
    for (var d = 1; d <= days; d++) {
      var day = iso(new Date(year, month, d)), col = (lead + d - 1) % 7;
      cells.push('<button type="button" class="cal-day' + (col > 4 ? ' is-weekend' : '') + (day === today ? ' is-today' : '') +
                 (day === chosen ? ' is-selected' : '') + '" data-cal-day="' + day + '">' + d + '</button>');
    }
    box.dataset.year = year; box.dataset.month = month;
    box.innerHTML = '<div class="cal-head"><button type="button" class="btn ghost icon small" data-cal-step="-1" aria-label="上个月">‹</button>' +
      '<span>' + year + '年' + (month + 1) + '月</span><button type="button" class="btn ghost icon small" data-cal-step="1" aria-label="下个月">›</button></div>' +
      '<div class="cal-grid">' + CAL_DAYS.split('').map(function (c, i) { return '<span class="cal-dow' + (i > 4 ? ' is-weekend' : '') + '">' + c + '</span>'; }).join('') +
      cells.join('') + '</div>';
  }
  document.addEventListener('click', function (e) {
    var custom = e.target.closest && e.target.closest('[data-due-custom]');
    if (custom) {
      var box = custom.form.querySelector('[data-due-calendar]'), start = box.dataset.value ? new Date(box.dataset.value + 'T00:00') : new Date();
      box.hidden = !box.hidden;
      custom.setAttribute('aria-expanded', String(!box.hidden));
      if (!box.hidden) drawMonth(box, start.getFullYear(), start.getMonth());
      return;
    }
    var step = e.target.closest && e.target.closest('[data-cal-step]');
    if (step) {
      var cal = step.closest('[data-due-calendar]'), m = Number(cal.dataset.month) + Number(step.dataset.calStep);
      drawMonth(cal, Number(cal.dataset.year) + Math.floor(m / 12), (m + 12) % 12);
      return;
    }
    var day = e.target.closest && e.target.closest('[data-cal-day]');
    if (day) saveDue(day.closest('form'), day.dataset.calDay);
  });
  document.addEventListener('click', function (e) {
    var button = e.target.closest && e.target.closest('[data-copy-link]');
    if (!button) return;
    // With a key the link names it (/issues/APUS-0002), so it still opens the issue after a renumbering.
    var link = button.dataset.link ? new URL(button.dataset.link, location.href).href : location.href;
    var done = function () { if (window.rlToast) window.rlToast(button.dataset.link ? '已复制带编号的链接' : '已复制链接'); };
    if (navigator.clipboard) navigator.clipboard.writeText(link).then(done, function () { window.prompt('复制这个链接', link); });
    else window.prompt('复制这个链接', link);
  });
  document.addEventListener('compositionstart', function (e) {
    var form = e.target.closest && e.target.closest('[data-inline-field]');
    if (form) { form.composing = true; clearTimeout(form.idle); }
  });
  document.addEventListener('compositionend', function (e) {
    var form = e.target.closest && e.target.closest('[data-inline-field]');
    if (!form) return;
    form.composing = false;
    if (form.hasAttribute('data-autosave')) schedule(form);
  });
  document.addEventListener('focusout', function (e) {
    var form = e.target.closest('[data-inline-field]:not([data-quick-sub])');
    if (!form || e.target !== control(form)) return;
    clearTimeout(form.idle);
    // An emptied title goes back to the saved one rather than becoming an error.
    if (form.classList.contains('issue-title-form') && !e.target.value.trim()) { e.target.value = e.target.defaultValue; grow(e.target); return; }
    saveField(form);
  });
  document.addEventListener('submit', function (event) {
    var form = event.target.closest('[data-issue-form]');
    if (!form) return;
    event.preventDefault();
    if (form.hasAttribute('data-quick-sub')) createSub(form, event.submitter);
    else if (form.hasAttribute('data-inline-field')) { clearTimeout(form.idle); saveField(form); }
    else submitForm(form, event.submitter);
  });
  // The Mac app (pywebview) never shows a beforeunload prompt and closing its window skips beforeunload,
  // so leaving or hiding the page sends every unsent edit at once. With nothing else on its way the
  // keepalive request starts before the page goes; behind another save it waits its turn and can be lost
  // if the window closes first.
  function flush() {
    document.querySelectorAll('#issue-workspace [data-inline-field]:not([data-quick-sub])').forEach(function (form) {
      clearTimeout(form.idle);
      if (form.classList.contains('issue-title-form') && !control(form).value.trim()) return;
      if (dirty(form)) saveField(form);
    });
  }
  window.addEventListener('pagehide', flush);
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') flush(); });
  // In a browser, leaving is also guarded: safe once a keepalive save is on its way.
  window.addEventListener('beforeunload', function (e) {
    var unsaved = Array.from(document.querySelectorAll('#issue-workspace [data-inline-field]:not([data-quick-sub])')).some(function (form) { return !form.sending && dirty(form); });
    if (waiting || unsaved || (!confirmedUploadLeave && document.querySelector("[data-issue-uploads] [data-pending]"))) { e.preventDefault(); e.returnValue = ''; }
  });
  document.addEventListener('click', function (e) {
    var add = e.target.closest('[data-add-sub]');
    if (add && !add.form.elements.title.value.trim()) { e.preventDefault(); openSub(add.form); }
    var toggle = e.target.closest('[data-issue-toggle]');
    if (toggle) {
      var children = toggle.closest('.issue-tree-item').querySelector('.issue-tree-children');
      if (children) { children.hidden = !children.hidden; toggle.setAttribute('aria-expanded', String(!children.hidden)); }
    }
    var done = e.target.closest('[data-issue-toggle-done]');
    if (done) {
      var hide = done.getAttribute('aria-pressed') !== 'true';
      done.setAttribute('aria-pressed', String(hide));
      done.closest('.issue-subissues').querySelectorAll('.issue-tree-item').forEach(function (row) {
        if (row.querySelector(':scope > .issue-list-row .issue-state.done')) row.hidden = hide && !row.querySelector('.issue-tree-children .issue-state.todo, .issue-tree-children .issue-state.doing');
      });
    }
    if (e.target.closest('[data-focus-sub]')) { var input = document.querySelector('.issue-add-sub input[name=title]'); if (input) input.focus(); }
    // The path as dispatched: a click that redrew part of a menu (the calendar's month) still counts as inside.
    var path = e.composedPath();
    document.querySelectorAll('.issue-popover[open]').forEach(function (menu) { if (path.indexOf(menu) < 0) menu.open = false; });
  });
  document.addEventListener('keydown', function (e) {
    var inline = e.target.closest && e.target.closest('[data-inline-field]');
    // Enter that confirms an IME candidate (keyCode 229 in Safari) is not a commit.
    if (inline && !e.isComposing && e.keyCode !== 229) {
      if (inline.classList.contains('issue-title-form') && e.key === 'Enter') { e.preventDefault(); e.target.blur(); return; }
      if (inline.classList.contains('issue-title-form') && e.key === 'Escape') {
        // Back to the title the server has or is about to have, so the blur that follows saves nothing.
        e.preventDefault(); e.target.value = base(inline); grow(e.target); clearError(inline); e.target.blur(); return;
      }
      if (inline.hasAttribute('data-quick-sub') && e.target.name === 'title' && e.key === 'Enter' && !e.target.value.trim()) { e.preventDefault(); return; }
    }
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter' && e.target.closest && e.target.closest('[data-issue-form]')) {
      e.preventDefault(); e.target.closest('form').requestSubmit();
    }
    if (e.key === 'Escape') document.querySelectorAll('.issue-popover[open]').forEach(function (m) { m.open = false; });
  });
  // The sub-issue composer in place (card 0098, ISSUES-LINEAR.md §2.4): the 添加子议题 row opens into a
  // description, the inherited 课题 and 类型, priority and due date; it stays open for the next one.
  function subComposer() { return document.querySelector('#issue-workspace [data-sub-composer]'); }
  function openSub(form, focusDescription) {
    form = form || subComposer();
    if (!form) return;
    var more = form.querySelector('[data-sub-more]'), desc = form.elements.description;
    form.classList.add('is-open');
    more.hidden = false;
    if (desc && !desc.rlEditor && window.RLIssueEditor) {
      desc.rlEditor = window.RLIssueEditor.mount(desc, {
        placeholder: desc.getAttribute('placeholder'), saveDelay: 864e5,
        onChange: function () { desc.dispatchEvent(new Event('input', {bubbles: true})); }
      });
      desc.hidden = true;
    }
    grow(desc);
    if (focusDescription && desc) { if (desc.rlEditor) desc.rlEditor.focus(); else desc.focus(); }
    else form.elements.title.focus();
  }
  function closeSub(form, clear) {
    form.classList.remove('is-open');
    form.querySelector('[data-sub-more]').hidden = true;
    form.querySelectorAll('.issue-popover[open]').forEach(function (m) { m.open = false; });
    if (clear) {
      form.elements.title.value = ''; delete form.dataset.edited;
      var desc = form.elements.description;
      desc.value = ''; if (desc.rlEditor) desc.rlEditor.setMarkdown('');
    }
  }
  document.addEventListener('click', function (e) {
    var opener = e.target.closest && e.target.closest('[data-open-sub-composer]');
    if (opener && subComposer() && !e.metaKey && !e.ctrlKey && !e.shiftKey) {
      e.preventDefault(); e.stopImmediatePropagation();     // not the centred composer: this one opens in place
      openSub(); subComposer().scrollIntoView({block: 'nearest'});
      return;
    }
    var cancel = e.target.closest && e.target.closest('[data-sub-cancel]');
    if (cancel) closeSub(cancel.form, true);
    // The composer's 分配人 chip shows the chosen one's avatar, not only the name.
    var who = e.target.closest && e.target.closest('[data-sub-composer] [data-choice] .issue-avatar, [data-sub-composer] [data-choice]');
    var pick = who && who.closest('[data-choice]'), face = pick && pick.querySelector('.issue-avatar');
    var shown = face && pick.closest('[data-choice-picker]').querySelector('summary .issue-avatar');
    if (shown) shown.replaceWith(face.cloneNode(true));
    // 创建 submits through the row's + (the form keeps a single submit button, so Enter has one path).
    var create = e.target.closest && e.target.closest('[data-sub-create]');
    if (create) create.form.requestSubmit(create.form.querySelector('[data-add-sub]'));
  });
  document.addEventListener('keydown', function (e) {
    if (typeof e.key !== 'string' || e.isComposing) return;
    var form = subComposer();
    if (!form) return;
    if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.code === 'KeyO') {
      e.preventDefault(); openSub(form); form.scrollIntoView({block: 'nearest'}); return;
    }
    if (!form.contains(e.target)) return;
    if (e.key === 'Tab' && !e.shiftKey && e.target.name === 'title' && !form.classList.contains('is-open')) {
      e.preventDefault(); openSub(form, true); return;
    }
    if (e.key === 'Escape' && form.classList.contains('is-open') && !form.querySelector('.issue-popover[open]')) {
      e.preventDefault(); closeSub(form, false); form.elements.title.focus();
    }
  });

  // Context actions share one confirmation; text editing keeps the native menu.
  var deleting = null;
  function pageRevision() { var r = document.querySelector('#issue-workspace [data-issue-form] input[name=revision]'); return r ? r.value : (document.querySelector('#issue-workspace')?.dataset.revision || ''); }
  function pageRef() {
    var f = document.querySelector('#issue-workspace .issue-title-form');
    return f ? decodeURIComponent(new URL(f.action, location.href).pathname.split('/')[2] || '') : '';
  }
  function actionToast(text, label, fn) {
    var stack = document.getElementById('toast-stack');
    if (!stack) { if (window.rlToast) window.rlToast(text); return; }
    var el = document.createElement('div'), span = document.createElement('span'), button = document.createElement('button');
    el.className = 'toast toast-undo'; el.setAttribute('role', 'status');
    span.textContent = text;
    button.type = 'button'; button.className = 'link-btn'; button.textContent = label;
    el.append(span, button);
    stack.appendChild(el);
    function leave() { el.classList.add('leaving'); setTimeout(function () { el.remove(); }, 300); }
    var timer = setTimeout(leave, 8000);
    button.addEventListener('click', function () { clearTimeout(timer); leave(); fn(); });
  }
  async function restoreDeleted(undo, revision, after) {
    try {
      var data = new FormData(); data.set('revision', revision || '');
      var response = await fetch(undo, {method: 'POST', body: data, headers: {Accept: 'application/json', 'HX-Request': 'true'}});
      var body = await response.json().catch(function () { return {}; });
      if (!response.ok) throw new Error(body.error || '没能撤销，请刷新后再试。');
      after(body);
    } catch (e) { if (window.rlToast) window.rlToast(e.message); }
  }
  var contextTarget = null;
  function closeIssueMenu(restoreFocus) {
    var menu = document.querySelector('[data-issue-context-menu]');
    if (menu) menu.hidden = true;
    if (restoreFocus && contextTarget) {
      var focus = contextTarget.querySelector('.issue-row-title, .issue-key-copy');
      if (focus) focus.focus();
    }
  }
  function confirmDelete(target) {
    var dialog = document.querySelector('dialog[data-issue-delete-dialog]');
    if (!target || !dialog) return;
    closeIssueMenu(false);
    deleting = {ref: target.dataset.issueContext, key: target.dataset.key || '', title: target.dataset.title || '',
                count: Number(target.dataset.count || 0), page: target.hasAttribute('data-page-issue'), row: target.closest('.issue-tree-item')};
    dialog.querySelector('[data-delete-text]').textContent = '删除 ' + (deleting.key ? deleting.key + ' ' : '') + '「' + deleting.title + '」？';
    var kids = dialog.querySelector('[data-delete-kids]');
    kids.hidden = !deleting.count;
    kids.textContent = deleting.count ? '它的 ' + deleting.count + ' 个子议题也会一起删除。' : '';
    dialog.returnValue = '';
    dialog.showModal();
  }
  function showIssueMenu(target, x, y) {
    var menu = document.querySelector('[data-issue-context-menu]');
    if (!menu) return false;
    // Fixed overlays belong to body, outside glass containing blocks.
    document.body.appendChild(menu);
    contextTarget = target;
    menu.hidden = false;
    var box = menu.getBoundingClientRect();
    menu.style.left = Math.max(8, Math.min(x, innerWidth - box.width - 8)) + 'px';
    menu.style.top = Math.max(8, Math.min(y, innerHeight - box.height - 8)) + 'px';
    menu.querySelector('button').focus();
    return true;
  }
  document.addEventListener('contextmenu', function (e) {
    var target = e.target.closest && e.target.closest('[data-issue-context]');
    if (!target || (e.target.closest('.issue-list-row') && !e.target.closest('.issue-list-row[data-issue-context]')) || e.target.closest('input, textarea, select, [contenteditable]') || String(window.getSelection()).trim()) return;
    if (showIssueMenu(target, e.clientX, e.clientY)) e.preventDefault();
  });
  document.addEventListener('pointerdown', function (e) {
    if (!e.target.closest('[data-issue-context-menu]')) closeIssueMenu(false);
  });
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-context-delete]')) confirmDelete(contextTarget);
  });
  document.addEventListener('keydown', function (e) {
    var menu = document.querySelector('[data-issue-context-menu]');
    if (menu && !menu.hidden && (e.key === 'Escape' || e.key === 'Tab')) {
      closeIssueMenu(true); if (e.key === 'Escape') e.preventDefault(); return;
    }
    if (e.key !== 'ContextMenu' && !(e.shiftKey && e.key === 'F10')) return;
    var target = e.target.closest && e.target.closest('[data-issue-context]');
    if (!target || (e.target.closest('.issue-list-row') && !e.target.closest('.issue-list-row[data-issue-context]')) || e.target.closest('input, textarea, select, [contenteditable]')) return;
    var box = e.target.getBoundingClientRect();
    if (showIssueMenu(target, box.left, box.bottom)) e.preventDefault();
  });
  window.addEventListener('resize', function () { closeIssueMenu(false); });
  document.addEventListener('scroll', function () { closeIssueMenu(false); }, true);
  // `close` does not bubble: caught on the way down.
  document.addEventListener('close', function (e) {
    var dialog = e.target;
    if (!dialog.matches || !dialog.matches('dialog[data-issue-delete-dialog]')) return;
    var d = deleting;
    deleting = null;
    if (dialog.returnValue !== 'delete' || !d) return;
    var data = new FormData();
    data.set('revision', pageRevision());
    if (!d.page) data.set('page', pageRef());
    var name = (d.key ? d.key + ' ' : '') + '「' + d.title + '」';
    enqueue(async function () {
      var response = await fetch('/issues/' + encodeURIComponent(d.ref) + '/delete',
                                 {method: 'POST', body: data, headers: {Accept: 'application/json', 'HX-Request': 'true'}});
      var body = await response.json().catch(function () { return {}; });
      if (!response.ok) throw new Error(body.error || '没能删除，请刷新后再试。');
      if (d.page) {
        try { sessionStorage.setItem('rl.issue.deleted', JSON.stringify({undo: body.undo, revision: body.revision, name: name})); } catch (_) { /* no Undo across the move */ }
        location.assign(body.url);
        return;
      }
      if (d.row) d.row.remove();
      actionToast('已删除 ' + name, 'Undo', function () {
        restoreDeleted(body.undo, body.revision, function () { window.rlReloadIssueWorkspace(); if (window.rlToast) window.rlToast('已恢复'); });
      });
      window.rlReloadIssueWorkspace();
    }).catch(function (e) { if (window.rlToast) window.rlToast(e.message); });
  }, true);
  // The focused row wins; otherwise the shortcut applies to the open issue.
  document.addEventListener('keydown', function (e) {
    if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey || e.key !== 'Backspace' || e.isComposing) return;
    if (e.target.closest && e.target.closest('input, textarea, select, [contenteditable]')) return;
    if (document.querySelector('dialog[open]')) return;
    var target = e.target.closest && e.target.closest('[data-issue-context]');
    var menu = document.querySelector('[data-issue-context-menu]');
    if (menu && !menu.hidden) target = contextTarget;
    target = target || document.querySelector('#issue-workspace[data-issue-context][data-page-issue]');
    if (!target) return;
    e.preventDefault(); confirmDelete(target);
  });

  // Reordering sub-issues (card 0147). A 待办-type row is dragged by its grip, or moved with Alt+↑/↓ while focus is
  // in it; outputs keep their place. Each move posts the page's live revision (an old page gets 409, nothing moves)
  // and then shows the list in its manual order (the cookie the page reads) by refreshing the workspace.
  function showManual() {
    document.cookie = 'rl_suborder=manual; path=/; max-age=31536000; SameSite=Lax';
  }
  function moveSub(ref, fields, focus) {
    var data = new FormData();
    data.set('page', pageRef());
    data.set('revision', pageRevision());
    Object.keys(fields).forEach(function (k) { data.set(k, fields[k]); });
    return enqueue(async function () {
      var response = await fetch('/issues/' + encodeURIComponent(ref) + '/move',
                                 {method: 'POST', body: data, headers: {Accept: 'application/json', 'HX-Request': 'true'}});
      var body = await response.json().catch(function () { return {}; });
      if (!response.ok) throw new Error(body.error || '没能调整顺序，请刷新后再试。');
      showManual();
      var page = await fetch(location.href);
      if (!page.ok) throw new Error('顺序已保存，但页面刷新失败，请刷新核对。');
      refreshWorkspace(await page.text());
      var title = focus && document.querySelector('#issue-workspace [data-movable="' + CSS.escape(focus) + '"] .issue-row-title');
      if (title) title.focus();
    }).catch(function (e) { if (window.rlToast) window.rlToast(e.message); });
  }
  document.addEventListener('click', function (e) {
    var toggle = e.target.closest && e.target.closest('[data-sub-order]');
    if (!toggle) return;
    if (toggle.dataset.subOrder === 'manual') document.cookie = 'rl_suborder=; path=/; max-age=0; SameSite=Lax';
    else showManual();
    window.rlReloadIssueWorkspace();
  });
  document.addEventListener('keydown', function (e) {
    if (!e.altKey || e.metaKey || e.ctrlKey || (e.key !== 'ArrowUp' && e.key !== 'ArrowDown')) return;
    var item = e.target.closest && e.target.closest('#issue-workspace .issue-subissues [data-movable]');
    if (!item || e.target.closest('input, textarea, select, [contenteditable]')) return;
    e.preventDefault();
    moveSub(item.dataset.movable, {step: e.key === 'ArrowUp' ? 'up' : 'down'}, item.dataset.movable);
  });
  var dragged = null;
  function clearDrop() { document.querySelectorAll('.drop-before, .drop-after').forEach(function (x) { x.classList.remove('drop-before', 'drop-after'); }); }
  function dropTarget(e) {
    var over = e.target.closest && e.target.closest('.issue-subissues [data-movable]');
    return dragged && over && over !== dragged && over.parentElement === dragged.parentElement ? over : null;
  }
  document.addEventListener('dragstart', function (e) {
    var grip = e.target.closest && e.target.closest('[data-issue-grip]');
    if (!grip) return;
    dragged = grip.closest('[data-movable]');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', dragged.dataset.movable);   // a string, never a file: the PDF drop ignores it
    e.dataTransfer.setDragImage(dragged.querySelector('.issue-list-row'), 24, 20);
    dragged.classList.add('is-dragging');
  });
  document.addEventListener('dragover', function (e) {
    var over = dropTarget(e);
    if (!over) return;
    e.preventDefault();
    var box = over.querySelector('.issue-list-row').getBoundingClientRect(), after = e.clientY > box.top + box.height / 2;
    clearDrop();
    over.classList.add(after ? 'drop-after' : 'drop-before');
  });
  document.addEventListener('drop', function (e) {
    var over = dropTarget(e);
    if (!over) return;
    e.preventDefault();
    var after = over.classList.contains('drop-after'), next = over;
    if (after) {
      next = over.nextElementSibling;
      while (next && (!next.hasAttribute('data-movable') || next === dragged)) next = next.nextElementSibling;
    }
    var ref = dragged.dataset.movable;
    moveSub(ref, {before: next ? next.dataset.movable : ''}, ref);
  });
  document.addEventListener('dragend', function () {
    if (dragged) dragged.classList.remove('is-dragging');
    dragged = null;
    clearDrop();
  });

  // The page an issue was deleted from lands here (its parent): Undo brings it back and opens it again.
  (function () {
    var raw = null;
    try { raw = sessionStorage.getItem('rl.issue.deleted'); sessionStorage.removeItem('rl.issue.deleted'); } catch (_) { return; }
    var d = null;
    try { d = JSON.parse(raw || 'null'); } catch (_) { return; }
    if (!d || !/^\/issues\/[A-Za-z0-9_-]+\/restore$/.test(d.undo || '')) return;
    actionToast('已删除 ' + d.name, 'Undo', function () { restoreDeleted(d.undo, d.revision, function (body) { location.assign(body.url); }); });
  })();
})();

// Shared choice menus and an issue composer mounted directly under body (outside glass panels).
(function () {
  var dialog, opener, changed = false, creating = false, loading = false;
  var drafts = new Map(), context = '';
  try { drafts = new Map(JSON.parse(sessionStorage.getItem('rl.issueDrafts') || '[]')); } catch (_) {}
  function persistDrafts() { try { sessionStorage.setItem('rl.issueDrafts', JSON.stringify(Array.from(drafts))); } catch (_) {} }
  function updateLabelCount(form) {
    form.querySelectorAll('[data-property-caption]').forEach(function (span) { span.textContent = form.elements[span.dataset.propertyCaption].value || span.dataset.placeholder; });
    var n = form.querySelectorAll('input[name=labels]:checked').length;
    form.querySelector('[data-label-count]').textContent = n ? n : '';
  }
  function caption(picker, value) {
    var button = Array.from(picker.querySelectorAll('[data-choice]')).find(function (b) { return b.dataset.choice === value; });
    if (!button) return;
    picker.querySelector('[data-choice-caption]').textContent = button.dataset.caption;
    picker.querySelector('summary').setAttribute('aria-label', picker.querySelector('.issue-menu-heading').textContent + '：' + button.dataset.caption);
    picker.querySelectorAll('[data-choice]').forEach(function (b) {
      var on = b === button; b.classList.toggle('selected', on); b.setAttribute('aria-pressed', String(on));
    });
  }
  async function refreshBackground() {
    if (!document.getElementById('issue-workspace')) { location.reload(); return; }
    await window.rlReloadIssueWorkspace();
  }
  function closeComposer() {
    if (creating || !dialog) return;
    var form = dialog.querySelector('form');
    if (form) { drafts.set(context, Array.from(new FormData(form)).filter(function (entry) { return entry[0] !== 'revision'; })); persistDrafts(); }
    dialog.close(); dialog.remove(); dialog = null;
    if (opener && document.contains(opener)) opener.focus();
    if (changed) { refreshBackground(); return; }
    if (window.rlToast && form && (form.elements.title.value || form.elements.description.value)) window.rlToast('草稿已保留，可重新打开继续');
  }
  async function openComposer(url, trigger) {
    if (loading || dialog) return;
    loading = true; opener = trigger; context = url; changed = false;
    try {
      var response = await fetch(url, {headers: {'X-Issue-Composer': '1'}});
      if (!response.ok) throw new Error('新建窗口暂时打不开，请刷新后重试。');
      dialog = document.createElement('dialog'); dialog.className = 'issue-compose-dialog'; dialog.setAttribute('aria-label', '新建议题');
      dialog.innerHTML = await response.text();
      document.body.appendChild(dialog);
      var form = dialog.querySelector('form'), draft = drafts.get(context);
      if (draft) {
        var restored = new FormData(); draft.forEach(function (entry) { restored.append(entry[0], entry[1]); }); draft = restored;
        form.querySelectorAll('input:not([type=hidden]), textarea').forEach(function (e) {
          if (e.type === 'checkbox') e.checked = draft.getAll(e.name).includes(e.value);
          else if (draft.has(e.name)) e.value = draft.get(e.name);
        });
        form.querySelectorAll('[data-choice-picker]').forEach(function (p) {
          var input = p.querySelector('input[type=hidden]');
          if (draft.has(input.name) && Array.from(p.querySelectorAll('[data-choice]')).some(function (b) { return b.dataset.choice === draft.get(input.name); })) {
            input.value = draft.get(input.name); caption(p, input.value);
          }
        });
      }
      updateLabelCount(form);
      dialog.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && dialog.querySelector('.issue-popover[open]')) {
          e.preventDefault(); e.stopPropagation();
          dialog.querySelectorAll('.issue-popover[open]').forEach(function (menu) { menu.open = false; menu.querySelector('summary').focus(); });
        }
      }, true);
      dialog.addEventListener('cancel', function (e) { e.preventDefault(); closeComposer(); });
      dialog.showModal(); form.querySelectorAll('textarea[data-autogrow]').forEach(window.rlGrow); form.elements.title.focus();
    } catch (e) { if (dialog) { dialog.remove(); dialog = null; } if (window.rlToast) window.rlToast(e.message); }
    finally { loading = false; }
  }
  document.addEventListener('click', function (e) {
    var choice = e.target.closest('[data-choice]');
    if (choice) {
      e.preventDefault(); var picker = choice.closest('[data-choice-picker]'), input = picker.querySelector('input[type=hidden]');
      input.value = choice.dataset.choice; caption(picker, input.value); picker.open = false;
      if (input.form.hasAttribute('data-choice-submit')) input.form.requestSubmit();
      else picker.querySelector('summary').focus();
      return;
    }
    var link = e.target.closest('a[href^="/issues/new"]');
    if (link && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey && e.button === 0) {
      e.preventDefault(); openComposer(link.getAttribute('href'), link); return;
    }
    if (e.target.closest('[data-composer-close]')) {
      if (dialog) closeComposer(); else location.assign('/issues');
    }
  });
  document.addEventListener('input', function (e) {
    var form = e.target.closest('[data-issue-create]'); if (!form) return;
    updateLabelCount(form);
  });
  document.addEventListener('submit', async function (e) {
    var form = e.target.closest('[data-issue-create]'); if (!form) return;
    e.preventDefault(); if (creating) return; creating = true;
    var button = form.querySelector('button[type=submit]'), data = new FormData(form);
    var alert = form.querySelector('[role=alert]'); if (alert) alert.remove();
    var enabled = Array.from(form.querySelectorAll('input, textarea, button')).filter(function (control) { return !control.disabled; });
    enabled.forEach(function (control) { control.disabled = true; });
    try {
      var response = await window.rlIssueRequest(form.action, data, {'X-Issue-Composer': '1'}, '创建失败，输入已保留。');
      var result = await response.json();
      drafts.delete(context); persistDrafts(); changed = true;
      if (data.has('create_more')) {
        form.elements.title.value = ''; form.elements.description.value = ''; form.elements.revision.value = result.revision; window.rlGrow(form.elements.description);
        form.elements.title.focus(); if (window.rlToast) window.rlToast('已创建，可继续添加');
      } else if (dialog && document.getElementById('issue-workspace') && (new URL(result.url, location.href).pathname === location.pathname || document.querySelector('[data-issue-form][data-edited]'))) {
        dialog.close(); dialog.remove(); dialog = null;
        if (opener && document.contains(opener)) opener.focus();
        await refreshBackground();
        if (window.rlToast) window.rlToast('已创建议题');
      } else { location.assign(result.url); }
    } catch (error) {
      var p = document.createElement('p'); p.className = 'form-error'; p.setAttribute('role','alert'); p.textContent = error.message; form.appendChild(p);
    } finally { creating = false; enabled.forEach(function (control) { control.disabled = false; }); button.classList.remove('is-busy'); if (changed && data.has('create_more')) form.elements.title.focus(); }
  });
  document.addEventListener('keydown', function (e) {
    if (typeof e.key !== 'string' || !e.target.closest) return;  // synthetic and autofill events carry no key
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter' && e.target.closest('[data-issue-create]')) {
      e.preventDefault(); e.target.closest('form').requestSubmit(); return;
    }
    // "c" belongs to the excerpt drawer on reading pages; here it only opens the composer on 首页 and 研究档案.
    if (e.defaultPrevented || e.isComposing || e.repeat || !(location.pathname === '/' || /^\/issues(\/|$)/.test(location.pathname))) return;
    if (!e.metaKey && !e.ctrlKey && !e.altKey && e.key.toLowerCase() === 'c' && !e.target.closest('input,textarea,select,[contenteditable]') && !document.querySelector('dialog[open]')) {
      var link = document.querySelector('#create-menu a[href^="/issues/new"]');
      if (link) { e.preventDefault(); openComposer(link.getAttribute('href'), link); }
    }
  });
})();
