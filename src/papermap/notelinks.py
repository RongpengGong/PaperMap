"""[[links]] between notes, papers and fields; backlinks (CLAUDE.md §15.8)."""

from __future__ import annotations

import re
from pathlib import Path

from .registry import Registry

_LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


def suggest(registry: Registry, q: str, limit: int = 12) -> list[dict]:
    q = q.strip()
    like = f"%{q}%"
    out = []
    for p in registry.query(
            "SELECT id, title, first_author, year, journal, kb_path FROM papers WHERE kb_path IS NOT NULL AND "
            "(title LIKE ? OR first_author LIKE ? OR journal LIKE ? OR doi LIKE ?) ORDER BY id DESC LIMIT ?",
            (like, like, like, like, limit)):
        who = f"{p['first_author'] or '?'} {p['year'] or ''}".strip()
        out.append({"kind": "paper", "label": f"{who} · {p['title']}", "insert": f"[[{p['kb_path'][:-3]}|{who}]]"})
    for f in registry.query("SELECT slug, name_zh, name_en FROM fields WHERE status='active' AND "
                            "(name_zh LIKE ? OR name_en LIKE ? OR slug LIKE ?) LIMIT ?", (like, like, like, 6)):
        out.append({"kind": "field", "label": f"学科 · {f['name_zh']}", "insert": f"[[fields/{f['slug']}|{f['name_zh']}]]"})
    return out


def _targets(text: str) -> set[str]:
    return {m.group(1).strip() for m in _LINK.finditer(text)}


def backlinks(registry: Registry, knowledge_dir: Path, paper_id: int) -> list[dict]:
    """Notes (of other papers), Q&A and comparisons that link to this paper."""
    p = registry.get_paper(paper_id)
    if p is None or not p["kb_path"]:
        return []
    full = p["kb_path"][:-3]                     # papers/<stem>/<stem>
    stem = Path(full).name
    wanted = {full, stem, f"{stem}.notes"}
    owners = {r["kb_path"][:-3].split("/")[-1]: r for r in registry.query(
        "SELECT id, title, first_author, year, kb_path FROM papers WHERE kb_path IS NOT NULL")}
    out = []
    files = list((knowledge_dir / "papers").glob("*/*.notes.md")) + list((knowledge_dir / "qa").glob("*.md")) \
        + list((knowledge_dir / "comparisons").glob("*.md"))
    for f in files:
        if f.name == f"{stem}.notes.md":
            continue
        try:
            text = f.read_text("utf-8")
        except OSError:
            continue
        if not (_targets(text) & wanted):
            continue
        m = next(m for m in _LINK.finditer(text) if m.group(1).strip() in wanted)
        snippet = " ".join(text[max(0, m.start() - 80):m.end() + 80].split())
        if f.name.endswith(".notes.md"):
            owner = owners.get(f.name[:-len(".notes.md")])
            out.append({"kind": "notes", "paper_id": owner["id"] if owner else None,
                        "label": f"{owner['first_author'] or '?'} {owner['year'] or ''} 的笔记" if owner else f.name,
                        "snippet": snippet})
        else:
            out.append({"kind": f.parent.name, "paper_id": None, "label": f"{'问答' if f.parent.name == 'qa' else '对比'} · {f.stem}",
                        "snippet": snippet, "path": f"{f.parent.name}/{f.name}"})
    return out
