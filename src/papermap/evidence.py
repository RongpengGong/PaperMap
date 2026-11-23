"""Research questions and quotations anchored to a specific PDF revision."""
from __future__ import annotations

import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode

from .registry import Registry, now_iso

STANCES = {"pending": "待核对", "supports": "支持", "opposes": "相反"}
SOURCES = {"native": "PDF 原文", "ocr": "本机 OCR"}


@lru_cache(maxsize=64)
def _hash_revision(path, device, inode, size, mtime, ctime):
    from .pdf_extract import sha256_file

    return sha256_file(path)


def document_revision(path: Path) -> str:
    st = path.stat()
    return _hash_revision(str(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def valid_text(value, name, limit, required=True):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{name}最多可输入 {limit} 个字符。")
    if required and not value.strip():
        raise ValueError(f"请填写{name}。")
    return value.strip()


def validate_rects(rects, width, height):
    if not isinstance(rects, list) or not 0 < len(rects) <= 1200:
        raise ValueError("请选择同一页上的一段原文。")
    clean = []
    for r in rects:
        if (not isinstance(r, list) or len(r) != 4 or
                any(type(x) not in (int, float) or abs(x) > 1e10 or not math.isfinite(x) for x in r)):
            raise ValueError("摘录位置无效，请重新选择原文。")
        # Browser font boxes may extend just beyond the page edge.
        x0, y0, x1, y1 = (round(max(0, min(at, bound)), 3) for at, bound in zip(r, (width, height, width, height)))
        if x0 >= x1 or y0 >= y1:
            raise ValueError("摘录位置在页面之外，请重新选择原文。")
        clean.append([x0, y0, x1, y1])
    return clean


def questions(registry):
    return registry.query("""
        SELECT q.id,q.title,COUNT(e.id) AS total FROM evidence_questions q
        LEFT JOIN evidence_entries e ON e.question_id=q.id WHERE q.status='active' GROUP BY q.id ORDER BY q.id DESC
    """)


def create_question(registry, title):
    title = valid_text(title, "研究问题", 300)
    registry.execute("INSERT INTO evidence_questions(title,created_at) VALUES (?,?) "
                     "ON CONFLICT(title) DO UPDATE SET status='active'", (title, now_iso()))
    return registry.one("SELECT id FROM evidence_questions WHERE title=?", (title,))["id"]


def valid_note(stance, note):
    if not isinstance(stance, str) or stance not in STANCES:
        raise ValueError("请选择摘录与问题的关系。")
    return stance, valid_text(note, "笔记", 12000, required=False)


def add_entry(registry, question_id, title, paper_id, page, revision, rects, quote, source, stance, note):
    quote = valid_text(quote, "摘录", 30000)
    if source not in ("native", "ocr"):
        raise ValueError("摘录来源无效。")
    stance, note = valid_note(stance, note)
    if type(question_id) is not int or question_id < 0:
        raise ValueError("请选择研究问题。")
    if not question_id:
        title = valid_text(title, "研究问题", 300)
    coords = json.dumps(rects, separators=(",", ":"))
    anchor = hashlib.sha256(json.dumps([revision, page, coords, quote], ensure_ascii=False).encode()).hexdigest()
    # A retry of the same selection cannot overwrite a note that was edited later.
    with registry._lock, registry.conn:
        db = registry.conn
        if not question_id:
            db.execute("INSERT INTO evidence_questions(title,created_at) VALUES (?,?) "
                       "ON CONFLICT(title) DO UPDATE SET status='active'", (title, now_iso()))
            question_id = db.execute("SELECT id FROM evidence_questions WHERE title=?", (title,)).fetchone()["id"]
        elif not db.execute("SELECT 1 FROM evidence_questions WHERE id=?", (question_id,)).fetchone():
            raise ValueError("这个研究问题已不存在。")
        else:
            db.execute("UPDATE evidence_questions SET status='active' WHERE id=?", (question_id,))
        inserted = db.execute("""
            INSERT INTO evidence_entries
            (question_id,paper_id,page,revision,rects_json,quote,source,stance,note,anchor_key,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(question_id,paper_id,anchor_key) DO NOTHING
        """, (question_id, paper_id, page, revision, coords, quote, source, stance, note, anchor, now_iso())).rowcount
        eid = db.execute("SELECT id FROM evidence_entries WHERE question_id=? AND paper_id=? AND anchor_key=?",
                         (question_id, paper_id, anchor)).fetchone()["id"]
    return {"id": eid, "question_id": question_id, "created": bool(inserted)}


def get_entries(registry, question_id):
    return registry.query("""
        SELECT e.*,p.title,p.first_author,p.year,p.doi FROM evidence_entries e
        JOIN papers p ON p.id=e.paper_id WHERE e.question_id=? ORDER BY e.id DESC
    """, (question_id,))


def anchor_path(entry):
    return f"/reader/{entry['paper_id']}?" + urlencode({"evidence": entry["id"], "page": entry["page"] + 1})


def export_markdown(title, entries, base_url):
    lines = ["# " + title.replace("\n", " "), ""]
    for entry in entries:
        who = " ".join(str(item) for item in (entry["first_author"], entry["year"]) if item)
        lines.extend([f"## {STANCES[entry['stance']]} · {entry['title'] or '未命名论文'}", ""])
        lines.extend("> " + t for t in entry["quote"].splitlines())
        lines.extend(["", f"{who} · 第 {entry['page'] + 1} 页 · {SOURCES[entry['source']]}",
                      f"[定位原文]({base_url.rstrip('/')}{anchor_path(entry)})"])
        if entry["doi"]:
            lines.append("DOI " + entry["doi"])
        if entry["note"]:
            lines.extend(["", entry["note"]])
        lines.append("")
    return "\n".join(lines)
