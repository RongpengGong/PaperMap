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
    live._pmEditor = ed;
    ta.hidden = true; live.hidden = false;
    const pv = panel.querySelector("[data-notes-preview]"); if (pv) pv.hidden = true;
    ed.on("focus", () => live.classList.add("typing")); ed.on("blur", () => live.classList.remove("typing"));
  }

  // Card: edit in place; saved as the user's version, kept when the card is refreshed.
  // Each editing session first stores the previous version, so earlier versions can be restored.
  function card(panel) {
    const btn = panel.querySelector("[data-card-edit]"), hist = panel.querySelector("[data-card-reset]");
    const undo = panel.querySelector("[data-card-undo]");
    const bodyEl = panel.querySelector("[data-card-body]"), status = panel.querySelector("[data-card-status]");
    const pid = panel.dataset.paper;
    if (!btn || !bodyEl) return;
    let ed = null, saved = "", start = null, timer = null, host = null, session = "";
    const send = body => fetch(`/paper/${pid}/card`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ csrf, session, ...body }) }).then(x => x.json()).catch(() => ({ ok: false }));
    async function save() {
      clearTimeout(timer); timer = null;
      if (!ed) return;
      const text = tidy(ed.getMarkdown());
      if (text === saved) return;
      status.textContent = t("保存中…");
      const r = await send({ text });
      if (!r.ok) { status.textContent = t("未保存"); say(r.message || t("保存失败")); return; }
      saved = text; status.textContent = t("已保存");
    }
    function close() {
      clearTimeout(timer); timer = null;
      if (ed) { ed.destroy(); ed = null; }
      if (host) { host.remove(); host = null; }
      if (window.pmOpenPaper) window.pmOpenPaper(pid, false);
    }
    btn.addEventListener("click", async () => {
      if (ed) { await save(); close(); return; }
      const [j] = await Promise.all([fetch(`/paper/${pid}/card.md`).then(r => r.json()), window.pmLoadEditor()]).catch(() => [{}]);
      if (!j || !j.ok) { say(t("打开编辑器失败")); return; }
      saved = j.text; start = { text: j.text, edited: j.edited }; session = String(Date.now());
      host = document.createElement("div"); host.className = "kb-user editing card-editing";
      bodyEl.hidden = true; bodyEl.after(host);
      ed = window.PMEditor.create(host, j.text, "", () => { status.textContent = t("未保存…"); clearTimeout(timer); timer = setTimeout(save, 1200); },
        () => save(), uploader("../../attachments/"));
      linkSuggest(ed);
      btn.textContent = t("完成");
      if (undo) undo.hidden = false;
      if (hist) hist.hidden = false;
      ed.commands.focus("start");
    });
    // back to how the card looked when this editor was opened
    undo && undo.addEventListener("click", async () => {
      if (!start) return;
      clearTimeout(timer);
      if (ed && tidy(ed.getMarkdown()) !== start.text &&
          !(await window.pmDialog(t("放弃这次编辑的改动，回到打开编辑器之前的样子？"), { okText: t("放弃这次编辑"), danger: true }))) return;
      const r = start.edited ? await send({ text: start.text }) : await send({ reset: true });
      if (!r.ok) { say(r.message || t("操作失败")); return; }
      close();
    });
    // earlier versions: before each editing session, and the generated card
    hist && hist.addEventListener("click", async e => {
      const x = e.clientX, y = e.clientY;
      await save();
      const j = await fetch(`/paper/${pid}/card/versions`).then(r => r.json()).catch(() => null);
      if (!j || !j.ok) { say(t("读取历史版本失败")); return; }
      const when = s => { const d = new Date(s), p = n => String(n).padStart(2, "0");
        return isNaN(d) ? s.slice(5, 16).replace("T", " ") : `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`; };
      const restore = async (body, label) => {
        if (!(await window.pmDialog(`${t("把卡片换成")}「${label}」？${t("现在的内容会先存为一个历史版本，随时可以换回来。")}`, { okText: t("恢复") }))) return;
        const r = await send(body);
        if (r.ok) { say(t("已恢复")); close(); } else say(r.message || t("操作失败"));
      };
      const items = [{ label: t("恢复到哪个版本？") }];
      j.versions.filter(v => !v.generated).forEach((v, i) => {
        const label = `${i === 0 ? t("上个版本") : t("我的版本")} · ${when(v.at)}`;
        items.push({ text: `${label}${v.preview ? " — " + v.preview : ""}`, run: () => restore({ restore: v.id }, label) });
      });
      if (j.versions.filter(v => !v.generated).length === 0) items.push({ label: t("还没有更早的我的版本") });
      items.push("-", { text: t("模型生成的版本（丢掉所有我的修改）"), run: () => restore({ reset: true }, t("模型生成的版本")) });
      window.pmMenu(x, y, items);
    });
  }

  window.pmPanelEditors = panel => { notes(panel); card(panel); };
  const first = document.querySelector(".panel");  // app.js set up this panel before we loaded
  if (first) window.pmPanelEditors(first);
})();
