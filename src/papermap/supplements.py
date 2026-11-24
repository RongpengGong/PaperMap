"""User-attached PDF supplements, stored beside the paper card and addressed by content."""
from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

import pymupdf

from . import library
from .evidence import document_revision
from .pdf_import import staged_file
from .registry import Registry

MAX_SIZE = 200 * 1024 * 1024


def file_path(knowledge_dir: Path, registry: Registry, paper_id: int, supplement_id: int) -> Path | None:
    row = registry.one("SELECT s.revision,p.kb_path FROM paper_supplements s JOIN papers p ON s.paper_id=p.id "
                       "WHERE s.id=? AND p.id=? AND p.status!='ignored'", (supplement_id, paper_id))
    if not row or not row["kb_path"]:
        return None
    folder = library.folder_of(knowledge_dir, row["kb_path"])
    path = folder / "supplements" / (row["revision"] + ".pdf")
    if not library._is_within(path, knowledge_dir):
        return None
    try:
        return path if path.is_file() and document_revision(path) == row["revision"] else None
    except OSError:
        return None


def listed(registry: Registry, paper_id: int) -> list:
    return registry.query("SELECT id,name FROM paper_supplements WHERE paper_id=? ORDER BY id", (paper_id,))


def attach(knowledge_dir: Path, registry: Registry, paper_id: int, main_pdf: Path, filename: str,
           stream: BinaryIO) -> int:
    row = registry.get_paper(paper_id)
    if not row or row["status"] != "done" or not row["kb_path"] or row["kb_path"].count("/") < 2:
        raise ValueError("请先完成主论文的导入。")
    folder = library.folder_of(knowledge_dir, row["kb_path"]) / "supplements"
    if not library._is_within(folder, knowledge_dir):
        raise ValueError("论文文件夹不可用。")
    name = Path(filename.replace("\\", "/")).name.strip()[:250]
    if not name.lower().endswith(".pdf"):
        raise ValueError("请选择 PDF 格式的补充材料。")
    with staged_file(folder) as tmp:
        with tmp.open("wb") as out:
            while chunk := stream.read(1024 * 1024):
                if out.tell() + len(chunk) > MAX_SIZE:
                    raise ValueError("补充 PDF 超过 200 MB。")
                out.write(chunk)
        try:
            # validate from memory: no file handle stays open, so Windows can move or delete the file
            with pymupdf.open(stream=tmp.read_bytes(), filetype="pdf") as doc:
                if not doc.is_pdf or doc.needs_pass or not doc.page_count:
                    raise ValueError
        except (RuntimeError, ValueError, pymupdf.FileDataError) as exc:
            raise ValueError("无法读取这个 PDF，请检查文件是否损坏或加密。") from exc
        revision = document_revision(tmp)
        if revision == document_revision(main_pdf) or registry.one(
                "SELECT 1 FROM files WHERE paper_id=? AND sha256=?", (paper_id, revision)):
            raise ValueError("这是主论文已有的 PDF。")
        dest = folder / (revision + ".pdf")
        with registry.transaction() as conn:
            current = conn.execute("SELECT status,kb_path FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not current or current["status"] != "done" or current["kb_path"] != row["kb_path"]:
                raise ValueError("论文文件夹已变更，请重新打开论文。")
            if not dest.is_file() or document_revision(dest) != revision:
                os.replace(tmp, dest)
            conn.execute("INSERT INTO paper_supplements(paper_id,name,revision) VALUES (?,?,?) "
                         "ON CONFLICT(paper_id,revision) DO NOTHING", (paper_id, name, revision))
            return conn.execute("SELECT id FROM paper_supplements WHERE paper_id=? AND revision=?",
                                (paper_id, revision)).fetchone()["id"]
