"""Discuss one paper with a model of the user's choice, next to the PDF.

The conversation is kept in the paper's folder (``<stem>.chat.json``). Ending a
discussion asks the model for a summary that the user reviews and edits; only
the user's explicit "write to notes" appends it to ``<stem>.notes.md``.
"""

from __future__ import annotations

import copy
import json
import re
import threading
from datetime import date, datetime, timezone
from pathlib import Path

import pymupdf

from . import ocr
from .config import PRESETS, PROVIDERS, Config
from .kb_writer import atomic_write
from .llm import LLMClient
from .registry import Registry

MAX_PAPER_CHARS = 120000
MAX_HISTORY = 24  # earlier turns sent with each message
MAX_MESSAGE_CHARS = 8000
MAX_QUOTE_CHARS = 4000
MAX_PAGE_CHARS = 40000
_LOCK = threading.Lock()
_TEXT_CACHE: dict[tuple[str, float], str] = {}


# ------------------------------------------------------------------ models
def _label(eff: dict) -> str:
    name = eff.get("preset_label") or PROVIDERS.get(eff.get("provider", ""), {}).get("label") or (
        "自定义" if eff.get("provider") == "custom" else eff.get("provider", ""))
    return f"{name} · {eff.get('model') or '?'}"


def _subscription_ready(config: Config, pid: str) -> bool:
    try:
        if pid == "chatgpt_plan":
            from .chatgpt_auth import get_auth

            return bool(get_auth(config.data_dir).status()["ready"])
        if pid == "claude_cli":
            from .claude_cli import get_cli

            return bool(get_cli(config.data_dir).status()["ready"])
    except Exception:  # noqa: BLE001 - an unavailable sign-in just hides the option
        return False
    return False


def model_choices(config: Config) -> list[dict]:
    """Models the user has already set up (key or sign-in present). ``main`` = the main model."""
    llm = config.llm
    main = config.effective_llm()
    out = [{"key": "main", "label": "主模型 · " + _label(main), "ready": config.model_ready()}]
    seen = {(main.get("provider"), main.get("model"))}

    def add(pid: str, model: str, label: str) -> None:
        if model and (pid, model) not in seen:
            seen.add((pid, model))
            out.append({"key": f"{pid}|{model}", "label": label, "ready": True})

    for pid, pre in PROVIDERS.items():
        if config.resolve_llm({**llm, "provider": pid})["api_key"]:
            for m in pre["models"]:
                add(pid, m, f"{pre['label']} · {m}")
    states = llm.get("presets") or {}
    for pid, pre in PRESETS.items():
        st = states.get(pid) or {}
        if pid in ("chatgpt_plan", "claude_cli"):
            if pid not in states and main.get("provider") != pid:
                continue  # never set up: do not probe sign-in state
            if not _subscription_ready(config, pid):
                continue
            if pid == "claude_cli":
                from .claude_cli import MODELS

                models = [m["id"] for m in MODELS]
            else:
                models = [st.get("model")] if st.get("model") else []
        else:
            if not (st.get("api_key") or "").strip():
                continue
            models = list(pre.get("models") or {}) or ([st["model"]] if st.get("model") else [])
        for m in models:
            add(pid, m, f"{pre['label']} · {m}")
    c = llm.get("custom") or {}
    custom = config.resolve_llm({**llm, "provider": "custom"})
    if c.get("base_url") and custom["model"] and custom["api_key"]:
        add("custom", custom["model"], f"自定义 · {custom['model']}")
    return out


def resolve(config: Config, key: str) -> dict:
    """Connection settings for one of :func:`model_choices` (anything else is refused)."""
    if not key or key == "main":
        return config.effective_llm()
    if key not in {c["key"] for c in model_choices(config)}:
        raise ValueError("这个模型还没有在设置里配置好")
    pid, model = key.split("|", 1)
    llm = copy.deepcopy(config.llm)
    llm["provider"] = pid
    if pid in PROVIDERS:
        llm["model"] = model
    elif pid in PRESETS:
        llm.setdefault("presets", {}).setdefault(pid, {})["model"] = model
    return config.resolve_llm(llm)


# ------------------------------------------------------------------ storage
def chat_path(card: Path) -> Path:
    """A paper's conversation lives in its folder."""
    return card.with_name(f"{card.stem}.chat.json")


def page_store(data_dir: Path, key: str) -> Path:
    """Conversations about other pages (evidence notes, map, a field...) live in <data>/chats/."""
    slug = re.sub(r"[^\w\-]+", "_", key.strip("/") or "home")[:120] or "home"
    return data_dir / "chats" / f"{slug}.json"


def load(store: Path) -> dict:
    try:
        data = json.loads(store.read_text("utf-8"))
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"messages": [], "model": ""}


def save(store: Path, data: dict) -> None:
    store.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(store, json.dumps(data, ensure_ascii=False, indent=1))


def clear(store: Path) -> None:
    with _LOCK:
        data = load(store)
        data["messages"] = []
        save(store, data)


