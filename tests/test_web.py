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


def test_settings_saves_key_locally(config, registry):
    c = client(config, registry)
    page = c.get("/settings").text
    import re
    token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = c.post("/settings", data={"csrf": token, "api_key": "sk-test", "base_url": "https://api.deepseek.com",
                                  "model": "deepseek-flash", "output_language": "en", "monthly_budget_usd": "3",
                                  "inbox_dirs": "/tmp/in", "exclude_dirs": "_private", "exclude_patterns": "*draft*"},
               follow_redirects=False)
    assert r.status_code == 303
    assert config.api_key() == "sk-test" and config.output_language == "en"
    assert "sk-test" in config.path.read_text()
    assert "sk-test" not in c.get("/settings").text  # never echoed back
    assert c.post("/settings", data={"csrf": "wrong", "base_url": "x", "model": "y"}).status_code == 403
