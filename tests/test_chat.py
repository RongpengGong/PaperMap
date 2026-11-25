"""AI discussion next to the PDF: chat with a chosen model, summary, write to notes on request."""
import json

from papermap import chat, library
from papermap.evidence import document_revision

from .conftest import OWN_DOI
from .test_reader import client as reader_client
from .test_web import _token


def test_figure_chat_uses_current_pixels_and_tracks_each_turn(config, registry, tmp_path, monkeypatch):
    import base64
    from fastapi.testclient import TestClient

    from papermap.llm import LLMHTTPError
    from papermap.web.app import create_app
    from .test_figures import figure_pdf
    from .test_pipeline import make_pipeline

    pipe, completions = make_pipeline(config, registry)
    pipe.handle_path(figure_pdf(tmp_path / "figure.pdf"))
    client = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    fig = client.get("/api/figures/1").json()[0]
    revision = document_revision(pipe.pdf_for(1))
    assert fig["revision"] == revision
    registry.execute("UPDATE figures SET explanation=? WHERE id=?", ("Existing explanation of panel B.", fig["id"]))
    route = "/api/paper/1/chat"
    basic = {"csrf": _token(client), "text": "Why are the panel B values different?"}
    bound = {**basic, "figure_id": fig["id"], "figure_revision": revision}
    first = client.post(route, json={**bound, "legend": "untrusted replacement", "page": 999}).json()
    assert first["ok"]
    ref = first["user"]["figure"]
    assert ref == {"id": fig["id"], "label": fig["label"], "page": fig["page"] + 1, "revision": revision}
    assert "Figures are not visible" not in completions.last_system
    content, image = completions.last_messages[-1]["content"]
    assert fig["legend"] in content["text"] and "Existing explanation of panel B." in content["text"]
    assert "untrusted replacement" not in content["text"] and "[p.1]" in content["text"]
    gallery_png = client.get(f"/api/figure/{fig['id']}.png", params={"revision": revision}).content
    assert gallery_png.startswith(b"\x89PNG") and base64.b64decode(image["image_url"]["url"].split(",")[1]) == gallery_png

    again = client.post(route, json={**bound, "text": "What do these axes mean?"}).json()
    assert again["ok"] and again["user"]["figure"] == ref
    assert all(isinstance(m["content"], str) for m in completions.last_messages[:-1])
    assert "Figure discussed: Fig. 1 [p.1]" in completions.last_messages[1]["content"]
    regular = client.post(route, json=basic).json()
    assert regular["ok"] and not regular["user"].get("figure") and isinstance(completions.last_messages[-1]["content"], str)
    assert "Figures are not visible" in completions.last_system
    assert [m.get("figure") for m in client.get(route).json()["messages"] if m["role"] == "user"] == [ref, ref, None]
    card = config.knowledge_dir / registry.get_paper(1)["kb_path"]
    assert "data:image/" not in chat.chat_path(card).read_text()
    count = completions.chat_calls
    other = registry.execute("INSERT INTO figures (paper_id,label,page) VALUES (123,'Fig. 9',0)").lastrowid
    for invalid in ({"figure_id": other}, {"figure_revision": "0" * 64}, {"figure_id": True}, {"figure_revision": None}):
        assert not client.post(route, json={**bound, **invalid}).json()["ok"]
    assert completions.chat_calls == count

    def no_images(**kwargs):
        raise LLMHTTPError(400, "image inputs not supported")
    with monkeypatch.context() as patch:
        patch.setattr(completions, "create", no_images)
        failed = client.post(route, json=bound).json()
        assert not failed["ok"] and "图片" in failed["message"]
    assert len(client.get(route).json()["messages"]) == 6
    assert client.post(route + "/summary", json={"csrf": basic["csrf"]}).json()["ok"]
    assert "Figure discussed: Fig. 1 [p.1]" in completions.last_messages[-1]["content"]
    assert client.get(route).json()["archive"][0]["messages"][0]["figure"] == ref


