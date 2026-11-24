// Edit the "我的笔记" block of tool, field and handbook pages in place.
// Only that block is written back; the generated rest of the page is untouched.
(function () {
  const box = document.querySelector("[data-kb-notes]"); if (!box) return;
  const rel = box.dataset.kbNotes, csrf = document.body.dataset.csrf;
  const head = [...box.querySelectorAll("h2")].find(h => /^(我的笔记|My notes)$/.test(h.textContent.trim()));
  if (!head) return;
  const wrap = document.createElement("div");
  wrap.className = "kb-user";
  while (head.nextSibling && !(head.nextSibling.nodeType === 1 && head.nextSibling.tagName === "H2")) wrap.appendChild(head.nextSibling);
  head.after(wrap);
  const btn = document.createElement("button");
  btn.type = "button"; btn.className = "ghost-btn kb-user-edit"; btn.textContent = "编辑";
  btn.title = "只改这一块，程序以后更新页面也不会动它";
  head.appendChild(btn);
  const say = m => (window.pmToast ? window.pmToast(m) : alert(m));
  let editing = false;

  async function open() {
    if (editing) return;
    const r = await fetch("/api/kb/notes?rel=" + encodeURIComponent(rel));
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) { say("读取笔记失败"); return; }
    editing = true; btn.hidden = true;
    const view = wrap.innerHTML;
    wrap.innerHTML = `<textarea class="notes kb-user-text" spellcheck="false" rows="8"></textarea>
      <div class="row"><button type="button" data-save>保存</button><button type="button" class="ghost" data-cancel>取消</button>
      <span class="muted small">Markdown，可以用 [[ ]] 链接论文或学科。⌘S 保存</span></div>`;
    const ta = wrap.querySelector("textarea");
    ta.value = j.text; ta.placeholder = j.hint || "写下你的笔记…"; ta.focus();
    const close = html => { wrap.innerHTML = html; editing = false; btn.hidden = false; };
    async function save() {
      const res = await fetch("/api/kb/notes", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ csrf, rel, text: ta.value }) });
      const s = await res.json().catch(() => ({ ok: false }));
      if (!s.ok) { say(s.message || "保存失败"); return; }
      close(s.html || `<p class="muted">${(j.hint || "（还没有笔记）").replace(/</g, "&lt;")}</p>`);
      say("已保存");
    }
    wrap.querySelector("[data-save]").addEventListener("click", save);
    wrap.querySelector("[data-cancel]").addEventListener("click", () => close(view));
    ta.addEventListener("keydown", e => { if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); } });
  }
  btn.addEventListener("click", open);
  wrap.addEventListener("dblclick", open);
})();
