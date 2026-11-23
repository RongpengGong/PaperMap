"""Import from a local Zotero library (CLAUDE.md §15.10).

Reads a *copy* of zotero.sqlite (Zotero may be running; its data is never
modified). PDFs go through the normal pipeline (cloned into the library);
collections become PaperMap groups under "Zotero"; tags and notes are kept.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Callable

from . import library
from .kb_writer import atomic_write
from .registry import Registry, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS zotero_notes (
    note_key TEXT PRIMARY KEY, paper_id INTEGER NOT NULL, markdown TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS zotero_collections (zotero_id INTEGER PRIMARY KEY, collection_id INTEGER NOT NULL);
"""


def ensure_schema(registry: Registry) -> None:
    with registry._lock:  # noqa: SLF001
        registry.conn.executescript(SCHEMA)
        registry.conn.commit()


def default_dir() -> Path:
    return Path.home() / "Zotero"


def _open_copy(zdir: Path) -> tuple[sqlite3.Connection, Path]:
    src = zdir / "zotero.sqlite"
    if not src.exists():
        raise FileNotFoundError(f"没有在 {zdir} 找到 zotero.sqlite")
    tmp = Path(tempfile.mkdtemp(prefix="papermap-zotero-"))
    shutil.copy2(src, tmp / "zotero.sqlite")
    for ext in ("-wal", "-shm"):
        if (zdir / f"zotero.sqlite{ext}").exists():
            shutil.copy2(zdir / f"zotero.sqlite{ext}", tmp / f"zotero.sqlite{ext}")
    con = sqlite3.connect(tmp / "zotero.sqlite")
    con.row_factory = sqlite3.Row
    return con, tmp


def html_to_md(s: str) -> str:
    s = re.sub(r"(?i)<br\s*/?>", "\n", s or "")
    s = re.sub(r"(?i)</p>|</div>|</h\d>", "\n\n", s)
    s = re.sub(r"(?i)<h(\d)[^>]*>", lambda m: "#" * min(6, int(m.group(1)) + 1) + " ", s)
    s = re.sub(r"(?i)<li[^>]*>", "- ", s)
    s = re.sub(r"(?i)<(strong|b)>(.*?)</\1>", r"**\2**", s)
    s = re.sub(r"(?i)<(em|i)>(.*?)</\1>", r"*\2*", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def scan(zdir: Path) -> dict:
    """Everything needed for an import (and a preview), read from a copy."""
    con, tmp = _open_copy(zdir)
    try:
        def field(item_id: int, name: str) -> str:
            r = con.execute("SELECT v.value FROM itemData d JOIN fields f USING(fieldID) JOIN itemDataValues v USING(valueID) "
                            "WHERE d.itemID=? AND f.fieldName=?", (item_id, name)).fetchone()
            return r[0] if r else ""

        items = []
        for it in con.execute(
                "SELECT i.itemID, i.key FROM items i JOIN itemTypes t USING(itemTypeID) "
                "WHERE t.typeName NOT IN ('attachment','note','annotation') "
                "AND i.itemID NOT IN (SELECT itemID FROM deletedItems)"):
            iid = it["itemID"]
            pdfs = []
            for a in con.execute("SELECT a.path, a.linkMode, i2.key FROM itemAttachments a JOIN items i2 ON i2.itemID=a.itemID "
                                 "WHERE a.parentItemID=? AND a.contentType='application/pdf' "
                                 "AND a.itemID NOT IN (SELECT itemID FROM deletedItems)", (iid,)):
                path = a["path"] or ""
                if path.startswith("storage:"):
                    p = zdir / "storage" / a["key"] / path[len("storage:"):]
                elif path and os.path.isabs(path):
                    p = Path(path)
                else:
                    continue
                if p.exists():
                    pdfs.append(str(p))
            notes = [{"key": n["key"], "markdown": html_to_md(n["note"])} for n in con.execute(
                "SELECT i2.key, n.note FROM itemNotes n JOIN items i2 ON i2.itemID=n.itemID WHERE n.parentItemID=? "
                "AND n.itemID NOT IN (SELECT itemID FROM deletedItems)", (iid,)) if (n["note"] or "").strip()]
            items.append({
                "key": it["key"], "title": field(iid, "title"), "doi": field(iid, "DOI"), "date": field(iid, "date"),
                "journal": field(iid, "publicationTitle"), "pdfs": pdfs, "notes": notes,
                "tags": [r[0] for r in con.execute("SELECT t.name FROM itemTags it JOIN tags t USING(tagID) WHERE it.itemID=?", (iid,))],
                "collections": [r[0] for r in con.execute("SELECT collectionID FROM collectionItems WHERE itemID=?", (iid,))],
            })
        collections = {r["collectionID"]: {"name": r["collectionName"], "parent": r["parentCollectionID"]}
                       for r in con.execute("SELECT collectionID, collectionName, parentCollectionID FROM collections")}
        return {"items": items, "collections": collections}
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)


