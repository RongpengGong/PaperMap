// PaperMap desktop UI: import (drag-and-drop / picker), split view, tabs, notes, live refresh.
(function () {
  const csrf = document.body.dataset.csrf;
  const toastEl = document.getElementById("toast");
  const settingsPage = location.pathname === "/settings" || location.pathname === "/zotero" || location.pathname === "/extensions/guide";
  const sidebar = document.querySelector(".sidebar, .settings-sidebar");
  const sidebarScrollKey = settingsPage ? "papermap.settingsSidebarScroll" : "papermap.sidebarScroll";
  const libraryPages = new Set(["/", "/search", "/qa", "/evidence", "/map", "/fields", "/handbooks", "/concepts",
    "/figures", "/resources", "/citations", "/review", "/skills", "/taxonomy", "/costs", "/events", "/compare"]);
  function libraryLocation(value) {
    if (!value) return null;
    try {
      const url = new URL(value, location.origin);
      if (url.origin !== location.origin || url.username || url.password) return null;
      // Older versions saved the embedded preview as the library return URL.
      const reader = url.pathname.match(/^\/reader\/(\d+)$/);
      if (reader && Number(url.searchParams.get("embed"))) return "/?id=" + reader[1];
      if (!libraryPages.has(url.pathname) && !/^\/(?:field|handbook|concept)\/[a-z0-9-]+$/.test(url.pathname)
          && !/^\/(?:paper|reader)\/\d+$/.test(url.pathname)) return null;
      return url.pathname + url.search + url.hash;
    } catch (_) { return null; }
  }
  function rememberLibraryLocation() {
    if (window.self !== window.top || settingsPage || document.body.classList.contains("bare")) return;
    const url = libraryLocation(location.href);
    try { if (url) sessionStorage.setItem("papermap.libraryReturn", url); }
    catch (_) { /* The default return link remains available. */ }
  }
  rememberLibraryLocation();
  window.addEventListener("pagehide", rememberLibraryLocation);
  let libraryReturn = "/";
  try { libraryReturn = libraryLocation(sessionStorage.getItem("papermap.libraryReturn") || "") || libraryLocation(document.referrer) || "/"; }
  catch (_) { libraryReturn = libraryLocation(document.referrer) || "/"; }
  document.querySelectorAll("[data-settings-back]").forEach(link => { link.href = libraryReturn; });
  if (sidebar) {
    // Navigating between server-rendered pages replaces the sidebar DOM.
    // Restore this window's scroll position so its lower items stay in reach.
    window.addEventListener("pagehide", () => {
      try { sessionStorage.setItem(sidebarScrollKey, String(sidebar.scrollTop)); }
      catch (_) { /* Storage may be disabled in a private webview. */ }
    });
    window.addEventListener("pageshow", event => {
      if (event.persisted) return;
      try {
        const position = sessionStorage.getItem(sidebarScrollKey);
        if (position !== null) { sidebar.scrollTop = Number(position); return; }
      } catch (_) { /* Keep the active navigation link visible. */ }
      sidebar.querySelector("a.active")?.scrollIntoView({ block: "nearest" });
    });
  }
  let toastTimer;
  function toast(msg, ms = 3500) {
    if (!toastEl) return;
    toastEl.textContent = t(String(msg)); toastEl.classList.add("show");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => toastEl.classList.remove("show"), ms);
  }
  window.pmToast = toast;

  // ---------------------------------------------------------------- import
  async function upload(files) {
    const pdfs = [...files].filter(f => /\.pdf$/i.test(f.name));
    if (!pdfs.length) { toast(t("只能导入 PDF 文件")); return; }
    const fd = new FormData(); fd.append("csrf", csrf);
    const lst = document.getElementById("paper_list");
    if (lst && /^\d+$/.test(lst.dataset.c || "")) fd.append("collection", lst.dataset.c);
    pdfs.forEach(f => fd.append("files", f, f.name));
    toast(`${t("正在导入")} ${pdfs.length} ${t("个 PDF…")}`, 60000);
    try {
      const r = await fetch("/api/import", { method: "POST", body: fd });
      const j = await r.json();
      toast(`${t("已导入")} ${j.count} ${t("篇，正在识别 DOI 和生成卡片…")}`);
      if (!location.pathname.startsWith("/") || location.pathname !== "/") location.href = "/";
      else refreshList();
      startPolling();
    } catch (e) { toast(t("导入失败：") + e); }
  }
  document.addEventListener("click", e => { if (e.target.closest("#import_btn")) document.getElementById("import_input")?.click(); });
  document.addEventListener("change", e => {
    if (e.target.id !== "import_input") return;
    const input = e.target; if (input.files.length) upload(input.files); input.value = "";
  });

  const overlay = document.getElementById("drop_overlay");
  let depth = 0;
  // a note editor takes its own drops (pictures); everywhere else a dropped file is a PDF to import
  const hasFiles = e => e.dataTransfer && [...e.dataTransfer.types].includes("Files")
    && !(e.target && e.target.closest && e.target.closest(".kb-user.editing"));
  window.addEventListener("dragenter", e => { if (!hasFiles(e)) return; e.preventDefault(); depth++; if (overlay) overlay.hidden = false; });
  window.addEventListener("dragover", e => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener("dragleave", e => { if (!hasFiles(e)) return; if (--depth <= 0) { depth = 0; if (overlay) overlay.hidden = true; } });
  window.addEventListener("drop", e => {
    if (!hasFiles(e)) return; e.preventDefault(); depth = 0; if (overlay) overlay.hidden = true;
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
    const html = await r.text();
    const old = detail.querySelector(".panel");
    if (old && old.dataset.paper == id && old.querySelector(".pdf-col iframe[src]")) {
      // same paper refreshed (e.g. a job finished): keep the PDF where the reader is
      const doc = new DOMParser().parseFromString(html, "text/html");
      const typing = document.activeElement && document.activeElement.classList.contains("notes");
      old.querySelector(".panel-head").replaceWith(doc.querySelector(".panel-head"));
      if (!typing) old.querySelector(".side-col").replaceWith(doc.querySelector(".side-col"));
    } else {
      detail.innerHTML = html;
    }
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
  const pref = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } };
  if (document.querySelector(".panel") && pref("pm_list_collapsed") === "1") {
    document.body.classList.add("list-collapsed");
  }
  document.querySelector("[data-show-library-list]")?.addEventListener("click", () => pref("pm_list_collapsed", "0"));

  function initPanel() {
    const panel = document.querySelector(".panel"); if (!panel) return;
    const id = panel.dataset.paper;
    if (window.pmPanelEditors) window.pmPanelEditors(panel);
    // right-hand tabs; the last one used is remembered across papers
    const showTab = name => {
      const tab = panel.querySelector(`.tab[data-tab="${name}"]`) || panel.querySelector(".tab");
      if (!tab) return;
      panel.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t === tab));
      panel.querySelectorAll(".tab-body").forEach(b => b.hidden = b.dataset.body !== tab.dataset.tab);
    };
    panel.querySelectorAll(".tab").forEach(tab => tab.addEventListener("click", () => { showTab(tab.dataset.tab); pref("pm_side_tab", tab.dataset.tab); }));
    showTab(pref("pm_side_tab") || "card");
    if (window.pmChatInit) window.pmChatInit(panel);
    const figBox = panel.querySelector("[data-figures]");
    if (figBox && pref("pm_side_tab") === "figures") loadFigures(figBox);
    // PDF column
    const split = panel.querySelector(".panel-split");
    const w = pref("pm_split"); if (split && w) split.style.setProperty("--pdf-w", w);
    const frame = panel.querySelector(".pdf-col iframe");
    const pdfBtn = panel.querySelector('[data-action="toggle-pdf"]');
    if (new URLSearchParams(location.search).get("read") === "1") {  // "continue reading": show the PDF next to the card
      pref("pm_pdf_hidden", "0");
      const u = new URL(location.href); u.searchParams.delete("read"); history.replaceState(history.state, "", u);
    }
    const applyPdf = () => {
      const hidden = pref("pm_pdf_hidden") === "1";
      panel.classList.toggle("pdf-hidden", hidden);
      if (pdfBtn) pdfBtn.textContent = hidden ? t("对照显示 PDF") : t("隐藏 PDF");
      if (frame && !hidden && !frame.src) frame.src = frame.dataset.src;
    };
    applyPdf();
    pdfBtn && pdfBtn.addEventListener("click", () => { pref("pm_pdf_hidden", pref("pm_pdf_hidden") === "1" ? "0" : "1"); applyPdf(); });
    const listBtn = panel.querySelector('[data-action="toggle-list"]');
    if (listBtn) {
      const updateListButton = () => {
        const expanded = !document.body.classList.contains("list-collapsed");
        listBtn.setAttribute("aria-expanded", String(expanded));
        listBtn.title = expanded ? t("收起论文列表") : t("展开论文列表");
        listBtn.setAttribute("aria-label", listBtn.title);
      };
      updateListButton();
      listBtn.addEventListener("click", () => {
        const collapsed = document.body.classList.toggle("list-collapsed");
        pref("pm_list_collapsed", collapsed ? "1" : "0");
        updateListButton();
      });
    }
    const handle = panel.querySelector(".split-handle");
    if (handle && !handle.dataset.bound) {
      handle.dataset.bound = "1";
      handle.addEventListener("pointerdown", e => {
        e.preventDefault(); handle.classList.add("dragging"); document.body.classList.add("splitting");
        const box = split.getBoundingClientRect();
        const move = ev => {
          const pct = Math.max(30, Math.min(78, ((ev.clientX - box.left) / box.width) * 100));
          split.style.setProperty("--pdf-w", pct.toFixed(1) + "%");
        };
        const up = () => {
          document.removeEventListener("pointermove", move); document.removeEventListener("pointerup", up);
          handle.classList.remove("dragging"); document.body.classList.remove("splitting");
          pref("pm_split", split.style.getPropertyValue("--pdf-w"));
        };
        document.addEventListener("pointermove", move); document.addEventListener("pointerup", up);
      });
    }
    const purposeBox = panel.querySelector("[data-purposes]");
    purposeBox && purposeBox.addEventListener("click", async e => {
      const chip = e.target.closest("[data-purpose]"), add = e.target.closest("[data-purpose-add]");
      if (chip) {
        const on = !chip.classList.contains("on");
        if ((await api("/api/purposes", { paper_ids: [id], tag: chip.dataset.purpose, on })).ok) { chip.classList.toggle("on", on); refreshSidebarCounts(); }
      } else if (add) {
        const tag = await dialog(t("自定义用途（例如：基金申请、综述第 3 节）："), { input: true, okText: t("添加") });
        if (tag && (await api("/api/purposes", { paper_ids: [id], tag, on: true })).ok) openPaper(id, false), refreshSidebarCounts();
      }
    });
    const del = panel.querySelector('[data-action="delete-paper"]');
    del && del.addEventListener("click", () => window.pmDeletePapers([id]));
    const restore = panel.querySelector('[data-action="restore-paper"]');
    restore && restore.addEventListener("click", async () => {
      const j = await api(`/paper/${id}/restore`);
      if (j.ok) { toast(t("已恢复")); location.href = "/?id=" + id; } else toast(j.message || t("恢复失败"));
    });
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
        if (r.ok) { saved = text; status.textContent = t("已保存 ") + new Date().toLocaleTimeString(); }
        else status.textContent = t("保存失败");
      };
      notes.addEventListener("input", () => { status.textContent = t("编辑中…"); clearTimeout(timer); timer = setTimeout(save, 800); });
      notes.addEventListener("keydown", e => { if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); } });
      notes.addEventListener("blur", () => setTimeout(save, 150));
      // [[ link suggestions
      const pop = document.createElement("div"); pop.className = "link-pop"; pop.hidden = true; document.body.appendChild(pop);
      let items = [], sel = 0, startPos = -1;
      const close = () => { pop.hidden = true; startPos = -1; };
      const pick = i => {
        const it = items[i]; if (!it) return;
        const v = notes.value, end = notes.selectionStart;
        notes.value = v.slice(0, startPos) + it.insert + v.slice(end);
        notes.selectionStart = notes.selectionEnd = startPos + it.insert.length;
        close(); notes.dispatchEvent(new Event("input"));
      };
      const render = () => {
        pop.innerHTML = items.map((it, i) => `<div class="lp-item ${i === sel ? "on" : ""}" data-i="${i}">${it.label.replace(/[<>&]/g, "")}</div>`).join("") || `<div class="lp-empty">${t("没有匹配的论文或学科")}</div>`;
      };
      notes.addEventListener("keyup", async e => {
        if (["ArrowDown", "ArrowUp", "Enter", "Escape"].includes(e.key) && !pop.hidden) return;
        const v = notes.value.slice(0, notes.selectionStart), at = v.lastIndexOf("[[");
        if (at === -1 || v.slice(at).includes("]]") || v.slice(at).includes("\n")) { close(); return; }
        startPos = at;
        items = await (await fetch("/api/link-suggest?q=" + encodeURIComponent(v.slice(at + 2)))).json(); sel = 0; render();
        const r = notes.getBoundingClientRect();
        pop.style.left = r.left + 12 + "px"; pop.style.top = Math.min(innerHeight - 260, r.top + 40) + "px"; pop.style.width = Math.min(520, r.width - 24) + "px";
        pop.hidden = false;
      });
      notes.addEventListener("keydown", e => {
        if (pop.hidden) return;
        if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(items.length - 1, sel + 1); render(); }
        if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(0, sel - 1); render(); }
        if (e.key === "Enter") { e.preventDefault(); pick(sel); }
        if (e.key === "Escape") close();
      });
      pop.addEventListener("mousedown", e => { const d = e.target.closest(".lp-item"); if (d) { e.preventDefault(); pick(+d.dataset.i); } });
      notes.addEventListener("blur", () => setTimeout(close, 150));
      const pv = panel.querySelector("[data-notes-preview]"), pvBox = panel.querySelector(".notes-preview");
      pv && pv.addEventListener("click", async () => {
        const showing = !pvBox.hidden;
        if (!showing) pvBox.innerHTML = (await api("/api/notes-preview", { text: notes.value })).html || "";
        pvBox.hidden = showing; notes.hidden = !showing; pv.textContent = showing ? t("预览") : t("编辑");
      });
    }
  }
  initPanel();

  // ---------------------------------------------------------- live refresh
  let polling = null, lastEvent = null;
  async function poll() {
    try {
      const s = await (await fetch("/api/state")).json();
      const ver = s.last_event + "|" + (s.jobs_version || "");
      if (lastEvent !== null && ver !== lastEvent) {
        await refreshList();
        const panel = document.querySelector(".panel");
        const ae = document.activeElement;
        const typing = (ae && (ae.classList.contains("notes") || ae.closest(".ProseMirror") || ae.closest(".chat-body"))) || document.querySelector(".card-editing, .chat-busy, .chat-summary:not([hidden])");
        if (panel && !typing) openPaper(panel.dataset.paper, false);
      }
      lastEvent = ver;
      const reviewLink = document.querySelector('.nav a[href="/review"]');
      const badge = reviewLink?.querySelector(".badge");
      if (s.review && !badge && reviewLink) reviewLink.insertAdjacentHTML("beforeend", `<b class="badge">${s.review}</b>`);
      else if (badge) { if (s.review) badge.textContent = s.review; else badge.remove(); }
      if (!s.pending && !s.busy && polling) { clearInterval(polling); polling = null; setTimeout(startPolling, 15000); }
    } catch (e) { /* server restarting */ }
  }
  function startPolling() { if (!polling) { poll(); polling = setInterval(poll, 2500); } }
  if (list) startPolling();
  window.pmStartPolling = startPolling;
  window.pmOpenPaper = openPaper;

  // ------------------------------------------- native folder picker (app only)
  function enableNativeWindow() {
    document.body.classList.add("native");
    const root = document.documentElement;
    if (root.hasAttribute("data-native-glass")) root.classList.add("native-glass");
  }
  if (window.pywebview?.api) enableNativeWindow();
  else window.addEventListener("pywebviewready", enableNativeWindow, { once: true });
  document.querySelectorAll("[data-pick-folder]").forEach(btn => btn.addEventListener("click", async () => {
    if (!window.pywebview) { toast(t("在 PaperMap 应用里可以直接选择文件夹；浏览器里请手动填写路径")); return; }
    const path = await window.pywebview.api.pick_folder();
    if (path) {
      const ta = document.getElementById(btn.dataset.pickFolder);
      if (!ta) return;
      ta.value = btn.hasAttribute("data-pick-replace") ? path : (ta.value.trim() ? ta.value.trim() + "\n" : "") + path;
    }
  }));

  // ------------------------------------------------------- groups (collections)
  const modal = document.getElementById("modal");
  function dialog(msg, { input = false, value = "", okText = t("确定"), danger = false } = {}) {
    if (!modal) return Promise.resolve(null);
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
  window.pmDeletePapers = async ids => {
    const msg = ids.length > 1 ? `${t("删除所选的")} ${ids.length} ${t("篇论文？")}` : t("删除这篇论文？");
    const ok = await dialog(msg + "\n\n" + t("它的 PDF 副本、卡片、笔记和译文会移到 PaperMap 的回收站文件夹，并从列表、学科页和知识地图里去掉。入口文件夹里的原文件不动，以后也不会被重新导入。在论文列表的筛选里选「已删除 / 已忽略」可以恢复。"), { okText: t("删除"), danger: true });
    if (!ok) return;
    const j = await api("/api/papers/delete", { paper_ids: ids });
    if (j.deleted) { toast(`${t("已删除")} ${j.deleted} ${t("篇")}`); const u = new URL(location.href); u.searchParams.delete("id"); location.href = u.pathname === "/" ? u.href : "/"; }
  };
  async function api(url, body = {}) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ csrf, ...body }) });
    const j = await r.json().catch(() => ({ ok: false, message: "HTTP " + r.status }));
    if (!j.ok && !j.need_ack) toast(j.message || t("操作失败"));
    return j;
  }
  // Only the sidebar's counts and entries (keeps the open paper as it is).
  async function refreshSidebarCounts() {
    if (settingsPage || !sidebar) return;
    const doc = new DOMParser().parseFromString(await (await fetch(location.href)).text(), "text/html");
    const fresh = doc.querySelector("#collections"), cur = document.getElementById("collections");
    if (fresh && cur) { cur.innerHTML = fresh.innerHTML; applyCollapsed(); }
  }
  async function refreshSidebar() {
    if (settingsPage || !sidebar) return;
    const html = await (await fetch(location.href)).text();
    const doc = new DOMParser().parseFromString(html, "text/html");
    const replacement = doc.querySelector(".sidebar");
    if (!replacement) return;
    sidebar.innerHTML = replacement.innerHTML;
    applyCollapsed();
    if (list) refreshList();
    const panel = document.querySelector(".panel"); if (panel) openPaper(panel.dataset.paper, false);
  }
  const closedKey = "pm_closed_groups";
  const closed = () => { try { return new Set(JSON.parse(localStorage.getItem(closedKey) || "[]")); } catch (e) { return new Set(); } };
  // Sidebar sections: open = the user's choice (remembered), or the section holding the current page.
  const NAV_KEY = "pm_nav_open";
  const navOpen = () => { try { return new Set(JSON.parse(localStorage.getItem(NAV_KEY) || "[]")); } catch (e) { return new Set(); } };
  document.addEventListener("toggle", e => {
    const d = e.target; if (!d.matches || !d.matches("details[data-nav-group]") || d._pmSetting) return;
    const o = navOpen(); if (d.open) o.add(d.dataset.navGroup); else o.delete(d.dataset.navGroup);
    try { localStorage.setItem(NAV_KEY, JSON.stringify([...o])); } catch (err) { /* private mode */ }
  }, true);
  function applyNavGroups() {
    const o = navOpen();
    document.querySelectorAll("details[data-nav-group]").forEach(d => {
      const want = d.hasAttribute("data-here") || o.has(d.dataset.navGroup);
      if (d.open !== want) { d._pmSetting = true; d.open = want; setTimeout(() => { d._pmSetting = false; }, 0); }
    });
  }
  function applyCollapsed() {
    applyNavGroups();
    const c = closed();
    document.querySelectorAll("[data-children-of]").forEach(el => el.hidden = c.has(el.dataset.childrenOf));
    document.querySelectorAll(".caret.has").forEach(el => el.classList.toggle("closed", c.has(el.dataset.toggle)));
  }
  applyCollapsed();

  const menu = document.getElementById("ctx_menu");
  function showMenu(x, y, items) {
    if (!menu) return;
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
  document.addEventListener("click", e => { if (menu && !menu.contains(e.target)) menu.hidden = true; });
  document.addEventListener("keydown", e => { if (menu && e.key === "Escape") menu.hidden = true; });

  window.pmDialog = dialog; window.pmApi = api; window.pmMenu = showMenu;

  // ------------------------------------------------------------ figures + lightbox
  const lb = document.getElementById("lightbox");
  let lbFig = null;
  function openFigure(f) {
    if (!lb) return;
    lbFig = f;
    document.getElementById("lb_title").textContent = f.label + (f.paper ? " · " + f.paper : "");
    document.getElementById("lb_img").src = `/api/figure/${f.id}.png?scale=2`;
    document.getElementById("lb_legend").textContent = f.legend || "";
    document.getElementById("lb_expl").innerHTML = f.explanation_html || "";
    document.getElementById("lb_notes").hidden = !f.explanation_html;
    document.getElementById("lb_page").href = `/reader/${f.paper_id}?page=${f.page + 1}`;
    lb.hidden = false;
  }
  window.pmOpenFigure = openFigure;
  const lbClose = document.getElementById("lb_close");
  if (lbClose) lbClose.onclick = () => { if (lb) lb.hidden = true; };
  lb?.addEventListener("click", e => { if (e.target === lb) lb.hidden = true; });
  document.addEventListener("keydown", e => { if (lb && e.key === "Escape") lb.hidden = true; });
  const lbExplain = document.getElementById("lb_explain");
  if (lbExplain) lbExplain.onclick = async ev => {
    const b = ev.target; b.disabled = true;
    document.getElementById("lb_expl").innerHTML = `<p class="job-line"><span class="spinner"></span>${t("模型正在看图…")}</p>`;
    const j = await api(`/api/figure/${lbFig.id}/explain`, {});
    b.disabled = false;
    document.getElementById("lb_expl").innerHTML = j.ok ? j.html : "";
    if (j.ok) { lbFig.explanation_html = j.html; document.getElementById("lb_notes").hidden = false; }
  };
  const lbNotes = document.getElementById("lb_notes");
  if (lbNotes) lbNotes.onclick = async () => {
    if ((await api(`/api/figure/${lbFig.id}/to-notes`, {})).ok) toast(t("已加到这篇论文的「我的笔记」"));
  };
  async function loadFigures(box) {
    if (box.dataset.loaded) return; box.dataset.loaded = "1";
    const pid = box.dataset.figures;
    const figs = await (await fetch(`/api/figures/${pid}`)).json();
    box.innerHTML = figs.length ? figs.map((f, i) => `<a href="#" class="fig-card" data-i="${i}"><img loading="lazy" src="/api/figure/${f.id}.png?scale=1" alt=""><span>${f.label}${f.explanation_html ? " · 已讲解" : ""}</span></a>`).join("")
      : `<p class="muted small">${t("没有找到带图注的图（扫描版或排版特殊的 PDF 可能识别不到）。")}</p>`;
    box.addEventListener("click", e => { const a = e.target.closest(".fig-card"); if (!a) return; e.preventDefault(); openFigure({ ...figs[+a.dataset.i], paper_id: +pid }); });
  }
  document.addEventListener("click", e => {
    const t = e.target.closest('.tab[data-tab="figures"]');
    if (t) { const box = t.closest(".panel").querySelector("[data-figures]"); box && loadFigures(box); }
  });

  // star straight from the paper list (capture phase so the row link doesn't open)
  document.addEventListener("click", async e => {
    const st = e.target.closest("[data-row-star]"); if (!st) return;
    e.preventDefault(); e.stopPropagation();
    const pid = st.closest(".paper-row").dataset.id;
    if ((await api(`/paper/${pid}/meta`, { starred: st.dataset.rowStar !== "1" })).ok) {
      refreshList();
      const panel = document.querySelector(`.panel[data-paper="${pid}"]`);
      if (panel) openPaper(pid, false);
    }
  }, true);

  // reading status + star (panel header)
  document.addEventListener("click", async e => {
    const star = e.target.closest(".star-btn"), seg = e.target.closest("[data-rs-val]");
    if (!star && !seg) return;
    const pid = e.target.closest(".panel").dataset.paper;
    const body = star ? { starred: star.dataset.star !== "1" } : { read_status: seg.dataset.rsVal };
    if ((await api(`/paper/${pid}/meta`, body)).ok) {
      openPaper(pid, false); refreshList();
      if (seg) document.dispatchEvent(new Event("papermap:read-status"));
    }
  });

  // export references
  async function exportRefs(fmt, query, extension = fmt) {
    const url = `/export.${fmt}?${query}`;
    if (window.pywebview && window.pywebview.api && window.pywebview.api.save_text) {
      const response = await fetch(url);
      if (!response.ok) { toast(t("导出失败，请检查模板是否仍已启用")); return; }
      const path = await window.pywebview.api.save_text(`papermap.${extension}`, await response.text());
      if (path) toast(t("已保存到 ") + path);
    } else { location.href = url; }
  }
  function exportMenu(x, y, ids) {
    const cur = list ? list.dataset.c || "" : "";
    const scopeQ = ids.length ? `ids=${ids.join(",")}` : (cur ? `c=${cur}` : "");
    const what = ids.length ? `${t("所选")} ${ids.length} ${t("篇")}` : (cur ? t("当前分组") : t("全部论文"));
    const templates = JSON.parse(document.getElementById("plugin_exports")?.textContent || "[]");
    showMenu(x, y, [{ label: `${t("导出")}${what}` },
      { text: "BibTeX（.bib）", run: () => exportRefs("bib", scopeQ) },
      { text: "RIS（EndNote / Zotero）", run: () => exportRefs("ris", scopeQ) },
      ...templates.map(t => ({ text: t.name, run: () => exportRefs(t.id, scopeQ, t.extension) }))]);
  }
  window.pmExportMenu = exportMenu;
  document.addEventListener("click", e => {
    const b = e.target.closest("#export_btn"); if (!b) return;
    const r = b.getBoundingClientRect();
    exportMenu(r.left, r.bottom + 4, list ? [...list.querySelectorAll(".paper-row.multi")].map(x => x.dataset.id) : []);
  });

  async function newGroup(parentId = null, paperIds = []) {
    const name = await dialog(parentId ? t("新建子分组：") : t("新建分组："), { input: true, okText: t("创建") });
    if (!name) return;
    const j = await api("/api/collections", { name, parent_id: parentId, paper_ids: paperIds });
    if (j.ok) { toast(`${t("已创建「")}${name}」`); refreshSidebar(); }
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
  document.addEventListener("change", async e => {
    const tsel = e.target.closest("[data-translate]"), ssel = e.target.closest("[data-skill]");
    if (tsel || ssel) {
      const el = tsel || ssel, value = el.value; el.value = ""; if (!value) return;
      const pid = el.closest(".panel").dataset.paper;
      let j;
      if (tsel) {
        j = await api(`/paper/${pid}/translate`, { service: value });
        if (j.need_ack) {
          if (!(await dialog(t("使用 bilibili 免费翻译时，论文正文会发送到 B 站的服务器（index-translate.bilibili.com）。确定继续？以后不再询问。"), { okText: t("继续翻译") }))) return;
          j = await api(`/paper/${pid}/translate`, { service: value, ack: true });
        }
      } else {
        j = await api(`/paper/${pid}/skill`, { skill: value });
      }
      if (j.ok) { toast(tsel ? t("已开始翻译，完成后会出现「译文对照」标签") : t("已开始解读，完成后会出现新标签")); openPaper(pid, false); startPolling(); }
      return;
    }
    const sel = e.target.closest("[data-add-group]"); if (!sel || !sel.value) return;
    const pid = sel.closest(".panel").dataset.paper;
    api(`/api/collections/${sel.value}/add`, { paper_ids: [pid] }).then(j => { if (j.ok) { toast(t("已加入分组")); refreshSidebar(); } });
  });

  document.addEventListener("contextmenu", e => {
    const g = e.target.closest(".coll[data-cid]");
    if (g && /^\d+$/.test(g.dataset.cid)) {
      e.preventDefault();
      const id = g.dataset.cid, name = g.querySelector(".name").textContent;
      showMenu(e.clientX, e.clientY, [
        { text: t("新建子分组…"), run: () => newGroup(id) },
        { text: t("重命名…"), run: async () => {
          const n = await dialog(t("重命名分组："), { input: true, value: name, okText: t("保存") });
          if (n && n !== name && (await api(`/api/collections/${id}/rename`, { name: n })).ok) refreshSidebar();
        } },
        { text: t("移到最上层"), run: async () => { if ((await api(`/api/collections/${id}/move`, { parent_id: null })).ok) refreshSidebar(); } },
        "-",
        { text: t("删除分组…"), danger: true, run: async () => {
          if (await dialog(`${t("删除分组「")}${name}${t("」及其子分组？里面的论文不会被删除，仍在「全部论文」里。")}`, { okText: t("删除"), danger: true })) {
            if ((await api(`/api/collections/${id}/delete`)).ok) {
              if (new URLSearchParams(location.search).get("c") === id) location.href = "/"; else refreshSidebar();
            }
          }
        } },
      ]);
      return;
    }
    if (g && (g.dataset.cid === "star" || g.dataset.cid.startsWith("u:"))) {  // export a starred / purpose list
      e.preventDefault();
      const scopeQ = "c=" + encodeURIComponent(g.dataset.cid), name = g.querySelector(".name").textContent;
      showMenu(e.clientX, e.clientY, [{ label: `${t("导出")}「${name}」` },
        { text: "BibTeX（.bib）", run: () => exportRefs("bib", scopeQ) },
        { text: "RIS（EndNote / Zotero）", run: () => exportRefs("ris", scopeQ) }]);
      return;
    }
    const row = e.target.closest(".paper-row");
    if (row && list) {
      e.preventDefault();
      if (!row.classList.contains("multi")) list.querySelectorAll(".multi").forEach(r => r.classList.remove("multi"));
      const ids = selectedPaperIds(row), cur = list.dataset.c;
      const groups = groupList();
      const items = [{ label: ids.length > 1 ? `${t("已选")} ${ids.length} ${t("篇 · 加入分组")}` : t("加入分组") }];
      groups.forEach(gr => items.push({ text: "　".repeat(gr.depth) + gr.name, run: async () => {
        const j = await api(`/api/collections/${gr.id}/add`, { paper_ids: ids });
        if (j.ok) { toast(`${t("已加入「")}${gr.name}」`); refreshSidebar(); }
      } }));
      items.push({ text: t("＋ 新建分组并加入…"), run: () => newGroup(null, ids) });
      items.push("-", { text: t("导出参考文献…"), run: () => exportMenu(e.clientX, e.clientY, ids) });
      items.push({ text: t("用这些论文写一段…"), run: () => { location.href = "/write?ids=" + ids.join(","); } });
      items.push({ text: ids.length > 1 ? `${t("删除所选")} ${ids.length} ${t("篇…")}` : t("删除这篇论文…"), danger: true, run: () => window.pmDeletePapers(ids) });
      if (ids.length > 1) items.push({ text: `${t("对比所选")} ${ids.length} ${t("篇论文")}`, run: () => { location.href = "/compare?ids=" + ids.join(","); } });
      const tags = JSON.parse(document.getElementById("purpose_tags")?.textContent || "[]");
      if (tags.length) {
        items.push("-", { label: t("标记用途") });
        tags.forEach(u => items.push({ text: "#" + u.label, run: async () => {
          if ((await api("/api/purposes", { paper_ids: ids, tag: u.tag, on: true })).ok) { toast(`${t("已标记")}「${u.label}」`); refreshSidebar(); }
        } }));
      }
      if (/^\d+$/.test(cur || "")) {
        items.push("-", { text: t("从当前分组移除"), run: async () => { if ((await api(`/api/collections/${cur}/remove`, { paper_ids: ids })).ok) refreshSidebar(); } });
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
      ghost.textContent = ids.length > 1 ? `${ids.length} ${t("篇论文")}` : row.querySelector(".t").textContent.slice(0, 40);
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
    const okPapers = types.includes(PAPERS) && /^(\d+|star|u:.+)$/.test(t.dataset.cid || "");
    const okGroup = types.includes(GROUP) && (/^\d+$/.test(t.dataset.cid || "") || t.dataset.cid === "root");
    if (okPapers || okGroup) { e.preventDefault(); t.classList.add("drop-target"); }
  });
  document.addEventListener("dragleave", e => { const t = targetOf(e); t && t.classList.remove("drop-target"); });
  document.addEventListener("drop", async e => {
    const t = targetOf(e); if (!t) return;
    t.classList.remove("drop-target");
    const papers = e.dataTransfer.getData(PAPERS), group = e.dataTransfer.getData(GROUP);
    if (papers && (t.dataset.cid === "star" || (t.dataset.cid || "").startsWith("u:"))) {
      e.preventDefault();
      const ids = JSON.parse(papers), name = t.querySelector(".name").textContent;
      const j = t.dataset.cid === "star"
        ? await Promise.all(ids.map(id => api(`/paper/${id}/meta`, { starred: true }))).then(rs => ({ ok: rs.every(r => r.ok) }))
        : await api("/api/purposes", { paper_ids: ids, tag: t.dataset.cid.slice(2), on: true });
      if (j.ok) { toast(`${t("已加入「")}${name}」`); refreshSidebar(); }
    } else if (papers && /^\d+$/.test(t.dataset.cid)) {
      e.preventDefault();
      const ids = JSON.parse(papers), name = t.querySelector(".name").textContent;
      const j = await api(`/api/collections/${t.dataset.cid}/add`, { paper_ids: ids });
      if (j.ok) { toast(j.added ? `${t("已加入「")}${name}」${ids.length > 1 ? `（${j.added} 篇）` : ""}` : `${t("已经在「")}${name}${t("」里了")}`); refreshSidebar(); }
    } else if (group) {
      e.preventDefault();
      const parent = t.dataset.cid === "root" ? null : t.dataset.cid;
      if (parent === group) return;
      if ((await api(`/api/collections/${group}/move`, { parent_id: parent })).ok) refreshSidebar();
    }
  });
})();

// Review page: "establish as new field" opens its form inline.
document.addEventListener("click", e => {
  const open = e.target.closest("[data-pf-open]"), cancel = e.target.closest("[data-pf-cancel]");
  if (!open && !cancel) return;
  const item = (open || cancel).closest(".review-item"), form = item && item.querySelector("[data-pf-form]");
  if (!form) return;
  form.hidden = !!cancel || !form.hidden;
  item.querySelector("[data-pf-open]").classList.toggle("open", !form.hidden);
  if (!form.hidden) form.querySelector("input[name=name_zh]").focus();
});

// "Checked" on a passage that changed between a preprint and its published version.
document.addEventListener("click", async e => {
  const b = e.target.closest("[data-vcheck]"); if (!b) return;
  b.disabled = true;
  const r = await fetch(`/api/version-checks/${b.dataset.vcheck}`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ csrf: document.body.dataset.csrf }) }).then(x => x.json()).catch(() => ({}));
  if (r.ok) { const item = b.closest("[data-vcheck-item]"); item.classList.add("done"); b.replaceWith(Object.assign(document.createElement("span"), { className: "muted small", textContent: (window.t ? t("已核对") : "已核对") })); }
  else b.disabled = false;
});

document.addEventListener("submit", async event => {
  const form = event.target.closest("[data-supplement-upload]");
  if (!form) return;
  event.preventDefault();
  const submit = form.querySelector("button");
  if (submit.disabled) return;
  const status = form.querySelector("[data-upload-status]");
  submit.disabled = true;
  status.textContent = t("正在添加补充材料…");
  try {
    const response = await fetch(form.action, {method: "POST", body: new FormData(form)});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.detail || t("添加失败，请重试。"));
    location.assign(data.url);
  } catch (error) {
    status.textContent = error.message || t("添加失败，请重试。");
  } finally {
    submit.disabled = false;
  }
});
