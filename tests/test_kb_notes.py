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
