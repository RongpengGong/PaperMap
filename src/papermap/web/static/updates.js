// Update cards ("what does this paper change"): start one, and confirm / rewrite / keep / dismiss.
(function () {
  const csrf = document.body.dataset.csrf;
  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ csrf, ...body }) });
    return r.json().catch(() => ({ ok: false, message: "HTTP " + r.status }));
  }
  const say = m => (window.pmToast ? window.pmToast(m) : alert(m));
  function refresh(card) {
    const panel = card && card.closest(".panel");
    if (panel && window.pmOpenPaper) window.pmOpenPaper(panel.dataset.paper, false);
    else location.reload();
  }

  document.addEventListener("change", async e => {
    const sel = e.target.closest("[data-update-q]"); if (!sel || !sel.value) return;
    const q = sel.value; sel.value = "";
    const panel = sel.closest(".panel");
    const j = await post(`/paper/${panel.dataset.paper}/update`, { question_id: q });
    if (!j.ok) { say(j.message || t("无法开始")); return; }
    say(t("模型正在读全文对照证据，约半分钟，完成后出现在这里"));
    refresh(sel);
    if (window.pmStartPolling) window.pmStartPolling();
  });

  document.addEventListener("click", async e => {
    const b = e.target.closest("[data-uact]"); if (!b) return;
    const card = b.closest("[data-ucard]"), form = card.querySelector("[data-urewrite]");
    const act = b.dataset.uact;
    if (act === "edit") { form.hidden = false; form.querySelector("textarea").focus(); return; }
    if (act === "cancel") { form.hidden = true; return; }
    b.disabled = true;
    const j = await post(`/api/updates/${card.dataset.ucard}`, { action: act });
    b.disabled = false;
    if (!j.ok) { say(j.message || t("操作失败")); return; }
    if (j.added) say(`${t("已确认，")}${j.added} ${t("条本文原句存进了证据笔记")}`);
    refresh(card);
  });

  document.addEventListener("submit", async e => {
    const form = e.target.closest("[data-urewrite]"); if (!form) return;
    e.preventDefault();
    const card = form.closest("[data-ucard]");
    const j = await post(`/api/updates/${card.dataset.ucard}`, { action: "rewrite",
      text: form.elements.text.value, outcome: form.elements.outcome.value });
    if (!j.ok) { say(j.message || t("保存失败")); return; }
    if (j.added) say(`${t("已确认，")}${j.added} ${t("条本文原句存进了证据笔记")}`);
    refresh(card);
  });
})();
