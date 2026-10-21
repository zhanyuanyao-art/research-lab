/* The reader (card 0129, docs/design/LIBRARY-UI.md §3.2): the side panel's 信息 / 概要 tabs (#info, #summary, #marks
   in the URL; 信息 by default, card 0200), Copy on the reference, the side panel toggle (\), the previous / next paper ([ and ]), the phone's 原文 · 概要 · 信息 rail,
   page links from the summary, ?page= and ?find= (search from that page and highlight), and the PDF itself.
   The library owns archival actions; this module owns only the reading surface. */
const reader = document.getElementById('reader');
const page = document.getElementById('read-page');
const byId = id => document.getElementById(id);
const labels = JSON.parse(byId('reading-page-labels')?.textContent || '[]');
const bare = label => String(label).replace(/^pp?\.\s*/i, '').replace(/[\[\]]/g, '').trim().toLowerCase();
const params = new URLSearchParams(location.search);
const store = {
  get: key => { try { return localStorage.getItem(key); } catch { return null; } },
  set: (key, value) => { try { localStorage.setItem(key, value); } catch { /* private window: nothing to remember */ } },
};
const phone = () => matchMedia('(max-width: 720px)').matches;
let viewer, links, bus;
let opened = false;   // the PDF has been asked for (startPDF)

// 信息 · 概要 in the side panel; on a phone the rail also has 原文.
function showTab(name, push) {
  if (!['summary', 'info'].includes(name)) return;
  document.querySelectorAll('#side-tabs [data-side-tab]').forEach(a => a.classList.toggle('on', a.dataset.sideTab === name));
  byId('side-summary').hidden = name !== 'summary';
  byId('side-info').hidden = name !== 'info';
  if (phone()) setMobile(name === 'info' ? 'info' : 'sum');
  if (push) history.replaceState(null, '', location.pathname + location.search + '#' + name);
}
function setMobile(tab) {
  page.classList.remove('tab-doc', 'tab-sum', 'tab-info');
  page.classList.add('tab-' + tab);
  document.querySelectorAll('.read-mtabs [data-mtab]').forEach(b => b.classList.toggle('on', b.dataset.mtab === tab));
  if (tab === 'doc') startPDF();
}
function showInfo(focus) {
  setSide(true);
  showTab('info', false);
  const target = focus && byId(focus);
  if (target) { if (target.tagName === 'DETAILS') target.open = true; target.scrollIntoView({block: 'start'}); }
}
// The side panel: open by default from 1200px, closed below it (it opens over the PDF there); a choice is kept.
function setSide(open, remember) {
  page.classList.toggle('side-closed', !open);
  const toggle = byId('side-toggle');
  if (toggle) {
    toggle.setAttribute('aria-pressed', String(open));
    toggle.setAttribute('aria-label', open ? '收起右栏' : '展开右栏');
    toggle.dataset.tip = (open ? '收起右栏' : '展开右栏') + '  \\';
  }
  if (remember) store.set('rl.reader.side', open ? 'open' : 'closed');
}
const savedSide = store.get('rl.reader.side');
setSide(savedSide ? savedSide === 'open' : innerWidth >= 1200);
byId('side-toggle')?.addEventListener('click', () => setSide(page.classList.contains('side-closed'), true));

