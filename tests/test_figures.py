from fastapi.testclient import TestClient

from papermap import figures as figmod
from papermap.web.app import create_app

from .conftest import BODY, make_pdf
from .test_pipeline import make_pipeline
from .test_web import _token


def figure_pdf(path):
    import pymupdf
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_textbox(pymupdf.Rect(50, 40, 550, 70), "Spatial transcriptomics reveals fibroblast niches in liver cancer", fontsize=16)
    p.insert_textbox(pymupdf.Rect(50, 75, 550, 90), "https://doi.org/10.1234/demo.2024.001", fontsize=9)
    p.draw_rect(pymupdf.Rect(60, 120, 540, 400), color=(0, 0, 1), fill=(0.8, 0.9, 1))  # the "figure"
    p.insert_textbox(pymupdf.Rect(50, 410, 550, 470), "Fig. 1 | Spatial niches of fibroblasts. a, Visium map. b, Quantification.", fontsize=8)
    p.insert_textbox(pymupdf.Rect(50, 480, 550, 800), "Abstract " + BODY, fontsize=9)
    doc.save(path)
    return path


def test_find_and_explain(config, registry, pdfs):
    pdf = figure_pdf(pdfs["paper"].parent / "fig.pdf")
    figs = figmod.find(pdf)
    assert [f["label"] for f in figs] == ["Fig. 1"] and figs[0]["rect"][3] <= 410 and "Visium map" in figs[0]["legend"]
    pipe, comp = make_pipeline(config, registry)
    pipe.handle_path(pdf)
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    lst = c.get("/api/figures/1").json()
    assert lst[0]["label"] == "Fig. 1"
    png = c.get(f"/api/figure/{lst[0]['id']}.png?scale=1")
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
    tok = _token(c)
    j = c.post(f"/api/figure/{lst[0]['id']}/explain", json={"csrf": tok}).json()
    assert j["ok"]
    sent = comp.last_messages[-1]["content"]
    assert isinstance(sent, list) and sent[1]["type"] == "image_url" and sent[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert c.post(f"/api/figure/{lst[0]['id']}/to-notes", json={"csrf": tok}).json()["ok"]
    from papermap import library
    assert "Fig. 1 讲解" in library.notes_path(config.knowledge_dir, registry.get_paper(1)["kb_path"]).read_text()
    assert "Fig. 1" in c.get("/figures").text


def test_vision_content_translation():
    from papermap.llm import _AnthropicClient, _GeminiClient, _ResponsesClient
    content = [{"type": "text", "text": "看图"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]
    turns = [{"role": "user", "content": content}]
    _, _, b = _ResponsesClient("https://x/v1", "k", 5).request("m", "", turns, 10, None, False, {})
    assert b["input"][0]["content"] == [{"type": "input_text", "text": "看图"}, {"type": "input_image", "image_url": "data:image/png;base64,QUJD"}]
    _, _, b = _AnthropicClient("https://x", "k", 5).request("m", "", turns, 10, None, False, {})
    assert b["messages"][0]["content"][1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}
    _, _, b = _GeminiClient("https://x", "k", 5).request("m", "", turns, 10, None, False, {})
    assert b["contents"][0]["parts"][1] == {"inline_data": {"mime_type": "image/png", "data": "QUJD"}}
