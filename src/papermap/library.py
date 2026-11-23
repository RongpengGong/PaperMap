"""Per-paper folders in the knowledge base (CLAUDE.md §14.3).

    knowledge/papers/<stem>/
        <stem>.pdf            copy of the paper (APFS clone on macOS = no extra space)
        <stem>.md             generated card
        <stem>.notes.md       the user's own notes (never rewritten)
        <stem>.<extra>.md     translation / skill outputs (stage 5b)

Files in watched inbox folders are never moved or modified; they are cloned.
Files the user dropped into the app live in <data>/imported/ until the paper
is identified, then they are *moved* into the paper's folder.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .kb_writer import USER_BEGIN, USER_END, atomic_write
from .registry import Registry

log = logging.getLogger(__name__)

NOTES_TEMPLATE = {
    "zh": "# 我的笔记 · {title}\n\n（这个文件完全属于你，PaperMap 永远不会改动它。）\n\n## 与我相关的点\n\n{body}\n",
    "en": "# My notes · {title}\n\n(This file is yours; PaperMap never changes it.)\n\n## Relevance to me\n\n{body}\n",
}
_DEFAULT_HINTS = ("（在这里写你自己的笔记，程序更新卡片时不会改动这一节。）",
                  "(Your own notes go here; PaperMap never overwrites this section.)")


def stem_of(kb_path: str) -> str:
    return Path(kb_path).stem


def folder_of(knowledge_dir: Path, kb_path: str) -> Path:
    return (knowledge_dir / kb_path).parent


def pdf_path(knowledge_dir: Path, kb_path: str) -> Path:
    return folder_of(knowledge_dir, kb_path) / f"{stem_of(kb_path)}.pdf"


def notes_path(knowledge_dir: Path, kb_path: str) -> Path:
    return folder_of(knowledge_dir, kb_path) / f"{stem_of(kb_path)}.notes.md"


def ensure_notes(knowledge_dir: Path, kb_path: str, title: str, language: str, body: str = "") -> Path:
    p = notes_path(knowledge_dir, kb_path)
    if not p.exists():
        tpl = NOTES_TEMPLATE["en" if language == "en" else "zh"]
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(p, tpl.format(title=(title or "")[:120], body=body.strip()))
    return p


def clone_copy(src: Path, dst: Path) -> None:
    """Copy-on-write clone where the filesystem supports it (APFS), else a normal copy."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    if sys.platform == "darwin":
        r = subprocess.run(["/bin/cp", "-c", str(src), str(tmp)], capture_output=True)
        if r.returncode == 0:
            os.replace(tmp, dst)
            return
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def _is_within(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def place_pdfs(knowledge_dir: Path, imported_dir: Path, registry: Registry, paper_id: int) -> list[str]:
    """Put every distinct PDF of a paper into its folder. Returns notes for the log."""
    row = registry.get_paper(paper_id)
    if row is None or not row["kb_path"]:
        return []
    folder = folder_of(knowledge_dir, row["kb_path"])
    stem = stem_of(row["kb_path"])
    folder.mkdir(parents=True, exist_ok=True)
    files = registry.query("SELECT * FROM files WHERE paper_id=? ORDER BY id", (paper_id,))
    notes: list[str] = []
    for i, f in enumerate(files):
        target = folder / (f"{stem}.pdf" if i == 0 else f"{stem}-{i + 1}.pdf")
        paths = [Path(r["path"]) for r in registry.query("SELECT path FROM file_paths WHERE file_id=?", (f["id"],))]
        in_lib = [p for p in paths if _is_within(p, folder) and p.exists()]
        imported = [p for p in paths if _is_within(p, imported_dir) and p.exists()]
        outside = [p for p in paths if p.exists() and p not in in_lib and p not in imported]
        try:
            if not in_lib and not target.exists():
                if imported:  # our own managed copy: move it (no duplicate on disk)
                    os.replace(imported[0], target)
                    registry.move_file_path(f["id"], str(imported[0]), str(target))
                    imported = imported[1:]
                    notes.append(f"moved into {target.name}")
                elif outside:  # a watched inbox file: never touch it, clone instead
                    clone_copy(outside[0], target)
                    notes.append(f"copied into {target.name}")
            # leftover managed copies of the same bytes are redundant
            for extra in imported:
                extra.unlink(missing_ok=True)
                registry.execute("DELETE FROM file_paths WHERE file_id=? AND path=?", (f["id"], str(extra)))
        except OSError as exc:
            log.warning("could not place %s: %s", target, exc)
    return notes


def migrate_flat_layout(knowledge_dir: Path, registry: Registry, language: str) -> int:
    """Stage ≤4 layout papers/<stem>.md -> papers/<stem>/<stem>.md (+ notes file)."""
    moved = 0
    for row in registry.query("SELECT id, kb_path, title FROM papers WHERE kb_path LIKE 'papers/%'"):
        kb = row["kb_path"]
        if kb.count("/") != 1:
            continue
        stem = stem_of(kb)
        old = knowledge_dir / kb
        new_rel = f"papers/{stem}/{stem}.md"
        new = knowledge_dir / new_rel
        body = ""
        if old.exists():
            text = old.read_text("utf-8")
            m = re.search(re.escape(USER_BEGIN) + r"\n?(.*?)\n?" + re.escape(USER_END), text, re.DOTALL)
            if m and m.group(1).strip() not in _DEFAULT_HINTS:
                body = m.group(1).strip()
            new.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old, new)
        registry.update_paper(row["id"], kb_path=new_rel)
        ensure_notes(knowledge_dir, new_rel, row["title"] or "", language, body)
        moved += 1
    return moved


