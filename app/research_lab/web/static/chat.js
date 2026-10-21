// Chat: one controller, mount(root), drives a composer and the conversation stream inside a root element.
// The /chat page mounts it on .chat-layout; every other page mounts it on the floating assistant
// (partials/_assistant_dock.html). Loaded by partials/_composer.html; the guard keeps a second include from
// binding everything twice.
(function () {
  if (window.RLChat) return;
  var RLChat = window.RLChat = { version: 2 };
  var calm = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;

  // PDFs go to the library; Word / Markdown / text are Leo's own materials for 建课题
  function accepted(f) { return f && (f.type === 'application/pdf' || /\.(pdf|docx|doc|rtf|odt|md|markdown|txt)$/i.test(f.name)); }
  function hasFiles(e) {
    var types = e.dataTransfer && e.dataTransfer.types;
    return types && Array.prototype.indexOf.call(types, 'Files') !== -1;
  }
  function plainClick(e) { return e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey; }

  // Where Leo is when he asks (agent/tasks.page_label): path and query, plus a #ctx fragment with the page's title,
  // active tab, date heading and the text he last selected outside the assistant. Clicking into the composer
  // collapses the page selection, so it is remembered as it changes.
  var lastSelection = '';
  document.addEventListener('selectionchange', function () {
    var sel = window.getSelection ? window.getSelection() : null;
    if (!sel || sel.isCollapsed || !sel.anchorNode) return;
    var node = sel.anchorNode.nodeType === 1 ? sel.anchorNode : sel.anchorNode.parentElement;
    if (node && node.closest && node.closest('[data-dock], .chat-layout, form[data-composer]')) return;
    lastSelection = String(sel).replace(/\s+/g, ' ').trim().slice(0, 200);
  });
  function pageForAssistant() {
    var loc = window.location;
    if (loc.pathname.indexOf('/chat') === 0) return loc.pathname;
    var main = document.querySelector('main') || document, ctx = {};
    var title = main.querySelector('.page-title'), tab = main.querySelector('.tabs [aria-current="page"]');
    var heading = main.querySelector('.edition-nav h2');
    if (title) ctx.title = title.textContent.trim();
    if (tab) ctx.tab = (tab.firstChild && tab.firstChild.nodeType === 3 ? tab.firstChild.textContent : tab.textContent).trim();
    if (heading) ctx.heading = heading.textContent.replace(/\s+/g, ' ').trim();
    if (lastSelection) ctx.selection = lastSelection;
    return loc.pathname + loc.search + (Object.keys(ctx).length ? '#ctx=' + encodeURIComponent(JSON.stringify(ctx)) : '');
  }

  // hooks.redirect(url, threadId) → Promise<boolean>: the server answered with a thread to go to (the first
  //   message of a new conversation); resolve true when this root now shows it. Default: navigate there.
  // hooks.dropTarget: where dropped files are taken (default the root); hooks.onDrag runs when files enter it.
  // hooks.fresh: with no thread, ask the server for a new conversation rather than a recently active one.
  function mount(root, hooks) {
    hooks = hooks || {};
    var form = root.querySelector('form[data-composer]'), scroller = root.querySelector('.chat-scroll');
    if (!form || !scroller) return null;
    var input = form.querySelector('.composer-input');
    var fileInput = form.querySelector('.composer-file');
    var chips = form.querySelector('.composer-files');
    var status = form.querySelector('.composer-status');
    var send = form.querySelector('.composer-send');
    var threadField = form.querySelector('[name=thread_id]');
    var files = [];
    var busy = false;

    function stream() { return scroller.querySelector('.chat-stream'); }

    function setStatus(text, isError) {
      status.textContent = text || '';
      status.classList.toggle('is-error', !!isError);
    }

    // Up to 40% of the conversation pane; the dock grows with it and the stream gives way. The box holds its
    // height while the textarea is measured: a momentary collapse would clamp the stream's scroll position,
    // which reads as the reader scrolling up and unpins it.
    function grow() {
      var pane = form.closest('.chat-main, .dock-panel'), box = input.parentNode;
      var max = (pane && pane.clientHeight ? pane.clientHeight : window.innerHeight) * 0.4;
      box.style.minHeight = box.offsetHeight + 'px';
      input.style.height = 'auto';
      var full = input.scrollHeight;
      input.style.height = Math.min(full, max) + 'px';
      input.style.overflowY = full > max ? 'auto' : 'hidden';
      box.style.minHeight = '';
    }

    function focus() {
      input.focus({ preventScroll: true });
      input.setSelectionRange(input.value.length, input.value.length);
    }

    function renderChips() {
      chips.innerHTML = '';
      chips.hidden = files.length === 0;
      files.forEach(function (f, i) {
        var chip = document.createElement('span');
        chip.className = 'composer-file-chip';
        var name = document.createElement('span');
        name.textContent = f.name;
        var rm = document.createElement('button');
        rm.type = 'button';
        rm.setAttribute('aria-label', 'Remove ' + f.name);
        rm.textContent = '×';
        rm.addEventListener('click', function () { files.splice(i, 1); renderChips(); });
        chip.appendChild(name);
        chip.appendChild(rm);
        chips.appendChild(chip);
      });
    }

    function addFiles(list) {
      var skipped = 0;
      Array.prototype.forEach.call(list || [], function (f) {
        if (accepted(f)) files.push(f); else skipped++;
      });
      renderChips();
      setStatus(skipped ? '跳过 ' + skipped + ' 个不支持的文件（可用 PDF、Word、Markdown）' : '', !!skipped);
      input.focus({ preventScroll: true });
    }

    fileInput.addEventListener('change', function () { addFiles(fileInput.files); fileInput.value = ''; });

    // The stream follows the newest message while the reader is at the end. Scrolling up unpins it; coming back
    // within reach of the end pins it again. Only scroll events change `pinned`, so content that grows (polled
    // replies, task steps) or shrinks (a finished task folding its steps) never flips it by itself.
    var pinned = true, lastTop = 0, glideUntil = 0;

    function toEnd(smooth) {
      pinned = true;
      if (smooth && !(calm && calm.matches)) {
        glideUntil = Date.now() + 500;
        scroller.scrollTo({ top: scroller.scrollHeight, behavior: 'smooth' });
      } else {
        scroller.scrollTop = scroller.scrollHeight;
        lastTop = scroller.scrollTop;
      }
    }

    function follow() {
      if (!pinned) return;
      // re-aim a glide that is still running instead of cutting it short with a jump
      if (Date.now() < glideUntil) scroller.scrollTo({ top: scroller.scrollHeight, behavior: 'smooth' });
      else { scroller.scrollTop = scroller.scrollHeight; lastTop = scroller.scrollTop; }
    }

    scroller.addEventListener('scroll', function () {
      var top = scroller.scrollTop, gap = scroller.scrollHeight - top - scroller.clientHeight;
      if (gap <= 2) pinned = true;            // at the end, also after a clamp when content shrank
      else if (top < lastTop) pinned = false;
      else if (gap < 80) pinned = true;
      lastTop = top;
    }, { passive: true });
    if (window.ResizeObserver) {
      var watch = new ResizeObserver(follow);
      watch.observe(scroller);                  // the composer grew or the window changed
      if (stream()) watch.observe(stream());    // replies, cards and images changed the content
    } else {
      document.body.addEventListener('htmx:afterSettle', follow);
    }
    // Opening a folded trace is reading, not waiting: let it unfold in place.
    scroller.addEventListener('click', function (e) {
      if (e.target.closest && e.target.closest('summary')) pinned = false;
    });

    // Another conversation (or the same one reloaded) replaces the stream's content; the element stays, so the
    // observer above keeps watching it.
    function setThread(threadId) {
      threadField.value = threadId || '';
      stream().setAttribute('data-thread', threadId || '');
    }

    function show(html, threadId) {
      var s = stream();
      s.innerHTML = html;
      setThread(threadId);
      if (window.htmx) window.htmx.process(s);
      toEnd(false);
    }

    function submit() {
      if (busy) return;
      var text = input.value.trim();
      if (!text && !files.length) { input.focus({ preventScroll: true }); return; }
      var data = new FormData();
      data.append('text', text);
      var sentTo = threadField.value;
      data.append('thread_id', sentTo);
      data.append('page', pageForAssistant());
      lastSelection = '';   // sent once: a later message on the same page does not carry it again
      if (hooks.fresh && !threadField.value) data.append('fresh', '1');
      files.forEach(function (f) { data.append('files', f, f.name); });
      busy = true;
      form.classList.add('is-busy');
      send.disabled = true;
      setStatus(files.length ? '正在添加 ' + files.length + ' 个文件…' : '发送中…');
      fetch('/chat/send', { method: 'POST', body: data, headers: { 'HX-Request': 'true' }, credentials: 'same-origin' })
        .then(function (r) {
          var redirect = r.headers.get('HX-Redirect');
          if (redirect) {
            var go = hooks.redirect || function (url) { window.location.href = url; return Promise.resolve(false); };
            return go(redirect, r.headers.get('X-Chat-Thread') || '').then(function (stays) { return stays ? '' : null; });
          }
          return r.text().then(function (html) {
            if (!r.ok) {
              var tmp = document.createElement('div');
              tmp.innerHTML = html;
              throw new Error(tmp.textContent.trim() || ('Error ' + r.status));
            }
            return html;
          });
        })
        .then(function (html) {
          if (html === null) return;   // leaving for another page
          var s = stream();
          // the reader switched threads while this was in flight: the reply belongs to the thread it was sent to
          if (sentTo && threadField.value !== sentTo) html = '';
          if (html) {
            var welcome = s.querySelector('.chat-welcome');
            if (welcome) welcome.remove();
            var box = document.createElement('div');
            box.innerHTML = html;
            Array.prototype.slice.call(box.children).forEach(function (el) {
              s.appendChild(el);
              if (window.htmx) window.htmx.process(el);
            });
          }
          input.value = '';
          files = [];
          renderChips();
          grow();
          setStatus('');
          if (html) toEnd(true);
        })
        .catch(function (err) { setStatus(err.message || 'Send failed', true); })
        .then(function () {
          busy = false;
          form.classList.remove('is-busy');
          send.disabled = false;
        });
    }

    form.addEventListener('submit', function (e) { e.preventDefault(); submit(); });
    input.addEventListener('input', grow);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
        e.preventDefault();
        submit();
      }
    });

    root.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('[data-chat-command]') : null;
      if (!btn) return;
      e.preventDefault();
      input.value = btn.getAttribute('data-chat-command');
      grow();
      if (btn.hasAttribute('data-send')) submit();
      else focus();
    });

    var dropTarget = hooks.dropTarget || root;
    var depth = 0;
    dropTarget.addEventListener('dragenter', function (e) {
      if (!hasFiles(e)) return;
      depth++;
      form.classList.add('is-drop');
      if (hooks.onDrag) hooks.onDrag();
    });
    dropTarget.addEventListener('dragover', function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
    });
    dropTarget.addEventListener('dragleave', function (e) {
      if (!hasFiles(e)) return;
      depth = Math.max(0, depth - 1);
      if (!depth) form.classList.remove('is-drop');
    });
    dropTarget.addEventListener('drop', function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth = 0;
      form.classList.remove('is-drop');
      addFiles(e.dataTransfer.files);
    });

    // Confirmation cards (partials/_issue_plan.html) under a reply. 执行 counts the ticked rows. While one of a
    // card's posts is in flight its buttons wait (the server applies once anyway); the answer replaces the whole
    // card, the stream's ResizeObserver keeps the reader where they were, and focus returns to the new card.
    function plan(el) { return el && el.closest ? el.closest('[data-plan]') : null; }
    var planFocus = '';
    root.addEventListener('change', function (e) {
      var card = plan(e.target), apply = card && card.querySelector('[data-plan-apply]');
      if (!apply || !e.target.classList.contains('plan-tick')) return;
      var n = card.querySelectorAll('.plan-tick:checked').length;
      apply.querySelector('[data-plan-count]').textContent = n;
      apply.disabled = !n;
    });
    root.addEventListener('keydown', function (e) {
      // Enter in a title would submit the whole card; it only ends the edit
      if (e.key !== 'Enter' || e.isComposing || e.keyCode === 229 || !e.target.matches('[data-plan-title]')) return;
      e.preventDefault();
      e.target.blur();
    });
    root.addEventListener('htmx:beforeRequest', function (e) {
      var card = plan(e.target);
      if (!card) return;
      if (card.classList.contains('is-posting')) { e.preventDefault(); return; }
      var cfg = e.detail && e.detail.requestConfig, by = cfg && cfg.triggeringEvent && cfg.triggeringEvent.submitter;
      card.classList.add('is-posting');
      card.setAttribute('aria-busy', 'true');
      Array.prototype.forEach.call(card.querySelectorAll('button'), function (b) {
        if (b === by) b.classList.add('is-busy');
        else { b.setAttribute('data-was-disabled', b.disabled ? '1' : ''); b.disabled = true; }
      });
      planFocus = card.contains(document.activeElement) ? card.id : '';
    });
    root.addEventListener('htmx:afterRequest', function (e) {
      var card = plan(e.target);
      if (!card || !card.isConnected || (e.detail && e.detail.successful)) return;   // swapped: the new card starts fresh
      card.classList.remove('is-posting');
      card.removeAttribute('aria-busy');
      Array.prototype.forEach.call(card.querySelectorAll('button'), function (b) {
        b.classList.remove('is-busy');
        if (b.hasAttribute('data-was-disabled')) { b.disabled = b.getAttribute('data-was-disabled') === '1'; b.removeAttribute('data-was-disabled'); }
      });
      planFocus = '';
      if (window.rlToast) window.rlToast('没有完成，稍后再试');
    });
    root.addEventListener('htmx:afterSettle', function () {
      var card = planFocus && document.getElementById(planFocus);
      if (!card) return;
      planFocus = '';
      var target = card.querySelector('button:not([disabled]), a[href]') || card;
      if (target === card) card.setAttribute('tabindex', '-1');
      target.focus({ preventScroll: true });
    });

    grow();
    window.addEventListener('resize', grow);
    toEnd(false);
    return { form: form, input: input, focus: focus, grow: grow, show: show, setThread: setThread, toEnd: toEnd,
             setStatus: setStatus };
  }
  RLChat.mount = mount;

  // ---- /chat: the expanded view with the thread list --------------------------------------------------------
  function page() {
    var root = document.querySelector('.chat-layout');
    if (!root) return;
    var chat = mount(root, { redirect: adopt, dropTarget: document });
    if (!chat) return;

    // The first message of a new conversation answers with HX-Redirect to the thread's page. Here that page's
    // head, thread list and stream are swapped in under the same URL change, so the panel does not blink
    // through a full reload; if anything is missing it falls back to a plain navigation.
    var shownPath = window.location.pathname;
    function adopt(url) {
      var head = root.querySelector('.chat-head'), list = root.querySelector('#chat-threads');
      if (!head || !list || !/^\/chat\/[^/?#]+$/.test(url) || !window.history.pushState) {
        window.location.href = url;
        return Promise.resolve(false);
      }
      return fetch(url, { credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
        .then(function (html) {
          var doc = new DOMParser().parseFromString(html, 'text/html');
          var s2 = doc.getElementById('chat-stream'), head2 = doc.querySelector('.chat-head'), list2 = doc.getElementById('chat-threads');
          if (!s2 || !head2 || !list2) throw new Error('unexpected page');
          window.history.pushState(null, '', url);
          shownPath = window.location.pathname;
          document.title = doc.title;
          head.innerHTML = head2.innerHTML;
          list.innerHTML = list2.innerHTML;
          if (window.htmx) [head, list].forEach(function (el) { window.htmx.process(el); });
          chat.show(s2.innerHTML, s2.getAttribute('data-thread'));
          return true;
        })
        .catch(function () { window.location.href = url; return false; });
    }
    // Back / forward across an adopted page: the panes show the other URL's thread, so load it for real.
    window.addEventListener('popstate', function () {
      if (window.location.pathname !== shownPath) window.location.reload();
    });

    // ⌘K is the global search everywhere else; here the composer is the thing to type into.
    document.addEventListener('keydown', function (e) {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        chat.focus();
      }
    });

    // The thread list has its own scroll: bring the open conversation into view (sideways when stacked).
    var box = root.querySelector('.chat-thread-scroll'), item = box && box.querySelector('li.active');
    if (item) {
      var b = box.getBoundingClientRect(), r = item.getBoundingClientRect();
      if (r.top < b.top || r.bottom > b.bottom) box.scrollTop += r.top - b.top - (b.height - r.height) / 2;
      if (r.left < b.left || r.right > b.right) box.scrollLeft += r.left - b.left - (b.width - r.width) / 2;
    }
    if (chat.form.hasAttribute('data-composer-here')) chat.input.focus({ preventScroll: true });
  }

  // ---- the floating assistant on every other page --------------------------------------------------------------
  // Pages are full loads, so the open state and the thread are kept per viewer in localStorage and restored by
  // the inline script in _assistant_dock.html before first paint. The conversation is fetched only while open;
  // closing empties it, which also stops its polling.
  var KEY = 'rl.dock';
  function dock() {
    var root = document.querySelector('[data-dock]');
    if (!root) return;
    var fab = root.querySelector('[data-dock-toggle]');
    var title = root.querySelector('[data-dock-title]');
    var switcher = root.querySelector('.dock-switch');
    var threads = root.querySelector('[data-dock-threads]');
    var expand = root.querySelector('[data-dock-expand]');
    var state = {};
    try { state = JSON.parse(window.localStorage.getItem(KEY) || '{}') || {}; } catch (e) { state = {}; }
    function save() { try { window.localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) { /* private window */ } }

    var chat = mount(root, {
      fresh: true,
      dropTarget: root,
      onDrag: function () { if (!isOpen()) open(false); },
      redirect: function (url, threadId) { return threadId ? load(threadId).then(function () { return true; }) : Promise.resolve(false); },
    });
    if (!chat) return;

    function isOpen() { return root.classList.contains('is-open'); }
    function load(threadId, prefetched) {
      chat.setThread(threadId);   // a message typed before the reply arrives still goes to this thread
      var req = prefetched || fetch('/chat/dock?thread=' + encodeURIComponent(threadId || ''), { credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); });
      return req.then(function (html) {
        var tpl = document.createElement('template');
        tpl.innerHTML = html;
        var meta = tpl.content.querySelector('[data-dock-meta]'), src = tpl.content.querySelector('[data-dock-threads-src]');
        var s = tpl.content.querySelector('.chat-stream');
        if (!meta || !s) throw new Error('unexpected reply');
        state.thread = meta.getAttribute('data-thread') || '';
        state.title = meta.getAttribute('data-title') || '新对话';
        save();
        title.textContent = state.title;
        expand.setAttribute('href', state.thread ? '/chat/' + state.thread : '/chat');
        threads.innerHTML = src ? src.innerHTML : '';
        chat.show(s.innerHTML, state.thread);
        chat.setStatus('');
      }).catch(function () {
        chat.setStatus('没能载入对话，稍后再试', true);
      });
    }

    function setOpen(on) {
      root.classList.toggle('is-open', on);
      fab.setAttribute('aria-expanded', on ? 'true' : 'false');
      state.open = on;
      save();
    }

    function open(withFocus) {
      if (!isOpen()) {
        setOpen(true);
        load(state.thread);
      }
      if (withFocus) chat.focus();
    }

    function close() {
      if (!isOpen()) return;
      var inside = root.contains(document.activeElement);
      setOpen(false);
      switcher.open = false;
      chat.show('', state.thread);
      if (inside) fab.focus({ preventScroll: true });
    }

    function toggle() { if (isOpen()) close(); else open(true); }
    RLChat.dock = { open: open, close: close, toggle: toggle };

    // Restored open by the inline script: take over its request, then let later opens animate.
    if (isOpen()) {
      load(state.thread, window.RLDockPrefetch);
      window.RLDockPrefetch = null;
      window.requestAnimationFrame(function () {
        window.requestAnimationFrame(function () { root.classList.remove('is-restoring'); });
      });
    }

    fab.addEventListener('click', toggle);
    root.querySelector('[data-dock-close]').addEventListener('click', close);
    root.querySelector('[data-dock-new]').addEventListener('click', function () {
      load('').then(function () { chat.focus(); });
    });
    expand.addEventListener('click', function () { state.open = false; save(); });
    threads.addEventListener('click', function (e) {
      var a = e.target.closest('[data-dock-thread]');
      if (!a || !plainClick(e)) return;
      e.preventDefault();
      switcher.open = false;
      load(a.getAttribute('data-dock-thread'));
    });

    // A link marked data-dock-open opens the panel instead of /chat (the sidebar's Theo goes to the full page).
    document.addEventListener('click', function (e) {
      var a = e.target.closest ? e.target.closest('[data-dock-open]') : null;
      if (!a || !plainClick(e)) return;
      e.preventDefault();
      document.body.classList.remove('nav-open');
      open(true);
    });

    // ⌘J toggles from anywhere (⌘K stays the global search); Esc closes the switcher, then the panel.
    document.addEventListener('keydown', function (e) {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && (e.key === 'j' || e.key === 'J')) {
        e.preventDefault();
        toggle();
      }
    });
    root.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape' || e.isComposing || !isOpen()) return;
      e.preventDefault();
      if (switcher.open) { switcher.open = false; switcher.querySelector('summary').focus(); }
      else close();
    });
  }

  function init() { page(); dock(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();

// 交付任务 confirm page (pages/chat_setup.html): 上移 / 下移 reorder the section rows and keep the hidden
// section_order in step (rendered indices in the new order), which materials.with_edits applies.
(function () {
  function initSetup() {
    var box = document.getElementById('setup-sections');
    var order = document.getElementById('section-order');
    if (!box || !order) return;
    function rows() { return Array.prototype.slice.call(box.querySelectorAll('.setup-section')); }
    function renumber() {
      var rs = rows();
      rs.forEach(function (r, i) {
        var n = r.querySelector('.setup-n'); if (n) n.textContent = '第 ' + (i + 1) + ' 节';
        var up = r.querySelector('[data-move=up]'), down = r.querySelector('[data-move=down]');
        if (up) up.disabled = i === 0;
        if (down) down.disabled = i === rs.length - 1;
      });
      order.value = rs.map(function (r) { return r.dataset.index; }).join(',');
    }
    box.addEventListener('click', function (e) {
      var b = e.target.closest('[data-move]'); if (!b || b.disabled) return;
      var row = b.closest('.setup-section'); if (!row) return;
      var sib = b.dataset.move === 'up' ? row.previousElementSibling : row.nextElementSibling;
      if (!sib || !sib.classList.contains('setup-section')) return;
      if (b.dataset.move === 'up') box.insertBefore(row, sib); else box.insertBefore(sib, row);
      renumber();
      b.focus();
    });
    renumber();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initSetup);
  else initSetup();
})();
