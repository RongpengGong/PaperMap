"""PDF reader backend (CLAUDE.md §14.7, stage 5c) built on PyMuPDF only:
page images, a word layer for selection, search, outline and annotations."""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from datetime import date
from pathlib import Path

import pymupdf
import yaml

from .kb_writer import atomic_write
from .registry import Registry

COLORS = {"yellow": "#ffe066", "green": "#8ce99a", "blue": "#74c0fc", "pink": "#faa2c1"}
COLOR_NAMES = {"yellow": "黄", "green": "绿", "blue": "蓝", "pink": "粉"}
ALLOWED_SCALES = (1.0, 1.5, 2.0, 2.5, 3.0)


class DocCache:
    """A few open documents shared between requests (PyMuPDF is not thread-safe per doc)."""

    def __init__(self, size: int = 4):
        self.size = size
        self.docs: OrderedDict[str, tuple[pymupdf.Document, threading.Lock]] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, path: Path) -> tuple[pymupdf.Document, threading.Lock]:
        key = f"{path}:{path.stat().st_mtime}"
        with self.lock:
            if key in self.docs:
                self.docs.move_to_end(key)
                return self.docs[key]
            entry = (pymupdf.open(path), threading.Lock())
            self.docs[key] = entry
            while len(self.docs) > self.size:
                _, (old, _) = self.docs.popitem(last=False)
                old.close()
            return entry


DOCS = DocCache()


def info(path: Path) -> dict:
    doc, lock = DOCS.get(path)
    with lock:
        pages = [{"w": round(p.rect.width, 2), "h": round(p.rect.height, 2)} for p in doc]
        toc = [{"level": lvl, "title": " ".join(title.split()), "page": max(1, pg)} for lvl, title, pg, *_ in doc.get_toc()]
    return {"pages": pages, "toc": toc}


def render(path: Path, page: int, scale: float, cache_dir: Path) -> Path:
    """PNG of one page, cached on disk by file content + page + scale."""
    scale = min(ALLOWED_SCALES, key=lambda s: abs(s - scale))
    digest = hashlib.sha1(f"{path}:{path.stat().st_size}:{path.stat().st_mtime}".encode()).hexdigest()[:16]
    out = cache_dir / digest / f"{page}-{scale}.png"
    if out.exists():
        return out
    doc, lock = DOCS.get(path)
    with lock:
        if not 0 <= page < doc.page_count:
            raise IndexError(page)
        pix = doc[page].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        data = pix.tobytes("png")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(out)
    return out


def words(path: Path, page: int) -> list[list]:
    """[x0, y0, x1, y1, text, line_key] in PDF points, reading order."""
    doc, lock = DOCS.get(path)
    with lock:
        if not 0 <= page < doc.page_count:
            raise IndexError(page)
        ws = doc[page].get_text("words", sort=False)
    return [[round(w[0], 2), round(w[1], 2), round(w[2], 2), round(w[3], 2), w[4], f"{w[5]}-{w[6]}"] for w in ws]


def search(path: Path, query: str, limit: int = 300) -> list[dict]:
    query = query.strip()
    if len(query) < 2:
        return []
    doc, lock = DOCS.get(path)
    hits = []
    with lock:
        for i, page in enumerate(doc):
            for r in page.search_for(query, quads=False):
                text = page.get_textbox(pymupdf.Rect(r.x0 - 120, r.y0 - 2, r.x1 + 120, r.y1 + 2))
                hits.append({"page": i, "rect": [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)],
                             "context": " ".join(text.split())[:160]})
                if len(hits) >= limit:
                    return hits
    return hits


# ---------------------------------------------------------------- annotations
def annotations_md(registry: Registry, paper_id: int, title: str, language: str) -> str:
    rows = registry.annotations(paper_id)
    zh = language != "en"
    front = {"title": title, "annotations": len(rows), "updated": date.today().isoformat()}
    out = ["---", yaml.safe_dump(front, allow_unicode=True, sort_keys=False, width=10**6).strip(), "---", "",
           f"# {'标注' if zh else 'Annotations'} · {title}", "",
           ("> 在 PaperMap 阅读器里做的高亮和批注（自动生成；要修改请在阅读器里改）。" if zh
            else "> Highlights and notes made in the PaperMap reader (generated; edit them in the reader)."), ""]
    for r in rows:
        color = COLOR_NAMES.get(r["color"], r["color"]) if zh else r["color"]
        out.append(f"- **p.{r['page'] + 1}** · {color} — “{' '.join((r['text'] or '').split())}”")
        if r["note"]:
            out.append(f"    - {r['note']}")
    return "\n".join(out).rstrip() + "\n"


def write_annotations_md(registry: Registry, knowledge_dir: Path, paper_id: int, language: str) -> Path | None:
    row = registry.get_paper(paper_id)
    if row is None or not row["kb_path"] or row["kb_path"].count("/") < 2:
        return None
    card = knowledge_dir / row["kb_path"]
    out = card.with_name(f"{card.stem}.annotations.md")
    if not registry.annotations(paper_id):
        out.unlink(missing_ok=True)
        return None
    atomic_write(out, annotations_md(registry, paper_id, row["title"] or "", language))
    return out


def clean_rects(rects) -> list[list[float]]:
    out = []
    for r in rects or []:
        if isinstance(r, (list, tuple)) and len(r) == 4:
            out.append([round(float(x), 2) for x in r])
    return out[:400]


def dumps(rects) -> str:
    return json.dumps(clean_rects(rects))
