// PaperMap PDF reader: pages rendered by the local server (PyMuPDF), an invisible
// word layer for selection, highlights/notes, select-to-translate, outline & search.
(function () {
  const root = document.getElementById("reader"); if (!root) return;
  const pid = root.dataset.paper;
  const csrf = document.body.dataset.csrf;
  const pagesEl = document.getElementById("pages");
  const toast = (m, t) => window.pmToast && window.pmToast(m, t);
  const COLORS = {};
  document.querySelectorAll(".dot-btn").forEach(b => COLORS[b.dataset.color] = getComputedStyle(b).getPropertyValue("--c").trim());

  let info = null, zoom = 1, fitWidth = 800, anns = [];
  if (document.body.classList.contains("bare")) root.classList.add("side-hidden");
  const pageEls = [];
  const measure = document.createElement("canvas").getContext("2d");

  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ csrf, ...body }) });
    return r.json().catch(() => ({ ok: false, message: "HTTP " + r.status }));
  }

  // ------------------------------------------------------------ layout
  const ps = i => (fitWidth * zoom) / info.pages[i].w;   // CSS px per PDF point
  function computeFit() {
    const avail = pagesEl.clientWidth - (document.body.classList.contains("bare") ? 28 : 48);
    const maxW = Math.max(...info.pages.map(p => p.w));
    fitWidth = Math.max(300, Math.min(avail, 1100)) * (info.pages[0].w / maxW);
  }
  function layout() {
    info.pages.forEach((p, i) => {
      const el = pageEls[i], s = ps(i);
      el.style.width = p.w * s + "px"; el.style.height = p.h * s + "px";
      el.dataset.scale = s;
      if (el.dataset.loaded) { loadImage(i, true); buildText(i); drawHighlights(i); }
    });
    document.getElementById("zoom_label").textContent = Math.round(zoom * 100) + "%";
  }
  function loadImage(i, force) {
    const el = pageEls[i];
    const want = Math.min(3, Math.max(1, ps(i) * (window.devicePixelRatio || 1)));
    const scale = [1, 1.5, 2, 2.5, 3].reduce((a, b) => Math.abs(b - want) < Math.abs(a - want) ? b : a);
    if (!force && el.dataset.imgScale == scale) return;
    el.dataset.imgScale = scale;
    el.querySelector("img").src = `/api/reader/${pid}/page/${i}.png?scale=${scale}`;
  }
  async function loadPage(i) {
    const el = pageEls[i];
    if (el.dataset.loaded) return;
    el.dataset.loaded = "1";
    loadImage(i);
    el._words = await (await fetch(`/api/reader/${pid}/page/${i}/words`)).json();
    buildText(i); drawHighlights(i);
  }
  function buildText(i) {
    const el = pageEls[i], layer = el.querySelector(".text-layer"), s = ps(i);
    layer.innerHTML = "";
    if (!el._words) return;
    const frag = document.createDocumentFragment();
    let line = null;
    for (const [x0, y0, x1, y1, text, lk] of el._words) {
      if (line !== null && lk !== line) frag.appendChild(document.createTextNode("\n"));
      line = lk;
      const span = document.createElement("span");
      const h = (y1 - y0) * s, w = (x1 - x0) * s, fs = h * 0.88;
      span.textContent = text + " ";
      measure.font = `${fs}px sans-serif`;
      const natural = measure.measureText(text + " ").width || w;
      span.style.cssText = `left:${x0 * s}px;top:${y0 * s}px;height:${h}px;font-size:${fs}px;transform:scaleX(${w / natural})`;
      frag.appendChild(span);
    }
    layer.appendChild(frag);
  }
  function drawHighlights(i) {
    const el = pageEls[i], layer = el.querySelector(".hl-layer"), s = ps(i);
    layer.innerHTML = "";
    anns.filter(a => a.page === i).forEach(a => {
      a.rects.forEach((r, k) => {
        const d = document.createElement("div");
        d.className = "hl"; d.dataset.ann = a.id;
        d.style.cssText = `left:${r[0] * s}px;top:${r[1] * s}px;width:${(r[2] - r[0]) * s}px;height:${(r[3] - r[1]) * s}px;background:${COLORS[a.color] || COLORS.yellow}`;
        if (a.note && k === a.rects.length - 1) { d.classList.add("has-note"); d.title = a.note; }
        layer.appendChild(d);
      });
    });
  }

  // ------------------------------------------------------------- loading
  async function init() {
    info = await (await fetch(`/api/reader/${pid}/info`)).json();
    anns = info.annotations;
    document.getElementById("page_total").textContent = info.pages.length;
    info.pages.forEach((p, i) => {
      const el = document.createElement("div");
      el.className = "page"; el.dataset.page = i;
      el.innerHTML = `<img alt="第 ${i + 1} 页" draggable="false"><div class="hl-layer"></div><div class="text-layer"></div><div class="flash-layer"></div><span class="page-num">${i + 1}</span>`;
      pagesEl.appendChild(el); pageEls.push(el);
    });
    computeFit(); layout();
    const io = new IntersectionObserver(entries => entries.forEach(e => { if (e.isIntersecting) loadPage(+e.target.dataset.page); }),
      { root: pagesEl, rootMargin: "800px 0px" });
    pageEls.forEach(el => io.observe(el));
    renderToc(); renderAnnList();
    const start = Math.max(1, Math.min(info.pages.length, +root.dataset.start || 1));
    if (start > 1) goto(start - 1);
  }

  function goto(i, rect) {
    const el = pageEls[i]; if (!el) return;
    const s = ps(i);
    pagesEl.scrollTop = el.offsetTop - 12 + (rect ? Math.max(0, rect[1] * s - 120) : 0);
    if (rect) flash(i, rect);
  }
  function flash(i, r) {
    const layer = pageEls[i].querySelector(".flash-layer"), s = ps(i);
    const d = document.createElement("div"); d.className = "flash";
    d.style.cssText = `left:${r[0] * s - 3}px;top:${r[1] * s - 2}px;width:${(r[2] - r[0]) * s + 6}px;height:${(r[3] - r[1]) * s + 4}px`;
    layer.appendChild(d); setTimeout(() => d.remove(), 2200);
  }

  // ----------------------------------------------------------- page / zoom
  const pageNo = document.getElementById("page_no");
  pagesEl.addEventListener("scroll", () => {
    const mid = pagesEl.scrollTop + pagesEl.clientHeight / 3;
    let cur = 0;
    for (let i = 0; i < pageEls.length; i++) { if (pageEls[i].offsetTop <= mid) cur = i; else break; }
    if (document.activeElement !== pageNo) pageNo.value = cur + 1;
    hideToolbar();
  });
  pageNo.addEventListener("change", () => goto(Math.max(0, Math.min(pageEls.length - 1, pageNo.value - 1))));
  function setZoom(z) {
    const anchor = pagesEl.scrollTop / Math.max(1, pagesEl.scrollHeight);
    zoom = Math.max(0.5, Math.min(3, z)); layout();
    pagesEl.scrollTop = anchor * pagesEl.scrollHeight;
  }
  document.getElementById("zoom_in").onclick = () => setZoom(zoom + 0.15);
  document.getElementById("zoom_out").onclick = () => setZoom(zoom - 0.15);
  document.getElementById("zoom_fit").onclick = () => { computeFit(); setZoom(1); };
  document.getElementById("toggle_side").onclick = () => { root.classList.toggle("side-hidden"); setTimeout(() => { computeFit(); layout(); }, 50); };
  let resizeT; window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => { computeFit(); layout(); }, 150); });
  document.addEventListener("keydown", e => {
    if (!(e.metaKey || e.ctrlKey)) return;
    if (e.key === "=" || e.key === "+") { e.preventDefault(); setZoom(zoom + 0.15); }
    if (e.key === "-") { e.preventDefault(); setZoom(zoom - 0.15); }
    if (e.key === "f") { e.preventDefault(); showSide("search"); document.getElementById("search_q").focus(); }
  });

  // ---------------------------------------------------------------- sidebar
  function showSide(name) {
    root.classList.remove("side-hidden");
    document.querySelectorAll("[data-rs]").forEach(b => b.classList.toggle("active", b.dataset.rs === name));
    document.querySelectorAll("[data-rs-body]").forEach(b => b.hidden = b.dataset.rsBody !== name);
  }
  document.querySelectorAll("[data-rs]").forEach(b => b.addEventListener("click", () => showSide(b.dataset.rs)));
  function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
  function renderToc() {
    const toc = document.getElementById("toc");
    if (!info.toc.length) { toc.innerHTML = `<p class="muted small pad">这个 PDF 没有目录。</p>`; return; }
    toc.innerHTML = info.toc.map(t => `<a href="#" class="toc-item" style="--lvl:${t.level - 1}" data-page="${t.page - 1}">${esc(t.title)}<i>${t.page}</i></a>`).join("");
  }
  document.getElementById("toc").addEventListener("click", e => {
    const a = e.target.closest(".toc-item"); if (!a) return; e.preventDefault(); goto(+a.dataset.page);
  });
  document.getElementById("search_q").addEventListener("keydown", async e => {
    if (e.key !== "Enter") return;
    const q = e.target.value.trim(), box = document.getElementById("search_results");
    if (q.length < 2) return;
    box.innerHTML = `<p class="muted small pad">搜索中…</p>`;
    const hits = await (await fetch(`/api/reader/${pid}/search?q=${encodeURIComponent(q)}`)).json();
    box.innerHTML = hits.length ? `<p class="muted small pad">${hits.length} 处</p>` + hits.map((h, k) =>
      `<a href="#" class="hit" data-k="${k}"><b>p.${h.page + 1}</b> ${esc(h.context)}</a>`).join("") : `<p class="muted small pad">没有找到。</p>`;
    box._hits = hits;
    if (hits.length) goto(hits[0].page, hits[0].rect);
  });
  document.getElementById("search_results").addEventListener("click", e => {
    const a = e.target.closest(".hit"); if (!a) return; e.preventDefault();
    const h = e.currentTarget._hits[+a.dataset.k]; loadPage(h.page).then(() => goto(h.page, h.rect));
  });
  function renderAnnList() {
    document.getElementById("ann_count").textContent = anns.length || "";
    const box = document.getElementById("ann_list");
    box.innerHTML = anns.length ? anns.map(a =>
      `<a href="#" class="ann-item" data-id="${a.id}"><span class="ann-dot" style="background:${COLORS[a.color]}"></span><b>p.${a.page + 1}</b> ${esc((a.text || "").slice(0, 140))}${a.note ? `<span class="ann-note">${esc(a.note)}</span>` : ""}</a>`).join("")
      : `<p class="muted small pad">还没有标注。选中文字后点颜色即可高亮。</p>`;
  }
  document.getElementById("ann_list").addEventListener("click", e => {
    const a = e.target.closest(".ann-item"); if (!a) return; e.preventDefault();
    const ann = anns.find(x => x.id == a.dataset.id); loadPage(ann.page).then(() => goto(ann.page, ann.rects[0]));
  });

  // --------------------------------------------------------------- selection
  const bar = document.getElementById("sel_toolbar");
  let selInfo = null;
  function hideToolbar() { bar.hidden = true; }
  function currentSelection() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    const range = sel.getRangeAt(0);
    const node = range.startContainer.nodeType === 1 ? range.startContainer : range.startContainer.parentElement;
    const pageEl = node && node.closest(".page");
    if (!pageEl || !pagesEl.contains(pageEl)) return null;
    const i = +pageEl.dataset.page, s = ps(i), box = pageEl.getBoundingClientRect();
    const rects = [];
    for (const r of range.getClientRects()) {
      if (r.width < 1 || r.height < 1) continue;
      if (r.bottom < box.top || r.top > box.bottom) continue;  // only the first page of a cross-page selection
      const x0 = (r.left - box.left) / s, y0 = (r.top - box.top) / s, x1 = (r.right - box.left) / s, y1 = (r.bottom - box.top) / s;
      // browsers report element and glyph boxes for each word: merge everything on one line
      const cy = (y0 + y1) / 2, h = y1 - y0;
      const line = rects.find(l => Math.abs((l[1] + l[3]) / 2 - cy) < Math.min(h, l[3] - l[1]) * 0.5);
      if (line) { line[0] = Math.min(line[0], x0); line[1] = Math.min(line[1], y0); line[2] = Math.max(line[2], x1); line[3] = Math.max(line[3], y1); }
      else rects.push([x0, y0, x1, y1]);
    }
    rects.sort((a, b) => a[1] - b[1]);
    const text = sel.toString().replace(/\s+/g, " ").trim();
    return rects.length && text ? { page: i, rects, text, end: range.getBoundingClientRect() } : null;
  }
  pagesEl.addEventListener("mouseup", () => setTimeout(() => {
    selInfo = currentSelection();
    if (!selInfo) { hideToolbar(); return; }
    bar.hidden = false;
    const r = selInfo.end;
    bar.style.left = Math.max(8, Math.min(innerWidth - bar.offsetWidth - 8, r.left + r.width / 2 - bar.offsetWidth / 2)) + "px";
    bar.style.top = (r.top > 60 ? r.top - bar.offsetHeight - 8 : r.bottom + 8) + "px";
  }, 10));
  document.addEventListener("mousedown", e => { if (!bar.contains(e.target) && !e.target.closest(".page")) hideToolbar(); });

  async function addAnnotation(color, note = "") {
    if (!selInfo) return;
    const j = await post(`/api/reader/${pid}/annotations`, { page: selInfo.page, rects: selInfo.rects, text: selInfo.text, color, note });
    if (!j.ok) { toast(j.message || "保存失败"); return; }
    anns.push({ id: j.id, page: selInfo.page, rects: selInfo.rects.map(r => r.map(v => Math.round(v * 100) / 100)), text: selInfo.text, color, note });
    drawHighlights(selInfo.page); renderAnnList();
    window.getSelection().removeAllRanges(); hideToolbar();
  }
  bar.addEventListener("mousedown", e => e.preventDefault());  // keep the selection
  bar.addEventListener("click", async e => {
    const b = e.target.closest("button"); if (!b || !selInfo) return;
    if (b.dataset.color) return addAnnotation(b.dataset.color);
    if (b.dataset.act === "copy") { navigator.clipboard && navigator.clipboard.writeText(selInfo.text); toast("已复制"); hideToolbar(); }
    if (b.dataset.act === "note") {
      const keep = selInfo;
      const note = await window.pmDialog("批注：", { input: true, okText: "保存" });
      selInfo = keep; if (note) addAnnotation("yellow", note);
    }
    if (b.dataset.act === "translate") translateSelection();
  });

  // ------------------------------------------------------- select-to-translate
  const pop = document.getElementById("tr_popup");
  let lastTr = null;
  async function translateSelection() {
    const keep = selInfo; hideToolbar();
    const r = keep.end;
    pop.hidden = false;
    pop.style.left = Math.max(8, Math.min(innerWidth - 440, r.left)) + "px";
    pop.style.top = Math.min(innerHeight - 220, r.bottom + 10) + "px";
    document.getElementById("tr_text").textContent = "翻译中…"; document.getElementById("tr_model").textContent = "";
    let j = await post("/api/translate-text", { text: keep.text, paper_id: +pid });
    if (j.need_ack) {
      if (!(await window.pmDialog("使用 bilibili 免费翻译时，选中的文字会发送到 B 站的服务器（index-translate.bilibili.com）。确定继续？以后不再询问。", { okText: "继续" }))) { pop.hidden = true; return; }
      j = await post("/api/translate-text", { text: keep.text, paper_id: +pid, ack: true });
    }
    document.getElementById("tr_text").textContent = j.ok ? j.translation : (j.message || "翻译失败");
    document.getElementById("tr_model").textContent = j.ok ? j.model : "";
    lastTr = j.ok ? { sel: keep, text: j.translation } : null;
  }
  document.getElementById("tr_close").onclick = () => pop.hidden = true;
  document.getElementById("tr_copy").onclick = () => { if (lastTr) { navigator.clipboard && navigator.clipboard.writeText(lastTr.text); toast("已复制译文"); } };
  document.getElementById("tr_save").onclick = () => { if (lastTr) { selInfo = lastTr.sel; addAnnotation("blue", lastTr.text); pop.hidden = true; } };

  // ------------------------------------------------- click a highlight to edit
  pagesEl.addEventListener("click", e => {
    if (!window.getSelection().isCollapsed) return;
    const page = e.target.closest(".page"); if (!page) return;
    const i = +page.dataset.page, s = ps(i), box = page.getBoundingClientRect();
    const x = (e.clientX - box.left) / s, y = (e.clientY - box.top) / s;
    const a = anns.find(an => an.page === i && an.rects.some(r => x >= r[0] && x <= r[2] && y >= r[1] && y <= r[3]));
    if (!a) return;
    const recolor = c => async () => { if ((await post(`/api/annotations/${a.id}/update`, { color: c })).ok) { a.color = c; drawHighlights(i); renderAnnList(); } };
    window.pmMenu(e.clientX, e.clientY, [
      { label: (a.note ? "批注：" + a.note : a.text || "").slice(0, 60) },
      { text: a.note ? "编辑批注…" : "添加批注…", run: async () => {
        const n = await window.pmDialog("批注：", { input: true, value: a.note || "", okText: "保存" });
        if (n !== null && (await post(`/api/annotations/${a.id}/update`, { note: n })).ok) { a.note = n; drawHighlights(i); renderAnnList(); }
      } },
      { text: "改成黄色", run: recolor("yellow") }, { text: "改成绿色", run: recolor("green") },
      { text: "改成蓝色", run: recolor("blue") }, { text: "改成粉色", run: recolor("pink") },
      "-",
      { text: "删除标注", danger: true, run: async () => {
        if ((await post(`/api/annotations/${a.id}/delete`, {})).ok) { anns = anns.filter(x => x !== a); drawHighlights(i); renderAnnList(); }
      } },
    ]);
  });

  init();
})();
