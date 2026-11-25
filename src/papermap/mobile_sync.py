"""Exchange a selected main PDF, revision-bound excerpts, and a desktop reader bookmark.

Pages are zero-based. PDF coordinates use the displayed page's top-left origin.
A note ID names one immutable append operation; its durable receipt survives deletion.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from contextlib import contextmanager
from itertools import groupby
from pathlib import Path

import pymupdf

from . import evidence, library, mobile_companion, mobile_marks, ocr, versions
from .registry import now_iso


class RevisionConflict(ValueError):
    """The phone is addressing a PDF revision other than the shared one."""


def library_id(registry) -> str:
    with registry.transaction() as db:
        row = db.execute("SELECT library_id FROM mobile_library WHERE singleton=1").fetchone()
        if row is not None:
            return row["library_id"]
        value = str(uuid.uuid4())
        db.execute("INSERT INTO mobile_library(singleton,library_id) VALUES(1,?)", (value,))
        return value


def _paper(registry, paper_id):
    if type(paper_id) is not int or not 0 < paper_id < 2**63:
        raise LookupError("论文不存在。")
    row = registry.get_paper(paper_id)
    if row is None or row["status"] == "ignored":
        raise LookupError("这篇论文已移出文献库。")
    return row


def _main_path(config, registry, row):
    # Match the version selected by the desktop reader; supplements have a separate identity.
    preferred = versions.preferred(config.knowledge_dir, registry, row["id"])
    if preferred and preferred[0].is_file():
        return preferred[0]
    if row["kb_path"] and row["kb_path"].count("/") >= 2:
        path = library.pdf_path(config.knowledge_dir, row["kb_path"])
        if path.is_file():
            return path
    for filename in registry.paper_file_paths(row["id"]):
        path = Path(filename)
        if path.is_file():
            return path
    raise LookupError("找不到正文 PDF。")


def pdf_path(config, registry, paper_id, revision) -> Path:
    if not isinstance(revision, str) or not re.fullmatch("[a-f0-9]{64}", revision):
        raise ValueError("PDF 修订标识无效。")
    path = _main_path(config, registry, _paper(registry, paper_id))
    if evidence.document_revision(path) != revision:
        raise RevisionConflict("电脑端正文 PDF 已更换版本。")
    return path


@contextmanager
def _document(path):
    try:
        doc = pymupdf.open(path)
    except (pymupdf.FileDataError, RuntimeError, OSError) as exc:
        raise ValueError("无法读取正文 PDF。") from exc
    with doc:
        if not doc.is_pdf or doc.needs_pass or not doc.page_count:
            raise ValueError("无法读取正文 PDF。")
        yield doc


_PAGE_COUNTS: dict[str, int] = {}  # PDF revision -> pages; the phone's catalog asks every minute


def _page_count(path, revision) -> int:
    count = _PAGE_COUNTS.get(revision)
    if count is None:
        with _document(path) as doc:
            count = doc.page_count
        if len(_PAGE_COUNTS) > 4096:
            _PAGE_COUNTS.clear()
        _PAGE_COUNTS[revision] = count
    return count


# Papers the phone can see: processed papers, and papers still being processed that already have a card.
_SHARED_SQL = """SELECT id FROM papers WHERE status != 'ignored' AND (status = 'done'
    OR (status IN ('new','extracting','error') AND kb_path IS NOT NULL)) ORDER BY id"""


class LibraryShare:
    """The whole library as the phone sees it: every paper with a readable PDF, at its current revision.

    Behaves like the ``{paper id: revision}`` map the listener checks requests against, but is worked
    out on each request, so papers imported later appear by themselves and deleted ones disappear."""

    def __init__(self, config, registry):
        self.config = config
        self.registry = registry

    def get(self, paper_id, default=None):
        try:
            row = _paper(self.registry, paper_id)
            if row["status"] != "done" and not (row["status"] in ("new", "extracting", "error") and row["kb_path"]):
                return default
            return evidence.document_revision(_main_path(self.config, self.registry, row))
        except (LookupError, OSError):
            return default

    def __contains__(self, paper_id):
        return self.get(paper_id) is not None

    def __getitem__(self, paper_id):
        revision = self.get(paper_id)
        if revision is None:
            raise KeyError(paper_id)
        return revision

    def __setitem__(self, paper_id, revision):
        pass  # A paper the phone imported is part of the library, so it is shared already.

    def __len__(self):
        return len(self.registry.query(_SHARED_SQL))

    def copy(self) -> dict:
        out = {}
        for row in self.registry.query(_SHARED_SQL):
            revision = self.get(row["id"])
            if revision is not None:
                out[row["id"]] = revision
        return out


def _check_page(page, count):
    if type(page) is not int or not 0 <= page < count:
        raise ValueError("页码超出正文 PDF 的范围。")


def _progress(row, count):
    return {"page": min(count - 1, max(0, row["reading_page"] or 0)),
            "offset": row["reading_offset"] or 0, "version": row["reading_at"] or 0}


def _notes(db, paper_id, revision):
    rows = db.execute("""
        SELECT e.id,e.page,e.quote,e.note,q.title,r.client_id,r.segment_id
        FROM evidence_entries e JOIN evidence_questions q ON q.id=e.question_id
        LEFT JOIN mobile_note_receipts r
          ON r.paper_id=e.paper_id AND r.revision=e.revision AND r.evidence_id=e.id
        WHERE e.paper_id=? AND e.revision=? AND e.source IN ('native','ocr') ORDER BY e.id
    """, (paper_id, revision))
    return [{"id": r["client_id"] or "desktop:" + str(r["id"]), "page": r["page"],
             "quote": r["quote"], "note": r["note"], "question": r["title"],
             "segmentId": r["segment_id"] or ""} for r in rows]


def capture(config, registry, paper_id, marks=False) -> dict:
    """The phone's view of a shared paper. ``marks`` adds its highlights (left out of the quick catalog listing)."""
    row = _paper(registry, paper_id)
    path = _main_path(config, registry, row)
    revision = evidence.document_revision(path)
    count = _page_count(path, revision)
    size = path.stat().st_size
    pdf_path(config, registry, paper_id, revision)
    where = mobile_marks.scope(config, registry, paper_id, revision) if marks else None
    with registry.transaction() as db:
        row = _paper(registry, paper_id)
        return {"id": paper_id, "title": row["title"] or "PDF", "revision": revision,
                "pageCount": count, "byteSize": size, "progress": _progress(row, count),
                "notes": _notes(db, paper_id, revision),
                **(mobile_marks.listing(db, paper_id, where) if marks else {}),
                "companionVersion": mobile_companion.version(config, row, revision)}


