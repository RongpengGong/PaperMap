from fastapi.testclient import TestClient

from papermap import library, notelinks
from papermap.web.app import create_app

from .conftest import OWN_DOI
from .test_pipeline import make_pipeline
from .test_web import _token


def test_suggest_backlinks_preview(config, registry, pdfs):
    pipe, _ = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    pipe.handle_path(pdfs["nodoi"])
    a = registry.paper_by_doi(OWN_DOI)
    b = [p for p in registry.list_papers() if p["id"] != a["id"]][0]
    sug = notelinks.suggest(registry, "Spatial")
    assert sug[0]["kind"] == "paper" and sug[0]["insert"].startswith("[[papers/") and sug[0]["insert"].endswith("|Zhang 2024]]")
    pipe.taxonomy()
    assert any(s["kind"] == "field" for s in notelinks.suggest(registry, "空间"))
    # paper b's notes link to paper a
    library.notes_path(config.knowledge_dir, b["kb_path"]).write_text(f"相关：{sug[0]['insert']} 值得对比")
    bl = notelinks.backlinks(registry, config.knowledge_dir, a["id"])
    assert bl and bl[0]["paper_id"] == b["id"] and "值得对比" in bl[0]["snippet"]
    assert notelinks.backlinks(registry, config.knowledge_dir, b["id"]) == []
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    assert "提到这篇论文的地方" in c.get(f"/paper/{a['id']}/panel").text
    html = c.post("/api/notes-preview", json={"csrf": _token(c), "text": sug[0]["insert"]}).json()["html"]
    assert f'href="/paper/{a["id"]}"' in html
    assert c.get("/api/link-suggest?q=Zhang").json()[0]["kind"] == "paper"
