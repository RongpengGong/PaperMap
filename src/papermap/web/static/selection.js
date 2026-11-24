// Right-click on selected text anywhere: save it to the 收藏簿; inside a paper card also highlight,
// colour, bold… the passage directly (no editor), saved as a new card version.
(() => {
  const csrf = document.body.dataset.csrf;
  const t = s => (window.t ? window.t(s) : s);
  const say = m => (window.pmToast ? window.pmToast(m) : alert(m));
  const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ csrf, ...body }) })
    .then(r => r.json()).catch(() => ({ ok: false, message: t("网络错误") }));
  const HL = [["#fff3a3", "黄"], ["#c8f7c5", "绿"], ["#cfe8ff", "蓝"], ["#ffd6e7", "粉"], ["#e6dcff", "紫"]];
  const FC = [["#d92d20", "红"], ["#dc6803", "橙"], ["#079455", "绿"], ["#1570ef", "蓝"], ["#7a5af8", "紫"]];
  let pop = null;
  const close = () => { if (pop) { pop.remove(); pop = null; } };
  document.addEventListener("mousedown", e => { if (pop && !pop.contains(e.target)) close(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape") close(); });
  document.addEventListener("scroll", close, true);

  // which occurrence of the selected text this is, counted in the rendered card
  function occurrence(root, range, text) {
    const before = document.createRange();
    before.setStart(root, 0); before.setEnd(range.startContainer, range.startOffset);
    const prior = before.toString();
    let n = 0, i = prior.indexOf(text);
    while (i !== -1) { n++; i = prior.indexOf(text, i + 1); }
    return n;
  }

  document.addEventListener("contextmenu", e => {
    const sel = window.getSelection();
    const text = sel && sel.rangeCount ? sel.toString().trim() : "";
    if (text.length < 2) return;
    if (e.target.closest("input, textarea, select, .ProseMirror, .card-editing, .pm-editor-host")) return;
    const range = sel.getRangeAt(0);
    const node = range.commonAncestorContainer.nodeType === 1 ? range.commonAncestorContainer : range.commonAncestorContainer.parentElement;
    if (!node || !node.closest("#main_content, .ai-drawer")) return;
    e.preventDefault(); e.stopPropagation();
    close();
    const panel = node.closest(".panel"), pid = panel ? panel.dataset.paper : null;
    const card = node.closest("[data-card-body]");
    const inChat = node.closest(".chat-msg");
    pop = document.createElement("div");
    pop.className = "sel-pop";
    pop.innerHTML = (card ? `<div class="sel-row"><span class="sel-lbl">${t("高亮")}</span>${HL.map(([c, n]) => `<button type="button" class="pm-swatch" style="--c:${c}" data-hl="${c}" title="${t("高亮")} · ${t(n)}"></button>`).join("")}
        <label class="sel-custom" title="${t("自定义高亮颜色")}"><input type="color" value="#ffe08a" data-custom="hl"></label></div>
      <div class="sel-row"><span class="sel-lbl">${t("文字")}</span>${FC.map(([c, n]) => `<button type="button" class="sel-fc" style="color:${c}" data-fc="${c}" title="${t("文字颜色")} · ${t(n)}">A</button>`).join("")}
        <label class="sel-custom" title="${t("自定义文字颜色")}"><input type="color" value="#b42318" data-custom="fc"></label></div>
      <div class="sel-row">
        <button type="button" class="sel-fmt" data-fmt="bold" title="${t("加粗")}"><b>B</b></button>
        <button type="button" class="sel-fmt" data-fmt="italic" title="${t("斜体")}"><i>I</i></button>
        <button type="button" class="sel-fmt" data-fmt="underline" title="${t("下划线")}"><u>U</u></button></div>` : "")
      + `<button type="button" class="sel-act" data-act="clip">☆ ${t("收藏这句")}</button>`
      + `<button type="button" class="sel-act" data-act="copy">${t("复制")}</button>`;
    document.body.appendChild(pop);
    const w = pop.offsetWidth, h = pop.offsetHeight;
    pop.style.left = Math.max(6, Math.min(innerWidth - w - 6, e.clientX)) + "px";
    pop.style.top = Math.max(6, Math.min(innerHeight - h - 6, e.clientY)) + "px";
    const occ = card ? occurrence(card, range, text) : 0;
    const format = async (kind, color) => {
      const r = await post(`/paper/${pid}/card/format`, { text, occurrence: occ, kind, color: color || "" });
      close();
      if (!r.ok) { say(r.message || t("操作失败")); return; }
      card.innerHTML = r.html;
      say(t("已保存到卡片（可在「历史版本」里撤销）"));
    };
    pop.addEventListener("change", ev => {
      const inp = ev.target.closest("[data-custom]"); if (!inp) return;
      format(inp.dataset.custom === "hl" ? "highlight" : "color", inp.value);
    });
    pop.addEventListener("click", async ev => {
      const b = ev.target.closest("button"); if (!b) return;
      if (b.dataset.act === "copy") { navigator.clipboard && navigator.clipboard.writeText(text); say(t("已复制")); close(); return; }
      if (b.dataset.act === "clip") {
        const r = await post("/api/clips", { text, paper_id: pid, source: card ? "card" : inChat ? "chat" : "page",
                                             url: location.pathname + location.search });
        say(r.ok ? t("已收藏到收藏簿") : (r.message || t("操作失败"))); close(); return;
      }
      format(b.dataset.hl ? "highlight" : b.dataset.fc ? "color" : b.dataset.fmt, b.dataset.hl || b.dataset.fc);
    });
  }, true);
})();
