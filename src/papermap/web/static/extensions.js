(function () {
  const panel = document.querySelector('[data-settings-panel="extensions"]');
  if (!panel) return;
  const list = document.getElementById("extensions-list");
  const status = document.getElementById("extension-status");
  const output = document.getElementById("extension-output");
  const token = document.getElementById("extension-token");
  const secret = document.getElementById("extension-secret");
  const save = document.getElementById("settings_save");
  let tokenEnabled = false;
  let request = 0;
  document.getElementById("extension-api-url").textContent = location.origin + "/api/v1";

  async function refresh() {
    const seq = ++request;
    const response = await fetch("/api/extensions/list", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({csrf: document.body.dataset.csrf})
    });
    if (!response.ok) throw Error("插件列表读取失败，请重试。");
    const html = await response.text();
    if (seq !== request) return;
    list.innerHTML = html;
    tokenEnabled = list.firstElementChild.dataset.apiEnabled === "yes";
    document.getElementById("extension-token-create").textContent = tokenEnabled ? "更换令牌…" : "创建访问令牌";
    document.getElementById("extension-token-revoke").hidden = !tokenEnabled;
  }
  function onSection() {
    save.hidden = !panel.hidden;
    if (panel.hidden) {
      token.value = ""; secret.hidden = true;
    } else {
      refresh().catch(error => { status.textContent = error.message; });
    }
  }
  window.addEventListener("pagehide", () => { token.value = ""; secret.hidden = true; });
  window.addEventListener("pageshow", event => { if (event.persisted) onSection(); });
  document.addEventListener("papermap:settings-section", onSection);
  onSection();

  panel.addEventListener("click", async event => {
    const button = event.target.closest("button[data-ext-action]");
    if (!button || button.disabled) return;
    const action = button.dataset.extAction;
    const id = encodeURIComponent(button.dataset.plugin || "");
    button.disabled = true;
    status.textContent = ""; output.hidden = true;
    try {
      if (action === "refresh") { await refresh(); return; }
      if (action === "copy") {
        token.select();
        try { await navigator.clipboard.writeText(token.value); status.textContent = "令牌已复制。"; }
        catch (_) { status.textContent = "已选中令牌，可按 ⌘C 或 Ctrl+C 复制。"; }
        return;
      }
      let result;
      if (action === "install" || action === "example") {
        const path = document.getElementById("extension-folder").value.trim();
        if (action === "install" && !path) throw Error("请先选择插件文件夹。");
        result = await pmApi("/api/extensions/install", action === "example" ? {example: button.dataset.example} : {path});
        if (result.ok) status.textContent = "插件已安装。启用后生效。";
      } else if (action === "toggle") {
        const enabled = button.dataset.enabled !== "yes";
        if (enabled && button.dataset.code === "yes" && !await pmDialog(
          "信任「" + button.dataset.name + "」的 Python 代码？声明的权限：" + button.dataset.permissions +
          "。代码以你的系统账号运行，可以访问本机文件和凭据。", {okText:"信任并启用"})) return;
        result = await pmApi("/api/extensions/" + id + "/enabled", {enabled, digest: button.dataset.digest});
        if (result.ok) status.textContent = enabled ? "插件已启用。重新打开相关页面即可使用。" : "插件已停用。";
      } else if (action === "run" || action === "retry") {
        result = await pmApi("/api/extensions/" + id + "/" + action);
        if (result.ok && action === "run") {
          output.textContent = JSON.stringify(result.result, null, 2);
          output.hidden = false;
        }
        if (result.ok) status.textContent = action === "run" ? "运行完成。" : "已恢复事件处理。";
      } else if (action === "token" || action === "revoke") {
        if (tokenEnabled && !await pmDialog("现有令牌会立即失效，已连接的工具需要重新授权。", {okText:action === "token" ? "更换" : "撤销"})) return;
        result = await pmApi("/api/extensions/token/" + (action === "token" ? "rotate" : "revoke"));
        if (result.ok) {
          token.value = !panel.hidden ? result.token || "" : "";
          secret.hidden = !token.value;
          status.textContent = action === "revoke" ? "已撤销本地 API 访问。" : "请将令牌保存到需要访问 PaperMap 的本机工具。";
        }
      }
      if (result?.ok) await refresh();
      else if (result) status.textContent = result.message || "操作失败，请刷新后重试。";
    } catch (error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  });
})();
