"""New-literature tracking around the user's own library (stage 7 task 6, CLAUDE.md §16.2).

Watches: one per active field (auto), papers citing the library, followed authors, open evidence
gaps. Sources (OpenAlex first; PubMed / bioRxiv plug in through SOURCES) are queried from each
watch's *last successful date*, so a computer that was off for days catches up. Every record is
de-duplicated into one candidate (DOI, then source ids, then title); each (watch, source) that
found it is kept as a reason. Scoring is free; the top few dozen can be re-ranked by the model.
A candidate is only a suggestion to read: "take in" fetches a legal open-access PDF and hands it
to the normal import, which does the usual de-duplication, card, classification and gap matching.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from rapidfuzz import fuzz

from .doi import normalize as norm_doi
from .i18n import L
from .registry import Registry, now_iso

log = logging.getLogger(__name__)

KINDS = {"field": L("学科"), "cites": L("引用了库内论文"), "author": L("关注的作者"), "gap": L("证据缺口")}
STATUSES = {"new": L("新的"), "later": L("稍后看"), "wanted": L("待下载"), "imported": L("已收进来"),
            "dismissed": L("不感兴趣")}
SOURCE_NAMES = {"openalex": "OpenAlex", "pubmed": "PubMed", "biorxiv": "bioRxiv / medRxiv"}
OVERLAP_DAYS = 7          # re-query a week before the last success: indexing lags publication dates
FIRST_LOOKBACK_DAYS = 30  # a new watch starts this far back
RUN_EVERY_H = 20
MODEL_PROMPT_VERSION = "tracking_rank_v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS track_watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL, ref TEXT NOT NULL, label TEXT NOT NULL, query TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1, auto INTEGER NOT NULL DEFAULT 1, query_edited INTEGER NOT NULL DEFAULT 0,
    user_disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(kind, ref)
);
CREATE TABLE IF NOT EXISTS track_cursors (
    watch_id INTEGER NOT NULL REFERENCES track_watches(id) ON DELETE CASCADE, source TEXT NOT NULL,
    last_success TEXT, last_run_at TEXT, last_error TEXT, last_found INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (watch_id, source)
);
CREATE TABLE IF NOT EXISTS track_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doi TEXT UNIQUE, openalex_id TEXT, pmid TEXT, title TEXT NOT NULL, title_key TEXT NOT NULL,
    abstract TEXT NOT NULL DEFAULT '', year INTEGER, pub_date TEXT, journal TEXT NOT NULL DEFAULT '',
    authors_json TEXT NOT NULL DEFAULT '[]', is_preprint INTEGER NOT NULL DEFAULT 0,
    oa_urls_json TEXT NOT NULL DEFAULT '[]', landing TEXT NOT NULL DEFAULT '',
    lib_refs_json TEXT NOT NULL DEFAULT '[]', gaps_json TEXT NOT NULL DEFAULT '[]',
    score REAL NOT NULL DEFAULT 0, model_score REAL, model_reason TEXT,
    status TEXT NOT NULL DEFAULT 'new', paper_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_track_cand_status ON track_candidates(status, score);
CREATE INDEX IF NOT EXISTS idx_track_cand_oa ON track_candidates(openalex_id);
CREATE INDEX IF NOT EXISTS idx_track_cand_title ON track_candidates(title_key);
CREATE TABLE IF NOT EXISTS track_hits (
    candidate_id INTEGER NOT NULL REFERENCES track_candidates(id) ON DELETE CASCADE,
    watch_id INTEGER NOT NULL REFERENCES track_watches(id) ON DELETE CASCADE,
    source TEXT NOT NULL, found_at TEXT NOT NULL,
    PRIMARY KEY (candidate_id, watch_id, source)
);
CREATE TABLE IF NOT EXISTS track_state (key TEXT PRIMARY KEY, value TEXT);
"""


def ensure_schema(registry: Registry) -> None:
    with registry._lock:  # noqa: SLF001
        registry.conn.executescript(SCHEMA)
        registry.conn.commit()


def settings(config) -> dict:
    t = config.raw.get("tracking") or {}
    return {"enabled": t.get("enabled", True) is not False, "model_rank": t.get("model_rank", True) is not False,
            "digest": t.get("digest", True) is not False,
            "model_top": int(t.get("model_top", 30) or 0), "per_query": int(t.get("per_query", 50) or 50)}


def _state(registry, key, value=None):
    if value is None:
        r = registry.one("SELECT value FROM track_state WHERE key=?", (key,))
        return r["value"] if r else None
    registry.execute("INSERT OR REPLACE INTO track_state(key, value) VALUES (?,?)", (key, value))
    return value


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())[:200]


# ------------------------------------------------------------------ watches
def _field_query(tax_cat, row, limit: int = 8) -> str:
    """OpenAlex boolean search: the field's English name OR its synonyms (as quoted phrases)."""
    words = [re.sub(r"\s*\(.*?\)", "", row["name_en"] or "")] + list(getattr(tax_cat, "synonyms", ()) or [])
    seen, parts = set(), []
    for w in words:
        w = " ".join(str(w).replace('"', " ").split())
        if len(w) < 3 or any(0x3400 <= ord(ch) <= 0x9FFF for ch in w) or w.lower() in seen:
            continue
        seen.add(w.lower())
        parts.append(f'"{w}"' if " " in w or "-" in w else w)
    return " OR ".join(parts[:limit])


