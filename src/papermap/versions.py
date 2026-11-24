"""Several versions of one work (preprint and published), kept side by side (CLAUDE.md §16.3).

When the published version of a preprint arrives, its PDF joins the paper's folder next to the
preprint, which is never renamed or changed: excerpts and update cards are anchored to the exact
file they were taken from, so they keep opening on the original page. The reader shows the
published version by default and can switch. Every passage the user quoted (evidence excerpts)
or highlighted (annotations) on the old version is looked up in the new one: the same words on
another page, changed wording, or gone. Changed and missing passages are put to the user to check;
nothing is rewritten automatically.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pymupdf
from rapidfuzz import fuzz

from . import library, supplements
from .registry import Registry, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS pdf_versions (
    file_id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL,
    kind TEXT NOT NULL,          -- preprint / published
    doi TEXT,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS version_checks (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL,
    item TEXT NOT NULL,          -- evidence / annotation / abstract
    ref_id INTEGER NOT NULL,     -- evidence_entries.id / annotations.id / 0
    old_file INTEGER, new_file INTEGER,
    old_page INTEGER, old_text TEXT NOT NULL,
    status TEXT NOT NULL,        -- same / moved / changed / missing
    new_page INTEGER, new_text TEXT NOT NULL DEFAULT '',
    score REAL NOT NULL DEFAULT 0,
    decided TEXT,                -- NULL until the user has looked at it
    created_at TEXT NOT NULL,
    UNIQUE(paper_id, item, ref_id, new_file)
);
"""
SAME, MOVED, CHANGED, MISSING = "same", "moved", "changed", "missing"
FUZZY_OK = 80  # partial-ratio for "the same passage, reworded"


def ensure_schema(registry: Registry) -> None:
    with registry._lock:  # noqa: SLF001
        registry.conn.executescript(SCHEMA)


# ------------------------------------------------------------------ files
def file_path(knowledge_dir: Path, registry: Registry, paper_id: int, file_id: int) -> Path | None:
    """An existing copy of one file of this paper, preferring the paper's own folder."""
    if file_id < 0:
        return supplements.file_path(knowledge_dir, registry, paper_id, -file_id)
    if not registry.one("SELECT 1 FROM files WHERE id=? AND paper_id=?", (file_id, paper_id)):
        return None
    row = registry.get_paper(paper_id)
    folder = library.folder_of(knowledge_dir, row["kb_path"]) if row and row["kb_path"] and row["kb_path"].count("/") >= 2 else None
    paths = [Path(r["path"]) for r in registry.query("SELECT path FROM file_paths WHERE file_id=?", (file_id,))]
    paths.sort(key=lambda p: not (folder and library._is_within(p, folder)))  # noqa: SLF001
    return next((p for p in paths if p.exists()), None)


def versions(knowledge_dir: Path, registry: Registry, paper_id: int) -> list[dict]:
    """Known versions, newest kind first (published before preprint)."""
    ensure_schema(registry)
    out = []
    for r in registry.query("SELECT * FROM pdf_versions WHERE paper_id=? ORDER BY kind='preprint', added_at DESC", (paper_id,)):
        p = file_path(knowledge_dir, registry, paper_id, r["file_id"])
        if p is not None:
            out.append({"file_id": r["file_id"], "kind": r["kind"], "doi": r["doi"], "path": p, "added_at": r["added_at"]})
    return out


def preferred(knowledge_dir: Path, registry: Registry, paper_id: int) -> tuple[Path, int] | None:
    """The version to show by default: the published one when there are several."""
    vs = versions(knowledge_dir, registry, paper_id)
    return (vs[0]["path"], vs[0]["file_id"]) if len(vs) > 1 else None


