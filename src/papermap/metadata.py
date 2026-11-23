"""Crossref / OpenAlex lookups with retry, rate limiting and a SQLite cache."""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any
from urllib.parse import quote

import httpx

from . import __version__
from .doi import normalize
from .registry import Registry

log = logging.getLogger(__name__)

CROSSREF = "https://api.crossref.org"
OPENALEX = "https://api.openalex.org"
OK_TTL = 90 * 86400
MISS_TTL = 86400


class MetadataUnavailable(Exception):
    """Transient failure (network, 5xx, rate limit): retry later."""


class MetadataClient:
    def __init__(
        self,
        registry: Registry,
        contact_email: str = "",
        openalex_api_key: str = "",
        transport: httpx.BaseTransport | None = None,
        retries: int = 3,
        min_interval_s: float = 0.2,
    ):
        self.registry = registry
        self.email = contact_email.strip()
        self.openalex_key = openalex_api_key.strip()
        ua = f"PaperMap/{__version__}"
        if self.email:
            ua += f" (mailto:{self.email})"
        self.http = httpx.Client(
            headers={"User-Agent": ua},
            timeout=httpx.Timeout(20.0, connect=10.0),
            transport=transport,
            follow_redirects=True,
        )
        self.retries = retries
        self.min_interval_s = min_interval_s
        self._last_call: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- HTTP ----------------------------------------------------------------
    def _get_json(self, url: str, params: dict | None = None, cache: bool = True) -> Any | None:
        full = str(httpx.URL(url, params=params or {}))
        cache_key = re.sub(r"([?&])(api_key|mailto)=[^&]*&?", r"\1", full).rstrip("?&")
        if cache:
            hit = self.registry.cache_get(cache_key, OK_TTL)
            if hit is not None:
                status, body = hit
                if status == 200:
                    return body
                if status == 404 and self.registry.cache_get(cache_key, MISS_TTL):
                    return None

        host = httpx.URL(url).host
        delay = 1.0
        for attempt in range(self.retries + 1):
            self._throttle(host)
            try:
                resp = self.http.get(url, params=params)
            except httpx.HTTPError as exc:
                if attempt >= self.retries:
                    raise MetadataUnavailable(f"{host}: {exc}") from exc
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code == 404:
                if cache:
                    self.registry.cache_put(cache_key, 404, None)
                return None
            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt >= self.retries:
                    raise MetadataUnavailable(f"{host}: HTTP {resp.status_code}")
                retry_after = resp.headers.get("Retry-After", "")
                time.sleep(float(retry_after) if retry_after.isdigit() else delay)
                delay *= 2
                continue
            if resp.status_code >= 400:
                # e.g. 400 for a malformed DOI: treat as "not found", do not retry
                log.warning("%s returned HTTP %s", full, resp.status_code)
                return None
            body = resp.json()
            if cache:
                self.registry.cache_put(cache_key, 200, body)
            return body
        return None

    def _throttle(self, host: str) -> None:
        with self._lock:
            wait = self._last_call.get(host, 0) + self.min_interval_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call[host] = time.monotonic()

    def _crossref_params(self) -> dict:
        return {"mailto": self.email} if self.email else {}

    def _openalex_params(self) -> dict:
        p: dict[str, str] = {}
        if self.openalex_key:
            p["api_key"] = self.openalex_key
        elif self.email:
            p["mailto"] = self.email
        return p

    # -- Crossref ------------------------------------------------------------
    def crossref_work(self, doi: str) -> dict | None:
        body = self._get_json(f"{CROSSREF}/works/{quote(doi, safe='/')}", self._crossref_params())
        if not body or "message" not in body:
            return None
        return parse_crossref(body["message"])

    def crossref_search(self, title: str, rows: int = 5) -> list[dict]:
        params = {"query.bibliographic": title[:300], "rows": rows, **self._crossref_params()}
        body = self._get_json(f"{CROSSREF}/works", params)
        items = (body or {}).get("message", {}).get("items", [])
        return [parse_crossref(it) for it in items]

    # -- OpenAlex ------------------------------------------------------------
    def openalex_work(self, doi: str) -> dict | None:
        body = self._get_json(
            f"{OPENALEX}/works/doi:{quote(doi, safe='/')}", self._openalex_params()
        )
        return parse_openalex(body) if body else None

    def lookup(self, doi: str) -> dict | None:
        """Merged metadata: Crossref first (authoritative), OpenAlex to enrich."""
        cr = self.crossref_work(doi)
        oa = None
        try:
            oa = self.openalex_work(doi)
        except MetadataUnavailable as exc:
            if cr is None:
                raise
            log.info("OpenAlex unavailable, using Crossref only: %s", exc)
        if cr is None and oa is None:
            return None
        return merge(cr, oa)


