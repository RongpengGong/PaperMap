"""Conservative checks for PDF text missing from the source side of a translation."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

import pymupdf

from . import ocr
from .pdf_extract import _REFERENCE_LINE_RE, _SECTION_NAMES


_NUMBERED = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?|[IVX]+\.)\s+[A-Za-z]", re.I)
_CAPTION = re.compile(r"^(?:(?:Extended Data|Supplementary)\s+)?(?:Fig(?:ure)?\.?|Table)\s+[A-Z]?\d+\b", re.I)
_BIB = re.compile(r"^(?:(?:\d+[.]?|[IVX]+[.])\s+)?(?:references(?: and notes)?|bibliography|literature cited)$", re.I)
_POST = re.compile(r"^(?:(?:online |supplementary |materials and )?methods|append(?:ix|ices)(?:\s+[A-Z\d])?"
                   r"|supplementary (?:information|materials)|extended data|data availability|code availability)"
                   r"(?:$|[.:\s–—-])", re.I)
_BACK = re.compile(r"^(?:acknowledg(?:e)?ments?|authors?[’']? (?:contributions|details|information)|"
                   r"funding|competing interests?|conflicts? of interest|publisher[’']?s note|"
                   r"open access|peer review information|additional information)$", re.I)
_AUTHOR_ENTRY = re.compile(r"^[A-Z][\w’'-]+,? [A-Z](?:[,. ]|$)")
_ENTRY = re.compile(r"^\s*(?:\[\d{1,3}\]|\d{1,3}[.)])(?:\s|$)")
_AFFILIATION = re.compile(r"^\s*\d+\s*(?:state key |national |key )?(?:laborator(?:y|ies)|department|"
                          r"institute|university|center|centre|school|faculty|division)\b", re.I)
_LICENSE = re.compile(r"©|creative commons|open access article|users may view, print|licen[cs]e[de]? (?:under|to)", re.I)
_CAPTION_LABEL = re.compile(r"^(?:(?:extended data|supplementary)\s+)?(?:fig(?:ure)?\.?|table)\s*"
                            r"[A-Z]?\d+[A-Z]?(?:\s*$|\s*[.|:–—])", re.I)
_FORM_HEADER = re.compile(r"nature (?:portfolio|research)\s*[|│]\s*reporting summary", re.I)
_FORM_INTRO = re.compile(r"last updated by author|nature (?:portfolio|research) wishes to improve", re.I)
_FORM = re.compile(r"^(?:nature (?:research|portfolio) )?reporting summary$", re.I)
_REVIEW_REPORT = re.compile(r"^reviewer report\s+\d{1,2}\s+[a-z]+\s+\d{4}$", re.I)
_WORDS = re.compile(r"[A-Za-z]{2,}")
_FUNCTION_WORD = re.compile(r"\b(?:the|for|that|with|from|which|were|was|this|and|are|between|our|have)\b", re.I)


def reporting_form_page(lines: Iterable[str]) -> bool:
    """A branded questionnaire header or its title and introductory form fields."""
    title = fields = False
    for line in lines:
        line = line.strip()
        if _FORM_HEADER.fullmatch(line):
            return True
        title |= bool(_FORM.fullmatch(line))
        fields |= bool(_FORM_INTRO.match(line))
    return title and fields


def _key(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text).casefold() if c.isalnum())


def _source_index(paras, n_pages):
    by_page = defaultdict(list)
    unlocated = []
    for row in paras:
        src = _key(row.get("src") or "")
        if not src:
            continue
        first = row.get("page")
        last = row.get("endPage", first)
        if type(first) is not int or type(last) is not int or not (0 <= first <= last < n_pages):
            # Older bilingual files have no page metadata. Match their text across the PDF.
            unlocated.append(src)
            continue
        for page_no in range(first, last + 1):
            by_page[page_no].append(src)
    return {n: "\n".join(lines) for n, lines in by_page.items()}, "\n".join(unlocated)


def _body_heading(label):
    return bool(_NUMBERED.match(label) or _POST.match(label)
                or _SECTION_NAMES.get(label.lower()) in {"abstract", "intro", "methods", "discussion", "results"})


def _omission(lines: list[str], text: str, present: str) -> tuple[bool, int]:
    """Strong single-block evidence, and the size of any entirely absent narrative."""
    word_count = len(_WORDS.findall(text))
    if word_count < 25 or len(_FUNCTION_WORD.findall(text)) < 3 or not re.search(r"[.!?](?:\s|$)", text):
        return False, 0
    keys = [_key(line) for line in lines]
    matched = [len(k) >= 12 and k in present for k in keys]
    if not any(matched):
        # A caption needs prose as well as the figure/table label. Disconnected
        # axis labels and single-line table cells do not establish a gap.
        return bool(_CAPTION.match(lines[0])), word_count if len(lines) >= 3 else 0
    for i in range(1, len(lines) - 1):
        if (len(keys[i]) >= 24 and not matched[i] and _REFERENCE_LINE_RE.search(lines[i])
                and any(matched[max(0, i - 3):i]) and any(matched[i + 1:i + 4])):
            return True, 0
    return False, 0


def _excluded_caption_gap(lines: list[str], present: str) -> bool:
    """A publisher may emit a figure caption out of order inside the bibliography."""
    for i, line in enumerate(lines):
        if _CAPTION_LABEL.match(line):
            end = next((j for j in range(i + 1, len(lines)) if _ENTRY.match(lines[j])), len(lines))
            caption = lines[i:end]
            if _omission(caption, " ".join(caption), present)[0]:
                return True
    return False


def _page_lines(page, cache):
    if cache is not None:
        words = ocr.page_words(page, cache, automatic=False)
        if words:
            blocks = []
            body_lines = []
            for line in ocr.lines(words):
                if (_BIB.fullmatch(line) or _FORM.fullmatch(line) or _BACK.fullmatch(line)
                        or len(line) <= 80 and _body_heading(line)):
                    if body_lines:
                        blocks.append(body_lines)
                        body_lines = []
                    blocks.append([line])
                else:
                    body_lines.append(line)
            return blocks + [body_lines]
    return [b[4].splitlines() for b in page.get_text("blocks") if b[6] == 0]


def coverage_gaps(pdf: str | Path, paras: list[dict], *, cache: Path | None = None) -> list[int]:
    """Return 1-based pages with affirmative evidence of omitted article text.

    Page ranges include paragraphs continued from an adjacent page. Only existing
    OCR is consulted. Unreadable pages, figure-only pages and sparse text alone do
    not establish missing source text. The language/quality of the translation
    itself is outside this check. Prose and caption signals target English papers.
    """
    gaps = []
    with pymupdf.open(pdf) as doc:
        page_source, legacy_source = _source_index(paras, len(doc))
        outline = defaultdict(dict)
        first_section = None
        for _, title, number in doc.get_toc():
            title = title.strip()
            if _body_heading(title) or _BIB.fullmatch(title):
                outline[number][_key(title)] = title
                if _body_heading(title):
                    first_section = min(first_section or number, number)

        excluded = False
        pending_refs = False
        for page in doc:
            number = page.number + 1
            blocks = _page_lines(page, cache)
            if reporting_form_page(line for block in blocks for line in block):
                continue
            present = page_source.get(page.number, "")
            # Include unlocated rows once in each page lookup only for mixed
            # formats. Pure legacy documents reuse one immutable search string.
            if legacy_source:
                present = f"{present}\n{legacy_source}" if present else legacy_source

            expected = outline.get(number, {})
            page_gap = False
            wholly_missing_words = 0
            known_body = bool(first_section and number >= first_section)
            cover = number == 1 and not known_body
            for raw in blocks:
                lines = [line.strip() for line in raw if line.strip()]
                if not lines:
                    continue
                title = " ".join(lines)
                first = lines[0]
                bookmark = (expected.get(_key(title)) or expected.get(_key(first))) if expected else None
                if number > 1 and _REVIEW_REPORT.fullmatch(first):
                    if page_gap or wholly_missing_words >= 85:
                        gaps.append(number)
                    return gaps
                # References and their first entry can be in the same PDF block.
                # A contents entry ("References 12") must not start the bibliography.
                if pending_refs:
                    excluded = bool(_ENTRY.match(first) or _AUTHOR_ENTRY.match(first))
                    pending_refs = False
                if excluded or _BIB.fullmatch(first):
                    page_gap |= _excluded_caption_gap(lines, present)
                if _BIB.fullmatch(first):
                    if bookmark or (len(lines) > 1 and (_ENTRY.match(lines[1]) or _AUTHOR_ENTRY.match(lines[1]))):
                        excluded = True
                    elif len(lines) == 1:
                        pending_refs = True
                    continue
                # Some publishers print the bibliography directly below the article
                # without a heading. Multiple distinct numbered entries in a block
                # plus publication years distinguish the list from a numbered paragraph.
                if (sum(bool(_ENTRY.match(line)) for line in lines) >= 3 and
                        len(re.findall(r"\b(?:19|20)\d{2}\b", title)) >= 2):
                    excluded = True
                    continue
                if _BACK.fullmatch(first):
                    excluded = True
                    continue
                if _LICENSE.search(title) or (number == 1 and _AFFILIATION.match(first)):
                    continue
                if excluded and _CAPTION_LABEL.match(first):
                    # The intervening caption does not end the reference list.
                    continue
                post = len(first) < 95 and _POST.match(first) and not first.endswith(".")
                named_section = _SECTION_NAMES.get(first.lower()) in {"abstract", "intro", "methods", "discussion", "results"}
                if (bookmark and _body_heading(bookmark)) or post or named_section:
                    excluded = False
                    cover = False
                    known_body = True
                if excluded:
                    continue
                # A short generic label may be absent while all its content is
                # present. Require a specific title physically printed on this page.
                if bookmark and (_NUMBERED.match(bookmark) or len(bookmark.split()) >= 4):
                    if _key(bookmark) not in present:
                        page_gap = True
                if len(title) <= 70 and _body_heading(title):
                    known_body = True
                    cover = False
                if cover or (first_section and number < first_section and not known_body):
                    continue
                missing, missing_words = _omission(lines, title, present)
                page_gap |= missing
                wholly_missing_words += missing_words
            if page_gap or wholly_missing_words >= 85:
                gaps.append(number)
    return gaps
