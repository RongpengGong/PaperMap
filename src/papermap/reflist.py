"""Import a whole reference list: pasted references, a BibTeX file or a RIS file.

Each entry is matched to a work (its DOI / PMID / arXiv ID, else a Crossref bibliographic search
whose title must appear in the reference). The preview shows which entries are already in the
library and which have a legal open-access PDF; the user ticks the ones to import, which then go
through the normal add-by-identifier flow (`id_import`).
"""
from __future__ import annotations

import json
import re
import secrets
import threading

from rapidfuzz import fuzz

from . import id_import
from .doi import normalize as norm_doi

MAX_ENTRIES = 200
_BIB_ENTRY = re.compile(r"@\w+\s*\{", re.I)
_BIB_FIELD = re.compile(r"(\w+)\s*=\s*(\{(?:[^{}]|\{[^{}]*\})*\}|\"[^\"]*\"|\d+)", re.S)
_RIS_LINE = re.compile(r"^([A-Z][A-Z0-9])  - ?(.*)$")
_NUMBERED = re.compile(r"^\s*(?:\[\d{1,4}\]|\d{1,4}[.)]\s)")


def looks_like_references(text: str) -> bool:
    """A reference list rather than a list of identifiers."""
    t = text or ""
    if _BIB_ENTRY.search(t) or re.search(r"^TY  - ", t, re.M):
        return True
    lines = [ln for ln in t.splitlines() if ln.strip()]
    long_lines = [ln for ln in lines if len(ln) > 60 and not re.fullmatch(r"\s*\S+\s*", ln)]
    return bool(lines) and len(long_lines) >= max(1, len(lines) // 3)


def _bib(text: str) -> list[dict]:
    out = []
    for chunk in _BIB_ENTRY.split(text)[1:]:
        fields = {k.lower(): v.strip("{}\" \n") for k, v in _BIB_FIELD.findall(chunk)}
        title = re.sub(r"[{}]", "", fields.get("title", ""))
        author = fields.get("author", "").split(" and ")[0]
        out.append({"raw": " ".join(x for x in (author, fields.get("year", ""), title, fields.get("journal", "")) if x),
                    "title": " ".join(title.split()), "doi": norm_doi(fields.get("doi")),
                    "pmid": fields.get("pmid") or None, "arxiv": fields.get("eprint") if "arxiv" in
                    (fields.get("archiveprefix", "") + fields.get("eprinttype", "")).lower() else None,
                    "year": fields.get("year")})
    return out


def _ris(text: str) -> list[dict]:
    out, cur = [], None
    for line in text.splitlines():
        m = _RIS_LINE.match(line.rstrip())
        if not m:
            continue
        tag, val = m.group(1), m.group(2).strip()
        if tag == "TY":
            cur = {"title": "", "authors": [], "doi": None, "pmid": None, "year": None, "journal": ""}
        elif cur is None:
            continue
        elif tag in ("TI", "T1") and not cur["title"]:
            cur["title"] = val
        elif tag in ("AU", "A1"):
            cur["authors"].append(val)
        elif tag == "DO":
            cur["doi"] = norm_doi(val)
        elif tag in ("PY", "Y1", "DA") and not cur["year"]:
            cur["year"] = (re.findall(r"\d{4}", val) or [None])[0]
        elif tag in ("JO", "JF", "T2") and not cur["journal"]:
            cur["journal"] = val
        elif tag == "AN" and val.isdigit():
            cur["pmid"] = val
        elif tag == "UR" and not cur["doi"]:
            cur["doi"] = norm_doi(val)
        elif tag == "ER":
            first = cur["authors"][0] if cur["authors"] else ""
            out.append({"raw": " ".join(x for x in (first, cur["year"] or "", cur["title"], cur["journal"]) if x),
                        "title": cur["title"], "doi": cur["doi"], "pmid": cur["pmid"], "arxiv": None, "year": cur["year"]})
            cur = None
    return out


def _plain(text: str) -> list[dict]:
    lines = [ln.rstrip() for ln in text.splitlines()]
    numbered = sum(1 for ln in lines if _NUMBERED.match(ln))
    chunks: list[str] = []
    if numbered >= 2:  # "1. ...", "[2] ..." – a reference may wrap over several lines
        for ln in lines:
            if _NUMBERED.match(ln) or not chunks:
                chunks.append(ln)
            elif ln.strip():
                chunks[-1] += " " + ln.strip()
    else:
        block: list[str] = []
        # blank lines between multi-line references -> one reference per block, else one per line
        blank_separated = any(not ln.strip() for ln in lines) and any(
            lines[i].strip() and lines[i + 1].strip() for i in range(len(lines) - 1))
        for ln in lines + [""]:
            if not ln.strip():
                if block:
                    chunks.append(" ".join(block))
                    block = []
            elif blank_separated:
                block.append(ln.strip())
            else:
                chunks.append(ln.strip())
    out = []
    for c in chunks:
        raw = _NUMBERED.sub("", " ".join(c.split())).strip()
        if len(raw) < 15:
            continue
        ids = id_import.parse_ids(raw)
        doi = next((v for k, v in ids if k == "doi"), None)
        arxiv = next((v for k, v in ids if k == "arxiv"), None)
        pmid = next((v for k, v in ids if k == "pmid" and re.search(r"PMID|pubmed", raw, re.I)), None)
        year = (re.findall(r"\b(?:19|20)\d{2}\b", raw) or [None])[0]
        out.append({"raw": raw, "title": "", "doi": doi, "pmid": pmid, "arxiv": arxiv, "year": year})
    return out


def parse(text: str) -> list[dict]:
    if _BIB_ENTRY.search(text or ""):
        items = _bib(text)
    elif re.search(r"^TY  - ", text or "", re.M):
        items = _ris(text)
    else:
        items = _plain(text or "")
    return items[:MAX_ENTRIES]


def _search(metadata, entry: dict) -> str | None:
    """DOI of the work this reference describes, by Crossref bibliographic search."""
    query = entry["raw"][:300]
    for hit in metadata.crossref_search(query, rows=3) or []:
        title = (hit.get("title") or "").lower()
        if len(title) < 12 or not hit.get("doi"):
            continue
        target = (entry.get("title") or entry["raw"]).lower()
        score = fuzz.ratio(title, target) if entry.get("title") else fuzz.partial_ratio(title, target)
        if score >= 90:
            return norm_doi(hit["doi"])
    return None


def match(pipeline, entry: dict) -> dict:
    """Find the work; report whether it is in the library and whether a free PDF exists."""
    out = {"raw": entry["raw"][:400]}
    kind, value = None, None
    for k in ("doi", "pmid", "arxiv"):
        if entry.get(k):
            kind, value = ("pmid" if k == "pmid" else k), entry[k]
            break
    try:
        if kind is None:
            doi = _search(pipeline.metadata, entry)
            if doi:
                kind, value = "doi", doi
        if kind is None:
            return {**out, "status": "not_found"}
        rec = id_import.resolve(pipeline.metadata, kind, value)
    except Exception as exc:  # noqa: BLE001 - network trouble is reported per entry
        return {**out, "status": "error", "message": f"{type(exc).__name__}: {str(exc)[:120]}"}
    if not rec or not rec.get("title"):
        return {**out, "status": "not_found"}
    out.update(kind=kind, id=value, title=rec["title"], year=rec.get("year"), doi=rec.get("doi"),
               author=(rec.get("authors") or [""])[0], oa=bool(rec.get("oa_urls")))
    pid = id_import.in_library(pipeline.registry, rec)
    if pid is not None:
        return {**out, "status": "already", "paper_id": pid}
    return {**out, "status": "found"}


# ------------------------------------------------------------------ background lookups
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def start(pipeline, text: str) -> dict:
    entries = parse(text)
    token = secrets.token_urlsafe(8)
    job = {"token": token, "total": len(entries), "done": 0, "items": [], "finished": not entries}
    with _lock:
        if len(_jobs) > 20:
            for k in [k for k, v in _jobs.items() if v["finished"]][:10]:
                _jobs.pop(k, None)
        _jobs[token] = job

    def run() -> None:
        try:
            for e in entries:
                item = match(pipeline, e)
                with _lock:
                    job["items"].append(item)
                    job["done"] += 1
        finally:
            with _lock:
                job["finished"] = True

    if entries:
        threading.Thread(target=run, daemon=True, name="reflist").start()
    return status(token)


def status(token: str) -> dict:
    with _lock:
        job = _jobs.get(token)
        return json.loads(json.dumps(job)) if job else {"token": token, "missing": True, "finished": True}