def _first(x: Any) -> str:
    if isinstance(x, list):
        return x[0] if x else ""
    return x or ""


def parse_crossref(msg: dict) -> dict:
    authors = []
    for a in msg.get("author", []) or []:
        name = " ".join(p for p in (a.get("given"), a.get("family")) if p) or a.get("name", "")
        authors.append({"given": a.get("given", ""), "family": a.get("family", "") or a.get("name", ""), "name": name})
    year = None
    for key in ("published-print", "published-online", "issued", "published", "posted", "created"):
        parts = (msg.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            year = int(parts[0][0])
            break
    relations: dict[str, list[str]] = {}
    for rel, items in (msg.get("relation") or {}).items():
        dois = [normalize(i.get("id")) for i in items if i.get("id-type") == "doi"]
        relations[rel] = [d for d in dois if d]
    abstract = re.sub(r"<[^>]+>", " ", msg.get("abstract", "") or "")
    return {
        "source": "crossref",
        "doi": normalize(msg.get("DOI")),
        "title": re.sub(r"\s+", " ", _first(msg.get("title"))).strip(),
        "authors": authors,
        "year": year,
        "journal": _first(msg.get("container-title")),
        "journal_abbrev": _first(msg.get("short-container-title")),
        "type": msg.get("type", ""),
        "subtype": msg.get("subtype", ""),
        "publisher": msg.get("publisher", ""),
        "abstract": re.sub(r"\s+", " ", abstract).strip(),
        "relations": relations,
        "volume": msg.get("volume", ""),
        "issue": msg.get("issue", ""),
        "page": msg.get("page", ""),
    }


def parse_openalex(w: dict) -> dict:
    authors = []
    for a in w.get("authorships", []) or []:
        name = (a.get("author") or {}).get("display_name") or a.get("raw_author_name") or ""
        family = name.split()[-1] if name else ""
        authors.append({"given": "", "family": family, "name": name})
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    topics = [t.get("display_name") for t in (w.get("topics") or []) if t.get("display_name")]
    abstract = ""
    inv = w.get("abstract_inverted_index")
    if inv:
        pos = sorted((i, word) for word, idx in inv.items() for i in idx)
        abstract = " ".join(word for _, word in pos)
    return {
        "source": "openalex",
        "openalex_id": (w.get("id") or "").rsplit("/", 1)[-1] or None,
        "doi": normalize(w.get("doi")),
        "title": (w.get("title") or w.get("display_name") or "").strip(),
        "authors": authors,
        "year": w.get("publication_year"),
        "journal": src.get("display_name", "") or "",
        "type": w.get("type", ""),
        "abstract": abstract,
        "topics": topics,
        "keywords": [k.get("display_name") for k in (w.get("keywords") or []) if k.get("display_name")],
        "relations": {},
    }


def merge(cr: dict | None, oa: dict | None) -> dict:
    if cr is None:
        return dict(oa or {})
    out = dict(cr)
    if oa:
        out["openalex_id"] = oa.get("openalex_id")
        out["topics"] = oa.get("topics", [])
        out["keywords"] = oa.get("keywords", [])
        if not out.get("abstract"):
            out["abstract"] = oa.get("abstract", "")
        if not out.get("authors"):
            out["authors"] = oa.get("authors", [])
        if not out.get("year"):
            out["year"] = oa.get("year")
        if not out.get("journal"):
            out["journal"] = oa.get("journal", "")
    return out