def test_chat_send_summary_and_notes(config, registry, pdfs):
    c, pipe, completions = reader_client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    row = registry.get_paper(pid)
    card = config.knowledge_dir / row["kb_path"]
    notes = library.notes_path(config.knowledge_dir, row["kb_path"])
    notes.write_text("我自己的笔记\n", "utf-8")
    tok = _token(c)

    panel = c.get(f"/paper/{pid}/panel").text
    assert 'data-action="toggle-chat"' in panel and f'aria-controls="paper-chat-{pid}"' in panel
    assert 'data-paper-chat-drawer hidden' in panel and panel.count(f'data-chat="{pid}"') == 1
    assert 'data-tab="chat"' not in panel
    got = c.get(f"/api/paper/{pid}/chat").json()
    assert got["ok"] and got["messages"] == [] and got["models"][0]["key"] == "main"

    assert c.post(f"/api/paper/{pid}/chat", json={"csrf": "x", "text": "hi"}).status_code == 403
    assert not c.post(f"/api/paper/{pid}/chat", json={"csrf": tok, "text": "hi", "model": "deepseek|evil"}).json()["ok"]
    assert not c.post(f"/api/paper/{pid}/chat", json={"csrf": tok, "text": "  "}).json()["ok"]

    r = c.post(f"/api/paper/{pid}/chat", json={"csrf": tok, "text": "这句话什么意思？", "quote": "Fibroblasts at margin", "page": 1}).json()
    assert r["ok"] and 'class="chat-page" data-page="1"' in r["reply"]["html"]
    system = completions.last_system
    assert "[p.1]" in system and "我自己的笔记" in system  # paper text with page markers, plus the user's notes
    assert "> Fibroblasts at margin" in completions.last_messages[-1]["content"]

    r2 = c.post(f"/api/paper/{pid}/chat", json={"csrf": tok, "text": "那局限呢？"}).json()
    assert r2["ok"] and "第 2 次" in r2["reply"]["content"]
    roles = [m["role"] for m in completions.last_messages]
    assert roles == ["system", "user", "assistant", "user"]  # earlier turns go with the new question
    saved = json.loads(chat.chat_path(card).read_text("utf-8"))
    assert [m["role"] for m in saved["messages"]] == ["user", "assistant", "user", "assistant"]
    assert registry.one("SELECT COUNT(*) n FROM llm_calls WHERE purpose='chat'")["n"] == 2

    s = c.post(f"/api/paper/{pid}/chat/summary", json={"csrf": tok}).json()
    assert s["ok"] and "[p.1]" in s["text"]
    assert notes.read_text("utf-8") == "我自己的笔记\n"  # nothing is written before the user confirms
    # ending the discussion starts a fresh one; the old one is kept under "earlier discussions"
    after = c.get(f"/api/paper/{pid}/chat").json()
    assert after["messages"] == [] and len(after["archive"]) == 1
    old = after["archive"][0]
    assert old["n"] == 2 and old["first"] == "这句话什么意思？" and "p.1" in old["summary_html"] and len(old["messages"]) == 4

    w = c.post(f"/api/paper/{pid}/chat/notes", json={"csrf": tok, "text": s["text"] + "\n- 我加的一条", "clear": True}).json()
    text = notes.read_text("utf-8")
    assert w["ok"] and text.startswith("我自己的笔记\n\n## 与 AI 讨论 · ") and "- 我加的一条" in text
    assert c.get(f"/api/paper/{pid}/chat").json()["messages"] == []
    assert not c.post(f"/api/paper/{pid}/chat/summary", json={"csrf": tok}).json()["ok"]
    c.post(f"/api/paper/{pid}/chat", json={"csrf": tok, "text": "再问一个"})
    assert c.post(f"/api/paper/{pid}/chat/clear", json={"csrf": tok}).json()["ok"]  # 「新对话」
    got = c.get(f"/api/paper/{pid}/chat").json()
    assert got["messages"] == [] and len(got["archive"]) == 2 and got["archive"][0]["first"] == "再问一个"


def test_translation_quotes_follow_the_current_pdf_and_translation(config, registry, pdfs):
    import hashlib

    c, pipe, _ = reader_client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    pdf, card = pipe._paper_files(pid)
    path = card.with_suffix(".translation.json")
    translation = {"revision": document_revision(pdf), "paragraphs": [
        {"src": "Source", "tr": "译文", "page": 1, "endPage": 1},
        {"src": "Across pages", "tr": "跨页", "page": 0, "endPage": 1},
        {"src": "Legacy text", "tr": "旧译文"},
    ]}
    for revision_matches in (True, False):
        if not revision_matches:
            translation["revision"] = "old-revision"
        payload = json.dumps(translation).encode()
        path.write_bytes(payload)
        panel = c.get(f"/paper/{pid}/panel").text
        assert f'data-translation-version="{hashlib.sha256(payload).hexdigest()}"' in panel
        assert panel.count('data-quote-page="2"') == (2 if revision_matches else 0)
        assert panel.count("data-quote-page=") == (2 if revision_matches else 0)
        assert 'data-tr-anchor="2-tr"' in panel


