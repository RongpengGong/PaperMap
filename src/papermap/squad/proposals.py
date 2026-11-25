"""The researcher's decision on a squad proposal. Accepting goes through the same code paths as doing
the thing by hand (evidence notebook, gaps, questions, tracking); nothing else writes to the library."""
from __future__ import annotations

import json

from .. import evidence
from ..registry import now_iso
from . import store

KINDS = {"evidence", "judgment", "gap", "question", "take_in", "skip", "collect"}


def _insert_candidate(registry, rec: dict) -> int:
    """An outside work becomes a tracking candidate, so taking it in uses the normal download/import."""
    from .. import tracking

    tracking.ensure_schema(registry)
    old = tracking.find_existing(registry, rec)
    if old is not None:
        return int(old["id"])
    now = now_iso()
    cur = registry.execute(
        "INSERT INTO track_candidates(doi, openalex_id, pmid, title, title_key, abstract, year, pub_date, journal, "
        "authors_json, is_preprint, oa_urls_json, landing, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec.get("doi"), rec.get("openalex_id"), rec.get("pmid"), rec.get("title") or "", tracking.title_key(rec.get("title") or ""),
         rec.get("abstract") or "", rec.get("year"), rec.get("pub_date"), rec.get("journal") or "",
         json.dumps(rec.get("authors") or [], ensure_ascii=False, default=str), int(bool(rec.get("is_preprint"))),
         json.dumps(rec.get("oa_urls") or []), rec.get("landing") or "", now, now))
    return int(cur.lastrowid)


def decide(pipeline, proposal_id: int, action: str, edits: dict | None = None) -> dict:
    reg = pipeline.registry
    row = reg.one("SELECT * FROM squad_proposals WHERE id=?", (proposal_id,))
    if row is None:
        raise ValueError("这条提议已不存在。")
    if row["status"] != "pending":
        raise ValueError("这条提议已经处理过了。")
    if action not in ("accept", "reject"):
        raise ValueError("操作无效")
    if action == "reject":
        reg.execute("UPDATE squad_proposals SET status='rejected', decided_at=? WHERE id=?", (now_iso(), proposal_id))
        return {"status": "rejected"}
    p = json.loads(row["payload_json"])
    edits = edits or {}
    kind = row["kind"]
    if kind == "evidence":
        stance = edits.get("stance") or p["stance"]
        dimension = edits.get("dimension") or p["dimension"]
        note = edits.get("note", p["note"])
        r = evidence.add_entry(reg, p["question_id"], "", p["paper_id"], p["page"], p["revision"], p["rects"],
                               p["quote"], "native", stance, note, dimension)
        result = {"entry_id": r["id"], "created": r["created"]}
    elif kind == "judgment":
        text = evidence.valid_text(edits.get("text") or p["readable"], "判断", 12000)
        cur = reg.execute("UPDATE evidence_questions SET judgment=?, judgment_at=? WHERE id=? AND status='active'",
                          (text, now_iso(), p["question_id"]))
        if not cur.rowcount:
            raise ValueError("这个研究问题已不存在。")
        result = {"question_id": p["question_id"]}
    elif kind == "gap":
        from .. import gaps

        gid = gaps.add_gap(reg, p["question_id"], edits.get("text") or p["text"], p.get("terms") or None,
                           origin="model" if p.get("terms") else "user")
        try:
            gaps.scan_library(reg, gid)
        except Exception:  # noqa: BLE001 - matching is a convenience
            pass
        result = {"gap_id": gid}
    elif kind == "question":
        qid = evidence.create_question(reg, edits.get("title") or p["title"])
        result = {"question_id": qid}
    elif kind == "take_in":
        from .. import tracking

        result = tracking.take_in(pipeline, p["candidate_id"])
    elif kind == "skip":
        from .. import tracking

        tracking.decide(reg, p["candidate_id"], "dismissed")
        result = {"skipped": True}
    elif kind == "collect":
        from .. import tracking

        rec = store.get_work(reg, row["run_id"], p["work_id"]) or p
        cid = _insert_candidate(reg, rec)
        result = {"candidate_id": cid, **tracking.take_in(pipeline, cid)}
    else:
        raise ValueError("未知的提议类型")
    reg.execute("UPDATE squad_proposals SET status='accepted', result_json=?, decided_at=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False, default=str), now_iso(), proposal_id))
    return {"status": "accepted", "result": result}