def read_companion(config, registry, paper_id, revision):
    path = pdf_path(config, registry, paper_id, revision)
    with _document(path) as doc:
        count = doc.page_count
    result = mobile_companion.read(config, _paper(registry, paper_id), revision, path, count)
    pdf_path(config, registry, paper_id, revision)
    return result


def _source_segments(config, revision, page):
    # Reuse existing Vision data, so opening the phone's library does not start a new OCR job.
    cache = config.data_dir / "cache" / "ocr" / ocr.CACHE_VERSION / revision
    cached = ocr.page_words(page, cache, automatic=False)
    if cached:
        blocks = [[list(group)] for _, group in groupby(cached, key=lambda w: w[5])]
        source = "ocr"
    else:
        words = []
        for x0, y0, x1, y1, text, block, line, _ in page.get_text("words"):
            rect = pymupdf.Rect(x0, y0, x1, y1) * page.rotation_matrix
            words.append([*rect, text, block, line])
        blocks = [[list(line) for _, line in groupby(block, key=lambda w: w[6])]
                  for _, block in groupby(words, key=lambda w: w[5])]
        source = "native"

    segments = []
    for block in blocks:
        lines, rects = [], []
        for words in block:
            bounds = pymupdf.Rect(words[0][:4])
            pieces = [words[0][4]]
            for word in words[1:]:
                bounds |= pymupdf.Rect(word[:4])
                pieces.extend((ocr.word_separator(pieces[-1], word[4]), word[4]))
            bounds &= page.rect
            if bounds.is_empty:
                continue
            line = "".join(pieces).strip()
            if line:
                lines.append(line)
                rects.append([round(v, 3) for v in bounds])
        if not lines:
            continue
        text = "\n".join(lines)
        if len(text) > 30000 or len(rects) > 1200:
            # Keep large unstructured blocks selectable line by line within desktop note limits.
            chunks = [(line, [rect]) for line, rect in zip(lines, rects) if len(line) <= 30000]
        else:
            chunks = [(text, rects)]
        for text, rects in chunks:
            raw = json.dumps([source, page.number, text, rects], ensure_ascii=False, separators=(",", ":"))
            segments.append({"id": hashlib.sha256(raw.encode()).hexdigest(), "text": text,
                             "source": source, "rects": rects})
    return segments


def read_page(config, registry, paper_id, revision, page) -> dict:
    path = pdf_path(config, registry, paper_id, revision)
    with _document(path) as doc:
        _check_page(page, doc.page_count)
        segments = _source_segments(config, revision, doc[page])
    if evidence.document_revision(path) != revision:
        raise RevisionConflict("电脑端正文 PDF 已更换版本。")
    return {"page": page, "segments": [{"id": seg["id"], "text": seg["text"]} for seg in segments]}


