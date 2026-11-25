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

from . import figures, ocr
from .config import PRESETS, PROVIDERS, SIGN_IN_PROVIDERS, Config
from .evidence import document_revision
from .kb_writer import atomic_write
from .llm import LLMClient
from .registry import Registry

MAX_PAPER_CHARS = 120000
MAX_HISTORY = 24  # earlier turns sent with each message
MAX_MESSAGE_CHARS = 8000
MAX_QUOTE_CHARS = 4000
MAX_PAGE_CHARS = 40000
_LOCK = threading.Lock()
_TEXT_CACHE: dict[tuple[str, str, int], str] = {}


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
        if pid == "antigravity_acp":
            from .antigravity_cli import get_cli

            return bool(get_cli(config.data_dir).status()["ready"])
    except Exception:  # noqa: BLE001 - an unavailable sign-in just hides the option
        return False
    return False


_CATALOG: dict[str, tuple[float, list[dict]]] = {}
# Provider names shown in the model picker; listed here so the UI catalogs translate them.
PROVIDER_LABELS = ("主模型", "自定义", "DeepSeek 官方", "ChatGPT 订阅", "Claude 订阅（Claude Code）", "Antigravity 订阅", "Kimi 开放平台",
                   "智谱开放平台（GLM）", "阿里云百炼（通义千问）")


def _chatgpt_catalog(config: Config) -> list[dict]:
    """The account's ChatGPT models with their reasoning-effort levels (cached 10 minutes)."""
    import time

    from .llm import list_chatgpt_model_options

    hit = _CATALOG.get("chatgpt_plan")
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    try:
        eff = config.resolve_llm({**config.llm, "provider": "chatgpt_plan"})
        rows = list_chatgpt_model_options(eff, timeout_s=15)
    except Exception:  # noqa: BLE001 - offline: fall back to the saved model without levels
        rows = []
    _CATALOG["chatgpt_plan"] = (time.monotonic(), rows)
    return rows


def _efforts(config: Config, pid: str, model: str) -> list[str]:
    if pid == "claude_cli":
        from .claude_cli import model_option

        return list(model_option(model)["reasoning_efforts"])
    if pid == "chatgpt_plan":
        return next((list(r["reasoning_efforts"]) for r in _chatgpt_catalog(config) if r["id"] == model), [])
    return []


def model_choices(config: Config, probe_all: bool = False) -> list[dict]:
    """Models the user has already set up (key or sign-in present). ``main`` = the main model.
    Signed-in providers also list their reasoning-effort levels. ``probe_all`` also asks sign-in
    providers that were never chosen in PaperMap (the squad model roster wants every subscription)."""
    llm = config.llm
    main = config.effective_llm()
    mp = main.get("provider", "")
    out = [{"key": "main", "label": "主模型 · " + _label(main), "ready": config.model_ready(),
            "efforts": _efforts(config, mp, main.get("model", "")),
            "effort": main.get("reasoning_effort") or ""}]
    seen = {(mp, main.get("model"))}

    def add(pid: str, model: str, label: str, efforts: list[str] | None = None) -> None:
        if model and (pid, model) not in seen:
            seen.add((pid, model))
            level = (states.get(pid) or {}).get("reasoning_effort") or ""
            out.append({"key": f"{pid}|{model}", "label": label, "ready": True, "efforts": efforts or [],
                        "effort": level if level in (efforts or []) else ""})

    states = llm.get("presets") or {}
    for pid, pre in PROVIDERS.items():
        if config.resolve_llm({**llm, "provider": pid})["api_key"]:
            for m in pre["models"]:
                add(pid, m, f"{pre['label']} · {m}")
    for pid, pre in PRESETS.items():
        st = states.get(pid) or {}
        if pid in SIGN_IN_PROVIDERS:
            if pid not in states and mp != pid and not probe_all:
                continue  # never set up: do not probe sign-in state
            ready = out[0]["ready"] if pid == mp else _subscription_ready(config, pid)
            if not ready:
                continue
            if pid == "antigravity_acp":
                from .antigravity_cli import get_cli

                try:
                    catalog = get_cli(config.data_dir).model_options()
                except Exception:  # The CLI might have signed out since the readiness check.
                    catalog = []
                for row in catalog:
                    efforts = row.get("reasoning_efforts") or []
                    if pid == mp and (row["id"] == main["model"] or (main["model"] == "auto" and row.get("is_current"))):
                        out[0]["efforts"] = efforts
                    add(pid, row["id"], f"{pre['label']} · {row.get('label') or row['id']}", efforts)
                continue
            if pid == "claude_cli":
                from .claude_cli import MODELS

                models = [m["id"] for m in MODELS]
            else:
                models = [r["id"] for r in _chatgpt_catalog(config)] or ([st["model"]] if st.get("model") else [])
            for m in models:
                add(pid, m, f"{pre['label']} · {m}", _efforts(config, pid, m))
            continue
        if not (st.get("api_key") or "").strip():
            continue
        for m in list(pre.get("models") or {}) or ([st["model"]] if st.get("model") else []):
            add(pid, m, f"{pre['label']} · {m}")
    c = llm.get("custom") or {}
    custom = config.resolve_llm({**llm, "provider": "custom"})
    if c.get("base_url") and custom["model"] and custom["api_key"]:
        add("custom", custom["model"], f"自定义 · {custom['model']}")
    return out


