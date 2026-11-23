// Knowledge map: dependency-free force-directed graph on <canvas>.
(function () {
  const canvas = document.getElementById("map");
  const wrap = document.getElementById("map_wrap");
  const tip = document.getElementById("tip");
  const ctx = canvas.getContext("2d");
  const fieldsOnly = document.getElementById("fields_only");
  const showTopics = document.getElementById("show_topics");
  const css = getComputedStyle(document.documentElement);
  const color = name => css.getPropertyValue(name).trim();
  const COLORS = { method: color("--map-method"), topic: color("--map-topic"), paper: color("--map-paper") };

  let data = null, nodes = [], edges = [], byId = {};
  let view = { x: 0, y: 0, k: 1 }, alpha = 1, dragging = null, panning = null, hover = null, moved = false;

  function size() {
    const r = wrap.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    canvas.width = r.width * dpr; canvas.height = r.height * dpr;
    canvas.style.width = r.width + "px"; canvas.style.height = r.height + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function radius(n) {
    if (n.kind === "paper") return 4.5;
    return 9 + 4 * Math.sqrt(n.count || 0);
  }

  function build() {
    const keep = n => n.kind === "paper" ? !fieldsOnly.checked : (n.kind !== "topic" || showTopics.checked);
    const old = byId;
    nodes = data.nodes.filter(keep).filter(n => n.kind === "paper" || n.count > 0 || fieldsOnly.checked)
      .map(n => Object.assign({}, n, old[n.id] ? { x: old[n.id].x, y: old[n.id].y } :
        { x: (Math.random() - .5) * 400, y: (Math.random() - .5) * 400 }, { vx: 0, vy: 0 }));
    byId = {}; nodes.forEach(n => byId[n.id] = n);
    const src = fieldsOnly.checked ? data.field_edges : data.paper_edges;
    edges = src.filter(e => byId[e.source] && byId[e.target]).map(e => ({ s: byId[e.source], t: byId[e.target], w: e.weight || 1, primary: e.primary }));
    alpha = 1;
  }

  function tick() {
    if (alpha < 0.005) return;
    const rep = fieldsOnly.checked ? 9000 : 2600;
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = b.x - a.x, dy = b.y - a.y, d2 = dx * dx + dy * dy + 0.01;
        if (d2 > 250000) continue;
        const f = rep / d2 * alpha, d = Math.sqrt(d2);
        dx /= d; dy /= d;
        a.vx -= dx * f; a.vy -= dy * f; b.vx += dx * f; b.vy += dy * f;
      }
    }
    const len = fieldsOnly.checked ? 170 : 70;
    edges.forEach(e => {
      const dx = e.t.x - e.s.x, dy = e.t.y - e.s.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = (d - len) * 0.02 * alpha * Math.min(3, e.w);
      e.s.vx += dx / d * f; e.s.vy += dy / d * f; e.t.vx -= dx / d * f; e.t.vy -= dy / d * f;
    });
    nodes.forEach(n => {
      n.vx -= n.x * 0.004 * alpha; n.vy -= n.y * 0.004 * alpha;  // gravity to center
      if (n !== dragging) { n.x += n.vx; n.y += n.vy; }
      n.vx *= 0.6; n.vy *= 0.6;
    });
    alpha *= 0.985;
  }

  function toScreen(n) {
    const r = canvas.getBoundingClientRect();
    return [r.width / 2 + (n.x + view.x) * view.k, r.height / 2 + (n.y + view.y) * view.k];
  }
  function toWorld(px, py) {
    const r = canvas.getBoundingClientRect();
    return [(px - r.width / 2) / view.k - view.x, (py - r.height / 2) / view.k - view.y];
  }

  function draw() {
    const r = canvas.getBoundingClientRect();
    ctx.clearRect(0, 0, r.width, r.height);
    const linked = new Set();
    if (hover) edges.forEach(e => { if (e.s === hover || e.t === hover) { linked.add(e.s); linked.add(e.t); } });
    edges.forEach(e => {
      const [x1, y1] = toScreen(e.s), [x2, y2] = toScreen(e.t);
      const hot = hover && (e.s === hover || e.t === hover);
      ctx.strokeStyle = hot ? color("--accent") : color("--map-edge");
      ctx.globalAlpha = hover && !hot ? 0.25 : 1;
      ctx.lineWidth = (fieldsOnly.checked ? Math.min(8, e.w * 1.5) : (e.primary ? 1.6 : 0.8)) * Math.sqrt(view.k);
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
    });
    ctx.globalAlpha = 1;
    nodes.forEach(n => {
      const [x, y] = toScreen(n), rad = radius(n) * Math.sqrt(view.k);
      ctx.globalAlpha = hover && n !== hover && !linked.has(n) ? 0.35 : (n.retired ? 0.5 : 1);
      ctx.fillStyle = COLORS[n.kind];
      ctx.beginPath(); ctx.arc(x, y, rad, 0, Math.PI * 2); ctx.fill();
      if (n === hover) { ctx.lineWidth = 2; ctx.strokeStyle = color("--fg"); ctx.stroke(); }
      if (n.kind !== "paper" || n === hover || view.k > 1.6) {
        ctx.fillStyle = color("--fg");
        ctx.font = `${n.kind === "paper" ? 11 : 13}px -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif`;
        ctx.textAlign = "center";
        ctx.fillText(n.label + (n.kind !== "paper" && n.count ? ` (${n.count})` : ""), x, y + rad + 14);
      }
    });
    ctx.globalAlpha = 1;
  }

  function loop() { tick(); draw(); requestAnimationFrame(loop); }

  function pick(ev) {
    const r = canvas.getBoundingClientRect(), px = ev.clientX - r.left, py = ev.clientY - r.top;
    for (let i = nodes.length - 1; i >= 0; i--) {
      const [x, y] = toScreen(nodes[i]);
      const rad = Math.max(8, radius(nodes[i]) * Math.sqrt(view.k));
      if ((x - px) ** 2 + (y - py) ** 2 <= rad * rad) return nodes[i];
    }
    return null;
  }

  canvas.addEventListener("mousedown", ev => {
    moved = false;
    const n = pick(ev);
    if (n) dragging = n; else panning = { x: ev.clientX, y: ev.clientY, vx: view.x, vy: view.y };
  });
  window.addEventListener("mousemove", ev => {
    if (dragging) {
      const r = canvas.getBoundingClientRect();
      [dragging.x, dragging.y] = toWorld(ev.clientX - r.left, ev.clientY - r.top);
      alpha = Math.max(alpha, 0.3); moved = true;
    } else if (panning) {
      view.x = panning.vx + (ev.clientX - panning.x) / view.k;
      view.y = panning.vy + (ev.clientY - panning.y) / view.k; moved = true;
    } else if (ev.target === canvas) {
      hover = pick(ev);
      canvas.style.cursor = hover ? "pointer" : "grab";
      if (hover) {
        tip.hidden = false;
        tip.textContent = hover.kind === "paper" ? `${hover.title}  (${hover.journal})` : `${hover.label} · ${hover.title} · ${hover.count} 篇`;
        const r = wrap.getBoundingClientRect();
        tip.style.left = (ev.clientX - r.left + 14) + "px"; tip.style.top = (ev.clientY - r.top + 10) + "px";
      } else tip.hidden = true;
    }
  });
  window.addEventListener("mouseup", ev => {
    if (!moved && dragging && dragging.url) window.location = dragging.url;
    dragging = null; panning = null;
  });
  canvas.addEventListener("wheel", ev => {
    ev.preventDefault();
    const r = canvas.getBoundingClientRect(), [wx, wy] = toWorld(ev.clientX - r.left, ev.clientY - r.top);
    view.k = Math.min(4, Math.max(0.25, view.k * (ev.deltaY < 0 ? 1.12 : 1 / 1.12)));
    const [nx, ny] = toWorld(ev.clientX - r.left, ev.clientY - r.top);
    view.x += nx - wx; view.y += ny - wy;
  }, { passive: false });
  [fieldsOnly, showTopics].forEach(el => el.addEventListener("change", build));
  window.addEventListener("resize", size);

  fetch("/api/graph").then(r => r.json()).then(d => {
    data = d;
    if (!d.paper_edges.length) document.getElementById("map_empty").hidden = false;
    size(); build(); loop();
  });
})();
