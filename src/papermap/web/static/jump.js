// ⌘K / Ctrl+K: jump to any page, paper, field, research question, tool or handbook.
(() => {
  const t = s => (window.t ? window.t(s) : s);
  const esc = s => String(s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const KIND = { page: "页面", paper: "论文", field: "学科", question: "问题", tool: "工具", handbook: "手册" };
  const pages = () => [...document.querySelectorAll(".sidebar .nav a[href]")]
    .map(a => ({ kind: "page", label: (a.querySelector("span") || a).textContent.trim(), hint: "", url: a.getAttribute("href") }))
    .filter((p, i, all) => p.label && p.url.startsWith("/") && all.findIndex(x => x.url === p.url) === i);
  let root = null, items = [], sel = 0, timer = null, seq = 0;

  function close() { if (root) { root.remove(); root = null; } }
  function draw() {
    const list = root.querySelector(".jump-list");
    list.innerHTML = items.length ? items.map((it, i) => `<li class="${i === sel ? "on" : ""}" data-i="${i}"><span class="jump-kind">${esc(t(KIND[it.kind] || it.kind))}</span>`
      + `<span class="jump-label">${esc(it.label)}</span><span class="jump-hint">${esc(it.hint)}</span></li>`).join("")
      : `<li class="jump-empty">${esc(t("没有找到"))}</li>`;
    const on = list.querySelector("li.on"); on && on.scrollIntoView({ block: "nearest" });
  }
  async function search(q) {
    const my = ++seq;
    const low = q.trim().toLowerCase();
    const local = pages().filter(p => !low || p.label.toLowerCase().includes(low) || p.url.includes(low));
    let remote = [];
    try { remote = (await fetch("/api/jump?q=" + encodeURIComponent(q.trim())).then(r => r.json())).items || []; } catch (e) { /* offline */ }
    if (my !== seq || !root) return;
    items = low ? [...local, ...remote] : [...remote, ...local];
    sel = 0; draw();
  }
  function go(i) { const it = items[i]; if (it) { close(); location.href = it.url; } }
  function open() {
    if (root) return;
    root = document.createElement("div");
    root.className = "jump";
    root.innerHTML = `<div class="jump-box" role="dialog" aria-label="${esc(t("快速跳转"))}"><input type="search" placeholder="${esc(t("跳转到页面、论文、学科、研究问题…"))}" autocomplete="off">`
      + `<ul class="jump-list"></ul><div class="jump-foot">${esc(t("↑↓ 选择 · ↩ 打开 · Esc 关闭"))}</div></div>`;
    document.body.appendChild(root);
    const input = root.querySelector("input");
    input.focus();
    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => search(input.value), 120); });
    input.addEventListener("keydown", e => {
      if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(items.length - 1, sel + 1); draw(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(0, sel - 1); draw(); }
      else if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); go(sel); }
      else if (e.key === "Escape") close();
    });
    root.addEventListener("mousedown", e => { if (e.target === root) close(); });
    root.querySelector(".jump-list").addEventListener("click", e => { const li = e.target.closest("li[data-i]"); if (li) go(+li.dataset.i); });
    search("");
  }
  document.addEventListener("keydown", e => {
    if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "k") { e.preventDefault(); root ? close() : open(); }
  });
  window.pmJump = open;
  document.addEventListener("click", e => { if (e.target.closest("[data-jump]")) { e.preventDefault(); open(); } });
})();
