// Keep one position per paper, across different reader sizes and reading sessions.
window.pmReadingProgress = function (root, pagesEl, pageEls, csrf) {
  const pid = root.dataset.paper, embedded = window.self !== window.top;
  const url = "/api/reader/" + pid + "/progress", cacheKey = "papermap.reading." + pid;
  let ready = false, engaged = false, timer = 0, pending = null, latest = null, inFlight = null;
  let placedTop = null;

  function position() {
    const top = pagesEl.scrollTop + 12;
    let page = pageEls.findIndex(el => top <= el.offsetTop + el.offsetHeight);
    if (page < 0) page = pageEls.length - 1;
    const el = pageEls[page];
    return {page, offset: Math.min(1, Math.max(0, (top - el.offsetTop) / Math.max(1, el.offsetHeight)))};
  }
  function place(pos) {
    const el = pageEls[Math.max(0, Math.min(pageEls.length - 1, pos.page))];
    if (!el) return;
    pagesEl.scrollTop = pos.page === 0 && pos.offset === 0 ? 0 : el.offsetTop + el.offsetHeight * pos.offset - 12;
    placedTop = pagesEl.scrollTop;
  }
  function restore() {
    // An iframe can be replaced before its last keepalive request reaches SQLite.
    let saved = {page:Number(root.dataset.resumePage), offset:Number(root.dataset.resumeOffset), at:Number(root.dataset.resumeAt)};
    try {
      const cached = JSON.parse(sessionStorage.getItem(cacheKey));
      if (cached && cached.at > saved.at && Number.isInteger(cached.page) && cached.offset >= 0 && cached.offset <= 1) saved = cached;
    } catch (_) { /* Local storage can be unavailable in a private window. */ }
    if (saved.at > 0) place(saved);
  }
  async function send(leaving = false) {
    clearTimeout(timer); timer = 0;
    const pos = pending || (leaving && inFlight);
    if (!pos) return;
    pending = null; inFlight = pos;
    try {
      const response = await fetch(url, {
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({csrf, ...pos}), keepalive:true
      });
      if (response.ok && embedded && !leaving && latest?.at === pos.at) {
        const data = await response.json();
        if (data.saved) window.parent.postMessage({type:"papermap:reading-progress", recent:data.recent}, location.origin);
      }
    } catch (_) { if (latest?.at === pos.at) pending = pos; }
    finally { if (inFlight === pos) inFlight = null; }
  }
  function record() {
    if (!ready || !engaged || document.hidden || !pagesEl.clientHeight) return;
    const pos = position();
    if (latest && latest.page === pos.page && Math.abs(latest.offset - pos.offset) < 0.0001) return;
    pos.at = Math.max((latest?.at || 0) + 1, Math.round((performance.timeOrigin + performance.now()) * 1000));
    latest = pending = pos;
    try { sessionStorage.setItem(cacheKey, JSON.stringify(pos)); } catch (_) { /* SQLite still saves the position. */ }
    if (!timer) timer = setTimeout(send, 300);
  }
  function engage(event) {
    if (!event.isTrusted || document.hidden) return;
    engaged = true; record();
  }
  document.addEventListener("pointerdown", engage, {passive:true});
  document.addEventListener("keydown", engage);
  pagesEl.addEventListener("wheel", engage, {passive:true});
  pagesEl.addEventListener("scroll", () => {
    if (pagesEl.scrollTop === placedTop) return;  // Restoration and reflow are passive.
    placedTop = null;
    record();
  }, {passive:true});
  window.addEventListener("pagehide", () => send(true));
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) send(true);
    else if (!embedded) { engaged = true; record(); }
  });
  function start() {
    ready = true;
    if (!embedded && !document.hidden) engaged = true;
    record();
  }
  return {position, place, restore, start};
};