def sync_watches(registry: Registry, taxonomy=None) -> None:
    """Keep the automatic watches in step with the library (fields, citing papers, open gaps)."""
    ensure_schema(registry)
    from . import gaps as gapmod

    gapmod.ensure_schema(registry)
    cats = {c.slug: c for c in (taxonomy.categories if taxonomy else [])}
    want: dict[tuple[str, str], tuple[str, str]] = {("cites", "library"): ("引用了你库里论文的新文章", "")}
    rows = registry.query(
        "SELECT f.id, f.slug, f.axis, f.name_zh, f.name_en, count(pf.paper_id) AS n FROM fields f "
        "JOIN paper_fields pf ON pf.field_id=f.id AND pf.status='accepted' "
        "WHERE f.status='active' GROUP BY f.id HAVING n >= 2 ORDER BY n DESC")
    # Method fields ("single-cell", "review", "cohort study") are paradigms, far too broad on their own:
    # they only count together with the library's own topics (HCC, fibrosis, tumour microenvironment…).
    topic_q = " OR ".join(q for q in (_field_query(cats.get(f["slug"]), f, 3) for f in rows if f["axis"] == "topic")
                          if q)
    for f in rows:
        q = _field_query(cats.get(f["slug"]), f)
        if f["axis"] != "topic":
            q = f"({q}) AND ({topic_q})" if q and topic_q else ""
        if q:
            want[("field", f["slug"])] = (f["name_zh"] or f["name_en"], q)
    for g in registry.query("SELECT g.id, g.text, g.terms_json FROM question_gaps g JOIN evidence_questions q "
                            "ON q.id=g.question_id WHERE g.status='open' AND q.status='active'"):
        terms = json.loads(g["terms_json"] or "[]")
        if terms:
            want[("gap", str(g["id"]))] = (g["text"], " OR ".join(f'"{t}"' if " " in t or "-" in t else t for t in terms))
    have = {(r["kind"], r["ref"]): r for r in registry.query("SELECT * FROM track_watches WHERE auto=1")}
    with registry._lock, registry.conn:  # noqa: SLF001
        for key, (label, q) in want.items():
            old = have.get(key)
            if old is None:
                registry.conn.execute(
                    "INSERT OR IGNORE INTO track_watches(kind, ref, label, query, created_at) VALUES (?,?,?,?,?)",
                    (*key, label, q, now_iso()))
            else:  # a query the user edited by hand is kept
                new_q = old["query"] if old["query_edited"] else q
                revive = not old["enabled"] and not old["user_disabled"]  # stopped by a sync, not by the user
                if old["label"] != label or old["query"] != new_q or revive:
                    registry.conn.execute("UPDATE track_watches SET label=?, query=?, enabled=? WHERE id=?",
                                          (label, new_q, 1 if revive else old["enabled"], old["id"]))
        for key, old in have.items():
            if key not in want:  # field emptied, gap closed: stop watching (history stays)
                registry.conn.execute("UPDATE track_watches SET enabled=0 WHERE id=? AND kind IN ('field','gap')",
                                      (old["id"],))


def watches(registry: Registry) -> list[dict]:
    ensure_schema(registry)
    out = []
    for w in registry.query("SELECT * FROM track_watches ORDER BY kind='cites' DESC, kind, label"):
        d = dict(w)
        d["kind_label"] = KINDS.get(d["kind"], d["kind"])
        d["cursors"] = [dict(c) for c in registry.query("SELECT * FROM track_cursors WHERE watch_id=?", (w["id"],))]
        out.append(d)
    return out


def follow_author(registry: Registry, author_id: str, name: str) -> int:
    ensure_schema(registry)
    aid = author_id.rsplit("/", 1)[-1]
    if not re.fullmatch(r"A\d+", aid):
        raise ValueError("无效的作者 ID")
    with registry._lock, registry.conn:  # noqa: SLF001
        registry.conn.execute(
            "INSERT INTO track_watches(kind, ref, label, query, auto, created_at) VALUES ('author',?,?,'',0,?) "
            "ON CONFLICT(kind, ref) DO UPDATE SET enabled=1, label=excluded.label", (aid, name[:120], now_iso()))
    return int(registry.one("SELECT id FROM track_watches WHERE kind='author' AND ref=?", (aid,))["id"])


def set_watch(registry: Registry, wid: int, enabled=None, query=None, delete=False) -> None:
    w = registry.one("SELECT * FROM track_watches WHERE id=?", (wid,))
    if w is None:
        raise ValueError("这个订阅不存在。")
    if delete:
        if w["kind"] != "author":
            raise ValueError("自动订阅不能删除，可以停用。")
        registry.execute("DELETE FROM track_watches WHERE id=?", (wid,))
        return
    if enabled is not None:
        registry.execute("UPDATE track_watches SET enabled=?, user_disabled=? WHERE id=?",
                         (1 if enabled else 0, 0 if enabled else 1, wid))
    if query is not None:
        q = " ".join(str(query).split())[:500]
        if not q:
            raise ValueError("检索式不能为空。")
        registry.execute("UPDATE track_watches SET query=?, query_edited=1 WHERE id=?", (q, wid))


# ------------------------------------------------------------------ sources
class OpenAlexSource:
    name = "openalex"
    API = "https://api.openalex.org/works"
    SELECT = ("id,doi,display_name,publication_date,publication_year,authorships,primary_location,"
              "best_oa_location,locations,abstract_inverted_index,type,referenced_works,ids")

    def __init__(self, metadata):
        self.md = metadata

    def supports(self, kind: str) -> bool:
        return kind in ("field", "cites", "author", "gap")

    def _get(self, flt: str, search: bool, per_page: int) -> list[dict]:
        params = {"filter": flt, "per_page": per_page, "select": self.SELECT,
                  "sort": "relevance_score:desc" if search else "publication_date:desc",
                  **self.md._openalex_params()}  # noqa: SLF001
        body = self.md._get_json(self.API, params, cache=False)  # noqa: SLF001
        if body is None:
            raise RuntimeError("OpenAlex returned no result")
        return [self.parse(w) for w in body.get("results", [])]

    def fetch(self, registry: Registry, watch, since: date, per_page: int) -> list[dict]:
        base = f"from_publication_date:{since.isoformat()},type:article|preprint|review|letter"
        if watch["kind"] in ("field", "gap"):
            return self._get(f"title_and_abstract.search:{watch['query']},{base}", True, per_page)
        if watch["kind"] == "author":
            return self._get(f"author.id:{watch['ref']},{base}", False, 100)
        if watch["kind"] == "cites":
            ids = [r["openalex_id"] for r in registry.query(
                "SELECT openalex_id FROM papers WHERE openalex_id IS NOT NULL AND status='done' ORDER BY id DESC")]
            out = []
            for i in range(0, len(ids), 100):  # OR of at most 100 values per filter
                out += self._get(f"cites:{'|'.join(ids[i:i + 100])},{base}", False, 100)
            return out
        return []

    @staticmethod
    def parse(w: dict) -> dict:
        inv = w.get("abstract_inverted_index") or {}
        abstract = " ".join(word for _, word in sorted((i, word) for word, idx in inv.items() for i in idx))
        loc = w.get("primary_location") or {}
        src = loc.get("source") or {}
        urls = []
        for l in [w.get("best_oa_location")] + list(w.get("locations") or []):
            if l and l.get("is_oa") and l.get("pdf_url") and l["pdf_url"] not in urls:
                urls.append(l["pdf_url"])
        ids = w.get("ids") or {}
        pmcid = (ids.get("pmcid") or "").rstrip("/").rsplit("/", 1)[-1]
        pmcid = f"PMC{pmcid}" if pmcid.isdigit() else pmcid  # OpenAlex sometimes gives the bare number
        if re.fullmatch(r"PMC\d+", pmcid or "") and f"pmc:{pmcid}" not in urls:
            urls.insert(0, f"pmc:{pmcid}")  # PMC's official open-access copy (see pmc_pdf_urls)
        return {
            "doi": norm_doi(w.get("doi")), "openalex_id": (w.get("id") or "").rsplit("/", 1)[-1] or None,
            "pmid": (ids.get("pmid") or "").rsplit("/", 1)[-1] or None,
            "title": (w.get("display_name") or "").strip(), "abstract": abstract,
            "pub_date": w.get("publication_date"), "year": w.get("publication_year"),
            "journal": src.get("display_name") or "",
            "authors": [((a.get("author") or {}).get("display_name") or "") for a in (w.get("authorships") or [])][:30],
            "is_preprint": w.get("type") == "preprint" or src.get("type") == "repository",
            "oa_urls": urls, "landing": loc.get("landing_page_url") or (f"https://doi.org/{norm_doi(w.get('doi'))}" if w.get("doi") else ""),
            "references": [r.rsplit("/", 1)[-1] for r in (w.get("referenced_works") or [])],
        }