# ---------------------------------------------------------------- relocation
KB_MARKERS = ("papers", "fields", "handbooks", "concepts", "_pending")


def resolve_move_target(current: Path, chosen: Path) -> Path:
    """Where the knowledge base would live if the user picked ``chosen``.
    An empty (or missing) folder is used as is; a folder that already has other
    files gets a ``PaperMap`` subfolder so we never mix into e.g. a vault root."""
    chosen = chosen.expanduser()
    if not chosen.is_absolute():
        raise ValueError("请选择一个完整的文件夹路径")
    current = current.resolve()
    target = chosen if not chosen.exists() or not any(_visible(chosen)) else chosen / "PaperMap"
    t = target.resolve()
    if t == current:
        raise ValueError("知识库已经在这个位置")
    if t.is_relative_to(current) or current.is_relative_to(t):
        raise ValueError("新位置不能在当前知识库里面，也不能是它的上级文件夹")
    if target.exists() and any(_visible(target)):
        raise ValueError(f"{target} 已经有文件了，请选一个空文件夹")
    if target.exists() and target.is_file():
        raise ValueError(f"{target} 是一个文件")
    return target


def _visible(d: Path):
    return (x for x in d.iterdir() if not x.name.startswith("."))


def _count_files(d: Path) -> tuple[int, int]:
    n = size = 0
    for root, _dirs, files in os.walk(d):
        for f in files:
            n += 1
            size += os.path.getsize(os.path.join(root, f))
    return n, size


def move_knowledge(current: Path, target: Path) -> tuple[int, str]:
    """Move the whole knowledge folder to ``target`` (stored paths are relative,
    so nothing in the database changes). Same disk: one rename. Another disk or
    cloud folder: copy, verify file count and size, then delete the original."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.rmdir()  # empty (checked by resolve_move_target); rename needs it gone
    if not current.exists():
        target.mkdir(parents=True)
        return 0, "renamed"
    before = _count_files(current)
    try:
        os.rename(current, target)
        return before[0], "renamed"
    except OSError:
        pass  # different volume
    shutil.copytree(current, target, copy_function=shutil.copy2)
    after = _count_files(target)
    if after != before:
        shutil.rmtree(target, ignore_errors=True)
        raise OSError(f"复制后文件数量或大小不一致（{after[0]}/{before[0]}），已取消，原知识库保持不变")
    shutil.rmtree(current)
    return before[0], "copied"
