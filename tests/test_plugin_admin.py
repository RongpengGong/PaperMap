"""The UI's management privileges are separate from the public API bearer token."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from papermap.pipeline import Pipeline
from papermap.web.app import _clean_markdown_html, create_app

from .conftest import FakeMetadata


@pytest.fixture
def ui(config, registry, monkeypatch):
    monkeypatch.setattr(type(config), "model_ready", lambda self: False)
    pipe = Pipeline(config, registry, metadata=FakeMetadata())
    app = create_app(config, registry, pipe)
    client = TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 55000))
    page = client.get("/settings?section=extensions")
    assert page.status_code == 200 and 'data-settings-panel="extensions"' in page.text
    csrf = re.search(r'id="csrf" value="([^"]+)"', page.text)[1]
    return client, csrf


def test_management_requires_local_csrf_and_does_not_disclose_a_token(ui):
    client, csrf = ui
    rotate = "/api/extensions/token/rotate"
    assert client.post(rotate, json={}).status_code == 403
    assert client.post(rotate, json={"csrf": csrf}, headers={"Origin": "http://localhost:9999"}).status_code == 403
    response = client.post(rotate, json={"csrf": csrf})
    token = response.json()["token"]
    assert response.headers["cache-control"] == "no-store"
    listing = client.post("/api/extensions/list", json={"csrf": csrf})
    assert token not in listing.text and 'data-api-enabled="yes"' in listing.text
    auth = {"Authorization": "Bearer " + token}
    assert client.get("/api/v1/papers", headers=auth).status_code == 200
    assert client.post(rotate, json={}, headers=auth).status_code == 403
    assert client.post("/api/extensions/token/revoke", json={"csrf": csrf}).json()["ok"]
    assert client.get("/api/v1/papers", headers=auth).status_code == 401


def test_shipped_examples_use_normal_approval_and_action_paths(ui):
    client, csrf = ui
    for name in ("reading-tools", "event-inspector"):
        result = client.post("/api/extensions/install", json={"csrf": csrf, "example": name})
        assert result.status_code == 200, result.text
        assert result.json()["id"] == name
    listing = client.post("/api/extensions/list", json={"csrf": csrf}).text
    attrs = re.search(r'data-plugin="event-inspector"[^>]*data-digest="([0-9a-f]+)"', listing)
    assert attrs
    code_path = "/api/extensions/event-inspector"
    assert client.post(code_path + "/run", json={"csrf": csrf}).status_code == 400
    assert client.post(code_path + "/enabled", json={"enabled": True, "csrf": csrf}).status_code == 400
    assert client.post(code_path + "/enabled", json={"enabled": True, "csrf": csrf, "digest": attrs[1]}).json()["ok"]
    assert client.post(code_path + "/run", json={"csrf": csrf}).json()["ok"]
    assert client.post(code_path + "/enabled", json={"enabled": False, "csrf": csrf}).json()["ok"]
    assert client.post(code_path + "/run", json={"csrf": csrf}).status_code == 400
    assert client.post("/api/extensions/reading-tools/enabled", json={"enabled": True, "csrf": csrf}).json()["ok"]
    catalog = re.search(r'<script type="application/json" id="plugin_exports">(.*?)</script>', client.get("/").text)[1]
    assert any("读书会" in item["name"] for item in json.loads(catalog))
    guide = client.get("/extensions/guide")
    assert guide.status_code == 200 and "Last-Event-ID" in guide.text


def test_untrusted_document_markup_preserves_text_and_citations_only():
    cleaned = _clean_markdown_html('<p>Paper <a class="cite" href="/paper/1">1</a></p><form>extra</form>')
    assert 'class="cite"' in cleaned and "extra" in cleaned and "<form" not in cleaned