# ------------------------------------------------------------------ boolean queries matched locally
_TOKEN = re.compile(r'\s*(\(|\)|"[^"]*"|[^\s()"]+)')


def parse_query(q: str):
    """The watches' search syntax: words, "phrases", AND / OR (AND binds tighter), parentheses.
    Returns a nested tuple tree, or None when empty."""
    toks = [t for t in _TOKEN.findall(q or "") if t.strip()]
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else None

    def term():
        nonlocal pos
        t = peek()
        if t is None:
            return None
        pos += 1
        if t == "(":
            node = expr()
            if peek() == ")":
                pos += 1
            return node
        if t == ")":
            return None
        return ("t", t.strip('"').lower())

    def conj():
        nonlocal pos
        parts = [term()]
        while peek() not in (None, ")") and peek().upper() != "OR":
            if peek().upper() == "AND":
                pos += 1
            parts.append(term())
        parts = [p for p in parts if p]
        return parts[0] if len(parts) == 1 else ("and", parts) if parts else None

    def expr():
        nonlocal pos
        parts = [conj()]
        while peek() is not None and peek().upper() == "OR":
            pos += 1
            parts.append(conj())
        parts = [p for p in parts if p]
        return parts[0] if len(parts) == 1 else ("or", parts) if parts else None

    return expr()


def query_matches(tree, text: str) -> bool:
    if tree is None:
        return False
    kind, val = tree
    if kind == "t":
        return re.search(r"(?<![a-z0-9])" + re.escape(val) + r"(?![a-z0-9])", text) is not None
    hits = (query_matches(t, text) for t in val)
    return all(hits) if kind == "and" else any(hits)


class PubMedSource:
    """NCBI E-utilities (esearch + efetch). Catch-up uses the Entrez date, i.e. when PubMed added
    the record, so a paper indexed late is still found on the next run."""
    name = "pubmed"
    BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    MIN_INTERVAL = 0.4  # NCBI allows 3 requests per second without an API key

    def __init__(self, metadata):
        self.md = metadata
        self._last = 0.0

    def supports(self, kind: str) -> bool:
        return kind in ("field", "gap")

    def _get(self, path: str, params: dict) -> httpx.Response:
        params = {**params, "tool": "PaperMap"}
        if getattr(self.md, "email", ""):
            params["email"] = self.md.email
        delay = 1.0
        for attempt in range(4):
            wait = self._last + self.MIN_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.md.http.get(f"{self.BASE}/{path}", params=params, timeout=40)
            except httpx.HTTPError as exc:
                if attempt == 3:
                    raise RuntimeError(f"PubMed: {exc}") from exc
            else:
                if r.status_code == 200:
                    return r
                if r.status_code not in (429, 500, 502, 503) or attempt == 3:
                    raise RuntimeError(f"PubMed: HTTP {r.status_code}")
            time.sleep(delay)
            delay *= 2
        raise RuntimeError("PubMed: no answer")

    def fetch(self, registry: Registry, watch, since: date, per_page: int) -> list[dict]:
        q = (watch["query"] or "").strip()
        if not q:
            return []
        term = f'({q}) AND ("{since:%Y/%m/%d}"[EDAT] : "3000"[EDAT])'
        ids = self._get("esearch.fcgi", {"db": "pubmed", "term": term, "retmax": per_page, "retmode": "json",
                                         "sort": "relevance"}).json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []
        xml = self._get("efetch.fcgi", {"db": "pubmed", "id": ",".join(ids), "retmode": "xml"}).text
        return self.parse(xml)

    @staticmethod
    def parse(xml: str) -> list[dict]:
        import xml.etree.ElementTree as ET

        out = []
        root = ET.fromstring(xml)
        for art in root.iter("PubmedArticle"):
            cit = art.find("MedlineCitation")
            a = cit.find("Article") if cit is not None else None
            if a is None:
                continue
            text = lambda el: "".join(el.itertext()).strip() if el is not None else ""  # noqa: E731
            ids = {i.get("IdType"): (i.text or "").strip() for i in art.iter("ArticleId")}
            pmid = text(cit.find("PMID")) or ids.get("pubmed")
            doi = norm_doi(ids.get("doi")) or next((norm_doi(e.text) for e in a.iter("ELocationID")
                                                    if e.get("EIdType") == "doi" and e.text), None)
            parts = []
            for ab in a.iter("AbstractText"):
                label = ab.get("Label")
                parts.append((f"{label}: " if label else "") + text(ab))
            authors = []
            for au in a.iter("Author"):
                name = " ".join(x for x in (text(au.find("ForeName")), text(au.find("LastName"))) if x) or text(au.find("CollectiveName"))
                if name:
                    authors.append(name)
            year, pub = None, None
            d = a.find("ArticleDate")
            if d is None:
                d = a.find("Journal/JournalIssue/PubDate")
            if d is not None:
                y, m, dd = text(d.find("Year")), text(d.find("Month")), text(d.find("Day"))
                if y.isdigit():
                    year = int(y)
                    months = {mo: i for i, mo in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                                            "sep", "oct", "nov", "dec"], 1)}
                    mm = int(m) if m.isdigit() else months.get(m[:3].lower(), 1)
                    pub = f"{year:04d}-{mm:02d}-{int(dd) if dd.isdigit() else 1:02d}"
            types = {text(t).lower() for t in a.iter("PublicationType")}
            if types & {"erratum", "published erratum", "retraction of publication", "comment", "editorial"}:
                continue
            pmc = ids.get("pmc") or ""
            out.append({
                "doi": doi, "openalex_id": None, "pmid": pmid,
                "title": re.sub(r"\s+", " ", text(a.find("ArticleTitle"))).strip().rstrip("."),
                "abstract": " ".join(parts), "pub_date": pub, "year": year,
                "journal": text(a.find("Journal/Title")), "authors": authors[:30],
                "is_preprint": "preprint" in types,
                "oa_urls": [f"pmc:{pmc}"] if re.fullmatch(r"PMC\d+", pmc) else [],
                "landing": f"https://doi.org/{doi}" if doi else f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "references": [],
            })
        return out