def find_revision(knowledge_dir: Path, registry: Registry, paper_id: int, revision: str) -> int | None:
    """Which of the paper's files an excerpt was taken from (by its revision hash)."""
    from .evidence import document_revision

    ensure_schema(registry)
    supplement = registry.one("SELECT id FROM paper_supplements WHERE paper_id=? AND revision=?", (paper_id, revision))
    if supplement:
        path = supplements.file_path(knowledge_dir, registry, paper_id, supplement["id"])
        if path is not None:
            return -supplement["id"]
    rows = registry.query("SELECT file_id FROM pdf_versions WHERE paper_id=?", (paper_id,))
    if len(rows) < 2:  # only real preprint / published versions; an edited PDF stays "changed"
        return None
    for r in rows:
        p = file_path(knowledge_dir, registry, paper_id, r["file_id"])
        try:
            if p is not None and document_revision(p) == revision:
                return int(r["file_id"])
        except OSError:
            continue
    return None


def register(registry: Registry, paper_id: int, file_id: int, kind: str, doi: str | None) -> None:
    ensure_schema(registry)
    registry.execute("INSERT OR IGNORE INTO pdf_versions(file_id, paper_id, kind, doi, added_at) VALUES (?,?,?,?,?)",
                     (file_id, paper_id, kind, doi, now_iso()))


# ------------------------------------------------------------------ comparing
def _norm(s: str) -> str:
    s = re.sub(r"-\s*\n\s*", "", s or "")
    return re.sub(r"\s+", " ", s).strip().lower()


def page_texts(path: Path) -> list[str]:
    with pymupdf.open(path) as doc:
        return [_norm(doc[i].get_text()) for i in range(min(doc.page_count, 120))]


def locate(quote: str, pages: list[str]) -> tuple[str, int | None, str, float]:
    """Where a quoted passage is in another version: (status, page, matched text, score)."""
    q = _norm(quote)
    if len(q) < 8:
        return MISSING, None, "", 0.0
    for i, t in enumerate(pages):
        if q in t:
            return SAME, i, quote, 100.0
    best = (0.0, None, "")
    for i, t in enumerate(pages):
        if not t:
            continue
        al = fuzz.partial_ratio_alignment(q, t, score_cutoff=best[0])
        if al and al.score > best[0]:
            best = (al.score, i, t[al.dest_start:al.dest_end])
    score, page, text = best
    if page is not None and score >= FUZZY_OK:
        return CHANGED, page, text, score
    return MISSING, page, text if score >= 60 else "", score


def compare(knowledge_dir: Path, registry: Registry, paper_id: int, old_file: int, new_file: int,
            old_abstract: str = "", new_abstract: str = "") -> dict:
    """Check every excerpt / highlight made on ``old_file`` against ``new_file``."""
    from .evidence import document_revision

    ensure_schema(registry)
    old_p = file_path(knowledge_dir, registry, paper_id, old_file)
    new_p = file_path(knowledge_dir, registry, paper_id, new_file)
    if old_p is None or new_p is None:
        return {}
    pages = page_texts(new_p)
    old_rev = document_revision(old_p)
    items = [("evidence", r["id"], r["page"], r["quote"]) for r in registry.query(
        "SELECT id, page, quote FROM evidence_entries WHERE paper_id=? AND revision=? AND source!='region'", (paper_id, old_rev))]
    original = registry.one("SELECT min(file_id) AS f FROM pdf_versions WHERE paper_id=?", (paper_id,))["f"]
    items += [("annotation", r["id"], r["page"], r["text"]) for r in registry.query(
        "SELECT id, page, text FROM annotations WHERE paper_id=? AND (pdf_file=? OR (pdf_file IS NULL AND ?=?)) "
        "AND coalesce(text, '') != ''", (paper_id, old_file, old_file, original))]
    stats = {SAME: 0, MOVED: 0, CHANGED: 0, MISSING: 0}
    for item, ref, page, text in items:
        status, new_page, new_text, score = locate(text, pages)
        if status == SAME and new_page != page:
            status = MOVED
        stats[status] += 1
        _store(registry, paper_id, item, ref, old_file, new_file, page, text, status, new_page, new_text, score)
    # A changed panel cannot be matched by comparing its label as if it were a quotation.
    for r in registry.query("SELECT id,page,region_label FROM evidence_entries WHERE paper_id=? "
                            "AND revision=? AND source='region'", (paper_id, old_rev)):
        _store(registry, paper_id, "evidence", r["id"], old_file, new_file, r["page"], r["region_label"],
               "visual_review", None, "", 0)
        stats["visual_review"] = stats.get("visual_review", 0) + 1
    if old_abstract and new_abstract:
        score = fuzz.ratio(_norm(old_abstract), _norm(new_abstract))
        if score < 97:
            _store(registry, paper_id, "abstract", 0, old_file, new_file, None, old_abstract,
                   CHANGED, None, new_abstract, score)
            stats["abstract"] = round(score)
    return stats


