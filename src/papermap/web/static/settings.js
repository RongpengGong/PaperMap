(function () {
  const form = document.getElementById("settings");
  const providerPicker = document.getElementById("provider_picker");
  const modelSection = form.querySelector('[data-settings-panel="models"]');
  const result = document.getElementById("test_result");
  const accountProviders = new Set(["chatgpt_plan", "claude_cli"]);
  let accountPoll = null;
  let accountEpoch = 0;
  const chatgptPane = form.querySelector('.pane[data-pane="chatgpt_plan"]');
  const chatgptModel = chatgptPane && chatgptPane.querySelector('[data-role="model"]');
  const effortSelect = chatgptPane && chatgptPane.querySelector('[data-role="reasoning-effort"]');
  const effortValue = chatgptPane && chatgptPane.querySelector('[data-role="reasoning-value"]');
  const effortHint = chatgptPane && chatgptPane.querySelector('[data-reasoning-hint]');
  let effortModel = chatgptModel ? chatgptModel.value.trim() : "";
  const effortsByModel = new Map([[effortModel, effortValue ? effortValue.value : ""]]);
  let chatgptOptions = new Map();
  let catalogLoaded = false;
  let catalogSequence = 0;
  let catalogRequest = null;

  function option(value, label, disabled = false) {
    const el = document.createElement("option"); el.value = value; el.textContent = label; el.disabled = disabled;
    return el;
  }
  function renderEffort(modelInput, select, value, hint, choices, models) {
    if (!select) return;
    const model = modelInput.value.trim();
    let selected = choices.get(model) || "";
    const meta = models.get(model);
    const known = Boolean(meta && Array.isArray(meta.reasoning_efforts) && meta.reasoning_efforts_known !== false);
    const levels = known ? [...new Set(meta.reasoning_efforts.filter(x => typeof x === "string" && x))] : [];
    const defaultLevel = meta && typeof meta.default_reasoning_effort === "string" ? meta.default_reasoning_effort : "";
    select.replaceChildren(option("", defaultLevel ? `${t("默认（")}${defaultLevel}）` : t("默认（模型决定）")));
    levels.forEach(level => select.appendChild(option(level, level)));
    if (selected && known && !levels.includes(selected)) {
      hint.textContent = `${t("原强度")} ${selected} ${t("不在该模型支持的选项中，已选择默认；保存后生效。")}`;
      selected = ""; choices.set(model, "");
    } else if (selected && !known) {
      // Preserve an explicit saved choice on missing capabilities or network
      // failure. It is displayed, but cannot be newly selected without evidence.
      select.appendChild(option(selected, `${selected}${t("（已保存，待服务端确认）")}`, true));
      hint.textContent = t("尚未取得该模型的推理强度选项，保留已有选择；选择默认可清除。");
    } else if (!model) {
      hint.textContent = t("先选择或输入模型，连接后会自动获取它支持的推理强度。");
    } else if (!known) {
      hint.textContent = t("尚未取得该模型的推理强度选项，暂用默认；可点击获取模型列表重试。");
    } else {
      hint.textContent = levels.length ? t("仅显示该模型支持的强度；默认由模型决定。") : t("该模型未提供可选择的推理强度，使用默认。");
    }
    select.value = selected;
    value.value = selected;
  }
  function renderReasoning() {
    renderEffort(chatgptModel, effortSelect, effortValue, effortHint, effortsByModel, chatgptOptions);
  }
  function invalidateChatgptCatalog() {
    ++catalogSequence; catalogRequest = null; catalogLoaded = false; chatgptOptions = new Map();
    renderReasoning();
  }

  const claudePane = form.querySelector('.pane[data-pane="claude_cli"]');
  const claudeModel = claudePane && claudePane.querySelector('[data-role="model"]');
  const claudeSelect = claudePane && claudePane.querySelector('[data-role="reasoning-effort"]');
  const claudeValue = claudePane && claudePane.querySelector('[data-role="reasoning-value"]');
  const claudeHint = claudePane && claudePane.querySelector('[data-reasoning-hint]');
  const claudeChoices = new Map([[claudeModel ? claudeModel.value.trim() : "", claudeValue ? claudeValue.value : ""]]);
  let claudeOptions = new Map(), claudeSequence = 0, claudeTimer = null;
  function renderClaudeReasoning() {
    renderEffort(claudeModel, claudeSelect, claudeValue, claudeHint, claudeChoices, claudeOptions);
  }
  if (claudePane) {
    try { claudeOptions = new Map(JSON.parse(claudePane.querySelector('[data-claude-model-options]').textContent).map(x => [x.id, x])); }
    catch (err) { /* A missing capability document leaves only the default. */ }
    claudeModel.addEventListener("input", () => {
      renderClaudeReasoning(); clearTimeout(claudeTimer);
      if (current() === "claude_cli" && claudeModel.value.trim() && !claudeOptions.has(claudeModel.value.trim())) {
        claudeTimer = setTimeout(() => loadClaudeModels(true), 300);
      }
    });
    claudeModel.addEventListener("change", () => { clearTimeout(claudeTimer); renderClaudeReasoning(); loadClaudeModels(true); });
    claudeSelect.addEventListener("change", () => { claudeChoices.set(claudeModel.value.trim(), claudeSelect.value); renderClaudeReasoning(); });
    renderClaudeReasoning();
  }
  if (chatgptModel) {
    chatgptModel.addEventListener("input", renderReasoning);
    chatgptModel.addEventListener("change", () => {
      renderReasoning();
      if (current() === "chatgpt_plan" && !catalogLoaded) loadChatgptModels(true);
    });
    effortSelect.addEventListener("change", () => {
      effortsByModel.set(chatgptModel.value.trim(), effortSelect.value);
      renderReasoning();
    });
    renderReasoning();
  }

  function current() { return providerPicker.value; }
  function showPane() {
    const p = current();
    form.querySelectorAll(".pane").forEach(el => { el.hidden = el.dataset.pane !== p; });
    result.textContent = ""; result.className = "muted";
    clearTimeout(accountPoll); const epoch = ++accountEpoch;
    ++catalogSequence; catalogRequest = null; ++claudeSequence; clearTimeout(claudeTimer);
    if (chatgptPane) {
      const btn = chatgptPane.querySelector("[data-fetch-models]");
      btn.disabled = false; delete btn.dataset.modelRequest;
    }
    if (modelSection.hidden) return;
    if (accountProviders.has(p)) refreshAccount(p).then(state => {
      if (state.ready && p === "chatgpt_plan" && epoch === accountEpoch) loadChatgptModels(true);
      if (state.pending && epoch === accountEpoch) accountPoll = setTimeout(() => waitForAccount(p, Date.now() + 180000, epoch), 2500);
    });
    if (p === "claude_cli") { renderClaudeReasoning(); loadClaudeModels(true); }
  }
  providerPicker.addEventListener("change", showPane);
  document.addEventListener("papermap:settings-section", showPane);

  function payload(p = current()) {
    const v = id => (document.getElementById(id) || {}).value || "";
    if (p === "custom") {
      return { csrf: v("csrf"), provider: p, base_url: v("custom_base_url"), api_key: v("custom_api_key"),
               protocol: v("custom_protocol"), model: v("custom_model") };
    }
    if (p !== "deepseek") {
      const pane = form.querySelector(`.pane[data-pane="${p}"]`);
      const data = { csrf: v("csrf"), provider: p, api_key: (pane.querySelector('[data-role="key"]') || {}).value || "",
                     model: pane.querySelector('[data-role="model"]').value };
      if (accountProviders.has(p)) data.reasoning_effort = pane.querySelector('[data-role="reasoning-value"]').value;
      return data;
    }
    return { csrf: v("csrf"), provider: p, api_key: v("official_api_key"), model: v("official_model") };
  }
  async function post(url, body = payload()) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) return { ok: false, message: t("请求失败 (HTTP ") + r.status + t(")，请刷新页面重试") };
    return r.json();
  }
  function show(ok, msg) { result.textContent = msg; result.className = ok ? "ok-text" : "err-text"; }

  function loadChatgptModels(quiet = false, force = false) {
    if (!chatgptPane || current() !== "chatgpt_plan") return Promise.resolve(null);
    if (catalogRequest && !force) return catalogRequest;
    if (catalogLoaded && quiet && !force) return Promise.resolve(null);
    const seq = ++catalogSequence, epoch = accountEpoch;
    const btn = chatgptPane.querySelector("[data-fetch-models]");
    btn.disabled = true; btn.dataset.modelRequest = String(seq);
    if (!quiet) { show(true, t("正在获取模型和推理强度…")); result.className = "muted"; }
    const request = (async () => {
      try {
        const r = await post("/api/llm/models", payload("chatgpt_plan"));
        if (seq !== catalogSequence || epoch !== accountEpoch || current() !== "chatgpt_plan") return null;
        if (!r.ok) throw new Error(r.message || t("获取失败"));
        const ids = Array.isArray(r.models) ? r.models.filter(x => typeof x === "string" && x) : [];
        chatgptOptions = new Map((Array.isArray(r.model_options) ? r.model_options : [])
          .filter(x => x && typeof x.id === "string" && ids.includes(x.id)).map(x => [x.id, x]));
        catalogLoaded = true;
        const dl = chatgptPane.querySelector("datalist"); dl.replaceChildren();
        ids.forEach(id => { const m = chatgptOptions.get(id); dl.appendChild(option(id, m && m.label || id)); });
        // Keep a typed/saved model on a temporary catalog difference. The
        // backend validates availability when the user tests or starts a task.
        renderReasoning();
        if (!quiet) show(true, `${t("✓ 找到")} ${ids.length} ${t("个模型，推理强度会随所选模型更新")}`);
        return r;
      } catch (err) {
        if (seq !== catalogSequence || epoch !== accountEpoch || current() !== "chatgpt_plan") return null;
        // A failed refresh keeps the last confirmed choices and saved value.
        renderReasoning();
        effortHint.textContent = t("暂时无法获取模型能力，保留当前选择；可点击获取模型列表重试。");
        if (!quiet) show(false, "✗ " + err.message + t("（已保留当前模型和强度）"));
        return null;
      } finally {
        if (btn.dataset.modelRequest === String(seq)) { btn.disabled = false; delete btn.dataset.modelRequest; }
        if (seq === catalogSequence) catalogRequest = null;
      }
    })();
    catalogRequest = request;
    return request;
  }

  async function loadClaudeModels(quiet = false) {
    if (!claudePane || current() !== "claude_cli") return;
    const seq = ++claudeSequence, epoch = accountEpoch, model = claudeModel.value.trim();
    const btn = claudePane.querySelector("[data-fetch-models]");
    btn.disabled = true; btn.dataset.modelRequest = String(seq);
    try {
      const r = await post("/api/llm/models", payload("claude_cli"));
      if (seq !== claudeSequence || epoch !== accountEpoch || current() !== "claude_cli" || model !== claudeModel.value.trim()) return;
      if (!r.ok) throw new Error(r.message || t("获取失败"));
      (Array.isArray(r.model_options) ? r.model_options : []).forEach(m => { if (m && typeof m.id === "string") claudeOptions.set(m.id, m); });
      const dl = claudePane.querySelector("datalist"); dl.replaceChildren();
      const ids = Array.isArray(r.models) ? r.models : [];
      ids.forEach(id => dl.appendChild(option(id, (claudeOptions.get(id) || {}).label || id)));
      renderClaudeReasoning();
      if (!quiet) show(true, `${t("✓ 找到")} ${ids.length} ${t("个模型，推理强度会随所选模型更新")}`);
    } catch (err) {
      if (seq !== claudeSequence || epoch !== accountEpoch || current() !== "claude_cli") return;
      renderClaudeReasoning(); claudeHint.textContent = t("暂时无法更新模型能力，保留当前选择；可点击获取模型列表重试。");
      if (!quiet) show(false, "✗ " + err.message);
    } finally {
      if (btn.dataset.modelRequest === String(seq)) { btn.disabled = false; delete btn.dataset.modelRequest; }
    }
  }

  function showAccount(provider, state) {
    const el = form.querySelector(`[data-account-result="${provider}"]`);
    if (!el) return;
    const pending = Boolean(state.pending || state.status === "pending" || state.status === "exchanging");
    el.textContent = state.error || state.message || (pending ? t("正在等待官方登录完成…") : (state.ready ? t("已连接，可以使用订阅额度。") : t("尚未连接，请完成官方登录。")));
    if (state.ready && state.email) el.textContent += "（" + state.email + "）";
    if (provider === "chatgpt_plan" && state.ready) el.textContent += t(" 该 ChatGPT 账号的套餐或余额将用于 PaperMap。");
    el.className = state.ready ? "ok-text small" : (state.ok === false || state.error ? "err-text small" : "muted small");
    const login = form.querySelector(`[data-account-start="${provider}"]`);
    if (login) {
      if (state.available !== undefined) login.dataset.available = String(state.available);
      login.dataset.pending = String(pending);
      if (state.needs_consent !== undefined || state.ready) login.dataset.consent = String(Boolean(state.needs_consent));
      if (provider === "chatgpt_plan") login.textContent = state.needs_consent ? t("Continue with ChatGPT（重新授权）") : "Continue with ChatGPT";
      if (login.dataset.busy !== "1") login.disabled = pending || login.dataset.available === "false";
    }
    const logout = form.querySelector(`[data-account-logout="${provider}"]`);
    if (logout) {
      const connected = state.authenticated ?? state.connected;
      if (state.ready || connected === true) logout.hidden = false;
      else if (connected === false) logout.hidden = true;
    }
    const cancel = form.querySelector(`[data-account-cancel="${provider}"]`);
    if (cancel) cancel.hidden = !pending;
    const link = form.querySelector(`[data-account-url="${provider}"]`);
    if (link && !pending) { link.hidden = true; link.removeAttribute("href"); }
  }
  async function accountAction(provider, action, body = {}) {
    return post(`/api/accounts/${provider}/${action}`, { csrf: document.getElementById("csrf").value, ...body });
  }
  async function refreshAccount(provider, refresh = false, epoch = accountEpoch) {
    try {
      const state = await accountAction(provider, "status", { refresh });
      if (epoch !== accountEpoch || current() !== provider) return state;
      showAccount(provider, state); return state;
    } catch (err) {
      const state = { ok: false, message: t("无法检查连接状态，请刷新重试。") };
      if (epoch === accountEpoch && current() === provider) showAccount(provider, state);
      return state;
    }
  }
  async function waitForAccount(provider, deadline, epoch) {
    if (current() !== provider || Date.now() > deadline || epoch !== accountEpoch) return;
    const state = await refreshAccount(provider);
    if (epoch === accountEpoch && state.ready && provider === "chatgpt_plan") loadChatgptModels(true);
    if (epoch === accountEpoch && !state.ready && state.ok !== false && (provider === "claude_cli" || state.pending)) accountPoll = setTimeout(() => waitForAccount(provider, deadline, epoch), 2500);
  }
  form.querySelectorAll("[data-account-start]").forEach(btn => btn.addEventListener("click", async () => {
    const provider = btn.dataset.accountStart;
    const epoch = ++accountEpoch; clearTimeout(accountPoll);
    if (provider === "chatgpt_plan") invalidateChatgptCatalog();
    btn.dataset.busy = "1"; btn.disabled = true;
    showAccount(provider, { message: t("正在打开官方登录…") });
    try {
      const state = await accountAction(provider, "login", { consent: btn.dataset.consent === "true" });
      if (epoch !== accountEpoch || current() !== provider) return;
      showAccount(provider, state);
      const link = form.querySelector(`[data-account-url="${provider}"]`);
      if (link && state.url && /^https:\/\//i.test(state.url)) { link.href = state.url; link.hidden = false; }
      if (state.ok) {
        clearTimeout(accountPoll);
        accountPoll = setTimeout(() => waitForAccount(provider, Date.now() + 180000, epoch), 2000);
      }
    } catch (err) { if (epoch === accountEpoch && current() === provider) showAccount(provider, { ok: false, message: t("登录未能打开，请重试。") }); }
    finally { delete btn.dataset.busy; btn.disabled = btn.dataset.pending === "true" || btn.dataset.available === "false"; }
  }));
  form.querySelectorAll("[data-account-cancel]").forEach(btn => btn.addEventListener("click", async () => {
    clearTimeout(accountPoll); const epoch = ++accountEpoch; btn.disabled = true;
    if (btn.dataset.accountCancel === "chatgpt_plan") invalidateChatgptCatalog();
    try { const state = await accountAction(btn.dataset.accountCancel, "cancel"); if (epoch === accountEpoch) showAccount(btn.dataset.accountCancel, state); }
    catch (err) { showAccount(btn.dataset.accountCancel, { ok: false, message: t("取消失败，请刷新连接状态。") }); }
    finally { btn.disabled = false; }
  }));
  form.querySelectorAll("[data-account-status]").forEach(btn => btn.addEventListener("click", async () => {
    btn.disabled = true;
    const epoch = accountEpoch;
    try { const state = await refreshAccount(btn.dataset.accountStatus, true, epoch);
      if (epoch === accountEpoch && state.ready && btn.dataset.accountStatus === "chatgpt_plan") loadChatgptModels(true, true); }
    finally { btn.disabled = false; }
  }));
  form.querySelectorAll("[data-account-logout]").forEach(btn => btn.addEventListener("click", async () => {
    const provider = btn.dataset.accountLogout;
    if (provider === "claude_cli" && !await window.pmDialog(t("注销这台电脑上的 Claude Code？其他使用同一登录的 Claude Code 会话也会受影响。"), { okText: t("注销") })) return;
    clearTimeout(accountPoll); const epoch = ++accountEpoch; btn.disabled = true;
    if (provider === "chatgpt_plan") invalidateChatgptCatalog();
    try {
      const state = await accountAction(provider, "logout");
      if (epoch !== accountEpoch || current() !== provider) return;
      showAccount(provider, state);
      const link = form.querySelector(`[data-account-url="${provider}"]`);
      if (link && state.ok) link.hidden = true;
      if (state.ok) await refreshAccount(provider);
    } catch (err) { showAccount(provider, { ok: false, message: t("断开连接失败，请重试。") }); }
    finally { btn.disabled = false; }
  }));
  showPane();

  document.getElementById("test_conn").addEventListener("click", async (e) => {
    e.target.disabled = true; show(true, t("正在测试…")); result.className = "muted";
    const body = payload(), epoch = accountEpoch;
    try {
      const r = await post("/api/llm/test", body);
      if (epoch !== accountEpoch || JSON.stringify(payload()) !== JSON.stringify(body)) return;
      show(r.ok, r.ok ? `✓ ${r.message}（${r.latency_ms} ms）` : `✗ ${r.message}`);
    } catch (err) { if (epoch === accountEpoch && JSON.stringify(payload()) === JSON.stringify(body)) show(false, "✗ " + err); }
    e.target.disabled = false;
  });

  form.querySelectorAll("[data-fetch-models]").forEach(btn => btn.addEventListener("click", async (e) => {
    const pane = btn.closest(".pane");
    const provider = pane.dataset.pane, epoch = accountEpoch;
    if (provider === "chatgpt_plan") { await loadChatgptModels(false, true); return; }
    if (provider === "claude_cli") { await loadClaudeModels(false); return; }
    e.target.disabled = true; show(true, t("正在获取模型列表…")); result.className = "muted";
    try {
      const r = await post("/api/llm/models", payload(provider));
      if (current() !== provider || epoch !== accountEpoch) return;
      if (r.ok) {
        const dl = pane.querySelector("datalist");
        const known = new Set([...dl.options].map(o => o.value));
        r.models.forEach(m => { if (!known.has(m)) { const o = document.createElement("option"); o.value = m; dl.appendChild(o); } });
        show(true, `${t("✓ 找到")} ${r.models.length} ${t("个模型，点击「模型」输入框即可选择")}`);
        pane.querySelector('[data-role="model"]').focus();
      } else { show(false, "✗ " + r.message + t("（也可以直接手动填写模型名）")); }
    } catch (err) { if (current() === provider && epoch === accountEpoch) show(false, "✗ " + err); }
    e.target.disabled = false;
  }));

  document.getElementById("fetch_models").addEventListener("click", async (e) => {
    e.target.disabled = true; show(true, t("正在获取模型列表…")); result.className = "muted";
    const epoch = accountEpoch;
    try {
      const r = await post("/api/llm/models", payload("custom"));
      if (current() !== "custom" || epoch !== accountEpoch) return;
      if (r.ok) {
        const dl = document.getElementById("model_list");
        dl.innerHTML = "";
        r.models.forEach(m => { const o = document.createElement("option"); o.value = m; dl.appendChild(o); });
        show(true, `${t("✓ 找到")} ${r.models.length} ${t("个模型，点击「模型名」输入框即可选择")}`);
        if (!document.getElementById("custom_model").value && r.models.length) document.getElementById("custom_model").focus();
      } else { show(false, "✗ " + r.message + t("（也可以直接手动填写模型名）")); }
    } catch (err) { if (current() === "custom" && epoch === accountEpoch) show(false, "✗ " + err); }
    e.target.disabled = false;
  });

  // appearance: apply at once, save, retint the native window
  const root = document.documentElement;
  async function setTheme(palette, mode) {
    root.dataset.palette = palette; root.dataset.appearance = mode;
    document.querySelectorAll("[data-palette-pick]").forEach(b => { const on = b.dataset.palettePick === palette; b.classList.toggle("on", on); b.setAttribute("aria-checked", on); });
    document.querySelectorAll("[data-mode-pick]").forEach(b => { const on = b.dataset.modePick === mode; b.classList.toggle("on", on); b.setAttribute("aria-checked", on); });
    try { window.pywebview && window.pywebview.api.set_theme && window.pywebview.api.set_theme(palette, mode); } catch (_) {}
    await window.pmApi("/api/appearance", { palette, mode });
  }
  document.querySelectorAll("[data-palette-pick]").forEach(b => b.addEventListener("click", () => setTheme(b.dataset.palettePick, root.dataset.appearance || "system")));
  document.querySelectorAll("[data-mode-pick]").forEach(b => b.addEventListener("click", () => setTheme(root.dataset.palette || "blue", b.dataset.modePick)));
  document.querySelectorAll("[data-lang-pick]").forEach(b => b.addEventListener("click", async () => {
    if (b.classList.contains("on")) return;
    const j = await window.pmApi("/api/appearance", { language: b.dataset.langPick });
    if (j.ok) location.reload();
  }));

  // backups
  const bStatus = document.getElementById("backup_status");
  function showReport(r) {
    const box = document.getElementById("backup_report");
    if (!box) return;
    const lines = [`${t("共")} ${r.papers} ${t("篇论文：卡片找到")} ${r.cards} ${t("篇，PDF 找到")} ${r.pdfs} ${t("篇，笔记找到")} ${r.notes} ${t("篇。")}`,
      `${t("证据摘录")} ${r.evidence} ${t("条：能回到原句")} ${r.evidence_ok} ${t("条")}` +
      (r.evidence_changed ? `${t("，PDF 已变化")} ${r.evidence_changed} ${t("条（需要核对）")}` : "") +
      (r.evidence_missing ? `${t("，PDF 找不到")} ${r.evidence_missing} ${t("条")}` : "") + "。"];
    if (r.missing.length) lines.push(t("缺卡片或 PDF 的论文：") + r.missing.slice(0, 20).map(m =>
      `${m.label}（${!m.card ? "无卡片" : ""}${!m.card && !m.pdf ? "、" : ""}${!m.pdf ? "无 PDF" : ""}）`).join("；") +
      (r.missing.length > 20 ? ` ${t("等")} ${r.missing.length} ${t("篇")}` : ""));
    box.textContent = lines.join("\n"); box.style.whiteSpace = "pre-line"; box.hidden = false;
    box.className = "notice " + (r.missing.length || r.evidence_missing || r.evidence_changed ? "warn" : "ok");
  }
  const bNow = document.getElementById("backup_now");
  bNow && bNow.addEventListener("click", async () => {
    bNow.disabled = true; bStatus.textContent = t("正在备份…");
    const r = await window.pmApi("/api/backup/create");
    bNow.disabled = false;
    if (r.ok) { bStatus.textContent = t("已保存 ") + r.name; setTimeout(() => location.reload(), 700); } else bStatus.textContent = "";
  });
  const bCheck = document.getElementById("backup_check");
  bCheck && bCheck.addEventListener("click", async () => {
    bStatus.textContent = t("正在检查…");
    const r = await (await fetch("/api/backup/check")).json();
    bStatus.textContent = ""; if (r.ok) showReport(r.report);
  });
  document.querySelectorAll("[data-restore]").forEach(btn => btn.addEventListener("click", async () => {
    const yes = await window.pmDialog(`${t("用这份备份替换当前的数据库和设置？\n\n知识库文件夹（卡片、笔记、PDF）不会改动。恢复前会自动再备份一次当前状态，可以随时撤销。")}`, { okText: t("恢复"), danger: true });
    if (!yes) return;
    bStatus.textContent = t("正在恢复…");
    const r = await window.pmApi("/api/backup/restore", { name: btn.dataset.restore });
    bStatus.textContent = r.ok ? `${t("已恢复。撤销用备份：")}${r.undo}${t("。建议重启 PaperMap。")}` : "";
    if (r.ok) showReport(r.report);
  }));

  // knowledge base location
  async function moveKb(body) {
    const pre = await window.pmApi("/api/knowledge/move", body);
    if (!pre.ok) return;
    const yes = await window.pmDialog(`${t("把知识库（")}${pre.files} ${t("个文件）移动到：\n")}${pre.target}${t("\n\n移动期间暂停处理新论文；完成后原位置不再保留副本。")}`, { okText: t("移动") });
    if (!yes) return;
    window.pmToast(t("正在移动知识库…"));
    const r = await window.pmApi("/api/knowledge/move", { ...body, confirm: true });
    if (r.ok) { window.pmToast(`${t("已移动")} ${r.files} ${t("个文件")}`); setTimeout(() => location.reload(), 800); }
  }
  const kbMove = document.getElementById("kb_move");
  kbMove && kbMove.addEventListener("click", () => {
    const path = document.getElementById("kb_new").value.trim();
    if (!path) { window.pmToast(t("请先选择或填写新位置")); return; }
    moveKb({ path });
  });
  const kbReset = document.getElementById("kb_reset");
  kbReset && kbReset.addEventListener("click", () => moveKb({ reset: true }));

  const translationPicker = document.getElementById("tr_default");
  if (translationPicker) {
    function showTranslationService() {
      form.querySelectorAll("[data-tr-pane]").forEach(pane => {
        pane.hidden = pane.dataset.trPane !== translationPicker.value;
      });
    }
    translationPicker.addEventListener("change", showTranslationService);
    showTranslationService();
  }

  // Add or delete a custom translation endpoint.
  const csrf = document.getElementById("csrf").value;
  const addBtn = document.getElementById("add_service_btn");
  addBtn && addBtn.addEventListener("click", async () => {
    const body = { csrf };
    document.querySelectorAll("#add_service [data-f]").forEach(el => body[el.dataset.f] = el.value);
    const j = await (await fetch("/api/translation/services", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
    if (j.ok) location.reload(); else window.pmToast(j.message || t("添加失败"));
  });
  document.querySelectorAll("[data-del-service]").forEach(b => b.addEventListener("click", async () => {
    const j = await (await fetch(`/api/translation/services/${b.dataset.delService}/delete`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ csrf }) })).json();
    if (j.ok) location.reload(); else window.pmToast(j.message || t("删除失败"));
  }));
})();
