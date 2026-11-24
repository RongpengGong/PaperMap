"""Full-text search over the whole library (CLAUDE.md §15.1).

One FTS5 table (trigram tokenizer: works for English and Chinese substrings)
holds PDF pages, cards, notes, annotations, translations and skill outputs.
Queries shorter than 3 characters (e.g. 2-character Chinese terms) fall back
to LIKE, since trigram matching needs at least three characters.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pymupdf

from . import library, ocr
from .registry import Registry

KINDS = {"page": "正文", "card": "卡片", "notes": "笔记", "annotation": "批注", "translation": "译文", "skill": "技能解读"}

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    paper_id UNINDEXED, kind UNINDEXED, page UNINDEXED, label UNINDEXED, body,
    tokenize='trigram'
);
CREATE TABLE IF NOT EXISTS search_state (paper_id INTEGER PRIMARY KEY, signature TEXT NOT NULL);
"""


def ensure_schema(registry: Registry) -> None:
    with registry._lock:  # noqa: SLF001
        registry.conn.executescript(SCHEMA)
        registry.conn.commit()


def _strip_front(text: str) -> str:
    return re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.S)


def _docs(knowledge_dir: Path, registry: Registry, row, pdf: Path | None) -> list[tuple[str, int | None, str, str]]:
    """(kind, page, label, text) for one paper."""
    docs: list[tuple[str, int | None, str, str]] = []
    if pdf is not None and pdf.exists():
        try:
            cache = ocr.cache_for(pdf, registry.db_path.parent)
            with pymupdf.open(pdf) as doc:
                auto = ocr.scanned_document(doc)
                for i, page in enumerate(doc):
                    t = " ".join(ocr.page_text(page, cache, auto).split())
                    if t:
                        docs.append(("page", i, f"p.{i + 1}", t))
        except Exception:  # noqa: BLE001 - a broken PDF must not stop indexing
            pass
    if row["kb_path"]:
        card = knowledge_dir / row["kb_path"]
        if card.exists():
            docs.append(("card", None, "卡片", _strip_front(card.read_text("utf-8"))))
        if row["kb_path"].count("/") >= 2:
            notes = library.notes_path(knowledge_dir, row["kb_path"])
            if notes.exists():
                docs.append(("notes", None, "我的笔记", notes.read_text("utf-8")))
            tj = card.with_name(f"{card.stem}.translation.json")
            if tj.exists():
                try:
                    paras = json.loads(tj.read_text("utf-8"))["paragraphs"]
                    docs.append(("translation", None, "译文", "\n".join(p.get("tr", "") for p in paras)))
                except (ValueError, KeyError):
                    pass
            for f in card.parent.glob(f"{card.stem}.*.md"):
                kind = f.name[len(card.stem) + 1:-3]
                if kind not in ("notes", "translation", "annotations"):
                    docs.append(("skill", None, kind, _strip_front(f.read_text("utf-8"))))
    for a in registry.annotations(row["id"]):
        docs.append(("annotation", a["page"], f"p.{a['page'] + 1} 批注", f"{a['text'] or ''}\n{a['note'] or ''}"))
    return docs


def _signature(knowledge_dir: Path, registry: Registry, row, pdf: Path | None) -> str:
    parts = [str(row["updated_at"]), str(len(registry.annotations(row["id"])))]
    for p in ([pdf] if pdf else []):
        if p.exists():
            cache = ocr.cache_for(p, registry.db_path.parent)
            parts.append(f"{ocr.CACHE_VERSION}:{cache.name}")
            if cache.exists():
                parts.append(str(cache.stat().st_mtime_ns))
    if row["kb_path"]:
        folder = (knowledge_dir / row["kb_path"]).parent
        if folder.exists():
            parts += [f"{f.name}:{f.stat().st_mtime}" for f in sorted(folder.glob("*.md")) + sorted(folder.glob("*.json"))]
    parts += [str(a["updated_at"]) for a in registry.annotations(row["id"])]
    return "|".join(parts)


def index_paper(knowledge_dir: Path, registry: Registry, paper_id: int, pdf: Path | None, force: bool = False) -> bool:
    """(Re)index one paper if anything changed. Returns True when it was indexed."""
    ensure_schema(registry)
    row = registry.get_paper(paper_id)
    if row is None:
        return False
    sig = _signature(knowledge_dir, registry, row, pdf)
    old = registry.one("SELECT signature FROM search_state WHERE paper_id=?", (paper_id,))
    if old is not None and old["signature"] == sig and not force:
        return False
    cache = ocr.cache_for(pdf, registry.db_path.parent) if pdf and pdf.is_file() else None
    before = cache.stat().st_mtime_ns if cache and cache.exists() else None
    docs = _docs(knowledge_dir, registry, row, pdf)
    with registry._lock, registry.conn:  # noqa: SLF001
        # A reader may have recognized a page after _docs had already read it.
        # Reconcile inside the same transaction used by index_ocr_page.
        if cache and cache.exists() and cache.stat().st_mtime_ns != before:
            changed_pages = {}
            for path in cache.glob("*.json"):
                if path.stem.isdecimal() and (before is None or path.stat().st_mtime_ns >= before):
                    found = ocr.cached_words(path)
                    if found:
                        page = int(path.stem)
                        changed_pages[page] = ("page", page, f"p.{page+1}", " ".join(ocr.lines(found)))
            docs = [d for d in docs if d[0] != "page" or d[1] not in changed_pages]
            docs.extend(changed_pages.values())
        registry.conn.execute("DELETE FROM search_index WHERE paper_id=?", (paper_id,))
        registry.conn.executemany(
            "INSERT INTO search_index(paper_id, kind, page, label, body) VALUES (?,?,?,?,?)",
            [(paper_id, k, p, lbl, body) for k, p, lbl, body in docs])
        registry.conn.execute("INSERT OR REPLACE INTO search_state(paper_id, signature) VALUES (?,?)", (paper_id, sig))
    return True