// The Mac window's web view may refuse the async clipboard; a selected textarea and execCommand still copy there.
function copyText(text) {
  if (!text) return;
  const done = () => window.rlToast?.('已复制引用');
  const fallback = () => {
    const area = Object.assign(document.createElement('textarea'), {value: text});
    area.style.cssText = 'position:fixed;opacity:0';
    document.body.append(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    ok ? done() : window.rlToast?.('没有复制成功，请手动选中');
  };
  if (navigator.clipboard?.writeText) navigator.clipboard.writeText(text).then(done, fallback); else fallback();
}

function jump(index) {
  if (viewer) viewer.currentPageNumber = index + 1;
  byId(`p-${index}`)?.scrollIntoView({block: 'start'});
  if (phone()) { setMobile('doc'); reader.scrollIntoView({block: 'start'}); }
}
document.addEventListener('click', event => {
  const tab = event.target.closest('[data-side-tab]');
  if (tab) { event.preventDefault(); showTab(tab.dataset.sideTab, true); return; }
  const mtab = event.target.closest('[data-mtab]');
  if (mtab) {
    store.set('rl.reader.mobile', mtab.dataset.mtab === 'doc' ? 'doc' : 'sum');
    if (mtab.dataset.mtab === 'doc') setMobile('doc'); else showTab(mtab.dataset.mtab === 'info' ? 'info' : 'summary', true);
    return;
  }
  const copy = event.target.closest('[data-copy-cite]');
  if (copy) { copyText(byId(copy.dataset.copyCite)?.textContent.trim() || ''); return; }
  const info = event.target.closest('[data-reader-info]');
  if (info) { event.preventDefault(); showInfo(info.dataset.readerInfo); return; }
  const add = event.target.closest('[data-add-project-open]');
  if (add) {
    showTab('summary', false);
    const menu = document.querySelector('[data-add-project]');
    if (menu) { menu.open = true; menu.scrollIntoView({block: 'nearest'}); }
    return;
  }
  if (event.target.closest('[data-regenerate]')) { const menu = event.target.closest('details.menu'); if (menu) menu.open = false; showTab('summary', false); }
  const link = event.target.closest('[data-jump], [data-page-label]');
  if (!link) return;
  let index = link.hasAttribute('data-jump') ? Number(link.dataset.jump) : labels.findIndex(label => bare(label) === bare(link.dataset.pageLabel));
  if (index < 0 && /^\d+$/.test(link.dataset.pageLabel || '')) {
    const physical = Number(link.dataset.pageLabel);
    if (physical >= 1 && physical <= labels.length) index = physical - 1;
  }
  if (index < 0) { event.preventDefault(); window.rlToast?.('未找到对应页码'); return; }
  if (!viewer?.pagesCount && reader.dataset.view !== 'trans') return;
  event.preventDefault();
  jump(index);
});
// [ and ] walk the list the paper was opened from; \ shows or hides the side panel.
document.addEventListener('keydown', event => {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.target.closest('input, textarea, select, [contenteditable]')) return;
  if (event.key === '[' || event.key === ']') {
    const a = document.querySelector(`.read-head a[rel="${event.key === '[' ? 'prev' : 'next'}"]`);
    if (a) { event.preventDefault(); location.href = a.href; }
  } else if (event.key === '\\') {
    event.preventDefault();
    setSide(page.classList.contains('side-closed'), true);
  }
});
// Polling must not collapse the section the researcher is reviewing.
let expanded = [];
document.addEventListener('htmx:beforeSwap', event => {
  if (event.detail.target?.id === 'summary-card') expanded = [...document.querySelectorAll('[data-summary-section][open]')].map(el => el.dataset.summarySection);
});
document.addEventListener('htmx:afterSwap', event => {
  if (event.detail.target?.id === 'summary-card') document.querySelectorAll('[data-summary-section]').forEach(el => { if (expanded.includes(el.dataset.summarySection)) el.open = true; });
});
const hash = location.hash.slice(1);
if (hash === 'info' || hash === 'summary') showTab(hash, false);
else if (hash === 'marks') showInfo('side-marks');
else if (phone()) { if (store.get('rl.reader.mobile') === 'doc') setMobile('doc'); else showTab('info', false); }
if (reader.dataset.view === 'trans') jump(Number(reader.dataset.initialPage));

