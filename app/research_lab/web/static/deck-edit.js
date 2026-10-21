// Card 0196: edit the deck's words in place. Edit turns every [data-edit] piece of slide text editable; Save sends
// only what changed as {"<slide>|<field>": text}; the server keeps the edits and rewrites the pptx.
(function () {
  const deck = document.querySelector('[data-testid="deck-preview"]');
  const editBtn = document.querySelector("[data-deck-edit]");
  if (!deck || !editBtn) return;
  const saveBtn = document.querySelector("[data-deck-save]");
  const cancelBtn = document.querySelector("[data-deck-cancel]");
  const makeForm = document.querySelector("[data-deck-make-form]");
  const download = document.querySelector("[data-deck-download]");
  const fields = () => deck.querySelectorAll("[data-edit]");

  function editing(on) {
    deck.classList.toggle("dk-editing", on);
    editBtn.hidden = on;
    saveBtn.hidden = !on;
    cancelBtn.hidden = !on;
    if (makeForm) makeForm.hidden = on;
    if (download) download.hidden = on;   // while editing, the file on offer is still the old one
    fields().forEach((el) => {
      if (on) { el.dataset.orig = el.textContent; el.contentEditable = "plaintext-only"; }
      else el.removeAttribute("contenteditable");
    });
  }

  editBtn.addEventListener("click", () => editing(true));
  cancelBtn.addEventListener("click", () => {
    fields().forEach((el) => { if (el.dataset.orig !== undefined) el.textContent = el.dataset.orig; });
    editing(false);
  });
  // one paragraph per field: Enter ends the edit instead of adding a line
  deck.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.closest && e.target.closest("[data-edit]")) { e.preventDefault(); e.target.blur(); }
  });
  saveBtn.addEventListener("click", async () => {
    const changes = {};
    fields().forEach((el) => {
      const text = el.textContent.replace(/\s+/g, " ").trim();
      if (text !== (el.dataset.orig || "").replace(/\s+/g, " ").trim()) changes[el.dataset.edit] = text;
    });
    if (!Object.keys(changes).length) { editing(false); return; }
    saveBtn.disabled = true;
    try {
      const r = await fetch(location.pathname.replace(/\/$/, "") + "/edits", {
        method: "POST", headers: { "Content-Type": "application/json", "HX-Request": "true" },
        body: JSON.stringify({ changes, at: editBtn.dataset.at || "" }),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      const { saved } = await r.json();
      location.href = location.pathname + "?msg=" + encodeURIComponent("已保存 " + saved + " 处改动");
    } catch (err) {
      saveBtn.disabled = false;
      const say = window.rlToast || window.alert;
      say("没有保存：" + err.message);
    }
  });
})();
