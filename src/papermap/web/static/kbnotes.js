// Edit the "我的笔记" block of tool, field and handbook pages in place, Typora-style:
// click the block and it becomes a live Markdown editor (typing "# ", "- ", "**bold**", "> " formats
// as you type), saved automatically as Markdown. Only that block is written back.
(function () {
  const box = document.querySelector("[data-kb-notes]"); if (!box) return;
  const rel = box.dataset.kbNotes, csrf = document.body.dataset.csrf;
  const head = [...box.querySelectorAll("h2")].find(h => /^(我的笔记|My notes)$/.test(h.textContent.trim()));
  if (!head) return;
  const wrap = document.createElement("div");
  wrap.className = "kb-user";
  wrap.title = "点击直接编辑";
  while (head.nextSibling && !(head.nextSibling.nodeType === 1 && head.nextSibling.tagName === "H2")) wrap.appendChild(head.nextSibling);
  head.after(wrap);
  const status = document.createElement("span");
  status.className = "muted small kb-user-status";
  head.appendChild(status);
  const done = document.createElement("button");
  done.type = "button"; done.className = "ghost-btn kb-user-edit"; done.textContent = "完成"; done.hidden = true;
  head.appendChild(done);
  const say = m => (window.pmToast ? window.pmToast(m) : alert(m));

  let loading = null;
  const load = () => window.PMEditor ? Promise.resolve() : (loading = loading || new Promise((ok, fail) => {
    const s = document.createElement("script");
    s.src = "/static/vendor/papermap-editor/papermap-editor.min.js"; s.onload = ok; s.onerror = fail;
    document.head.appendChild(s);
  }));
  // keep [[wiki links]] (and their |alias) readable in the Markdown file
  const tidy = md => md.replace(/\\\[\\\[([^\]\n]*?)\\\]\\\]/g, (_, x) => `[[${x.replace(/\\\|/g, "|")}]]`)
    .replace(/\[\[([^\]\n]*?)\]\]/g, (_, x) => `[[${x.replace(/\\([|_*])/g, "$1")}]]`).replace(/&nbsp;/g, " ").trim();

  let editor = null, saved = "", timer = null, saving = null;
  async function save() {
    clearTimeout(timer); timer = null;
    if (!editor) return;
    const text = tidy(editor.getMarkdown());
    if (text === saved) { status.textContent = "已保存"; return; }
    status.textContent = "保存中…";
    saving = fetch("/api/kb/notes", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ csrf, rel, text }) }).then(r => r.json()).catch(() => ({ ok: false }));
    const s = await saving; saving = null;
    if (!s.ok) { status.textContent = "未保存"; say(s.message || "保存失败"); return; }
    saved = text; status.textContent = "已保存";
  }
  async function open() {
    if (editor || wrap.dataset.opening) return;
    wrap.dataset.opening = "1";
    try {
      const [r] = await Promise.all([fetch("/api/kb/notes?rel=" + encodeURIComponent(rel)), load()]);
      const j = await r.json();
      if (!r.ok || !j.ok) throw Error();
      saved = j.text;
      wrap.innerHTML = "";
      wrap.classList.add("editing"); wrap.title = "";
      editor = window.PMEditor.create(wrap, j.text, j.hint || "写下你的笔记… 输入 # 标题、- 列表、**粗体**、> 引用、[ ] 待办",
        () => { status.textContent = "未保存…"; clearTimeout(timer); timer = setTimeout(save, 1000); },
        () => save());
      done.hidden = false;
      editor.commands.focus("end");
    } catch (e) {
      say("打开编辑器失败");
    } finally {
      delete wrap.dataset.opening;
    }
  }
  async function close() {
    await save();
    if (saving) await saving;
    const j = await fetch("/api/kb/notes?rel=" + encodeURIComponent(rel)).then(r => r.json()).catch(() => ({}));
    editor.destroy(); editor = null;
    wrap.classList.remove("editing"); wrap.title = "点击直接编辑";
    wrap.innerHTML = j.html || `<p class="muted">${(j.hint || "（还没有笔记，点击这里写）").replace(/</g, "&lt;")}</p>`;
    done.hidden = true; status.textContent = "";
  }
  wrap.addEventListener("click", e => { if (!editor && !e.target.closest("a")) open(); });
  done.addEventListener("click", close);
  document.addEventListener("keydown", e => {
    if (editor && (e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); }
    if (editor && e.key === "Escape") close();
  });
  window.addEventListener("beforeunload", e => { if (editor && tidy(editor.getMarkdown()) !== saved) { save(); e.preventDefault(); } });
})();
