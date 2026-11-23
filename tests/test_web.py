from fastapi.testclient import TestClient

from papermap.pipeline import Pipeline
from papermap.web.app import create_app

from .conftest import FakeMetadata


def client(config, registry):
    app = create_app(config, registry, Pipeline(config, registry, metadata=FakeMetadata()))
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_pages_render(config, registry):
    c = client(config, registry)
    r = c.get("/")
    assert r.status_code == 200 and "尚未设置 LLM API Key" in r.text
    assert c.get("/settings").status_code == 200
    assert c.get("/events").status_code == 200


def test_foreign_host_and_origin_rejected(config, registry):
    c = client(config, registry)
    assert c.get("/", headers={"host": "evil.example"}).status_code == 403
    r = c.post("/settings", headers={"origin": "https://evil.example"}, data={"csrf": "x"})
    assert r.status_code == 403


def _token(c):
    import re
    return re.search(r'id="csrf" value="([^"]+)"', c.get("/settings").text).group(1)


BASE_FORM = {"output_language": "en", "monthly_budget_usd": "3", "inbox_dirs": "/tmp/in",
             "exclude_dirs": "_private", "exclude_patterns": "*draft*"}


def test_official_provider_saves_key_locally(config, registry):
    c = client(config, registry)
    r = c.post("/settings", data={"csrf": _token(c), "provider": "deepseek", "official_api_key": "sk-test",
                                  "official_model": "deepseek-v4-pro", **BASE_FORM}, follow_redirects=False)
    assert r.status_code == 303
    eff = config.effective_llm()
    assert eff["api_key"] == "sk-test" and eff["model"] == "deepseek-v4-pro"
    assert eff["base_url"] == "https://api.deepseek.com" and eff["protocol"] == "openai"
    assert config.output_language == "en" and "sk-test" in config.path.read_text()
    assert "sk-test" not in c.get("/settings").text  # never echoed back
    # blank key on a later save keeps the stored key
    c.post("/settings", data={"csrf": _token(c), "provider": "deepseek", "official_model": "deepseek-flash", **BASE_FORM})
    assert config.api_key() == "sk-test"
    assert c.post("/settings", data={"csrf": "wrong"}).status_code == 403


def test_custom_provider(config, registry):
    c = client(config, registry)
    c.post("/settings", data={"csrf": _token(c), "provider": "custom", "custom_base_url": "https://x.example/v1",
                              "custom_api_key": "k2", "custom_protocol": "anthropic", "custom_model": "m1",
                              "custom_price_in": "1", "custom_price_out": "2", **BASE_FORM})
    eff = config.effective_llm()
    assert (eff["provider"], eff["base_url"], eff["protocol"], eff["model"], eff["api_key"]) == (
        "custom", "https://x.example/v1", "anthropic", "m1", "k2")
    assert eff["price_output_per_1m"] == 2.0


def test_api_endpoints_use_form_values(config, registry, monkeypatch):
    seen = {}

    def fake_models(eff, timeout_s=20):
        seen["models"] = eff
        return ["a", "b"]

    def fake_check(eff, timeout_s=60):
        seen["test"] = eff
        return {"ok": True, "message": "ok", "latency_ms": 1, "tokens": 3}

    monkeypatch.setattr("papermap.web.app.list_models", fake_models)
    monkeypatch.setattr("papermap.web.app.check_connection", fake_check)
    c = client(config, registry)
    tok = _token(c)
    body = {"csrf": tok, "provider": "custom", "base_url": "https://y/v1", "api_key": "kk", "protocol": "openai", "model": "m"}
    assert c.post("/api/llm/models", json=body).json() == {"ok": True, "models": ["a", "b"]}
    assert seen["models"]["base_url"] == "https://y/v1" and seen["models"]["api_key"] == "kk"
    assert c.post("/api/llm/test", json={**body, "provider": "deepseek", "model": "deepseek-flash"}).json()["ok"]
    assert seen["test"]["base_url"] == "https://api.deepseek.com"
    assert c.post("/api/llm/test", json={**body, "csrf": "bad"}).status_code == 403