def preview(zdir: Path) -> dict:
    d = scan(zdir)
    with_pdf = [i for i in d["items"] if i["pdfs"]]
    return {"dir": str(zdir), "items": len(d["items"]), "with_pdf": len(with_pdf),
            "without_pdf": [{"title": i["title"], "doi": i["doi"]} for i in d["items"] if not i["pdfs"]],
            "collections": len(d["collections"]), "tags": len({t for i in d["items"] for t in i["tags"]}),
            "notes": sum(len(i["notes"]) for i in d["items"])}


def _map_collections(registry: Registry, cols: dict) -> dict[int, int]:
    """Zotero collection id -> PaperMap group id (recreated under 'Zotero', idempotent)."""
    known = {r["zotero_id"]: r["collection_id"] for r in registry.query("SELECT * FROM zotero_collections")}
    root = registry.one("SELECT id FROM collections WHERE name='Zotero' AND parent_id IS NULL")
    root_id = root["id"] if root else registry.create_collection("Zotero")

    def ensure(zid: int) -> int:
        if zid in known and registry.one("SELECT 1 FROM collections WHERE id=?", (known[zid],)):
            return known[zid]
        c = cols[zid]
        parent = ensure(c["parent"]) if c["parent"] in cols else root_id
        gid = registry.create_collection(c["name"], parent)
        registry.execute("INSERT OR REPLACE INTO zotero_collections VALUES (?,?)", (zid, gid))
        known[zid] = gid
        return gid

    return {zid: ensure(zid) for zid in cols}


def apply_pending_notes(registry: Registry, knowledge_dir: Path, paper_id: int) -> int:
    """Append imported Zotero notes to the paper's notes file once it exists."""
    ensure_schema(registry)
    row = registry.get_paper(paper_id)
    if row is None or not row["kb_path"] or row["kb_path"].count("/") < 2:
        return 0
    pending = registry.query("SELECT * FROM zotero_notes WHERE paper_id=? AND applied=0", (paper_id,))
    if not pending:
        return 0
    path = library.notes_path(knowledge_dir, row["kb_path"])
    text = path.read_text("utf-8") if path.exists() else ""
    for n in pending:
        text = text.rstrip() + f"\n\n## 来自 Zotero 的笔记\n\n{n['markdown']}\n"
        registry.execute("UPDATE zotero_notes SET applied=1 WHERE note_key=?", (n["note_key"],))
    atomic_write(path, text)
    return len(pending)


def run_import(pipeline, zdir: Path, collections: bool = True, tags: bool = True, notes: bool = True,
               progress: Callable[[int, int], None] | None = None) -> dict:
    """Register Zotero PDFs (LLM work is held for the usual bulk confirmation)."""
    from .pdf_extract import sha256_file

    registry = pipeline.registry
    ensure_schema(registry)
    d = scan(zdir)
    items = [i for i in d["items"] if i["pdfs"]]
    gmap = _map_collections(registry, d["collections"]) if collections else {}
    pipeline.hold = True
    added = linked = 0
    for n, it in enumerate(items, 1):
        pdf = Path(it["pdfs"][0])
        outcome = pipeline.handle_path(pdf)
        f = registry.file_by_sha(sha256_file(pdf))
        if f is None or not f["paper_id"]:
            if progress:
                progress(n, len(items))
            continue
        pid = f["paper_id"]
        added += outcome not in ("known", "duplicate_file", "duplicate_doi", "duplicate_title", "same_work")
        linked += 1
        cols: dict = {"zotero_key": it["key"]}
        if tags and it["tags"]:
            row = registry.get_paper(pid)
            old = json.loads(row["tags_json"] or "[]")
            cols["tags_json"] = json.dumps(sorted(set(old) | set(it["tags"])), ensure_ascii=False)
        registry.update_paper(pid, **cols)
        for zc in it["collections"]:
            if zc in gmap:
                registry.add_to_collection(gmap[zc], [pid])
        if notes:
            for note in it["notes"]:
                registry.execute("INSERT OR IGNORE INTO zotero_notes VALUES (?,?,?,0,?)",
                                 (note["key"], pid, note["markdown"], now_iso()))
            apply_pending_notes(registry, pipeline.config.knowledge_dir, pid)
        pipeline._refresh_card(pid)  # noqa: SLF001
        if progress:
            progress(n, len(items))
    threshold = int(pipeline.config.raw.get("bulk_confirm_threshold", 5))
    waiting = pipeline.pending_count()
    if waiting < threshold:
        pipeline.hold = False
        pipeline.process_pending()
    registry.log("info", f"Zotero import: {linked} PDFs linked ({added} new), {len(d['items']) - len(items)} items without PDF")
    return {"linked": linked, "new": added, "without_pdf": len(d["items"]) - len(items), "waiting": waiting}
