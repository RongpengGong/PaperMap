"""Subscription settings and account actions use official adapters, without real logins."""

import re

import pytest
from fastapi.testclient import TestClient

from papermap.pipeline import Pipeline
from papermap.web.app import create_app

from .conftest import FakeMetadata


@pytest.fixture
def subscription_client(config, registry, monkeypatch):
    # Rendering settings must not inspect credentials on the developer's machine.
    monkeypatch.setattr(type(config), "model_ready", lambda self: False)
    pipe = Pipeline(config, registry, metadata=FakeMetadata())
    app = create_app(config, registry, pipe)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    token = re.search(r'id="csrf" value="([^"]+)"', client.get("/settings").text).group(1)
    return client, token


def test_subscription_choices_do_not_request_api_keys(subscription_client):
    client, _ = subscription_client
    html = client.get("/settings").text
    picker = re.search(r'<select\b(?=[^>]*\bid="provider_picker")(?=[^>]*\bname="provider")[^>]*>(.*?)</select>', html, re.S)
    assert picker, "One native provider selector must remain the submitted input"
    for provider in ("chatgpt_plan", "claude_cli", "antigravity_acp"):
        assert f'value="{provider}"' in picker.group(1)
        assert f'data-account-start="{provider}"' in html
        assert f'data-account-status="{provider}"' in html
        assert f'name="p_{provider}_key"' not in html
        assert f'name="p_{provider}_model"' in html
    assert "https://chatgpt.com/settings/usage" in html
    assert "注销本机 Claude Code" in html
    assert "按量使用 API 请单独选择「Anthropic Claude API」" in html


def test_claude_quota_is_localized_without_requiring_login_again(subscription_client, config, monkeypatch):
    from unittest.mock import Mock
    from papermap.claude_cli import ClaudeUsageLimit

    client, token = subscription_client
    config.raw["appearance"]["language"] = "en"
    cli = Mock()
    cli.chat.completions.create.side_effect = ClaudeUsageLimit("12:10", "Europe/Berlin")
    monkeypatch.setattr("papermap.llm.make_client", lambda *args, **kwargs: cli)
    result = client.post("/api/llm/test", json={"csrf": token, "provider": "claude_cli", "model": "sonnet"}).json()
    assert not result["ok"] and result["reset_time"] == "12:10" and result["reset_zone"] == "Europe/Berlin"
    assert "12:10" in result["message"] and "Europe/Berlin" in result["message"] and "额度" not in result["message"]