def resolve(config: Config, key: str, effort: str = "", choices: list[dict] | None = None) -> dict:
    """Connection settings for one of :func:`model_choices` (anything else is refused).
    ``effort`` must be one of that model's levels; empty keeps the configured default.
    ``choices`` lets a caller reuse a list it already has (probing sign-in providers is slow)."""
    if choices is not None:
        choices = {c["key"]: c for c in choices}
    else:
        choices = {c["key"]: c for c in model_choices(config)} if (key not in ("", "main") or effort) else {}
    if not key or key == "main":
        eff = config.effective_llm()
        key = "main"
    else:
        if key not in choices:
            raise ValueError("这个模型还没有在设置里配置好")
        pid, model = key.split("|", 1)
        llm = copy.deepcopy(config.llm)
        llm["provider"] = pid
        if pid in PROVIDERS:
            llm["model"] = model
        elif pid in PRESETS:
            state = llm.setdefault("presets", {}).setdefault(pid, {})
            if pid == "antigravity_acp" and state.get("model") != model:
                state["reasoning_effort"] = choices[key]["effort"]
            state["model"] = model
        eff = config.resolve_llm(llm)
    if effort:
        if effort not in (choices.get(key) or {}).get("efforts", []):
            raise ValueError("这个模型不支持所选的推理强度")
        eff = {**eff, "reasoning_effort": effort}
    return eff


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


MAX_ARCHIVE = 30


def archive(store: Path, summary: str | None = None) -> bool:
    """End the current discussion: its messages move to the list of earlier discussions
    (newest first) and the conversation starts empty. Nothing is deleted."""
    with _LOCK:
        data = load(store)
        if not data["messages"]:
            return False
        data["archive"] = ([{"ended_at": _now(), "messages": data["messages"], "summary": summary or ""}]
                           + list(data.get("archive") or []))[:MAX_ARCHIVE]
        data["messages"] = []
        save(store, data)
        return True


def append(store: Path, *msgs: dict) -> None:
    with _LOCK:
        data = load(store)
        data["messages"] += list(msgs)
        save(store, data)


def set_squad_result(store: Path, run_id: int, text: str) -> None:
    """A squad started from this conversation finished: its report becomes part of the discussion."""
    with _LOCK:
        data = load(store)
        hit = False
        for m in data["messages"]:
            if m.get("role") == "squad" and m.get("run_id") == run_id:
                m["content"] = text
                hit = True
        if hit:
            save(store, data)


def clear(store: Path) -> None:
    """New chat: same as ending without a summary; the old conversation stays in the archive."""
    archive(store)


# ------------------------------------------------------------------ context
def paper_text(pdf: Path, data_dir: Path, limit: int = MAX_PAPER_CHARS) -> str:
    """Full text with ``[p.N]`` page markers so answers can cite pages."""
    try:
        key = (str(pdf), document_revision(pdf), limit)
    except OSError:
        return ""
    if key in _TEXT_CACHE:
        return _TEXT_CACHE[key]
    parts, total = [], 0
    with pymupdf.open(pdf) as doc:
        auto = ocr.scanned_document(doc)
        cache = ocr.cache_for(pdf, data_dir)
        for i in range(min(doc.page_count, 80)):
            t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", ocr.page_text(doc[i], cache, auto)).strip()
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


def _system(config: Config, row, card_text: str, notes: str, text: str, *, with_figure: bool = False) -> str:
    return "\n".join([
        "You are a research colleague discussing ONE paper with its reader inside PaperMap, a local literature app.",
        f"Answer in {_language(config)}. Be concise and specific; use Markdown.",
        "Ground statements about the paper in its text below and cite the page like [p.5] (page markers are "
        "given in the text). If the paper does not say something, say so plainly. When you add background "
        "knowledge that is not from the paper, mark it as background (背景知识) so the reader can tell it apart.",
        ("The current user message includes a cropped figure from this PDF. Read its actual panels, labels, axes "
         "and legend to answer the question. Previous figure explanations are model-generated context. Say which "
         "details are unreadable and identify any inference. Only the figure attached to this turn is visible."
         if with_figure else "Figures are not visible to you; only their legends are in the text."),
        "",
        f"Title: {row['title']}",
        f"Journal: {row['journal'] or ''} ({row['year'] or ''})   DOI: {row['doi'] or 'none'}",
        "", "===== PaperMap card (model-generated summary) =====", card_text[:12000] or "(none)",
        "", "===== Reader's own notes =====", notes[:8000] or "(empty)",
        "", "===== PAPER TEXT =====", text or "(no text could be extracted from the PDF)",
    ])


