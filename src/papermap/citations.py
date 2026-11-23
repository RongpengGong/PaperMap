"""Citation network from OpenAlex (CLAUDE.md §15.7).

- references of each library paper (stored once, cached)
- citations between library papers
- "missing key papers": works cited by several library papers but not in the library
- recent works citing a library paper (on demand)
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from .doi import normalize
from .metadata import OPENALEX, MetadataClient
from .registry import Registry, now_iso

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS works (
    openalex_id TEXT PRIMARY KEY, doi TEXT, title TEXT, year INTEGER, first_author TEXT,
    journal TEXT, cited_by_count INTEGER, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS paper_refs (
    paper_id INTEGER NOT NULL, ref_id TEXT NOT NULL, PRIMARY KEY (paper_id, ref_id)
);
CREATE TABLE IF NOT EXISTS refs_state (paper_id INTEGER PRIMARY KEY, openalex_id TEXT, cited_by_count INTEGER, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS dismissed_works (openalex_id TEXT PRIMARY KEY, created_at TEXT);
"""


def ensure_schema(registry: Registry) -> None:
    with registry._lock:  # noqa: SLF001
        registry.conn.executescript(SCHEMA)
        registry.conn.commit()


def _short(url: str | None) -> str:
    return (url or "").rsplit("/", 1)[-1]


def _work_row(w: dict) -> tuple:
    auth = (w.get("authorships") or [{}])
    name = ((auth[0].get("author") or {}).get("display_name") or "") if auth else ""
    src = ((w.get("primary_location") or {}).get("source") or {})
    return (_short(w.get("id")), normalize(w.get("doi")), w.get("title") or w.get("display_name") or "",
            w.get("publication_year"), name.split()[-1] if name else "", src.get("display_name") or "",
            w.get("cited_by_count") or 0, now_iso())


def _store_works(registry: Registry, works: list[dict]) -> None:
    with registry._lock:  # noqa: SLF001
        registry.conn.executemany("INSERT OR REPLACE INTO works VALUES (?,?,?,?,?,?,?,?)", [_work_row(w) for w in works])
        registry.conn.commit()


WORK_FIELDS = "id,doi,title,publication_year,cited_by_count,authorships,primary_location"


def fetch_paper(registry: Registry, client: MetadataClient, paper_id: int) -> int:
    """References of one library paper (+ their details). Returns the number of references."""
    ensure_schema(registry)
    p = registry.get_paper(paper_id)
    if p is None or not p["doi"]:
        return 0
    params = {"select": "id,referenced_works,cited_by_count", **client._openalex_params()}  # noqa: SLF001
    w = client._get_json(f"{OPENALEX}/works/doi:{p['doi']}", params)  # noqa: SLF001
    if not w:
        registry.execute("INSERT OR REPLACE INTO refs_state VALUES (?,?,?,?)", (paper_id, None, None, now_iso()))
        return 0
    oid = _short(w.get("id"))
    refs = [_short(r) for r in w.get("referenced_works") or []]
    with registry._lock:  # noqa: SLF001
        registry.conn.execute("DELETE FROM paper_refs WHERE paper_id=?", (paper_id,))
        registry.conn.executemany("INSERT OR IGNORE INTO paper_refs VALUES (?,?)", [(paper_id, r) for r in refs])
        registry.conn.execute("INSERT OR REPLACE INTO refs_state VALUES (?,?,?,?)",
                              (paper_id, oid, w.get("cited_by_count") or 0, now_iso()))
        registry.conn.commit()
    if not p["openalex_id"] and oid:
        registry.update_paper(paper_id, openalex_id=oid)
    known = {r["openalex_id"] for r in registry.query("SELECT openalex_id FROM works")}
    todo = [r for r in refs if r not in known]
    for i in range(0, len(todo), 50):
        batch = todo[i:i + 50]
        res = client._get_json(f"{OPENALEX}/works", {  # noqa: SLF001
            "filter": "openalex_id:" + "|".join(batch), "select": WORK_FIELDS, "per-page": 50,
            **client._openalex_params()})  # noqa: SLF001
        _store_works(registry, (res or {}).get("results", []))
    return len(refs)