class BioRxivSource:
    """bioRxiv / medRxiv. The public API lists preprints by posting date but cannot search, so each
    run downloads the window once and every watch's query is matched against title + abstract here."""
    name = "biorxiv"
    API = "https://api.biorxiv.org/details"
    SERVERS = ("biorxiv", "medrxiv")
    MAX_PAGES = 60  # per server and run (100 records each)

    def __init__(self, metadata):
        self.md = metadata
        self._cache: dict[str, list[dict]] = {}
        self._since: dict[str, date] = {}

    def supports(self, kind: str) -> bool:
        return kind in ("field", "gap")

    def _window(self, server: str, since: date) -> list[dict]:
        if server in self._since and self._since[server] <= since:
            return [r for r in self._cache[server] if (r.get("pub_date") or "") >= since.isoformat()]
        recs, cursor, today = [], 0, date.today().isoformat()
        for _ in range(self.MAX_PAGES):
            body = self.md._get_json(f"{self.API}/{server}/{since.isoformat()}/{today}/{cursor}/json", None, cache=False)  # noqa: SLF001
            if body is None:
                raise RuntimeError(f"{server}: no answer")
            batch = body.get("collection") or []
            recs += [self.parse(r, server) for r in batch]
            msg = (body.get("messages") or [{}])[0]
            total = int(msg.get("total") or msg.get("count") or 0)
            cursor += len(batch)
            if not batch or cursor >= total:
                break
        latest: dict[str, dict] = {}
        for r in recs:  # the API lists every version; keep the newest
            if r["doi"] and (r["doi"] not in latest or r["version"] > latest[r["doi"]]["version"]):
                latest[r["doi"]] = r
        self._cache[server], self._since[server] = list(latest.values()), since
        return self._cache[server]

    def fetch(self, registry: Registry, watch, since: date, per_page: int) -> list[dict]:
        tree = parse_query(watch["query"])
        if tree is None:
            return []
        out = []
        for server in self.SERVERS:
            for r in self._window(server, since):
                if query_matches(tree, f"{r['title']} {r['abstract']}".lower()):
                    out.append(r)
        return out[: per_page * 2]

    @staticmethod
    def parse(r: dict, server: str) -> dict:
        doi = norm_doi(r.get("doi"))
        try:
            version = int(r.get("version") or 1)
        except ValueError:
            version = 1
        published = norm_doi(r.get("published")) if (r.get("published") or "NA") != "NA" else None
        authors = [" ".join(reversed([x.strip() for x in a.split(",", 1)])) for a in (r.get("authors") or "").split(";") if a.strip()]
        host = "www.medrxiv.org" if server == "medrxiv" else "www.biorxiv.org"
        return {
            "doi": doi, "openalex_id": None, "pmid": None, "version": version,
            "title": " ".join((r.get("title") or "").split()), "abstract": " ".join((r.get("abstract") or "").split()),
            "pub_date": r.get("date"), "year": int(r["date"][:4]) if (r.get("date") or "")[:4].isdigit() else None,
            "journal": "medRxiv" if server == "medrxiv" else "bioRxiv", "authors": authors[:30],
            "is_preprint": True, "related_dois": [published] if published else [],
            "oa_urls": [f"https://{host}/content/{doi}v{version}.full.pdf"] if doi else [],
            "landing": f"https://doi.org/{doi}" if doi else "", "references": [],
        }


def sources(pipeline) -> dict:
    return {"openalex": OpenAlexSource(pipeline.metadata), "pubmed": PubMedSource(pipeline.metadata),
            "biorxiv": BioRxivSource(pipeline.metadata)}


# ------------------------------------------------------------------ candidates
def _library_keys(registry: Registry) -> tuple[set, set, dict]:
    dois = {r["doi"] for r in registry.query("SELECT doi FROM papers WHERE doi IS NOT NULL")}
    dois |= {r["doi"] for r in registry.query("SELECT doi FROM paper_dois")}
    oa = {r["openalex_id"]: r["id"] for r in registry.query(
        "SELECT id, openalex_id FROM papers WHERE openalex_id IS NOT NULL AND status!='ignored'")}
    titles = {title_key(r["title"]): r["id"] for r in registry.query("SELECT id, title FROM papers WHERE title IS NOT NULL")}
    return {d.lower() for d in dois if d}, set(oa), titles


