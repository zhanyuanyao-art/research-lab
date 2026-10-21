// The import sheet (card 0130, docs/design/LIBRARY-UI.md §3.3). One dialog for every way in: the library's Import,
// 新建 → 导入文献, a PDF dropped anywhere (app.js's drop forms are caught at submit, so its drag overlay stays as
// it is), the dialog's own drop zone and Choose files, and the paste box for DOIs, arXiv ids and article URLs.
// Each file or line is one request to routes/imports.py, one at a time, and one queue row that shows how it went.
// Closing the dialog does not stop the queue; once it is empty and the dialog is closed, the library reloads
// so the new papers appear at the top.
(function () {
  var sheet = null, loading = null, queue = [], busy = false, added = 0;
  var icons = (document.querySelector('use[href*="icons.svg"]') || {getAttribute: function () { return '/static/icons.svg#x'; }})
    .getAttribute('href').split('#')[0];

  function icon(name) { return '<svg class="i sm" aria-hidden="true"><use href="' + icons + '#' + name + '"></use></svg>'; }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
  function $(sel) { return sheet && sheet.querySelector(sel); }

  function open() {
    if (!sheet) {
      sheet = document.createElement('dialog');
      sheet.className = 'sheet';
      sheet.id = 'import-sheet';
      sheet.setAttribute('aria-labelledby', 'import-title');
      sheet.innerHTML = '<p class="sheet-loading" role="status">正在打开…</p>';
      document.body.appendChild(sheet);
      sheet.addEventListener('close', closed);
      loading = fetch('/imports/sheet?page=' + encodeURIComponent(location.pathname + location.search))
        .then(function (r) { if (!r.ok) throw new Error(); return r.text(); })
        .then(function (html) { sheet.innerHTML = html; wire(); })
        .catch(function () { sheet.innerHTML = '<p class="sheet-loading" role="alert">导入窗口没有打开，请刷新后重试。</p>'; });
    }
    if (!sheet.open) sheet.showModal();
    return loading;
  }
  function closed() {
    if (!busy && added && /^\/papers\/?$/.test(location.pathname)) location.reload();
  }

  function picks() {
    var form = $('[data-import-form]'), out = {};
    if (form) ['direction', 'project', 'collection'].forEach(function (k) { out[k] = form.elements[k] ? form.elements[k].value : ''; });
    return out;
  }
  function row(name) {
    var list = $('[data-import-queue]');
    list.hidden = false;
    var li = document.createElement('li');
    list.appendChild(li);
    draw(li, {state: 'wait', name: name, note: '等待中…'});
    return li;
  }
  function draw(li, r) {
    var st = {ok: ['ok', 'check'], exists: ['ok', 'check'], biblio: ['', 'file-text'], dup: ['warn', 'info'],
      error: ['err', 'x'], run: ['run', 'refresh-cw'], wait: ['', 'refresh-cw']}[r.state] || ['', 'info'];
    var title = r.title ? '<span class="name serif">' + esc(r.title) + '</span>' : '<span class="name">' + esc(r.name) + '</span>';
    var act = '';
    if (r.paper_id && (r.state === 'ok' || r.state === 'exists')) act = '<a class="btn ghost small" href="/papers/' + encodeURIComponent(r.paper_id) + '">Open</a>';
    if (r.paper_id && r.state === 'biblio') act = '<a class="btn ghost small" href="/papers/' + encodeURIComponent(r.paper_id) + '">Add PDF</a>';
    if (r.state === 'dup' && r.pending) {
      act = '<span class="act two"><button class="btn secondary small" type="button" data-answer="same">Same paper</button>' +
        '<button class="btn ghost small" type="button" data-answer="keep">Keep both</button></span>';
      li.dataset.pending = r.pending;
      li.dataset.duplicateOf = r.duplicate_of || '';
    }
    li.dataset.state = r.state;
    li.innerHTML = '<span class="q-state ' + st[0] + '">' + icon(st[1]) + '</span><span class="q-main">' + title +
      '<span class="note">' + esc(r.note) + '</span></span>' + (act ? (act.indexOf('act two') > 0 ? act : '<span class="act">' + act + '</span>') : '<span></span>');
  }

  function enqueue(job) {
    job.li = row(job.name);
    job.picks = picks();
    queue.push(job);
    pump();
  }
  function post(url, data) {
    return fetch(url, {method: 'POST', body: data, headers: {'HX-Request': 'true'}}).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        if (!r.ok) return {state: 'error', note: body.detail || '导入失败，请重试。'};
        return body;
      });
    }).catch(function () { return {state: 'error', note: '连不上应用，请重试。'}; });
  }
  function pump() {
    if (busy || !queue.length) return;
    busy = true;
    var job = queue.shift();
    draw(job.li, {state: 'run', name: job.name, note: job.file ? '正在识别引用信息…' : '正在查找引用信息和开放获取 PDF…'});
    var data = new FormData();
    Object.keys(job.picks).forEach(function (k) { data.append(k, job.picks[k]); });
    if (job.file) data.append('file', job.file, job.file.name); else data.append('id', job.id);
    post(job.file ? '/imports/files' : '/imports/identifiers', data).then(function (r) {
      r.name = r.name || job.name;
      if (r.state === 'ok' || r.state === 'biblio') added++;
      job.li.dataset.picks = JSON.stringify(job.picks);
      draw(job.li, r);
    }).then(function () {
      busy = false;
      if (queue.length) pump(); else if (sheet && !sheet.open) closed();
    });
  }
  function isPdf(f) { return !(f.type && f.type !== 'application/pdf' && !/\.pdf$/i.test(f.name)); }
  function files(list) {
    var skipped = [];
    Array.prototype.forEach.call(list || [], function (f) {
      if (!isPdf(f)) { skipped.push(f.name); return; }
      enqueue({file: f, name: f.name});
    });
    // A file that is not a PDF is not imported; the queue says so instead of passing over it in silence.
    if (skipped.length) {
      var queueList = $('[data-import-queue]'), li = document.createElement('li');
      queueList.hidden = false;
      li.dataset.state = 'skip';
      li.innerHTML = '<span class="q-state">' + icon('info') + '</span><span class="q-main"><span class="name">跳过 ' + skipped.length +
        ' 个文件</span><span class="note">只导入 PDF：' + esc(skipped.slice(0, 5).join('、')) + (skipped.length > 5 ? ' 等' : '') + '</span></span><span></span>';
      queueList.appendChild(li);
    }
  }

  function wire() {
    var input = $('[data-import-files]'), zone = $('[data-import-drop]'), form = $('[data-import-form]');
    input.addEventListener('change', function () { files(input.files); input.value = ''; });
    ['dragenter', 'dragover'].forEach(function (ev) { zone.addEventListener(ev, function (e) { e.preventDefault(); e.stopPropagation(); zone.classList.add('over'); }); });
    ['dragleave', 'drop'].forEach(function (ev) { zone.addEventListener(ev, function () { zone.classList.remove('over'); }); });
    zone.addEventListener('drop', function (e) { e.preventDefault(); e.stopPropagation(); files(e.dataTransfer.files); });
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      e.stopPropagation();   // app.js would mark Add busy until the page changes, and this page never changes
      var box = $('[data-import-ids]');
      box.value.split(/\n+/).map(function (s) { return s.trim(); }).filter(Boolean).forEach(function (id) { enqueue({id: id, name: id}); });
      box.value = '';
    });
    sheet.addEventListener('click', function (e) {
      if (e.target.closest('[data-import-close]')) { sheet.close(); return; }
      var answer = e.target.closest('[data-answer]');
      if (!answer) return;
      var li = answer.closest('li'), data = new FormData(), saved = JSON.parse(li.dataset.picks || '{}');
      Object.keys(saved).forEach(function (k) { data.append(k, saved[k]); });
      if (answer.dataset.answer === 'same') data.append('paper_id', li.dataset.duplicateOf);
      draw(li, {state: 'run', name: li.querySelector('.name').textContent, note: '正在处理…'});
      post('/imports/pending/' + encodeURIComponent(li.dataset.pending) + '/' + answer.dataset.answer, data).then(function (r) {
        if (r.state === 'ok') added++;
        draw(li, r);
      });
    });
  }

  // Ways in. A label for a file input would open the system picker: it opens the sheet instead, which has its own.
  document.addEventListener('click', function (e) {
    var t = e.target.closest('[data-import-open], label[for="dropfile"], label[for="global-drop-file"]');
    if (!t) return;
    e.preventDefault();
    var menu = t.closest('details.menu'); if (menu) menu.open = false;
    open();
  });
  // app.js submits #global-drop-form for a PDF dropped anywhere and #dropzone for the library's picker; the files
  // come here instead. Its drop handler hands the files to the form's first input, which is the hidden `page`
  // field, so the dropped files are taken from the drop event itself, before app.js sees it.
  var dropped = null;
  window.addEventListener('drop', function (e) {
    dropped = !(e.target.closest && e.target.closest('[data-issue-drop]')) && e.dataTransfer && e.dataTransfer.files.length
      ? Array.prototype.slice.call(e.dataTransfer.files) : null;
  }, true);
  function take(list) {
    document.body.classList.remove('importing');
    // On the reader page of a paper that has no PDF yet, one PDF dropped there is most likely that paper's: offer to
    // attach it (POST /papers/<id>/files, which refuses a PDF another paper already has) before importing a new one.
    var reader = document.getElementById('reader'), pdfs = Array.prototype.filter.call(list || [], isPdf);
    if (reader && reader.dataset.paperId && !reader.dataset.pdfUrl && pdfs.length === 1) { offerAttach(reader.dataset.paperId, pdfs[0], list); return; }
    (open() || Promise.resolve()).then(function () { files(list); });
  }
  function ensureCss() {
    // The sheet brings import.css with its body; this offer can come first, so it asks for the same file.
    if (document.querySelector('link[href*="import.css"]')) return;
    var js = document.querySelector('script[src*="import.js"]'), link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = js ? js.getAttribute('src').replace('import.js', 'import.css') : '/static/import.css';
    document.head.appendChild(link);
  }
  function offerAttach(paperId, file, list) {
    ensureCss();
    var box = document.createElement('dialog');
    box.className = 'sheet attach-offer';
    box.setAttribute('aria-labelledby', 'attach-title');
    box.innerHTML = '<header><h2 id="attach-title">这篇文献还没有 PDF</h2></header><div class="imp-body"><p>把「' + esc(file.name) +
      '」挂到这篇文献上，还是作为一篇新文献导入？</p><p class="note" data-attach-error role="alert" hidden></p></div>' +
      '<footer><span class="spacer"></span><button class="btn ghost small" type="button" data-attach-cancel>Cancel</button>' +
      '<button class="btn secondary small" type="button" data-attach-new>Import as new</button>' +
      '<button class="btn small" type="button" data-attach-here>Attach to this paper</button></footer>';
    document.body.appendChild(box);
    box.addEventListener('close', function () { box.remove(); });
    box.addEventListener('click', function (e) {
      if (e.target.closest('[data-attach-cancel]')) { box.close(); return; }
      if (e.target.closest('[data-attach-new]')) { box.close(); (open() || Promise.resolve()).then(function () { files(list); }); return; }
      var here = e.target.closest('[data-attach-here]');
      if (!here) return;
      here.disabled = true;
      var data = new FormData();
      data.append('files', file, file.name);
      fetch('/papers/' + encodeURIComponent(paperId) + '/files', {method: 'POST', body: data, headers: {'HX-Request': 'true'}})
        .then(function (r) {
          var error = new URL(r.url, location.href).searchParams.get('pdf_error');
          if (!r.ok || error) throw new Error(error || '没有挂上，请重试。');
          location.assign(r.url);
        })
        .catch(function (err) {
          here.disabled = false;
          var note = box.querySelector('[data-attach-error]');
          note.textContent = err.message;
          note.hidden = false;
        });
    });
    box.showModal();
  }
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || !/^(global-drop-form|dropzone)$/.test(form.id)) return;
    var input = form.querySelector('input[type=file]');
    var picked = dropped || (input && input.files.length ? Array.prototype.slice.call(input.files) : null);
    dropped = null;
    if (!picked) return;
    e.preventDefault();
    if (input) input.value = '';
    take(picked);
  }, true);
  // 新建 → 导入文献 opens the sheet (the click handler above), but a picker opened another way still lands here.
  document.addEventListener('change', function (e) {
    if (e.target.id !== 'global-drop-file' || !e.target.files.length) return;
    var picked = Array.prototype.slice.call(e.target.files);
    e.target.value = '';
    take(picked);
  }, true);
  window.rlImport = {open: open};
})();
