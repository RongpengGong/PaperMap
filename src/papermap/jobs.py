"""Background jobs for slow per-paper work (translation, skills).

Jobs are stored in SQLite so the UI can show progress and interrupted jobs
are visible after a restart. One worker thread runs them in order.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

from .registry import Registry, now_iso

log = logging.getLogger(__name__)


class JobRunner:
    def __init__(self, registry: Registry, handlers: dict[str, Callable[[dict, Callable[[int, int], None]], str]]):
        self.registry = registry
        self.handlers = handlers
        self.q: queue.Queue[int] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # jobs interrupted by a quit are marked so the UI does not show them as running forever
        registry.execute("UPDATE jobs SET status='error', message='interrupted (app closed)' "
                         "WHERE status IN ('queued','running')")

    def submit(self, kind: str, paper_id: int, arg: str = "") -> int:
        existing = self.registry.one(
            "SELECT id FROM jobs WHERE kind=? AND paper_id=? AND arg=? AND status IN ('queued','running')",
            (kind, paper_id, arg))
        if existing:
            return int(existing["id"])
        cur = self.registry.execute(
            "INSERT INTO jobs(kind, paper_id, arg, status, progress, total, created_at, updated_at) "
            "VALUES (?,?,?, 'queued', 0, 0, ?, ?)", (kind, paper_id, arg, now_iso(), now_iso()))
        jid = int(cur.lastrowid)
        self.q.put(jid)
        self._ensure_thread()
        return jid

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._work, name="jobs", daemon=True)
                self._thread.start()

    def _set(self, jid: int, **cols) -> None:
        cols["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in cols)
        self.registry.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*cols.values(), jid))

    def _work(self) -> None:
        while True:
            jid = self.q.get()
            row = self.registry.one("SELECT * FROM jobs WHERE id=?", (jid,))
            if row is None or row["status"] != "queued":
                continue
            self._set(jid, status="running")
            try:
                msg = self.handlers[row["kind"]](dict(row), lambda done, total: self._set(jid, progress=done, total=total))
                self._set(jid, status="done", message=msg or "")
            except Exception as exc:  # noqa: BLE001 - a failed job must not stop the worker
                log.exception("job %s failed", jid)
                self._set(jid, status="error", message=f"{type(exc).__name__}: {exc}"[:500])
                self.registry.log("error", f"{row['kind']} failed for paper #{row['paper_id']}: {exc}", row["paper_id"])
