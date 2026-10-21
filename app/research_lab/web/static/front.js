// 首页轮播 (partials/_front_carousel.html), after the W3C APG tabbed carousel:
// - slides sit in a scroll-snap track, so swipe and trackpad scrolling work without a library; the track is
//   fixed at the tallest slide's natural height (measured at start, on resize and when a slide's content
//   changes), so switching pages never moves the rest of the page;
// - tabs, previous / next and arrow keys move between slides; the active tab follows the scroll;
// - it rotates every ROTATE_MS only on a fine pointer without reduced motion; it pauses while hovered or
//   focused, and stops for good once Leo uses a control (users miss content that moves on its own). There is
//   deliberately no progress line or countdown: Leo read the filling bar as being hurried (0.5.3 feedback);
// - the button top right is 刷新今天 (card 0104, it used to pause): POST /feed/refresh-today starts the refresh,
//   GET polls it, and when it is done the slides and the status line are swapped in from /feed/front, without
//   reloading the page. The button carries how many manual refreshes are left today and greys out at none.
(function () {
  var root = document.getElementById('front');
  if (!root || root.classList.contains('single')) return;
  var track = root.querySelector('.front-track');
  var slides = Array.prototype.slice.call(root.querySelectorAll('.front-slide'));
  var tabs = Array.prototype.slice.call(root.querySelectorAll('[role=tab]'));
  var refreshBtn = root.querySelector('[data-front=refresh]');
  var ROTATE_MS = 10000;
  var canRotate = window.matchMedia('(pointer: fine)').matches && !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var stopped = !canRotate, hovering = false, focused = false, timer = null, current = 0, sized = false;

  var fitting = false;
  function fit() {
    // The track is as tall as the tallest slide. Clearing the height first lets every slide take its
    // natural height for the measurement (slides stretch to the track with min-height: 100%). The
    // observers watch the slides' content boxes, not the slides, so the stretch does not re-trigger fit.
    if (fitting) return;
    fitting = true;
    track.style.height = '';
    var h = 0;
    slides.forEach(function (s) { h = Math.max(h, s.offsetHeight); });
    if (!sized) { track.style.transition = 'none'; sized = true; }
    track.style.height = h + 'px';
    if (track.style.transition === 'none') { void track.offsetHeight; track.style.transition = ''; }
    fitting = false;
  }

  function select(i, fromScroll) {
    current = (i + slides.length) % slides.length;
    tabs.forEach(function (t, k) {
      var on = k === current;
      t.classList.toggle('on', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      t.tabIndex = on ? 0 : -1;
    });
    slides.forEach(function (s, k) { s.setAttribute('aria-hidden', k === current ? 'false' : 'true'); s.inert = k !== current; });
    if (!fromScroll) track.scrollTo({ left: slides[current].offsetLeft - track.offsetLeft, behavior: canRotate ? 'smooth' : 'auto' });
  }

  function render() {
    var running = !stopped && !hovering && !focused && !document.hidden;
    root.classList.toggle('playing', !stopped);
    track.setAttribute('aria-live', stopped ? 'polite' : 'off');
    clearTimeout(timer);
    if (running) timer = setTimeout(function () { select(current + 1); render(); }, ROTATE_MS);
  }

  function stop() { stopped = true; render(); }

  root.addEventListener('click', function (e) {
    var tab = e.target.closest('[role=tab]');
    var btn = e.target.closest('[data-front]');
    if (tab) { select(tabs.indexOf(tab)); stop(); }
    else if (btn && btn.dataset.front === 'prev') { select(current - 1); stop(); }
    else if (btn && btn.dataset.front === 'next') { select(current + 1); stop(); }
    else if (btn && btn.dataset.front === 'refresh') { refreshToday(); }
  });
  root.querySelector('[role=tablist]').addEventListener('keydown', function (e) {
    var step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
    if (e.key === 'Home') step = -current; else if (e.key === 'End') step = slides.length - 1 - current;
    if (step === undefined) return;
    e.preventDefault();
    select(current + step);
    tabs[current].focus();
    stop();
  });
  root.addEventListener('mouseenter', function () { hovering = true; render(); });
  root.addEventListener('mouseleave', function () { hovering = false; render(); });
  root.addEventListener('focusin', function () { focused = true; render(); });
  root.addEventListener('focusout', function (e) { if (!root.contains(e.relatedTarget)) { focused = false; render(); } });

  // A swipe or trackpad scroll moves the track directly: follow it, and treat it as Leo taking over.
  var settle = null;
  track.addEventListener('scroll', function () {
    clearTimeout(settle);
    settle = setTimeout(function () {
      var i = Math.round(track.scrollLeft / Math.max(1, track.clientWidth));
      if (i !== current) { select(i, true); }
    }, 90);
  }, { passive: true });
  ['wheel', 'touchstart'].forEach(function (ev) {
    track.addEventListener(ev, function (e) { if (ev === 'touchstart' || Math.abs(e.deltaX) > Math.abs(e.deltaY)) stop(); }, { passive: true });
  });
  document.addEventListener('visibilitychange', render);
  window.addEventListener('resize', function () { fit(); track.scrollLeft = slides[current].offsetLeft - track.offsetLeft; });
  var ro = window.ResizeObserver ? new ResizeObserver(function () { fit(); }) : null;
  function observe() {
    if (!ro) return;
    ro.disconnect();
    slides.forEach(function (s) { ro.observe(s.querySelector('.front-body, .today') || s); });
  }
  observe();

  // 刷新今天 ------------------------------------------------------------------------------------------
  var busy = false;
  function showState(st) {
    if (!refreshBtn || !st) return;
    var off = !st.running && st.left <= 0;
    refreshBtn.title = st.title;
    refreshBtn.setAttribute('aria-label', st.title);
    // aria-disabled rather than disabled, so the tooltip still says why
    refreshBtn.setAttribute('aria-disabled', off ? 'true' : 'false');
    refreshBtn.style.opacity = off ? '.45' : '';
    refreshBtn.style.cursor = off ? 'not-allowed' : '';
    refreshBtn.classList.toggle('is-busy', !!st.running);
  }
  function getState() {
    return fetch('/feed/refresh-today', { headers: { Accept: 'application/json' } }).then(function (r) { return r.json(); });
  }
  function swapIn() {
    return fetch('/feed/front').then(function (r) { return r.text(); }).then(function (html) {
      var doc = new DOMParser().parseFromString(html, 'text/html');
      slides.forEach(function (s) {
        var fresh = doc.getElementById(s.id);
        if (fresh) s.innerHTML = fresh.innerHTML;
      });
      var status = root.querySelector('#feed-status'), next = doc.getElementById('feed-status');
      if (status && next) {
        status.replaceWith(next);
        if (window.htmx) window.htmx.process(next);
      }
      observe();
      fit();
      select(current, true);
    });
  }
  function poll() {
    setTimeout(function () {
      getState().then(function (st) {
        if (st.running) { showState(st); poll(); return; }
        return swapIn().then(function () { busy = false; showState(st); });
      }).catch(function () { busy = false; refreshBtn.classList.remove('is-busy'); });
    }, 3000);
  }
  function refreshToday() {
    if (!refreshBtn || busy || refreshBtn.getAttribute('aria-disabled') === 'true') return;
    busy = true;
    refreshBtn.classList.add('is-busy');
    fetch('/feed/refresh-today', { method: 'POST', headers: { Accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (st) {
        if (st.result === 'limit') { busy = false; showState(st); return; }
        showState(st);
        poll();
      })
      .catch(function () { busy = false; refreshBtn.classList.remove('is-busy'); });
  }
  if (refreshBtn) {
    getState().then(function (st) {
      showState(st);
      if (st.running && !busy) { busy = true; poll(); }   // a refresh started elsewhere (the 8 o'clock one)
    }).catch(function () {});
  }

  // Measure again once everything that can change line breaks has arrived: the full page load (a cold
  // cache can apply the stylesheet after this script ran) and the web fonts.
  window.addEventListener('load', fit);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(fit);

  fit();
  select(0, true);
  render();
})();

// Keep expanded parent/sub-issue rows while a checkbox refreshes the home agenda.
(function () {
  var openRows = [];
  document.addEventListener('htmx:beforeSwap', function (e) {
    if (e.detail.target && e.detail.target.id === 'home-tasks') {
      openRows = Array.from(document.querySelectorAll('#home-tasks details[data-agenda-key][open]')).map(function (d) { return d.dataset.agendaKey; });
    }
  });
  document.addEventListener('htmx:afterSwap', function (e) {
    if (e.detail.target && e.detail.target.id === 'home-tasks') {
      document.querySelectorAll('#home-tasks details[data-agenda-key]').forEach(function (d) { d.open = openRows.indexOf(d.dataset.agendaKey) >= 0; });
      // The last completed child can move its parent under the completed section.
      var completed = document.querySelector('#home-tasks .agenda-completed');
      if (completed && completed.querySelector('.agenda-parent[open]')) completed.open = true;
    }
  });
})();
