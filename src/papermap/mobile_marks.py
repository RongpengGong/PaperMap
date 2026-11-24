"""Highlights and underlines shared both ways with a paired phone.

Each mark has a stable ``uid``. Rectangles are PDF points from the displayed page's top-left corner,
the same space the desktop reader stores. A mark the phone has never sent is added; a mark both sides
know keeps the computer's colour and style (the computer is the primary copy). A deletion on either side
leaves a tombstone, so the other side removes its copy instead of sending it back.
"""
from __future__ import annotations

import json
import re
import uuid

from . import reader as reader_mod, versions
from .registry import now_iso

STYLES = ("highlight", "underline")
_UID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
MAX_MARKS = 2000


def scope(config, registry, paper_id, revision):
    """The PDF file the shared revision belongs to, and the paper's original file, as the reader filters them."""
    file_id = versions.find_revision(config.knowledge_dir, registry, paper_id, revision)
    original = min((v["file_id"] for v in versions.versions(config.knowledge_dir, registry, paper_id)), default=None)
    return file_id, original


def _belongs(row, file_id, original) -> bool:
    return (row["pdf_file"] or original) == (file_id or original)


def _row(row) -> dict:
    return {"id": row["uid"], "page": row["page"], "style": row["style"] or "highlight", "color": row["color"],
            "text": row["text"] or "", "rects": json.loads(row["rects_json"])}


def listing(db, paper_id, where) -> dict:
    """This revision's marks (giving older desktop highlights a uid on first use) and the paper's tombstones.

    ``where`` comes from scope(), computed before the transaction: finding the version may hash PDF files.
    """
    file_id, original = where
    marks = []
    for row in db.execute("SELECT * FROM annotations WHERE paper_id=? ORDER BY page, id", (paper_id,)).fetchall():
        if not _belongs(row, file_id, original):
            continue
        if not row["uid"]:
            db.execute("UPDATE annotations SET uid=? WHERE id=? AND uid IS NULL", (str(uuid.uuid4()), row["id"]))
            row = db.execute("SELECT * FROM annotations WHERE id=?", (row["id"],)).fetchone()
        marks.append(_row(row))
    gone = [r["uid"] for r in db.execute("SELECT uid FROM annotation_tombstones WHERE paper_id=?", (paper_id,))]
    return {"marks": marks, "markTombstones": gone}


def parse(changes, page_count) -> tuple[list[dict], list[str]]:
    """Validated upserts and deletions from the phone; anything malformed rejects the whole request."""
    block = changes.get("marks") if isinstance(changes, dict) else None
    if block is None:
        return [], []
    if not isinstance(block, dict):
        raise ValueError("标注格式无效。")
    upserts, deletes = block.get("upserts", []), block.get("deletes", [])
    if not isinstance(upserts, list) or not isinstance(deletes, list) or len(upserts) + len(deletes) > MAX_MARKS:
        raise ValueError("标注格式无效。")
    clean = []
    for item in upserts:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not _UID.fullmatch(item["id"]):
            raise ValueError("标注 ID 必须是 UUID。")
        page = item.get("page")
        if type(page) is not int or not 0 <= page < page_count:
            raise ValueError("标注页码超出范围。")
        style = item.get("style")
        color = item.get("color")
        if style not in STYLES or color not in reader_mod.COLORS:
            raise ValueError("标注样式无效。")
        rects = reader_mod.clean_rects(item.get("rects"))
        if not rects or any(r[0] > r[2] or r[1] > r[3] for r in rects):
            raise ValueError("标注位置无效。")
        text = item.get("text", "")
        if not isinstance(text, str):
            raise ValueError("标注文字无效。")
        clean.append({"id": item["id"].lower(), "page": page, "style": style, "color": color, "rects": rects,
                      "text": " ".join(text.split())[:4000]})
    for uid in deletes:
        if not isinstance(uid, str) or not _UID.fullmatch(uid):
            raise ValueError("标注 ID 必须是 UUID。")
    return clean, [u.lower() for u in deletes]


def apply(db, paper_id, where, upserts, deletes) -> list[str]:
    """Store the phone's new marks and deletions inside the caller's transaction; returns acknowledged IDs."""
    if not upserts and not deletes:
        return []
    file_id, _ = where
    acknowledged = []
    for uid in deletes:
        db.execute("DELETE FROM annotations WHERE paper_id=? AND uid=?", (paper_id, uid))
        db.execute("INSERT OR REPLACE INTO annotation_tombstones(uid, paper_id, deleted_at) VALUES (?,?,?)",
                   (uid, paper_id, now_iso()))
        acknowledged.append(uid)
    for m in upserts:
        acknowledged.append(m["id"])
        if db.execute("SELECT 1 FROM annotation_tombstones WHERE uid=?", (m["id"],)).fetchone():
            continue  # deleted on the computer: the phone drops its copy from the reply
        if db.execute("SELECT 1 FROM annotations WHERE uid=?", (m["id"],)).fetchone():
            continue  # already known: the computer's colour and style stay
        ts = now_iso()
        db.execute("INSERT INTO annotations(paper_id, page, rects_json, text, color, note, created_at, updated_at, "
                   "pdf_file, uid, style) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (paper_id, m["page"], json.dumps(m["rects"]), m["text"], m["color"], "", ts, ts, file_id,
                    m["id"], m["style"]))
    return acknowledged