def index_ocr_page(registry: Registry, paper_id: int, page: int, words: list) -> None:
    """Make a scan searchable immediately, including papers without a model card."""
    ensure_schema(registry)
    body = " ".join(ocr.lines(words))
    with registry._lock, registry.conn:
        key = (paper_id, page)
        previous = registry.conn.execute(
            "SELECT body FROM search_index WHERE paper_id=? AND page=? AND kind='page'", key).fetchone()
        if previous is not None and previous["body"] == body:
            return
        registry.conn.execute("DELETE FROM search_index WHERE paper_id=? AND page=? AND kind='page'", key)
        registry.conn.execute("INSERT INTO search_index(paper_id,kind,page,label,body) VALUES (?,'page',?,?,?)",
                              (paper_id, page, f"p.{page+1}", body))


def drop_paper(registry: Registry, paper_id: int) -> None:
    ensure_schema(registry)
    registry.execute("DELETE FROM search_index WHERE paper_id=?", (paper_id,))
    registry.execute("DELETE FROM search_state WHERE paper_id=?", (paper_id,))


def snippet(text: str, terms: list[str], width: int = 90) -> str:
    low = text.lower()
    pos = min((i for i in (low.find(t.lower()) for t in terms) if i >= 0), default=0)
    start = max(0, pos - width)
    s = (("…" if start else "") + text[start:pos + width * 2] + ("…" if pos + width * 2 < len(text) else ""))
    return " ".join(s.split())


def query_terms(q: str) -> list[str]:
    """Split on whitespace; "quoted phrases" stay together."""
    terms = [a or b for a, b in re.findall(r'"([^"]+)"|(\S+)', q)]
    return [t for t in terms if t.strip()]


def search(registry: Registry, q: str, kinds: set[str] | None = None, paper_ids: list[int] | None = None,
           limit: int = 300, any_term: bool = False, terms: list[str] | None = None, *,
           offset: int = 0, visible_only: bool = False) -> list[dict]:
    """Hits for all terms (AND; ``any_term`` = OR). Each: paper_id, kind, page, label, snippet."""
    if not registry.one("SELECT 1 FROM sqlite_master WHERE name='search_index' AND type='table'"):
        return []
    terms = terms if terms is not None else query_terms(q)
    if not terms:
        return []
    where, params = [], []
    long_terms = [t for t in terms if len(t) >= 3]
    short_terms = [t for t in terms if len(t) < 3]
    conds, cparams = [], []
    if long_terms:
        conds.append("search_index MATCH ?")
        cparams.append((" OR " if any_term else " AND ").join('"' + t.replace('"', '""') + '"' for t in long_terms))
    for t in short_terms:
        conds.append("body LIKE ?")
        cparams.append(f"%{t}%")
    where.append("(" + (" OR " if any_term else " AND ").join(conds) + ")")
    params += cparams
    if kinds:
        where.append(f"kind IN ({','.join('?' * len(kinds))})")
        params += sorted(kinds)
    if paper_ids:
        where.append(f"paper_id IN ({','.join('?' * len(paper_ids))})")
        params += paper_ids
    if visible_only:
        where.append("paper_id IN (SELECT id FROM papers WHERE status != 'ignored')")
    order = "ORDER BY rank,rowid" if long_terms else "ORDER BY rowid"
    rows = registry.query(
        f"SELECT paper_id, kind, page, label, body FROM search_index WHERE {' AND '.join(where)} {order} LIMIT ? OFFSET ?",
        (*params, limit, offset))
    return [{"paper_id": int(r["paper_id"]), "kind": r["kind"], "page": r["page"], "label": r["label"],
             "snippet": snippet(r["body"], terms), "body": r["body"]} for r in rows]


def grouped(registry: Registry, hits: list[dict]) -> list[dict]:
    """Group hits by paper, papers ordered by number of hits."""
    by: dict[int, list[dict]] = {}
    for h in hits:
        by.setdefault(h["paper_id"], []).append(h)
    out = []
    for pid, hs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        p = registry.get_paper(pid)
        if p is None:
            continue
        hs.sort(key=lambda h: (list(KINDS).index(h["kind"]) if h["kind"] in KINDS else 9, h["page"] or 0))
        out.append({"paper": p, "hits": hs})
    return out
