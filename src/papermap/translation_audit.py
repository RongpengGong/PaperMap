"""Check every saved full-text translation for omissions, locally and for free.

Translations made before the extractor fix (docs/全文翻译漏译修复方案.md) may skip whole sections.
The same check as the 「核对漏译」 button runs over the library in the background; papers with
suspected gaps are listed under 待审核 with a 补译 button. A result is reused until the translation
file, the PDF or the checker changes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading

from .registry import Registry, now_iso

log = logging.getLogger(__name__)
CHECKER = "coverage_v1"  # bump when translate.saved_translation_gaps changes meaningfully
SCHEMA = """
CREATE TABLE IF NOT EXISTS translation_audits (
    paper_id INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
    signature TEXT NOT NULL, pages_json TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL
);
"""
_RUN = threading.Lock()


def ensure_schema(registry: Registry) -> None:
    with registry._lock:  # noqa: SLF001
        registry.conn.executescript(SCHEMA)


def _signature(content: bytes, revision: str) -> str:
    return hashlib.sha256(content).hexdigest() + ":" + revision + ":" + CHECKER


def check_paper(pipeline, paper_id: int, force: bool = False) -> list[int] | None:
    """Pages that may be missing from this paper's translation; None when it has no translation."""
    from . import ocr, translate
    from .evidence import document_revision

    reg = pipeline.registry
    ensure_schema(reg)
    try:
        pdf, card = pipeline._paper_files(paper_id, before_card=True)  # noqa: SLF001
    except ValueError:
        return None
    path = card.with_suffix(".translation.json")
    if not path.exists() or not pdf.exists():
        reg.execute("DELETE FROM translation_audits WHERE paper_id=?", (paper_id,))
        return None
    content = path.read_bytes()
    revision = document_revision(pdf)
    sig = _signature(content, revision)
    old = reg.one("SELECT * FROM translation_audits WHERE paper_id=?", (paper_id,))
    if old and old["signature"] == sig and not force:
        return json.loads(old["pages_json"])
    pages: list[int] = []
    error = ""
    try:
        saved = json.loads(content)
        if saved.get("revision") and saved["revision"] != revision:
            error = "revision"  # the PDF changed since translating; the reader already says so
        else:
            pages = translate.saved_translation_gaps(pdf, saved["paragraphs"], ocr.cache_for(pdf, pipeline.config.data_dir))
    except Exception as exc:  # noqa: BLE001 - one odd file must not stop the sweep
        error = f"{type(exc).__name__}: {exc}"[:300]
    reg.execute("INSERT OR REPLACE INTO translation_audits(paper_id, signature, pages_json, error, checked_at) "
                "VALUES (?,?,?,?,?)", (paper_id, sig, json.dumps(pages), error, now_iso()))
    return pages


def audit_library(pipeline) -> int:
    """Background sweep at startup; returns how many translations have suspected gaps."""
    if not _RUN.acquire(blocking=False):
        return 0
    try:
        ensure_schema(pipeline.registry)
        flagged = 0
        for r in pipeline.registry.query("SELECT id FROM papers WHERE status='done' AND kb_path IS NOT NULL"):
            try:
                pages = check_paper(pipeline, r["id"])
            except Exception as exc:  # noqa: BLE001
                log.info("translation audit skipped paper %s: %s", r["id"], exc)
                continue
            flagged += bool(pages)
        return flagged
    finally:
        _RUN.release()


def flagged(registry: Registry) -> list[dict]:
    """Translations with suspected gaps, for 待审核 (papers in the trash are left out)."""
    if not registry.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='translation_audits'"):
        return []
    rows = registry.query("SELECT a.paper_id, a.pages_json, a.checked_at, p.title, p.first_author, p.year "
                          "FROM translation_audits a JOIN papers p ON p.id=a.paper_id "
                          "WHERE a.pages_json != '[]' AND p.status='done' ORDER BY a.checked_at DESC")
    return [{**dict(r), "pages": json.loads(r["pages_json"])} for r in rows]


def flagged_count(registry: Registry) -> int:
    return len(flagged(registry))
