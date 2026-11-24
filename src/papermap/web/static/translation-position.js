// Track the paragraph at the top of the actual reading viewport. Its height can
// change when the two languages stack, or when the PDF and chat take up space.
(() => {
  let current;
  const memory = new Map();

  function attach(panel) {
    const body = panel.querySelector('[data-body="translation"]');
    if (body && current?.body === body) return current;
    current?.detach();
    current = null;
    if (!body) return null;

    const split = panel.querySelector(".panel-split");
    const side = body.closest(".side-scroll");
    const grid = body.querySelector(".bilingual");
    if (!split || !side || !grid) return null;
    const items = Array.from(grid.querySelectorAll("[data-tr-anchor]"));
    const key = "papermap.translation-position." + panel.dataset.paper + "." + body.dataset.translationVersion;
    let position = memory.get(key);
    if (!position) {
      try { position = JSON.parse(sessionStorage.getItem(key)); } catch (_) { /* Per-window storage is optional. */ }
    }
    let active = false;
    let pending = 0;
    let writing = 0;
    let lastLayout;

    function viewport() {
      return getComputedStyle(side).overflowY === "visible" ? split : side;
    }
    function layout() {
      const v = viewport();
      return [v === side, v.clientWidth, v.clientHeight, grid.clientWidth, grid.clientHeight].join("/");
    }
    function persist() {
      clearTimeout(writing);
      if (!position) return;
      memory.set(key, position);
      try { sessionStorage.setItem(key, JSON.stringify(position)); } catch (_) { /* Keep the in-memory copy. */ }
    }
    function measure() {
      const v = viewport();
      // At the top, keep the header and (in a stacked workspace) the PDF visible.
      if (!items.length || v.scrollTop <= 1) return { top: 0 };
      const edge = v.getBoundingClientRect().top + v.clientTop;
      let low = 0, high = items.length;
      while (low < high) {
        const mid = (low + high) >>> 1;
        if (items[mid].getBoundingClientRect().top <= edge + 0.5) low = mid + 1;
        else high = mid;
      }
      if (!low) return { top: v.scrollTop };
      const node = items[low - 1];
      const box = node.getBoundingClientRect();
      return { anchor: node.dataset.trAnchor, fraction: Math.min(1, Math.max(0, (edge - box.top) / Math.max(1, box.height))),
        gap: Math.max(0, edge - box.bottom) };
    }
    function capture(flush) {
      if (!active || body.hidden || !body.isConnected) return;
      // Layout may already have changed while its resize callback is still queued.
      if (!pending && lastLayout === layout()) position = measure();
      if (flush) persist();
      else {
        clearTimeout(writing);
        writing = setTimeout(persist, 200);
      }
    }
    function restore() {
      if (!active || body.hidden || !body.isConnected) return;
      const v = viewport();
      const node = position?.anchor && body.querySelector('[data-tr-anchor="' + CSS.escape(position.anchor) + '"]');
      if (node) {
        const box = node.getBoundingClientRect();
        v.scrollTop += box.top - v.getBoundingClientRect().top - v.clientTop + box.height * position.fraction + position.gap;
      } else v.scrollTop = position?.top || 0;
      lastLayout = layout();
    }
    function afterLayout() {
      if (!active || pending) return;
      pending = requestAnimationFrame(() => { pending = 0; restore(); });
    }
    function onLayout() {
      if (active && layout() !== lastLayout) afterLayout();
    }
    function onScroll(event) {
      if (active && event.target === viewport()) capture(false);
    }
    function onChatLayout() {
      if (!active) return;
      capture(true);
      afterLayout();
    }

    const resize = new ResizeObserver(onLayout);
    [split, side, grid].forEach(node => resize.observe(node));
    [split, side].forEach(node => node.addEventListener("scroll", onScroll, { passive: true }));
    panel.addEventListener("pm:chat-layout", onChatLayout);
    current = {
      body,
      capture: () => capture(true),
      show() {
        if (active) return;
        active = true;
        restore();
        afterLayout();
      },
      leave() {
        capture(true);
        active = false;
        cancelAnimationFrame(pending);
        pending = 0;
      },
      detach() {
        this.leave();
        persist();
        resize.disconnect();
        [split, side].forEach(node => node.removeEventListener("scroll", onScroll));
        panel.removeEventListener("pm:chat-layout", onChatLayout);
      }
    };
    return current;
  }

  window.addEventListener("pagehide", () => current?.capture());
  window.pmTranslationPosition = { attach, capture: () => current?.capture() };
})();
