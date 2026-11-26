"""SQLite state store (papers, files, DOIs, LLM calls, events, HTTP cache)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .extension_events import SCHEMA as EXTENSION_SCHEMA

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY,
    doi TEXT UNIQUE,
    doi_candidate TEXT,
    openalex_id TEXT,
    title TEXT,
    year INTEGER,
    journal TEXT,
    first_author TEXT,
    authors_json TEXT,
    abstract TEXT,
    meta_json TEXT,
    card_json TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    status_detail TEXT,
    kb_path TEXT,
    prompt_version TEXT,
    model TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_papers_openalex ON papers(openalex_id);
CREATE INDEX IF NOT EXISTS ix_papers_status ON papers(status);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER REFERENCES papers(id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    size INTEGER,
    mtime REAL,
    seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_files_path ON files(path);

CREATE TABLE IF NOT EXISTS file_paths (
    file_id INTEGER NOT NULL REFERENCES files(id),
    path TEXT NOT NULL,
    size INTEGER,
    mtime REAL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (file_id, path)
);
CREATE INDEX IF NOT EXISTS ix_file_paths_path ON file_paths(path);

CREATE TABLE IF NOT EXISTS paper_dois (
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    doi TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'published'
);

CREATE TABLE IF NOT EXISTS fields (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE,
    name_zh TEXT, name_en TEXT,
    axis TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS paper_fields (
    paper_id INTEGER, field_id INTEGER,
    confidence REAL, is_primary INTEGER,
    PRIMARY KEY (paper_id, field_id)
);
CREATE TABLE IF NOT EXISTS pending_fields (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    axis TEXT NOT NULL,
    name_zh TEXT, name_en TEXT,
    description TEXT,
    synonyms_json TEXT NOT NULL DEFAULT '[]',
    embedding_json TEXT,
    status TEXT NOT NULL DEFAULT 'open',   -- open / established / merged / rejected
    merged_into TEXT,
    ready_notified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_supplements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    name TEXT NOT NULL, revision TEXT NOT NULL,
    reading_page INTEGER, reading_offset REAL, reading_at INTEGER,
    UNIQUE(paper_id, revision)
);
CREATE TABLE IF NOT EXISTS evidence_questions (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_question_fields (
    question_id INTEGER NOT NULL REFERENCES evidence_questions(id) ON DELETE CASCADE,
    field_id INTEGER NOT NULL REFERENCES fields(id),
    PRIMARY KEY (question_id, field_id)
);
CREATE TABLE IF NOT EXISTS evidence_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, question_id INTEGER NOT NULL REFERENCES evidence_questions(id),
    paper_id INTEGER NOT NULL REFERENCES papers(id), merged_from INTEGER, page INTEGER NOT NULL,
    revision TEXT NOT NULL, rects_json TEXT NOT NULL, quote TEXT NOT NULL, source TEXT NOT NULL,
    stance TEXT NOT NULL, note TEXT NOT NULL, anchor_key TEXT NOT NULL,
    created_at TEXT NOT NULL, UNIQUE(question_id, paper_id, anchor_key)
);
CREATE TABLE IF NOT EXISTS evidence_region_previews (
    revision TEXT NOT NULL, page INTEGER NOT NULL, rects_json TEXT NOT NULL, png BLOB NOT NULL,
    PRIMARY KEY(revision, page, rects_json)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS mobile_library (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    library_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mobile_note_receipts (
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    revision TEXT NOT NULL, client_id TEXT NOT NULL, signature TEXT NOT NULL,
    segment_id TEXT NOT NULL, evidence_id INTEGER NOT NULL,
    PRIMARY KEY (paper_id, revision, client_id)
) WITHOUT ROWID;
CREATE TRIGGER IF NOT EXISTS evidence_region_cleanup AFTER DELETE ON evidence_entries WHEN OLD.source='region'
BEGIN
    DELETE FROM evidence_region_previews WHERE revision=OLD.revision AND page=OLD.page AND rects_json=OLD.rects_json
    AND NOT EXISTS (SELECT 1 FROM evidence_entries WHERE source='region' AND revision=OLD.revision
                    AND page=OLD.page AND rects_json=OLD.rects_json);
END;
CREATE TABLE IF NOT EXISTS dataset_overlap_reviews (
    question_id INTEGER NOT NULL REFERENCES evidence_questions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, accession TEXT NOT NULL,
    paper_a INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    paper_b INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE CHECK(paper_a < paper_b),
    context_key TEXT NOT NULL, decision TEXT NOT NULL, note TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(question_id, kind, accession, paper_a, paper_b)
);
CREATE TABLE IF NOT EXISTS card_versions (  -- earlier states of a card the user edited (text NULL = generated)
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL,
    text TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_card_versions ON card_versions(paper_id, id);
CREATE TABLE IF NOT EXISTS update_cards (  -- "what does this paper change" (stage 7 task 3)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    question_id INTEGER NOT NULL REFERENCES evidence_questions(id) ON DELETE CASCADE,
    outcome TEXT NOT NULL, headline TEXT NOT NULL, claims_json TEXT NOT NULL,
    comparability TEXT NOT NULL DEFAULT 'comparable', comparability_note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',  -- pending / confirmed / rewritten / kept / dismissed
    user_text TEXT, user_outcome TEXT, revision TEXT NOT NULL,
    model TEXT, prompt_version TEXT, created_at TEXT NOT NULL, decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_update_cards_question ON update_cards(question_id, status);
CREATE TABLE IF NOT EXISTS annotations (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    page INTEGER NOT NULL,            -- 0-based
    rects_json TEXT NOT NULL,         -- [[x0,y0,x1,y1], ...] in PDF points
    text TEXT,
    color TEXT NOT NULL DEFAULT 'yellow',
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- A deleted highlight that a paired phone may still hold; keeps the deletion from coming back.
CREATE TABLE IF NOT EXISTS annotation_tombstones (
    uid TEXT PRIMARY KEY,
    paper_id INTEGER NOT NULL,
    deleted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,          -- translate / skill
    paper_id INTEGER NOT NULL,
    arg TEXT NOT NULL DEFAULT '',  -- service id / skill id
    status TEXT NOT NULL,        -- queued / running / done / error
    progress INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS translation_inputs (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    paragraphs_digest TEXT NOT NULL,
    glossary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS translation_parts (
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    paragraph INTEGER NOT NULL,
    text TEXT NOT NULL,
    model TEXT NOT NULL,
    PRIMARY KEY (job_id, paragraph)
);
CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES collections(id),
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_collections (
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    collection_id INTEGER NOT NULL REFERENCES collections(id),
    added_at TEXT NOT NULL,
    PRIMARY KEY (paper_id, collection_id)
);
CREATE TABLE IF NOT EXISTS field_syntheses (
    id INTEGER PRIMARY KEY,
    field_id INTEGER NOT NULL REFERENCES fields(id),
    n_papers INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    model TEXT, prompt_version TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS field_proposals (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER REFERENCES papers(id),
    axis TEXT, name_zh TEXT, name_en TEXT,
    description TEXT, reason TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER,
    purpose TEXT,
    model TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    cost_usd REAL,
    ok INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    paper_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS http_cache (
    url TEXT PRIMARY KEY,
    status INTEGER,
    body TEXT,
    fetched_at REAL
);
"""

