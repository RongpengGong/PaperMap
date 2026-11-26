"""Add papers by identifier: pasted DOIs, PubMed IDs, PMC IDs, arXiv IDs or their links.

Each identifier is resolved with OpenAlex (free single lookups). A paper already in the
library is reported, not imported again. Otherwise it becomes a tracking candidate and
goes through `tracking.take_in`: a legal open-access PDF is downloaded and imported the
normal way; without one the candidate waits as "to download" and a PDF dragged in later
is matched by DOI (same flow as new-literature tracking, CLAUDE.md §16.2 item 6).
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from urllib.parse import quote

from . import tracking
from .doi import normalize as norm_doi
from .registry import Registry, now_iso

MAX_IDS = 50

_DOI_URL = re.compile(r"(?:doi\.org/|doi:\s*)(10\.\d{4,9}/[^\s\"'<>]+)", re.I)
_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+)")
_PMC = re.compile(r"\bPMC\s?(\d{4,9})\b", re.I)
_PUBMED_URL = re.compile(r"pubmed(?:\.ncbi\.nlm\.nih\.gov|/)/?(\d{1,9})", re.I)
_PMID = re.compile(r"\bPMID\s*[:：]?\s*(\d{1,9})\b", re.I)
_ARXIV = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv\s*[:：]\s*)(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
_BARE_ARXIV = re.compile(r"^(\d{4}\.\d{4,5})(?:v\d+)?$")
_BARE_NUM = re.compile(r"^\d{5,9}$")
_SEP = re.compile(r"[\s,;，；、]+")


def parse_ids(text: str) -> list[tuple[str, str]]:
    """(kind, value) pairs in the order found; kind is doi / pmid / pmcid / arxiv. Duplicates removed."""
    out: list[tuple[str, str]] = []

    def add(kind: str, value: str) -> None:
        if (kind, value) not in out:
            out.append((kind, value))

    for token in _SEP.split(text or ""):
        token = token.strip().strip("<>()[]\"'")
        if not token:
            continue
        m = _ARXIV.search(token) or (_BARE_ARXIV.match(token) if "/" not in token else None)
        if m:
            add("arxiv", m.group(1))
            continue
        m = _DOI_URL.search(token) or _DOI.search(token)
        if m:
            d = norm_doi(m.group(1))
            if d:
                if d.startswith("10.48550/arxiv."):
                    add("arxiv", d.split("arxiv.", 1)[1])
                else:
                    add("doi", d)
            continue
        m = _PMC.search(token)
        if m:
            add("pmcid", "PMC" + m.group(1))
            continue
        m = _PUBMED_URL.search(token)
        if m:
            add("pmid", m.group(1))
            continue
        if _BARE_NUM.match(token):
            add("pmid", token)
    # "PMID: 123" written with a space between label and number
    for m in _PMID.finditer(text or ""):
        add("pmid", m.group(1))
    return out[:MAX_IDS]


def _openalex_path(kind: str, value: str) -> str:
    if kind == "doi":
        return f"doi:{quote(value, safe='/')}"
    if kind == "arxiv":
        return f"doi:10.48550/arxiv.{value}"
    return f"{kind}:{value}"


def resolve(metadata, kind: str, value: str) -> dict | None:
    """A tracking-style record for the identifier, or None when OpenAlex does not know it."""
    body = metadata._get_json(f"{tracking.OpenAlexSource.API}/{_openalex_path(kind, value)}",  # noqa: SLF001
                              {"select": tracking.OpenAlexSource.SELECT, **metadata._openalex_params()},  # noqa: SLF001
                              cache=False)
    if not body and kind == "doi":  # very new DOIs reach Crossref before OpenAlex
        cr = metadata.crossref_work(value)
        if cr and cr.get("title"):
            authors = [a.get("name", "") for a in cr.get("authors") or []]
            return {"doi": value, "title": cr["title"], "year": cr.get("year"), "journal": cr.get("journal") or "",
                    "authors": authors[:30], "oa_urls": [], "landing": f"https://doi.org/{value}", "abstract": ""}
        return None
    if not body:
        return None
    rec = tracking.OpenAlexSource.parse(body)
    if kind == "arxiv":
        rec["oa_urls"] = list(dict.fromkeys([f"https://arxiv.org/pdf/{value}", *(rec.get("oa_urls") or [])]))
        rec["is_preprint"] = True
    return rec


def in_library(registry: Registry, rec: dict) -> int | None:
    doi = (rec.get("doi") or "").lower()
    if doi:
        r = registry.one("SELECT id FROM papers WHERE lower(doi)=? AND status!='ignored'", (doi,)) or \
            registry.one("SELECT d.paper_id AS id FROM paper_dois d JOIN papers p ON p.id=d.paper_id "
                         "WHERE lower(d.doi)=? AND p.status!='ignored'", (doi,))
        if r:
            return int(r["id"])
    if rec.get("openalex_id"):
        r = registry.one("SELECT id FROM papers WHERE openalex_id=? AND status!='ignored'", (rec["openalex_id"],))
        if r:
            return int(r["id"])
    key = tracking.title_key(rec.get("title") or "")
    if len(key) >= 20:
        for r in registry.query("SELECT id, title FROM papers WHERE title IS NOT NULL AND status!='ignored'"):
            if tracking.title_key(r["title"]) == key:
                return int(r["id"])
    return None


def ignored_paper(registry: Registry, rec: dict) -> int | None:
    doi = (rec.get("doi") or "").lower()
    r = registry.one("SELECT id FROM papers WHERE lower(doi)=? AND status='ignored'", (doi,)) if doi else None
    return int(r["id"]) if r else None


def store_candidate(registry: Registry, rec: dict) -> int:
    """Create (or reuse) the tracking candidate for a manually added paper."""
    tracking.ensure_schema(registry)
    old = tracking.find_existing(registry, rec)
    now = now_iso()
    if old is not None:
        urls = list(dict.fromkeys(json.loads(old["oa_urls_json"]) + list(rec.get("oa_urls") or [])))
        registry.execute("UPDATE track_candidates SET oa_urls_json=?, doi=COALESCE(doi, ?), updated_at=? WHERE id=?",
                         (json.dumps(urls), rec.get("doi"), now, old["id"]))
        return int(old["id"])
    with registry._lock, registry.conn:  # noqa: SLF001
        cur = registry.conn.execute(
            "INSERT INTO track_candidates(doi, openalex_id, pmid, title, title_key, abstract, year, pub_date, journal, "
            "authors_json, is_preprint, oa_urls_json, landing, created_at, updated_at, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'wanted')",
            (rec.get("doi"), rec.get("openalex_id"), rec.get("pmid"), rec["title"], tracking.title_key(rec["title"]),
             rec.get("abstract") or "", rec.get("year"), rec.get("pub_date"), rec.get("journal") or "",
             json.dumps(rec.get("authors") or [], ensure_ascii=False), int(bool(rec.get("is_preprint"))),
             json.dumps(rec.get("oa_urls") or []), rec.get("landing") or "", now, now))
        return int(cur.lastrowid)


def add_one(pipeline, kind: str, value: str, take_in=None) -> dict:
    """Resolve one identifier and bring the paper in. Returns a status dict for the UI."""
    registry = pipeline.registry
    base = {"kind": kind, "id": value}
    try:
        rec = resolve(pipeline.metadata, kind, value)
    except Exception as exc:  # noqa: BLE001 - network trouble is reported per item
        return {**base, "status": "error", "message": f"{type(exc).__name__}: {str(exc)[:160]}"}
    if not rec or not rec.get("title"):
        return {**base, "status": "not_found"}
    out = {**base, "title": rec["title"], "doi": rec.get("doi"), "year": rec.get("year"),
           "author": (rec.get("authors") or [""])[0]}
    pid = in_library(registry, rec)
    if pid is not None:
        return {**out, "status": "already", "paper_id": pid}
    pid = ignored_paper(registry, rec)
    if pid is not None:  # deleted earlier: restoring is free and keeps fields and groups
        return {**out, "status": "deleted", "paper_id": pid}
    cid = store_candidate(registry, rec)
    res = (take_in or tracking.take_in)(pipeline, cid)
    if res.get("already"):
        return {**out, "status": "already", "candidate_id": cid}
    if res.get("downloaded"):
        return {**out, "status": "downloading", "candidate_id": cid}
    return {**out, "status": "wanted", "candidate_id": cid, "landing": res.get("landing") or rec.get("landing") or ""}


# ------------------------------------------------------------------ background batches
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def start(pipeline, text: str, take_in=None) -> dict:
    ids = parse_ids(text)
    token = secrets.token_urlsafe(8)
    job = {"token": token, "total": len(ids), "done": 0, "items": [], "finished": not ids}
    with _jobs_lock:
        if len(_jobs) > 20:  # keep memory bounded
            for k in [k for k, v in _jobs.items() if v["finished"]][:10]:
                _jobs.pop(k, None)
        _jobs[token] = job

    def run() -> None:
        try:
            for kind, value in ids:
                item = add_one(pipeline, kind, value, take_in)
                with _jobs_lock:
                    job["items"].append(item)
                    job["done"] += 1
        finally:
            with _jobs_lock:
                job["finished"] = True
            n = sum(1 for i in job["items"] if i["status"] in ("downloading", "wanted"))
            if n:
                pipeline.registry.log("info", f"Added {n} paper(s) by identifier")

    if ids:
        threading.Thread(target=run, daemon=True, name="id-import").start()
    return status(token)


def status(token: str) -> dict:
    with _jobs_lock:
        job = _jobs.get(token)
        return json.loads(json.dumps(job)) if job else {"token": token, "missing": True, "finished": True}