def _store(registry, paper_id, item, ref, old_file, new_file, page, text, status, new_page, new_text, score) -> None:
    registry.execute(
        "INSERT INTO version_checks(paper_id, item, ref_id, old_file, new_file, old_page, old_text, status, new_page, "
        "new_text, score, decided, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(paper_id, item, ref_id, new_file) DO UPDATE SET status=excluded.status, new_page=excluded.new_page, "
        "new_text=excluded.new_text, score=excluded.score",
        (paper_id, item, ref, old_file, new_file, page, text, status, new_page, new_text, score,
         "auto" if status in (SAME, MOVED) else None, now_iso()))


def checks(registry: Registry, paper_id: int | None = None, open_only: bool = False) -> list[dict]:
    ensure_schema(registry)
    sql, params = "SELECT c.*, p.title, p.first_author, p.year FROM version_checks c JOIN papers p ON p.id=c.paper_id WHERE 1=1", []
    if paper_id is not None:
        sql += " AND c.paper_id=?"
        params.append(paper_id)
    if open_only:
        sql += " AND c.decided IS NULL"
    out = []
    for r in registry.query(sql + " ORDER BY c.decided IS NOT NULL, c.item='abstract' DESC, c.id", tuple(params)):
        d = dict(r)
        if r["item"] == "evidence":
            q = registry.one("SELECT e.question_id, q.title FROM evidence_entries e JOIN evidence_questions q ON q.id=e.question_id "
                             "WHERE e.id=?", (r["ref_id"],))
            d["question_id"], d["question"] = (q["question_id"], q["title"]) if q else (None, "")
        out.append(d)
    return out


def open_count(registry: Registry) -> int:
    ensure_schema(registry)
    return registry.one("SELECT count(*) AS n FROM version_checks WHERE decided IS NULL")["n"]


def decide(registry: Registry, check_id: int) -> None:
    ensure_schema(registry)
    registry.execute("UPDATE version_checks SET decided=? WHERE id=?", (now_iso(), check_id))


def on_new_version(knowledge_dir: Path, registry: Registry, paper_id: int, old_meta: dict, new_meta: dict,
                   new_file: int, new_kind: str, new_doi: str) -> dict:
    """Called when another version of a paper already in the library arrives."""
    ensure_schema(registry)
    first = registry.one("SELECT id FROM files WHERE paper_id=? AND id != ? ORDER BY id LIMIT 1", (paper_id, new_file))
    if first is None:
        return {}
    old_kind = "preprint" if old_meta.get("type") == "posted-content" else "published"
    old_doi = registry.one("SELECT doi FROM paper_dois WHERE paper_id=? AND kind=? LIMIT 1", (paper_id, old_kind))
    register(registry, paper_id, int(first["id"]), old_kind, old_doi["doi"] if old_doi else None)
    register(registry, paper_id, new_file, new_kind, new_doi)
    if new_kind == "published" and old_kind == "preprint":
        return compare(knowledge_dir, registry, paper_id, int(first["id"]), new_file,
                       old_meta.get("abstract") or "", new_meta.get("abstract") or "")
    return {}


def kind_label(kind: str) -> str:
    return {"preprint": "预印本", "published": "正式发表版"}.get(kind, kind)


def dumps(stats: dict) -> str:
    return json.dumps(stats, ensure_ascii=False)
