"""DOI extraction, normalization and candidate ranking.

Validation against Crossref/OpenAlex lives in ``pipeline``; this module is pure
and has no network access, so it is easy to unit-test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# A DOI is "10.<registrant>/<suffix>"; suffix may contain almost anything but
# whitespace. We stop at quotes/angle brackets which appear in XML/HTML.
DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s\"'<>{}]+)", re.IGNORECASE)
_PREFIX_RE = re.compile(
    r"^(?:https?://)?(?:dx\.)?doi\.org/|^doi:\s*|^urn:doi:", re.IGNORECASE
)
_TRAILING = ".,;:)]}"
# DOI explicitly marked as the article's own DOI (e.g. "https://doi.org/..."
# in the header, or "DOI: 10...."), used to rank candidates.
_MARKED_RE = re.compile(
    r"(?:doi\.org/|doi\s*[:：]?\s*)(10\.\d{4,9}/[^\s\"'<>{}]+)", re.IGNORECASE
)

# Springer Nature article ids used as PDF file names, e.g. s41586-026-11234-8.
_SPRINGER_ID_RE = re.compile(r"\b(s\d{5}-\d{3}-\d{4,5}-[\dx])", re.IGNORECASE)
# Literal DOI-ish file names: "10.1038_s41586-020-1234-5.pdf"
_FILENAME_DOI_RE = re.compile(r"(10\.\d{4,9})[_%](2F)?(.+)", re.IGNORECASE)


def normalize(doi: str | None) -> str | None:
    """Return the canonical form of a DOI (lowercase, no prefix/trailing punct)."""
    if not doi:
        return None
    s = doi.strip()
    s = _PREFIX_RE.sub("", s).strip()
    m = DOI_RE.search(s)
    if not m:
        return None
    s = m.group(1)
    # Strip trailing punctuation, but keep a closing bracket that is balanced,
    # e.g. 10.1016/s0140-6736(20)30183-5 must survive intact.
    while s and s[-1] in _TRAILING:
        ch = s[-1]
        if ch == ")" and s.count("(") >= s.count(")"):
            break
        if ch == "]" and s.count("[") >= s.count("]"):
            break
        s = s[:-1]
    s = s.lower()
    return s if "/" in s and len(s.split("/", 1)[1]) > 0 else None


@dataclass(frozen=True)
class Candidate:
    doi: str
    source: str  # pdf_metadata / text_marked / text / filename
    score: float


def from_pdf_metadata(info: dict | None, xmp: str | None) -> list[str]:
    """DOIs found in the PDF Info dict and XMP packet."""
    found: list[str] = []
    for value in (info or {}).values():
        if isinstance(value, str):
            found += [d for d in (normalize(x) for x in DOI_RE.findall(value)) if d]
    if xmp:
        # prism:doi, dc:identifier, pdfx:doi, crossmark:DOI ...
        for m in re.finditer(
            r"<(?:prism:doi|pdfx:doi|crossmark:DOI|dc:identifier|prism:url)[^>]*>(.*?)</",
            xmp,
            re.IGNORECASE | re.DOTALL,
        ):
            d = normalize(re.sub(r"<[^>]+>", " ", m.group(1)))
            if d:
                found.append(d)
        for m in re.finditer(r'(?:prism:doi|pdfx:doi|crossmark:DOI)="([^"]+)"', xmp, re.I):
            d = normalize(m.group(1))
            if d:
                found.append(d)
    return _unique(found)


_REF_LINE_RE = re.compile(
    r"^\s*\[?\d{1,3}[.\])]\s+\S|et al\.|\((?:19|20)\d{2}\)", re.IGNORECASE
)


@dataclass(frozen=True)
class TextHit:
    doi: str
    marked: bool  # preceded by "doi:" / "doi.org/"
    page: int  # 0 = first page
    ref_like: bool  # the line looks like a reference-list entry


def from_pages(pages: list[str]) -> list[TextHit]:
    """DOIs in the first page(s); pass only the first 1-2 pages.

    Reference lists contain many DOIs of *other* papers, often also written as
    "doi:..." - so page position and reference-line shape matter for ranking.
    """
    hits: list[TextHit] = []
    seen: set[str] = set()
    for page_no, page_text in enumerate(pages):
        for line in _dehyphenate(page_text).splitlines():
            for raw in DOI_RE.findall(line):
                d = normalize(raw)
                if not d or d in seen:
                    continue
                seen.add(d)
                marked = any(normalize(m) == d for m in _MARKED_RE.findall(line))
                hits.append(TextHit(d, marked, page_no, bool(_REF_LINE_RE.search(line))))
    return hits


def from_text(first_pages_text: str) -> list[tuple[str, bool]]:
    """Simple variant over a single text blob: (doi, explicitly_marked)."""
    return [(h.doi, h.marked) for h in from_pages([first_pages_text])]


def from_filename(path: str | Path) -> list[str]:
    """Weak DOI hints from the file name; must always be validated."""
    stem = Path(path).stem
    found: list[str] = []
    m = _FILENAME_DOI_RE.search(stem)
    if m:
        d = normalize(f"{m.group(1)}/{m.group(3)}")
        if d:
            found.append(d)
    for sid in _SPRINGER_ID_RE.findall(stem):
        sid = sid.lower()
        if sid.startswith("s41"):  # Nature Portfolio journals
            found.append(f"10.1038/{sid}")
        else:  # other Springer / BMC journals
            found += [f"10.1007/{sid}", f"10.1186/{sid}"]
    return _unique(found)


def rank_candidates(
    meta_dois: list[str], text_hits: list[TextHit], filename_dois: list[str]
) -> list[Candidate]:
    """Merge candidates: metadata > page-1 marked > page-1 > page-2 > filename.

    A DOI seen in several places gets a bonus; reference-like lines and pages
    crowded with DOIs are penalized.
    """
    scores: dict[str, Candidate] = {}

    def add(doi: str, source: str, score: float) -> None:
        prev = scores.get(doi)
        if prev is None:
            scores[doi] = Candidate(doi, source, score)
        else:
            best_src = prev.source if prev.score >= score else source
            scores[doi] = Candidate(doi, best_src, max(prev.score, score) + 0.5)

    for d in meta_dois:
        add(d, "pdf_metadata", 4.0)
    crowd = max(0, len(text_hits) - 2) * 0.1
    for i, h in enumerate(text_hits):
        if isinstance(h, tuple):  # (doi, marked) from from_text()
            h = TextHit(h[0], h[1], 0, False)
        base = (3.0 if h.marked else 2.0) if h.page == 0 else (1.6 if h.marked else 1.2)
        if h.ref_like:
            base -= 1.5
        if not h.marked:
            base -= min(crowd, 1.0)
        add(h.doi, "text_marked" if h.marked else "text", base - 0.01 * i)
    for d in filename_dois:
        add(d, "filename", 1.0)
    return sorted(scores.values(), key=lambda c: -c.score)


def slug(doi: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", doi.lower()).strip("-")[:80]


def _dehyphenate(text: str) -> str:
    # DOIs broken across lines: "10.1038/s41586-\n026-11234-8"
    return re.sub(r"(10\.\d{4,9}/\S*?)-\s*\n\s*(\S)", r"\1-\2", text)


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