def test_model_choices_only_configured(config):
    keys = [m["key"] for m in chat.model_choices(config)]
    assert keys[0] == "main" and not any(k.startswith("anthropic|") for k in keys)
    config.raw["llm"].setdefault("presets", {})["anthropic"] = {"api_key": "sk-test"}
    keys = [m["key"] for m in chat.model_choices(config)]
    assert "anthropic|claude-sonnet-5-5" in keys
    eff = chat.resolve(config, "anthropic|claude-sonnet-5-5")
    assert eff["provider"] == "anthropic" and eff["model"] == "claude-sonnet-5-5" and eff["api_key"] == "sk-test"
    assert config.llm.get("provider") != "anthropic"  # choosing a chat model never changes the main model


def test_page_chat_drawer_and_notes(config, registry, pdfs):
    from papermap.kb_writer import USER_BEGIN, USER_END, read_user_section

    c, pipe, completions = reader_client(config, registry, pdfs)
    tok = _token(c)
    assert 'id="ai_drawer"' in c.get("/evidence").text
    assert 'id="ai_drawer"' not in c.get("/").text  # the library uses a per-paper drawer

    r = c.post("/api/page-chat", json={"csrf": tok, "key": "/map", "title": "知识地图", "page_text": "", "text": "哪个学科最薄弱？"}).json()
    assert r["ok"]
    assert "LIBRARY OVERVIEW" in completions.last_system  # little text on the page: the library overview is added
    got = c.get("/api/page-chat", params={"key": "/map"}).json()
    assert [m["role"] for m in got["messages"]] == ["user", "assistant"]
    assert c.get("/api/page-chat", params={"key": "/evidence"}).json()["messages"] == []  # one conversation per page
    assert c.post("/api/page-chat", json={"csrf": tok, "key": "javascript:x", "text": "hi"}).status_code == 400

    s = c.post("/api/page-chat/summary", json={"csrf": tok, "key": "/map", "title": "知识地图"}).json()
    w = c.post("/api/page-chat/notes", json={"csrf": tok, "key": "/map", "title": "知识地图", "text": s["text"]}).json()
    assert w["ok"] and w["where"].startswith("discussions/")
    assert "## 与 AI 讨论" in (config.knowledge_dir / w["where"]).read_text("utf-8")

    page = config.knowledge_dir / "concepts" / "msigdb.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"# MSigDB\n\n## 我的笔记\n\n{USER_BEGIN}\n（写下用法、参数心得、踩过的坑…）\n{USER_END}\n\n## 用到它的论文\n\n- x\n", "utf-8")
    c.post("/api/page-chat", json={"csrf": tok, "key": "/concept/msigdb", "title": "MSigDB", "page_text": "MSigDB 用于 GSEA", "text": "怎么用？"})
    w = c.post("/api/page-chat/notes", json={"csrf": tok, "key": "/concept/msigdb", "rel": "concepts/msigdb.md",
                                              "text": "- 用 Hallmark 集合", "clear": True}).json()
    note = read_user_section(page)
    assert w["ok"] and "### 与 AI 讨论" in note and "- 用 Hallmark 集合" in note and "写下用法" not in note
    assert "## 用到它的论文\n\n- x" in page.read_text("utf-8")  # generated part untouched
    assert c.get("/api/page-chat", params={"key": "/concept/msigdb"}).json()["messages"] == []


def test_subscription_models_offer_reasoning_effort(config, monkeypatch):
    for k in ("OPUS", "SONNET", "HAIKU", "FABLE"):  # aliases resolve to the first-party models
        monkeypatch.delenv(f"ANTHROPIC_DEFAULT_{k}_MODEL", raising=False)
    config.raw["llm"].setdefault("presets", {})["claude_cli"] = {"model": "sonnet"}
    monkeypatch.setattr(chat, "_subscription_ready", lambda cfg, pid: pid == "claude_cli")
    ch = {m["key"]: m for m in chat.model_choices(config)}
    opus = ch["claude_cli|opus"]
    assert "high" in opus["efforts"] and "low" in opus["efforts"]
    eff = chat.resolve(config, "claude_cli|opus", "high")
    assert eff["provider"] == "claude_cli" and eff["model"] == "opus" and eff["reasoning_effort"] == "high"
    import pytest

    with pytest.raises(ValueError):
        chat.resolve(config, "claude_cli|opus", "turbo")
    assert not ch.get("main", {}).get("efforts")  # the default test model (DeepSeek) has none


def test_model_picker_follows_interface_language(config, registry, pdfs):
    from .test_reader import client as reader_client

    c, _, _ = reader_client(config, registry, pdfs)
    config.raw.setdefault("appearance", {})["language"] = "en"
    got = c.get("/api/page-chat", params={"key": "/map"}).json()
    assert got["models"][0]["label"] == "Main model · DeepSeek (official) · deepseek-flash"
    r = c.post("/api/page-chat", json={"csrf": _token(c), "key": "/map", "text": "x", "model": "nope|x"}).json()
    assert r["message"] == "This model is not set up in Settings yet"
