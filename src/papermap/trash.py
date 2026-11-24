"""Delete a paper the user no longer wants, and restore it.

Deleting never destroys data: the paper's folder (PDF copy, card, notes, translation, chat) is
moved to ``<data_dir>/trash/``, its field and group memberships are saved next to it, and the
database row stays with status ``ignored`` so the same PDF or DOI found again in an inbox
folder is not imported a second time. Restoring moves everything back.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from . import search
from .registry import Registry

RESTORE_FILE = "_papermap_restore.json"


def trash_dir(data_dir: Path) -> Path:
    return data_dir / "trash"


def delete_paper(pipeline, paper_id: int) -> dict:
    reg: Registry = pipeline.registry
    config = pipeline.config
    row = reg.get_paper(paper_id)
    if row is None:
        raise ValueError("找不到这篇论文")
    if row["status"] == "ignored" and row["trash_path"]:
        raise ValueError("这篇论文已经删除了")
    fields = [dict(r) for r in reg.query(
        "SELECT pf.*, f.slug FROM paper_fields pf JOIN fields f ON f.id=pf.field_id WHERE pf.paper_id=?", (paper_id,))]
    colls = [[r["collection_id"], r["added_at"]] for r in reg.query(
        "SELECT collection_id, added_at FROM paper_collections WHERE paper_id=?", (paper_id,))]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = trash_dir(config.data_dir) / f"{stamp}_{paper_id}"
    dest.mkdir(parents=True, exist_ok=True)
    moved = ""
    if row["kb_path"]:
        card = config.knowledge_dir / row["kb_path"]
        src = card.parent if row["kb_path"].count("/") >= 2 else card  # per-paper folder, or a flat old card
        if src.exists():
            shutil.move(str(src), str(dest / src.name))
            moved = src.name
    (dest / RESTORE_FILE).write_text(json.dumps({
        "paper_id": paper_id, "title": row["title"], "kb_path": row["kb_path"], "moved": moved,
        "status": row["status"], "fields": fields, "collections": colls, "deleted_at": stamp,
    }, ensure_ascii=False, indent=1), "utf-8")
    reg.execute("DELETE FROM paper_fields WHERE paper_id=?", (paper_id,))
    reg.execute("DELETE FROM paper_collections WHERE paper_id=?", (paper_id,))
    reg.execute("UPDATE jobs SET status='error', message='paper deleted' WHERE paper_id=? AND status='queued'", (paper_id,))
    search.drop_paper(reg, paper_id)
    reg.update_paper(paper_id, status="ignored", status_detail="Deleted by user",
                     deleted_status=row["status"], trash_path=str(dest.relative_to(config.data_dir)))
    reg.log("info", f"Deleted paper (moved to {dest})", paper_id)
    _refresh(pipeline, {f["slug"] for f in fields})
    pipeline.kb_commit(f"Delete paper {row['title'] or paper_id}")
    return {"trash": str(dest)}


def restore_paper(pipeline, paper_id: int) -> None:
    reg: Registry = pipeline.registry
    config = pipeline.config
    row = reg.get_paper(paper_id)
    if row is None or not row["trash_path"]:
        raise ValueError("这篇论文不在回收站里")
    src = config.data_dir / row["trash_path"]
    info = json.loads((src / RESTORE_FILE).read_text("utf-8"))
    if info.get("moved") and row["kb_path"]:
        card = config.knowledge_dir / row["kb_path"]
        target = card.parent if row["kb_path"].count("/") >= 2 else card
        if target.exists():
            raise ValueError(f"原位置已有同名文件夹：{target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src / info["moved"]), str(target))
    slugs = set()
    for f in info.get("fields", []):
        fid = reg.one("SELECT id FROM fields WHERE slug=?", (f["slug"],))
        if not fid:
            continue
        cols = [k for k in f if k not in ("slug", "paper_id", "field_id")]
        reg.execute(f"INSERT OR IGNORE INTO paper_fields(paper_id, field_id{''.join(', ' + c for c in cols)}) "
                    f"VALUES (?, ?{', ?' * len(cols)})", (paper_id, fid["id"], *[f[c] for c in cols]))
        slugs.add(f["slug"])
    for cid, added in info.get("collections", []):
        if reg.one("SELECT 1 FROM collections WHERE id=?", (cid,)):
            reg.execute("INSERT OR IGNORE INTO paper_collections(paper_id, collection_id, added_at) VALUES (?, ?, ?)",
                        (paper_id, cid, added))
    reg.update_paper(paper_id, status=row["deleted_status"] or info.get("status") or "done", status_detail="",
                     trash_path=None, deleted_status=None)
    shutil.rmtree(src, ignore_errors=True)
    reg.log("info", "Restored paper from trash", paper_id)
    try:
        pipeline.reindex(paper_id, force=True)
    except Exception:  # noqa: BLE001 - search index catches up on the next maintenance pass
        pass
    _refresh(pipeline, slugs)
    pipeline.kb_commit(f"Restore paper {row['title'] or paper_id}")


def _refresh(pipeline, slugs: set[str]) -> None:
    try:
        if slugs:
            pipeline.refresh_fields(slugs)
        pipeline.refresh_concepts()
    except Exception as exc:  # noqa: BLE001 - pages are rebuilt again on the next change
        pipeline.registry.log("warning", f"Could not refresh pages after delete/restore: {exc}")