def _gap_matches(registry: Registry, text: str) -> list[int]:
    low = text.lower()
    out = []
    for g in registry.query("SELECT g.id, g.terms_json FROM question_gaps g JOIN evidence_questions q ON q.id=g.question_id "
                            "WHERE g.status='open' AND q.status='active'"):
        terms = json.loads(g["terms_json"] or "[]")
        if terms and sum(1 for t in terms if t.lower() in low) >= min(2, len(terms)):
            out.append(g["id"])
    return out


def find_existing(registry: Registry, rec: dict):
    for d in [rec.get("doi"), *(rec.get("related_dois") or [])]:  # preprint and published version: one candidate
        if d:
            r = registry.one("SELECT * FROM track_candidates WHERE doi=?", (d,))
            if r:
                return r
    for col in ("openalex_id", "pmid"):
        if rec.get(col):
            r = registry.one(f"SELECT * FROM track_candidates WHERE {col}=?", (rec[col],))
            if r:
                return r
    key = title_key(rec.get("title", ""))
    if len(key) >= 20:
        r = registry.one("SELECT * FROM track_candidates WHERE title_key=?", (key,))
        if r:
            return r
        for r in registry.query("SELECT * FROM track_candidates WHERE year IS ? OR year IS ?",
                                (rec.get("year"), (rec.get("year") or 0) - 1)):
            if fuzz.ratio(key, r["title_key"]) >= 95:
                return r
    return None


def upsert(registry: Registry, rec: dict, watch_id: int, source: str, lib: tuple) -> tuple[int | None, bool]:
    """Store one record as a candidate (or add this watch/source as a reason). None if already in the library."""
    dois, oa_ids, titles = lib
    if any(d and d.lower() in dois for d in rec.get("related_dois") or []):
        return None, False  # a preprint whose published version is already in the library
    if (rec.get("doi") and rec["doi"].lower() in dois) or (rec.get("openalex_id") in oa_ids) \
            or title_key(rec.get("title", "")) in titles or not rec.get("title"):
        return None, False
    lib_refs = sorted(set(rec.get("references") or []) & oa_ids)
    gaps = _gap_matches(registry, f"{rec.get('title', '')} {rec.get('abstract', '')}")
    now = now_iso()
    old = find_existing(registry, rec)
    with registry._lock, registry.conn:  # noqa: SLF001
        if old is None:
            cur = registry.conn.execute(
                "INSERT INTO track_candidates(doi, openalex_id, pmid, title, title_key, abstract, year, pub_date, journal, "
                "authors_json, is_preprint, oa_urls_json, landing, lib_refs_json, gaps_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec.get("doi"), rec.get("openalex_id"), rec.get("pmid"), rec["title"], title_key(rec["title"]),
                 rec.get("abstract") or "", rec.get("year"), rec.get("pub_date"), rec.get("journal") or "",
                 json.dumps(rec.get("authors") or [], ensure_ascii=False), int(bool(rec.get("is_preprint"))),
                 json.dumps(rec.get("oa_urls") or []), rec.get("landing") or "", json.dumps(lib_refs),
                 json.dumps(gaps), now, now))
            cid, new = int(cur.lastrowid), True
        else:
            cid, new = int(old["id"]), False
            merged_urls = list(dict.fromkeys(json.loads(old["oa_urls_json"]) + list(rec.get("oa_urls") or [])))
            merged_refs = sorted(set(json.loads(old["lib_refs_json"])) | set(lib_refs))
            registry.conn.execute(
                "UPDATE track_candidates SET doi=COALESCE(doi, ?), openalex_id=COALESCE(openalex_id, ?), "
                "pmid=COALESCE(pmid, ?), abstract=CASE WHEN abstract='' THEN ? ELSE abstract END, "
                "oa_urls_json=?, lib_refs_json=?, gaps_json=?, updated_at=? WHERE id=?",
                (rec.get("doi"), rec.get("openalex_id"), rec.get("pmid"), rec.get("abstract") or "",
                 json.dumps(merged_urls), json.dumps(merged_refs),
                 json.dumps(sorted(set(json.loads(old["gaps_json"])) | set(gaps))), now, cid))
        registry.conn.execute("INSERT OR IGNORE INTO track_hits(candidate_id, watch_id, source, found_at) VALUES (?,?,?,?)",
                              (cid, watch_id, source, now))
    return cid, new


def rescore(registry: Registry, ids: list[int] | None = None) -> None:
    """Free score: citing the library, followed authors, gaps, fields; plus the model's view if any."""
    rows = registry.query("SELECT * FROM track_candidates" + (
        f" WHERE id IN ({','.join('?' * len(ids))})" if ids else " WHERE status IN ('new','later','wanted')"), tuple(ids or ()))
    kinds = {r["id"]: r["kind"] for r in registry.query("SELECT id, kind FROM track_watches")}
    for c in rows:
        hits = registry.query("SELECT DISTINCT watch_id FROM track_hits WHERE candidate_id=?", (c["id"],))
        wk = [kinds.get(h["watch_id"]) for h in hits]
        s = 3.0 * min(len(json.loads(c["lib_refs_json"])), 5)
        s += 5.0 * wk.count("author") + 4.0 * len(json.loads(c["gaps_json"]))
        nf = wk.count("field")  # broad: the first field counts, more fields add little
        s += min(2.0 + 0.5 * (nf - 1), 3.5) if nf else 0
        if c["pub_date"]:
            try:
                if (date.today() - date.fromisoformat(c["pub_date"][:10])).days <= 30:
                    s += 1
            except ValueError:
                pass
        if c["model_score"] is not None:
            s += float(c["model_score"])
        registry.execute("UPDATE track_candidates SET score=? WHERE id=?", (round(s, 2), c["id"]))


def _profile(registry: Registry) -> str:
    fields = [r["name_en"] or r["name_zh"] for r in registry.query(
        "SELECT f.name_en, f.name_zh, count(*) n FROM fields f JOIN paper_fields pf ON pf.field_id=f.id "
        "WHERE f.status='active' GROUP BY f.id ORDER BY n DESC LIMIT 15")]
    qs = [r["title"] for r in registry.query("SELECT title FROM evidence_questions WHERE status='active' LIMIT 12")]
    gaps = [r["text"] for r in registry.query("SELECT text FROM question_gaps WHERE status='open' LIMIT 12")]
    titles = [r["title"] for r in registry.query(
        "SELECT title FROM papers WHERE status='done' ORDER BY starred DESC, id DESC LIMIT 25")]
    return ("FIELDS: " + "; ".join(fields) + "\nRESEARCH QUESTIONS: " + " | ".join(qs) +
            "\nOPEN EVIDENCE GAPS: " + " | ".join(gaps) + "\nRECENT / STARRED PAPERS IN THE LIBRARY:\n- " + "\n- ".join(titles))


