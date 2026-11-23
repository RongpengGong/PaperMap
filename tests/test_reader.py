import json
import time

import httpx
from fastapi.testclient import TestClient

from papermap.web.app import create_app

from .conftest import OWN_DOI
from .test_pipeline import make_pipeline
from .test_web import _token


def client(config, registry, pdfs):
    pipe, comp = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    return TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765"), pipe, comp


def test_reader_page_info_render_words_search(config, registry, pdfs):
    c, pipe, _ = client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    assert c.get(f"/reader/{pid}").status_code == 200
    embed = c.get(f"/reader/{pid}?embed=1").text
    assert 'class="sidebar"' not in embed and 'id="pages"' in embed
    info = c.get(f"/api/reader/{pid}/info").json()
    assert len(info["pages"]) == 2 and info["pages"][0]["w"] > 500 and info["annotations"] == []
    png = c.get(f"/api/reader/{pid}/page/0.png?scale=1.7")
    assert png.status_code == 200 and png.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert list((config.data_dir / "cache" / "render").rglob("0-1.5.png"))  # snapped to an allowed scale, cached
    assert c.get(f"/api/reader/{pid}/page/9.png").status_code == 404
    words = c.get(f"/api/reader/{pid}/page/0/words").json()
    assert words[0][4] == "Spatial" and len(words[0]) == 6
    hits = c.get(f"/api/reader/{pid}/search?q=fibroblast").json()
    assert hits and hits[0]["page"] == 0 and "fibroblast" in hits[0]["context"].lower()
    assert c.get(f"/api/reader/{pid}/search?q=x").json() == []
    panel = c.get(f"/paper/{pid}/panel").text
    assert f"/reader/{pid}?embed=1" in panel and "全屏阅读" in panel


def test_annotations_crud_and_markdown(config, registry, pdfs):
    c, pipe, _ = client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    tok = _token(c)
    j = c.post(f"/api/reader/{pid}/annotations", json={"csrf": tok, "page": 0, "rects": [[50, 50, 200, 62], "bad"],
                                                      "text": "Spatial transcriptomics", "color": "green", "note": "关键"}).json()
    assert j["ok"]
    aid = j["id"]
    info = c.get(f"/api/reader/{pid}/info").json()
    assert info["annotations"][0]["rects"] == [[50.0, 50.0, 200.0, 62.0]] and info["annotations"][0]["color"] == "green"
    card = config.knowledge_dir / registry.get_paper(pid)["kb_path"]
    md = card.with_name(card.stem + ".annotations.md")
    for _ in range(50):
        if md.exists():
            break
        time.sleep(0.05)
    assert "p.1" in md.read_text() and "关键" in md.read_text() and "Spatial transcriptomics" in md.read_text()
    assert c.post(f"/api/annotations/{aid}/update", json={"csrf": tok, "color": "pink", "note": "改"}).json()["ok"]
    assert registry.annotations(pid)[0]["color"] == "pink"
    assert not c.post(f"/api/reader/{pid}/annotations", json={"csrf": tok, "page": 0, "rects": []}).json()["ok"]
    assert c.post(f"/api/annotations/{aid}/delete", json={"csrf": tok}).json()["ok"]
    for _ in range(50):
        if not md.exists():
            break
        time.sleep(0.05)
    assert not md.exists() and registry.annotations(pid) == []
    assert c.post(f"/api/annotations/{aid}/delete", json={"csrf": tok}).status_code == 404
    assert c.post(f"/api/reader/{pid}/annotations", json={"csrf": "bad"}).status_code == 403


def test_translate_selection(config, registry, pdfs, monkeypatch):
    c, pipe, comp = client(config, registry, pdfs)
    tok = _token(c)
    assert c.post("/api/translate-text", json={"csrf": tok, "text": "Hello"}).json() == {"ok": False, "need_ack": True}

    def fake_bilibili(self, prompt, paper_id):
        assert "Fibroblasts sit at the margin." in prompt and "专名/术语对照" not in prompt
        return "成纤维细胞位于边缘。"

    monkeypatch.setattr("papermap.translate.Translator._bilibili", fake_bilibili)
    j = c.post("/api/translate-text", json={"csrf": tok, "text": "Fibroblasts  sit\nat the margin.", "ack": True}).json()
    assert j == {"ok": True, "translation": "成纤维细胞位于边缘。", "model": "Index-Translate-35B-A3B"}
    assert config.raw["translation"]["privacy_ack"] is True
    assert c.post("/api/translate-text", json={"csrf": tok, "text": "   "}).json()["ok"] is False
