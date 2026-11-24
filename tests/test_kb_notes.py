"""Editing the user's block on tool / field pages from the app."""
from fastapi.testclient import TestClient

from papermap.kb_writer import USER_BEGIN, USER_END, read_user_section
from papermap.web.app import create_app

from .test_pipeline import make_pipeline
from .test_web import _token


def test_edit_notes_block_only(config, registry):
    pipe, _ = make_pipeline(config, registry)
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    page = config.knowledge_dir / "concepts" / "cellphonedb.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"# CellPhoneDB\n\n## 我的笔记\n\n{USER_BEGIN}\n（写下用法、参数心得、踩过的坑…）\n{USER_END}\n\n## 用到它的论文\n\n- x\n", "utf-8")
    assert 'data-kb-notes="concepts/cellphonedb.md"' in c.get("/concept/cellphonedb").text
    got = c.get("/api/kb/notes", params={"rel": "concepts/cellphonedb.md"}).json()
    assert got["text"] == "" and got["hint"].startswith("（写下用法")  # the placeholder is not shown as content
    tok = _token(c)
    r = c.post("/api/kb/notes", json={"csrf": tok, "rel": "concepts/cellphonedb.md",
                                      "text": f"阈值用 **0.1**\n{USER_END}\n第二行"}).json()
    assert r["ok"] and "<strong>0.1</strong>" in r["html"]
    text = page.read_text("utf-8")
    assert read_user_section(page) == "阈值用 **0.1**\n\n第二行" and text.count(USER_END) == 1 and "## 用到它的论文\n\n- x" in text
    for bad in ("../config.yaml", "papers/x.md", "concepts/missing.md"):
        assert c.get("/api/kb/notes", params={"rel": bad}).status_code == 404
    assert c.post("/api/kb/notes", json={"csrf": "x", "rel": "concepts/cellphonedb.md", "text": ""}).status_code == 403


def test_note_images_upload_serve_and_render(config, registry):
    import fitz

    pipe, _ = make_pipeline(config, registry)
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    page = config.knowledge_dir / "fields" / "spatial.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"# Spatial\n\n## 我的笔记\n\n{USER_BEGIN}\n{USER_END}\n", "utf-8")
    png = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 4, 4), False).tobytes("png")
    tok = _token(c)
    up = lambda data, csrf=tok, name="Fig 2 UMAP.png": c.post(  # noqa: E731
        "/api/kb/attachments", content=data, headers={"X-CSRF": csrf, "X-Filename": name})
    assert up(png, csrf="x").status_code == 403
    assert not up(b"<svg onload=alert(1)>").json()["ok"]
    r = up(png).json()
    assert r["ok"] and r["src"].startswith("../attachments/Fig-2-UMAP-") and r["src"].endswith(".png")
    assert up(png).json()["src"] == r["src"]  # same picture is stored once
    name = r["src"].split("/")[-1]
    got = c.get(f"/attachments/{name}")
    assert got.status_code == 200 and got.headers["content-type"] == "image/png" and got.content == png
    for bad in ("../config.yaml", "x.svg", "missing.png"):
        assert c.get(f"/attachments/{bad}").status_code == 404
    saved = c.post("/api/kb/notes", json={"csrf": tok, "rel": "fields/spatial.md",
                                          "text": f"看这张图\n\n![UMAP]({r['src']})"}).json()
    assert f'<img alt="UMAP" src="/attachments/{name}"' in saved["html"]
    assert f"![UMAP](../attachments/{name})" in page.read_text("utf-8")  # relative path also works in Obsidian


def test_card_edit_kept_over_refresh_and_reset(config, registry, pdfs):
    from .conftest import OWN_DOI
    from .test_reader import client as reader_client

    c, pipe, _ = reader_client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    tok = _token(c)
    got = c.get(f"/paper/{pid}/card.md").json()
    assert got["ok"] and not got["edited"] and got["text"].startswith("# ") and "我的笔记" not in got["text"]
    text = got["text"] + '\n\n我的补充：==重点== <span style="color: #d92d20; position: fixed">红字</span>'
    r = c.post(f"/paper/{pid}/card", json={"csrf": tok, "text": text}).json()
    assert r["ok"] and r["edited"] and "<mark>重点</mark>" in r["html"]
    assert '<span style="color: #d92d20">红字</span>' in r["html"] or 'style="color:#d92d20"' in r["html"].replace(" ", "")
    assert "position" not in r["html"]  # only colour survives the sanitizer
    pipe._refresh_card(pid)  # stars, groups, classification all rewrite the card
    card = (config.knowledge_dir / registry.get_paper(pid)["kb_path"]).read_text("utf-8")
    assert "我的补充" in card and "edited: true" in card and "## 我的笔记" in card and "notes]]" in card
    r = c.post(f"/paper/{pid}/card", json={"csrf": tok, "reset": True}).json()
    assert r["ok"] and not r["edited"] and "我的补充" not in r["html"]
    assert not c.post(f"/paper/{pid}/card", json={"csrf": tok, "text": " "}).json()["ok"]
