(function () {
  const form = document.getElementById("settings");
  const radios = form.querySelectorAll('input[name="provider"]');
  const result = document.getElementById("test_result");
  const accountProviders = new Set(["chatgpt_plan", "claude_cli"]);
  let accountPoll = null;
  let accountEpoch = 0;

  function current() { return form.querySelector('input[name="provider"]:checked').value; }
  function showPane() {
    const p = current();
    form.querySelectorAll(".pane").forEach(el => { el.hidden = el.dataset.pane !== p; });
    form.querySelectorAll(".choice").forEach(el => el.classList.toggle("active", el.querySelector("input").checked));
    result.textContent = ""; result.className = "muted";
    clearTimeout(accountPoll); const epoch = ++accountEpoch;
    if (accountProviders.has(p)) refreshAccount(p).then(state => {
      if (state.pending && epoch === accountEpoch) accountPoll = setTimeout(() => waitForAccount(p, Date.now() + 180000, epoch), 2500);
    });
  }
  radios.forEach(r => r.addEventListener("change", showPane));

  function payload() {
    const p = current();
    const v = id => (document.getElementById(id) || {}).value || "";
    if (p === "custom") {
      return { csrf: v("csrf"), provider: p, base_url: v("custom_base_url"), api_key: v("custom_api_key"),
               protocol: v("custom_protocol"), model: v("custom_model") };
    }
    if (p !== "deepseek") {
      const pane = form.querySelector(`.pane[data-pane="${p}"]`);
      return { csrf: v("csrf"), provider: p, api_key: (pane.querySelector('[data-role="key"]') || {}).value || "",
               model: pane.querySelector('[data-role="model"]').value };
    }
    return { csrf: v("csrf"), provider: p, api_key: v("official_api_key"), model: v("official_model") };
  }
  async function post(url, body = payload()) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) return { ok: false, message: "请求失败 (HTTP " + r.status + ")，请刷新页面重试" };
    return r.json();
  }
  function show(ok, msg) { result.textContent = msg; result.className = ok ? "ok-text" : "err-text"; }

  function showAccount(provider, state) {
    const el = form.querySelector(`[data-account-result="${provider}"]`);
    if (!el) return;
    const pending = Boolean(state.pending || state.status === "pending" || state.status === "exchanging");
    el.textContent = state.error || state.message || (pending ? "正在等待官方登录完成…" : (state.ready ? "已连接，可以使用订阅额度。" : "尚未连接，请完成官方登录。"));
    if (state.ready && state.email) el.textContent += "（" + state.email + "）";
    if (provider === "chatgpt_plan" && state.ready) el.textContent += " 该 ChatGPT 账号的套餐或余额将用于 PaperMap。";
    el.className = state.ready ? "ok-text small" : (state.ok === false || state.error ? "err-text small" : "muted small");
    const login = form.querySelector(`[data-account-start="${provider}"]`);
    if (login) {
      if (state.available !== undefined) login.dataset.available = String(state.available);
      login.dataset.pending = String(pending);
      if (state.needs_consent !== undefined || state.ready) login.dataset.consent = String(Boolean(state.needs_consent));
      if (provider === "chatgpt_plan") login.textContent = state.needs_consent ? "Continue with ChatGPT（重新授权）" : "Continue with ChatGPT";
      if (login.dataset.busy !== "1") login.disabled = pending || login.dataset.available === "false";
    }
    const cancel = form.querySelector(`[data-account-cancel="${provider}"]`);
    if (cancel) cancel.hidden = !pending;
    const link = form.querySelector(`[data-account-url="${provider}"]`);
    if (link && !pending) { link.hidden = true; link.removeAttribute("href"); }
  }
  async function accountAction(provider, action, body = {}) {
    return post(`/api/accounts/${provider}/${action}`, { csrf: document.getElementById("csrf").value, ...body });
  }
  async function refreshAccount(provider, refresh = false) {
    try {
      const state = await accountAction(provider, "status", { refresh });
      showAccount(provider, state); return state;
    } catch (err) {
      const state = { ok: false, message: "无法检查连接状态，请刷新重试。" };
      showAccount(provider, state); return state;
    }
  }
  async function waitForAccount(provider, deadline, epoch) {
    if (current() !== provider || Date.now() > deadline || epoch !== accountEpoch) return;
    const state = await refreshAccount(provider);
    if (epoch === accountEpoch && !state.ready && state.ok !== false && (provider === "claude_cli" || state.pending)) accountPoll = setTimeout(() => waitForAccount(provider, deadline, epoch), 2500);
  }
  form.querySelectorAll("[data-account-start]").forEach(btn => btn.addEventListener("click", async () => {
    const provider = btn.dataset.accountStart;
    const epoch = ++accountEpoch; clearTimeout(accountPoll);
    btn.dataset.busy = "1"; btn.disabled = true;
    showAccount(provider, { message: "正在打开官方登录…" });
    try {
      const state = await accountAction(provider, "login", { consent: btn.dataset.consent === "true" });
      showAccount(provider, state);
      const link = form.querySelector(`[data-account-url="${provider}"]`);
      if (link && state.url && /^https:\/\//i.test(state.url)) { link.href = state.url; link.hidden = false; }
      if (state.ok) {
        clearTimeout(accountPoll);
        accountPoll = setTimeout(() => waitForAccount(provider, Date.now() + 180000, epoch), 2000);
      }
    } catch (err) { showAccount(provider, { ok: false, message: "登录未能打开，请重试。" }); }
    finally { delete btn.dataset.busy; btn.disabled = btn.dataset.pending === "true" || btn.dataset.available === "false"; }
  }));
  form.querySelectorAll("[data-account-cancel]").forEach(btn => btn.addEventListener("click", async () => {
    clearTimeout(accountPoll); ++accountEpoch; btn.disabled = true;
    try { showAccount(btn.dataset.accountCancel, await accountAction(btn.dataset.accountCancel, "cancel")); }
    catch (err) { showAccount(btn.dataset.accountCancel, { ok: false, message: "取消失败，请刷新连接状态。" }); }
    finally { btn.disabled = false; }
  }));
  form.querySelectorAll("[data-account-status]").forEach(btn => btn.addEventListener("click", async () => {
    btn.disabled = true;
    try { await refreshAccount(btn.dataset.accountStatus, true); }
    finally { btn.disabled = false; }
  }));
  form.querySelectorAll("[data-account-logout]").forEach(btn => btn.addEventListener("click", async () => {
    const provider = btn.dataset.accountLogout;
    if (provider === "claude_cli" && !await window.pmDialog("注销这台电脑上的 Claude Code？其他使用同一登录的 Claude Code 会话也会受影响。", { okText: "注销" })) return;
    clearTimeout(accountPoll); ++accountEpoch; btn.disabled = true;
    try {
      const state = await accountAction(provider, "logout");
      showAccount(provider, state);
      const link = form.querySelector(`[data-account-url="${provider}"]`);
      if (link && state.ok) link.hidden = true;
      if (state.ok) await refreshAccount(provider);
    } catch (err) { showAccount(provider, { ok: false, message: "断开连接失败，请重试。" }); }
    finally { btn.disabled = false; }
  }));
  showPane();

  document.getElementById("test_conn").addEventListener("click", async (e) => {
    e.target.disabled = true; show(true, "正在测试…"); result.className = "muted";
    try {
      const r = await post("/api/llm/test");
      show(r.ok, r.ok ? `✓ ${r.message}（${r.latency_ms} ms）` : `✗ ${r.message}`);
    } catch (err) { show(false, "✗ " + err); }
    e.target.disabled = false;
  });

  form.querySelectorAll("[data-fetch-models]").forEach(btn => btn.addEventListener("click", async (e) => {
    const pane = btn.closest(".pane");
    e.target.disabled = true; show(true, "正在获取模型列表…"); result.className = "muted";
    try {
      const r = await post("/api/llm/models");
      if (r.ok) {
        const dl = pane.querySelector("datalist");
        if (pane.dataset.pane === "chatgpt_plan") {
          dl.innerHTML = "";
          const model = pane.querySelector('[data-role="model"]');
          if (!r.models.includes(model.value)) model.value = "";
        }
        const known = new Set([...dl.options].map(o => o.value));
        r.models.forEach(m => { if (!known.has(m)) { const o = document.createElement("option"); o.value = m; dl.appendChild(o); } });
        show(true, `✓ 找到 ${r.models.length} 个模型，点击「模型」输入框即可选择`);
        pane.querySelector('[data-role="model"]').focus();
      } else { show(false, "✗ " + r.message + "（也可以直接手动填写模型名）"); }
    } catch (err) { show(false, "✗ " + err); }
    e.target.disabled = false;
  }));

  document.getElementById("fetch_models").addEventListener("click", async (e) => {
    e.target.disabled = true; show(true, "正在获取模型列表…"); result.className = "muted";
    try {
      const r = await post("/api/llm/models");
      if (r.ok) {
        const dl = document.getElementById("model_list");
        dl.innerHTML = "";
        r.models.forEach(m => { const o = document.createElement("option"); o.value = m; dl.appendChild(o); });
        show(true, `✓ 找到 ${r.models.length} 个模型，点击「模型名」输入框即可选择`);
        if (!document.getElementById("custom_model").value && r.models.length) document.getElementById("custom_model").focus();
      } else { show(false, "✗ " + r.message + "（也可以直接手动填写模型名）"); }
    } catch (err) { show(false, "✗ " + err); }
    e.target.disabled = false;
  });

  // knowledge base location
  async function moveKb(body) {
    const pre = await window.pmApi("/api/knowledge/move", body);
    if (!pre.ok) return;
    const yes = await window.pmDialog(`把知识库（${pre.files} 个文件）移动到：\n${pre.target}\n\n移动期间暂停处理新论文；完成后原位置不再保留副本。`, { okText: "移动" });
    if (!yes) return;
    window.pmToast("正在移动知识库…");
    const r = await window.pmApi("/api/knowledge/move", { ...body, confirm: true });
    if (r.ok) { window.pmToast(`已移动 ${r.files} 个文件`); setTimeout(() => location.reload(), 800); }
  }
  const kbMove = document.getElementById("kb_move");
  kbMove && kbMove.addEventListener("click", () => {
    const path = document.getElementById("kb_new").value.trim();
    if (!path) { window.pmToast("请先选择或填写新位置"); return; }
    moveKb({ path });
  });
  const kbReset = document.getElementById("kb_reset");
  kbReset && kbReset.addEventListener("click", () => moveKb({ reset: true }));

  // translation services: add / delete custom endpoints
  const csrf = document.getElementById("csrf").value;
  const addBtn = document.getElementById("add_service_btn");
  addBtn && addBtn.addEventListener("click", async () => {
    const body = { csrf };
    document.querySelectorAll("#add_service [data-f]").forEach(el => body[el.dataset.f] = el.value);
    const j = await (await fetch("/api/translation/services", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
    if (j.ok) location.reload(); else window.pmToast(j.message || "添加失败");
  });
  document.querySelectorAll("[data-del-service]").forEach(b => b.addEventListener("click", async () => {
    const j = await (await fetch(`/api/translation/services/${b.dataset.delService}/delete`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ csrf }) })).json();
    if (j.ok) location.reload(); else window.pmToast(j.message || "删除失败");
  }));
})();