async function openPDF() {
  if (!('CSSScopeRule' in window)) throw new Error('PDF viewer requires scoped styles');
  const pdfjs = await import('./pdfjs/build/pdf.mjs');
  const {PDFViewer, PDFLinkService, PDFFindController, EventBus, LinkTarget} = await import('./pdfjs/web/pdf_viewer.mjs');
  pdfjs.GlobalWorkerOptions.workerSrc = new URL('./pdfjs/build/pdf.worker.mjs', import.meta.url).href;
  const base = new URL('./pdfjs/', import.meta.url).href;
  bus = new EventBus();
  links = new PDFLinkService({eventBus: bus, externalLinkTarget: LinkTarget.BLANK, externalLinkRel: 'noopener noreferrer'});
  const find = new PDFFindController({eventBus: bus, linkService: links});
  viewer = new PDFViewer({container: byId('pdf-container'), viewer: byId('pdf-viewer'), eventBus: bus,
    linkService: links, findController: find, annotationEditorMode: pdfjs.AnnotationEditorType.DISABLE,
    annotationMode: pdfjs.AnnotationMode.ENABLE, enableScripting: false, enableHighlightFloatingButton: false,
    enableSelectionRendering: false,
    imageResourcesPath: `${base}web/images/`, maxCanvasPixels: 8388608});
  links.setViewer(viewer);
  const task = pdfjs.getDocument({url: reader.dataset.pdfUrl, cMapUrl: `${base}cmaps/`, cMapPacked: true,
    standardFontDataUrl: `${base}standard_fonts/`, wasmUrl: `${base}wasm/`, isEvalSupported: false,
    enableXfa: false});
  task.onPassword = () => { task.destroy(); failure('此 PDF 需要密码，请用系统阅读器打开。'); };
  const doc = await task.promise;
  links.setDocument(doc);
  bus.on('pagesinit', () => {
    viewer.currentScaleValue = 'auto';   // page width, but never past 125%: a 1920px window would blow pages up
    viewer.currentPageNumber = Math.min(doc.numPages, Number(reader.dataset.initialPage) + 1);
    document.querySelectorAll('.read-tools [disabled]').forEach(el => { el.disabled = false; });
    byId('pdf-page').max = doc.numPages;
    byId('pdf-total').textContent = `/ ${doc.numPages}`;
    updatePage();
    const wanted = params.get('find');
    if (wanted) {   // ?page=7&find=alliance (search results): from that page, the first hit, every hit highlighted
      const bar = byId('pdf-findbar'); bar.hidden = false;
      byId('pdf-find-toggle').setAttribute('aria-expanded', 'true');
      byId('paper-find').value = wanted;
      search();
    }
  });
  bus.on('pagerendered', event => {
    byId('pdf-loading').hidden = true;
    if (event.error) failure('此页无法显示，可打开原文件阅读。');
  });
  const updatePage = () => {
    byId('pdf-page').value = viewer.currentPageNumber;
    byId('pdf-prev').disabled = viewer.currentPageNumber <= 1;
    byId('pdf-next').disabled = viewer.currentPageNumber >= doc.numPages;
    byId('pdf-page').title = `第 ${labels[viewer.currentPageNumber - 1] || viewer.currentPageNumber} 页`;
  };
  bus.on('pagechanging', updatePage);
  const matchCount = ({matchesCount}) => { byId('pdf-find-count').textContent = matchesCount?.total ? `${matchesCount.current} / ${matchesCount.total}` : '无匹配'; };
  bus.on('updatefindmatchescount', matchCount);
  bus.on('updatefindcontrolstate', event => {
    if (event.state === 3) byId('pdf-find-count').textContent = '查找中…';
    else matchCount(event);
  });
  const search = (type = '', previous = false) => bus.dispatch('find', {source: reader, type, query: byId('paper-find').value,
    phraseSearch: true, caseSensitive: false, entireWord: false, highlightAll: true, findPrevious: previous, matchDiacritics: false});
  viewer.setDocument(doc);
  const pageLabels = await doc.getPageLabels();
  if (pageLabels) viewer.setPageLabels(pageLabels);
  byId('pdf-prev').onclick = () => { viewer.currentPageNumber--; };
  byId('pdf-next').onclick = () => { viewer.currentPageNumber++; };
  byId('pdf-page').onchange = event => {
    const page = Number(event.target.value);
    if (Number.isInteger(page) && page >= 1 && page <= doc.numPages) viewer.currentPageNumber = page;
    updatePage();
  };
  byId('pdf-zoom').onchange = event => { viewer.currentScaleValue = event.target.value; };
  new ResizeObserver(() => {
    if (['auto', 'page-width', 'page-fit'].includes(viewer.currentScaleValue)) viewer.currentScaleValue = viewer.currentScaleValue;
    viewer.update();
  }).observe(byId('pdf-container'));
  byId('paper-find').oninput = () => search();
  byId('pdf-findbar').onsubmit = event => { event.preventDefault(); search('again'); };
  byId('pdf-find-prev').onclick = () => search('again', true);
  const toggleFind = () => {
    const bar = byId('pdf-findbar'); bar.hidden = !bar.hidden;
    byId('pdf-find-toggle').setAttribute('aria-expanded', String(!bar.hidden));
    if (!bar.hidden) byId('paper-find').focus();
  };
  byId('pdf-find-toggle').onclick = toggleFind;
  byId('pdf-findbar').onkeydown = event => { if (event.key === 'Escape') { toggleFind(); byId('pdf-find-toggle').focus(); } };
  byId('pdf-container').onkeydown = event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'f') {
      event.preventDefault(); if (byId('pdf-findbar').hidden) toggleFind(); else byId('paper-find').focus();
    }
  };
  const outlinePanel = byId('pdf-outline');
  byId('pdf-outline-toggle').onclick = () => {
    outlinePanel.hidden = !outlinePanel.hidden;
    byId('pdf-outline-toggle').setAttribute('aria-expanded', String(!outlinePanel.hidden));
  };
  const outline = await doc.getOutline();
  function outlineList(items) {
    const ul = document.createElement('ul');
    for (const item of items) {
      const li = document.createElement('li');
      const button = document.createElement('button');
      button.className = 'btn ghost small'; button.textContent = item.title;
      button.disabled = !item.dest;
      button.onclick = () => links.goToDestination(item.dest);
      li.append(button);
      if (item.items?.length) li.append(outlineList(item.items));
      ul.append(li);
    }
    return ul;
  }
  if (outline?.length) outlinePanel.append(outlineList(outline));
  else { const empty = document.createElement('p'); empty.className = 'muted small'; empty.textContent = '此 PDF 没有目录。'; outlinePanel.append(empty); }
  window.addEventListener('pagehide', () => { task.destroy(); }, {once: true});
}
function failure(message) {
  byId('pdf-loading').hidden = true;
  byId('pdf-error').hidden = false;
  byId('pdf-error-text').textContent = message;
}
// On a phone the PDF opens when 原文 is first shown: laid out while hidden, PDF.js cannot place its pages.
function startPDF() {
  if (opened || !reader?.dataset.pdfUrl || reader.dataset.view === 'trans') return;
  opened = true;
  openPDF().catch(() => failure('PDF 暂时无法显示，请重试或打开原文件。'));
}
if (!phone() || page.classList.contains('tab-doc')) startPDF();