def _changes(changes, count):
    if not isinstance(changes, dict) or not isinstance(changes.get("notes", []), list):
        raise ValueError("同步内容无效。")
    notes = {}
    for item in changes.get("notes", []):
        if not isinstance(item, dict):
            raise ValueError("摘录格式无效。")
        key, page, segment = (item.get(k) for k in ("id", "page", "segmentId"))
        if not isinstance(key, str) or not re.fullmatch(
                r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", key):
            raise ValueError("摘录 ID 必须是 UUID。")
        _check_page(page, count)
        if not isinstance(segment, str) or not re.fullmatch("[0-9a-f]{64}", segment):
            raise ValueError("原文段落标识无效。")
        note = evidence.valid_text(item.get("note", ""), "笔记", 12000, required=False)
        signature = hashlib.sha256(json.dumps([page, segment, note], ensure_ascii=False).encode()).hexdigest()
        if key in notes and notes[key]["signature"] != signature:
            raise ValueError("同一个摘录 ID 对应了不同内容。")
        notes[key] = {"page": page, "segment": segment, "note": note, "signature": signature}
    progress = changes.get("progress")
    if progress is not None:
        if not isinstance(progress, dict):
            raise ValueError("阅读进度格式无效。")
        _check_page(progress.get("page"), count)
        base, offset = progress.get("baseVersion"), progress.get("offset")
        if type(base) is not int or not 0 <= base < 2**53:
            raise ValueError("阅读进度版本无效。")
        if type(offset) not in (int, float) or not 0 <= offset <= 1 or not math.isfinite(offset):
            raise ValueError("页内位置必须在 0 和 1 之间。")
    return notes, progress


def _receipt(db, paper_id, revision, key, signature):
    row = db.execute("SELECT signature FROM mobile_note_receipts WHERE paper_id=? AND revision=? AND client_id=?",
                     (paper_id, revision, key)).fetchone()
    if row and row["signature"] != signature:
        raise ValueError("同一个摘录 ID 对应了不同内容。")
    return row is not None


def _store_notes(db, paper_id, revision, notes, segments):
    qid = None
    for key, item in notes.items():
        if _receipt(db, paper_id, revision, key, item["signature"]):
            continue
        if qid is None:
            title = "移动端摘录"
            db.execute("INSERT INTO evidence_questions(title,created_at) VALUES (?,?) "
                       "ON CONFLICT(title) DO UPDATE SET status='active'", (title, now_iso()))
            qid = db.execute("SELECT id FROM evidence_questions WHERE title=?", (title,)).fetchone()[0]
        seg = segments[item["page"]][item["segment"]]
        eid = db.execute("""
            INSERT INTO evidence_entries(question_id,paper_id,page,revision,rects_json,quote,source,
                                         stance,note,anchor_key,created_at)
            VALUES (?,?,?,?,?,?,?,'pending',?,?,?)
        """, (qid, paper_id, item["page"], revision, json.dumps(seg["rects"]), seg["text"], seg["source"],
              item["note"], f"mobile:{revision}:{key}", now_iso())).lastrowid
        db.execute("INSERT INTO mobile_note_receipts(paper_id,revision,client_id,signature,segment_id,evidence_id) "
                   "VALUES(?,?,?,?,?,?)", (paper_id, revision, key, item["signature"], item["segment"], eid))


def apply_changes(config, registry, paper_id, revision, changes) -> dict:
    path = pdf_path(config, registry, paper_id, revision)
    segments = {}
    with _document(path) as doc:
        count = doc.page_count
        notes, requested = _changes(changes, count)
        mark_upserts, mark_deletes = mobile_marks.parse(changes, count)
        for key, item in notes.items():
            with registry._lock:
                if _receipt(registry.conn, paper_id, revision, key, item["signature"]):
                    continue
            page = item["page"]
            if page not in segments:
                segments[page] = {seg["id"]: seg for seg in _source_segments(config, revision, doc[page])}
            if item["segment"] not in segments[page]:
                raise ValueError("所选原文段落已变化，请重新核对摘录。")

    pdf_path(config, registry, paper_id, revision)
    where = mobile_marks.scope(config, registry, paper_id, revision)
    with registry.transaction() as db:
        progress = _progress(_paper(registry, paper_id), count)
        _store_notes(db, paper_id, revision, notes, segments)
        marked = mobile_marks.apply(db, paper_id, where, mark_upserts, mark_deletes)
        conflict = False
        if requested is not None:
            page, offset = requested["page"], requested["offset"]
            same_position = (page, offset) == (progress["page"], progress["offset"])
            if progress["version"] != requested["baseVersion"]:
                conflict = not same_position
            elif not same_position or not progress["version"]:
                version = max(time.time_ns() // 1000, progress["version"] + 1)
                db.execute("""UPDATE papers SET reading_page=?,reading_offset=?,reading_at=?,
                              read_status=CASE read_status WHEN 'unread' THEN 'reading' ELSE read_status END
                              WHERE id=?""", (page, offset, version, paper_id))
                progress = {"page": page, "offset": offset, "version": version}
        result = {"progress": progress, "progressConflict": conflict,
                  "acknowledgedNoteIds": list(notes), "notes": _notes(db, paper_id, revision),
                  "acknowledgedMarkIds": marked, **mobile_marks.listing(db, paper_id, where)}
    if marked:
        try:  # keep the annotations Markdown beside the card current; the database already holds the marks
            from . import reader as reader_mod
            reader_mod.write_annotations_md(registry, config.knowledge_dir, paper_id, config.output_language)
        except OSError:
            pass
    return result
