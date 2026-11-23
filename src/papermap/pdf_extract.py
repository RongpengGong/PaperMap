"""PDF text and metadata extraction with PyMuPDF."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

MAX_PAGES_FOR_LLM = 80

_SUPPLEMENT_FILENAME_RE = re.compile(
    r"(supplement|suppl|_si\b|\bsi_|moesm|_esm\b|reporting[-_ ]?summary|peer[-_ ]?review)",
    re.IGNORECASE,
)
_SUPPLEMENT_HEAD_RE = re.compile(
    r"^\s*(supplementary (information|materials?|data|figures?|tables?|methods|notes?)|"
    r"supporting information|reporting summary|peer review (file|information))\b",
    re.IGNORECASE | re.MULTILINE,
)

_SECTION_NAMES = {
    "abstract": "abstract",
    "summary": "abstract",
    "introduction": "intro",
    "background": "intro",
    "main": "intro",
    "results": "results",
    "results and discussion": "results",
    "discussion": "discussion",
    "conclusion": "discussion",
    "conclusions": "discussion",
    "methods": "methods",
    "materials and methods": "methods",
    "material and methods": "methods",
    "online methods": "methods",
    "experimental procedures": "methods",
    "star methods": "methods",
    "references": "references",
    "bibliography": "references",
    "acknowledgements": "back",
    "acknowledgments": "back",
    "data availability": "back",
    "code availability": "back",
    "author contributions": "back",
    "competing interests": "back",
    "reporting summary": "back",
    "additional information": "back",
}
_REFERENCE_LINE_RE = re.compile(
    r"^\s*\d{1,3}\.\s+\S.*(?:\(\d{4}\)|\b(?:19|20)\d{2}\b)"  # "12. Smith, J. ... (2020)"
    r"|et al\.,?\s.*\b(?:19|20)\d{2}\b"
)
_FIG_LEGEND_RE = re.compile(r"^\s*(?:extended data )?fig(?:ure)?\.?\s*\d+\s*[|.:]", re.I)


@dataclass
class PdfInfo:
    path: Path
    page_count: int = 0
    info: dict = field(default_factory=dict)
    xmp: str = ""
    first_pages_text: str = ""
    first_pages: list[str] = field(default_factory=list)
    title_guess: str = ""
    is_supplement: bool = False
    supplement_reason: str = ""
    error: str = ""


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def read_info(path: str | Path) -> PdfInfo:
    """Cheap pass: metadata, text of the first 2 pages, title, supplement check."""
    p = Path(path)
    res = PdfInfo(path=p)
    try:
        with pymupdf.open(p) as doc:
            res.page_count = doc.page_count
            res.info = {k: v for k, v in (doc.metadata or {}).items() if v}
            try:
                res.xmp = doc.get_xml_metadata() or ""
            except Exception:  # noqa: BLE001 - broken XMP is common
                res.xmp = ""
            pages = [doc[i].get_text() for i in range(min(2, doc.page_count))]
            res.first_pages = pages
            res.first_pages_text = "\n".join(pages)
            res.title_guess = _guess_title(doc, res.info)
    except Exception as exc:  # noqa: BLE001 - corrupt PDFs must not crash us
        res.error = f"{type(exc).__name__}: {exc}"
        return res
    res.is_supplement, res.supplement_reason = _detect_supplement(p, res.first_pages_text)
    return res


def _guess_title(doc: pymupdf.Document, info: dict) -> str:
    meta_title = (info.get("title") or "").strip()
    if _plausible_title(meta_title):
        return meta_title
    if doc.page_count == 0:
        return ""
    # Largest-font text on page 1 is usually the title.
    spans: list[tuple[float, float, str]] = []
    page = doc[0]
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                txt = span.get("text", "").strip()
                if txt:
                    spans.append((round(span.get("size", 0), 1), line["bbox"][1], txt))
    if not spans:
        return ""
    sizes = sorted({s[0] for s in spans}, reverse=True)
    for size in sizes[:4]:
        parts = [t for s, _, t in sorted((x for x in spans if x[0] == size), key=lambda x: x[1])]
        title = re.sub(r"\s+", " ", " ".join(parts)).strip()
        if _plausible_title(title):
            return title
    return ""


def _plausible_title(t: str) -> bool:
    if not t or len(t) < 15 or len(t) > 400:
        return False
    if len(t.split()) < 3:
        return False
    low = t.lower()
    bad = ("untitled", "microsoft word", ".doc", ".pdf", "springernature_", "1..", "article in press")
    return not any(b in low for b in bad)


def _detect_supplement(path: Path, first_text: str) -> tuple[bool, str]:
    if _SUPPLEMENT_FILENAME_RE.search(path.stem):
        return True, f"file name '{path.name}'"
    head = first_text[:300]
    m = _SUPPLEMENT_HEAD_RE.search(head)
    if m and "abstract" not in first_text[:4000].lower():
        return True, f"first page starts with '{m.group(0).strip()}'"
    return False, ""


def extract_for_llm(path: str | Path, max_chars: int) -> str:
    """Return the most informative text (title/abstract/results/methods/legends).

    Image-heavy PDFs are fine: only the text layer is read. Reference lists are
    dropped, then each section gets a share of the character budget.
    """
    with pymupdf.open(path) as doc:
        n = min(doc.page_count, MAX_PAGES_FOR_LLM)
        page_lines = [doc[i].get_text().splitlines() for i in range(n)]
    lines = _drop_running_headers(page_lines)

    sections: dict[str, list[str]] = {"head": []}
    legends: list[str] = []
    current = "head"
    in_legend = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            in_legend = False
            continue
        key = re.sub(r"[^a-z ]", "", stripped.lower()).strip()
        if key in _SECTION_NAMES and len(stripped) < 40:
            current = _SECTION_NAMES[key]
            sections.setdefault(current, [])
            in_legend = False
            continue
        if current == "references" or _REFERENCE_LINE_RE.search(stripped):
            continue
        if _FIG_LEGEND_RE.match(stripped):
            in_legend = True
        if in_legend:
            legends.append(stripped)
            continue
        sections.setdefault(current, []).append(stripped)

    def joined(name: str) -> str:
        return _join_lines(sections.get(name, []))

    parts = [
        ("HEADER/ABSTRACT", joined("head") + "\n" + joined("abstract"), 0.25),
        ("INTRODUCTION", joined("intro"), 0.10),
        ("RESULTS", joined("results"), 0.30),
        ("DISCUSSION", joined("discussion"), 0.10),
        ("METHODS", joined("methods"), 0.15),
        ("FIGURE LEGENDS", _join_lines(legends), 0.10),
    ]
    found = [p for p in parts if p[1].strip()]
    if len(found) <= 1:  # no recognizable headings: just truncate cleaned text
        return _join_lines([ln for v in sections.values() for ln in v])[:max_chars]
    # Redistribute budget of empty sections proportionally.
    total_w = sum(w for _, _, w in found)
    out, leftover = [], 0
    for label, text, w in found:
        budget = int(max_chars * w / total_w) - 40 + leftover
        chunk = text[:budget]
        leftover = max(0, budget - len(chunk))
        out.append(f"## {label}\n{chunk}")
    return "\n\n".join(out)[:max_chars]


def _drop_running_headers(page_lines: list[list[str]]) -> list[str]:
    """Remove page headers/footers: short lines repeated on many pages."""
    from collections import Counter

    if len(page_lines) < 4:
        return [ln for page in page_lines for ln in page]

    def key(ln: str) -> str:
        return re.sub(r"\d+", "#", ln.strip().lower())

    counts = Counter(k for page in page_lines for k in {key(ln) for ln in page if ln.strip()})
    limit = max(3, int(len(page_lines) * 0.3))
    repeated = {k for k, c in counts.items() if c >= limit and len(k) < 120}
    return [ln for page in page_lines for ln in page if key(ln) not in repeated]


def extract_full_text(path: str | Path, max_chars: int = 200000) -> str:
    """Whole main text (running headers and reference lists removed) for skills."""
    with pymupdf.open(path) as doc:
        n = min(doc.page_count, MAX_PAGES_FOR_LLM)
        page_lines = [doc[i].get_text().splitlines() for i in range(n)]
    lines = [ln for ln in _drop_running_headers(page_lines) if not _REFERENCE_LINE_RE.search(ln)]
    return _join_lines(lines)[:max_chars]


def _join_lines(lines: list[str]) -> str:
    text = "\n".join(lines)
    text = re.sub(r"-\n(?=[a-z])", "", text)  # de-hyphenate line breaks
    text = re.sub(r"(?<![.:!?])\n(?=[a-z(])", " ", text)
    return text