@pytest.mark.parametrize("provider", ["chatgpt_plan", "claude_cli", "antigravity_acp"])
def test_subscription_provider_and_model_saved(subscription_client, config, provider):
    client, token = subscription_client
    response = client.post("/settings", data={
        "csrf": token, "provider": provider, f"p_{provider}_model": "selected-model",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert config.llm["provider"] == provider
    assert config.llm["presets"][provider]["model"] == "selected-model"
    assert "模型订阅尚未连接" in client.get("/").text


def test_account_actions_validate_csrf_and_never_return_credentials(subscription_client, config, monkeypatch):
    client, token = subscription_client
    calls = []

    class FakeAccount:
        def status(self):
            calls.append("status")
            return {"ready": True, "access_token": "private-access", "refresh_token": "private-refresh",
                    "auth_method": "claude.ai", "message": "已连接"}

        def start(self, *, consent=False):
            calls.append("start")
            return {"url": "https://auth.openai.com/authorize?state=public-state", "code_verifier": "private-verifier"}

        def login(self):
            calls.append("login")
            return {"ok": True, "message": "已打开官方终端"}

        def logout(self):
            calls.append("logout")
            return {"ok": True, "message": "已注销"}

    def fake_client(provider, data_dir):
        assert data_dir == config.data_dir
        return FakeAccount()

    monkeypatch.setattr("papermap.web.app._account_client", fake_client)
    for provider in ("chatgpt_plan", "claude_cli"):
        for action in ("login", "status", "logout"):
            assert client.post(f"/api/accounts/{provider}/{action}", json={"csrf": "wrong"}).status_code == 403
    assert not calls

    login = client.post("/api/accounts/chatgpt_plan/login", json={"csrf": token})
    assert login.json()["url"].startswith("https://auth.openai.com/")
    assert "private" not in login.text
    status = client.post("/api/accounts/claude_cli/status", json={"csrf": token})
    assert status.json()["ready"]
    assert "private" not in status.text
    assert client.post("/api/accounts/claude_cli/login", json={"csrf": token}).json()["ok"]
    assert client.post("/api/accounts/claude_cli/logout", json={"csrf": token}).json()["ok"]
    assert calls == ["start", "status", "login", "logout"]
    assert client.post("/api/accounts/custom/login", json={"csrf": token}).status_code == 404


def test_unselected_default_antigravity_does_not_register_an_account(subscription_client, config, monkeypatch):
    from papermap import chat

    client, token = subscription_client
    fields = {"csrf": token, "p_antigravity_acp_model": "auto", "p_antigravity_acp_reasoning_effort": ""}
    probed = []
    monkeypatch.setattr(chat, "_subscription_ready", lambda cfg, provider: probed.append(provider) or False)

    def save(provider):
        assert client.post("/settings", data={**fields, "provider": provider}, follow_redirects=False).status_code == 303

    save("deepseek")
    assert "antigravity_acp" not in config.llm["presets"]
    chat.model_choices(config)
    assert "antigravity_acp" not in probed

    save("antigravity_acp")
    save("deepseek")
    assert config.llm["presets"]["antigravity_acp"]["model"] == "auto"
    chat.model_choices(config)
    assert "antigravity_acp" in probed


def test_antigravity_login_and_catalog_use_official_client(subscription_client, config, monkeypatch):
    from unittest.mock import Mock

    client, token = subscription_client
    official = Mock()
    official.start_login.return_value = {"ok": True, "message": "登录已打开", "ready": False,
                                         "private_token": "secret"}
    official.status.return_value = {"ready": True, "available": True, "auth_token": "secret"}
    official.model_options.return_value = [{"id": "gemini", "label": "Gemini",
                                           "reasoning_efforts": ["low", "high"]}]

    def account(provider, data_dir):
        assert provider == "antigravity_acp" and data_dir == config.data_dir
        return official

    monkeypatch.setattr("papermap.web.app._account_client", account)
    provider = "antigravity_acp"
    prefix = f"/api/accounts/{provider}"
    body = {"csrf": token, "provider": provider, "refresh": True}
    assert client.post(f"{prefix}/login", json={}).status_code == 403
    official.start_login.assert_not_called()
    login = client.post(f"{prefix}/login", json=body).json()
    assert login["ok"] and not login["ready"] and "private_token" not in login
    official.start_login.assert_called_once_with()
    status = client.post(f"{prefix}/status", json=body).json()
    assert status["ready"] and "auth_token" not in status
    official.status.assert_called_once_with(refresh=True)
    assert client.post(f"{prefix}/logout", json=body).status_code == 404
    catalog = client.post("/api/llm/models", json=body).json()
    assert catalog["models"] == ["gemini"]
    assert catalog["model_options"] == official.model_options.return_value
    official.model_options.assert_called_once_with(refresh=True)
    assert config.llm["provider"] == "deepseek"


def test_chatgpt_consent_retry_pending_and_cancel(subscription_client, monkeypatch):
    from papermap.chatgpt_auth import ChatGPTAuthError

    client, token = subscription_client
    calls = []

    class FakeAccount:
        pending = False

        def start(self, *, consent=False):
            calls.append(("start", consent))
            if self.pending:
                raise ChatGPTAuthError("login_pending", "登录正在进行，请完成或取消。")
            self.pending = True
            return {"status": "pending", "url": "https://auth.openai.com/authorize"}

        def status(self):
            return {"ready": False, "pending": self.pending, "needs_consent": True,
                    "error": "请重新授权套餐使用。", "error_code": "scope_missing"}

        def cancel(self):
            calls.append(("cancel", None))
            self.pending = False

    account = FakeAccount()
    monkeypatch.setattr("papermap.web.app._account_client", lambda *args: account)
    body = {"csrf": token}
    state = client.post("/api/accounts/chatgpt_plan/status", json=body).json()
    assert state["needs_consent"] and state["error_code"] == "scope_missing"
    assert "重新授权" in state["error"]
    assert client.post("/api/accounts/chatgpt_plan/login", json={**body, "consent": True}).json()["ok"]
    duplicate = client.post("/api/accounts/chatgpt_plan/login", json=body).json()
    assert not duplicate["ok"] and duplicate["pending"] and duplicate["error_code"] == "login_pending"
    cancelled = client.post("/api/accounts/chatgpt_plan/cancel", json=body).json()
    assert cancelled["ok"] and not cancelled["pending"]
    assert calls == [("start", True), ("start", False), ("cancel", None)]
    assert client.post("/api/accounts/claude_cli/cancel", json=body).status_code == 404


@pytest.mark.parametrize("provider", ["chatgpt_plan", "claude_cli"])
def test_models_and_connection_use_unsaved_subscription_model(subscription_client, config, monkeypatch, provider):
    client, token = subscription_client
    seen = []

    def fake_models(eff):
        seen.append(eff)
        return ["model-a", "model-b"]

    def fake_test(eff):
        seen.append(eff)
        return {"ok": True, "message": "connected", "latency_ms": 2}

    monkeypatch.setattr("papermap.web.app.list_models", fake_models)
    monkeypatch.setattr("papermap.web.app.list_chatgpt_model_options", lambda eff: [
        {"id": m, "label": m, "reasoning_efforts": [], "reasoning_efforts_known": False,
         "default_reasoning_effort": ""} for m in fake_models(eff)])
    monkeypatch.setattr("papermap.web.app.check_connection", fake_test)
    body = {"csrf": token, "provider": provider, "model": "unsaved-model"}
    assert client.post("/api/llm/models", json=body).json()["models"] == ["model-a", "model-b"]
    assert client.post("/api/llm/test", json=body).json()["ok"]
    assert all(eff["provider"] == provider and eff["model"] == "unsaved-model" for eff in seen)
    assert all(eff["data_dir"] == str(config.data_dir) for eff in seen)
    assert config.llm["provider"] == "deepseek"  # testing a choice does not save it


@pytest.mark.parametrize("provider", ["chatgpt_plan", "claude_cli"])
def test_subscription_ignores_submitted_and_legacy_keys(subscription_client, config, monkeypatch, provider):
    client, token = subscription_client
    config.llm.setdefault("presets", {})[provider] = {"api_key": "legacy-key", "model": "chosen"}
    response = client.post("/settings", data={
        "csrf": token, "provider": provider, f"p_{provider}_model": "chosen",
        f"p_{provider}_key": "autofilled-key",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert "api_key" not in config.llm["presets"][provider]
    assert "legacy-key" not in config.path.read_text(encoding="utf-8")
    assert "autofilled-key" not in config.path.read_text(encoding="utf-8")
    seen = []
    monkeypatch.setattr("papermap.web.app.list_models", lambda eff: seen.append(eff) or ["chosen"])
    monkeypatch.setattr("papermap.web.app.list_chatgpt_model_options", lambda eff: seen.append(eff) or [
        {"id": "chosen", "label": "chosen", "reasoning_efforts": [], "reasoning_efforts_known": False,
         "default_reasoning_effort": ""}])
    assert client.post("/api/llm/models", json={
        "csrf": token, "provider": provider, "model": "chosen", "api_key": "injected-key",
    }).json()["ok"]
    assert not seen[0].get("api_key")


def test_subscription_is_not_paused_by_api_budget(subscription_client, config, registry, monkeypatch):
    client, _ = subscription_client
    config.llm["provider"] = "chatgpt_plan"
    config.llm["monthly_budget_usd"] = 1
    monkeypatch.setattr(registry, "month_cost", lambda: 12.0)
    html = client.get("/").text
    assert "本月 LLM 预算已用完" not in html
    assert "https://chatgpt.com/settings/usage" in client.get("/costs").text


def test_account_error_does_not_expose_exception_secrets(subscription_client, monkeypatch):
    client, token = subscription_client

    def broken_client(*args):
        raise RuntimeError("sensitive-token-in-error")

    monkeypatch.setattr("papermap.web.app._account_client", broken_client)
    response = client.post("/api/accounts/chatgpt_plan/status", json={"csrf": token})
    assert not response.json()["ok"]
    assert "sensitive-token" not in response.text


def test_reasoning_choices_save_restore_and_clear_independently(subscription_client, config):
    client, token = subscription_client
    response = client.post("/settings", data={"csrf": token, "provider": "chatgpt_plan",
        "p_chatgpt_plan_model": "model-a", "p_chatgpt_plan_reasoning_effort": "ultra",
        "p_claude_cli_model": "opus", "p_claude_cli_reasoning_effort": "max"}, follow_redirects=False)
    assert response.status_code == 303
    saved = config.llm["presets"]
    assert saved["chatgpt_plan"]["reasoning_effort"] == "ultra"
    assert saved["claude_cli"]["reasoning_effort"] == "max"
    html = client.get("/settings").text
    for provider, value in (("chatgpt_plan", "ultra"), ("claude_cli", "max")):
        assert f'id="p_{provider}_reasoning_effort"' in html
        assert f'name="p_{provider}_reasoning_effort" data-role="reasoning-value" value="{value}"' in html
    assert 'data-claude-model-options' in html
    assert "默认（模型决定）" in html
    # Saving another provider without these fields preserves both choices.
    client.post("/settings", data={"csrf": token, "provider": "custom"})
    assert saved["chatgpt_plan"]["reasoning_effort"] == "ultra" and saved["claude_cli"]["reasoning_effort"] == "max"
    client.post("/settings", data={"csrf": token, "provider": "claude_cli", "p_claude_cli_reasoning_effort": "default"})
    assert config.llm["presets"]["claude_cli"]["reasoning_effort"] == ""
    assert config.llm["presets"]["chatgpt_plan"]["reasoning_effort"] == "ultra"


@pytest.mark.parametrize("provider", ["chatgpt_plan", "claude_cli", "antigravity_acp"])
def test_unsaved_reasoning_and_default_reach_connection_test(subscription_client, config, monkeypatch, provider):
    client, token = subscription_client
    config.llm.setdefault("presets", {})[provider] = {"model": "old", "reasoning_effort": "high"}
    seen = []
    monkeypatch.setattr("papermap.web.app.check_connection", lambda eff: seen.append(eff) or {"ok": True})
    for effort in ("ultra", ""):
        assert client.post("/api/llm/test", json={"csrf": token, "provider": provider,
            "model": "new", "reasoning_effort": effort}).json()["ok"]
        assert seen[-1]["model"] == "new" and seen[-1]["reasoning_effort"] == effort
    assert config.llm["presets"][provider] == {"model": "old", "reasoning_effort": "high"}


def test_chatgpt_models_return_capabilities_without_changing_selection(subscription_client, config, monkeypatch):
    client, token = subscription_client
    capabilities = [{"id": "model-a", "label": "Sol", "reasoning_efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
                     "default_reasoning_effort": "medium", "reasoning_efforts_known": True},
                    {"id": "model-b", "label": "Astra", "reasoning_efforts": [],
                     "default_reasoning_effort": "", "reasoning_efforts_known": False}]
    seen = []
    monkeypatch.setattr("papermap.web.app.list_chatgpt_model_options", lambda eff: seen.append(eff) or capabilities)
    data = client.post("/api/llm/models", json={"csrf": token, "provider": "chatgpt_plan",
        "model": "model-a", "reasoning_effort": "ultra"}).json()
    assert data["models"] == ["model-a", "model-b"] and data["model_options"] == capabilities
    assert seen[0]["reasoning_effort"] == "ultra"
    assert config.llm["provider"] == "deepseek"
    def offline(eff):
        raise OSError("offline")
    monkeypatch.setattr("papermap.web.app.list_chatgpt_model_options", offline)
    assert not client.post("/api/llm/models", json={"csrf": token, "provider": "chatgpt_plan"}).json()["ok"]


def test_claude_full_model_capabilities_keep_alias_model_list(subscription_client):
    client, token = subscription_client
    data = client.post("/api/llm/models", json={"csrf": token, "provider": "claude_cli", "model": "claude-opus-5-5"}).json()
    assert data["ok"] and "opus" in data["models"] and "claude-opus-5-5" not in data["models"]
    full = next(m for m in data["model_options"] if m["id"] == "claude-opus-5-5")
    assert full["reasoning_efforts_known"] and "max" in full["reasoning_efforts"]


@pytest.mark.parametrize("provider", ["chatgpt_plan", "claude_cli", "antigravity_acp"])
def test_invalid_reasoning_never_mutates_saved_settings(subscription_client, config, monkeypatch, provider):
    import copy

    client, token = subscription_client
    before = copy.deepcopy(config.raw)
    response = client.post("/settings", data={"csrf": token, "provider": provider,
        f"p_{provider}_reasoning_effort": "bad\nvalue", "output_language": "en"})
    assert response.status_code == 422 and config.raw == before
    assert client.get("/settings").status_code == 200
    monkeypatch.setattr("papermap.web.app.check_connection", lambda eff: pytest.fail("invalid value reached inference"))
    response = client.post("/api/llm/test", json={"csrf": token, "provider": provider, "reasoning_effort": ["high"]})
    assert response.status_code == 422 and config.raw == before


def test_settings_script_model_effort_changes_and_async_races():
    """Exercise real settings JS with a small DOM/HTTP fixture, without a browser account."""
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is only needed for this optional browser-script contract test")
    script = r'''
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
class Element {
  constructor(value = "", dataset = {}) { this.value = value; this.dataset = dataset; this.listeners = {}; this.children = []; this.textContent = ""; this.classList = {toggle(){}}; }
  addEventListener(name, cb) { (this.listeners[name] ||= []).push(cb); }
  async emit(name) { await Promise.all((this.listeners[name] || []).map(cb => cb({target:this}))); }
  appendChild(el) { this.children.push(el); return el; }
  replaceChildren(...els) { this.children = els; }
  get options() { return this.children; }
  removeAttribute(key) { delete this[key]; }
  focus() {}
  closest() { return this.pane; }
}
const ids = {}, panes = {}, account = {}, starts = [], cancels = [], statuses = [], logouts = [], fetchers = [];
for (const name of ["csrf","provider_picker","test_result","test_conn","fetch_models","custom_model","custom_base_url","custom_api_key","custom_protocol","official_api_key","official_model"]) ids[name] = new Element();
ids.csrf.value = "test-csrf";
ids.provider_picker.value = "custom";
const caps = [
  {id:"gpt-a", label:"Sol", reasoning_efforts:["low","medium","high","xhigh","max","ultra"], default_reasoning_effort:"medium", reasoning_efforts_known:true},
  {id:"gpt-b", label:"Other", reasoning_efforts:["low","high"], default_reasoning_effort:"high", reasoning_efforts_known:true},
];
const aliases = [
  {id:"opus",label:"Opus",reasoning_efforts:["low","medium","high","xhigh","max"], reasoning_efforts_known:true,default_reasoning_effort:""},
  {id:"haiku",label:"Haiku",reasoning_efforts:[],reasoning_efforts_known:true,default_reasoning_effort:""},
];
for (const p of ["chatgpt_plan","claude_cli","antigravity_acp","custom","deepseek"]) {
  const pane = panes[p] = new Element("", {pane:p});
  const parts = pane.parts = {model:new Element(p === "chatgpt_plan" ? "gpt-a" : p === "claude_cli" ? "opus" : p === "antigravity_acp" ? "auto" : "other"), datalist:new Element()};
  if (p === "chatgpt_plan" || p === "claude_cli" || p === "antigravity_acp") {
    parts["reasoning-effort"] = new Element(); parts["reasoning-value"] = new Element(p === "chatgpt_plan" ? "ultra" : "max"); parts.hint = new Element();
    parts.bootstrap = new Element(); parts.bootstrap.textContent = JSON.stringify(aliases);
    for (const [kind, all, data] of [["result",null,"accountResult"],["url",null,"accountUrl"],["start",starts,"accountStart"],["cancel",cancels,"accountCancel"],["status",statuses,"accountStatus"],["logout",logouts,"accountLogout"]]) {
      const el = new Element("", {[data]:p}); account[p+":"+kind] = el; if (all) all.push(el);
    }
  }
  parts.fetch = new Element("", {fetchModels:""}); parts.fetch.pane = pane; if (!["custom","deepseek"].includes(p)) fetchers.push(parts.fetch);
  pane.querySelector = sel => {
    const match = sel.match(/data-role="([^"]+)"/); if (match) return parts[match[1]] || null;
    if (sel === "datalist") return parts.datalist;
    if (sel === "[data-reasoning-hint]") return parts.hint;
    if (sel === "[data-claude-model-options]") return parts.bootstrap;
    if (sel === "[data-fetch-models]") return parts.fetch;
    return null;
  };
}
const form = ids.settings = new Element();
const modelSection = new Element(); modelSection.hidden = false;
form.querySelector = sel => {
  if (sel === '[data-settings-panel="models"]') return modelSection;
  let m = sel.match(/\.pane\[data-pane="([^"]+)"\]/); if (m) return panes[m[1]];
  m = sel.match(/\[data-account-(result|url|start|cancel|status|logout)="([^"]+)"\]/); if (m) return account[m[2]+":"+m[1]];
  return null;
};
form.querySelectorAll = sel => ({".pane":Object.values(panes),".choice":[],"[data-account-start]":starts,"[data-account-cancel]":cancels,"[data-account-status]":statuses,"[data-account-logout]":logouts,"[data-fetch-models]":fetchers}[sel] || []);
const requests = []; let mode = "normal", release;
function reply(data) { return {ok:true,json:async()=>data}; }
async function fetch(url, opts) {
  const body = JSON.parse(opts.body); requests.push({url,body});
  if (url.endsWith("/status")) return reply({ok:true,ready:true});
  if (url.endsWith("/login")) return reply({ok:true,pending:true});
  if (url.endsWith("/logout")) return reply({ok:true,ready:false});
  if (url === "/api/llm/test") return reply({ok:true,message:"connected",latency_ms:1});
  if (url === "/api/llm/models") {
    if (body.provider === "antigravity_acp") return reply({ok:true,models:["gemini","gemini-pro"],model_options:[
      {id:"gemini",label:"Gemini",reasoning_efforts:["low","high"],reasoning_efforts_known:true},
      {id:"gemini-pro",label:"Gemini Pro",is_current:true,reasoning_efforts:["medium","high"],reasoning_efforts_known:true}
    ]});
    if (body.provider === "claude_cli") return reply({ok:true,models:["opus","haiku"],model_options:[...aliases,{id:body.model,label:body.model,reasoning_efforts:["low","high","max"],reasoning_efforts_known:true}]});
    if (mode === "hold") return new Promise(resolve=>{release=data=>resolve(reply(data));});
    if (mode === "offline") throw new Error("offline");
    return reply({ok:true,models:caps.map(x=>x.id),model_options:mode === "missing" ? [] : caps});
  }
  throw new Error("Unexpected request " + url);
}
const documentEvents = new Element(), windowEvents = new Element();
const context = {t:s=>s,document:{getElementById:id=>ids[id]||null,createElement:()=>new Element(),querySelectorAll:()=>[],addEventListener:documentEvents.addEventListener.bind(documentEvents)},
  fetch,window:{pmDialog:async()=>true,pmToast(){},addEventListener:windowEvents.addEventListener.bind(windowEvents)},setTimeout:()=>1,clearTimeout(){},console};
vm.runInNewContext(fs.readFileSync(process.argv[1],"utf8"),context);
const flush = async()=>{for(let i=0;i<12;i++) await new Promise(resolve=>setImmediate(resolve));};
async function choose(p) { ids.provider_picker.value=p; await ids.provider_picker.emit("change"); await flush(); }
const values = select=>select.children.map(x=>x.value);
(async()=>{
  const gp=panes.chatgpt_plan.parts, cp=panes.claude_cli.parts;
  assert.equal(requests.length,0,"custom startup must not inspect subscription accounts");
  modelSection.hidden=true;
  await choose("chatgpt_plan");
  assert.equal(requests.length,0,"non-model settings must not inspect subscription accounts or fetch models");
  modelSection.hidden=false; await documentEvents.emit("papermap:settings-section"); await flush();
  assert.deepEqual(values(gp["reasoning-effort"]),["","low","medium","high","xhigh","max","ultra"]);
  assert.equal(gp["reasoning-effort"].value,"ultra","saved selection restored after automatic model discovery");
  gp.model.value="gpt-b"; await gp.model.emit("input");
  assert.deepEqual(values(gp["reasoning-effort"]),["","low","high"]);
  gp["reasoning-effort"].value="high"; await gp["reasoning-effort"].emit("change");
  await ids.test_conn.emit("click");
  assert.equal(requests.at(-1).body.model,"gpt-b"); assert.equal(requests.at(-1).body.reasoning_effort,"high");
  gp.model.value="gpt-a"; await gp.model.emit("input"); assert.equal(gp["reasoning-value"].value,"ultra");
  mode="missing"; await gp.fetch.emit("click");
  assert.equal(gp["reasoning-value"].value,"ultra"); assert.equal(gp["reasoning-effort"].children.at(-1).disabled,true);
  gp["reasoning-effort"].value=""; await gp["reasoning-effort"].emit("change"); assert.equal(gp["reasoning-value"].value,"");
  mode="normal"; await gp.fetch.emit("click");
  gp["reasoning-effort"].value="ultra"; await gp["reasoning-effort"].emit("change");
  mode="offline"; await gp.fetch.emit("click"); assert.equal(gp["reasoning-value"].value,"ultra");
  mode="hold"; const old=gp.fetch.emit("click"); await flush();
  await choose("claude_cli");
  assert.equal(cp["reasoning-value"].value,"max");
  cp.model.value="haiku"; await cp.model.emit("input"); assert.deepEqual(values(cp["reasoning-effort"]),[""]);
  cp.model.value="opus"; await cp.model.emit("input"); assert.equal(cp["reasoning-value"].value,"max");
  cp["reasoning-effort"].value="high"; await cp["reasoning-effort"].emit("change"); await ids.test_conn.emit("click");
  assert.equal(requests.at(-1).body.provider,"claude_cli"); assert.equal(requests.at(-1).body.reasoning_effort,"high");
  release({ok:true,models:["late-model"],model_options:[]}); await old;
  assert(!gp.datalist.children.some(x=>x.value==="late-model"),"other provider's late response must be ignored");
  mode="normal"; await choose("chatgpt_plan");
  mode="hold"; const beforeLogin=gp.fetch.emit("click"); await flush();
  await account["chatgpt_plan:start"].emit("click");
  release({ok:true,models:["pre-authorization-model"],model_options:[]}); await beforeLogin;
  assert(!gp.datalist.children.some(x=>x.value==="pre-authorization-model"),"pre-login response must not replace authorized capabilities");
  await choose("claude_cli"); await windowEvents.emit("focus"); await flush();
  assert.equal(requests.at(-1).url,"/api/accounts/claude_cli/status");
  assert.equal(requests.at(-1).body.refresh,true,"return from official login must check the current CLI session");
  await choose("antigravity_acp");
  const ap = panes.antigravity_acp.parts;
  assert.deepEqual(values(ap.model), ["auto","gemini","gemini-pro"]);
  assert.equal(ap.model.value, "auto");
  assert.deepEqual(values(ap["reasoning-effort"]), ["","medium","high"]);
  ap.model.value="gemini"; await ap.model.emit("change");
  assert.deepEqual(values(ap["reasoning-effort"]), ["","low","high"]);
  ap["reasoning-effort"].value="high"; await ap["reasoning-effort"].emit("change");
  await ids.test_conn.emit("click");
  assert.equal(requests.at(-1).body.provider,"antigravity_acp"); assert.equal(requests.at(-1).body.model,"gemini"); assert.equal(requests.at(-1).body.reasoning_effort,"high");
  const requestsBeforeHidden=requests.length;
  modelSection.hidden=true; await documentEvents.emit("papermap:settings-section"); await choose("claude_cli");
  await windowEvents.emit("focus"); await flush();
  assert.equal(requests.length,requestsBeforeHidden,"switching hidden provider state must not contact accounts");
  console.log("settings effort behavior and races passed");
})().catch(err=>{console.error(err);process.exitCode=1;});
'''
    source = Path(__file__).parents[1] / "src" / "papermap" / "web" / "static" / "settings.js"
    result = subprocess.run([node, "-e", script, str(source)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
