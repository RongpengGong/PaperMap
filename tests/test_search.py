from fastapi.testclient import TestClient

from papermap import search as srch
from papermap.web.app import create_app

from .conftest import OWN_DOI
from .test_pipeline import make_pipeline
from .test_web import _token


def test_index_and_search(config, registry, pdfs):
    pipe, _ = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    assert registry.one("SELECT count(*) AS n FROM search_state")["n"] == 1
    hits = srch.search(registry, "Visium spatial")
    assert any(h["kind"] == "page" and h["page"] == 0 for h in hits)
    assert any(h["kind"] == "card" for h in srch.search(registry, "Finding two"))
    # notes + annotations reach the index after a change
    from papermap import library
    library.notes_path(config.knowledge_dir, registry.get_paper(pid)["kb_path"]).write_text("我的想法：肝癌 微环境 对照", encoding="utf-8")
    registry.add_annotation(pid, 0, "[[1,1,2,2]]", "fibroblast niches", "yellow", "重点看")
    assert pipe.reindex(pid) is True and pipe.reindex(pid) is False  # unchanged -> skipped
    assert srch.search(registry, "肝癌")[0]["kind"] == "notes"       # 2 chars -> LIKE fallback
    assert srch.search(registry, "我的想法")[0]["kind"] == "notes"   # trigram
    assert {h["kind"] for h in srch.search(registry, "重点看")} == {"annotation"}
    assert srch.search(registry, "nothing-like-this") == []
    assert srch.query_terms('"tumour margin" CAF') == ["tumour margin", "CAF"]
    g = srch.grouped(registry, srch.search(registry, "fibroblast"))
    assert g[0]["paper"]["id"] == pid
    srch.drop_paper(registry, pid)
    assert srch.search(registry, "fibroblast") == []


def test_search_page(config, registry, pdfs):
    pipe, _ = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    r = c.get("/search?q=Visium").text
    assert "<mark>Visium</mark>" in r and "/reader/1?page=1&q=Visium" in r
    assert "全文搜索" in c.get("/").text
    assert c.get("/search?q=fibroblast&kind=card").status_code == 200
    assert c.get("/reader/1?page=1&q=Visium").status_code == 200
