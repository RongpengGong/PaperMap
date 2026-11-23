// Capture an anchored quotation from either reader, or manage a question notebook.
(function () {
  function problem(element, text) {
    element.textContent = text;
    element.hidden = !text;
  }
  async function api(path, data) {
    const res = await fetch(path, data === undefined ? {} : {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({csrf: document.body.dataset.csrf, ...data})
    });
    const value = await res.json();
    if (!res.ok || !value.ok) throw Error(value.message || value.detail || "操作未完成，请重试。");
    return value;
  }
  const dialog = document.getElementById("evidence-dialog");
  if (dialog) {
    const form = document.getElementById("capture-evidence");
    const picker = document.getElementById("capture-question"), title = document.getElementById("capture-title");
    const save = document.getElementById("capture-save"), error = document.getElementById("capture-error");
    let selected = null, saving = false;
    function questionChanged() {
      const creating = picker.value === "";
      title.hidden = !creating; title.required = creating;
      form.querySelector(".ev-new-label").hidden = !creating;
    }
    window.pmEvidence = {async capture(item) {
      if (dialog.open) return;
      form.reset(); selected = item; save.disabled = true; problem(error, "");
      picker.replaceChildren(); title.hidden = true; form.querySelector(".ev-new-label").hidden = true;
      document.getElementById("capture-page").textContent = "第 " + (item.page + 1) + " 页 · " + (item.source === "ocr" ? "本机 OCR" : "PDF 原文");
      document.getElementById("capture-quote").textContent = item.quote;
      dialog.showModal();
      try {
        const {questions} = await api("/api/evidence/questions");
        if (!dialog.open || selected !== item) return;
        for (const q of questions) picker.add(new Option(q.title, q.id));
        picker.add(new Option("新建研究问题…", ""));
        let last;
        try { last = localStorage.getItem("papermap.evidenceQuestion"); } catch (_) {}
        if (questions.some(q => String(q.id) === last)) picker.value = last;
        questionChanged(); save.disabled = false; (title.hidden ? picker : title).focus();
      } catch (err) { if (dialog.open && selected === item) problem(error, err.message); }
    }};
    picker.addEventListener("change", questionChanged);
    document.getElementById("capture-cancel").addEventListener("click", () => { if (!saving) dialog.close(); });
    dialog.addEventListener("cancel", event => { if (saving) event.preventDefault(); });
    dialog.addEventListener("close", () => { selected = null; });
    form.addEventListener("submit", async event => {
      event.preventDefault(); if (!selected || saving || save.disabled) return;
      save.disabled = true; saving = true; problem(error, "");
      try {
        const {paper, page, quote, rects, revision, source} = selected;
        const result = await api("/api/reader/" + paper + "/evidence", {
          page, quote, rects, revision, source, question_id: Number(picker.value),
          question_title: title.value, stance: document.getElementById("capture-stance").value,
          note: document.getElementById("capture-note").value
        });
        try { localStorage.setItem("papermap.evidenceQuestion", String(result.question_id)); } catch (_) {}
        dialog.close(); window.getSelection()?.removeAllRanges();
        window.pmToast(result.created ? "已加入证据笔记" : "这段原文已在该问题的笔记中");
      } catch (err) { problem(error, err.message); }
      finally { saving = false; save.disabled = false; }
    });
  }

  const notebook = document.getElementById("evidence-notebook");
  if (!notebook) return;
  const newForm = document.getElementById("new-question-form");
  const error = document.getElementById("notebook-error");
  document.getElementById("new-question").addEventListener("click", () => {
    newForm.hidden = false; document.getElementById("new-question-title").focus();
  });
  document.getElementById("cancel-question").addEventListener("click", () => { newForm.hidden = true; });
  newForm.addEventListener("submit", async e => {
    e.preventDefault(); const button = newForm.querySelector("button[type=submit]");
    if (button.disabled) return; button.disabled = true;
    try {
      const r = await api("/api/evidence/questions", {title: document.getElementById("new-question-title").value});
      location.href = "/evidence?question=" + r.id;
    } catch (err) { problem(error, err.message); } finally { button.disabled = false; }
  });
  document.getElementById("question-filter")?.addEventListener("change", e => {
    location.href = "/evidence?question=" + e.target.value;
  });
  document.getElementById("evidence-export")?.addEventListener("click", async e => {
    if (!window.pywebview?.api?.save_text) return;
    e.preventDefault();
    const link = e.currentTarget;
    try {
      const response = await fetch(link.href);
      if (!response.ok) throw Error("未能导出证据笔记。");
      if (await window.pywebview.api.save_text(link.download, await response.text())) window.pmToast("笔记已导出");
    } catch (err) { problem(error, err.message); }
  });
  for (const row of notebook.querySelectorAll("[data-entry]")) {
    const form = row.querySelector("[data-entry-form]");
    const buttons = row.querySelector(".ev-entry-actions");
    function edit(open) { form.hidden = !open; buttons.hidden = open; if (open) form.elements.note.focus(); }
    row.querySelector("[data-edit]").addEventListener("click", () => edit(true));
    row.querySelector("[data-cancel]").addEventListener("click", () => { form.reset(); edit(false); });
    const path = "/api/evidence/entries/" + row.dataset.entry + "/";
    row.querySelector("[data-delete]").addEventListener("click", async () => {
      if (!await pmDialog("从这个研究问题中删除这条摘录及笔记？", {okText:"删除", danger:true})) return;
      try { await api(path + "delete", {}); location.reload(); } catch (err) { problem(error, err.message); }
    });
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const save = form.querySelector("button[type=submit]"); if (save.disabled) return; save.disabled = true;
      try {
        await api(path + "update", {stance: form.elements.stance.value, note: form.elements.note.value});
        history.replaceState(null, "", location.pathname + location.search + "#" + row.id);
        location.reload();
      } catch (err) { problem(error, err.message); } finally { save.disabled = false; }
    });
  }
})();
