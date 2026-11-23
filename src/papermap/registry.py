"""SQLite state store (papers, files, DOIs, LLM calls, events, HTTP cache)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

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
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # -- low level ---------------------------------------------------------
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
    ) -> None:
        self.execute(
            "INSERT INTO llm_calls(paper_id, purpose, model, prompt_tokens, completion_tokens, "
            "cost_usd, ok, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (paper_id, purpose, model, prompt_tokens, completion_tokens, cost_usd, int(ok), now_iso()),
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

    def iter_papers_by_status(self, statuses: tuple[str, ...]) -> Iterator[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        yield from self.query(f"SELECT * FROM papers WHERE status IN ({marks})", statuses)
