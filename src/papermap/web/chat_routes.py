"""Per-paper AI discussion next to the PDF: chat, end-of-discussion summary, write to notes."""
from __future__ import annotations

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .. import chat, library
from ..kb_writer import editable_part
from ..llm import BudgetExceeded, LLMNotConfigured


MAX_SHOWN = 15


def mount_chat(app, config, registry, pipeline, md_html, json_body, pdf_file, background, kb_notes_path, tr=lambda s: s):
    def models():
        """The picker's labels ("主模型 · ChatGPT 订阅 · gpt-x") in the interface language."""
        return [{**m, "label": " · ".join(tr(part) for part in m["label"].split(" · "))} for m in chat.model_choices(config)]

    def paper(paper_id: int):
        row = registry.get_paper(paper_id)
        if row is None or not row["kb_path"] or row["kb_path"].count("/") < 2:
            raise HTTPException(404)
        card = config.knowledge_dir / row["kb_path"]
        return row, card

    def render(m: dict) -> dict:
        out = {k: m.get(k) for k in ("role", "content", "model", "at", "quote", "page", "run_id", "squad_to")}
        if m.get("figure"):
            out["figure"] = m["figure"]
        out["html"] = chat.page_links(md_html(m["content"])) if m["role"] == "assistant" else ""
        return out

    def archived(data: dict) -> list[dict]:
        out = []
        for a in (data.get("archive") or [])[:MAX_SHOWN]:
            first = next((m["content"] for m in a["messages"] if m["role"] == "user"), "")
            out.append({"ended_at": a["ended_at"], "first": first[:80], "n": sum(m["role"] == "user" for m in a["messages"]),
                        "summary_html": md_html(a["summary"]) if a.get("summary") else "",
                        "messages": [render(m) for m in a["messages"]]})
        return out

    def error(exc: Exception) -> dict:
        if isinstance(exc, (ValueError, LLMNotConfigured, BudgetExceeded)):
            return {"ok": False, "message": tr(str(exc))}
        return {"ok": False, "message": f"{type(exc).__name__}: {str(exc)[:300]}"}

    @app.get("/api/paper/{paper_id}/chat")
    def chat_get(paper_id: int):
        _, card = paper(paper_id)
        data = chat.load(chat.chat_path(card))
        return {"ok": True, "messages": [render(m) for m in data["messages"]],
                "models": models(), "model": data.get("model") or "main",
                "effort": data.get("effort") or "", "archive": archived(data)}

    @app.post("/api/paper/{paper_id}/chat")
    async def chat_send(paper_id: int, request: Request):
        body = await json_body(request)
        row, card = paper(paper_id)
        notes_p = library.notes_path(config.knowledge_dir, row["kb_path"])
        notes = notes_p.read_text("utf-8") if notes_p.exists() else ""
        card_text = editable_part(card.read_text("utf-8"), config.output_language) if card.exists() else ""
        page = body.get("page")
        try:
            r = await run_in_threadpool(
                chat.send, config, registry, pipeline.llm, paper_id, card, pdf_file(row), notes, card_text,
                str(body.get("text", "")), str(body.get("model") or "main"), str(body.get("quote") or ""),
                int(page) if str(page or "").isdigit() else None, str(body.get("effort") or ""),
                figure_id=body.get("figure_id"), figure_revision=body.get("figure_revision"))
        except Exception as exc:  # noqa: BLE001 - shown in the chat, never crashes the app
            return error(exc)
        return {"ok": True, "user": render(r["user"]), "reply": render(r["reply"])}

    @app.post("/api/paper/{paper_id}/chat/summary")
    async def chat_summary(paper_id: int, request: Request):
        body = await json_body(request)
        row, card = paper(paper_id)
        try:
            text = await run_in_threadpool(chat.summarize, config, pipeline.llm, chat.chat_path(card), row["title"] or "",
                                           str(body.get("model") or "main"), paper_id, str(body.get("effort") or ""))
        except Exception as exc:  # noqa: BLE001
            return error(exc)
        return {"ok": True, "text": text}

    @app.post("/api/paper/{paper_id}/chat/notes")
    async def chat_to_notes(paper_id: int, request: Request):
        """Only on the user's click: append the (edited) summary to their notes."""
        body = await json_body(request)
        row, card = paper(paper_id)
        try:
            notes = chat.append_to_notes(config, library.notes_path(config.knowledge_dir, row["kb_path"]),
                                         str(body.get("text", "")))
        except ValueError as exc:
            return {"ok": False, "message": tr(str(exc))}
        if body.get("clear"):
            chat.clear(chat.chat_path(card))
        background("reindex", pipeline.reindex, paper_id)
        return {"ok": True, "notes": notes}

    @app.post("/api/paper/{paper_id}/chat/clear")
    async def chat_clear(paper_id: int, request: Request):
        await json_body(request)
        _, card = paper(paper_id)
        chat.clear(chat.chat_path(card))
        return {"ok": True}

    # ---------------------------------------------------------------- any other page
    def page_key(value) -> str:
        key = str(value or "").split("?")[0].split("#")[0][:200]
        if not key.startswith("/"):
            raise HTTPException(400)
        return key

    @app.get("/api/page-chat")
    def page_chat_get(key: str):
        data = chat.load(chat.page_store(config.data_dir, page_key(key)))
        return {"ok": True, "messages": [render(m) for m in data["messages"]],
                "models": models(), "model": data.get("model") or "main",
                "effort": data.get("effort") or "", "archive": archived(data)}

    @app.post("/api/page-chat")
    async def page_chat_send(request: Request):
        body = await json_body(request)
        key = page_key(body.get("key"))
        system = chat.page_system(config, registry, str(body.get("title") or "")[:200], key, str(body.get("page_text") or ""))
        try:
            r = await run_in_threadpool(chat.converse, config, pipeline.llm, chat.page_store(config.data_dir, key),
                                        system, str(body.get("text", "")), str(body.get("model") or "main"),
                                        str(body.get("quote") or ""), None, None, str(body.get("effort") or ""))
        except Exception as exc:  # noqa: BLE001
            return error(exc)
        return {"ok": True, "user": render(r["user"]), "reply": render(r["reply"])}

    @app.post("/api/page-chat/summary")
    async def page_chat_summary(request: Request):
        body = await json_body(request)
        key = page_key(body.get("key"))
        try:
            text = await run_in_threadpool(chat.summarize, config, pipeline.llm, chat.page_store(config.data_dir, key),
                                           str(body.get("title") or key), str(body.get("model") or "main"), None,
                                           str(body.get("effort") or ""))
        except Exception as exc:  # noqa: BLE001
            return error(exc)
        return {"ok": True, "text": text}

    @app.post("/api/page-chat/notes")
    async def page_chat_notes(request: Request):
        """Only on the user's click. Pages with a notes area get it there; others a discussion file."""
        body = await json_body(request)
        key = page_key(body.get("key"))
        rel = str(body.get("rel") or "")
        try:
            if rel:
                chat.append_to_section(config, kb_notes_path(rel), str(body.get("text", "")))
                where = rel
            else:
                where = chat.append_to_discussion(config, str(body.get("title") or key)[:200], key, str(body.get("text", "")))
        except ValueError as exc:
            return {"ok": False, "message": tr(str(exc))}
        pipeline.kb_commit(f"AI discussion notes for {key}")
        if body.get("clear"):
            chat.clear(chat.page_store(config.data_dir, key))
        return {"ok": True, "where": where}

    @app.post("/api/page-chat/clear")
    async def page_chat_clear(request: Request):
        body = await json_body(request)
        chat.clear(chat.page_store(config.data_dir, page_key(body.get("key"))))
        return {"ok": True}
