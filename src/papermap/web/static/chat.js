// The paper and page drawers share a composer and retain their own conversation histories.
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
    const effSel = box.querySelector("[data-chat-effort]");
    let models = [];
    const EFFORT_NAMES = { minimal: t("最低"), low: t("低"), medium: t("中"), high: t("高"), xhigh: t("很高"), max: t("最高"), ultra: t("极高") };
    function fillEfforts(want) {
      const m = models.find(x => x.key === sel.value) || {};
      const list = m.efforts || [];
      effSel.hidden = !list.length;
      effSel.innerHTML = `<option value="">${esc(t("默认强度"))}${m.effort ? " (" + esc(EFFORT_NAMES[m.effort] || m.effort) + ")" : ""}</option>`
        + list.map(e => `<option value="${esc(e)}">${esc(EFFORT_NAMES[e] ? `${EFFORT_NAMES[e]} · ${e}` : e)}</option>`).join("");
      effSel.value = list.includes(want) ? want : "";
    }
    const effort = () => (effSel.hidden ? "" : effSel.value);
    const input = box.querySelector("[data-chat-text]"), sendBtn = box.querySelector("[data-chat-send]");
    const quoteBox = box.querySelector("[data-chat-quote]"), quoteText = box.querySelector("[data-chat-quote-text]");
    const sumBox = box.querySelector("[data-chat-summary]"), sumText = box.querySelector("[data-chat-summary-text]");
    let quote = null, loaded = false, loading, count = 0;
    if (isPage) {
      const pv = box.querySelector(".chat-privacy");
      if (pv) pv.title = t("当前页面的文字（必要时加上文献库概况）会发给所选模型的服务商；费用计入费用页。");
    }

    const busy = on => { box.classList.toggle("chat-busy", on); sendBtn.disabled = on; };
    const scroll = () => { box.scrollTop = box.scrollHeight; };

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

    function load() {
      if (loaded) return true;
      if (!loading) loading = readChat().finally(() => { loading = null; });
      return loading;
    }
    async function readChat() {
      const j = await fetch(isPage ? `${base}?key=${encodeURIComponent(key)}` : base).then(r => r.json()).catch(() => null);
      if (!j || !j.ok) { log.textContent = t("读取对话失败"); return false; }
      const pref = localStorage.getItem(MODEL_PREF);
      sel.innerHTML = j.models.map(m => `<option value="${esc(m.key)}" ${m.ready ? "" : "disabled"}>${esc(m.label)}${m.ready ? "" : " — " + esc(t("未配置"))}</option>`).join("");
      const keys = j.models.filter(m => m.ready).map(m => m.key);
      sel.value = keys.includes(j.model) ? j.model : keys.includes(pref) ? pref : (keys[0] || "main");
      models = j.models; fillEfforts(j.effort || localStorage.getItem(MODEL_PREF + "_effort") || "");
      log.innerHTML = ""; count = 0;
      if (!j.messages.length) empty(); else j.messages.forEach(bubble);
      showArchive(j.archive || []);
      scroll();
      loaded = true;
      return true;
    }
    sel.addEventListener("change", () => { try { localStorage.setItem(MODEL_PREF, sel.value); } catch (e) { /* private mode */ } fillEfforts(effSel.value); });
    effSel.addEventListener("change", () => { try { localStorage.setItem(MODEL_PREF + "_effort", effSel.value); } catch (e) { /* private mode */ } });

    async function send() {
      if (!loaded && !(await load())) return;
      const text = input.value.trim();
      if ((!text && !quote) || box.classList.contains("chat-busy")) return;
      if (!count && log.querySelector(".chat-empty")) log.innerHTML = "";
      const u = bubble({ role: "user", content: text || t("请解释这段。"), quote: quote && quote.text, page: quote && quote.page });
      const wait = document.createElement("div"); wait.className = "chat-msg assistant pending"; wait.innerHTML = `<span class="chat-dots"><i></i><i></i><i></i></span>`;
      log.appendChild(wait); scroll();
      const body = { text, model: sel.value, quote: quote ? quote.text : "", page: quote ? quote.page : null, effort: effort(), ...extra() };
      input.value = ""; setQuote(null); busy(true);
      const j = await post(base, body);
      busy(false); wait.remove();
      if (!j.ok) { u.remove(); input.value = text; if (body.quote) setQuote({ text: body.quote, page: body.page }); bubble({ role: "error", content: j.message || t("发送失败") }); scroll(); return; }
      bubble(j.reply); scroll();
    }
    sendBtn.addEventListener("click", send);
    // Enter to send (Shift+Enter = new line) or ⌘/Ctrl+Enter to send; never while an input method is composing
    const enterSel = box.querySelector("[data-chat-enter]");
    const enterMode = () => { try { return localStorage.getItem("pm_chat_enter") || "enter"; } catch (e) { return "enter"; } };
    const hint = () => {
      const cmd = enterMode() === "cmd";
      input.placeholder = isPage ? t(cmd ? "问问这个页面的内容…… ⌘↩ 发送" : "问问这个页面的内容…… ↩ 发送，⇧↩ 换行")
                                 : t("输入问题，或选中卡片、原文、译文后点「问 AI」。") + " " + t(cmd ? "⌘↩ 发送" : "↩ 发送，⇧↩ 换行");
    };
    if (enterSel) {
      enterSel.value = enterMode();
      enterSel.addEventListener("change", () => {
        try { localStorage.setItem("pm_chat_enter", enterSel.value); } catch (e) { /* private mode */ }
        document.querySelectorAll("[data-chat-enter]").forEach(s => { s.value = enterSel.value; }); hint();
      });
    }
    hint();
    input.addEventListener("keydown", e => {
      if (e.key !== "Enter" || e.isComposing || e.keyCode === 229) return;
      const cmd = e.metaKey || e.ctrlKey;
      if (enterMode() === "cmd" ? cmd : (!e.shiftKey && !e.altKey && !cmd) || cmd) { e.preventDefault(); send(); }
    });

    function setQuote(q) {
      quote = q && q.text ? { text: q.text.slice(0, 4000), page: q.page || null } : null;
      quoteBox.hidden = !quote;
      if (quote) quoteText.textContent = (quote.page ? `p.${quote.page} · ` : "") + quote.text;
    }
    box.querySelector("[data-chat-quote-x]").addEventListener("click", () => setQuote(null));

    box.querySelector("[data-chat-new]").addEventListener("click", async () => {
      if (!count) return;
      const j = await post(`${base}/clear`, extra());
      if (j.ok) { log.innerHTML = ""; count = 0; empty(); sumBox.hidden = true; reloadArchive(); say(t("已开始新对话，原对话在「以前的讨论」里")); }
    });

    // earlier (ended) discussions, kept as they were
    const histBtn = box.querySelector("[data-chat-history]"), arch = box.querySelector("[data-chat-archive]");
    function showArchive(list) {
      histBtn.hidden = !list.length;
      box.querySelector("[data-chat-history-n]").textContent = list.length || "";
      arch.innerHTML = list.map(a => `<details class="chat-old"><summary><b>${esc(a.ended_at.slice(0, 16).replace("T", " "))}</b> · ${esc(a.first)} <span class="muted">（${a.n} ${esc(t("个问题"))}）</span></summary>`
        + (a.summary_html ? `<div class="chat-old-sum md">${a.summary_html}</div>` : "")
        + a.messages.map(m => m.role === "user" ? `<div class="chat-msg user"><div>${esc(m.content).replace(/\n/g, "<br>")}</div></div>`
                                                : `<div class="chat-msg assistant"><div class="md">${m.html}</div></div>`).join("") + "</details>").join("");
    }
    async function reloadArchive() {
      const j = await fetch(isPage ? `${base}?key=${encodeURIComponent(key)}` : base).then(r => r.json()).catch(() => null);
      if (j && j.ok) showArchive(j.archive || []);
    }
    histBtn.addEventListener("click", () => { arch.hidden = !arch.hidden; });

    box.querySelector("[data-chat-end]").addEventListener("click", async () => {
      if (!count) { say(t("还没有讨论内容")); return; }
      const btn = box.querySelector("[data-chat-end]"); btn.disabled = true; btn.textContent = t("正在总结并结束…");
      const j = await post(`${base}/summary`, { model: sel.value, effort: effort(), ...extra() });
      btn.disabled = false; btn.textContent = t("结束讨论");
      if (!j.ok) { say(j.message || t("总结失败")); return; }
      // the discussion has ended: start empty, show the summary to confirm
      log.innerHTML = ""; count = 0; empty(); reloadArchive();
      sumText.value = j.text; sumBox.hidden = false; sumText.focus(); sumBox.scrollIntoView({ block: "start" });
    });
    box.querySelector("[data-chat-summary-cancel]").addEventListener("click", () => { sumBox.hidden = true; });
    box.querySelector("[data-chat-write]").addEventListener("click", async () => {
      const j = await post(`${base}/notes`, { ...extra(), text: sumText.value });
      if (!j.ok) { say(j.message || t("写入失败")); return; }
      sumBox.hidden = true;
      // keep the notes tab in step with the file we just appended to
      const owner = box.closest(".panel") || panel;
      const ta = owner.querySelector("textarea.notes"), live = owner.querySelector("[data-notes-live]");
      if (ta) ta.value = j.notes;
      if (live && live._pmEditor) live._pmEditor.commands.setContent(j.notes, { contentType: "markdown", emitUpdate: false });
      if (isPage && j.where && !rel) { say(t("已保存到") + " knowledge/" + j.where); return; }
      say(t("已写入「我的笔记」"));
      if (isPage && rel) { try { sessionStorage.setItem("pm_ai_open", "1"); } catch (e) { /* */ } location.reload(); }
    });

    // [p.N] citations jump the PDF next to the panel
    box.addEventListener("click", e => {
      const a = e.target.closest(".chat-page"); if (!a) return;
      e.preventDefault(); goto(box.closest(".panel") || panel, +a.dataset.page);
    });

    box._pmAsk = q => { setQuote(q); input.focus({ preventScroll: true }); };
    box._pmLoad = load;
    if (!box.closest("[hidden]")) load();
  }

  function goto(panel, page) {
    const frame = panel.querySelector(".pdf-col iframe");
    if (!frame || !page) return;
    if (panel.classList.contains("pdf-hidden")) { const b = panel.querySelector('[data-action="toggle-pdf"]'); if (b) b.click(); }
    const w = frame.contentWindow;
    if (w && w.pmGotoPage) w.pmGotoPage(page - 1);
    else frame.src = `${(frame.dataset.src || "").split("&page=")[0]}&page=${page}`;
  }

  function showPaperChat(panel, on, focus = true) {
    const drawer = panel.querySelector("[data-paper-chat-drawer]");
    const toggle = panel.querySelector('[data-action="toggle-chat"]');
    if (!drawer || !toggle) return false;
    if (drawer.hidden === on) panel.dispatchEvent(new Event("pm:chat-layout"));
    drawer.hidden = !on;
    toggle.setAttribute("aria-expanded", String(on));
    if (on) {
      init(panel);
      drawer.querySelector("[data-chat]")._pmLoad();
      if (focus) drawer.querySelector("[data-chat-text]").focus({ preventScroll: true });
    } else if (focus) toggle.focus({ preventScroll: true });
    return true;
  }

  document.addEventListener("click", e => {
    const button = e.target.closest('[data-action="toggle-chat"], [data-action="close-chat"]');
    if (!button) return;
    const panel = button.closest(".panel");
    const drawer = panel.querySelector("[data-paper-chat-drawer]");
    showPaperChat(panel, button.dataset.action === "toggle-chat" && drawer.hidden);
  });

  // Both the comparison view and its embedded PDF quote into this paper's conversation.
  window.pmChatAsk = (paperId, text, page) => {
    const panel = document.querySelector(".panel[data-paper]");
    if (!panel || panel.dataset.paper !== String(paperId) || !showPaperChat(panel, true)) return false;
    panel.querySelector("[data-chat]")._pmAsk({ text, page });
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

  window.pmChatInit = panel => {
    const drawer = panel.querySelector("[data-paper-chat-drawer]");
    if (!drawer) return;
    // Migrate the old discussion tab to an open drawer, with a reading tab underneath it.
    let legacy = false;
    try {
      legacy = localStorage.getItem("pm_side_tab") === "chat";
      if (legacy) localStorage.setItem("pm_side_tab", "card");
    } catch (_) { /* Storage can be disabled. */ }
    showPaperChat(panel, legacy || !drawer.hidden, false);
  };
  const first = document.querySelector(".panel");
  if (first) window.pmChatInit(first);
})();
