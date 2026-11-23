// PaperMap desktop UI: import (drag-and-drop / picker), split view, tabs, notes, live refresh.
(function () {
  const csrf = document.body.dataset.csrf;
  const toastEl = document.getElementById("toast");
  let toastTimer;
  function toast(msg, ms = 3500) {
    toastEl.textContent = msg; toastEl.classList.add("show");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => toastEl.classList.remove("show"), ms);
  }
  window.pmToast = toast;

  // ---------------------------------------------------------------- import
  async function upload(files) {
    const pdfs = [...files].filter(f => /\.pdf$/i.test(f.name));
    if (!pdfs.length) { toast("只能导入 PDF 文件"); return; }
    const fd = new FormData(); fd.append("csrf", csrf);
    pdfs.forEach(f => fd.append("files", f, f.name));
    toast(`正在导入 ${pdfs.length} 个 PDF…`, 60000);
    try {
      const r = await fetch("/api/import", { method: "POST", body: fd });
      const j = await r.json();
      toast(`已导入 ${j.count} 篇，正在识别 DOI 和生成卡片…`);
      if (!location.pathname.startsWith("/") || location.pathname !== "/") location.href = "/";
      else refreshList();
      startPolling();
    } catch (e) { toast("导入失败：" + e); }
  }
  const input = document.getElementById("import_input");
  document.getElementById("import_btn").addEventListener("click", () => input.click());
  input.addEventListener("change", () => { if (input.files.length) upload(input.files); input.value = ""; });

  const overlay = document.getElementById("drop_overlay");
  let depth = 0;
  const hasFiles = e => e.dataTransfer && [...e.dataTransfer.types].includes("Files");
  window.addEventListener("dragenter", e => { if (!hasFiles(e)) return; e.preventDefault(); depth++; overlay.hidden = false; });
  window.addEventListener("dragover", e => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener("dragleave", e => { if (!hasFiles(e)) return; if (--depth <= 0) { depth = 0; overlay.hidden = true; } });
  window.addEventListener("drop", e => {
    if (!hasFiles(e)) return; e.preventDefault(); depth = 0; overlay.hidden = true;
    upload(e.dataTransfer.files);
  });

  // ------------------------------------------------------------ split view
  const list = document.getElementById("paper_list");
  const detail = document.getElementById("detail");
  const q = document.getElementById("q");
  const statusSel = document.getElementById("status_filter");

  function listParams() {
    const p = new URLSearchParams();
    if (q && q.value.trim()) p.set("q", q.value.trim());
    if (statusSel && statusSel.value) p.set("status", statusSel.value);
    if (list && list.dataset.selected) p.set("selected", list.dataset.selected);
    return p;
  }
  async function refreshList() {
    if (!list) return;
    const r = await fetch("/papers/list?" + listParams());
    list.innerHTML = await r.text();
  }
  async function openPaper(id, push = true) {
    if (!detail) { location.href = "/?id=" + id; return; }
    list.dataset.selected = id;
    list.querySelectorAll(".paper-row").forEach(a => a.classList.toggle("selected", a.dataset.id == id));
    const r = await fetch(`/paper/${id}/panel`);
    if (!r.ok) return;
    detail.innerHTML = await r.text();
    detail.scrollTop = 0;
    initPanel();
    if (push) history.replaceState(null, "", "/?" + new URLSearchParams({ ...Object.fromEntries(listParams()), id }));
  }
  if (list) {
    list.addEventListener("click", e => {
      const a = e.target.closest(".paper-row"); if (!a) return;
      e.preventDefault(); openPaper(a.dataset.id);
    });
    let t; q && q.addEventListener("input", () => { clearTimeout(t); t = setTimeout(refreshList, 250); });
    statusSel && statusSel.addEventListener("change", refreshList);
    document.addEventListener("keydown", e => {
      if (!["ArrowDown", "ArrowUp"].includes(e.key) || ["TEXTAREA", "INPUT", "SELECT"].includes(document.activeElement.tagName)) return;
      const rows = [...list.querySelectorAll(".paper-row")];
      const i = rows.findIndex(r => r.classList.contains("selected"));
      const next = rows[Math.max(0, Math.min(rows.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))];
      if (next) { e.preventDefault(); openPaper(next.dataset.id); next.scrollIntoView({ block: "nearest" }); }
    });
  }

  // ----------------------------------------------------------------- panel
  function initPanel() {
    const panel = document.querySelector(".panel"); if (!panel) return;
    const id = panel.dataset.paper;
    panel.querySelectorAll(".tab").forEach(tab => tab.addEventListener("click", () => {
      panel.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t === tab));
      panel.querySelectorAll(".tab-body").forEach(b => b.hidden = b.dataset.body !== tab.dataset.tab);
      const frame = panel.querySelector(`[data-body="${tab.dataset.tab}"] iframe[data-src]`);
      if (frame && !frame.src) frame.src = frame.dataset.src;
    }));
    const reveal = panel.querySelector('[data-action="reveal"]');
    reveal && reveal.addEventListener("click", () =>
      fetch(`/paper/${id}/reveal`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ csrf }) }));
    const notes = panel.querySelector("textarea.notes");
    if (notes) {
      const status = panel.querySelector("[data-notes-status]");
      let timer, saved = notes.value;
      const save = async () => {
        if (notes.value === saved) return;
        const text = notes.value;
        const r = await fetch(`/paper/${id}/notes`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ csrf, text }) });
        if (r.ok) { saved = text; status.textContent = "已保存 " + new Date().toLocaleTimeString(); }
        else status.textContent = "保存失败";
      };
      notes.addEventListener("input", () => { status.textContent = "编辑中…"; clearTimeout(timer); timer = setTimeout(save, 800); });
      notes.addEventListener("keydown", e => { if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); } });
      notes.addEventListener("blur", save);
    }
  }
  initPanel();

  // ---------------------------------------------------------- live refresh
  let polling = null, lastEvent = null;
  async function poll() {
    try {
      const s = await (await fetch("/api/state")).json();
      if (lastEvent !== null && s.last_event !== lastEvent) {
        await refreshList();
        const panel = document.querySelector(".panel");
        const typing = document.activeElement && document.activeElement.classList.contains("notes");
        if (panel && !typing) openPaper(panel.dataset.paper, false);
      }
      lastEvent = s.last_event;
      const badge = document.querySelector('.nav a[href="/review"] .badge');
      if (s.review && !badge) document.querySelector('.nav a[href="/review"]').insertAdjacentHTML("beforeend", `<b class="badge">${s.review}</b>`);
      else if (badge) { if (s.review) badge.textContent = s.review; else badge.remove(); }
      if (!s.pending && !s.busy && polling) { clearInterval(polling); polling = null; setTimeout(startPolling, 15000); }
    } catch (e) { /* server restarting */ }
  }
  function startPolling() { if (!polling) { poll(); polling = setInterval(poll, 2500); } }
  if (list) startPolling();

  // ------------------------------------------- native folder picker (app only)
  window.addEventListener("pywebviewready", () => document.body.classList.add("native"));
  document.querySelectorAll("[data-pick-folder]").forEach(btn => btn.addEventListener("click", async () => {
    if (!window.pywebview) { toast("在 PaperMap 应用里可以直接选择文件夹；浏览器里请手动填写路径"); return; }
    const path = await window.pywebview.api.pick_folder();
    if (path) {
      const ta = document.getElementById(btn.dataset.pickFolder);
      ta.value = (ta.value.trim() ? ta.value.trim() + "\n" : "") + path;
    }
  }));
})();
