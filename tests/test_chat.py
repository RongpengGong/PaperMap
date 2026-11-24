"""AI discussion next to the PDF: chat with a chosen model, summary, write to notes on request."""
import json

from papermap import chat, library

from .conftest import OWN_DOI
from .test_reader import client as reader_client
from .test_web import _token


def test_chat_send_summary_and_notes(config, registry, pdfs):
    c, pipe, completions = reader_client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    row = registry.get_paper(pid)
    card = config.knowledge_dir / row["kb_path"]
    notes = library.notes_path(config.knowledge_dir, row["kb_path"])
    notes.write_text("我自己的笔记\n", "utf-8")
    tok = _token(c)

    assert 'data-tab="chat"' in c.get(f"/paper/{pid}/panel").text
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

    w = c.post(f"/api/paper/{pid}/chat/notes", json={"csrf": tok, "text": s["text"] + "\n- 我加的一条", "clear": True}).json()
    text = notes.read_text("utf-8")
    assert w["ok"] and text.startswith("我自己的笔记\n\n## 与 AI 讨论 · ") and "- 我加的一条" in text
    assert c.get(f"/api/paper/{pid}/chat").json()["messages"] == []
    assert not c.post(f"/api/paper/{pid}/chat/summary", json={"csrf": tok}).json()["ok"]


def test_model_choices_only_configured(config):
    keys = [m["key"] for m in chat.model_choices(config)]
    assert keys[0] == "main" and not any(k.startswith("anthropic|") for k in keys)
    config.raw["llm"].setdefault("presets", {})["anthropic"] = {"api_key": "sk-test"}
    keys = [m["key"] for m in chat.model_choices(config)]
    assert "anthropic|claude-sonnet-5-5" in keys
    eff = chat.resolve(config, "anthropic|claude-sonnet-5-5")
    assert eff["provider"] == "anthropic" and eff["model"] == "claude-sonnet-5-5" and eff["api_key"] == "sk-test"
    assert config.llm.get("provider") != "anthropic"  # choosing a chat model never changes the main model