# ------------------------------------------------------------------ context
def paper_text(pdf: Path, data_dir: Path, limit: int = MAX_PAPER_CHARS) -> str:
    """Full text with ``[p.N]`` page markers so answers can cite pages."""
    try:
        key = (str(pdf), pdf.stat().st_mtime)
    except OSError:
        return ""
    if key in _TEXT_CACHE:
        return _TEXT_CACHE[key]
    parts, total = [], 0
    with pymupdf.open(pdf) as doc:
        auto = ocr.scanned_document(doc)
        cache = ocr.cache_for(pdf, data_dir)
        for i in range(min(doc.page_count, 80)):
            t = ocr.page_text(doc[i], cache, auto).strip()
            if not t:
                continue
            chunk = f"\n\n[p.{i + 1}]\n{t}"
            if total + len(chunk) > limit:
                parts.append(f"\n\n[... text after p.{i} omitted for length ...]")
                break
            parts.append(chunk)
            total += len(chunk)
    text = "".join(parts).strip()
    if len(_TEXT_CACHE) > 8:
        _TEXT_CACHE.clear()
    _TEXT_CACHE[key] = text
    return text


def _language(config: Config) -> str:
    return {"en": "English", "bilingual": "Simplified Chinese with English key terms in parentheses"}.get(
        config.output_language, "Simplified Chinese (keep gene, software and technical terms in English)")


def _system(config: Config, row, card_text: str, notes: str, text: str) -> str:
    return "\n".join([
        "You are a research colleague discussing ONE paper with its reader inside PaperMap, a local literature app.",
        f"Answer in {_language(config)}. Be concise and specific; use Markdown.",
        "Ground statements about the paper in its text below and cite the page like [p.5] (page markers are "
        "given in the text). If the paper does not say something, say so plainly. When you add background "
        "knowledge that is not from the paper, mark it as background (背景知识) so the reader can tell it apart.",
        "Figures are not visible to you; only their legends are in the text.",
        "",
        f"Title: {row['title']}",
        f"Journal: {row['journal'] or ''} ({row['year'] or ''})   DOI: {row['doi'] or 'none'}",
        "", "===== PaperMap card (model-generated summary) =====", card_text[:12000] or "(none)",
        "", "===== Reader's own notes =====", notes[:8000] or "(empty)",
        "", "===== PAPER TEXT =====", text or "(no text could be extracted from the PDF)",
    ])