def fetch_missing(registry: Registry, client: MetadataClient, max_age_days: int = 30) -> int:
    ensure_schema(registry)
    cutoff = (date.today() - timedelta(days=max_age_days)).isoformat()
    n = 0
    for r in registry.query(
            "SELECT p.id FROM papers p LEFT JOIN refs_state s ON s.paper_id=p.id "
            "WHERE p.status='done' AND p.doi IS NOT NULL AND (s.paper_id IS NULL OR s.fetched_at < ?)", (cutoff,)):
        try:
            fetch_paper(registry, client, r["id"])
            n += 1
        except Exception as exc:  # noqa: BLE001 - network problems: try again next time
            log.warning("citations for paper %s: %s", r["id"], exc)
            break
    return n


def _library_ids(registry: Registry) -> tuple[dict[str, int], dict[str, int]]:
    by_oid, by_doi = {}, {}
    for p in registry.query("SELECT p.id, p.doi, COALESCE(p.openalex_id, s.openalex_id) AS oid FROM papers p "
                            "LEFT JOIN refs_state s ON s.paper_id=p.id WHERE p.status != 'ignored'"):
        if p["oid"]:
            by_oid[p["oid"]] = p["id"]
        if p["doi"]:
            by_doi[p["doi"]] = p["id"]
    for d in registry.query("SELECT paper_id, doi FROM paper_dois"):
        by_doi[d["doi"]] = d["paper_id"]
    return by_oid, by_doi


def library_edges(registry: Registry) -> list[tuple[int, int]]:
    """(citing paper id, cited paper id) inside the library."""
    ensure_schema(registry)
    by_oid, by_doi = _library_ids(registry)
    edges = set()
    for r in registry.query("SELECT pr.paper_id, pr.ref_id, w.doi FROM paper_refs pr LEFT JOIN works w ON w.openalex_id=pr.ref_id"):
        target = by_oid.get(r["ref_id"]) or (by_doi.get(r["doi"]) if r["doi"] else None)
        if target and target != r["paper_id"]:
            edges.add((r["paper_id"], target))
    return sorted(edges)


def missing_key_papers(registry: Registry, min_citing: int = 2, limit: int = 100) -> list[dict]:
    """Works cited by >= min_citing library papers that are not in the library."""
    ensure_schema(registry)
    by_oid, by_doi = _library_ids(registry)
    dismissed = {r["openalex_id"] for r in registry.query("SELECT openalex_id FROM dismissed_works")}
    rows = registry.query(
        "SELECT pr.ref_id, count(DISTINCT pr.paper_id) AS n, group_concat(DISTINCT pr.paper_id) AS citing, "
        "w.doi, w.title, w.year, w.first_author, w.journal, w.cited_by_count "
        "FROM paper_refs pr LEFT JOIN works w ON w.openalex_id=pr.ref_id GROUP BY pr.ref_id HAVING n >= ? "
        "ORDER BY n DESC, w.cited_by_count DESC LIMIT ?", (min_citing, limit * 3))
    out = []
    for r in rows:
        if r["ref_id"] in by_oid or r["ref_id"] in dismissed or (r["doi"] and r["doi"] in by_doi):
            continue
        out.append({**dict(r), "citing": [int(x) for x in (r["citing"] or "").split(",") if x]})
        if len(out) >= limit:
            break
    return out


def citing_works(registry: Registry, client: MetadataClient, paper_id: int, limit: int = 25) -> list[dict]:
    """Most recent works citing a library paper (live query, cached for a day by the HTTP cache)."""
    ensure_schema(registry)
    st = registry.one("SELECT openalex_id FROM refs_state WHERE paper_id=?", (paper_id,))
    p = registry.get_paper(paper_id)
    oid = (st["openalex_id"] if st else None) or (p["openalex_id"] if p else None)
    if not oid:
        return []
    res = client._get_json(f"{OPENALEX}/works", {  # noqa: SLF001
        "filter": f"cites:{oid}", "sort": "publication_date:desc", "per-page": limit, "select": WORK_FIELDS,
        **client._openalex_params()}, cache=False)  # noqa: SLF001
    works = (res or {}).get("results", [])
    _store_works(registry, works)
    by_oid, by_doi = _library_ids(registry)
    out = []
    for w in works:
        row = _work_row(w)
        out.append({"openalex_id": row[0], "doi": row[1], "title": row[2], "year": row[3], "first_author": row[4],
                    "journal": row[5], "cited_by_count": row[6],
                    "in_library": by_oid.get(row[0]) or (by_doi.get(row[1]) if row[1] else None)})
    return out
