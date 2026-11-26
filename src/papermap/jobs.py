"""Durable local job queue, with one worker per library across app processes."""

from __future__ import annotations

import errno
import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .registry import Registry, now_iso

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NeedsReview:
    message: str


def _worker_lock(db: Path, name: str = "jobs"):
    # Keep this file in place. The OS drops ownership when the handle/process closes.
    handle = db.with_suffix(db.suffix + f".{name}.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EDEADLK):
            return None
        raise
    return handle


class JobRunner:
    def __init__(self, registry: Registry, handlers: dict[str, Callable[[dict, Callable[[int, int], None]], str | NeedsReview]]):
        self.registry = registry
        self.handlers = handlers

    def recover(self) -> None:
        """Called by the long-lived application on startup, never by a status query."""
        self._schedule(None)

    def submit(self, kind: str, paper_id: int, arg: str = "", *, options: dict | None = None) -> int:
        encoded = json.dumps(options, ensure_ascii=False, sort_keys=True) if options else ""
        return self._schedule((kind, paper_id, arg, encoded))

    def _schedule(self, request) -> int:
        lock = None
        jid = 0
        try:
            with self.registry.transaction() as conn:
                lock = _worker_lock(self.registry.db_path)
                if lock:
                    # A queued non-translation job cannot be replayed safely after an exit.
                    # Translations and card regenerations resume; they are safe to replay.
                    conn.execute("UPDATE jobs SET status='error', message='interrupted (app closed)', updated_at=? "
                                 "WHERE kind NOT IN ('translate','recard') AND status IN ('queued','running')",
                                 (now_iso(),))
                    conn.execute("UPDATE jobs SET status='queued', message='', updated_at=? "
                                 "WHERE kind IN ('translate','recard') AND status='running'", (now_iso(),))
                if request:
                    jid = self._enqueue(conn, *request)
                pending = conn.execute("SELECT 1 FROM jobs WHERE status='queued' LIMIT 1").fetchone()
                if lock and not pending:
                    lock.close()
                    lock = None
            if lock:
                threading.Thread(target=self._work, args=(lock,), name="jobs", daemon=True).start()
                lock = None  # the worker now owns the file
        finally:
            if lock:
                lock.close()
        return jid

    @staticmethod
    def _enqueue(conn, kind, paper_id, arg, options) -> int:
        params = (kind, paper_id, arg, options)
        match = "kind=? AND paper_id=? AND arg=? AND (options_json=? OR options_json='')"
        active = conn.execute(f"SELECT id FROM jobs WHERE {match} AND status IN ('queued','running') LIMIT 1",
                              params).fetchone()
        if active:
            return active["id"]
        if kind == "translate":
            last = conn.execute(f"SELECT id,status FROM jobs WHERE {match} ORDER BY id DESC LIMIT 1",
                                params).fetchone()
            if last and last["status"] == "error":
                conn.execute("UPDATE jobs SET status='queued', message='', updated_at=? WHERE id=?",
                             (now_iso(), last["id"]))
                return last["id"]
        cur = conn.execute("INSERT INTO jobs(kind,paper_id,arg,options_json,status,created_at,updated_at) "
                           "VALUES (?,?,?,?,'queued',?,?)", (*params, now_iso(), now_iso()))
        return cur.lastrowid

    def _set(self, jid: int, **cols) -> None:
        cols["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in cols)
        self.registry.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*cols.values(), jid))

    def _work(self, lock) -> None:
        try:
            while True:
                with self.registry.transaction() as conn:
                    row = conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                    if row is None:
                        # Release while producers are excluded: a concurrent submit must either
                        # see an owner that will read its row, or be able to become the next owner.
                        lock.close()
                        return
                    jid = row["id"]
                    conn.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=?", (now_iso(), jid))
                try:
                    msg = self.handlers[row["kind"]](dict(row), lambda done, total:
                                                     self._set(jid, progress=done, total=total))
                    with self.registry.transaction() as conn:
                        status, message = ("needs_review", msg.message) if isinstance(msg, NeedsReview) else ("done", msg)
                        conn.execute("UPDATE jobs SET status=?, message=?, updated_at=? WHERE id=?",
                                     (status, message or "", now_iso(), jid))
                        if row["kind"] == "translate":
                            conn.execute("DELETE FROM translation_parts WHERE job_id=?", (jid,))
                            conn.execute("DELETE FROM translation_inputs WHERE job_id=?", (jid,))
                except Exception as exc:  # noqa: BLE001 - a failed job must not stop the worker
                    log.exception("job %s failed", jid)
                    self._set(jid, status="error", message=f"{type(exc).__name__}: {exc}"[:500])
                    self.registry.log("error", f"{row['kind']} failed for paper #{row['paper_id']}: {exc}",
                                      row["paper_id"])
        except sqlite3.ProgrammingError as exc:
            if "closed database" not in str(exc).lower():
                raise
        finally:
            lock.close()
