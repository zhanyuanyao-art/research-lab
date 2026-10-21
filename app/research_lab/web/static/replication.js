// Card 0196: a picture on the replication page opens large in a dialog, beside a note on how to read it.
(function () {
  const dialog = document.getElementById("rp-zoom");
  if (!dialog) return;
  const img = dialog.querySelector("img"), title = dialog.querySelector("aside h3"), note = dialog.querySelector("aside p");
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-zoom]");
    if (!btn) return;
    img.src = btn.dataset.zoom;
    img.alt = btn.dataset.label || "";
    title.textContent = btn.dataset.label || "";
    note.textContent = btn.dataset.note || "";
    dialog.showModal();
  });
  // a click on the backdrop (outside the sheet) closes it
  dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
})();

// Card 0203: the 一键跑完 box. Its clock ticks every second between the 3 s polls, the box stays open across them (and
// across the reload at the end), and the pulse and shimmer keep their phase instead of restarting on every swap.
// Card 0204: the same for every .jobline box with an id, such as Preinstall's on 设置 (#r-env-box).
(function () {
  const took = (s) => {
    s = Math.max(0, Math.floor(s));
    if (s < 60) return `${s} 秒`;
    if (s < 3600) return s % 60 ? `${Math.floor(s / 60)} 分 ${s % 60} 秒` : `${Math.floor(s / 60)} 分`;
    return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`;
  };
  const key = (box) => (box.dataset.plan ? `rl.runall.open.${box.dataset.plan}` : `rl.jobline.open.${box.id}`);
  const store = {
    get: (k) => { try { return sessionStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { sessionStorage.setItem(k, v); } catch { /* private window: the box just starts closed */ } },
  };
  function tick() {
    const now = Date.now() / 1000;
    document.querySelectorAll(".jobline [data-since]").forEach((el) => {
      const step = el.dataset.stepSince ? new Date(el.dataset.stepSince.replace(" ", "T")).getTime() / 1000 : NaN;
      el.textContent = took(now - (Number.isFinite(step) ? step : Number(el.dataset.since)));
    });
  }
  function setup(root) {
    if (!root || !root.querySelectorAll) return;
    const boxes = [...(root.matches && root.matches("details.jobline[id]") ? [root] : []),
                   ...root.querySelectorAll("details.jobline[id]")];
    const t = performance.now();
    boxes.forEach((box) => {
      if (store.get(key(box)) === "1") box.open = true;
      box.querySelectorAll(".jobline-dot").forEach((el) => { el.style.animationDelay = `-${t % 1200}ms`; });
      box.querySelectorAll(".jobline-text").forEach((el) => { el.style.animationDelay = `-${t % 2400}ms`; });
    });
    if (boxes.length) tick();
  }
  document.addEventListener("toggle", (e) => {
    const box = e.target;
    if (box.matches && box.matches("details.jobline[id]")) store.set(key(box), box.open ? "1" : "0");
  }, true);
  document.addEventListener("htmx:load", (e) => setup(e.detail.elt));
  setup(document);
  setInterval(tick, 1000);
})();
