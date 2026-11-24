"""Question-oriented evidence notebooks and revision-checked reader anchors."""
from __future__ import annotations

import json
import secrets

import pymupdf
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .. import evidence
from .. import questions as qmod
from .. import updates as upd


def mount_evidence(app, config, registry, render, csrf_token, reader_pdf):
    def check(body):
        csrf = body.get("csrf")
        if not isinstance(csrf, str) or not secrets.compare_digest(csrf.encode(), csrf_token.encode()):
            raise HTTPException(403, "请刷新页面后重试。")

    def problem(message, status=400):
        return JSONResponse({"ok": False, "message": str(message)}, status_code=status)

    def get_question(qid):
        row = registry.one("SELECT * FROM evidence_questions WHERE id=?", (qid,)) if 0 < qid < 2**63 else None
        if not row:
            raise HTTPException(404, "未找到这个研究问题。")
        return row

    @app.get("/evidence", response_class=HTMLResponse)
    def notebook(request: Request, question: int = 0):
        topics = evidence.questions(registry)
        selected = get_question(question) if question else (topics[0] if topics else None)
        items = evidence.get_entries(registry, selected["id"]) if selected else []
        linked = qmod.question_fields(registry, selected["id"]) if selected else []
        all_fields = registry.query("SELECT id, slug, name_zh, axis FROM fields WHERE status='active' "
                                    "ORDER BY axis, sort_order, name_zh")
        return render(request, "evidence.html", questions=topics, selected=selected, entries=items,
                      linked_fields=linked, all_fields=all_fields,
                      update_cards=upd.for_question(registry, selected["id"]) if selected else [],
                      update_outcomes=upd.OUTCOMES,
                      stances=evidence.STANCES, sources=evidence.SOURCES, anchor_path=evidence.anchor_path)

    @app.get("/evidence/{qid}.md")
    def export(request: Request, qid: int):
        topic = get_question(qid)
        text = evidence.export_markdown(topic["title"], evidence.get_entries(registry, qid), str(request.base_url))
        return Response(text, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="papermap-evidence-{qid}.md"'})

    @app.get("/api/evidence/questions")
    def question_list():
        return {"ok": True, "questions": [dict(q) for q in evidence.questions(registry)]}

    @app.post("/api/evidence/questions")
    def new_question(body: dict):
        check(body)
        try:
            return {"ok": True, "id": evidence.create_question(registry, body.get("title"))}
        except ValueError as exc:
            return problem(exc)

    @app.post("/api/evidence/questions/{qid}/fields")
    def question_field(qid: int, body: dict):
        check(body)
        get_question(qid)
        fid = body.get("field_id")
        if type(fid) is not int:
            return problem("请选择学科。")
        try:
            qmod.link_field(registry, qid, fid, bool(body.get("on", True)))
        except ValueError as exc:
            return problem(exc)
        return {"ok": True, "fields": qmod.question_fields(registry, qid)}

    @app.post("/api/evidence/questions/{qid}/status")
    def question_status(qid: int, body: dict):
        check(body)
        get_question(qid)
        try:
            qmod.set_status(registry, qid, str(body.get("status", "")), body.get("title"))
        except ValueError as exc:
            return problem(exc)
        return {"ok": True}

    @app.post("/api/reader/{pid}/evidence")
    def add(pid: int, body: dict):
        check(body)
        if not 0 < pid < 2**63:
            raise HTTPException(404)
        path = reader_pdf(pid)
        revision = evidence.document_revision(path)
        if body.get("revision") != revision:
            return problem("PDF 版本已更换，请重新打开论文并选取原文。", 409)
        page, qid = body.get("page"), body.get("question_id", 0)
        if type(qid) is not int or not 0 <= qid < 2**63:
            return problem("请选择研究问题。")
        try:
            with pymupdf.open(path) as doc:
                if type(page) is not int or not 0 <= page < len(doc):
                    return problem("PDF 页码无效。")
                bounds = doc[page].rect
            rects = evidence.validate_rects(body.get("rects"), bounds.width, bounds.height)
            result = evidence.add_entry(registry, qid, body.get("question_title"), pid, page, revision, rects,
                                        body.get("quote"), body.get("source"), body.get("stance", "pending"),
                                        body.get("note", ""))
            return {"ok": True, **result}
        except ValueError as exc:
            return problem(exc)

    @app.get("/api/reader/{pid}/evidence/{eid}")
    def anchor(pid: int, eid: int):
        if not 0 < eid < 2**63 or not 0 < pid < 2**63:
            raise HTTPException(404)
        row = registry.one("SELECT * FROM evidence_entries WHERE id=? AND paper_id=?", (eid, pid))
        if not row:
            raise HTTPException(404, "未找到这条摘录，请在证据笔记中查看。")
        if evidence.document_revision(reader_pdf(pid)) != row["revision"]:
            raise HTTPException(409, "PDF 版本已更换，请对照笔记重新核对摘录位置。")
        return {"ok": True, "id": eid, "question_id": row["question_id"], "page": row["page"],
                "rects": json.loads(row["rects_json"]), "quote": row["quote"], "source": row["source"]}

    @app.get("/api/reader/{pid}/update/{cid}/{k}")
    def update_anchor(pid: int, cid: int, k: int):
        if not 0 < cid < 2**63 or not 0 < pid < 2**63:
            raise HTTPException(404)
        row = upd.anchor(registry, cid, pid, k)
        if row is None:
            raise HTTPException(404, "未找到这句原文，请回到论文的「变化」标签查看。")
        if evidence.document_revision(reader_pdf(pid)) != row["revision"]:
            raise HTTPException(409, "PDF 版本已更换，请重新生成这张更新卡。")
        return {"ok": True, "id": f"u{cid}-{k}", "question_id": row["question_id"], "page": row["page"],
                "rects": row["rects"], "quote": row["quote"], "source": "native", "back": f"/?id={pid}"}

    @app.post("/api/evidence/entries/{eid}/{action}")
    def edit_entry(eid: int, action: str, body: dict):
        check(body)
        if not 0 < eid < 2**63:
            raise HTTPException(404)
        if action == "delete":
            # A repeated delete after a lost response has the same outcome.
            registry.execute("DELETE FROM evidence_entries WHERE id=?", (eid,))
        elif action == "update":
            try:
                stance, note = evidence.valid_note(body.get("stance"), body.get("note"))
            except ValueError as exc:
                return problem(exc)
            if not registry.execute("UPDATE evidence_entries SET stance=?,note=? WHERE id=?", (stance, note, eid)).rowcount:
                raise HTTPException(404, "未找到这条证据。")
        else:
            raise HTTPException(404)
        return {"ok": True}
