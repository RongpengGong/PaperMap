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
    for provider in ("chatgpt_plan", "claude_cli"):
        assert f'name="provider" value="{provider}"' in html
        assert f'data-account-start="{provider}"' in html
        assert f'data-account-status="{provider}"' in html
        assert f'name="p_{provider}_key"' not in html
        assert f'name="p_{provider}_model"' in html
    assert "https://chatgpt.com/settings/usage" in html
    assert "注销本机 Claude Code" in html
    assert "API Key 登录不能作为订阅调用" in html


@pytest.mark.parametrize("provider", ["chatgpt_plan", "claude_cli"])
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
    assert "legacy-key" not in config.path.read_text()
    assert "autofilled-key" not in config.path.read_text()
    seen = []
    monkeypatch.setattr("papermap.web.app.list_models", lambda eff: seen.append(eff) or ["chosen"])
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
