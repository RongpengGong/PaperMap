import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from papermap import search
from papermap.extension_events import event_page, latest_event_id
from papermap.registry import Registry
from papermap.web.plugin_api import mount_api, revoke_token, rotate_token, token_enabled


@pytest.fixture
def api(config, registry):
    app = FastAPI()
    imported = []
    pipe = SimpleNamespace(handle_path=imported.append)
    mount_api(app, config, registry, pipe, lambda name, fn, *args: fn(*args))
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 7890)) as client:
        yield client, imported


def auth(registry):
    return {"Authorization": "Bearer " + rotate_token(registry)}


def test_loopback_api_token_lifecycle(api, config, registry):
    client, _ = api
    assert not token_enabled(registry)
    assert client.get("/api/v1/papers").status_code == 401
    first = auth(registry)
    second = auth(registry)
    assert token_enabled(registry)
    assert client.get("/api/v1/papers", headers=first).status_code == 401
    assert client.get("/api/v1/papers", headers=second).status_code == 200
    assert second["Authorization"].split()[1] not in str(dict(registry.one("SELECT * FROM extension_api_credentials")))
    reopened = Registry(config.db_path)
    try:
        assert token_enabled(reopened)
        revoke_token(reopened)
    finally:
        reopened.close()
    assert client.get("/api/v1/papers", headers=second).status_code == 401
    assert not token_enabled(registry)


def test_origin_and_peer_checks(api, registry):
    client, _ = api
    headers = auth(registry)
    assert client.get("/api/v1/papers", headers=dict(headers, Origin="http://localhost:5911")).status_code == 403
    assert client.get("/api/v1/papers", headers=dict(headers, Origin="http://localhost")).status_code == 200
    assert client.get("/api/v1/papers", headers=dict(headers, Host="unrelated.example")).status_code == 403
    with TestClient(client.app, base_url="http://localhost", client=("192.0.2.5", 3456)) as remote:
        assert remote.get("/api/v1/papers", headers=headers).status_code == 403


def test_papers_cards_and_search(api, registry):
    client, _ = api
    h = auth(registry)
    paper = registry.create_paper(title="Xenium", status="done", card_json={"methods": ["Xenium"]})
    later = registry.create_paper(title="Another title")
    registry.create_paper(title="ignored", status="ignored")
    page = client.get("/api/v1/papers?limit=1", headers=h).json()
    assert page["items"][0]["id"] == page["next_cursor"] == paper
    assert "meta_json" not in page["items"][0]
    assert len(client.get(f"/api/v1/papers?after={paper}", headers=h).json()["items"]) == 1
    assert client.get(f"/api/v1/papers/{paper}", headers=h).json()["title"] == "Xenium"
    assert client.get(f"/api/v1/papers/{paper}/card", headers=h).json()["card"] == {"methods": ["Xenium"]}
    assert client.get(f"/api/v1/papers/{later}/card", headers=h).json()["detail"] == "card_not_ready"
    assert client.get("/api/v1/papers/999999999999999999999", headers=h).status_code == 404
    search.ensure_schema(registry)
    registry.execute("INSERT INTO search_index(paper_id,kind,page,label,body) VALUES (?,?,?,?,?)",
                     (paper, "page", 0, "p.1", "We used Xenium for single cell analysis."))
    hits = client.get("/api/v1/search?q=Xenium", headers=h).json()["items"]
    assert len(hits) == 1 and hits[0]["paper_id"] == paper and hits[0]["page"] == 1
    assert "Xenium" in hits[0]["snippet"] and "body" not in hits[0]


def test_pdf_import_stream_limits_and_file_collision(api, config, registry, monkeypatch, pdfs):
    client, imported = api
    h = dict(auth(registry), **{"Content-Type": "application/pdf"})
    raw = pdfs["paper"].read_bytes()
    url = "/api/v1/import?filename=one.pdf"
    assert client.post(url, content=raw, headers={"Content-Type": "application/pdf"}).status_code == 401
    assert client.post(url, content=raw, headers=dict(h, **{"Content-Type": "text/plain"})).status_code == 415
    assert client.post(url, content=b"not a pdf", headers=h).status_code == 422
    assert not list(config.imported_dir.iterdir())
    saved = client.post(url, content=raw, headers=h)
    assert saved.status_code == 202 and saved.json()["accepted"]
    assert client.post(url, content=raw, headers=h).status_code == 202
    assert [p.name for p in imported] == ["one.pdf", "one (2).pdf"]
    assert all(Path(p).read_bytes() == raw for p in imported)
    monkeypatch.setattr("papermap.web.plugin_api.MAX_UPLOAD", 20)
    assert client.post(url, content=iter([raw[:12], raw[12:]]), headers=h).status_code == 413
    assert len(list(config.imported_dir.iterdir())) == 2


def test_durable_events_and_sse_resume(api, config, registry):
    client, _ = api
    h = auth(registry)
    paper = registry.create_paper(title="A")
    registry.update_paper(paper, status="done", card_json={"key_findings": ["A"]})
    registry.update_paper(paper, card_json={"key_findings": ["new wording"]})
    field = registry.create_pending("new-field", "method", "新学科", "New field", "Example", [])
    registry.update_pending(field, ready_notified=1)
    registry.update_pending(field, ready_notified=1)
    items = client.get("/api/v1/events", headers=h).json()["items"]
    first, second = items
    assert [e["type"] for e in items] == ["paper.ready", "field.proposed"]
    assert first["data"] == {"paper_id": paper}
    assert second["data"] == {"field_id": field, "slug": "new-field"}
    assert client.get(f"/api/v1/events?after={first['id']}", headers=h).json() == {
        "items": [second], "next_cursor": second["id"]}
    reopened = Registry(config.db_path)
    try:
        assert latest_event_id(reopened) == second["id"]
        assert event_page(reopened, first["id"], 10) == [second]
    finally:
        reopened.close()
    sse = client.get("/api/v1/events/stream?timeout=1", headers=dict(h, **{"Last-Event-ID": str(first["id"])}))
    assert sse.headers["content-type"].startswith("text/event-stream")
    lines = sse.text.splitlines()
    assert "event: field.proposed" in lines and "event: paper.ready" not in lines
    assert json.loads(next(line[6:] for line in lines if line.startswith("data: "))) == second
    assert client.get("/api/v1/events/stream", headers=dict(h, **{"Last-Event-ID": "-1"})).status_code == 422
