// Squad settings: an ordered list of models per role, extra instructions, step limits, defaults.
(function () {
  const root = document.getElementById("sqs");
  if (!root) return;
  const remote = root.dataset.remote === "1", csrf = document.body.dataset.csrf;
  const roster = JSON.parse(document.getElementById("sqs_roster").textContent);
  const byKey = Object.fromEntries(roster.map(m => [m.key, m]));
  const $ = id => document.getElementById(id);
  const esc = s => { const d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; };
  const EFFORT = { minimal: t("最低"), low: t("低"), medium: t("中"), high: t("高"), xhigh: t("很高"), max: t("最高"), ultra: t("极高") };
  const price = m => m.billing === "subscription" ? t("订阅额度") : `$${m.price_in.toFixed(2)}/$${m.price_out.toFixed(2)}`;

  function modelOptions(cur) {
    const groups = {};
    roster.forEach(m => (groups[m.vendor] = groups[m.vendor] || []).push(m));
    return Object.entries(groups).map(([v, list]) => `<optgroup label="${esc(v)}">` + list.map(m =>
      `<option value="${esc(m.key)}" ${m.key === cur ? "selected" : ""}>${esc(m.channel)} · ${esc(m.model)}${m.main ? " (" + esc(t("主模型")) + ")" : ""} — ${esc(price(m))}</option>`).join("") + "</optgroup>").join("");
  }
  function effortOptions(key, cur) {
    const m = byKey[key] || { efforts: [] };
    return `<option value="">${esc(t("默认强度"))}</option>` + m.efforts.map(e =>
      `<option value="${esc(e)}" ${e === cur ? "selected" : ""}>${esc(EFFORT[e] || e)}</option>`).join("");
  }
  function drawCand(li) {
    const key = byKey[li.dataset.model] ? li.dataset.model : "main";
    const m = byKey[key] || {};
    li.innerHTML = `<select data-model ${remote ? "disabled" : ""}>${modelOptions(key)}</select>`
      + `<select data-effort ${remote || !(m.efforts || []).length ? "disabled" : ""}>${effortOptions(key, li.dataset.effort)}</select>`
      + `<span class="muted small">${esc(m.vendor || "")}</span>`
      + (remote ? "" : `<button type="button" class="icon-btn" data-up title="${esc(t("上移"))}">↑</button><button type="button" class="icon-btn" data-remove title="${esc(t("移除"))}">×</button>`);
  }
  document.querySelectorAll("[data-cand]").forEach(drawCand);

  root.addEventListener("change", e => {
    const li = e.target.closest("[data-cand]");
    if (!li) return;
    if (e.target.matches("[data-model]")) { li.dataset.model = e.target.value; li.dataset.effort = ""; drawCand(li); }
    if (e.target.matches("[data-effort]")) li.dataset.effort = e.target.value;
  });
  root.addEventListener("click", e => {
    const li = e.target.closest("[data-cand]");
    if (e.target.closest("[data-remove]") && li) { if (li.parentElement.children.length > 1) li.remove(); return; }
    if (e.target.closest("[data-up]") && li && li.previousElementSibling) { li.parentElement.insertBefore(li, li.previousElementSibling); return; }
    const add = e.target.closest("[data-add-cand]");
    if (add) {
      const ol = add.closest(".sqs-role").querySelector("[data-cands]");
      if (ol.children.length >= 5) return;
      const n = document.createElement("li"); n.dataset.cand = ""; n.dataset.model = "main"; n.dataset.effort = "";
      ol.appendChild(n); drawCand(n);
    }
  });

  function fill(roles) {
    for (const [role, cands] of Object.entries(roles)) {
      const ol = document.querySelector(`.sqs-role[data-role="${role}"] [data-cands]`);
      if (!ol) continue;
      ol.innerHTML = "";
      cands.forEach(c => { const n = document.createElement("li"); n.dataset.cand = ""; n.dataset.model = c.model; n.dataset.effort = c.effort || ""; ol.appendChild(n); drawCand(n); });
    }
    $("sqs_msg").textContent = t("已填入，确认无误后点「保存」");
  }
  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(Object.assign({ csrf }, body)) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) throw new Error(j.message || j.detail || t("请求失败"));
    return j;
  }
  function collect() {
    const roles = {}, disabled = {};
    document.querySelectorAll(".sqs-role").forEach(card => {
      const role = card.dataset.role;
      roles[role] = { candidates: [...card.querySelectorAll("[data-cand]")].map(li => ({ model: li.dataset.model, effort: li.dataset.effort || "" })),
                      extra: card.querySelector("[data-extra]").value, max_steps: card.querySelector("[data-steps]").value };
      card.querySelectorAll("[data-enable]").forEach(cb => { if (!cb.checked) (disabled[cb.dataset.enable] = disabled[cb.dataset.enable] || []).push(role); });
    });
    return { budget: $("sqs_budget").value, parallel: +$("sqs_parallel").value, default_cap_usd: +$("sqs_cap").value,
             max_calls: +$("sqs_calls").value, external: $("sqs_external").checked, handoff_quotes: $("sqs_handoff").checked, roles, disabled };
  }
  document.querySelectorAll("[data-estimate]").forEach(td => { td.textContent = window.pmSquadCost(JSON.parse(td.dataset.estimate)); });
  if (remote) return;
  $("sqs_save").addEventListener("click", async () => {
    $("sqs_save").disabled = true;
    try { await post("/api/squads/settings", collect()); location.reload(); }
    catch (err) { $("sqs_msg").textContent = err.message; $("sqs_save").disabled = false; }
  });
  $("sqs_recommend").addEventListener("click", async () => {
    try { fill((await post("/api/squads/recommend", { budget: $("sqs_budget").value })).roles); }
    catch (err) { $("sqs_msg").textContent = err.message; }
  });
  $("sqs_reset").addEventListener("click", () => {
    const roles = {};
    document.querySelectorAll(".sqs-role").forEach(card => { roles[card.dataset.role] = [{ model: "main", effort: "" }];
      card.querySelector("[data-extra]").value = ""; card.querySelector("[data-steps]").value = "";
      card.querySelectorAll("[data-enable]").forEach(cb => { cb.checked = true; }); });
    fill(roles);
  });
  $("sqs_refresh").addEventListener("click", async () => {
    $("sqs_refresh").disabled = true; $("sqs_msg").textContent = t("正在检查订阅…");
    try { await post("/api/squads/models/refresh", {}); location.reload(); }
    catch (err) { $("sqs_msg").textContent = err.message; $("sqs_refresh").disabled = false; }
  });
})();
