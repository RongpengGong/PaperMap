(function () {
  const section = document.getElementById("recent-reading");
  const list = section?.querySelector("ul");
  const query = document.getElementById("q"), filter = document.getElementById("status_filter");
  function showSection() { section.hidden = !list.children.length || Boolean(query.value.trim() || filter.value); }
  function render(recent) {
    if (!list) return;
    const items = recent.map(item => {
      const li = document.createElement("li"), link = document.createElement("a");
      const title = document.createElement("span"), page = document.createElement("small");
      link.href = "/?id=" + item.id + "&read=1";
      link.title = title.textContent = item.title || t("无标题论文");
      page.textContent = t("第 ") + (item.page + 1) + t(" 页");
      link.append(title, page); li.append(link);
      return li;
    });
    list.replaceChildren(...items); showSection();
  }
  window.addEventListener("message", event => {
    const frame = document.querySelector(".pdf-col iframe");
    if (!frame || event.source !== frame.contentWindow || event.origin !== location.origin ||
        event.data?.type !== "papermap:reading-progress" || !Array.isArray(event.data.recent)) return;
    render(event.data.recent);
    const panel = frame.closest(".panel"), id = Number(panel.dataset.paper);
    if (event.data.recent.some(item => item.id === id)) {
      const unread = panel.querySelector('[data-rs-val="unread"].on');
      if (unread) {
        unread.classList.remove("on");
        panel.querySelector('[data-rs-val="reading"]').classList.add("on");
        document.querySelector(`.paper-row[data-id="${id}"] .unread-dot`)?.remove();
      }
    }
  });
  if (!section) return;
  query.addEventListener("input", showSection); filter.addEventListener("change", showSection);
  async function refresh() {
    try {
      const response = await fetch("/api/reading/recent");
      if (response.ok) render((await response.json()).recent);
    } catch (_) { /* Keep the last known bookmarks. */ }
  }
  document.addEventListener("papermap:read-status", refresh);
  window.addEventListener("pageshow", refresh);
})();