// 议题 (card 0131, LIBRARY-UI.md §3.6): Link issue opens a picker of issues by key, title or 课题; ↑↓ move, ↵ links or,
// on one already linked, unlinks; Esc closes (issues.js closes .issue-popover on Esc and outside clicks). A pick or an
// Unlink sends that issue's revision and then redraws #paper-props from the page, so the next one carries fresh ones.
let issueAsk = 0;
async function issueResults(input) {
  const box = input.form.querySelector('[data-paper-issue-results]');
  const ask = ++issueAsk;
  try {
    const response = await fetch(`${input.dataset.url}?q=${encodeURIComponent(input.value)}`, {headers: {'HX-Request': 'true'}});
    if (ask === issueAsk && response.ok) { box.innerHTML = await response.text(); input.removeAttribute('aria-activedescendant'); }
  } catch { if (ask === issueAsk) box.innerHTML = '<p class="issue-pick-empty">连不上应用，稍后再试。</p>'; }
}
async function redrawProps() {
  const html = await (await fetch(location.href)).text();
  const fresh = new DOMParser().parseFromString(html, 'text/html').getElementById('paper-props');
  if (fresh) byId('paper-props')?.replaceWith(fresh);
}
let issueTimer = 0;
document.addEventListener('toggle', event => {
  const menu = event.target;
  if (!menu.matches?.('[data-paper-issue-picker]') || !menu.open) return;
  const input = menu.querySelector('[data-paper-issue-search]');
  issueResults(input);
  setTimeout(() => input.focus(), 0);
}, true);
document.addEventListener('input', event => {
  if (!event.target.matches?.('[data-paper-issue-search]')) return;
  clearTimeout(issueTimer);
  const input = event.target;
  issueTimer = setTimeout(() => issueResults(input), 150);
});
document.addEventListener('keydown', event => {
  if (!event.target.matches?.('[data-paper-issue-search]') || event.isComposing) return;
  const picks = [...event.target.form.querySelectorAll('.issue-pick')];
  const current = picks.findIndex(b => b.classList.contains('is-active'));
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    if (!picks.length) return;
    if (picks[current]) { picks[current].classList.remove('is-active'); picks[current].setAttribute('aria-selected', 'false'); }
    const next = picks[(current + (event.key === 'ArrowDown' ? 1 : -1) + picks.length) % picks.length];
    next.classList.add('is-active');
    next.setAttribute('aria-selected', 'true');
    event.target.setAttribute('aria-activedescendant', next.id);     // a screen reader follows the highlight
    next.scrollIntoView({block: 'nearest'});
  } else if (event.key === 'Enter') {
    event.preventDefault();
    const pick = picks[current] || (picks.length === 1 ? picks[0] : null);
    if (pick) event.target.form.requestSubmit(pick);
  }
});
document.addEventListener('submit', async event => {
  const form = event.target.closest?.('[data-paper-issue-form]');
  if (!form) return;
  event.preventDefault();
  const body = new FormData(form, event.submitter);
  try {
    const response = await fetch(form.action, {method: 'POST', body, headers: {Accept: 'application/json', 'HX-Request': 'true'}});
    const result = await response.json().catch(() => ({}));
    if (!response.ok) { window.rlToast?.(result.error || '没有改成，请刷新后再试。'); return; }
    window.rlToast?.(result.linked ? '已关联议题' : '已取消关联');
    await redrawProps();
  } catch { window.rlToast?.('连不上应用，稍后再试。'); }
});