def model_rank(llm, registry: Registry, top: int = 30) -> int:
    """Re-rank the best-scoring unranked candidates with the main model (one call)."""
    rows = registry.query("SELECT id, title, abstract, journal, year FROM track_candidates WHERE status='new' "
                          "AND model_score IS NULL ORDER BY score DESC, id DESC LIMIT ?", (top,))
    if not rows:
        return 0
    refs = {f"C{r['id']}": r["id"] for r in rows}
    items = "\n\n".join(f"[C{r['id']}] {r['title']} ({r['journal']} {r['year'] or ''})\n{(r['abstract'] or '')[:900]}"
                        for r in rows)
    system = ("You help a biomedical researcher triage new papers against their own library. For each candidate, "
              "rate 0-10 how worth reading it is for THIS researcher (10 = directly answers one of their questions or "
              "gaps; 0 = unrelated). Do not reward novelty or journal prestige by themselves. Return JSON "
              "{\"ranks\": [{\"id\": \"C12\", \"score\": 7, \"reason\": \"one short sentence\"}]}. "
              "Write the reason in the same language as the research questions.")

    def validate(data):
        ok = [x for x in (data.get("ranks") or []) if isinstance(x, dict) and x.get("id") in refs
              and isinstance(x.get("score"), (int, float))]
        if not ok:
            return ["'ranks' must list the candidates with numeric scores"]
        data["ranks"] = ok
        return []

    res = llm.chat_json(system, _profile(registry) + "\n\nCANDIDATES:\n" + items, validate,
                        purpose="tracking_rank", max_tokens=3000)
    for x in res.data["ranks"]:
        registry.execute("UPDATE track_candidates SET model_score=?, model_reason=? WHERE id=?",
                         (max(0.0, min(10.0, float(x["score"]))), str(x.get("reason", ""))[:300], refs[x["id"]]))
    rescore(registry, list(refs.values()))
    return len(res.data["ranks"])


# ------------------------------------------------------------------ running
_run_lock = threading.Lock()


def run(pipeline, force: bool = False) -> dict:
    """One tracking round over every enabled watch and source. Safe to call repeatedly."""
    registry, config = pipeline.registry, pipeline.config
    ensure_schema(registry)
    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "message": "正在检查中"}
    try:
        try:
            tax = pipeline.taxonomy()
        except Exception:  # noqa: BLE001
            tax = None
        sync_watches(registry, tax)
        sync_library(registry)
        st = settings(config)
        today = date.today()
        lib = _library_keys(registry)
        found_new, errors, touched = 0, [], []
        srcs = sources(pipeline)  # once per run: bioRxiv reuses one download for every watch
        for w in registry.query("SELECT * FROM track_watches WHERE enabled=1"):
            for name, src in srcs.items():
                if not src.supports(w["kind"]):
                    continue
                cur = registry.one("SELECT * FROM track_cursors WHERE watch_id=? AND source=?", (w["id"], name))
                last = date.fromisoformat(cur["last_success"]) if cur and cur["last_success"] else None
                if last == today and not force:
                    continue
                since = (last - timedelta(days=OVERLAP_DAYS)) if last else today - timedelta(days=FIRST_LOOKBACK_DAYS)
                try:
                    recs = src.fetch(registry, w, since, st["per_query"])
                except Exception as exc:  # noqa: BLE001 - one source failing never stops the others
                    msg = str(exc)[:300]
                    errors.append(f"{SOURCE_NAMES.get(name, name)} · {w['label']}: {msg}")
                    registry.execute(
                        "INSERT INTO track_cursors(watch_id, source, last_run_at, last_error) VALUES (?,?,?,?) "
                        "ON CONFLICT(watch_id, source) DO UPDATE SET last_run_at=excluded.last_run_at, last_error=excluded.last_error",
                        (w["id"], name, now_iso(), msg))
                    continue
                n_new = 0
                for rec in recs:
                    cid, new = upsert(registry, rec, w["id"], name, lib)
                    if cid:
                        touched.append(cid)
                        n_new += new
                found_new += n_new
                registry.execute(
                    "INSERT INTO track_cursors(watch_id, source, last_success, last_run_at, last_error, last_found) "
                    "VALUES (?,?,?,?,NULL,?) ON CONFLICT(watch_id, source) DO UPDATE SET last_success=excluded.last_success, "
                    "last_run_at=excluded.last_run_at, last_error=NULL, last_found=excluded.last_found",
                    (w["id"], name, today.isoformat(), now_iso(), n_new))
        if touched:
            rescore(registry, sorted(set(touched)))
        ranked = 0
        if st["model_rank"] and st["model_top"] and config.model_ready():
            try:
                ranked = model_rank(pipeline.llm, registry, st["model_top"])
            except Exception as exc:  # noqa: BLE001
                errors.append(f"模型精排：{str(exc)[:200]}")
        prune(registry)
        _state(registry, "last_run", now_iso())
        try:
            maybe_digest(config, registry)
        except Exception as exc:  # noqa: BLE001 - the digest is a nicety
            log.warning("weekly digest failed: %s", exc)
        registry.log("info" if not errors else "warning",
                     f"Literature tracking: {found_new} new candidate(s)" + (f"; {len(errors)} error(s)" if errors else ""))
        return {"ok": True, "new": found_new, "ranked": ranked, "errors": errors}
    finally:
        _run_lock.release()


# ------------------------------------------------------------------ weekly digest
DIGEST_EVERY_DAYS = 7
DIGEST_SIZE = 5


def last_digest(registry: Registry) -> dict | None:
    raw = _state(registry, "last_digest")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def digest_items(registry: Registry, since: str | None) -> list[dict]:
    """The best new candidates found since the last digest (all-time on the first one)."""
    rows = registry.query(
        "SELECT id FROM track_candidates WHERE status='new' AND created_at > ? ORDER BY score DESC, id DESC LIMIT ?",
        (since or "", DIGEST_SIZE))
    ids = {r["id"] for r in rows}
    return [c for c in candidates(registry, "new", 500) if c["id"] in ids][:DIGEST_SIZE]