# Columns added after the first release; created on open if missing.
MIGRATIONS = {
    "jobs": {"options_json": "TEXT NOT NULL DEFAULT ''"},
    "fields": {"description": "TEXT", "sort_order": "INTEGER"},
    "paper_fields": {
        "status": "TEXT NOT NULL DEFAULT 'accepted'",  # accepted / suggested / rejected
        "source": "TEXT NOT NULL DEFAULT 'llm'",  # llm / user
        "note": "TEXT",  # what this paper adds to the field (from the classifier)
        "reason": "TEXT",
    },
    "papers": {"classify_key": "TEXT", "classified_at": "TEXT", "needs_review": "INTEGER DEFAULT 0",
               "read_status": "TEXT NOT NULL DEFAULT 'unread'", "starred": "INTEGER NOT NULL DEFAULT 0",
               "tags_json": "TEXT", "zotero_key": "TEXT",
               "reading_page": "INTEGER", "reading_offset": "REAL", "reading_at": "INTEGER",
               "card_override": "TEXT", "trash_path": "TEXT", "deleted_status": "TEXT",
               "read_at": "TEXT",  # when the user last marked the paper read (periodic reviews)
               "card_language": "TEXT"},  # content language the card text was generated in  # when the user last marked the paper read (periodic reviews)  # the user's own edit of the card body (kept over regeneration)
    "annotations": {"pdf_file": "INTEGER",  # which version of the PDF (files.id) when a paper has several
                    "uid": "TEXT",  # stable ID shared with a paired phone
                    "style": "TEXT NOT NULL DEFAULT 'highlight'"},  # highlight / underline
    "field_proposals": {"pending_id": "INTEGER", "outcome": "TEXT"},
    "llm_calls": {"billing": "TEXT NOT NULL DEFAULT 'token'"},  # token / subscription
    # research questions: user-made are active at once; model-proposed ones wait as 'suggested'
    "evidence_questions": {"status": "TEXT NOT NULL DEFAULT 'active'",  # active / suggested / dismissed
                           "origin": "TEXT NOT NULL DEFAULT 'user'",  # user / suggested
                           "rationale": "TEXT", "source_json": "TEXT",
                           "judgment": "TEXT NOT NULL DEFAULT ''", "judgment_at": "TEXT"},
    "evidence_entries": {"dimension": "TEXT NOT NULL DEFAULT 'general'", "region_label": "TEXT NOT NULL DEFAULT ''"},
}

