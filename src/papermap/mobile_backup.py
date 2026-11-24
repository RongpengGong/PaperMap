"""Copies of papers imported on a paired phone, kept on the computer so the phone can restore them.

Backups live in ``<data>/phone_backup/<revision>/`` (``paper.pdf`` plus ``meta.json`` with the phone's
title, excerpts, marks, ink and discussion summaries). They are not part of the library: nothing is
imported, indexed or sent to a model. A PDF is only kept after its SHA-256 matches the revision.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

MAX_PDF = 256 * 1024 * 1024
MAX_TOTAL = 5 * 1024 * 1024 * 1024
MAX_META = 8 * 1024 * 1024
CHUNK = 1024 * 1024
_SHA = re.compile(r"[a-f0-9]{64}")
_lock = threading.Lock()


class BackupError(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def root(config) -> Path:
    return Path(config.data_dir) / "phone_backup"


def _folder(config, revision) -> Path:
    if not isinstance(revision, str) or not _SHA.fullmatch(revision):
        raise BackupError(400, "revision_invalid")
    return root(config) / revision


def _used(config, skip: Path | None = None) -> int:
    total = 0
    base = root(config)
    if not base.is_dir():
        return 0
    for f in base.rglob("*"):
        if f.is_file() and (skip is None or skip not in f.parents):
            total += f.stat().st_size
    return total


def listing(config) -> list[dict]:
    out = []
    base = root(config)
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        pdf, meta = d / "paper.pdf", d / "meta.json"
        if not (_SHA.fullmatch(d.name) and pdf.is_file() and meta.is_file()):
            continue
        try:
            info = json.loads(meta.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"revision": d.name, "title": str(info.get("title") or "PDF")[:400],
                    "byteSize": pdf.stat().st_size, "pageCount": int(info.get("pageCount") or 0),
                    "at": str(info.get("at") or ""), "metaVersion": hashlib.sha256(meta.read_bytes()).hexdigest()[:16]})
    return out


def summary(config) -> dict:
    items = listing(config)
    return {"count": len(items), "bytes": sum(i["byteSize"] for i in items), "path": str(root(config))}


def begin(config, revision, size) -> dict:
    if type(size) is not int or not 0 < size <= MAX_PDF:
        raise BackupError(413, "pdf_size_invalid")
    folder = _folder(config, revision)
    with _lock:
        if (folder / "paper.pdf").is_file() and (folder / "paper.pdf").stat().st_size == size:
            return {"offset": size, "done": True}  # already backed up: only the notes are refreshed
        if _used(config, folder) + size > MAX_TOTAL:
            raise BackupError(507, "storage_full")
        folder.mkdir(parents=True, exist_ok=True)
        part = folder / "paper.pdf.part"
        if part.is_file() and part.stat().st_size <= size:
            return {"offset": part.stat().st_size}
        part.write_bytes(b"")
        return {"offset": 0}


def chunk(config, revision, offset, data, size) -> dict:
    folder = _folder(config, revision)
    with _lock:
        part = folder / "paper.pdf.part"
        if not part.is_file():
            raise BackupError(409, "begin_upload")
        current = part.stat().st_size
        if not 0 < len(data) <= CHUNK or offset != current or (size and offset + len(data) > size):
            raise BackupError(409, "offset_mismatch")
        with part.open("ab") as f:
            f.write(data)
        return {"offset": part.stat().st_size}


def finish(config, revision, meta: dict) -> dict:
    """Keep the uploaded PDF if it matches the revision, then write the phone's notes beside it."""
    if not isinstance(meta, dict):
        raise BackupError(400, "meta_invalid")
    folder = _folder(config, revision)
    encoded = json.dumps({**meta, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                         ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_META:
        raise BackupError(413, "meta_too_large")
    with _lock:
        pdf, part = folder / "paper.pdf", folder / "paper.pdf.part"
        if part.is_file():
            digest = hashlib.sha256()
            with part.open("rb") as f:
                for block in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != revision:
                part.unlink(missing_ok=True)
                raise BackupError(409, "checksum_mismatch")
            os.replace(part, pdf)
        if not pdf.is_file():
            raise BackupError(409, "begin_upload")
        temp = folder / "meta.json.tmp"
        temp.write_bytes(encoded)
        os.replace(temp, folder / "meta.json")
    return {"ok": True}


def pdf_file(config, revision) -> Path:
    path = _folder(config, revision) / "paper.pdf"
    if not path.is_file():
        raise LookupError(revision)
    return path


def meta(config, revision) -> dict:
    path = _folder(config, revision) / "meta.json"
    if not path.is_file():
        raise LookupError(revision)
    return json.loads(path.read_text("utf-8"))


def remove(config, revision) -> dict:
    folder = _folder(config, revision)
    with _lock:
        if folder.is_dir():
            shutil.rmtree(folder)
    return {"ok": True}