def maybe_digest(config, registry: Registry, send=None, force: bool = False) -> dict | None:
    """Once a week: notify the top candidates of the week and keep them for the 「新文献」 page."""
    from . import notify

    ensure_schema(registry)
    if not settings(config)["digest"] and not force:
        return None
    last = last_digest(registry)
    if last and not force:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(last["at"])
        if age < timedelta(days=DIGEST_EVERY_DAYS):
            return None
    items = digest_items(registry, last["at"] if last else None)
    if not items:
        return None
    info = {"at": now_iso(), "ids": [c["id"] for c in items]}
    _state(registry, "last_digest", json.dumps(info))
    top = items[0]
    body = (f"{len(items)} 篇值得一读，最相关：{top['title'][:90]}" if config.output_language != "en"
            else f"{len(items)} papers worth reading; top: {top['title'][:90]}")
    (send or notify.send)("PaperMap · 本周新文献" if config.output_language != "en" else "PaperMap · New this week", body)
    registry.log("info", f"Weekly new-literature digest: {len(items)} paper(s)")
    return info


def digest_for_page(registry: Registry) -> dict | None:
    last = last_digest(registry)
    if not last:
        return None
    by_id = {c["id"]: c for c in candidates(registry, "all", 2000)} if last["ids"] else {}
    items = [by_id[i] for i in last["ids"] if i in by_id]
    return {"at": last["at"], "items": items} if items else None


def due(registry: Registry) -> bool:
    ensure_schema(registry)
    last = _state(registry, "last_run")
    if not last:
        return True
    return datetime.now(timezone.utc) - datetime.fromisoformat(last) > timedelta(hours=RUN_EVERY_H)


def run_scheduler(pipeline, stop: threading.Event | None = None, interval_s: float = 1800) -> None:
    """Check once at startup when due, then every half hour (so a day off is caught up on next start)."""
    stop = stop or threading.Event()
    stop.wait(60)  # let startup work (migration, indexing) go first
    while not stop.is_set():
        try:
            if settings(pipeline.config)["enabled"] and due(pipeline.registry):
                run(pipeline)
        except Exception as exc:  # noqa: BLE001
            log.exception("literature tracking failed")
            pipeline.registry.log("error", f"Literature tracking failed: {exc}")
        try:
            from . import clips

            clips.maybe_auto_digest(pipeline)
        except Exception as exc:  # noqa: BLE001
            log.warning("weekly clippings digest failed: %s", exc)
        stop.wait(interval_s)


def prune(registry: Registry, days: int = 90) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    registry.execute("DELETE FROM track_candidates WHERE status IN ('new','dismissed') AND score < 4 AND updated_at < ?",
                     (cutoff,))


def sync_library(registry: Registry) -> None:
    """Candidates that are now in the library (taken in, dragged in, imported elsewhere) become 'imported'."""
    ensure_schema(registry)
    rows = registry.query(
        "SELECT c.id, p.id AS pid FROM track_candidates c JOIN papers p ON p.status!='ignored' AND "
        "((c.doi IS NOT NULL AND lower(p.doi)=c.doi) OR (c.openalex_id IS NOT NULL AND p.openalex_id=c.openalex_id)) "
        "WHERE c.status!='imported'")
    rows += registry.query(
        "SELECT c.id, d.paper_id AS pid FROM track_candidates c JOIN paper_dois d ON lower(d.doi)=c.doi "
        "WHERE c.status!='imported'")
    for r in rows:
        registry.execute("UPDATE track_candidates SET status='imported', paper_id=?, decided_at=COALESCE(decided_at, ?) "
                         "WHERE id=?", (r["pid"], now_iso(), r["id"]))


# ------------------------------------------------------------------ reading the list
def candidates(registry: Registry, status: str = "new", limit: int = 200) -> list[dict]:
    ensure_schema(registry)
    sync_library(registry)
    if status == "all":
        rows = registry.query("SELECT * FROM track_candidates ORDER BY score DESC, pub_date DESC, id DESC LIMIT ?", (limit,))
    else:
        rows = registry.query("SELECT * FROM track_candidates WHERE status=? ORDER BY score DESC, pub_date DESC, id DESC "
                              "LIMIT ?", (status, limit))
    watch = {r["id"]: r for r in registry.query("SELECT * FROM track_watches")}
    lib_by_oa = {r["openalex_id"]: r for r in registry.query(
        "SELECT id, openalex_id, first_author, year FROM papers WHERE openalex_id IS NOT NULL")}
    gaps = {r["id"]: r for r in registry.query("SELECT g.id, g.text, q.title AS question, q.id AS qid FROM question_gaps g "
                                               "JOIN evidence_questions q ON q.id=g.question_id")}
    out = []
    for c in rows:
        d = dict(c)
        d["authors"] = json.loads(d.pop("authors_json"))
        d["oa_urls"] = json.loads(d.pop("oa_urls_json"))
        reasons = []
        refs = [lib_by_oa[x] for x in json.loads(d.pop("lib_refs_json")) if x in lib_by_oa]
        if refs:
            reasons.append({"kind": "cites", "text": L("引用了你库里的"), "papers": refs[:6], "n": len(refs)})
        hits = registry.query("SELECT watch_id, source FROM track_hits WHERE candidate_id=? ORDER BY found_at", (c["id"],))
        by_watch: dict[int, list[str]] = {}
        for h in hits:
            by_watch.setdefault(h["watch_id"], []).append(SOURCE_NAMES.get(h["source"], h["source"]))
        gap_ids = json.loads(d.pop("gaps_json"))
        for wid, srcs in by_watch.items():
            w = watch.get(wid)
            if w is None or w["kind"] == "cites" or (w["kind"] == "gap" and w["ref"].isdigit() and int(w["ref"]) in gap_ids):
                continue
            reasons.append({"kind": w["kind"], "text": KINDS[w["kind"]], "label": w["label"], "sources": srcs})
        for gid in gap_ids:
            if gid in gaps:
                reasons.append({"kind": "gapmatch", "text": L("标题摘要碰到缺口"), "label": gaps[gid]["text"],
                                "question": gaps[gid]["question"], "qid": gaps[gid]["qid"]})
        d["reasons"] = reasons
        d["sources"] = sorted({s for v in by_watch.values() for s in v})
        out.append(d)
    return out


