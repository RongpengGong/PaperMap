(function () {
  const form = document.getElementById("settings");
  const radios = form.querySelectorAll('input[name="provider"]');
  const result = document.getElementById("test_result");

  function current() { return form.querySelector('input[name="provider"]:checked').value; }
  function showPane() {
    const p = current();
    form.querySelectorAll(".pane").forEach(el => { el.hidden = el.dataset.pane !== p; });
    form.querySelectorAll(".choice").forEach(el => el.classList.toggle("active", el.querySelector("input").checked));
    result.textContent = ""; result.className = "muted";
  }
  radios.forEach(r => r.addEventListener("change", showPane));
  showPane();

  function payload() {
    const p = current();
    const v = id => (document.getElementById(id) || {}).value || "";
    if (p === "custom") {
      return { csrf: v("csrf"), provider: p, base_url: v("custom_base_url"), api_key: v("custom_api_key"),
               protocol: v("custom_protocol"), model: v("custom_model") };
    }
    return { csrf: v("csrf"), provider: p, api_key: v("official_api_key"), model: v("official_model") };
  }
  async function post(url) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload()) });
    if (!r.ok) return { ok: false, message: "请求失败 (HTTP " + r.status + ")，请刷新页面重试" };
    return r.json();
  }
  function show(ok, msg) { result.textContent = msg; result.className = ok ? "ok-text" : "err-text"; }

  document.getElementById("test_conn").addEventListener("click", async (e) => {
    e.target.disabled = true; show(true, "正在测试…"); result.className = "muted";
    try {
      const r = await post("/api/llm/test");
      show(r.ok, r.ok ? `✓ ${r.message}（${r.latency_ms} ms）` : `✗ ${r.message}`);
    } catch (err) { show(false, "✗ " + err); }
    e.target.disabled = false;
  });

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
