// Live Markdown editing inside the paper panel: the "My notes" tab and the card itself.
// Uses the same editor bundle as the tool / field pages (toolbar, colours, pasted images).
(function () {
  const csrf = document.body.dataset.csrf;
  const say = m => (window.pmToast ? window.pmToast(m) : alert(m));
  let loading = null;
  window.pmLoadEditor = () => window.PMEditor ? Promise.resolve() : (loading = loading || new Promise((ok, fail) => {
    const s = document.createElement("script");
    s.src = "/static/vendor/papermap-editor/papermap-editor.min.js"; s.onload = ok; s.onerror = fail;
    document.head.appendChild(s);
  }));
  // keep [[wiki links]] readable in the Markdown file
  const tidy = md => md.replace(/\\\[\\\[([^\]\n]*?)\\\]\\\]/g, (_, x) => `[[${x.replace(/\\\|/g, "|")}]]`)
    .replace(/\[\[([^\]\n]*?)\]\]/g, (_, x) => `[[${x.replace(/\\([|_*])/g, "$1")}]]`).replace(/&nbsp;/g, " ").trim();
  window.pmTidyMarkdown = tidy;
  const uploader = prefix => async file => {
    const r = await fetch("/api/kb/attachments", { method: "POST", body: file,
      headers: { "Content-Type": "application/octet-stream", "X-CSRF": csrf, "X-Filename": encodeURIComponent(file.name || "image") } })
      .then(x => x.json()).catch(() => ({ ok: false }));
    if (!r.ok) { say(r.message || t("图片上传失败")); return null; }
    return { src: prefix + r.name, alt: r.alt };
  };

  // "[[" in the editor: suggest papers and fields, insert a wiki link
  function linkSuggest(editor) {
    const pop = document.createElement("div"); pop.className = "link-pop"; pop.hidden = true; document.body.appendChild(pop);
    let items = [], sel = 0, range = null, seq = 0;
    const close = () => { pop.hidden = true; range = null; };
    const render = () => {
      pop.innerHTML = items.map((it, i) => `<div class="lp-item ${i === sel ? "on" : ""}" data-i="${i}">${it.label.replace(/[<>&]/g, "")}</div>`).join("")
        || `<div class="lp-empty">${t("没有匹配的论文或学科")}</div>`;
    };
    const pick = i => {
      const it = items[i]; if (!it || !range) return;
      editor.chain().focus().deleteRange(range).insertContent(it.insert).run();
      close();
    };
    editor.on("update", async () => {
      const { from } = editor.state.selection;
      const before = editor.state.doc.textBetween(Math.max(0, from - 80), from, "\n", "\n");
      const at = before.lastIndexOf("[[");
      if (at === -1 || before.slice(at).includes("]]") || before.slice(at).includes("\n")) { close(); return; }
      const q = before.slice(at + 2);
      range = { from: from - q.length - 2, to: from };
      const mine = ++seq;
      const res = await fetch("/api/link-suggest?q=" + encodeURIComponent(q)).then(r => r.json()).catch(() => []);
      if (mine !== seq || !range) return;
      items = res; sel = 0; render();
      const c = editor.view.coordsAtPos(from);
      pop.style.left = Math.min(innerWidth - 540, c.left) + "px"; pop.style.top = Math.min(innerHeight - 260, c.bottom + 6) + "px";
      pop.style.width = "520px"; pop.hidden = false;
    });
    editor.view.dom.addEventListener("keydown", e => {
      if (pop.hidden) return;
      if (e.key === "ArrowDown") { e.preventDefault(); e.stopPropagation(); sel = Math.min(items.length - 1, sel + 1); render(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); e.stopPropagation(); sel = Math.max(0, sel - 1); render(); }
      else if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); pick(sel); }
      else if (e.key === "Escape") { e.stopPropagation(); close(); }
    }, true);
    pop.addEventListener("mousedown", e => { const d = e.target.closest(".lp-item"); if (d) { e.preventDefault(); pick(+d.dataset.i); } });
    editor.on("blur", () => setTimeout(close, 150));
    editor.on("destroy", () => pop.remove());
  }

  // My notes tab: the textarea stays as the source of truth for the existing autosave
  async function notes(panel) {
    const ta = panel.querySelector("textarea.notes"), live = panel.querySelector("[data-notes-live]");
    if (!ta || !live || live.dataset.ready) return;
    live.dataset.ready = "1";
    try { await window.pmLoadEditor(); } catch (e) { return; }  // keep the plain textarea
    const ed = window.PMEditor.create(live, ta.value, t("写下你的笔记… 直接粘贴图片；输入 [[ 链接其他论文或学科"),
      e => { ta.value = tidy(e.getMarkdown()); ta.dispatchEvent(new Event("input")); },
      () => ta.dispatchEvent(new Event("blur")), uploader("../../attachments/"));
    linkSuggest(ed);
    ta.hidden = true; live.hidden = false;
    const pv = panel.querySelector("[data-notes-preview]"); if (pv) pv.hidden = true;
    ed.on("focus", () => live.classList.add("typing")); ed.on("blur", () => live.classList.remove("typing"));
  }

  // Card: edit in place; saved as the user's version, kept when the card is refreshed
  function card(panel) {
    const btn = panel.querySelector("[data-card-edit]"), reset = panel.querySelector("[data-card-reset]");
    const bodyEl = panel.querySelector("[data-card-body]"), status = panel.querySelector("[data-card-status]");
    const pid = panel.dataset.paper;
    if (reset) reset.addEventListener("click", async () => {
      if (!confirm(t("丢弃你对这张卡片的修改，恢复为自动生成的版本？"))) return;
      const r = await fetch(`/paper/${pid}/card`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ csrf, reset: true }) }).then(x => x.json());
      if (r.ok && window.pmOpenPaper) window.pmOpenPaper(pid, false); else say(r.message || t("操作失败"));
    });
    if (!btn || !bodyEl) return;
    let ed = null, saved = "", timer = null, host = null;
    async function save() {
      clearTimeout(timer); timer = null;
      if (!ed) return;
      const text = tidy(ed.getMarkdown());
      if (text === saved) return;
      status.textContent = t("保存中…");
      const r = await fetch(`/paper/${pid}/card`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ csrf, text }) })
        .then(x => x.json()).catch(() => ({ ok: false }));
      if (!r.ok) { status.textContent = t("未保存"); say(r.message || t("保存失败")); return; }
      saved = text; status.textContent = t("已保存");
    }
    btn.addEventListener("click", async () => {
      if (ed) { await save(); ed.destroy(); ed = null; host.remove(); if (window.pmOpenPaper) window.pmOpenPaper(pid, false); return; }
      const [j] = await Promise.all([fetch(`/paper/${pid}/card.md`).then(r => r.json()), window.pmLoadEditor()]).catch(() => [{}]);
      if (!j || !j.ok) { say(t("打开编辑器失败")); return; }
      saved = j.text;
      host = document.createElement("div"); host.className = "kb-user editing card-editing";
      bodyEl.hidden = true; bodyEl.after(host);
      ed = window.PMEditor.create(host, j.text, "", () => { status.textContent = t("未保存…"); clearTimeout(timer); timer = setTimeout(save, 1200); },
        () => save(), uploader("../../attachments/"));
      linkSuggest(ed);
      btn.textContent = t("完成");
      ed.commands.focus("start");
    });
  }

  window.pmPanelEditors = panel => { notes(panel); card(panel); };
  const first = document.querySelector(".panel");  // app.js set up this panel before we loaded
  if (first) window.pmPanelEditors(first);
})();
