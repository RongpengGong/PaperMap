// New-literature page: run a check, act on candidates, manage watches, follow authors.
(function () {
  const root = document.getElementById("tracking"); if (!root) return;
  const csrf = document.body.dataset.csrf;
  const say = m => (window.pmToast ? window.pmToast(m) : alert(m));
  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ csrf, ...body }) });
    return r.json().catch(() => ({ ok: false, message: "HTTP " + r.status }));
  }

  const run = document.getElementById("tr-run");
  async function waitDone() {
    for (;;) {
      await new Promise(r => setTimeout(r, 2500));
      const s = await fetch("/api/tracking/status").then(r => r.json()).catch(() => ({}));
      if (!s.running) { location.reload(); return; }
    }
  }
  run.addEventListener("click", async () => {
    run.disabled = true; run.textContent = t("检查中…");
    const j = await post("/api/tracking/run", { force: true });
    if (!j.ok) { say(j.message || t("操作失败")); }
    waitDone();
  });
  if (run.disabled) waitDone();

  root.addEventListener("click", async e => {
    const b = e.target.closest("[data-tact]");
    if (b) {
      const card = b.closest("[data-cand]"), act = b.dataset.tact;
      b.disabled = true;
      if (act === "take") say(t("正在找开放获取 PDF…"));
      const j = await post(`/api/tracking/candidates/${card.dataset.cand}`, { action: act });
      b.disabled = false;
      if (!j.ok) { say(j.message || t("操作失败")); return; }
      if (act === "take") {
        if (j.already) say(t("这篇已经在文献库里了"));
        else if (j.downloaded) { say(t("已下载开放获取 PDF，正在导入、生成卡片…")); if (window.pmStartPolling) window.pmStartPolling(); }
        else {
          say(t("没有找到合法的开放获取 PDF，已打开出版商页面；下载后拖进窗口即可"));
          if (j.landing) window.open(j.landing, "_blank", "noopener");
        }
      }
      card.style.opacity = ".45";
      setTimeout(() => location.reload(), 900);
      return;
    }
    const w = e.target.closest("[data-wact]");
    if (w && w.dataset.wact !== "toggle") {
      const box = w.closest("[data-watch]"), id = box.dataset.watch;
      const body = w.dataset.wact === "save" ? { query: box.querySelector("[data-wquery]").value }
        : { delete: true };
      if (w.dataset.wact === "delete" && !confirm(t("取消关注这位作者？"))) return;
      const j = await post(`/api/tracking/watches/${id}`, body);
      if (!j.ok) { say(j.message || t("操作失败")); return; }
      say(t("已保存，下次检查生效")); if (body.delete) location.reload();
    }
  });
  root.addEventListener("change", async e => {
    const w = e.target.closest("[data-wact=toggle]"); if (!w) return;
    const box = w.closest("[data-watch]");
    const j = await post(`/api/tracking/watches/${box.dataset.watch}`, { enabled: w.checked });
    if (!j.ok) { say(j.message || t("操作失败")); w.checked = !w.checked; return; }
    box.classList.toggle("off", !w.checked);
  });

  // follow an author
  const q = document.getElementById("tr-author-q"), res = document.getElementById("tr-author-res");
  let timer = null;
  q.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const v = q.value.trim(); res.replaceChildren();
      if (v.length < 3) return;
      const j = await fetch("/api/tracking/authors?q=" + encodeURIComponent(v)).then(r => r.json()).catch(() => ({}));
      if (!j.ok) { say(j.message || t("查询失败")); return; }
      for (const a of j.authors) {
        const row = document.createElement("button");
        row.type = "button"; row.className = "tr-author-hit";
        const name = document.createElement("b"); name.textContent = a.name;
        const meta = document.createElement("span"); meta.className = "muted small";
        meta.textContent = ` ${a.institution || ""} · ${a.works} ${t("篇")}`;
        row.append(name, meta);
        row.addEventListener("click", async () => {
          const r = await post("/api/tracking/authors", { id: a.id, name: a.name });
          if (!r.ok) { say(r.message || t("操作失败")); return; }
          say(t("已关注，下次检查时查找这位作者的新论文")); location.reload();
        });
        res.append(row);
      }
      if (!j.authors.length) res.textContent = t("没有找到");
    }, 450);
  });
})();
