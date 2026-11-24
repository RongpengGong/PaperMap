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
  const ocrButton = document.getElementById("reader_ocr");
  let activeEvidence = null;
  const measure = document.createElement("canvas").getContext("2d");
  const progress = window.pmReadingProgress(root, pagesEl, pageEls, csrf);

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
    el.querySelector("img").src = `/api/reader/${pid}/page/${i}.png?scale=${scale}&revision=${encodeURIComponent(info.revision)}`;
  }
  function setPageWords(i, result) {
    const el = pageEls[i];
    el._words = result.words;
    el.dataset.source = result.source === "ocr" ? "ocr" : "native";
    buildText(i);
    drawHighlights(i);
    syncOcrButton();
  }
  async function loadPage(i) {
    const el = pageEls[i];
    if (el._words || el._loading) return el._loading;
    el.dataset.loaded = "1";
    loadImage(i);
    el._loading = (async () => {
      try {
        const response = await fetch(`/api/reader/${pid}/page/${i}/words?details=1`);
        if (!response.ok) throw Error(t("无法读取本页文字。"));
        const result = await response.json();
        if (!Array.isArray(result.words)) throw Error(t("本页文字暂不可用。"));
        setPageWords(i, result);
        if (result.message && !result.words.length && +pageNo.value === i + 1) toast(result.message);
      } catch (error) {
        if (+pageNo.value === i + 1) toast(error.message);
      } finally {
        el._loading = null;
      }
    })();
    return el._loading;
  }
  function syncOcrButton() {
    const page = pageEls[(+pageNo.value || 1) - 1];
    const recognized = page?.dataset.source === "ocr";
    ocrButton.disabled = !page || !!page._recognizing || recognized;
    ocrButton.textContent = page?._recognizing ? t("识别中…") : recognized ? t("本页已识别") : t("识别本页");
  }
  ocrButton.addEventListener("click", async () => {
    const i = Math.min(pageEls.length - 1, Math.max(0, (+pageNo.value || 1) - 1));
    const el = pageEls[i];
    if (!el || el._recognizing) return;
    el._recognizing = true;
    syncOcrButton();
    try {
      await loadPage(i);
      const result = await post(`/api/reader/${pid}/page/${i}/ocr`, {});
      if (!result.ok || !Array.isArray(result.words)) throw Error(result.message || t("识别失败，请重试。"));
      setPageWords(i, result);
      toast(result.message || (result.words.length ? t("本页文字已就绪，可以选中原文摘录。") : t("本页未识别到文字。")));
    } catch (error) {
      toast(error.message);
    } finally {
      el._recognizing = false;
      syncOcrButton();
    }
  });
  function wordSeparator(current, next) {
    if (!next || current[5] !== next[5]) return "";
    const left = String(current[4]), right = String(next[4]);
    const cjk = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u;
    if ((cjk.test(left.slice(-1)) || /[，。；：？！、）】》”’]$/.test(left)) && cjk.test(right.slice(0, 1))) return "";
    if (/^[，。；：？！、）】》”’.,;:!?%)\]]/.test(right) || /[（【《“‘(\[]$/.test(left)) return "";
    return " ";
  }
  function buildText(i) {
    const el = pageEls[i], layer = el.querySelector(".text-layer"), s = ps(i);
    layer.innerHTML = "";
    if (!el._words) return;
    const frag = document.createDocumentFragment();
    let line = null;
    for (const [index, word] of el._words.entries()) {
      const [x0, y0, x1, y1, text, lk] = word;
      if (line !== null && lk !== line) frag.appendChild(document.createTextNode("\n"));
      line = lk;
      const span = document.createElement("span");
      const h = (y1 - y0) * s, w = (x1 - x0) * s, fs = h * 0.88;
      span.textContent = text + wordSeparator(word, el._words[index + 1]);
      measure.font = `${fs}px sans-serif`;
      const natural = measure.measureText(span.textContent).width || w;
      span.style.cssText = `left:${x0 * s}px;top:${y0 * s}px;height:${h}px;font-size:${fs}px;transform:scaleX(${w / natural})`;
      frag.appendChild(span);
    }
    layer.appendChild(frag);
  }
  function drawHighlights(i) {
    const el = pageEls[i], layer = el.querySelector(".hl-layer"), s = ps(i);
    layer.innerHTML = "";
    if (activeEvidence?.page === i) {
      for (const [x0, y0, x1, y1] of activeEvidence.rects) {
        const mark = document.createElement("div");
        mark.className = "evidence-source-mark";
        mark.style.cssText = `left:${x0 * s}px;top:${y0 * s}px;width:${(x1 - x0) * s}px;height:${(y1 - y0) * s}px`;
        layer.appendChild(mark);
      }
    }
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
    ocrButton.hidden = !info.ocr_available;
    syncOcrButton();
    document.getElementById("page_total").textContent = info.pages.length;
    info.pages.forEach((p, i) => {
      const el = document.createElement("div");
      el.className = "page"; el.dataset.page = i;
      el.innerHTML = `<img alt="第 ${i + 1} ${t("页\" draggable=\"false\">")}<div class="hl-layer"></div><div class="text-layer"></div><div class="flash-layer"></div><span class="page-num">${i + 1}</span>`;
      pagesEl.appendChild(el); pageEls.push(el);
    });
    computeFit(); layout();
    const io = new IntersectionObserver(entries => entries.forEach(e => { if (e.isIntersecting) loadPage(+e.target.dataset.page); }),
      { root: pagesEl, rootMargin: "800px 0px" });
    pageEls.forEach(el => io.observe(el));
    renderToc(); renderAnnList();
    const start = Math.max(1, Math.min(info.pages.length, +root.dataset.start || 1));
    const find = (root.dataset.find || "").replace(/"/g, "").trim();
    const params = new URLSearchParams(location.search);
    const evidenceId = params.get("evidence"), updateId = params.get("update");
    if (updateId) {
      await openEvidence(`u${updateId}`, start - 1, `/api/reader/${pid}/update/${encodeURIComponent(updateId)}/${encodeURIComponent(params.get("anchor") || "0")}`);
    } else if (evidenceId) {
      await openEvidence(evidenceId, start - 1);
    } else if (find) {  // opened from full-text search: show the hits on that page
      const q = find.split(/\s+/).sort((a, b) => b.length - a.length)[0];
      document.getElementById("search_q").value = q;
      const hits = await (await fetch(`/api/reader/${pid}/search?q=${encodeURIComponent(q)}`)).json();
      const onPage = hits.filter(h => h.page === start - 1);
      await loadPage(start - 1);
      goto(start - 1, (onPage[0] || {}).rect);
      onPage.slice(1).forEach(h => flash(h.page, h.rect));
    } else if (root.dataset.start !== "") {
      goto(start - 1);
    } else {
      progress.restore();
    }
    progress.start();
  }

  window.pmGotoPage = i => goto(i);
  try {  // "Ask AI" only when the reader sits in the paper panel next to the chat
    const ask = document.querySelector('#sel_toolbar [data-act="ask"]');
    if (ask && window.parent !== window && window.parent.pmChatAsk) ask.hidden = false;
  } catch (err) { /* cross-origin parent */ }
  function goto(i, rect) {
    const el = pageEls[i]; if (!el) return;
    const s = ps(i);
    pagesEl.scrollTop = el.offsetTop - 12 + (rect ? Math.max(0, rect[1] * s - 120) : 0);
    if (rect) flash(i, rect);
  }
  async function openEvidence(id, fallbackPage, url) {
    const notice = document.getElementById("evidence_location");
    const status = document.getElementById("evidence_location_text");
    notice.hidden = false;
    try {
      const response = await fetch(url || `/api/reader/${pid}/evidence/${encodeURIComponent(id)}`);
      const entry = await response.json();
      if (!response.ok || !entry.ok) throw Error(typeof entry.detail === "string" ? entry.detail : t("原文位置已不可用。"));
      activeEvidence = entry;
      const rect = entry.rects[0];
      if (entry.back) document.getElementById("evidence_location_back").textContent = t("返回论文");
      document.getElementById("evidence_location_back").href = entry.back || `/evidence?question=${entry.question_id}#evidence-${encodeURIComponent(id)}`;
      status.textContent = `${url ? "已定位更新卡原句" : "已定位证据"} ${t("· 第")} ${entry.page + 1} ${t("页")}`;
      drawHighlights(entry.page);
      goto(entry.page, rect);
      const left = pageEls[entry.page].getBoundingClientRect().left - pagesEl.getBoundingClientRect().left;
      const s = ps(entry.page);
      if (left + rect[0] * s < 0 || left + rect[2] * s > pagesEl.clientWidth) {
        pagesEl.scrollLeft += left + rect[0] * s - 24;
      }
    } catch (error) {
      activeEvidence = null;
      goto(fallbackPage);
      status.textContent = error.message;
    }
  }
  document.getElementById("evidence_location_close").addEventListener("click", () => {
    const page = activeEvidence?.page;
    activeEvidence = null;
    if (page !== undefined) drawHighlights(page);
    document.getElementById("evidence_location").hidden = true;
  });
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
    syncOcrButton();
    hideToolbar();
  });
  pageNo.addEventListener("change", () => goto(Math.max(0, Math.min(pageEls.length - 1, pageNo.value - 1))));
  function relayout(update) {
    if (!info || !pageEls.length) return;
    const position = progress.position();
    update(); layout(); progress.place(position);
  }
  function setZoom(z) {
    relayout(() => { zoom = Math.max(0.5, Math.min(3, z)); });
  }
  // Ctrl/⌘ + mouse wheel, trackpad pinch (Chromium/Edge send it as ctrl+wheel,
  // WebKit as gesture events): zoom around the pointer, batched per frame.
  let pendingZoom = null, anchor = null;
  function zoomAt(z, clientX, clientY) {
    z = Math.max(0.5, Math.min(3, z));
    const r = pagesEl.getBoundingClientRect();
    if (!anchor) anchor = { from: zoom, x: pagesEl.scrollLeft + clientX - r.left, y: pagesEl.scrollTop + clientY - r.top,
                            ox: clientX - r.left, oy: clientY - r.top };
    if (pendingZoom === null) requestAnimationFrame(() => {
      const a = anchor, target = pendingZoom;
      pendingZoom = null; anchor = null;
      if (!info || !pageEls.length || Math.abs(target - zoom) < 0.001) return;
      zoom = target; layout();
      const k = zoom / a.from;
      pagesEl.scrollLeft = a.x * k - a.ox;
      pagesEl.scrollTop = a.y * k - a.oy;
    });
    pendingZoom = z;
  }
  pagesEl.addEventListener("wheel", e => {
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
    const base = pendingZoom === null ? zoom : pendingZoom;
    zoomAt(base * Math.exp(-Math.max(-60, Math.min(60, dy)) * 0.004), e.clientX, e.clientY);
  }, { passive: false });
  let gestureStart = 1;
  pagesEl.addEventListener("gesturestart", e => { e.preventDefault(); gestureStart = zoom; });
  pagesEl.addEventListener("gesturechange", e => { e.preventDefault(); zoomAt(gestureStart * e.scale, e.clientX, e.clientY); });
  pagesEl.addEventListener("gestureend", e => e.preventDefault());
  document.getElementById("zoom_in").onclick = () => setZoom(zoom + 0.15);
  document.getElementById("zoom_out").onclick = () => setZoom(zoom - 0.15);
  document.getElementById("zoom_fit").onclick = () => relayout(() => { computeFit(); zoom = 1; });
  document.getElementById("toggle_side").onclick = () => {
    root.classList.toggle("side-hidden"); requestAnimationFrame(() => relayout(computeFit));
  };
  let resizeT; window.addEventListener("resize", () => {
    clearTimeout(resizeT); resizeT = setTimeout(() => relayout(computeFit), 150);
  });
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
    if (!info.toc.length) { toc.innerHTML = `<p class="muted small pad">${t("这个 PDF 没有目录。")}</p>`; return; }
    toc.innerHTML = info.toc.map(t => `<a href="#" class="toc-item" style="--lvl:${t.level - 1}" data-page="${t.page - 1}">${esc(t.title)}<i>${t.page}</i></a>`).join("");
  }
  document.getElementById("toc").addEventListener("click", e => {
    const a = e.target.closest(".toc-item"); if (!a) return; e.preventDefault(); goto(+a.dataset.page);
  });
  document.getElementById("search_q").addEventListener("keydown", async e => {
    if (e.key !== "Enter") return;
    const q = e.target.value.trim(), box = document.getElementById("search_results");
    if (q.length < 2) return;
    box.innerHTML = `<p class="muted small pad">${t("搜索中…")}</p>`;
    const hits = await (await fetch(`/api/reader/${pid}/search?q=${encodeURIComponent(q)}`)).json();
    box.innerHTML = hits.length ? `<p class="muted small pad">${hits.length} ${t("处")}</p>` + hits.map((h, k) =>
      `<a href="#" class="hit" data-k="${k}"><b>p.${h.page + 1}</b> ${esc(h.context)}</a>`).join("") : `<p class="muted small pad">${t("没有找到。")}</p>`;
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
      : `<p class="muted small pad">${t("还没有标注。选中文字后点颜色即可高亮。")}</p>`;
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
      // Merge adjacent glyph and element boxes without bridging separate PDF columns.
      const cy = (y0 + y1) / 2, h = y1 - y0;
      const line = rects.find(l => Math.abs((l[1] + l[3]) / 2 - cy) < Math.min(h, l[3] - l[1]) * 0.5
        && x0 <= l[2] + h * 0.6 && x1 >= l[0] - h * 0.6);
      if (line) { line[0] = Math.min(line[0], x0); line[1] = Math.min(line[1], y0); line[2] = Math.max(line[2], x1); line[3] = Math.max(line[3], y1); }
      else rects.push([x0, y0, x1, y1]);
    }
    const endNode = range.endContainer.nodeType === 1 ? range.endContainer : range.endContainer.parentElement;
    const crossPage = !pageEl.contains(endNode);
    // Selection.toString() can drop separators between absolutely positioned PDF lines.
    // The DOM range retains the text layer's explicit line breaks, including between columns.
    const quote = range.toString().trim();
    const text = quote.replace(/\s+/g, " ");
    return rects.length && quote ? {page: i, rects, text, quote, crossPage, end: range.getBoundingClientRect()} : null;
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
    if (!j.ok) { toast(j.message || t("保存失败")); return; }
    anns.push({ id: j.id, page: selInfo.page, rects: selInfo.rects.map(r => r.map(v => Math.round(v * 100) / 100)), text: selInfo.text, color, note });
    drawHighlights(selInfo.page); renderAnnList();
    window.getSelection().removeAllRanges(); hideToolbar();
  }
  bar.addEventListener("mousedown", e => e.preventDefault());  // keep the selection
  bar.addEventListener("click", async e => {
    const b = e.target.closest("button"); if (!b || !selInfo) return;
    if (b.dataset.act === "evidence") {
      if (selInfo.crossPage) { toast(t("请按页摘录，以便准确定位每段原文。")); return; }
      const selection = selInfo;
      hideToolbar();
      window.pmEvidence.capture({
        paper: pid, revision: info.revision, page: selection.page, rects: selection.rects,
        quote: selection.quote, source: pageEls[selection.page].dataset.source || "native",
      });
      return;
    }
    if (b.dataset.color) return addAnnotation(b.dataset.color);
    if (b.dataset.act === "copy") { navigator.clipboard && navigator.clipboard.writeText(selInfo.text); toast(t("已复制")); hideToolbar(); }
    if (b.dataset.act === "note") {
      const keep = selInfo;
      const note = await window.pmDialog(t("批注："), { input: true, okText: t("保存") });
      selInfo = keep; if (note) addAnnotation("yellow", note);
    }
    if (b.dataset.act === "translate") translateSelection();
    if (b.dataset.act === "ask") {
      const keep = selInfo; hideToolbar(); window.getSelection().removeAllRanges();
      try { window.parent.pmChatAsk(pid, keep.text, keep.page + 1); } catch (err) { /* not embedded */ }
    }
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
    document.getElementById("tr_text").textContent = t("翻译中…"); document.getElementById("tr_model").textContent = "";
    let j = await post("/api/translate-text", { text: keep.text, paper_id: +pid });
    if (j.need_ack) {
      if (!(await window.pmDialog(t("使用 bilibili 免费翻译时，选中的文字会发送到 B 站的服务器（index-translate.bilibili.com）。确定继续？以后不再询问。"), { okText: t("继续") }))) { pop.hidden = true; return; }
      j = await post("/api/translate-text", { text: keep.text, paper_id: +pid, ack: true });
    }
    document.getElementById("tr_text").textContent = j.ok ? j.translation : (j.message || t("翻译失败"));
    document.getElementById("tr_model").textContent = j.ok ? j.model : "";
    lastTr = j.ok ? { sel: keep, text: j.translation } : null;
  }
  document.getElementById("tr_close").onclick = () => pop.hidden = true;
  document.getElementById("tr_copy").onclick = () => { if (lastTr) { navigator.clipboard && navigator.clipboard.writeText(lastTr.text); toast(t("已复制译文")); } };
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
      { label: (a.note ? t("批注：") + a.note : a.text || "").slice(0, 60) },
      { text: a.note ? t("编辑批注…") : t("添加批注…"), run: async () => {
        const n = await window.pmDialog(t("批注："), { input: true, value: a.note || "", okText: t("保存") });
        if (n !== null && (await post(`/api/annotations/${a.id}/update`, { note: n })).ok) { a.note = n; drawHighlights(i); renderAnnList(); }
      } },
      { text: t("改成黄色"), run: recolor("yellow") }, { text: t("改成绿色"), run: recolor("green") },
      { text: t("改成蓝色"), run: recolor("blue") }, { text: t("改成粉色"), run: recolor("pink") },
      "-",
      { text: t("删除标注"), danger: true, run: async () => {
        if ((await post(`/api/annotations/${a.id}/delete`, {})).ok) { anns = anns.filter(x => x !== a); drawHighlights(i); renderAnnList(); }
      } },
    ]);
  });

  init();
})();
