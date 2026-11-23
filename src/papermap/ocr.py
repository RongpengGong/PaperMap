"""Page text from Apple Vision, cached separately from the original PDF.

All returned rectangles use the displayed PDF page's top-left coordinate system.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import threading
from functools import lru_cache
from pathlib import Path

import pymupdf

log = logging.getLogger(__name__)
CACHE_VERSION = "vision-1"
# Accurate Vision returns word boxes for Latin scripts and individual Han glyph boxes.
_HAN = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0003134f"
_HAN_CHAR = re.compile(f"[{_HAN}]")
_TOKENS = re.compile(f"[{_HAN}]|[^\\s{_HAN}]+")
# Bound CPU/memory for adjacent-page prefetches and background processing.
_SLOTS = threading.BoundedSemaphore(2)
_LOCKS = [threading.Lock() for _ in range(32)]


class OCRError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        from Vision import VNRecognizeTextRequest
        return VNRecognizeTextRequest is not None
    except (ImportError, AttributeError):
        return False


def cache_for(pdf: Path | str, data_dir: Path) -> Path:
    from .evidence import document_revision
    return data_dir / "cache" / "ocr" / CACHE_VERSION / document_revision(Path(pdf))


def scan_like(page: pymupdf.Page) -> bool:
    """Ignore native documents, blanks and isolated logos; allow page numbers."""
    bounds = page.rect
    body = pymupdf.Rect(0, bounds.height * .09, bounds.width, bounds.height * .91)
    chars = 0
    for word in page.get_text("words"):
        rect = pymupdf.Rect(word[:4]) * page.rotation_matrix
        if rect.intersects(body):
            chars += len(word[4].strip())
            if chars >= 48:
                return False
    image_area = sum((pymupdf.Rect(image["bbox"]) * page.rotation_matrix & bounds).get_area()
                     for image in page.get_image_info())
    return bool(bounds.get_area() and image_area >= bounds.get_area() * .45)


def scanned_document(doc: pymupdf.Document) -> bool:
    if not doc.page_count or not available():
        return False
    sample = {0, doc.page_count // 2, doc.page_count - 1}
    return sum(scan_like(doc[n]) for n in sample) > len(sample) / 2


def cached_words(path: Path) -> list | None:
    try:
        words = json.loads(path.read_bytes())
        if isinstance(words, list) and all(isinstance(w, list) and len(w) == 6 for w in words):
            return words
    except (FileNotFoundError, ValueError):
        pass
    return None


def _store(path: Path, words: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(mode="w", encoding="utf8", dir=path.parent, delete=False)
    try:
        with tmp:
            json.dump(words, tmp, ensure_ascii=False, allow_nan=False)
        os.replace(tmp.name, path)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def _vision(png: bytes, width: float, height: float) -> list:
    from Foundation import NSData, NSDictionary
    import Vision
    import objc

    def box(rect):
        x, y = rect.origin.x, rect.origin.y
        w, h = rect.size.width, rect.size.height
        return [round(max(0., min(1., v)) * scale, 3) for v, scale in
                ((x, width), (1-y-h, height), (x+w, width), (1-y, height))]

    with objc.autorelease_pool():
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        if hasattr(request, "supportedRecognitionLanguagesAndReturnError_"):
            supported, _ = request.supportedRecognitionLanguagesAndReturnError_(None)
        else:
            supported, _ = Vision.VNRecognizeTextRequest.supportedRecognitionLanguagesForTextRecognitionLevel_revision_error_(
                request.recognitionLevel(), request.revision(), None)
        languages = [lang for lang in ("en-US", "zh-Hans", "zh-Hant") if lang in (supported or [])]
        if languages:
            request.setRecognitionLanguages_(languages)
        if hasattr(request, "setAutomaticallyDetectsLanguage_"):
            request.setAutomaticallyDetectsLanguage_(True)
        request.setUsesLanguageCorrection_(False)
        image = NSData.dataWithBytes_length_(png, len(png))
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(image, NSDictionary.dictionary())
        ok, error = handler.performRequests_error_([request], None)
        if not ok:
            raise OCRError(str(error.localizedDescription()) if error else "Vision 未返回识别结果。")
        words = []
        for i, observation in enumerate(request.results() or []):
            candidates = observation.topCandidates_(1)
            if not candidates:
                continue
            candidate = candidates[0]
            line = str(candidate.string())
            line_words = []
            for match in _TOKENS.finditer(line):
                # NSRange uses UTF-16 code units, including supplementary glyphs.
                start = len(line[:match.start()].encode("utf-16-le")) // 2
                length = len(match[0].encode("utf-16-le")) // 2
                region, _ = candidate.boundingBoxForRange_error_((start, length), None)
                if region is None:
                    line_words = []
                    break
                rect = box(region.boundingBox())
                if rect[2] <= rect[0] or rect[3] <= rect[1]:
                    line_words = []
                    break
                line_words.append([*rect, match[0], f"ocr-{i}"])
            if not line_words and line.strip():
                line_words = [[*box(observation.boundingBox()), line.strip(), f"ocr-{i}"]]
            words.extend(line_words)
        return words


def page_words(page: pymupdf.Page, cache: Path, *, automatic: bool = True, force: bool = False) -> list | None:
    """Return cached or newly recognized words; None means use the native text."""
    path = cache / f"{page.number}.json"
    cached = cached_words(path)
    if cached or (cached == [] and not force):
        return cached
    if not (force or (automatic and available() and scan_like(page))):
        return None
    if not available():
        raise OCRError("本机文字识别需要 macOS 的 Vision 服务。")
    with _LOCKS[hash(path) % len(_LOCKS)]:
        cached = cached_words(path)
        if cached or (cached == [] and not force):
            return cached
        try:
            with _SLOTS:
                width, height = page.rect.width, page.rect.height
                scale = min(3., 3200 / max(width, height, 1.))
                pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False)
                words = _vision(pix.tobytes("png"), width, height)
            _store(path, words)
        except Exception as exc:
            if force:
                raise OCRError(f"本页文字识别失败：{exc}") from exc
            log.warning("Could not recognize PDF page %s: %s", page.number+1, exc)
            return None
        return words


def word_separator(previous: str, current: str) -> str:
    if _HAN_CHAR.fullmatch(previous[-1:]) and _HAN_CHAR.fullmatch(current[:1]):
        return ""
    if current[:1] in "，。；：？！、）】》”’.,;:!?%)]" or previous[-1:] in "，。；：？！、（【《“‘([":
        return ""
    return " "


def lines(words: list) -> list[str]:
    result = []
    last_key, previous = None, ""
    for *_, text, key in words:
        if key == last_key and result:
            result[-1] += word_separator(previous, text) + text
        else:
            result.append(text)
        last_key, previous = key, text
    return result


def page_text(page: pymupdf.Page, cache: Path | None, auto: bool) -> str:
    words = page_words(page, cache, automatic=auto) if cache else None
    return "\n".join(lines(words)) if words else page.get_text()
