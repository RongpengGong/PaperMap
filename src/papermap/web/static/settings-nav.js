// Switch settings categories without rebuilding the form or losing draft values.
(function () {
  const form = document.getElementById("settings");
  const links = [...document.querySelectorAll("[data-settings-link]")];
  const panels = new Map([...document.querySelectorAll("[data-settings-panel]")]
    .map(el => [el.dataset.settingsPanel, el]));
  if (!form || !links.length || !panels.size) return;
  const title = document.querySelector("[data-settings-title]");
  const group = document.querySelector("[data-settings-group-label]");
  const sectionField = form.querySelector('[name="settings_section"]');
  const scroll = document.querySelector("[data-settings-scroll]") || document.getElementById("main_content");
  const positions = new Map();
  const valid = id => panels.has(id) ? id : "models";
  let active = valid(sectionField?.value || "models");
  let initialized = false;
  try {
    const saved = JSON.parse(sessionStorage.getItem("papermap.settingsPanelScroll") || "{}");
    for (const [id, value] of Object.entries(saved)) {
      if (panels.has(id) && Number.isFinite(value) && value >= 0) positions.set(id, value);
    }
  } catch (_) { /* A private webview can disable storage. */ }

  function saveScroll() { if (scroll) positions.set(active, scroll.scrollTop); }
  function activate(id, historyMode = "none") {
    id = valid(id); if (initialized) saveScroll();
    const changed = id !== active;
    const previous = panels.get(active);
    const movingFocus = previous?.contains(document.activeElement);
    active = id;
    panels.forEach((panel, key) => { panel.hidden = key !== id; });
    let selected;
    links.forEach(link => {
      const on = link.dataset.settingsLink === id;
      link.classList.toggle("active", on);
      if (on) { link.setAttribute("aria-current", "page"); selected = link; }
      else link.removeAttribute("aria-current");
    });
    if (sectionField) sectionField.value = id;
    if (title && selected) title.textContent = selected.dataset.title || selected.textContent.trim();
    if (group && selected) group.textContent = selected.dataset.groupLabel || t("设置");
    if (title) document.title = title.textContent + " - PaperMap";
    if (scroll) scroll.scrollTop = positions.get(id) || 0;
    initialized = true;
    if (changed) document.dispatchEvent(new CustomEvent("papermap:settings-section", { detail: { section: id } }));
    if (movingFocus && title) { title.tabIndex = -1; title.focus({ preventScroll: true }); }
    selected?.scrollIntoView({ block: "nearest" });
    if (historyMode !== "none") {
      const url = new URL(location.href); url.searchParams.set("section", id);
      url.searchParams.delete("saved"); url.hash = "";
      const state = { ...history.state, settings_section: id };
      history[historyMode === "push" ? "pushState" : "replaceState"](state, "", url.pathname + url.search);
    }
  }

  links.forEach(link => {
    const panel = panels.get(link.dataset.settingsLink);
    if (panel) {
      if (!panel.id) panel.id = "settings-panel-" + link.dataset.settingsLink;
      link.setAttribute("aria-controls", panel.id);
    }
    link.addEventListener("click", event => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || !panel) return;
      event.preventDefault();
      activate(link.dataset.settingsLink, active === link.dataset.settingsLink ? "none" : "push");
    });
  });
  window.addEventListener("popstate", () => activate(new URL(location.href).searchParams.get("section")));
  window.addEventListener("pagehide", () => {
    saveScroll();
    try { sessionStorage.setItem("papermap.settingsPanelScroll", JSON.stringify(Object.fromEntries(positions))); }
    catch (_) { /* Draft navigation still works without storage. */ }
  });

  // Native form validation checks all categories at once. Reveal only the first
  // invalid control after that pass, so several errors cannot hide one another.
  let invalid = null, reporting = false;
  form.addEventListener("invalid", event => {
    if (reporting) return;
    event.preventDefault();
    if (invalid) return;
    invalid = event.target;
    setTimeout(() => {
      const field = invalid; invalid = null;
      const panel = field.closest("[data-settings-panel]");
      if (panel) activate(panel.dataset.settingsPanel, active === panel.dataset.settingsPanel ? "none" : "push");
      const providerPane = field.closest(".pane[data-pane]");
      if (providerPane?.hidden) {
        const picker = form.querySelector('[name="provider"]');
        if (picker?.tagName === "SELECT") {
          picker.value = providerPane.dataset.pane;
          picker.dispatchEvent(new Event("change", { bubbles: true }));
        } else {
          const radio = [...form.querySelectorAll('input[name="provider"]')]
            .find(el => el.value === providerPane.dataset.pane);
          if (radio) { radio.checked = true; radio.dispatchEvent(new Event("change", { bubbles: true })); }
        }
      }
      for (let parent = field.parentElement; parent && parent !== form; parent = parent.parentElement) {
        if (parent.tagName === "DETAILS") parent.open = true;
      }
      field.scrollIntoView({ block: "center" });
      field.focus({ preventScroll: true });
      reporting = true;
      try { field.reportValidity(); } finally { reporting = false; }
    }, 0);
  }, true);

  const requested = new URL(location.href).searchParams.get("section");
  activate(requested || active, requested && !panels.has(requested) ? "replace" : "none");
})();
