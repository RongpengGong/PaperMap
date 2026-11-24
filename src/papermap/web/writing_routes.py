"""Writing from the library (/write) and the manuscript citation check (/citecheck)."""
from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from .. import citecheck, clips, writing
from .. import purposes as _purposes

MAX_DOCX = 30 * 1024 * 1024
VERDICT_LABELS = {"supports": "支持", "partial": "部分支持", "not_found": "没找到", "contradicts": "相反",
                  "unclear": "无法确认", "not_in_library": "不在文献库", "no_text": "没有全文", "pending": "待核对"}


def mount_writing(app, config, registry, pipeline, render, json_body, md_html, tr, check_csrf, csrf_token, flat_tree):
    writing.ensure_schema(registry)
    citecheck.ensure_schema(registry)
    clips.ensure_schema(registry)

    def scopes():
        out = [("star", "★ " + tr("星标"))]
        out += [(f"u:{u['tag']}", "# " + tr(str(u["label"]))) for u in _purposes.counts(registry)]
        out += [(str(g["id"]), "　" * g["depth"] + g["name"]) for g in flat_tree(registry.collection_tree())]
        out += [(f"f:{f['slug']}", tr("学科") + " · " + (f["name_zh"] or f["name_en"])) for f in registry.active_fields()
                if registry.field_paper_rows(f["id"])]
        return out

    def draft_view(d: dict) -> dict:
        return {"id": d["id"], "topic": d["topic"], "html": md_html(d["text"]), "markdown": writing.markdown(d),
                "refs": [md_html(r) for r in d["refs"]], "cited": d["cited"], "gaps": d["gaps"],
                "model": d["model"], "cost": d["cost_usd"] or 0, "style": d["style"]}

    # ------------------------------------------------------------ writing
    @app.get("/write", response_class=HTMLResponse)
    def write_page(request: Request, id: int | None = None, scope: str = "", ids: str = ""):
        current = None
        if id:
            try:
                current = draft_view(writing.get(registry, id))
            except ValueError:
                current = None
        return render(request, "write.html", scopes=scopes(), history=writing.history(registry), current=current,
                      scope=scope, ids=ids, langs=writing.LANGS, default_lang="zh" if config.output_language == "zh" else "en")

    @app.post("/api/write")
    async def api_write(request: Request):
        body = await json_body(request)
        ids = [int(x) for x in str(body.get("ids") or "").split(",") if x.strip().isdigit()]
        try:
            d = await run_in_threadpool(writing.write, config, registry, pipeline.llm, str(body.get("topic", "")),
                                        str(body.get("scope", "")), ids, str(body.get("style") or "author_year"),
                                        str(body.get("language") or "en"), str(body.get("length") or "medium"))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": tr(str(exc))}
        return {"ok": True, **draft_view(d)}

    @app.post("/api/write/{draft_id}/style")
    async def api_write_style(draft_id: int, request: Request):
        body = await json_body(request)
        style = "numbered" if body.get("style") == "numbered" else "author_year"
        registry.execute("UPDATE write_drafts SET style=? WHERE id=?", (style, draft_id))
        return {"ok": True, **draft_view(writing.get(registry, draft_id))}

    @app.post("/api/write/{draft_id}/delete")
    async def api_write_delete(draft_id: int, request: Request):
        await json_body(request)
        registry.execute("DELETE FROM write_drafts WHERE id=?", (draft_id,))
        return {"ok": True}

    @app.get("/write/{draft_id}.{fmt}")
    def write_export(draft_id: int, fmt: str):
        from .. import citations_export as ce

        try:
            d = writing.get(registry, draft_id)
        except ValueError:
            raise HTTPException(404) from None
        if fmt == "docx":
            return Response(writing.to_docx(d),
                            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            headers={"Content-Disposition": f'attachment; filename="papermap-draft-{draft_id}.docx"'})
        if fmt == "bib":
            return Response(ce.bibtex(registry, d["cited"]), media_type="application/x-bibtex; charset=utf-8",
                            headers={"Content-Disposition": f'attachment; filename="papermap-draft-{draft_id}.bib"'})
        if fmt == "md":
            return Response(writing.markdown(d), media_type="text/markdown; charset=utf-8",
                            headers={"Content-Disposition": f'attachment; filename="papermap-draft-{draft_id}.md"'})
        raise HTTPException(404)

    @app.post("/api/write/{draft_id}/check")
    async def api_write_check(draft_id: int, request: Request):
        """Send a draft to the citation check and start it (drafts are short)."""
        await json_body(request)
        try:
            s = citecheck.create_from_draft(registry, writing.get(registry, draft_id))
        except ValueError as exc:
            return {"ok": False, "message": tr(str(exc))}
        citecheck.start(pipeline, s["id"])
        return {"ok": True, "run": s["id"]}

    # ------------------------------------------------------------ citation check
    @app.get("/citecheck", response_class=HTMLResponse)
    def citecheck_page(request: Request, id: int | None = None):
        current, rows = None, []
        if id:
            try:
                current = citecheck.summary(registry, id)
                rows = citecheck.items(registry, id)
            except ValueError:
                current = None
        return render(request, "citecheck.html", runs=citecheck.runs(registry), current=current, rows=rows,
                      verdicts=VERDICT_LABELS)

    @app.post("/api/citecheck/upload")
    async def citecheck_upload(request: Request):
        form = await request.form()
        check_csrf(str(form.get("csrf", "")), csrf_token)
        f = form.get("file")
        if f is None or not hasattr(f, "read") or not (f.filename or "").lower().endswith(".docx"):
            return {"ok": False, "message": tr("请上传 Word 文件（.docx）")}
        data = await f.read(MAX_DOCX + 1)
        if len(data) > MAX_DOCX:
            return {"ok": False, "message": tr("文件太大（最大 30 MB）")}
        try:
            s = await run_in_threadpool(citecheck.create_from_docx, registry, f.filename, data)
        except Exception as exc:  # noqa: BLE001 - a broken file is the user's to know about
            return {"ok": False, "message": tr("无法读取这个 Word 文件：") + str(exc)[:200]}
        return {"ok": True, "id": s["id"]}

    @app.get("/api/citecheck/{run_id}")
    def citecheck_status(run_id: int):
        try:
            s = citecheck.summary(registry, run_id)
        except ValueError:
            raise HTTPException(404) from None
        return {"ok": True, "status": s["status"], "done": s["done"], "total": s["total"], "counts": s["counts"],
                "message": s["message"]}

    @app.post("/api/citecheck/{run_id}/{action}")
    async def citecheck_action(run_id: int, action: str, request: Request):
        body = await json_body(request)
        if action == "start":
            if not config.model_ready():
                return {"ok": False, "message": tr("还没有连接模型，请先到设置里配置")}
            return {"ok": citecheck.start(pipeline, run_id)}
        if action == "stop":
            citecheck.stop(run_id)
            return {"ok": True}
        if action == "delete":
            citecheck.delete(registry, run_id)
            return {"ok": True}
        if action == "decide":
            registry.execute("UPDATE cite_items SET decided=? WHERE id=? AND run_id=?",
                             (1 if body.get("on", True) else 0, int(body.get("item", 0)), run_id))
            return {"ok": True}
        raise HTTPException(404)

    @app.get("/citecheck/{run_id}.md")
    def citecheck_report(run_id: int):
        try:
            text = citecheck.report_md(registry, run_id, {k: tr(v) for k, v in VERDICT_LABELS.items()})
        except ValueError:
            raise HTTPException(404) from None
        return Response(text, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="citation-check-{run_id}.md"'})

    # ------------------------------------------------------------ clippings notebook (收藏簿)
    def digest_html(text: str) -> str:
        import re as _re

        return _re.sub(r"\[C(\d+)\]", r'<a class="clip-ref" href="#clip-\1">C\1</a>', md_html(text))

    @app.get("/clips", response_class=HTMLResponse)
    def clips_page(request: Request):
        last = clips.last_digest(registry)
        return render(request, "clips.html", items=clips.listing(registry), digest=last,
                      digest_html=digest_html(last["text"]) if last else "", digests=clips.digests(registry),
                      new_n=clips.new_since_digest(registry), auto=clips.auto_enabled(config))

    @app.post("/api/clips")
    async def clips_add(request: Request):
        body = await json_body(request)
        pid = body.get("paper_id")
        page = body.get("page")
        try:
            cid = clips.add(registry, str(body.get("text", "")), int(pid) if str(pid or "").isdigit() else None,
                            int(page) if str(page or "").lstrip("-").isdigit() else None,
                            str(body.get("source") or "page"), str(body.get("url") or ""), str(body.get("note") or ""))
        except ValueError as exc:
            return {"ok": False, "message": tr(str(exc))}
        return {"ok": True, "id": cid}

    @app.post("/api/clips/{clip_id}/{action}")
    async def clips_action(clip_id: int, action: str, request: Request):
        body = await json_body(request)
        if action == "delete":
            clips.delete(registry, clip_id)
        elif action == "note":
            clips.set_note(registry, clip_id, str(body.get("note", "")))
        else:
            raise HTTPException(404)
        return {"ok": True}

    @app.post("/api/clips-digest")
    async def clips_digest(request: Request):
        body = await json_body(request)
        if body.get("auto") is not None:
            config.raw.setdefault("clips", {})["auto_digest"] = bool(body.get("auto"))
            config.save()
            return {"ok": True}
        try:
            d = await run_in_threadpool(clips.digest, config, registry, pipeline.llm, str(body.get("scope") or "all"))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": tr(str(exc))}
        return {"ok": True, "id": d["id"]}
