// Evidence gaps under a research question, and "this paper may answer your question" reminders.
(function () {
  const csrf = document.body.dataset.csrf;
  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ csrf, ...body }) });
    return r.json().catch(() => ({ ok: false, message: "HTTP " + r.status }));
  }
  const say = m => (window.pmToast ? window.pmToast(m) : alert(m));
  function refresh(el) {
    const panel = el && el.closest(".panel");
    if (panel && window.pmOpenPaper) window.pmOpenPaper(panel.dataset.paper, false);
    else location.reload();
  }
  const foundMsg = n => n ? `${t("在库里找到")} ${n} ${t("篇可能相关的论文")}` : t("库里暂时没有匹配的论文；以后导入新论文时会自动对照");

  // highlight the matched terms inside each passage (text nodes only, so nothing is parsed as HTML)
  function mark(root) {
    root.querySelectorAll("[data-ghit]").forEach(card => {
      const terms = [...card.querySelectorAll("code")].map(c => c.textContent).filter(Boolean);
      if (!terms.length) return;
      const re = new RegExp("(" + terms.map(x => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "gi");
      card.querySelectorAll(".ghit-passage").forEach(a => {
        if (a.dataset.marked) return; a.dataset.marked = "1";
        [...a.childNodes].filter(n => n.nodeType === 3).forEach(n => {
          const parts = n.textContent.split(re); if (parts.length < 2) return;
          const frag = document.createDocumentFragment();
          parts.forEach((x, i) => { if (i % 2) { const m = document.createElement("mark"); m.textContent = x; frag.append(m); } else frag.append(x); });
          n.replaceWith(frag);
        });
      });
    });
  }
  mark(document);
  new MutationObserver(() => mark(document)).observe(document.body, { childList: true, subtree: true });

  // reminders
  document.addEventListener("click", async e => {
    const read = e.target.closest("[data-ghit-read]");
    if (read) {  // reading a passage counts as having seen the reminder
      const card = read.closest("[data-ghit]");
      if (card.dataset.status === "new") post(`/api/gap-hits/${card.dataset.ghit}`, { action: "seen" });
      return;
    }
    const b = e.target.closest("[data-gact]"); if (!b) return;
    const card = b.closest("[data-ghit]");
    b.disabled = true;
    const j = await post(`/api/gap-hits/${card.dataset.ghit}`, { action: b.dataset.gact });
    b.disabled = false;
    if (!j.ok) { say(j.message || t("操作失败")); return; }
    if (b.dataset.gact === "update") {
      say(t("模型正在读全文对照证据，约半分钟，完成后出现在论文的「变化」标签"));
      if (window.pmStartPolling) window.pmStartPolling();
    }
    if (b.dataset.gact === "answered") say(t("已标记：这个缺口已补上"));
    refresh(card);
  });

  // gaps on the evidence notebook
  const box = document.getElementById("gaps"); if (!box) return;
  const qid = box.dataset.question;
  const newForm = box.querySelector("[data-gap-form]");
  box.querySelector("[data-gap-new]").addEventListener("click", () => { newForm.hidden = false; newForm.text.focus(); });
  box.addEventListener("click", async e => {
    if (e.target.closest("[data-gap-cancel]")) { e.target.closest("form").hidden = true; return; }
    const b = e.target.closest("[data-gap-act]"); if (!b) return;
    const gap = b.closest("[data-gap]"), gid = gap.dataset.gap, act = b.dataset.gapAct;
    if (act === "terms") { const f = gap.querySelector("[data-gap-terms]"); f.hidden = false; f.terms.focus(); return; }
    if (act === "delete" && !confirm(t("删除这个缺口和它的提醒？"))) return;
    b.disabled = true;
    const body = act === "regen" ? { action: "edit", regenerate: true } : { action: act };
    if (act === "regen") say(t("正在生成检索词…"));
    const j = await post(`/api/gaps/${gid}`, body);
    b.disabled = false;
    if (!j.ok) { say(j.message || t("操作失败")); return; }
    if (act === "regen" || act === "reopen") say(foundMsg(j.found));
    location.reload();
  });
  box.addEventListener("submit", async e => {
    e.preventDefault();
    const f = e.target, btn = f.querySelector("[type=submit]");
    btn.disabled = true;
    let j;
    if (f.matches("[data-gap-form]")) {
      if (!f.terms.value.trim()) say(t("正在生成检索词…"));
      j = await post(`/api/evidence/questions/${qid}/gaps`, { text: f.text.value, terms: f.terms.value });
    } else {
      j = await post(`/api/gaps/${f.closest("[data-gap]").dataset.gap}`, { action: "edit", terms: f.terms.value });
    }
    btn.disabled = false;
    if (!j.ok) { say(j.message || t("保存失败")); return; }
    say(foundMsg(j.found));
    setTimeout(() => location.reload(), 600);
  });
})();
