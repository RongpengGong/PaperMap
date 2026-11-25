"""Read a shared paper's card, paragraph translation and the user's notes for paired offline readers."""
from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path

from . import kb_writer, ocr, translate

# Leave headroom below the phone's 10 MiB HTTP response limit, including JSON escaping.
_CARD_BYTES = 1024 * 1024
_TRANSLATION_BYTES = 8 * 1024 * 1024
_NOTES_BYTES = 256 * 1024  # the phone shows the notes read-only; they are edited on the computer


def _paths(config, row):
    relative = Path(row["kb_path"] or "")
    parts = relative.parts
    if (len(parts) not in (2, 3) or parts[0] != "papers" or relative.suffix != ".md"
            or ".." in parts or (len(parts) == 3 and parts[1] != relative.stem)):
        return None, None
    card = config.knowledge_dir.resolve() / relative
    try:
        if card.parent.resolve() != card.parent:
            return None, None
    except (OSError, RuntimeError):
        return None, None
    return card, card.with_name(f"{card.stem}.translation.json")


def _stamp(path):
    if path is not None:
        try:
            st = path.lstat()
            if stat.S_ISREG(st.st_mode):
                return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns
        except OSError:
            pass
    return None


def version(config, row, revision):
    card, translation = _paths(config, row)
    stamp = _stamp(translation)
    cache_time = None
    if stamp:
        # Newly cached OCR can anchor previously unlocated legacy paragraphs.
        cache = config.data_dir / "cache" / "ocr" / ocr.CACHE_VERSION / revision
        try:
            cache_time = cache.stat().st_mtime_ns
        except OSError:
            pass
    notes = card.with_name(f"{card.stem}.notes.md") if card is not None else None
    identity = ["reading-materials-v1", revision, _stamp(card), stamp, cache_time, _stamp(notes)]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def _read(path, limit):
    stamp = _stamp(path)
    if stamp is None:
        raise FileNotFoundError
    if stamp[2] > limit:
        raise ValueError("Reading material too large")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit or stamp != _stamp(path) or path.resolve() != path:
        raise ValueError("Reading material changed")
    return data.decode("utf-8-sig")


def _anchors(paras, data_revision, pdf, config, page_count):
    # New translation files carry anchors captured during translation of this exact revision.
    def valid(p):
        a, b = p.get("page"), p.get("endPage")
        return type(a) is int and type(b) is int and 0 <= a <= b < page_count

    if data_revision and all(valid(p) for p in paras):
        return [(p["page"], p["endPage"]) for p in paras]
    extracted = translate.paragraphs(pdf, ocr.cache_for(pdf, config.data_dir), automatic_ocr=False)
    if [p["src"] for p in paras] == [p["src"] for p in extracted]:
        return [(p["page"], p["endPage"]) for p in extracted]
    if not data_revision:
        return None  # Without the PDF hash we require the entire source sequence to match.
    by_text = {}
    for p in extracted:
        interval = p["page"], p["endPage"]
        previous = by_text.setdefault(p["src"], interval)
        if previous != interval:
            by_text[p["src"]] = (-1, -1)
    return [by_text.get(p["src"], (-1, -1)) for p in paras]


def _translation(path, config, paper_id, revision, pdf, page_count):
    try:
        data = json.loads(_read(path, _TRANSLATION_BYTES * 2))
        if not isinstance(data, dict) or data.get("paper_id", paper_id) != paper_id:
            raise ValueError("Invalid translation")
        source_revision = data.get("revision")
        if source_revision and source_revision != revision:
            return {"state": "differentRevision", "paragraphs": []}
        paras = data.get("paragraphs")
        if (not isinstance(paras, list) or not paras or
                not all(isinstance(p, dict) and p.get("kind") in ("heading", "meta", "para")
                        and isinstance(p.get("src"), str) and p["src"].strip()
                        and isinstance(p.get("tr"), str) for p in paras)):
            raise ValueError("Invalid translation paragraphs")
        anchors = _anchors(paras, source_revision, pdf, config, page_count)
        if anchors is None:
            return {"state": "unverified", "paragraphs": []}
        rows = [{"kind": p["kind"], "src": p["src"], "tr": p["tr"], "page": a, "endPage": b}
                for p, (a, b) in zip(paras, anchors)]
        if len(json.dumps(rows, ensure_ascii=False).encode()) > _TRANSLATION_BYTES:
            raise ValueError("Translation too large")
        return {"state": "ready", "paragraphs": rows}
    except FileNotFoundError:
        return {"state": "missing", "paragraphs": []}
    except (ValueError, OSError, RuntimeError, RecursionError):
        return {"state": "unavailable", "paragraphs": []}


def read(config, row, revision, pdf, page_count):
    card, translation = _paths(config, row)
    # If a desktop edit lands during the read, the next catalog advertises the newer version.
    original_version = version(config, row, revision)
    try:
        text = _read(card, _CARD_BYTES * 2)
        first, _, rest = text.partition("\n")
        if first.strip() == "---":
            delimiter = re.search(r"^---[ \t]*\r?$", rest, re.MULTILINE)
            if delimiter is None:
                raise ValueError("Incomplete card metadata")
            text = rest[delimiter.end():]
        body = kb_writer.editable_part(text, config.output_language)
        if len(json.dumps(body, ensure_ascii=False).encode()) > _CARD_BYTES:
            body = ""
    except (ValueError, OSError, RuntimeError):
        body = ""
    try:
        notes = _read(card.with_name(f"{card.stem}.notes.md"), _NOTES_BYTES) if card is not None else ""
    except (ValueError, OSError, RuntimeError):
        notes = ""
    return {"id": row["id"], "revision": revision, "version": original_version, "card": body, "notes": notes or "",
            "translation": _translation(translation, config, row["id"], revision, pdf, page_count)}
