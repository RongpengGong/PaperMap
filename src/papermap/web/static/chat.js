// The paper and page drawers share a composer and retain their own conversation histories.
// "ChatGPT 订阅 · gpt-6.2-sol 约 20 次 · DeepSeek … 约 6 次（$0.01）": subscriptions use quota, paid models money
window.pmSquadCost = j => {
  const t = s => (window.t ? window.t(s) : s);
  const rows = (j.by_model || []).map(r => r.billing === "subscription"
    ? t("{m} 约 {n} 次").replace("{m}", r.label).replace("{n}", r.calls)
    : t("{m} 约 {n} 次（${usd}）").replace("{m}", r.label).replace("{n}", r.calls).replace("{usd}", r.usd.toFixed(3)));
  return t("预计") + "：" + rows.join(" · ") + (j.usd > 0 ? " · " + t("合计约 ${usd}").replace("{usd}", j.usd.toFixed(3)) : "");
};
(() => {
  const csrf = document.body.dataset.csrf;
  const t = s => (window.t ? window.t(s) : s);
  const say = m => (window.pmToast ? window.pmToast(m) : alert(m));
  const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ csrf, ...body }) })
    .then(r => r.json()).catch(() => ({ ok: false, message: t("网络错误") }));
  const esc = s => String(s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const MODEL_PREF = "pm_chat_model";
  const figureLabel = f => t("附图：{figure}").replace("{figure}", f.label) + ` · p.${f.page}`;
  const figureTag = f => f ? `<div class="chat-figure">${esc(figureLabel(f))}</div>` : "";

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
    const figureNote = box.querySelector("[data-chat-figure]");
    const privacy = box.querySelector(".chat-privacy"), paperPrivacy = privacy.title;
    const drafts = new Map();
    const draftKey = f => f ? `${f.id}:${f.revision}` : "";
    let quote = null, figure = null, loaded = false, loading, count = 0;
    if (isPage) {
      const pv = box.querySelector(".chat-privacy");
      if (pv) pv.title = t("当前页面的文字（必要时加上文献库概况）会发给所选模型的服务商；费用计入费用页。");
    }

    const busy = on => { box.classList.toggle("chat-busy", on); sendBtn.disabled = on; };
    const scroll = () => { box.scrollTop = box.scrollHeight; };

    function bubble(m) {
      const el = document.createElement("div");
      el.className = "chat-msg " + m.role;
      if (m.role === "squad") {
        el.className = "chat-msg assistant chat-squad-card";
        el.dataset.run = m.run_id;
        el.innerHTML = `<div class="muted small">${esc(t("智能体小队"))} · …</div>`;
        log.appendChild(el); count += 1; watchSquad(el);
        return el;
      }
      if (m.role === "user") {
        el.innerHTML = figureTag(m.figure) + (m.squad_to ? `<div class="chat-squad-tag small">${esc(t("交给小队"))}</div>` : "") + (m.quote ?`<blockquote class="chat-q">${m.page ? `<a href="#" class="chat-page" data-page="${+m.page}">p.${+m.page}</a> ` : ""}${esc(m.quote)}</blockquote>` : "")
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
      const attachment = figure, text = input.value.trim();
      if (!text && !quote && !attachment) return;
      if (!loaded && !(await load())) return;
      if (attachment !== figure || text !== input.value.trim() || box.classList.contains("chat-busy")) return;
      if (squadOn()) { squadAsk(text); return; }
      if (!count && log.querySelector(".chat-empty")) log.innerHTML = "";
      const question = text || (attachment ? t("请解释这张图。") : t("请解释这段。"));
      const u = bubble({ role: "user", content: question, figure: attachment, quote: quote && quote.text, page: quote && quote.page });
      const wait = document.createElement("div"); wait.className = "chat-msg assistant pending"; wait.innerHTML = `<span class="chat-dots"><i></i><i></i><i></i></span>`;
      log.appendChild(wait); scroll();
      const body = { text: attachment ? question : text, model: sel.value, quote: quote ? quote.text : "", page: quote ? quote.page : null, effort: effort(), ...extra() };
      if (attachment) { body.figure_id = attachment.id; body.figure_revision = attachment.revision; }
      input.value = ""; setQuote(null); busy(true);
      const j = await post(base, body);
      busy(false); wait.remove();
      if (!j.ok) {
        u.remove();
        const savedQuote = body.quote ? { text: body.quote, page: body.page } : null;
        if (draftKey(attachment) === draftKey(figure) && !input.value) { input.value = text; setQuote(savedQuote); }
        else if (!drafts.get(draftKey(attachment))?.text) drafts.set(draftKey(attachment), { text, quote: savedQuote });
        bubble({ role: "error", content: j.message || t("发送失败") }); scroll(); return;
      }
      const r = bubble(j.reply);
      if (squadBtn && !squadBtn.hidden && (BIG.test(text) || (!isPage && DEEP.test(text)))) {
        const tip = document.createElement("div");
        tip.className = "chat-squad-hint small";
        tip.innerHTML = esc(t("这个问题适合交给智能体小队：几名成员分头查库、精读、挑错，再写成带出处的报告。"))
          + ` <button type="button" class="link">${esc(t("交给小队试试"))}</button>`;
        tip.querySelector("button").addEventListener("click", () => { setSquad(true); input.value = text; squadAsk(text); tip.remove(); });
        r.appendChild(tip);
      }
      scroll();
    }

    // ---- agent squad from the page drawer: preview with cost, confirm, then a live card in the log
    const squadUrl = isPage ? "/api/page-chat/squad" : `/api/paper/${pid}/chat/squad`;
    const DEEP = /精读|站得住|可信|局限|方法.*(对|靠谱)|设计.*(合理|问题)|和我.*库|robust|limitation|critique/i;
    const BIG = /比较|对比|综述|梳理|核对|汇总|系统地|所有|哪些研究|哪些论文|compare|review|systematic|which (studies|papers)|all (studies|papers)/i;
    const squadBtn = box.querySelector("[data-chat-squad]"), sqBar = box.querySelector("[data-chat-squad-confirm]");
    const sqPlan = box.querySelector("[data-chat-squad-plan]"), sqCap = box.querySelector("[data-chat-squad-cap]");
    let sqPending = "";
    const squadOn = () => !!squadBtn && !squadBtn.hidden && squadBtn.getAttribute("aria-pressed") === "true";
    function setSquad(on) {
      if (!squadBtn) return;
      squadBtn.setAttribute("aria-pressed", String(on));
      box.classList.toggle("squad-mode", on);
      try { localStorage.setItem("pm_chat_squad", on ? "1" : "0"); } catch (e) { /* private mode */ }
      if (!on) { sqBar.hidden = true; sqPending = ""; }
      hint();
    }
    const squadBody = (text, more) => ({ text, search: location.search, ...extra(), ...more });
    async function squadAsk(text) {
      busy(true);
      const j = await post(squadUrl, squadBody(text, { confirm: false }));
      busy(false);
      if (!j.ok) { say(j.message || t("发送失败")); return; }
      if (j.followed) {  // a squad is working here: this goes to its lead
        input.value = "";
        if (!count && log.querySelector(".chat-empty")) log.innerHTML = "";
        bubble({ role: "user", content: text, squad_to: j.followed }); scroll();
        say(t("已转给队长，它会在下一轮处理"));
        return;
      }
      sqPending = text;
      const scope = j.questions.length ? `${esc(t("研究问题"))}：${j.questions.map(esc).join("；")}`
        : j.field ? `${esc(t("学科"))}：${esc(j.field)}` : "";
      const cost = window.pmSquadCost(j);
      const simple = text.length < 24 && !BIG.test(text) && !DEEP.test(text);
      sqPlan.innerHTML = (simple ? `<div class="chat-squad-simple">${esc(t("这个问题可能普通回答就够了，不一定需要小队。"))}</div>` : "")
        + `<b>${esc(j.label)}</b> · ${j.members.map(esc).join(" · ")}<br>${esc(j.description)}`
        + (scope ? `<br>${scope}` : "") + `<br><span class="muted">${esc(cost)}</span>`;
      if (j.cap_usd !== undefined) sqCap.value = j.cap_usd;
      sqBar.hidden = false; scroll();
    }
    const MARK = { queued: "○", running: "◔", done: "●", error: "✕", cancelled: "–" };
    async function watchSquad(el) {
      const j = await fetch(`/api/squads/${el.dataset.run}/brief`).then(r => r.json()).catch(() => null);
      if (!el.isConnected) return;
      if (!j || !j.ok) { el.innerHTML = `<div class="muted small">${esc(t("小队记录已不存在"))}</div>`; return; }
      const active = ["queued", "running", "stopping"].includes(j.status);
      const tasks = j.tasks.map(x => `<li class="sq-t-${esc(x.status)}"><span>${MARK[x.status] || "·"}</span> <b>${esc(x.member)}</b> ${esc(x.title)}</li>`).join("");
      const money = `$${j.spent_usd.toFixed(3)}` + (j.cap_usd > 0 ? ` / $${j.cap_usd.toFixed(2)}` : "");
      let html = `<div class="chat-squad-head small"><b>✦ ${esc(t("智能体小队"))}</b> · ${esc(j.template_label)}`
        + ` <span class="chip sq-${esc(j.status)}">${esc(j.status_label)}</span> <span class="muted">${money}</span></div>`;
      if (j.message) html += `<div class="muted small">${esc(j.message)}</div>`;
      if (j.result_html) html += `<div class="md">${j.result_html}</div>`;
      else html += tasks ? `<ul class="chat-squad-tasks small">${tasks}</ul>` : `<div class="muted small">${esc(t("队长正在拆分任务…"))}</div>`;
      html += `<div class="chat-squad-foot small"><a href="/squad/${+j.id}">${esc(t("打开任务板"))}</a>`;
      if (j.pending) html += ` · <a href="/squad/${+j.id}#proposals"><b>${esc(t("有 {n} 条提议等你确认。").replace("{n}", j.pending))}</b></a>`;
      if (active) html += ` · <span class="muted">${esc(t("在这里继续输入，会转给队长"))}</span>`;
      el.innerHTML = html + "</div>";
      if (active) setTimeout(() => watchSquad(el), 2500);
    }
    sendBtn.addEventListener("click", send);
    // Enter to send (Shift+Enter = new line) or ⌘/Ctrl+Enter to send; never while an input method is composing
    const enterSel = box.querySelector("[data-chat-enter]");
    const enterMode = () => { try { return localStorage.getItem("pm_chat_enter") || "enter"; } catch (e) { return "enter"; } };
    const hint = () => {
      const cmd = enterMode() === "cmd";
      if (figure) { input.placeholder = t("想问这张图的哪个部分？"); return; }
      if (box.classList.contains("squad-mode")) {
        input.placeholder = isPage ? t("把一个大问题交给小队，例如：比较这些研究对成纤维细胞作用的结论")
                                   : t("让小队精读这篇，例如：这篇的结论站得住吗？和我库里的研究有什么不同？");
        return;
      }
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
    if (squadBtn && !document.body.classList.contains("remote-touch")) {
      squadBtn.hidden = false;
      squadBtn.addEventListener("click", () => setSquad(!squadOn()));
      let saved = false;
      try { saved = localStorage.getItem("pm_chat_squad") === "1"; } catch (e) { /* private mode */ }
      if (saved) setSquad(true);
      box.querySelector("[data-chat-squad-cancel]").addEventListener("click", () => { sqBar.hidden = true; sqPending = ""; });
      box.querySelector("[data-chat-squad-go]").addEventListener("click", async () => {
        const text = sqPending || input.value.trim();
        if (!text || box.classList.contains("chat-busy")) return;
        busy(true);
        const j = await post(squadUrl, squadBody(text, { confirm: true, cap_usd: sqCap.value }));
        busy(false);
        if (!j.ok) { say(j.message || t("发送失败")); return; }
        sqBar.hidden = true; sqPending = ""; input.value = "";
        if (!count && log.querySelector(".chat-empty")) log.innerHTML = "";
        bubble({ role: "user", content: text, squad_to: j.id });
        bubble({ role: "squad", run_id: j.id }); scroll();
      });
    }
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
        + a.messages.map(m => m.role === "user" ? `<div class="chat-msg user">${figureTag(m.figure)}<div>${esc(m.content).replace(/\n/g, "<br>")}</div></div>`
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
      e.preventDefault();
      if (figure) { location.href = `/reader/${pid}?page=${+a.dataset.page}`; return; }
      goto(box.closest(".panel") || panel, +a.dataset.page);
    });

    box._pmFigure = f => {
      const next = f ? { id: Number(f.id), revision: f.revision, label: f.label, page: Number(f.page) + 1 } : null;
      if (draftKey(next) === draftKey(figure)) return;
      const key = draftKey(figure);
      if (input.value || quote) drafts.set(key, { text: input.value, quote });
      else drafts.delete(key);
      figure = next;
      const draft = drafts.get(draftKey(figure));
      input.value = draft?.text || ""; setQuote(draft?.quote);
      figureNote.hidden = !figure;
      figureNote.textContent = figure ? figureLabel(figure) : "";
      privacy.title = figure ? t("提问时会把当前图片、图注和已有讲解，连同论文正文、卡片和笔记发给所选模型。") : paperPrivacy;
      hint();
    };
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
      drawer.querySelector("[data-chat]")?._pmLoad();
      if (focus) drawer.querySelector("[data-chat-text]")?.focus({ preventScroll: true });
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

  // Use the actual paper chat when available so questions, drafts and in-flight
  // replies stay in one place. The gallery mounts the same composer for its paper.
  const figureHost = document.getElementById("lb_chat");
  const galleryBoxes = new Map();
  let mounted = null;
  window.pmFigureChat = f => {
    if (!figureHost) return;
    if (mounted && (!f || mounted.box.dataset.chat !== String(f.paper_id))) {
      mounted.box._pmFigure(null);
      if (mounted.home) mounted.home.append(mounted.box);
      else mounted.box.remove();
      mounted = null;
    }
    if (!f) return;
    if (!mounted) {
      const panel = document.querySelector(`.panel[data-paper="${Number(f.paper_id)}"]`);
      let box = panel?.querySelector("[data-paper-chat-drawer] [data-chat]");
      const home = box?.parentNode;
      if (box) init(panel);
      else {
        const pid = String(f.paper_id);
        box = galleryBoxes.get(pid);
        if (!box) {
          box = document.getElementById("lb_chat_template").content.firstElementChild.cloneNode(true);
          box.dataset.chat = pid;
          galleryBoxes.set(pid, box);
        }
      }
      figureHost.append(box);
      if (!home) init(figureHost);
      mounted = { box, home };
    }
    mounted.box._pmFigure(f);
    mounted.box._pmLoad();
    mounted.box.querySelector("[data-chat-text]").focus({ preventScroll: true });
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
