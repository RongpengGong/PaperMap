"""Reuse immutable source anchors and review possible dataset overlap between studies."""
from __future__ import annotations

import hashlib
import json
from itertools import groupby

from . import evidence
from .registry import now_iso


# Repositories/identifiers that name datasets, series, studies or projects.
# A link to a code repository or the name of an analysis tool is not a dataset match.
DATASET_KINDS = ("geo", "sra", "ega", "arrayexpress", "cngb", "gsa", "dbgap", "pride", "tcga")
DECISIONS = {"same", "partial", "distinct", "citation", "uncertain"}


def valid_id(value):
    return type(value) is int and 0 < value < 2**63


def reuse_entry(registry, eid, body):
    qid = body.get("question_id", 0)
    if type(qid) is not int or not 0 <= qid < 2**63:
        raise ValueError("请选择研究问题。")
    title = evidence.valid_text(body.get("question_title"), "研究问题", 300) if not qid else None
    stance, note = evidence.valid_note(body.get("stance", "pending"), body.get("note", ""))
    dimension = evidence.valid_dimension(body.get("dimension", "general"))
    with registry.transaction() as db:
        source = db.execute(
            "SELECT e.* FROM evidence_entries e JOIN papers p ON p.id=e.paper_id "
            "WHERE e.id=? AND p.status!='ignored'", (eid,)).fetchone()
        if source is None:
            raise LookupError("未找到这条证据。")
        if not qid:
            db.execute("INSERT INTO evidence_questions(title,created_at) VALUES (?,?) ON CONFLICT(title) DO NOTHING",
                       (title, now_iso()))
            qid = db.execute("SELECT id FROM evidence_questions WHERE title=?", (title,)).fetchone()["id"]
        if qid == source["question_id"]:
            raise ValueError("请选择另一个研究问题。")
        if not db.execute("SELECT 1 FROM evidence_questions WHERE id=? AND status='active'", (qid,)).fetchone():
            raise ValueError("请选择已确认的研究问题。")
        # The source is immutable and shared by (paper_id, anchor_key). Its version,
        # rectangles, and saved region preview survive deletion of either question's use.
        columns = "paper_id,merged_from,page,revision,rects_json,quote,source,anchor_key,region_label"
        inserted = db.execute(
            f"INSERT INTO evidence_entries ({columns},question_id,stance,note,dimension,created_at) "
            f"SELECT {columns},?,?,?,?,? FROM evidence_entries WHERE id=? "
            "ON CONFLICT(question_id,paper_id,anchor_key) DO NOTHING",
            (qid, stance, note, dimension, now_iso(), eid)).rowcount
        dest = db.execute("SELECT id FROM evidence_entries WHERE question_id=? AND paper_id=? AND anchor_key=?",
                          (qid, source["paper_id"], source["anchor_key"])).fetchone()
        return {"id": dest["id"], "question_id": qid, "created": bool(inserted)}


def _resource_rows(db, qid, kind=None, accession=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='data_resources'").fetchone():
        return []
    params = [qid, *DATASET_KINDS]
    where = ""
    if kind is not None:
        where = " AND r.kind=? AND r.accession=?"
        params.extend([kind, accession])
    return db.execute(f"""
        SELECT r.kind,r.accession,r.label,r.context,r.page,r.paper_id,p.title,p.first_author,p.year
        FROM data_resources r JOIN papers p ON p.id=r.paper_id
        WHERE p.status!='ignored' AND r.paper_id IN (SELECT e.paper_id FROM evidence_entries e
               JOIN evidence_questions q ON q.id=e.question_id WHERE e.question_id=? AND q.status='active')
          AND r.kind IN ({','.join('?' for _ in DATASET_KINDS)}) {where}
        ORDER BY r.kind,r.accession,r.paper_id
    """, params).fetchall()


def _context_key(row):
    # Resource row IDs change on reindex; unchanged source text keeps a prior review valid.
    encoded = json.dumps([row["page"], row["context"]], ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def overlap_groups(registry, qid):
    with registry._lock:
        rows = _resource_rows(registry.conn, qid)
        decisions = registry.conn.execute("SELECT * FROM dataset_overlap_reviews WHERE question_id=? "
                                          "ORDER BY paper_a,paper_b", (qid,)).fetchall()
    by_dataset = {}
    for review in decisions:
        by_dataset.setdefault((review["kind"], review["accession"]), []).append(review)
    groups = []
    for key, matches in groupby(rows, key=lambda r: (r["kind"], r["accession"])):
        papers = {r["paper_id"]: {**dict(r), "context_key": _context_key(r)} for r in matches}
        if len(papers) < 2:
            continue
        reviews = []
        for saved in by_dataset.get(key, ()):
            a, b = papers.get(saved["paper_a"]), papers.get(saved["paper_b"])
            if a and b:
                reviews.append({**dict(saved), "stale": saved["context_key"] != a["context_key"] + b["context_key"]})
        groups.append({"kind": key[0], "accession": key[1], "papers": list(papers.values()), "reviews": reviews})
    return groups


def save_overlap(registry, qid, body):
    a, b = body.get("paper_a"), body.get("paper_b")
    kind, accession, decision, note = (body.get(k) for k in ("kind", "accession", "decision", "note"))
    if not valid_id(a) or not valid_id(b) or a == b:
        raise ValueError("请选择两篇不同的论文。")
    if not isinstance(kind, str) or kind not in DATASET_KINDS or not isinstance(accession, str) or len(accession) > 160:
        raise ValueError("数据登录号无效。")
    if not isinstance(decision, str) or decision not in DECISIONS:
        raise ValueError("请选择核对结论。")
    note = evidence.valid_text("" if note is None else note, "笔记", 12000, required=False)
    a, b = sorted((a, b))
    with registry.transaction() as db:
        papers = {r["paper_id"]: r for r in _resource_rows(db, qid, kind, accession)}
        if a not in papers or b not in papers:
            raise ValueError("这两篇论文已不再共同关联此登录号。")
        key = _context_key(papers[a]) + _context_key(papers[b])
        if body.get("context_key") != key:
            raise ValueError("原文上下文已更新，请刷新后重新核对。")
        existing = db.execute("SELECT decision,note,context_key FROM dataset_overlap_reviews "
                              "WHERE question_id=? AND kind=? AND accession=? AND paper_a=? AND paper_b=?",
                              (qid, kind, accession, a, b)).fetchone()
        previous = dict(existing) if existing else None
        current = {"decision": decision, "note": note, "context_key": key}
        if previous == current:
            return
        if previous != body.get("previous"):
            raise ValueError("这个问题已在别处更新，请保留你的草稿，刷新后再保存。")
        db.execute("""
            INSERT INTO dataset_overlap_reviews(question_id,kind,accession,paper_a,paper_b,context_key,decision,note,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(question_id,kind,accession,paper_a,paper_b)
            DO UPDATE SET context_key=excluded.context_key,decision=excluded.decision,note=excluded.note,updated_at=excluded.updated_at
        """, (qid, kind, accession, a, b, key, decision, note, now_iso()))
