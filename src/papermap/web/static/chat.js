// AI discussion tab in the paper panel: chat with a chosen model, ask about a PDF selection,
// end with a summary the user edits and then appends to their notes.
(() => {
  const csrf = document.body.dataset.csrf;
  const t = s => (window.t ? window.t(s) : s);
  const say = m => (window.pmToast ? window.pmToast(m) : alert(m));
  const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ csrf, ...body }) })
    .then(r => r.json()).catch(() => ({ ok: false, message: t("网络错误") }));
  const esc = s => String(s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const MODEL_PREF = "pm_chat_model";

  function init(panel) {
    const box = panel.querySelector("[data-chat]");
    if (!box || box.dataset.ready) return;
    box.dataset.ready = "1";
    const pid = box.dataset.chat, isPage = "chatPage" in box.dataset;
    const key = location.pathname;
    const notesEl = document.querySelector("[data-kb-notes]"), rel = notesEl ? notesEl.dataset.kbNotes : "";
    const base = isPage ? "/api/page-chat" : `/api/paper/${pid}/chat`;
    const pageText = () => {
      const main = document.getElementById("main_content"); if (!main) return "";
      const c = main.cloneNode(true);
      c.querySelectorAll("script, style, .notice, textarea, select, .pm-toolbar").forEach(n => n.remove());
      return (c.innerText || c.textContent || "").replace(/\n{3,}/g, "\n\n").slice(0, 40000);
    };
    const extra = () => (isPage ? { key, title: document.title.replace(/ - PaperMap$/, ""), page_text: pageText(), rel } : {});
    const log = box.querySelector("[data-chat-log]"), sel = box.querySelector("[data-chat-model]");
    const input = box.querySelector("[data-chat-text]"), sendBtn = box.querySelector("[data-chat-send]");
    const quoteBox = box.querySelector("[data-chat-quote]"), quoteText = box.querySelector("[data-chat-quote-text]");
    const sumBox = box.querySelector("[data-chat-summary]"), sumText = box.querySelector("[data-chat-summary-text]");
    let quote = null, loaded = false, count = 0;
    if (isPage) {
      input.placeholder = t("问问这个页面的内容…… ⌘↩ 发送");
      const pv = box.querySelector(".chat-privacy");
      if (pv) pv.textContent = t("当前页面的文字（必要时加上文献库概况）会发给所选模型的服务商；费用计入费用页。");
    }

    const busy = on => { box.classList.toggle("chat-busy", on); sendBtn.disabled = on; };
    const scroll = () => { const s = isPage ? box : panel.querySelector(".side-scroll"); if (s) s.scrollTop = s.scrollHeight; };

    function bubble(m) {
      const el = document.createElement("div");
      el.className = "chat-msg " + m.role;
      if (m.role === "user") {
        el.innerHTML = (m.quote ? `<blockquote class="chat-q">${m.page ? `<a href="#" class="chat-page" data-page="${+m.page}">p.${+m.page}</a> ` : ""}${esc(m.quote)}</blockquote>` : "")
          + `<div>${esc(m.content).replace(/\n/g, "<br>")}</div>`;
      } else if (m.role === "assistant") {
        el.innerHTML = `<div class="md">${m.html}</div><div class="chat-meta muted small">${esc(m.model || "")}</div>`;
      } else {
        el.innerHTML = `<div class="err-text small">${esc(m.content)}</div>`;
      }
      log.appendChild(el); count += m.role === "assistant" ? 1 : 0;
      return el;
    }
    function empty() {
      if (isPage) {
        log.innerHTML = `<div class="chat-empty muted small"><p>${t("问问这个页面：比如这个学科的主要分歧在哪、这些证据能不能支持某个结论、下一步该读什么……")}</p>`
          + `<p>${t("模型会读当前页面的内容（页面文字少时再加上文献库的学科和论文概况）。讨论完可以总结写进这个页面的「我的笔记」；没有笔记区的页面会存到 knowledge/discussions/。")}</p></div>`;
        return;
      }
      log.innerHTML = `<div class="chat-empty muted small"><p>${t("一边读一边问：这篇的设计为什么这样、某个结果怎么理解、和你库里别的论文有什么不同……")}</p>`
        + `<p>${t("回答会尽量标出页码，点页码会跳到 PDF 对应位置。讨论完点「结束讨论，总结到笔记」，你看过、改过之后才会写进笔记。")}</p></div>`;
    }

    async function load() {
      if (loaded) return; loaded = true;
      const j = await fetch(isPage ? `${base}?key=${encodeURIComponent(key)}` : base).then(r => r.json()).catch(() => null);
      if (!j || !j.ok) { log.textContent = t("读取对话失败"); return; }
      const pref = localStorage.getItem(MODEL_PREF);
      sel.innerHTML = j.models.map(m => `<option value="${esc(m.key)}" ${m.ready ? "" : "disabled"}>${esc(m.label)}${m.ready ? "" : " — " + esc(t("未配置"))}</option>`).join("");
      const keys = j.models.filter(m => m.ready).map(m => m.key);
      sel.value = keys.includes(j.model) ? j.model : keys.includes(pref) ? pref : (keys[0] || "main");
      log.innerHTML = ""; count = 0;
      if (!j.messages.length) empty(); else j.messages.forEach(bubble);
      scroll();
    }
    sel.addEventListener("change", () => { try { localStorage.setItem(MODEL_PREF, sel.value); } catch (e) { /* private mode */ } });

    async function send() {
      const text = input.value.trim();
      if ((!text && !quote) || box.classList.contains("chat-busy")) return;
      if (!count && log.querySelector(".chat-empty")) log.innerHTML = "";
      const u = bubble({ role: "user", content: text || t("请解释这段。"), quote: quote && quote.text, page: quote && quote.page });
      const wait = document.createElement("div"); wait.className = "chat-msg assistant pending"; wait.innerHTML = `<span class="chat-dots"><i></i><i></i><i></i></span>`;
      log.appendChild(wait); scroll();
      const body = { text, model: sel.value, quote: quote ? quote.text : "", page: quote ? quote.page : null, ...extra() };
      input.value = ""; setQuote(null); busy(true);
      const j = await post(base, body);
      busy(false); wait.remove();
      if (!j.ok) { u.remove(); input.value = text; if (body.quote) setQuote({ text: body.quote, page: body.page }); bubble({ role: "error", content: j.message || t("发送失败") }); scroll(); return; }
      bubble(j.reply); scroll();
    }
    sendBtn.addEventListener("click", send);
    input.addEventListener("keydown", e => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send(); } });

    function setQuote(q) {
      quote = q && q.text ? { text: q.text.slice(0, 4000), page: q.page || null } : null;
      quoteBox.hidden = !quote;
      if (quote) quoteText.textContent = (quote.page ? `p.${quote.page} · ` : "") + quote.text;
    }
    box.querySelector("[data-chat-quote-x]").addEventListener("click", () => setQuote(null));

    box.querySelector("[data-chat-new]").addEventListener("click", async () => {
      if (count && !confirm(t("清空这次对话？还没写进笔记的内容会丢失。"))) return;
      const j = await post(`${base}/clear`, extra());
      if (j.ok) { log.innerHTML = ""; count = 0; empty(); sumBox.hidden = true; }
    });

    box.querySelector("[data-chat-end]").addEventListener("click", async () => {
      if (!count) { say(t("还没有讨论内容")); return; }
      const btn = box.querySelector("[data-chat-end]"); btn.disabled = true; btn.textContent = t("正在总结…");
      const j = await post(`${base}/summary`, { model: sel.value, ...extra() });
      btn.disabled = false; btn.textContent = t("结束讨论，总结到笔记");
      if (!j.ok) { say(j.message || t("总结失败")); return; }
      sumText.value = j.text; sumBox.hidden = false; sumText.focus(); sumBox.scrollIntoView({ block: "nearest" });
    });
    box.querySelector("[data-chat-summary-cancel]").addEventListener("click", () => { sumBox.hidden = true; });
    box.querySelector("[data-chat-write]").addEventListener("click", async () => {
      const clear = box.querySelector("[data-chat-clear]").checked;
      const j = await post(`${base}/notes`, { ...extra(), text: sumText.value, clear });
      if (!j.ok) { say(j.message || t("写入失败")); return; }
      sumBox.hidden = true;
      // keep the notes tab in step with the file we just appended to
      const ta = panel.querySelector("textarea.notes"), live = panel.querySelector("[data-notes-live]");
      if (ta) ta.value = j.notes;
      if (live && live._pmEditor) live._pmEditor.commands.setContent(j.notes, { contentType: "markdown", emitUpdate: false });
      if (clear) { log.innerHTML = ""; count = 0; empty(); }
      if (isPage && j.where && !rel) { say(t("已保存到") + " knowledge/" + j.where); return; }
      say(t("已写入「我的笔记」"));
      if (isPage && rel) { try { sessionStorage.setItem("pm_ai_open", "1"); } catch (e) { /* */ } location.reload(); }
    });

    // [p.N] citations jump the PDF next to the panel
    box.addEventListener("click", e => {
      const a = e.target.closest(".chat-page"); if (!a) return;
      e.preventDefault(); goto(panel, +a.dataset.page);
    });

    box._pmAsk = q => { setQuote(q); input.focus(); };
    box._pmLoad = load;
    if (!box.hidden) load();
  }

  function goto(panel, page) {
    const frame = panel.querySelector(".pdf-col iframe");
    if (!frame || !page) return;
    if (panel.classList.contains("pdf-hidden")) { const b = panel.querySelector('[data-action="toggle-pdf"]'); if (b) b.click(); }
    const w = frame.contentWindow;
    if (w && w.pmGotoPage) w.pmGotoPage(page - 1);
    else frame.src = `${(frame.dataset.src || "").split("&page=")[0]}&page=${page}`;
  }

  // The tab loads its conversation the first time it is shown.
  document.addEventListener("click", e => {
    const tab = e.target.closest('.tab[data-tab="chat"]'); if (!tab) return;
    const box = tab.closest(".panel") && tab.closest(".panel").querySelector("[data-chat]");
    if (box && box._pmLoad) box._pmLoad();
  });

  // Called by the embedded PDF reader: "Ask AI" about the selected passage.
  window.pmChatAsk = (paperId, text, page) => {
    const panel = document.querySelector(`.panel[data-paper="${paperId}"]`) || document.querySelector(".panel");
    if (!panel) return false;
    const tab = panel.querySelector('.tab[data-tab="chat"]'); if (!tab) return false;
    tab.click();
    const box = panel.querySelector("[data-chat]");
    if (box && box._pmAsk) { box._pmLoad(); box._pmAsk({ text, page }); }
    return true;
  };

  // The right-hand drawer on every other page (evidence notes, Q&A, map, fields, handbooks…)
  const drawer = document.getElementById("ai_drawer"), fab = document.getElementById("ai_fab");
  if (drawer && fab) {
    const box = drawer.querySelector("[data-chat]");
    const open = on => {
      drawer.hidden = !on; fab.hidden = on; document.body.classList.toggle("ai-open", on);
      try { localStorage.setItem("pm_ai_drawer", on ? "1" : "0"); } catch (e) { /* private mode */ }
      if (on) { init(drawer); box._pmLoad(); setTimeout(() => drawer.querySelector("[data-chat-text]").focus(), 50); }
    };
    fab.addEventListener("click", () => open(true));
    document.getElementById("ai_drawer_close").addEventListener("click", () => open(false));
    let again = false;
    try { again = sessionStorage.getItem("pm_ai_open") === "1" || localStorage.getItem("pm_ai_drawer") === "1"; sessionStorage.removeItem("pm_ai_open"); } catch (e) { /* */ }
    if (again) open(true);
  }

  window.pmChatInit = init;
  const first = document.querySelector(".panel");
  if (first) init(first);
})();