def _user_content(msg: dict) -> str:
    parts = []
    figure = msg.get("figure")
    if isinstance(figure, dict) and figure.get("label") and figure.get("page"):
        parts.append(f"Figure discussed: {figure['label']} [p.{figure['page']}]")
    if msg.get("quote"):
        where = f" (p.{msg['page']})" if msg.get("page") else ""
        quoted = "\n".join("> " + ln for ln in msg["quote"].splitlines())
        parts.append(f"About this passage{where}:\n{quoted}")
    return "\n\n".join([*parts, msg["content"]])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ actions
def converse(config: Config, llm: LLMClient, store: Path, system: str, text: str, key: str = "main",
             quote: str = "", page: int | None = None, paper_id: int | None = None, effort: str = "",
             *, attachment: tuple[dict, bytes] | None = None) -> dict:
    """One turn: earlier turns from ``store`` go with the new message; both are saved after the answer."""
    text = (text or "").strip()[:MAX_MESSAGE_CHARS]
    quote = (quote or "").strip()[:MAX_QUOTE_CHARS]
    if not text and quote:
        text = "请解释这段。" if config.output_language != "en" else "Please explain this passage."
    if not text:
        raise ValueError("请输入问题")
    eff = resolve(config, key, effort)
    with _LOCK:
        data = load(store)
    # a squad's finished report (role "squad") counts as an answer the reader can discuss further
    history = [{"role": "user" if m["role"] == "user" else "assistant",
                "content": _user_content(m) if m["role"] == "user" else m["content"]}
               for m in data["messages"][-MAX_HISTORY:] if m["role"] != "squad" or m.get("content")]
    msg = {"role": "user", "content": text, "at": _now()}
    if quote:
        msg["quote"] = quote
        if page:
            msg["page"] = int(page)
    image_options = {}
    if attachment:
        figure, png = attachment
        msg["figure"] = {k: figure[k] for k in ("id", "label", "page", "revision")}
        image_options["images"] = [png]
    user_prompt = _user_content(msg)
    if attachment:
        user_prompt = (f"Figure legend:\n{figure['legend'] or '(unavailable)'}\n\n"
                       f"Previously generated explanation:\n{(figure['explanation'] or '')[:12000] or '(none)'}"
                       f"\n\n{user_prompt}")
    try:
        res = llm.chat_text(system, user_prompt, purpose="chat", paper_id=paper_id, max_tokens=4096,
                            eff=eff, history=history, **image_options)
    except Exception as exc:
        if attachment:
            status = getattr(exc, "status_code", None)
            reason = str(exc)[:1000].lower()
            refers_to_image = any(word in reason for word in ("image", "vision", "multimodal", "图片", "图像"))
            if status == 415 or (status in (400, 422) and (refers_to_image or ("content" in reason and "string" in reason))):
                raise ValueError("模型服务未接受这次带图请求，请选择支持图片的模型或重新尝试。") from exc
        raise
    reply = {"role": "assistant", "content": res.raw.strip(), "at": _now(),
             "model": res.model + (f" · {effort}" if effort else "")}
    with _LOCK:
        data = load(store)
        data["messages"] += [msg, reply]
        data["model"] = key
        data["effort"] = effort
        save(store, data)
    return {"user": msg, "reply": reply}


def send(config: Config, registry: Registry, llm: LLMClient, paper_id: int, card: Path, pdf: Path | None,
         notes: str, card_text: str, text: str, key: str = "main", quote: str = "", page: int | None = None,
         effort: str = "", *, figure_id: int | None = None, figure_revision: str | None = None) -> dict:
    attachment = None
    if figure_id is not None or figure_revision is not None:
        if (type(figure_id) is not int or not 0 < figure_id < 2**63
                or not isinstance(figure_revision, str) or not re.fullmatch(r"[0-9a-f]{64}", figure_revision)):
            raise ValueError("图表信息不完整，请重新打开图表。")
        attachment = figures.chat_attachment(registry, paper_id, pdf, figure_id, figure_revision,
                                             config.data_dir / "cache" / "figures")
    row = registry.get_paper(paper_id)
    paper = paper_text(pdf, config.data_dir) if pdf else ""
    if attachment and (pdf is None or document_revision(pdf) != figure_revision):
        raise ValueError(figures.STALE_FIGURE)
    system = _system(config, row, card_text, notes, paper, with_figure=attachment is not None)
    return converse(config, llm, chat_path(card), system, text, key, quote, page, paper_id, effort,
                    attachment=attachment)


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
              paper_id: int | None = None, effort: str = "", end: bool = True) -> str:
    """Summary of the current discussion; with ``end`` the discussion is then archived (a fresh one starts).
    If the model fails, nothing is archived and the user can go on."""
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
                        purpose="chat_summary", paper_id=paper_id, max_tokens=2048, eff=resolve(config, key, effort))
    text = res.raw.strip()
    if end:
        archive(store, text)
    return text


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
