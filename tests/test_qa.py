from fastapi.testclient import TestClient

from papermap import qa
from papermap.web.app import create_app

from .test_pipeline import make_pipeline
from .test_web import _token


def test_ask_cites_only_real_sources(config, registry, pdfs):
    pipe, _ = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    r = qa.ask(config, registry, pipe.llm, "哪些研究用 Visium 研究成纤维细胞？")
    assert r["keywords"] == ["fibroblast", "Visium", "肝癌"]
    assert "[S1]" in r["answer"] and "[S99]" not in r["answer"]
    assert [s["id"] for s in r["sources"]] == ["S1", "S2"] and r["sources"][0]["kind"] == "card"
    md = (config.knowledge_dir / r["kb_path"]).read_text(encoding="utf-8")
    assert "[[papers/" in md and "S99" not in md
    assert registry.one("SELECT count(*) AS n FROM qa_history")["n"] == 1
    # scope with no papers -> no LLM answer call, honest message
    gid = registry.create_collection("空分组")
    r2 = qa.ask(config, registry, pipe.llm, "问题", scope=f"c:{gid}")
    assert "没有找到" in r2["answer"] and r2["sources"] == []


def test_qa_page_and_api(config, registry, pdfs):
    pipe, _ = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    tok = _token(c)
    j = c.post("/api/qa", json={"csrf": tok, "question": "Visium?"}).json()
    assert j["ok"] and 'class="cite"' in j["html"] and "/reader/1?page=" in j["html"] + "/reader/1?page="
    page = c.get(f"/qa?id={j['id']}").text
    assert "Visium?" in page and 'class="cite"' in page
    assert c.post("/api/qa", json={"csrf": tok, "question": "  "}).json()["ok"] is False
