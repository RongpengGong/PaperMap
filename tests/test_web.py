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
    assert r.status_code == 200 and "尚未设置模型 API Key" in r.text
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


def _classified_client(config, registry, pdfs):
    from fastapi.testclient import TestClient

    from papermap.web.app import create_app

    from .test_pipeline import make_pipeline
    pipe, _ = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    return TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765"), pipe


def test_stage2_pages(config, registry, pdfs):
    c, pipe = _classified_client(config, registry, pdfs)
    for url in ("/map", "/fields", "/review", "/taxonomy", "/field/spatial-transcriptomics", "/paper/1"):
        r = c.get(url)
        assert r.status_code == 200, url
    hub = c.get("/field/spatial-transcriptomics").text
    assert 'href="/paper/1"' in hub  # wikilinks converted
    assert 'href="/field/spatial-transcriptomics"' in c.get("/paper/1").text
    review = c.get("/review").text
    assert "湿实验机制研究" in review and "Glioma" in review
    g = c.get("/api/graph").json()
    kinds = {n["kind"] for n in g["nodes"]}
    assert {"method", "topic", "paper"} <= kinds
    assert any(e["primary"] for e in g["paper_edges"]) and g["field_edges"]


def test_review_and_taxonomy_actions(config, registry, pdfs):
    c, pipe = _classified_client(config, registry, pdfs)
    tok = _token(c)
    r = c.post("/review/label", data={"csrf": tok, "paper_id": 1, "slug": "wet-lab-mechanism", "action": "accept"},
               follow_redirects=False)
    assert r.status_code == 303 and registry.get_paper(1)["needs_review"] == 0
    bad = c.post("/taxonomy/save", data={"csrf": tok, "yaml_text": "method: []\ntopic: []"}, follow_redirects=True)
    assert "分类表有错误" in bad.text and config.raw["taxonomy_file"] == "liver_omics"
    from papermap.taxonomy import builtin_text
    text = builtin_text("liver_omics").replace("Hepatocellular carcinoma (HCC), primary", "HCC, primary")
    ok = c.post("/taxonomy/save", data={"csrf": tok, "yaml_text": text.replace("\n", "\r\n")}, follow_redirects=True)
    assert "\r" not in (config.data_dir / "taxonomy.yaml").read_text()
    assert "已保存" in ok.text and config.raw["taxonomy_file"] == "taxonomy.yaml"
    assert "用旧版分类表" in ok.text


def test_import_upload_panel_pdf_notes(config, registry, pdfs):
    import time

    from fastapi.testclient import TestClient

    from papermap.web.app import create_app

    from .test_pipeline import make_pipeline
    pipe, _ = make_pipeline(config, registry)
    config.ensure_dirs()
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    tok = _token(c)
    data = pdfs["paper"].read_bytes()
    r = c.post("/api/import", data={"csrf": tok},
               files=[("files", ("my paper.pdf", data, "application/pdf")), ("files", ("notes.txt", b"x", "text/plain"))])
    assert r.json() == {"ok": True, "count": 1, "skipped": 1}
    for _ in range(100):
        rows = registry.list_papers()
        if rows and rows[0]["status"] == "done" and rows[0]["kb_path"]:
            break
        time.sleep(0.05)
    row = registry.list_papers()[0]
    assert row["status"] == "done"
    assert not list(config.imported_dir.glob("*.pdf"))  # moved into the paper folder
    home = c.get("/").text
    assert "paper-row selected" in home and "我的笔记" in home
    assert c.get(f"/paper/{row['id']}/pdf").content == data
    panel = c.get(f"/paper/{row['id']}/panel").text
    assert "Finding two" in panel and 'class="pdf-col"' in panel and 'split-handle' in panel
    assert c.post(f"/paper/{row['id']}/notes", json={"csrf": tok, "text": "hello\r\nworld"}).json()["ok"]
    from papermap import library
    assert library.notes_path(config.knowledge_dir, row["kb_path"]).read_text() == "hello\nworld"
    assert c.post(f"/paper/{row['id']}/notes", json={"csrf": "bad", "text": "x"}).status_code == 403
    assert c.post("/api/import", data={"csrf": "bad"}).status_code == 403
    lst = c.get("/papers/list?q=Spatial").text
    assert "paper-row" in lst and "paper-row" not in c.get("/papers/list?q=zzzz-nothing").text
    assert c.get(f"/paper/{row['id']}", follow_redirects=False).status_code == 307
