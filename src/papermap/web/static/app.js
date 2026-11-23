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
    const lst = document.getElementById("paper_list");
    if (lst && /^\d+$/.test(lst.dataset.c || "")) fd.append("collection", lst.dataset.c);
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
  document.addEventListener("click", e => { if (e.target.closest("#import_btn")) document.getElementById("import_input").click(); });
  document.addEventListener("change", e => {
    if (e.target.id !== "import_input") return;
    const input = e.target; if (input.files.length) upload(input.files); input.value = "";
  });

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
    if (list && list.dataset.c) p.set("c", list.dataset.c);
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
    let anchor = null;
    list.addEventListener("click", e => {
      const a = e.target.closest(".paper-row"); if (!a) return;
      e.preventDefault();
      const rows = [...list.querySelectorAll(".paper-row")];
      if (e.metaKey || e.ctrlKey) {           // toggle multi-selection
        if (!list.querySelector(".multi")) { const cur = list.querySelector(".selected"); cur && cur.classList.add("multi"); }
        a.classList.toggle("multi"); anchor = a; return;
      }
      if (e.shiftKey && anchor) {             // range selection
        const [i, j] = [rows.indexOf(anchor), rows.indexOf(a)].sort((x, y) => x - y);
        rows.forEach((r, k) => r.classList.toggle("multi", k >= i && k <= j)); return;
      }
      rows.forEach(r => r.classList.remove("multi")); anchor = a;
      openPaper(a.dataset.id);
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

  // ------------------------------------------------------- groups (collections)
  const modal = document.getElementById("modal");
  function dialog(msg, { input = false, value = "", okText = "确定", danger = false } = {}) {
    return new Promise(resolve => {
      const inp = document.getElementById("modal_input"), ok = document.getElementById("modal_ok");
      document.getElementById("modal_msg").textContent = msg;
      inp.hidden = !input; inp.value = value; ok.textContent = okText; ok.className = danger ? "ghost" : "";
      modal.hidden = false; (input ? inp : ok).focus(); if (input) inp.select();
      const done = v => { modal.hidden = true; ok.onclick = cancel.onclick = inp.onkeydown = null; resolve(v); };
      const cancel = document.getElementById("modal_cancel");
      ok.onclick = () => done(input ? inp.value.trim() || null : true);
      cancel.onclick = () => done(input ? null : false);
      inp.onkeydown = e => { if (e.key === "Enter") ok.click(); if (e.key === "Escape") cancel.click(); };
    });
  }
  async function api(url, body = {}) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ csrf, ...body }) });
    const j = await r.json().catch(() => ({ ok: false, message: "HTTP " + r.status }));
    if (!j.ok) toast(j.message || "操作失败");
    return j;
  }
  async function refreshSidebar() {
    const html = await (await fetch(location.href)).text();
    const doc = new DOMParser().parseFromString(html, "text/html");
    document.querySelector(".sidebar").innerHTML = doc.querySelector(".sidebar").innerHTML;
    applyCollapsed();
    if (list) refreshList();
    const panel = document.querySelector(".panel"); if (panel) openPaper(panel.dataset.paper, false);
  }
  const closedKey = "pm_closed_groups";
  const closed = () => { try { return new Set(JSON.parse(localStorage.getItem(closedKey) || "[]")); } catch (e) { return new Set(); } };
  function applyCollapsed() {
    const c = closed();
    document.querySelectorAll("[data-children-of]").forEach(el => el.hidden = c.has(el.dataset.childrenOf));
    document.querySelectorAll(".caret.has").forEach(el => el.classList.toggle("closed", c.has(el.dataset.toggle)));
  }
  applyCollapsed();

  const menu = document.getElementById("ctx_menu");
  function showMenu(x, y, items) {
    menu.innerHTML = "";
    items.forEach(it => {
      if (it === "-") { menu.appendChild(document.createElement("hr")); return; }
      if (it.label && !it.run) { const d = document.createElement("div"); d.className = "label-row"; d.textContent = it.label; menu.appendChild(d); return; }
      const b = document.createElement("button"); b.type = "button"; b.textContent = it.text;
      if (it.danger) b.className = "danger";
      b.onclick = () => { menu.hidden = true; it.run(); };
      menu.appendChild(b);
    });
    menu.hidden = false;
    const r = menu.getBoundingClientRect();
    menu.style.left = Math.min(x, innerWidth - r.width - 8) + "px";
    menu.style.top = Math.min(y, innerHeight - r.height - 8) + "px";
  }
  document.addEventListener("click", e => { if (!menu.contains(e.target)) menu.hidden = true; });
  document.addEventListener("keydown", e => { if (e.key === "Escape") menu.hidden = true; });

  async function newGroup(parentId = null, paperIds = []) {
    const name = await dialog(parentId ? "新建子分组：" : "新建分组：", { input: true, okText: "创建" });
    if (!name) return;
    const j = await api("/api/collections", { name, parent_id: parentId, paper_ids: paperIds });
    if (j.ok) { toast(`已创建「${name}」`); refreshSidebar(); }
  }
  const groupList = () => [...document.querySelectorAll(".coll[data-cid]")].filter(a => /^\d+$/.test(a.dataset.cid))
    .map(a => ({ id: a.dataset.cid, name: a.querySelector(".name").textContent, depth: +getComputedStyle(a).getPropertyValue("--d") || 0 }));
  function selectedPaperIds(row) {
    const multi = list ? [...list.querySelectorAll(".paper-row.multi")].map(r => r.dataset.id) : [];
    return row && multi.includes(row.dataset.id) ? multi : row ? [row.dataset.id] : multi;
  }

  document.addEventListener("click", e => {
    const caret = e.target.closest(".caret.has");
    if (caret) {
      e.preventDefault(); e.stopPropagation();
      const c = closed(); c.has(caret.dataset.toggle) ? c.delete(caret.dataset.toggle) : c.add(caret.dataset.toggle);
      try { localStorage.setItem(closedKey, JSON.stringify([...c])); } catch (err) { /* private mode */ }
      applyCollapsed(); return;
    }
    if (e.target.closest("#coll_new")) newGroup();
    const rm = e.target.closest("[data-remove-group]");
    if (rm) {
      const pid = rm.closest(".panel").dataset.paper;
      api(`/api/collections/${rm.dataset.removeGroup}/remove`, { paper_ids: [pid] }).then(j => j.ok && refreshSidebar());
    }
  }, true);
  document.addEventListener("change", e => {
    const sel = e.target.closest("[data-add-group]"); if (!sel || !sel.value) return;
    const pid = sel.closest(".panel").dataset.paper;
    api(`/api/collections/${sel.value}/add`, { paper_ids: [pid] }).then(j => { if (j.ok) { toast("已加入分组"); refreshSidebar(); } });
  });

  document.addEventListener("contextmenu", e => {
    const g = e.target.closest(".coll[data-cid]");
    if (g && /^\d+$/.test(g.dataset.cid)) {
      e.preventDefault();
      const id = g.dataset.cid, name = g.querySelector(".name").textContent;
      showMenu(e.clientX, e.clientY, [
        { text: "新建子分组…", run: () => newGroup(id) },
        { text: "重命名…", run: async () => {
          const n = await dialog("重命名分组：", { input: true, value: name, okText: "保存" });
          if (n && n !== name && (await api(`/api/collections/${id}/rename`, { name: n })).ok) refreshSidebar();
        } },
        { text: "移到最上层", run: async () => { if ((await api(`/api/collections/${id}/move`, { parent_id: null })).ok) refreshSidebar(); } },
        "-",
        { text: "删除分组…", danger: true, run: async () => {
          if (await dialog(`删除分组「${name}」及其子分组？里面的论文不会被删除，仍在「全部论文」里。`, { okText: "删除", danger: true })) {
            if ((await api(`/api/collections/${id}/delete`)).ok) {
              if (new URLSearchParams(location.search).get("c") === id) location.href = "/"; else refreshSidebar();
            }
          }
        } },
      ]);
      return;
    }
    const row = e.target.closest(".paper-row");
    if (row && list) {
      e.preventDefault();
      if (!row.classList.contains("multi")) list.querySelectorAll(".multi").forEach(r => r.classList.remove("multi"));
      const ids = selectedPaperIds(row), cur = list.dataset.c;
      const groups = groupList();
      const items = [{ label: ids.length > 1 ? `已选 ${ids.length} 篇 · 加入分组` : "加入分组" }];
      groups.forEach(gr => items.push({ text: "　".repeat(gr.depth) + gr.name, run: async () => {
        const j = await api(`/api/collections/${gr.id}/add`, { paper_ids: ids });
        if (j.ok) { toast(`已加入「${gr.name}」`); refreshSidebar(); }
      } }));
      items.push({ text: "＋ 新建分组并加入…", run: () => newGroup(null, ids) });
      if (/^\d+$/.test(cur || "")) {
        items.push("-", { text: "从当前分组移除", run: async () => { if ((await api(`/api/collections/${cur}/remove`, { paper_ids: ids })).ok) refreshSidebar(); } });
      }
      showMenu(e.clientX, e.clientY, items);
    }
  });

  // drag papers onto groups; drag groups onto groups (nest) or onto 全部论文 (top level)
  const PAPERS = "text/x-papermap-papers", GROUP = "text/x-papermap-group";
  document.addEventListener("dragstart", e => {
    const row = e.target.closest && e.target.closest(".paper-row");
    if (row) {
      const ids = selectedPaperIds(row);
      e.dataTransfer.setData(PAPERS, JSON.stringify(ids));
      e.dataTransfer.effectAllowed = "copy";
      const ghost = document.createElement("div"); ghost.className = "drag-ghost";
      ghost.textContent = ids.length > 1 ? `${ids.length} 篇论文` : row.querySelector(".t").textContent.slice(0, 40);
      document.body.appendChild(ghost); e.dataTransfer.setDragImage(ghost, 10, 10); setTimeout(() => ghost.remove(), 0);
      return;
    }
    const g = e.target.closest && e.target.closest(".coll[data-cid]");
    if (g && /^\d+$/.test(g.dataset.cid)) { e.dataTransfer.setData(GROUP, g.dataset.cid); e.dataTransfer.effectAllowed = "move"; }
  });
  const targetOf = e => e.target.closest && e.target.closest(".coll[data-cid], .coll-root");
  document.addEventListener("dragover", e => {
    const t = targetOf(e); if (!t) return;
    const types = [...e.dataTransfer.types];
    const okPapers = types.includes(PAPERS) && /^\d+$/.test(t.dataset.cid || "");
    const okGroup = types.includes(GROUP) && (/^\d+$/.test(t.dataset.cid || "") || t.dataset.cid === "root");
    if (okPapers || okGroup) { e.preventDefault(); t.classList.add("drop-target"); }
  });
  document.addEventListener("dragleave", e => { const t = targetOf(e); t && t.classList.remove("drop-target"); });
  document.addEventListener("drop", async e => {
    const t = targetOf(e); if (!t) return;
    t.classList.remove("drop-target");
    const papers = e.dataTransfer.getData(PAPERS), group = e.dataTransfer.getData(GROUP);
    if (papers && /^\d+$/.test(t.dataset.cid)) {
      e.preventDefault();
      const ids = JSON.parse(papers), name = t.querySelector(".name").textContent;
      const j = await api(`/api/collections/${t.dataset.cid}/add`, { paper_ids: ids });
      if (j.ok) { toast(j.added ? `已加入「${name}」${ids.length > 1 ? `（${j.added} 篇）` : ""}` : `已经在「${name}」里了`); refreshSidebar(); }
    } else if (group) {
      e.preventDefault();
      const parent = t.dataset.cid === "root" ? null : t.dataset.cid;
      if (parent === group) return;
      if ((await api(`/api/collections/${group}/move`, { parent_id: parent })).ok) refreshSidebar();
    }
  });
})();