def _user_content(msg: dict) -> str:
    if msg.get("quote"):
        where = f" (p.{msg['page']})" if msg.get("page") else ""
        quoted = "\n".join("> " + ln for ln in msg["quote"].splitlines())
        return f"About this passage{where}:\n{quoted}\n\n{msg['content']}"
    return msg["content"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ actions
def converse(config: Config, llm: LLMClient, store: Path, system: str, text: str, key: str = "main",
             quote: str = "", page: int | None = None, paper_id: int | None = None) -> dict:
    """One turn: earlier turns from ``store`` go with the new message; both are saved after the answer."""
    text = (text or "").strip()[:MAX_MESSAGE_CHARS]
    quote = (quote or "").strip()[:MAX_QUOTE_CHARS]
    if not text and quote:
        text = "请解释这段。" if config.output_language != "en" else "Please explain this passage."
    if not text:
        raise ValueError("请输入问题")
    eff = resolve(config, key)
    with _LOCK:
        data = load(store)
    history = [{"role": m["role"], "content": _user_content(m) if m["role"] == "user" else m["content"]}
               for m in data["messages"][-MAX_HISTORY:]]
    msg = {"role": "user", "content": text, "at": _now()}
    if quote:
        msg["quote"] = quote
        if page:
            msg["page"] = int(page)
    res = llm.chat_text(system, _user_content(msg), purpose="chat", paper_id=paper_id, max_tokens=4096,
                        eff=eff, history=history)
    reply = {"role": "assistant", "content": res.raw.strip(), "model": res.model, "at": _now()}
    with _LOCK:
        data = load(store)
        data["messages"] += [msg, reply]
        data["model"] = key
        save(store, data)
    return {"user": msg, "reply": reply}


def send(config: Config, registry: Registry, llm: LLMClient, paper_id: int, card: Path, pdf: Path | None,
         notes: str, card_text: str, text: str, key: str = "main", quote: str = "", page: int | None = None) -> dict:
    row = registry.get_paper(paper_id)
    paper = paper_text(pdf, config.data_dir) if pdf else ""
    return converse(config, llm, chat_path(card), _system(config, row, card_text, notes, paper), text, key,
                    quote, page, paper_id)


def library_overview(registry: Registry, limit_chars: int = 30000) -> str:
    """Fields with their papers (author, year, title, one-line conclusion) for pages that are mostly pictures."""
    lines = []
    for f in registry.query("SELECT id, slug, name_zh, name_en, axis FROM fields WHERE status='active' ORDER BY axis, name_zh"):
        papers = registry.query(
            "SELECT p.first_author, p.year, p.title, p.card_json FROM paper_fields pf JOIN papers p ON p.id=pf.paper_id "
            "WHERE pf.field_id=? AND pf.status='accepted' AND p.status != 'ignored' ORDER BY p.year DESC", (f["id"],))
        if not papers:
            continue
        lines.append(f"\n## {f['name_zh'] or f['name_en']} ({f['axis']}, {len(papers)} papers)")
        for p in papers:
            one = ""
            try:
                one = (json.loads(p["card_json"] or "{}").get("one_sentence") or "")[:200]
            except ValueError:
                pass
            lines.append(f"- {p['first_author'] or '?'} {p['year'] or ''}: {p['title']}" + (f" — {one}" if one else ""))
    text = "\n".join(lines).strip()
    return text[:limit_chars]


def page_system(config: Config, registry: Registry, title: str, path: str, page_text: str) -> str:
    page_text = (page_text or "").strip()[:MAX_PAGE_CHARS]
    parts = [
        "You are a research colleague helping the reader think about a page of their own literature library "
        "in PaperMap, a local app that turns their papers into cards, fields (sub-disciplines), a knowledge map, "
        "handbooks, evidence notes and Q&A.",
        f"Answer in {_language(config)}. Be concise and specific; use Markdown.",
        "Ground statements in the page content and library overview below, naming papers as Author Year. "
        "When you add background knowledge that is not in the material, mark it as background (背景知识). "
        "If the material does not cover something, say so.",
        "", f"===== PAGE: {title} ({path}) =====", page_text or "(empty)",
    ]
    if len(page_text) < 4000:
        parts += ["", "===== LIBRARY OVERVIEW (fields and their papers) =====", library_overview(registry) or "(empty)"]
    return "\n".join(parts)


def summarize(config: Config, llm: LLMClient, store: Path, subject: str, key: str = "main",
              paper_id: int | None = None) -> str:
    msgs = load(store)["messages"]
    if not msgs:
        raise ValueError("还没有讨论内容")
    transcript = "\n\n".join(
        ("READER: " + _user_content(m)) if m["role"] == "user" else ("ASSISTANT: " + m["content"]) for m in msgs)
    system = "\n".join([
        "You turn a discussion about one paper or one page of a literature library into notes for the reader's own notebook.",
        f"Write in {_language(config)}, as a Markdown bullet list without a heading.",
        "Keep only what matters for later: insights and conclusions the reader reached, answers to their "
        "questions, doubts or limitations raised, open questions and things to check or try. Keep page "
        "citations like [p.5] and paper names. Do not add anything that was not in the discussion. Keep it short (3-10 bullets).",
    ])
    res = llm.chat_text(system, f"Subject: {subject}\n\n===== DISCUSSION =====\n{transcript[-60000:]}",
                        purpose="chat_summary", paper_id=paper_id, max_tokens=2048, eff=resolve(config, key))
    return res.raw.strip()


def _heading(config: Config) -> str:
    return ("## Discussion with AI · " if config.output_language == "en" else "## 与 AI 讨论 · ") + date.today().isoformat()


def append_to_section(config: Config, page: Path, summary: str) -> str:
    """Append to the user's block of a field / tool / handbook page (the only part the user owns)."""
    from .kb_writer import read_user_section, write_user_section

    summary = (summary or "").strip()
    if not summary:
        raise ValueError("总结是空的")
    old = read_user_section(page)
    if old is None:
        raise ValueError("这个页面没有「我的笔记」区域")
    if old.strip().startswith("（") and old.strip().endswith("）") and "\n" not in old.strip():
        old = ""  # the placeholder hint
    new = (old.rstrip() + "\n\n" if old.strip() else "") + _heading(config).replace("## ", "### ") + "\n\n" + summary
    write_user_section(page, new)
    return new


def append_to_discussion(config: Config, title: str, key: str, summary: str) -> str:
    """Pages without a notes area: knowledge/discussions/<page>.md, one file per page, newest at the end."""
    summary = (summary or "").strip()
    if not summary:
        raise ValueError("总结是空的")
    slug = page_store(config.data_dir, key).stem
    rel = f"discussions/{slug}.md"
    path = config.knowledge_dir / rel
    old = path.read_text("utf-8") if path.exists() else f"# {title}\n"
    atomic_write(path, old.rstrip() + "\n\n" + _heading(config) + "\n\n" + summary + "\n")
    return rel


def append_to_notes(config: Config, notes_path: Path, summary: str) -> str:
    summary = (summary or "").strip()
    if not summary:
        raise ValueError("总结是空的")
    head = _heading(config)
    old = notes_path.read_text("utf-8") if notes_path.exists() else ""
    new = (old.rstrip() + "\n\n" if old.strip() else "") + head + "\n\n" + summary + "\n"
    atomic_write(notes_path, new)
    return new


def page_links(html: str) -> str:
    """Turn [p.5] / [p.5-6] citations in rendered answers into page jumps."""
    return re.sub(r"\[p\.\s?(\d{1,4})(?:\s?[-–]\s?(\d{1,4}))?\]",
                  lambda m: f'<a href="#" class="chat-page" data-page="{m.group(1)}">p.{m.group(1)}'
                            + (f"–{m.group(2)}" if m.group(2) else "") + "</a>", html)