def counts(registry: Registry) -> dict:
    ensure_schema(registry)
    return {r["status"]: r["n"] for r in registry.query("SELECT status, count(*) AS n FROM track_candidates GROUP BY status")}


def badge(registry: Registry) -> int:
    ensure_schema(registry)
    r = registry.one("SELECT count(*) AS n FROM track_candidates WHERE status='new' AND score >= 5")
    return int(r["n"]) if r else 0


def source_status(registry: Registry) -> list[dict]:
    ensure_schema(registry)
    out = []
    for name, label in SOURCE_NAMES.items():
        r = registry.one("SELECT min(last_success) AS oldest, max(last_success) AS newest, max(last_run_at) AS ran, "
                         "sum(last_error IS NOT NULL) AS errs, count(*) AS n FROM track_cursors c JOIN track_watches w "
                         "ON w.id=c.watch_id AND w.enabled=1 WHERE source=?", (name,))
        out.append({"name": name, "label": label, "connected": True, **(dict(r) if r else {})})
    return out


def decide(registry: Registry, cid: int, action: str) -> None:
    if action not in ("later", "dismissed", "new", "wanted"):
        raise ValueError("无效的操作")
    if not registry.one("SELECT 1 FROM track_candidates WHERE id=?", (cid,)):
        raise ValueError("这条候选不存在。")
    registry.execute("UPDATE track_candidates SET status=?, decided_at=? WHERE id=?", (action, now_iso(), cid))


# ------------------------------------------------------------------ taking a paper in
def _download(url: str, folder: Path, name: str, timeout: float = 90) -> Path | None:
    from .pdf_import import publish_pdf, staged_file

    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                             "Version/18.0 Safari/605.1.15 PaperMap", "Accept": "application/pdf,*/*;q=0.5"}
    with staged_file(folder) as tmp:
        try:
            with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=timeout) as r:
                if r.status_code != 200:
                    return None
                size = 0
                with tmp.open("wb") as fh:
                    for chunk in r.iter_bytes(1 << 16):
                        size += len(chunk)
                        if size > 300 * 1024 * 1024:
                            return None
                        fh.write(chunk)
        except httpx.HTTPError:
            return None
        with tmp.open("rb") as fh:
            if fh.read(5) != b"%PDF-" or tmp.stat().st_size < 20_000:
                return None
        return publish_pdf(tmp, name)


PMC_BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"


def pmc_pdf_urls(pmcid: str) -> list[str]:
    """PDFs of PMC's open-access subset, from PMC's official cloud distribution (AWS Open Data).
    The pmc.ncbi.nlm.nih.gov web pages challenge scripts; this bucket is the route PMC offers for programs."""
    if not re.fullmatch(r"PMC\d+", pmcid or ""):
        return []
    try:
        r = httpx.get(PMC_BUCKET + "/", params={"list-type": "2", "prefix": pmcid + "."}, timeout=30)
    except httpx.HTTPError:
        return []
    keys = re.findall(r"<Key>([^<]+\.pdf)</Key>", r.text) if r.status_code == 200 else []
    keys = [k for k in keys if re.fullmatch(rf"{pmcid}\.\d+/{pmcid}\.\d+\.pdf", k)]
    keys.sort(key=lambda k: -int(k.split("/")[0].rsplit(".", 1)[1]))  # newest version first
    return [f"{PMC_BUCKET}/{k}" for k in keys]


def expand_urls(urls: list[str]) -> list[str]:
    """PMC links become direct downloads from PMC's open-access bucket (tried first); others stay."""
    out: list[str] = []
    for u in urls:
        m = re.match(r"pmc:(PMC\d+)$", u) or re.search(r"(?:ncbi\.nlm\.nih\.gov|europepmc\.org).*?(PMC\d+)", u)
        if m:
            out += [x for x in pmc_pdf_urls(m.group(1)) if x not in out]
            if not u.startswith("pmc:"):
                out.append(u)
        elif u not in out:
            out.append(u)
    return out


def take_in(pipeline, cid: int) -> dict:
    """Fetch a legal open-access PDF for a candidate and hand it to the normal import."""
    registry = pipeline.registry
    c = registry.one("SELECT * FROM track_candidates WHERE id=?", (cid,))
    if c is None:
        raise ValueError("这条候选不存在。")
    sync_library(registry)
    if registry.one("SELECT status FROM track_candidates WHERE id=?", (cid,))["status"] == "imported":
        return {"ok": True, "already": True}
    urls = json.loads(c["oa_urls_json"])
    if not urls and c["doi"]:  # the record may be older than its OA copy: ask OpenAlex again (free single lookup)
        try:
            w = pipeline.metadata._get_json(f"{OpenAlexSource.API}/doi:{c['doi']}", pipeline.metadata._openalex_params(),  # noqa: SLF001
                                            cache=False)
            urls = OpenAlexSource.parse(w)["oa_urls"] if w else []
        except Exception:  # noqa: BLE001
            urls = []
    name = (c["doi"] or c["title_key"][:60] or f"candidate-{cid}").replace("/", "_") + ".pdf"
    for u in expand_urls(urls):
        path = _download(u, pipeline.config.imported_dir, name)
        if path is not None:
            registry.execute("UPDATE track_candidates SET status='wanted', decided_at=? WHERE id=?", (now_iso(), cid))
            threading.Thread(target=_import_then_sync, args=(pipeline, path), daemon=True).start()
            registry.log("info", f"Tracking: downloaded open-access PDF for {c['doi'] or c['title'][:60]}")
            return {"ok": True, "downloaded": True, "url": u}
    registry.execute("UPDATE track_candidates SET status='wanted', decided_at=? WHERE id=?", (now_iso(), cid))
    return {"ok": True, "downloaded": False, "landing": c["landing"] or (f"https://doi.org/{c['doi']}" if c["doi"] else "")}


def _import_then_sync(pipeline, path: Path) -> None:
    try:
        pipeline.handle_path(path)
    finally:
        sync_library(pipeline.registry)
