"""PDF page images, native and local OCR text, search, outline and annotations."""

from __future__ import annotations

import json
import re
import threading
from bisect import bisect_right
from collections import OrderedDict
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile

import pymupdf
import yaml

from . import ocr
from .evidence import document_revision
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
        key = document_revision(path)
        with self.lock:
            if key in self.docs:
                self.docs.move_to_end(key)
                return self.docs[key]
            entry = (pymupdf.open(path), threading.Lock())
            self.docs[key] = entry
            while len(self.docs) > self.size:
                # An in-flight request may still own the document and its lock.
                self.docs.popitem(last=False)
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
    digest = document_revision(path)
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
        tmp = NamedTemporaryFile(dir=out.parent, delete=False)
        try:
            with tmp:
                tmp.write(data)
            Path(tmp.name).replace(out)
        finally:
            Path(tmp.name).unlink(missing_ok=True)
    return out


def page_layer(path: Path, page: int, cache: Path | None = None, *, force: bool = False) -> dict:
    """Display-coordinate words from the native text layer or the local scan cache."""
    # Use a separate document so that slow OCR does not block PNG rendering.
    with pymupdf.open(path) as doc:
        if not 0 <= page < doc.page_count:
            raise IndexError(page)
        pg = doc[page]
        found = ocr.page_words(pg, cache, force=force) if cache else None
        if found:
            return {"words": found, "source": "ocr"}
        words = []
        for x0, y0, x1, y1, text, block, line, _ in pg.get_text("words", sort=False):
            rect = pymupdf.Rect(x0, y0, x1, y1) * pg.rotation_matrix
            words.append([*[round(v, 2) for v in rect], text, f"{block}-{line}"])
        layer = {"words": words, "source": "pdf"}
        if found == []:
            layer["message"] = "本页没有识别到文字。"
        return layer


def words(path: Path, page: int) -> list[list]:
    return page_layer(path, page)["words"]


def _ocr_hits(words: list, query: str, number: int, limit: int) -> list[dict]:
    parts, starts, offset = [], [], 0
    previous = None
    for word in words:
        if previous is not None:
            sep = ocr.word_separator(previous[4], word[4]) if previous[5] == word[5] else " "
            parts.append(sep)
            offset += len(sep)
        starts.append(offset)
        parts.append(word[4])
        offset += len(word[4])
        previous = word
    text = "".join(parts)
    hits = []
    for match in re.finditer(re.escape(query), text, re.I):
        first = max(0, bisect_right(starts, match.start())-1)
        idx = first
        rect = pymupdf.Rect(words[first][:4])
        while idx + 1 < len(words) and starts[idx+1] < match.end() and words[idx+1][5] == words[first][5]:
            idx += 1
            rect |= pymupdf.Rect(words[idx][:4])
        hits.append({"page": number, "rect": list(rect),
                     "context": text[max(0, match.start()-65):match.end()+65]})
        if len(hits) >= limit:
            break
    return hits


def search(path: Path, query: str, limit: int = 300, cache: Path | None = None) -> list[dict]:
    query = query.strip()
    if len(query) < 2:
        return []
    hits = []
    with pymupdf.open(path) as doc:
        auto = cache is not None and ocr.scanned_document(doc)
        for i, page in enumerate(doc):
            found = ocr.page_words(page, cache, automatic=auto) if cache else None
            if found:
                hits.extend(_ocr_hits(found, query, i, limit-len(hits)))
            else:
                for rect in page.search_for(query, quads=False):
                    text = page.get_textbox(pymupdf.Rect(rect.x0-120, rect.y0-2, rect.x1+120, rect.y1+2))
                    hits.append({"page": i, "rect": [round(v, 2) for v in rect * page.rotation_matrix],
                                 "context": " ".join(text.split())[:160]})
                    if len(hits) >= limit:
                        break
            if len(hits) >= limit:
                break
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
