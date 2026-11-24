// The phone control panel has its own forms; keep all other settings drafts in the DOM.
(function () {
  const panel = document.querySelector('[data-settings-panel="mobile"]');
  if (!panel) return;
  let generation = 0;

  async function update(form) {
    const request = ++generation;
    const options = { headers: { "X-PaperMap-Panel": "mobile" }, cache: "no-store" };
    if (form) {
      options.method = "POST";
      options.body = new URLSearchParams(new FormData(form));
    }
    panel.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(form?.action || "/settings/mobile/panel", options);
      if (!response.ok) throw new Error(String(response.status));
      const html = await response.text();
      if (request === generation && !panel.hidden) panel.innerHTML = html;
      else if (form && !panel.hidden) update();
    } catch (_) {
      if (request !== generation || panel.hidden) return;
      const retry = document.createElement("button");
      retry.type = "button";
      retry.textContent = t("重新加载手机同步设置");
      retry.onclick = () => update();
      panel.replaceChildren(retry);
    } finally {
      if (request === generation) panel.removeAttribute("aria-busy");
    }
  }

  function clear() {
    ++generation;
    panel.removeAttribute("aria-busy");
    panel.replaceChildren();
  }
  // Sharing no papers is allowed (lists only), but say what the phone will then miss.
  panel.addEventListener("change", event => {
    if (event.target.name !== "paper_id") return;
    const hint = panel.querySelector("[data-mobile-none]");
    if (hint) hint.hidden = !!panel.querySelector('input[name="paper_id"]:checked');
  });
  panel.addEventListener("submit", event => {
    event.preventDefault();
    if (!panel.hasAttribute("aria-busy")) update(event.target);
  });
  document.addEventListener("papermap:settings-section", () => {
    if (panel.hidden) clear();
    else update();
  });
  window.addEventListener("pagehide", clear);
  window.addEventListener("pageshow", event => {
    if (event.persisted && !panel.hidden) update();
  });
})();
