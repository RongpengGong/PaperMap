(function () {
  const panel = document.querySelector('[data-settings-panel="extensions"]');
  if (!panel) return;
  const list = document.getElementById("extensions-list");
  const status = document.getElementById("extension-status");
  const output = document.getElementById("extension-output");
  const save = document.getElementById("settings_save");
  const scopes = {};
  for (const area of panel.querySelectorAll("[data-token-scope]")) {
    const name = area.dataset.tokenScope;
    scopes[name] = {
      enabled: false, busy: false, token: document.getElementById(name + "-token"),
      secret: document.getElementById(name + "-secret"), create: document.getElementById(name + "-token-create"),
      revoke: document.getElementById(name + "-token-revoke"), endpoint: name === "mcp" ? "mcp-token" : "token"
    };
  }
  let request = 0;
  document.getElementById("extension-api-url").textContent = location.origin + "/api/v1";
  const mcpURL = location.origin + "/mcp";
  document.getElementById("mcp-api-url").textContent = mcpURL;
  document.getElementById("mcp-client-config").textContent = JSON.stringify({
    mcpServers: { papermap: { type: "http", url: mcpURL, headers: { Authorization: "Bearer ${PAPERMAP_MCP_TOKEN}" } } }
  }, null, 2);

  function clearSecrets() {
    for (const scope of Object.values(scopes)) {
      scope.token.value = ""; scope.secret.hidden = true;
    }
  }

  async function refresh() {
    const seq = ++request;
    const response = await fetch("/api/extensions/list", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({csrf: document.body.dataset.csrf})
    });
    if (!response.ok) throw Error(t("插件列表读取失败，请重试。"));
    const html = await response.text();
    if (seq !== request) return;
    list.innerHTML = html;
    const state = list.firstElementChild.dataset;
    for (const [name, scope] of Object.entries(scopes)) {
      scope.enabled = (name === "mcp" ? state.mcpEnabled : state.apiEnabled) === "yes";
      scope.create.textContent = scope.enabled ? t("更换令牌…") : (name === "mcp" ? t("创建只读令牌") : t("创建访问令牌"));
      scope.create.disabled = scope.busy;
      scope.revoke.hidden = !scope.enabled;
    }
  }
  function onSection() {
    save.hidden = !panel.hidden;
    if (panel.hidden) {
      clearSecrets();
    } else {
      refresh().catch(error => { status.textContent = error.message; });
    }
  }
  window.addEventListener("pagehide", clearSecrets);
  window.addEventListener("pageshow", event => { if (event.persisted) onSection(); });
  document.addEventListener("papermap:settings-section", onSection);
  onSection();

  panel.addEventListener("click", async event => {
    const button = event.target.closest("button[data-ext-action]");
    if (!button || button.disabled) return;
    const action = button.dataset.extAction;
    const area = button.closest("[data-token-scope]");
    const scope = area && scopes[area.dataset.tokenScope];
    if (scope && scope.busy) return;
    const id = encodeURIComponent(button.dataset.plugin || "");
    button.disabled = true;
    status.textContent = ""; output.hidden = true;
    try {
      if (action === "refresh") { await refresh(); return; }
      if (action === "copy-config") {
        const config = document.getElementById("mcp-client-config");
        try {
          await navigator.clipboard.writeText(config.textContent);
          status.textContent = t("客户端配置已复制。");
        } catch (_) {
          config.focus();
          const range = document.createRange();
          range.selectNodeContents(config);
          const selection = window.getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
          status.textContent = t("已选中配置，可按 ⌘C 或 Ctrl+C 复制。");
        }
        return;
      }
      if (action === "copy") {
        const token = scope.token;
        token.select();
        try { await navigator.clipboard.writeText(token.value); status.textContent = t("令牌已复制。"); }
        catch (_) { status.textContent = t("已选中令牌，可按 ⌘C 或 Ctrl+C 复制。"); }
        return;
      }
      let result;
      if (action === "install" || action === "example") {
        const path = document.getElementById("extension-folder").value.trim();
        if (action === "install" && !path) throw Error(t("请先选择插件文件夹。"));
        result = await pmApi("/api/extensions/install", action === "example" ? {example: button.dataset.example} : {path});
        if (result.ok) status.textContent = t("插件已安装。启用后生效。");
      } else if (action === "toggle") {
        const enabled = button.dataset.enabled !== "yes";
        if (enabled && button.dataset.code === "yes" && !await pmDialog(
          t("信任「") + button.dataset.name + t("」的 Python 代码？声明的权限：") + button.dataset.permissions +
          t("。代码以你的系统账号运行，可以访问本机文件和凭据。"), {okText:t("信任并启用")})) return;
        result = await pmApi("/api/extensions/" + id + "/enabled", {enabled, digest: button.dataset.digest});
        if (result.ok) status.textContent = enabled ? t("插件已启用。重新打开相关页面即可使用。") : t("插件已停用。");
      } else if (action === "run" || action === "retry") {
        result = await pmApi("/api/extensions/" + id + "/" + action);
        if (result.ok && action === "run") {
          output.textContent = JSON.stringify(result.result, null, 2);
          output.hidden = false;
        }
        if (result.ok) status.textContent = action === "run" ? t("运行完成。") : t("已恢复事件处理。");
      } else if (action === "token" || action === "revoke") {
        scope.busy = true;
        if (scope.enabled && !await pmDialog(t("现有令牌会立即失效，已连接的工具需要重新授权。"), {okText:action === "token" ? t("更换") : t("撤销")})) return;
        result = await pmApi("/api/extensions/" + scope.endpoint + "/" + (action === "token" ? "rotate" : "revoke"));
        if (result.ok) {
          scope.enabled = action === "token";
          scope.token.value = !panel.hidden ? result.token || "" : "";
          scope.secret.hidden = !scope.token.value;
          status.textContent = action === "revoke" ? (scope === scopes.mcp ? t("已撤销 MCP 只读访问。") : t("已撤销本地 API 访问。")) : t("请将令牌保存到需要访问 PaperMap 的本机工具。");
        }
      }
      if (result?.ok) await refresh();
      else if (result) status.textContent = result.message || t("操作失败，请刷新后重试。");
    } catch (error) { status.textContent = error.message; }
    finally {
      if (scope) { scope.busy = false; scope.create.disabled = false; }
      button.disabled = false;
    }
  });
})();