PAPER_STATUSES = (
    "new",
    "extracting",
    "done",
    "doi_unverified",
    "skipped_supplement",
    "error",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Registry:
    """Thread-safe wrapper around a single SQLite connection."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.initialize_schema()

    def initialize_schema(self) -> None:
        """Set up tables and migrate columns after opening or restoring a database."""
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.executescript(EXTENSION_SCHEMA)
        self.conn.commit()

    def _migrate(self) -> None:
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # -- low level ---------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize short read-modify-write operations across connections."""
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            yield self.conn

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def merge_duplicate_paper(self, paper_id: int, existing_id: int) -> None:
        """Move a confirmed duplicate's files and evidence to the existing paper together."""
        with self._lock, self.conn:
            # Two records for the same PDF may have different personal notes on the same passage.
            # Keep both entries (and their IDs); new captures still deduplicate against the original.
            self.conn.execute("""
                UPDATE evidence_entries AS incoming SET anchor_key=anchor_key || '-' || id
                WHERE paper_id=? AND EXISTS (
                    SELECT 1 FROM evidence_entries AS original WHERE original.paper_id=?
                    AND original.question_id=incoming.question_id AND original.anchor_key=incoming.anchor_key
                )
            """, (paper_id, existing_id))
            self.conn.execute("UPDATE evidence_entries SET merged_from=COALESCE(merged_from,paper_id), paper_id=? "
                              "WHERE paper_id=?", (existing_id, paper_id))
            self.conn.execute("UPDATE files SET paper_id=? WHERE paper_id=?", (existing_id, paper_id))
            self.conn.execute("UPDATE update_cards SET paper_id=? WHERE paper_id=?", (existing_id, paper_id))
            if self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='gap_hits'").fetchone():
                self.conn.execute("UPDATE OR IGNORE gap_hits SET paper_id=? WHERE paper_id=?", (existing_id, paper_id))
                self.conn.execute("DELETE FROM gap_hits WHERE paper_id=?", (paper_id,))
            if self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='question_gaps'").fetchone():
                self.conn.execute("UPDATE question_gaps SET closed_by=? WHERE closed_by=?", (existing_id, paper_id))
            self.conn.execute("DELETE FROM papers WHERE id=?", (paper_id,))

    # -- events ------------------------------------------------------------
    def log(self, level: str, message: str, paper_id: int | None = None) -> None:
        self.execute(
            "INSERT INTO events(level, message, paper_id, created_at) VALUES (?,?,?,?)",
            (level, message, paper_id, now_iso()),
        )

    def recent_events(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # -- files -------------------------------------------------------------
    def file_by_sha(self, sha: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM files WHERE sha256=?", (sha,))

    def file_by_path_stat(self, path: str, size: int, mtime: float) -> sqlite3.Row | None:
        """Fast path: same path, size and mtime as before -> no need to rehash."""
        return self.one(
            "SELECT f.* FROM file_paths fp JOIN files f ON f.id = fp.file_id "
            "WHERE fp.path=? AND fp.size=? AND abs(fp.mtime-?) < 0.001",
            (path, size, mtime),
        )

    def upsert_file(
        self, sha: str, path: str, size: int, mtime: float, paper_id: int | None = None
    ) -> int:
        with self._lock:
            row = self.file_by_sha(sha)
            ts = now_iso()
            if row is None:
                cur = self.conn.execute(
                    "INSERT INTO files(paper_id, path, sha256, size, mtime, seen_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (paper_id, path, sha, size, mtime, ts),
                )
                file_id = cur.lastrowid
            else:
                file_id = row["id"]
                self.conn.execute(
                    "UPDATE files SET path=?, size=?, mtime=?, seen_at=?, "
                    "paper_id=COALESCE(?, paper_id) WHERE id=?",
                    (path, size, mtime, ts, paper_id, file_id),
                )
            self.conn.execute(
                "INSERT INTO file_paths(file_id, path, size, mtime, seen_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(file_id, path) DO UPDATE SET size=excluded.size, "
                "mtime=excluded.mtime, seen_at=excluded.seen_at",
                (file_id, path, size, mtime, ts),
            )
            self.conn.commit()
            return file_id

    def move_file_path(self, file_id: int, old: str, new: str) -> None:
        """A managed file was moved on disk (imported/ -> paper folder)."""
        with self._lock:
            self.conn.execute("UPDATE files SET path=? WHERE id=? AND path=?", (new, file_id, old))
            self.conn.execute("UPDATE OR REPLACE file_paths SET path=? WHERE file_id=? AND path=?", (new, file_id, old))
            self.conn.commit()

    def link_file(self, sha: str, paper_id: int) -> None:
        self.execute("UPDATE files SET paper_id=? WHERE sha256=?", (paper_id, sha))

    def paper_file_paths(self, paper_id: int) -> list[str]:
        rows = self.query(
            "SELECT DISTINCT fp.path FROM files f JOIN file_paths fp ON fp.file_id=f.id "
            "WHERE f.paper_id=? ORDER BY fp.path",
            (paper_id,),
        )
        return [r["path"] for r in rows]

    # -- papers ------------------------------------------------------------
    def create_paper(self, **fields: Any) -> int:
        ts = now_iso()
        fields.setdefault("status", "new")
        for key in ("authors_json", "meta_json", "card_json"):
            if key in fields and not isinstance(fields[key], (str, type(None))):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        cols = list(fields) + ["created_at", "updated_at"]
        vals = list(fields.values()) + [ts, ts]
        cur = self.execute(
            f"INSERT INTO papers({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            tuple(vals),
        )
        return int(cur.lastrowid)

    def update_paper(self, paper_id: int, **fields: Any) -> None:
        if not fields:
            return
        for key in ("authors_json", "meta_json", "card_json"):
            if key in fields and not isinstance(fields[key], (str, type(None))):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        fields["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE papers SET {sets} WHERE id=?", (*fields.values(), paper_id))

    def get_paper(self, paper_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM papers WHERE id=?", (paper_id,))

    def paper_by_doi(self, doi: str) -> sqlite3.Row | None:
        row = self.one(
            "SELECT p.* FROM paper_dois d JOIN papers p ON p.id=d.paper_id WHERE d.doi=?", (doi,)
        )
        return row or self.one("SELECT * FROM papers WHERE doi=?", (doi,))

    def paper_by_openalex(self, openalex_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM papers WHERE openalex_id=?", (openalex_id,))

    def add_doi(self, paper_id: int, doi: str, kind: str = "published") -> None:
        self.execute(
            "INSERT OR IGNORE INTO paper_dois(paper_id, doi, kind) VALUES (?,?,?)",
            (paper_id, doi, kind),
        )

    def paper_dois(self, paper_id: int) -> list[sqlite3.Row]:
        return self.query("SELECT doi, kind FROM paper_dois WHERE paper_id=?", (paper_id,))

    def papers_without_doi(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM papers WHERE doi IS NULL AND title IS NOT NULL")

    def list_papers(self, status: str | None = None, limit: int = 500) -> list[sqlite3.Row]:
        if status:
            return self.query(
                "SELECT * FROM papers WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
            )
        return self.query("SELECT * FROM papers ORDER BY id DESC LIMIT ?", (limit,))

    def status_counts(self) -> dict[str, int]:
        rows = self.query("SELECT status, count(*) AS n FROM papers GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    # -- LLM accounting ----------------------------------------------------
    def record_llm_call(
        self,
        paper_id: int | None,
        purpose: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        ok: bool,
        billing: str = "token",
    ) -> None:
        self.execute(
            "INSERT INTO llm_calls(paper_id, purpose, model, prompt_tokens, completion_tokens, "
            "cost_usd, ok, created_at, billing) VALUES (?,?,?,?,?,?,?,?,?)",
            (paper_id, purpose, model, prompt_tokens, completion_tokens, cost_usd, int(ok), now_iso(),
             billing if billing in ("subscription", "external") else "token"),
        )

    def month_cost(self) -> float:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        row = self.one(
            "SELECT COALESCE(SUM(cost_usd),0) AS c FROM llm_calls WHERE substr(created_at,1,7)=?",
            (month,),
        )
        return float(row["c"]) if row else 0.0

    # -- HTTP cache --------------------------------------------------------
    def cache_get(self, url: str, max_age_s: float) -> tuple[int, Any] | None:
        row = self.one("SELECT status, body, fetched_at FROM http_cache WHERE url=?", (url,))
        if row is None or time.time() - row["fetched_at"] > max_age_s:
            return None
        return row["status"], json.loads(row["body"]) if row["body"] else None

    def cache_put(self, url: str, status: int, body: Any) -> None:
        self.execute(
            "INSERT OR REPLACE INTO http_cache(url, status, body, fetched_at) VALUES (?,?,?,?)",
            (url, status, json.dumps(body) if body is not None else None, time.time()),
        )

    # -- fields / classification -----------------------------------------
    def sync_fields(self, taxonomy) -> None:
        """Mirror the taxonomy into the fields table; removed slugs -> retired."""
        with self._lock:
            slugs = set()
            for i, c in enumerate(taxonomy.categories):
                slugs.add(c.slug)
                self.conn.execute(
                    "INSERT INTO fields(slug, name_zh, name_en, axis, status, description, sort_order) "
                    "VALUES (?,?,?,?, 'active', ?, ?) ON CONFLICT(slug) DO UPDATE SET "
                    "name_zh=excluded.name_zh, name_en=excluded.name_en, axis=excluded.axis, "
                    "description=excluded.description, sort_order=excluded.sort_order, "
                    "status=CASE WHEN fields.status IN ('active','retired') THEN 'active' ELSE fields.status END",
                    (c.slug, c.name_zh, c.name_en, c.axis, c.description, i),
                )
            marks = ",".join("?" * len(slugs))
            self.conn.execute(
                f"UPDATE fields SET status='retired' WHERE status='active' AND slug NOT IN ({marks})",
                tuple(slugs),
            )
            self.conn.commit()

    def field_by_slug(self, slug: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM fields WHERE slug=?", (slug,))

    def active_fields(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM fields WHERE status='active' ORDER BY axis, sort_order")

    def paper_field_rows(self, paper_id: int, include_rejected: bool = False) -> list[sqlite3.Row]:
        cond = "" if include_rejected else "AND pf.status != 'rejected'"
        return self.query(
            "SELECT pf.*, f.slug, f.name_zh, f.name_en, f.axis, f.status AS field_status "
            f"FROM paper_fields pf JOIN fields f ON f.id=pf.field_id WHERE pf.paper_id=? {cond} "
            "ORDER BY f.axis, pf.is_primary DESC, pf.confidence DESC",
            (paper_id,),
        )

    def field_paper_rows(self, field_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT pf.*, p.title, p.year, p.journal, p.first_author, p.kb_path, p.card_json, p.doi "
            "FROM paper_fields pf JOIN papers p ON p.id=pf.paper_id "
            "WHERE pf.field_id=? AND pf.status='accepted' AND p.status='done' "
            "ORDER BY p.year DESC, p.id DESC",
            (field_id,),
        )

    def set_paper_field(self, paper_id: int, field_id: int, **cols: Any) -> None:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM paper_fields WHERE paper_id=? AND field_id=?", (paper_id, field_id)
            ).fetchone()
            if row is None:
                cols.setdefault("confidence", 1.0)
                cols.setdefault("is_primary", 0)
                names = ["paper_id", "field_id", *cols]
                self.conn.execute(
                    f"INSERT INTO paper_fields({','.join(names)}) VALUES ({','.join('?' * len(names))})",
                    (paper_id, field_id, *cols.values()),
                )
            elif cols:
                sets = ", ".join(f"{k}=?" for k in cols)
                self.conn.execute(
                    f"UPDATE paper_fields SET {sets} WHERE paper_id=? AND field_id=?",
                    (*cols.values(), paper_id, field_id),
                )
            self.conn.commit()

    def add_proposal(self, paper_id: int, axis: str, name_zh: str, name_en: str,
                     description: str, reason: str, pending_id: int | None = None,
                     outcome: str | None = None, status: str = "open") -> int:
        cur = self.execute(
            "INSERT INTO field_proposals(paper_id, axis, name_zh, name_en, description, reason, "
            "pending_id, outcome, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (paper_id, axis, name_zh, name_en, description, reason, pending_id, outcome, status, now_iso()),
        )
        return int(cur.lastrowid)

    # -- pending (proposed) fields -------------------------------------------
    def pending_fields(self, status: str | None = "open") -> list[sqlite3.Row]:
        cond, params = ("WHERE pf.status=?", (status,)) if status else ("", ())
        return self.query(
            "SELECT pf.*, (SELECT count(DISTINCT fp.paper_id) FROM field_proposals fp "
            " WHERE fp.pending_id=pf.id) AS n_papers FROM pending_fields pf "
            f"{cond} ORDER BY n_papers DESC, pf.id",
            params,
        )

    def pending_by_id(self, pid: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM pending_fields WHERE id=?", (pid,))

    def pending_papers(self, pid: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT DISTINCT p.id, p.title, p.year, p.journal, p.first_author, p.kb_path, p.doi, "
            "fp.reason, fp.name_zh AS prop_zh, fp.name_en AS prop_en FROM field_proposals fp "
            "JOIN papers p ON p.id=fp.paper_id WHERE fp.pending_id=? GROUP BY p.id ORDER BY p.year DESC",
            (pid,),
        )

    def create_pending(self, slug: str, axis: str, name_zh: str, name_en: str, description: str,
                       synonyms: list[str]) -> int:
        ts = now_iso()
        cur = self.execute(
            "INSERT INTO pending_fields(slug, axis, name_zh, name_en, description, synonyms_json, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (slug, axis, name_zh, name_en, description, json.dumps(synonyms, ensure_ascii=False), ts, ts),
        )
        return int(cur.lastrowid)

    def update_pending(self, pid: int, **cols: Any) -> None:
        if "synonyms_json" in cols and not isinstance(cols["synonyms_json"], str):
            cols["synonyms_json"] = json.dumps(cols["synonyms_json"], ensure_ascii=False)
        cols["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in cols)
        self.execute(f"UPDATE pending_fields SET {sets} WHERE id=?", (*cols.values(), pid))

    # -- reader annotations ---------------------------------------------------
    def annotations(self, paper_id: int) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM annotations WHERE paper_id=? ORDER BY page, id", (paper_id,))

    def add_annotation(self, paper_id: int, page: int, rects_json: str, text: str, color: str, note: str,
                       pdf_file: int | None = None) -> int:
        ts = now_iso()
        cur = self.execute(
            "INSERT INTO annotations(paper_id, page, rects_json, text, color, note, created_at, updated_at, pdf_file) "
            "VALUES (?,?,?,?,?,?,?,?,?)", (paper_id, page, rects_json, text, color, note, ts, ts, pdf_file))
        return int(cur.lastrowid)

    def update_annotation(self, ann_id: int, **cols: Any) -> None:
        cols["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in cols)
        self.execute(f"UPDATE annotations SET {sets} WHERE id=?", (*cols.values(), ann_id))

    def delete_annotation(self, ann_id: int) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT uid, paper_id FROM annotations WHERE id=?", (ann_id,)).fetchone()
            if row is not None and row["uid"]:
                db.execute("INSERT OR REPLACE INTO annotation_tombstones(uid, paper_id, deleted_at) VALUES (?,?,?)",
                           (row["uid"], row["paper_id"], now_iso()))
            db.execute("DELETE FROM annotations WHERE id=?", (ann_id,))

    # -- collections (Zotero-style groups; a paper may be in several) --------
    def collections(self) -> list[sqlite3.Row]:
        return self.query(
            "SELECT c.*, (SELECT count(*) FROM paper_collections pc JOIN papers p ON p.id=pc.paper_id "
            " WHERE pc.collection_id=c.id AND p.status != 'ignored') AS n "
            "FROM collections c ORDER BY c.sort_order, c.name COLLATE NOCASE")

    def collection_tree(self) -> list[dict]:
        rows = [dict(r) for r in self.collections()]
        by_parent: dict[int | None, list[dict]] = {}
        for r in rows:
            by_parent.setdefault(r["parent_id"], []).append(r)

        def build(parent, depth):
            out = []
            for r in by_parent.get(parent, []):
                r["depth"] = depth
                r["children"] = build(r["id"], depth + 1)
                out.append(r)
            return out

        return build(None, 0)

    def collection_descendants(self, cid: int) -> list[int]:
        ids, frontier = [cid], [cid]
        while frontier:
            marks = ",".join("?" * len(frontier))
            frontier = [r["id"] for r in self.query(f"SELECT id FROM collections WHERE parent_id IN ({marks})", tuple(frontier))]
            ids += frontier
        return ids

    def create_collection(self, name: str, parent_id: int | None = None) -> int:
        name = name.strip()[:120]
        if not name:
            raise ValueError("name must not be empty")
        if parent_id is not None and self.one("SELECT 1 FROM collections WHERE id=?", (parent_id,)) is None:
            raise ValueError("parent collection not found")
        cur = self.execute("INSERT INTO collections(name, parent_id, created_at) VALUES (?,?,?)",
                           (name, parent_id, now_iso()))
        return int(cur.lastrowid)

    def rename_collection(self, cid: int, name: str) -> None:
        name = name.strip()[:120]
        if not name:
            raise ValueError("name must not be empty")
        self.execute("UPDATE collections SET name=? WHERE id=?", (name, cid))

    def move_collection(self, cid: int, parent_id: int | None) -> None:
        if parent_id is not None and parent_id in self.collection_descendants(cid):
            raise ValueError("cannot move a group into itself or its own subgroup")
        self.execute("UPDATE collections SET parent_id=? WHERE id=?", (parent_id, cid))

    def delete_collection(self, cid: int) -> list[int]:
        """Delete a group and its subgroups; papers stay in the library. Returns affected paper ids."""
        ids = self.collection_descendants(cid)
        marks = ",".join("?" * len(ids))
        papers = [r["paper_id"] for r in self.query(
            f"SELECT DISTINCT paper_id FROM paper_collections WHERE collection_id IN ({marks})", tuple(ids))]
        with self._lock:
            self.conn.execute(f"DELETE FROM paper_collections WHERE collection_id IN ({marks})", tuple(ids))
            self.conn.execute(f"DELETE FROM collections WHERE id IN ({marks})", tuple(ids))
            self.conn.commit()
        return papers

    def add_to_collection(self, cid: int, paper_ids: list[int]) -> int:
        if self.one("SELECT 1 FROM collections WHERE id=?", (cid,)) is None:
            raise ValueError("collection not found")
        n = 0
        with self._lock:
            for pid in paper_ids:
                if self.conn.execute("SELECT 1 FROM papers WHERE id=?", (pid,)).fetchone():
                    n += self.conn.execute(
                        "INSERT OR IGNORE INTO paper_collections(paper_id, collection_id, added_at) VALUES (?,?,?)",
                        (pid, cid, now_iso())).rowcount
            self.conn.commit()
        return n

    def remove_from_collection(self, cid: int, paper_ids: list[int]) -> None:
        with self._lock:
            for pid in paper_ids:
                self.conn.execute("DELETE FROM paper_collections WHERE paper_id=? AND collection_id=?", (pid, cid))
            self.conn.commit()

    def paper_collections(self, paper_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT c.* FROM paper_collections pc JOIN collections c ON c.id=pc.collection_id "
            "WHERE pc.paper_id=? ORDER BY c.name COLLATE NOCASE", (paper_id,))

    def collection_path(self, cid: int) -> str:
        parts, cur = [], self.one("SELECT * FROM collections WHERE id=?", (cid,))
        while cur is not None:
            parts.append(cur["name"])
            cur = self.one("SELECT * FROM collections WHERE id=?", (cur["parent_id"],)) if cur["parent_id"] else None
        return " / ".join(reversed(parts))

    def iter_papers_by_status(self, statuses: tuple[str, ...]) -> Iterator[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        yield from self.query(f"SELECT * FROM papers WHERE status IN ({marks})", statuses)
