// Squad run page: polls the run's state, draws the task board, the event stream and the proposals.
(function () {
  const root = document.getElementById("squad");
  if (!root) return;
  const id = root.dataset.id, remote = root.dataset.remote === "1", csrf = document.body.dataset.csrf;
  const L = JSON.parse(document.getElementById("sq_labels").textContent);
  const $ = s => document.getElementById(s);
  const esc = s => { const d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; };
  const who = k => L.members[k] || (k === "user" ? t("你") : k === "system" ? "PaperMap" : k);
  let after = 0, tasks = [], timer = null, filter = "", lastStatus = "";
  const opened = new Set();

  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ csrf }, body)) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) throw new Error(j.message || j.detail || t("请求失败"));
    return j;
  }

  function board() {
    const cols = { queued: [], running: [], done: [] };
    for (const tk of tasks) (cols[tk.status === "running" ? "running" : tk.status === "queued" ? "queued" : "done"]).push(tk);
    for (const [col, list] of Object.entries(cols)) {
      const box = document.querySelector(`.squad-col[data-col="${col}"]`);
      box.querySelector(".n").textContent = list.length || "";
      box.querySelector(".squad-cards").innerHTML = list.map(tk => `
        <details class="squad-task st-${esc(tk.status)}" data-key="${esc(tk.key)}" ${opened.has(tk.key) ? "open" : ""}>
          <summary><b>${esc(who(tk.member))}</b>${tk.final ? ` <span class="chip">${esc(t("最终报告"))}</span>` : ""}
            <span class="squad-task-title">${esc(tk.title)}</span>
            <span class="muted small">${esc(tk.key)}${tk.blocked_by.length ? " · " + esc(t("等待")) + " " + tk.blocked_by.map(esc).join(", ") : ""}
              · ${esc(t("{n} 步").replace("{n}", tk.steps))} · $${(tk.cost_usd || 0).toFixed(3)}
              ${tk.status === "error" ? " · " + esc(L.status.error) : tk.status === "cancelled" ? " · " + esc(L.status.cancelled) : ""}</span>
          </summary>
          ${tk.instructions ? `<p class="small muted">${esc(tk.instructions)}</p>` : ""}
          ${tk.summary_html ? `<div class="md small">${tk.summary_html}</div>` : ""}
          <button type="button" class="link small" data-only="${esc(tk.key)}">${esc(t("只看它的过程"))}</button>
        </details>`).join("") || `<p class="muted small">—</p>`;
    }
    const to = $("sq_to");
    if (to) {
      const cur = to.value;
      to.innerHTML = `<option value="lead">${esc(L.members.lead)}</option>` + tasks.filter(x => x.status === "running" || x.status === "queued")
        .map(x => `<option value="${esc(x.key)}">${esc(who(x.member))} · ${esc(x.key)} · ${esc(x.title.slice(0, 30))}</option>`).join("");
      to.value = [...to.options].some(o => o.value === cur) ? cur : "lead";
    }
    const f = $("sq_filter"), fv = f.value;
    f.innerHTML = `<option value="">${esc(t("全部成员"))}</option><option value="lead">${esc(L.members.lead)}</option>` +
      tasks.map(x => `<option value="${esc(x.key)}">${esc(who(x.member))} · ${esc(x.key)}</option>`).join("");
    f.value = fv;
  }

  function eventLine(e) {
    const data = e.data_json ? JSON.parse(e.data_json) : {};
    let body = esc(e.text);
    if (e.kind === "plan") {
      const add = (data.added || []).map(a => `${esc(a.key)} → ${esc(who(a.member))}：${esc(a.title)}`);
      if (add.length) body += `<ul class="small">${add.map(x => `<li>${x}</li>`).join("")}</ul>`;
      if ((data.cancelled || []).length) body += `<div class="small muted">${esc(t("取消"))}：${data.cancelled.map(esc).join(", ")}</div>`;
    }
    if (e.kind === "action" && data.observation)
      body = `<code>${esc(e.text)}</code><details class="small"><summary>${esc(t("结果"))}</summary><pre>${esc(data.observation)}</pre></details>`;
    if (e.kind === "done") body = esc(t("完成。")) + (data.pending ? " " + esc(t("有 {n} 条提议等你确认。").replace("{n}", data.pending)) : "");
    if (e.kind === "start") body = esc(t("开始：")) + esc(e.text);
    if (e.kind === "user") body = (e.task_key && e.task_key !== "lead" ? `→ ${esc(e.task_key)} ` : `→ ${esc(L.members.lead)} `) + esc(e.text);
    const li = document.createElement("li");
    li.className = `sq-ev ev-${e.kind}`;
    li.dataset.task = e.who === "lead" ? "lead" : e.task_key || "";
    li.innerHTML = `<span class="sq-who">${esc(who(e.who))}${e.task_key && e.who !== "user" ? ` <i>${esc(e.task_key)}</i>` : ""}</span>
      <span class="sq-body">${e.kind === "result" ? `<details><summary>${esc(t("报告"))}</summary><div class="md">${esc(e.text).replace(/\n/g, "<br>")}</div></details>` : body}</span>
      <time class="muted small">${esc((e.created_at || "").slice(11, 19))}</time>`;
    li.hidden = !!filter && li.dataset.task !== filter;
    return li;
  }

  function proposalHtml(p) {
    const k = p.payload, kind = L.kinds[p.kind] || p.kind;
    let main = "";
    if (p.kind === "evidence") {
      const opt = (m, cur) => Object.entries(m).map(([v, l]) => `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(l)}</option>`).join("");
      main = `<div class="small muted">Q${k.question_id} · ${esc(k.question)}</div>
        <blockquote>${esc(k.quote)}</blockquote>
        <div class="small"><a href="/reader/${k.paper_id}?page=${k.page + 1}">${esc(k.who)} · p.${k.page + 1}</a> · ${esc(k.paper)}</div>
        ${p.status === "pending" && !remote ? `<div class="row small">
          <select data-edit="stance">${opt({ supports: L.stances.supports, opposes: L.stances.opposes, pending: L.stances.pending }, k.stance)}</select>
          <select data-edit="dimension">${opt(L.dimensions, k.dimension)}</select>
          <input data-edit="note" class="grow" value="${esc(k.note)}"></div>` : `<div class="small">${esc(L.stances[k.stance] || k.stance)} · ${esc(L.dimensions[k.dimension] || "")} · ${esc(k.note)}</div>`}`;
    } else if (p.kind === "judgment") {
      main = `<div class="small muted">Q${k.question_id} · ${esc(k.question)}</div>
        ${k.previous ? `<div class="small muted">${esc(t("现在的判断"))}：${esc(k.previous)}</div>` : ""}
        ${p.status === "pending" && !remote ? `<textarea data-edit="text" rows="4">${esc(k.readable)}</textarea>` : `<p>${esc(k.readable)}</p>`}`;
    } else if (p.kind === "gap") {
      main = `<div class="small muted">Q${k.question_id} · ${esc(k.question)}</div><p>${esc(k.text)}</p>
        ${(k.terms || []).length ? `<div class="small muted">${esc(t("检索词"))}：${k.terms.map(esc).join(", ")}</div>` : ""}`;
    } else if (p.kind === "question") {
      main = `<p><b>${esc(k.title)}</b></p><p class="small">${esc(k.rationale)}</p>`;
    } else {
      main = `<p><b>${esc(k.title)}</b> <span class="muted small">${esc(k.journal || "")} ${esc(k.year || "")}${k.doi ? " · " + esc(k.doi) : ""}</span></p>
        <p class="small">${esc(k.reason || "")}</p>${k.read_first ? `<p class="small">${esc(t("先读"))}：${esc(k.read_first)}</p>` : ""}`;
    }
    const state = p.status === "pending" ? (remote ? "" : `<div class="row"><button type="button" data-act="accept">${esc(t("接受"))}</button>
        <button type="button" class="secondary" data-act="reject">${esc(t("拒绝"))}</button><span class="sq-perr error small"></span></div>`)
      : `<span class="chip">${esc({ accepted: t("已接受"), rejected: t("已拒绝"), failed: t("失败") }[p.status] || p.status)}</span>`;
    return `<article class="card squad-prop sp-${esc(p.status)}" data-pid="${p.id}"><header class="small"><b>${esc(kind)}</b>
      <span class="muted">${esc(who(tasks.find(x => x.key === p.task_key)?.member || ""))} ${esc(p.task_key)}</span></header>${main}${state}</article>`;
  }

  function drawProposals(list) {
    const pending = list.filter(p => p.status === "pending");
    $("sq_pn").textContent = pending.length ? pending.length : "";
    const box = $("sq_props"), focus = document.activeElement && box.contains(document.activeElement);
    if (focus) return;  // do not redraw under the user's cursor
    const order = [...pending, ...list.filter(p => p.status !== "pending")];
    box.innerHTML = order.map(proposalHtml).join("") || `<p class="muted small">${esc(t("还没有提议。"))}</p>`;
  }

  async function poll() {
    clearTimeout(timer);
    let s;
    try { s = await (await fetch(`/api/squads/${id}/state?after=${after}`)).json(); } catch (_) { timer = setTimeout(poll, 4000); return; }
    tasks = s.tasks;
    const run = s.run;
    $("sq_status").textContent = L.status[run.status] || run.status;
    $("sq_status").className = `chip sq-${run.status}`;
    $("sq_money").textContent = `$${run.spent_usd.toFixed(3)}` + (run.cap_usd > 0 ? ` / $${run.cap_usd.toFixed(2)}` : "") + ` · ${t("{n} 次调用").replace("{n}", run.calls)}`;
    $("sq_msg").textContent = run.message || "";
    if ($("sq_stop")) $("sq_stop").hidden = !["queued", "running"].includes(run.status);
    board();
    const ol = $("sq_events");
    for (const e of s.events) { ol.appendChild(eventLine(e)); after = Math.max(after, e.id); }
    drawProposals(s.proposals);
    if (s.result_html) {
      $("sq_result_box").hidden = false; $("sq_result").innerHTML = s.result_html;
      $("sq_result_path").textContent = run.result_path;
    }
    const active = ["queued", "running", "stopping"].includes(run.status);
    lastStatus = run.status;
    timer = setTimeout(poll, active ? 2000 : 10000);
  }

  document.addEventListener("toggle", e => {
    const d = e.target.closest && e.target.closest(".squad-task");
    if (d && e.target === d) d.open ? opened.add(d.dataset.key) : opened.delete(d.dataset.key);
  }, true);
  document.addEventListener("click", async e => {
    const only = e.target.closest("[data-only]");
    if (only) { $("sq_filter").value = only.dataset.only; $("sq_filter").dispatchEvent(new Event("change")); return; }
    const act = e.target.closest(".squad-prop [data-act]");
    if (act) {
      const card = act.closest(".squad-prop"), edits = {};
      card.querySelectorAll("[data-edit]").forEach(x => { edits[x.dataset.edit] = x.value; });
      act.disabled = true;
      try { await post(`/api/squads/proposals/${card.dataset.pid}`, { action: act.dataset.act, edits }); card.querySelectorAll("button").forEach(b => b.disabled = true); act.blur(); poll(); }
      catch (err) { card.querySelector(".sq-perr").textContent = err.message; act.disabled = false; }
    }
  });
  $("sq_filter").addEventListener("change", () => {
    filter = $("sq_filter").value;
    document.querySelectorAll("#sq_events .sq-ev").forEach(li => { li.hidden = !!filter && li.dataset.task !== filter; });
  });
  if ($("sq_send")) {
    const send = async () => {
      const text = $("sq_text").value.trim();
      if (!text) return;
      $("sq_send").disabled = true;
      try { await post(`/api/squads/${id}/message`, { to: $("sq_to").value, text }); $("sq_text").value = ""; poll(); }
      catch (err) { alert(err.message); }
      $("sq_send").disabled = false;
    };
    $("sq_send").addEventListener("click", send);
    $("sq_text").addEventListener("keydown", e => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); send(); } });
    $("sq_stop").addEventListener("click", async () => { try { await post(`/api/squads/${id}/stop`, {}); poll(); } catch (err) { alert(err.message); } });
    $("sq_cap").addEventListener("change", async () => { try { await post(`/api/squads/${id}/cap`, { cap_usd: $("sq_cap").value }); poll(); } catch (err) { alert(err.message); } });
  }
  poll();
})();
