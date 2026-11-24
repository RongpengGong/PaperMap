"""Daily backups of the database and settings (CLAUDE.md §16.2 task 1).

A backup holds what exists only in the data folder: papermap.db (taken with
SQLite's online backup API, safe while the app writes in WAL mode),
config.yaml and the user's taxonomy.yaml. The Markdown knowledge base, PDF
copies and notes live in ``knowledge/`` and are *not* in a backup; moving the
whole library is a different operation (Settings -> knowledge location, or
copying the folders). After a restore, ``check_library`` reports which
papers' cards / PDFs / notes and which evidence quotes can still be found.

OAuth tokens (e.g. data_dir/chatgpt) are never backed up; sign in again.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import library
from .registry import Registry

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^papermap-backup-(\d{8}-\d{6})(?:-(\w+))?\.zip$")
MACHINE_KEYS = ("data_dir", "knowledge_dir")  # where things live on *this* computer
_lock = threading.Lock()


def settings(config) -> dict[str, Any]:
    b = config.raw.get("backup") or {}
    return {"enabled": b.get("enabled", True), "keep": max(1, int(b.get("keep") or 14)),
            "dir": (b.get("dir") or "").strip(), "every_hours": float(b.get("every_hours") or 24)}


def backup_dir(config) -> Path:
    d = settings(config)["dir"]
    return Path(d).expanduser() if d else config.data_dir / "backups"


def list_backups(config) -> list[dict[str, Any]]:
    d = backup_dir(config)
    out = []
    if d.is_dir():
        for p in d.iterdir():
            m = NAME_RE.match(p.name)
            if m and p.is_file():
                when = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
                meta = {}
                try:
                    with zipfile.ZipFile(p) as z:
                        meta = json.loads(z.read("manifest.json"))
                except (OSError, KeyError, ValueError, zipfile.BadZipFile):
                    meta = {"broken": True}
                out.append({"name": p.name, "path": p, "when": when, "kind": m.group(2) or "auto",
                            "size": p.stat().st_size, "meta": meta})
    return sorted(out, key=lambda b: b["when"], reverse=True)


def _snapshot_db(registry: Registry, dest: Path) -> None:
    dst = sqlite3.connect(dest)
    try:
        with registry._lock:  # noqa: SLF001 - consistent point in time
            registry.conn.commit()
            registry.conn.backup(dst)
    finally:
        dst.close()


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for table in ("papers", "evidence_questions", "evidence_entries", "annotations", "collections"):
        try:
            out[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            out[table] = 0
    return out


def create(config, registry: Registry, kind: str = "auto") -> Path:
    """Write papermap-backup-<utc>[-kind].zip atomically, then prune old auto backups."""
    with _lock:
        d = backup_dir(config)
        d.mkdir(parents=True, exist_ok=True)
        try:
            d.chmod(0o700)  # config.yaml may hold API keys
        except OSError:
            pass
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        name = f"papermap-backup-{stamp}" + ("" if kind == "auto" else f"-{kind}") + ".zip"
        target = d / name
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "papermap.db"
            _snapshot_db(registry, db)
            check = sqlite3.connect(db)
            try:
                if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("backup copy failed the integrity check")
                counts = _counts(check)
            finally:
                check.close()
            manifest = {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind,
                        "counts": counts, "knowledge_dir": str(config.knowledge_dir)}
            partial = target.with_suffix(".partial")
            with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(db, "papermap.db")
                if config.path and Path(config.path).exists():
                    z.write(config.path, "config.yaml")
                tax = config.data_dir / "taxonomy.yaml"
                if tax.exists():
                    z.write(tax, "taxonomy.yaml")
                z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
            os.chmod(partial, 0o600)
            os.replace(partial, target)
        prune(config)
        return target


def prune(config) -> int:
    keep = settings(config)["keep"]
    auto = [b for b in list_backups(config) if b["kind"] == "auto"]
    removed = 0
    for b in auto[keep:]:
        try:
            b["path"].unlink()
            removed += 1
        except OSError:
            pass
    return removed


def due(config) -> bool:
    s = settings(config)
    if not s["enabled"]:
        return False
    auto = [b for b in list_backups(config) if b["kind"] == "auto"]
    if not auto:
        return True
    return (datetime.now(timezone.utc) - auto[0]["when"]).total_seconds() >= s["every_hours"] * 3600 - 60


def run_scheduler(config, registry: Registry, stop: threading.Event | None = None, interval_s: float = 1800) -> None:
    """Back up at startup when due, then check every half hour (catches up after sleep/shutdown)."""
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            if due(config):
                path = create(config, registry)
                registry.log("info", f"Backup saved: {path.name}")
        except Exception as exc:  # noqa: BLE001 - never crash the app over a backup
            log.exception("backup failed")
            registry.log("error", f"Automatic backup failed: {exc}")
        stop.wait(interval_s)


def _resolve(config, name: str) -> Path:
    if not NAME_RE.match(name or ""):
        raise ValueError("备份文件名无效")
    p = backup_dir(config) / name
    if not p.is_file():
        raise ValueError("找不到这个备份")
    return p


def restore(config, registry: Registry, name: str, pipeline_lock=None) -> dict[str, Any]:
    """Replace the database and settings with a backup's (knowledge/ is left alone).
    A 'before-restore' backup is taken first, so a restore can be undone."""
    src = _resolve(config, name)
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(src) as z:
            names = set(z.namelist())
            if "papermap.db" not in names:
                raise ValueError("这个备份里没有数据库")
            z.extractall(tmp, members=[n for n in ("papermap.db", "config.yaml", "taxonomy.yaml") if n in names])
        db = Path(tmp) / "papermap.db"
        conn = sqlite3.connect(db)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("备份里的数据库已损坏，没有恢复")
        finally:
            conn.close()
        undo = create(config, registry, kind="before-restore")
        hold = pipeline_lock or threading.Lock()
        with hold:
            source = sqlite3.connect(db)
            try:
                with registry._lock:  # noqa: SLF001
                    # Restoring an older library must not re-authorize a revoked MCP client.
                    credential = registry.conn.execute("SELECT * FROM mcp_read_credentials").fetchone()
                    source.backup(registry.conn)
                    registry.initialize_schema()
                    with registry.conn:
                        registry.conn.execute("DELETE FROM mcp_read_credentials")
                        if credential:
                            registry.conn.execute("INSERT INTO mcp_read_credentials(id,digest) VALUES (?,?)",
                                                  (credential["id"], credential["digest"]))
            finally:
                source.close()
            cfg_file = Path(tmp) / "config.yaml"
            if cfg_file.exists() and config.path:
                data = yaml.safe_load(cfg_file.read_text("utf-8")) or {}
                for key in MACHINE_KEYS:  # keep this computer's locations
                    data[key] = config.raw.get(key, data.get(key))
                for key, value in data.items():
                    config.raw[key] = value
                config.save()
            tax = Path(tmp) / "taxonomy.yaml"
            if tax.exists():
                (config.data_dir / "taxonomy.yaml").write_bytes(tax.read_bytes())
    registry.log("info", f"Restored database and settings from {name} (undo: {undo.name})")
    return {"undo": undo.name}


def check_library(config, registry: Registry) -> dict[str, Any]:
    """What the (restored) database can still reach in knowledge/ and the original files."""
    from .evidence import document_revision

    kd = config.knowledge_dir
    papers = registry.query("SELECT id, title, first_author, year, kb_path, status FROM papers "
                            "WHERE status NOT IN ('ignored')")
    res = {"papers": 0, "cards": 0, "pdfs": 0, "notes": 0, "missing": [],
           "evidence": 0, "evidence_ok": 0, "evidence_changed": 0, "evidence_missing": 0}
    pdf_of: dict[int, Path | None] = {}
    for p in papers:
        res["papers"] += 1
        card = (kd / p["kb_path"]).exists() if p["kb_path"] else False
        pdf = None
        if p["kb_path"] and p["kb_path"].count("/") >= 2 and library.pdf_path(kd, p["kb_path"]).exists():
            pdf = library.pdf_path(kd, p["kb_path"])
        else:
            pdf = next((Path(f) for f in registry.paper_file_paths(p["id"]) if Path(f).exists()), None)
        notes = bool(p["kb_path"]) and library.notes_path(kd, p["kb_path"]).exists()
        pdf_of[p["id"]] = pdf
        res["cards"] += card
        res["pdfs"] += pdf is not None
        res["notes"] += notes
        if p["status"] == "done" and (not card or pdf is None):
            res["missing"].append({"id": p["id"], "label": f"{p['first_author'] or '?'} {p['year'] or ''}".strip(),
                                   "title": p["title"] or "", "card": card, "pdf": pdf is not None})
    revisions: dict[Path, str] = {}
    for e in registry.query("SELECT paper_id, revision FROM evidence_entries"):
        res["evidence"] += 1
        pdf = pdf_of.get(e["paper_id"])
        if pdf is None:
            res["evidence_missing"] += 1
            continue
        if pdf not in revisions:
            try:
                revisions[pdf] = document_revision(pdf)
            except OSError:
                revisions[pdf] = ""
        res["evidence_ok" if revisions[pdf] == e["revision"] else "evidence_changed"] += 1
    return res
