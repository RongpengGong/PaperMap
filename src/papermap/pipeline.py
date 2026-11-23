"""Processing pipeline: file -> dedupe -> DOI -> metadata -> LLM card."""

from __future__ import annotations

import fnmatch
import json
import logging
import re
import threading
import unicodedata
from pathlib import Path

from rapidfuzz import fuzz

from . import doi as doimod
from .config import Config
from .extract import PROMPT_VERSION, extract_paper
from .kb_writer import write_card
from .llm import BudgetExceeded, LLMClient, LLMError, LLMNotConfigured
from .metadata import MetadataClient, MetadataUnavailable
from .pdf_extract import extract_for_llm, read_info, sha256_file
from . import classify as clf
from .kb_fields import write_fields
from .registry import Registry
from .taxonomy import Taxonomy, TaxonomyError

log = logging.getLogger(__name__)

TEMP_SUFFIXES = (".crdownload", ".part", ".download", ".tmp", ".partial")
PREPRINT_RELATIONS = {"is-preprint-of": "preprint", "has-preprint": "published"}
VERSION_RELATIONS = ("is-version-of", "has-version", "is-identical-to")
MIN_TEXT_CHARS = 500


def is_candidate_file(path: Path, config: Config) -> bool:
    """Exclusion rules: non-PDF, hidden, temp downloads, excluded dirs/patterns."""
    name = path.name
    if not name.lower().endswith(".pdf") or name.lower().endswith(TEMP_SUFFIXES):
        return False
    if name.startswith(".") or name.startswith("~$"):
        return False
    excluded_dirs = {d.lower() for d in config.exclude_dir_names}
    for inbox in config.inbox_dirs:
        try:
            rel_parts = path.resolve().relative_to(inbox).parts[:-1]
        except ValueError:
            continue
        if any(p.startswith(".") or p.lower() in excluded_dirs for p in rel_parts):
            return False
    low = name.lower()
    return not any(fnmatch.fnmatch(low, pat.lower()) for pat in config.exclude_patterns)


def _norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"-\s*\n\s*", "", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def title_similarity(ref_title: str, pdf_title: str, first_page_text: str) -> float:
    """How well a registry title matches the PDF (0-100)."""
    a = _norm_text(ref_title)
    if not a:
        return 0.0
    score = fuzz.token_sort_ratio(a, _norm_text(pdf_title)) if pdf_title else 0.0
    if len(a) >= 25 and first_page_text:
        score = max(score, fuzz.partial_ratio(a, _norm_text(first_page_text[:4000])))
    return float(score)


class Pipeline:
    def __init__(
        self,
        config: Config,
        registry: Registry,
        metadata: MetadataClient | None = None,
        llm: LLMClient | None = None,
    ):
        self.config = config
        self.registry = registry
        self.metadata = metadata or MetadataClient(
            registry,
            contact_email=config.metadata.get("contact_email", ""),
            openalex_api_key=config.metadata.get("openalex_api_key", ""),
        )
        self.llm = llm or LLMClient(config, registry)
        self.threshold = float(config.metadata.get("title_match_threshold", 85))
        self._lock = threading.Lock()  # one file at a time
        self._llm_block_reported = False
        self._taxonomy: Taxonomy | None = None
        self._taxonomy_sig: tuple | None = None

    # ------------------------------------------------------------------ files
    def handle_path(self, path: str | Path) -> str:
        """Process one file. Returns a short outcome label (for logs/tests)."""
        p = Path(path).expanduser().resolve()
        with self._lock:
            try:
                return self._handle_path(p)
            except MetadataUnavailable as exc:
                self.registry.log("warning", f"Metadata service unavailable, will retry later: {p.name} ({exc})")
                return "retry_later"
            except Exception as exc:  # noqa: BLE001 - the daemon must never die
                log.exception("Unexpected error on %s", p)
                self.registry.log("error", f"Unexpected error on {p.name}: {type(exc).__name__}: {exc}")
                return "error"

    def _handle_path(self, p: Path) -> str:
        if not p.is_file() or not is_candidate_file(p, self.config):
            return "excluded"
        st = p.stat()
        known = self.registry.file_by_path_stat(str(p), st.st_size, st.st_mtime)
        if known is not None and known["paper_id"] is not None:
            return self._resume_if_pending(known["paper_id"]) or "known"

        sha = sha256_file(p)
        row = self.registry.file_by_sha(sha)
        if row is not None and row["paper_id"] is not None:
            new_path = row["path"] != str(p) and str(p) not in self.registry.paper_file_paths(row["paper_id"])
            self.registry.upsert_file(sha, str(p), st.st_size, st.st_mtime, row["paper_id"])
            if new_path:
                self.registry.log("info", f"Same file at a new path (no reprocessing): {p}", row["paper_id"])
                self._refresh_card(row["paper_id"])
            return self._resume_if_pending(row["paper_id"]) or "duplicate_file"

        info = read_info(p)
        if info.error:
            pid = self.registry.create_paper(title=p.stem, status="error", status_detail=f"Cannot read PDF: {info.error}")
            self.registry.upsert_file(sha, str(p), st.st_size, st.st_mtime, pid)
            self.registry.log("error", f"Cannot read PDF {p.name}: {info.error}", pid)
            return "error"
        if info.is_supplement:
            pid = self.registry.create_paper(
                title=info.title_guess or p.stem, status="skipped_supplement",
                status_detail=f"Looks like supplementary material ({info.supplement_reason})",
            )
            self.registry.upsert_file(sha, str(p), st.st_size, st.st_mtime, pid)
            self.registry.log("info", f"Skipped supplementary/non-article PDF: {p.name}", pid)
            return "skipped_supplement"

        candidates = doimod.rank_candidates(
            doimod.from_pdf_metadata(info.info, info.xmp),
            doimod.from_pages(info.first_pages),
            doimod.from_filename(p),
        )
        verified, meta, source = self._verify(candidates[:5], info.title_guess, info.first_pages_text)
        if verified is None and info.title_guess:
            verified, meta, source = self._search_by_title(info.title_guess, info.first_pages_text)

        if verified is None:
            if candidates:
                return self._doi_unverified(p, sha, st, info, candidates)
            return self._no_doi(p, sha, st, info)

        assert meta is not None
        existing = self.registry.paper_by_doi(verified) or (
            self.registry.paper_by_openalex(meta["openalex_id"]) if meta.get("openalex_id") else None
        )
        if existing is not None:
            kind = "preprint" if meta.get("type") == "posted-content" else "published"
            self.registry.add_doi(existing["id"], verified, kind)
            self.registry.upsert_file(sha, str(p), st.st_size, st.st_mtime, existing["id"])
            self.registry.log("info", f"Already in library ({verified}); new file recorded: {p.name}", existing["id"])
            self._refresh_card(existing["id"])
            return self._resume_if_pending(existing["id"]) or "duplicate_doi"

        related = self._find_related_work(meta)
        if related is not None:
            return self._attach_version(related, verified, meta, p, sha, st)

        pid = self.registry.create_paper(
            doi=verified, openalex_id=meta.get("openalex_id"), title=meta.get("title") or info.title_guess,
            year=meta.get("year"), journal=meta.get("journal"),
            first_author=_first_author(meta), authors_json=meta.get("authors", []),
            abstract=meta.get("abstract"), meta_json=meta, status="new",
            status_detail=f"DOI via {source}",
        )
        self.registry.add_doi(pid, verified, "preprint" if meta.get("type") == "posted-content" else "published")
        self.registry.upsert_file(sha, str(p), st.st_size, st.st_mtime, pid)
        self.registry.log("info", f"New paper: {meta.get('title', '')[:120]} ({verified}, via {source})", pid)
        return self.process_paper(pid)

    # ------------------------------------------------------------ DOI checks
    def _verify(self, candidates, pdf_title: str, first_text: str):
        transient: MetadataUnavailable | None = None
        for cand in candidates:
            try:
                meta = self.metadata.lookup(cand.doi)
            except MetadataUnavailable as exc:
                transient = exc
                continue
            if not meta:
                continue
            score = title_similarity(meta.get("title", ""), pdf_title, first_text)
            log.debug("candidate %s (%s): title score %.0f", cand.doi, cand.source, score)
            if score >= self.threshold:
                meta["doi"] = meta.get("doi") or cand.doi
                return meta["doi"], meta, cand.source
        if transient is not None:
            # We could not check every candidate; do not conclude "unverified".
            raise transient
        return None, None, None

    def _search_by_title(self, title: str, first_text: str):
        for hit in self.metadata.crossref_search(title):
            if not hit.get("doi"):
                continue
            if fuzz.token_sort_ratio(_norm_text(hit.get("title", "")), _norm_text(title)) >= max(self.threshold, 90):
                meta = self.metadata.lookup(hit["doi"]) or hit
                return hit["doi"], meta, "crossref_title_search"
        return None, None, None

    def _find_related_work(self, meta: dict):
        rels = meta.get("relations") or {}
        for rel in list(PREPRINT_RELATIONS) + list(VERSION_RELATIONS):
            for d in rels.get(rel, []):
                row = self.registry.paper_by_doi(d)
                if row is not None:
                    return row
        return None

    def _attach_version(self, paper, new_doi: str, meta: dict, p: Path, sha: str, st) -> str:
        is_preprint = meta.get("type") == "posted-content"
        kind = "preprint" if is_preprint else "published"
        pid = paper["id"]
        self.registry.add_doi(pid, new_doi, kind)
        self.registry.upsert_file(sha, str(p), st.st_size, st.st_mtime, pid)
        old_meta = json.loads(paper["meta_json"] or "{}")
        if not is_preprint and old_meta.get("type") == "posted-content":
            # Published version supersedes the preprint as the primary record.
            self.registry.update_paper(
                pid, doi=new_doi, title=meta.get("title"), year=meta.get("year"),
                journal=meta.get("journal"), meta_json=meta,
                openalex_id=meta.get("openalex_id") or paper["openalex_id"],
            )
        self.registry.log("info", f"Linked {kind} version {new_doi} to existing paper #{pid}", pid)
        self._refresh_card(pid)
        return self._resume_if_pending(pid) or "same_work"

    def _doi_unverified(self, p: Path, sha: str, st, info, candidates) -> str:
        best = candidates[0]
        pid = self.registry.create_paper(
            doi_candidate=best.doi, title=info.title_guess or p.stem, status="doi_unverified",
            status_detail=f"Candidate DOI {best.doi} ({best.source}) did not match the PDF title",
        )
        self.registry.upsert_file(sha, str(p), st.st_size, st.st_mtime, pid)
        self.registry.log("warning", f"DOI not verified for {p.name}; candidate {best.doi} needs review", pid)
        return "doi_unverified"

    def _no_doi(self, p: Path, sha: str, st, info) -> str:
        title = info.title_guess or p.stem
        if info.title_guess:
            for row in self.registry.papers_without_doi():
                if fuzz.token_sort_ratio(_norm_text(row["title"]), _norm_text(title)) >= 95:
                    self.registry.upsert_file(sha, str(p), st.st_size, st.st_mtime, row["id"])
                    self.registry.log("info", f"No-DOI duplicate of paper #{row['id']} by title: {p.name}", row["id"])
                    self._refresh_card(row["id"])
                    return self._resume_if_pending(row["id"]) or "duplicate_title"
        meta = {"title": title, "authors": [], "year": None, "journal": "", "doi": None}
        pid = self.registry.create_paper(title=title, meta_json=meta, status="new", status_detail="No DOI found")
        self.registry.upsert_file(sha, str(p), st.st_size, st.st_mtime, pid)
        self.registry.log("info", f"New paper without DOI: {title[:120]}", pid)
        return self.process_paper(pid)

    # ------------------------------------------------------------- LLM step
    def _resume_if_pending(self, paper_id: int) -> str | None:
        row = self.registry.get_paper(paper_id)
        if row is not None and row["status"] in ("new", "extracting"):
            return self.process_paper(paper_id)
        return None

    def process_paper(self, paper_id: int, force: bool = False) -> str:
        row = self.registry.get_paper(paper_id)
        if row is None:
            return "missing"
        if row["status"] == "done" and not force:
            return "known"
        meta = json.loads(row["meta_json"] or "{}")
        meta.setdefault("title", row["title"])
        meta["doi"] = row["doi"]
        path = next((x for x in self.registry.paper_file_paths(paper_id) if Path(x).exists()), None)
        if path is None:
            self.registry.update_paper(paper_id, status="error", status_detail="No readable file on disk")
            return "error"
        try:
            self.llm.check_budget()
            self.llm._get_client()
        except (LLMNotConfigured, BudgetExceeded) as exc:
            self.registry.update_paper(paper_id, status="new", status_detail=str(exc))
            if not self._llm_block_reported:
                self.registry.log("warning", str(exc))
                self._llm_block_reported = True
            return "waiting_llm"

        self.registry.update_paper(paper_id, status="extracting", status_detail=None)
        try:
            text = extract_for_llm(path, int(self.config.llm.get("max_input_chars", 60000)))
        except Exception as exc:  # noqa: BLE001
            self.registry.update_paper(paper_id, status="error", status_detail=f"Text extraction failed: {exc}")
            self.registry.log("error", f"Text extraction failed for paper #{paper_id}: {exc}", paper_id)
            return "error"
        if len(text.strip()) < MIN_TEXT_CHARS:
            msg = "PDF has (almost) no text layer - scanned image? OCR is not supported yet"
            self.registry.update_paper(paper_id, status="error", status_detail=msg)
            self.registry.log("error", f"{msg}: paper #{paper_id}", paper_id)
            return "error"

        try:
            result = extract_paper(self.llm, self.config, meta, text, paper_id)
        except (LLMNotConfigured, BudgetExceeded) as exc:
            self.registry.update_paper(paper_id, status="new", status_detail=str(exc))
            self.registry.log("warning", str(exc), paper_id)
            return "waiting_llm"
        except LLMError as exc:
            raw_dir = self.config.logs_dir / "llm_raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / f"paper_{paper_id}.txt").write_text(exc.raw, "utf-8")
            self.registry.update_paper(paper_id, status="error", status_detail=str(exc))
            self.registry.log("error", f"LLM extraction failed for paper #{paper_id}: {exc} (raw output saved in logs/llm_raw)", paper_id)
            return "error"
        except Exception as exc:  # noqa: BLE001 - network/auth/rate-limit from the SDK
            transient = _is_transient_llm_error(exc)
            self.registry.update_paper(
                paper_id, status="new" if transient else "error",
                status_detail=f"LLM call failed: {type(exc).__name__}: {exc}",
            )
            self.registry.log("warning" if transient else "error",
                              f"LLM call failed for paper #{paper_id}: {type(exc).__name__}: {exc}", paper_id)
            return "retry_later" if transient else "error"

        self.registry.update_paper(
            paper_id, card_json=result.data, prompt_version=PROMPT_VERSION, model=result.model,
        )
        self._refresh_card(paper_id)
        self.registry.update_paper(paper_id, status="done", status_detail=None)
        self.registry.log(
            "info",
            f"Card written for paper #{paper_id} ({result.prompt_tokens}+{result.completion_tokens} tokens, ${result.cost_usd:.4f})",
            paper_id,
        )
        self.classify_paper(paper_id)
        return "processed"

    def _refresh_card(self, paper_id: int) -> None:
        row = self.registry.get_paper(paper_id)
        if row is None or not row["card_json"]:
            return
        meta = json.loads(row["meta_json"] or "{}")
        meta["doi"] = row["doi"]
        meta.setdefault("title", row["title"])
        fallback = self._fallback_id(paper_id)
        rel = write_card(
            self.config.knowledge_dir, meta, json.loads(row["card_json"]),
            self.registry.paper_file_paths(paper_id),
            [dict(r) for r in self.registry.paper_dois(paper_id)],
            self.config.output_language, fallback, existing_rel_path=row["kb_path"],
            fields=[dict(r) for r in self.registry.paper_field_rows(paper_id)],
        )
        if rel != row["kb_path"]:
            self.registry.update_paper(paper_id, kb_path=rel)

    def _fallback_id(self, paper_id: int) -> str:
        row = self.registry.one("SELECT sha256 FROM files WHERE paper_id=? ORDER BY id LIMIT 1", (paper_id,))
        return row["sha256"][:10] if row else f"id{paper_id}"

    def process_pending(self) -> dict[str, int]:
        """Retry papers waiting for the LLM (no key yet, budget, network),
        then classify finished papers that were never classified."""
        self._llm_block_reported = False
        counts: dict[str, int] = {}
        for row in list(self.registry.iter_papers_by_status(("new", "extracting"))):
            with self._lock:
                outcome = self.process_paper(row["id"])
            counts[outcome] = counts.get(outcome, 0) + 1
            if outcome in ("waiting_llm", "retry_later"):
                return counts  # no point trying the rest
        for k, v in self.classify_papers(mode="unclassified").items():
            counts[f"classify:{k}"] = v
        return counts

    # ------------------------------------------------------- classification
    def taxonomy(self) -> Taxonomy:
        """Current taxonomy; reloaded when the config or the YAML file changes."""
        from .taxonomy import resolve_path

        spec = str(self.config.raw.get("taxonomy_file") or "default")
        path = resolve_path(spec, self.config.data_dir)
        mtime = path.stat().st_mtime if path is not None and path.exists() else None
        sig = (spec, mtime)
        if self._taxonomy is None or sig != self._taxonomy_sig:
            self._taxonomy = self.config.load_taxonomy()
            self._taxonomy_sig = sig
            self.registry.sync_fields(self._taxonomy)
        return self._taxonomy

    def stale_classification_count(self) -> int:
        try:
            key = clf.classify_key(self.taxonomy())
        except TaxonomyError:
            return 0
        row = self.registry.one(
            "SELECT count(*) AS n FROM papers WHERE status='done' AND classify_key IS NOT NULL AND classify_key != ?",
            (key,),
        )
        return int(row["n"]) if row else 0

    def classify_paper(self, paper_id: int) -> str:
        row = self.registry.get_paper(paper_id)
        if row is None or row["status"] != "done":
            return "skipped"
        try:
            tax = self.taxonomy()
        except TaxonomyError as exc:
            self.registry.log("error", f"Taxonomy invalid, classification skipped: {exc}")
            return "error"
        before = {r["slug"] for r in self.registry.paper_field_rows(paper_id, include_rejected=True)}
        paper, meta, card = clf.paper_inputs(row)
        try:
            result = clf.classify(self.llm, self.config, tax, paper, meta, card, paper_id)
        except (LLMNotConfigured, BudgetExceeded) as exc:
            if not self._llm_block_reported:
                self.registry.log("warning", str(exc))
                self._llm_block_reported = True
            return "waiting_llm"
        except LLMError as exc:
            self.registry.log("error", f"Classification failed for paper #{paper_id}: {exc}", paper_id)
            return "error"
        except Exception as exc:  # noqa: BLE001
            transient = _is_transient_llm_error(exc)
            self.registry.log("warning" if transient else "error",
                              f"Classification call failed for paper #{paper_id}: {type(exc).__name__}: {exc}", paper_id)
            return "retry_later" if transient else "error"
        summary = clf.apply(self.registry, self.config, tax, paper_id, result.data)
        after = {r["slug"] for r in self.registry.paper_field_rows(paper_id, include_rejected=True)}
        self._refresh_card(paper_id)
        self.refresh_fields(before | after)
        msg = f"Classified paper #{paper_id}: {', '.join(summary['accepted']) or '-'}"
        if summary["suggested"]:
            msg += f"; needs review: {', '.join(summary['suggested'])}"
        if result.data.get("proposed_new"):
            msg += f"; proposed new: {', '.join(p['name_en'] or p['name_zh'] for p in result.data['proposed_new'])}"
        self.registry.log("warning" if summary["needs_review"] else "info", msg, paper_id)
        return "classified"

    def classify_papers(self, mode: str = "unclassified", paper_ids: list[int] | None = None) -> dict[str, int]:
        """mode: unclassified | stale (taxonomy/prompt changed) | all."""
        try:
            key = clf.classify_key(self.taxonomy())
        except TaxonomyError as exc:
            self.registry.log("error", f"Taxonomy invalid: {exc}")
            return {"error": 1}
        if paper_ids:
            rows = [r for r in (self.registry.get_paper(i) for i in paper_ids) if r]
        elif mode == "all":
            rows = self.registry.list_papers(status="done", limit=10**6)
        elif mode == "stale":
            rows = self.registry.query(
                "SELECT * FROM papers WHERE status='done' AND (classify_key IS NULL OR classify_key != ?)", (key,))
        else:
            rows = self.registry.query("SELECT * FROM papers WHERE status='done' AND classify_key IS NULL")
        counts: dict[str, int] = {}
        for row in rows:
            with self._lock:
                outcome = self.classify_paper(row["id"])
            counts[outcome] = counts.get(outcome, 0) + 1
            if outcome in ("waiting_llm", "retry_later"):
                break
        return counts

    def refresh_fields(self, slugs: set[str] | None = None) -> None:
        try:
            tax = self.taxonomy()
        except TaxonomyError:
            tax = None
        write_fields(self.config.knowledge_dir, self.registry, tax, self.config.output_language, slugs)

    def set_label(self, paper_id: int, slug: str, action: str) -> None:
        """User decision on a label: accept / reject / add (source='user' sticks)."""
        self.taxonomy()
        field = self.registry.field_by_slug(slug)
        if field is None or self.registry.get_paper(paper_id) is None:
            raise ValueError(f"unknown paper or field: {paper_id}, {slug}")
        status = {"accept": "accepted", "add": "accepted", "reject": "rejected"}[action]
        with self._lock:
            self.registry.set_paper_field(paper_id, field["id"], status=status, source="user",
                                          **({"confidence": 1.0} if action == "add" else {}))
            clf.recompute_review_flag(self.registry, paper_id)
            self._refresh_card(paper_id)
            self.refresh_fields({slug})
        self.registry.log("info", f"User {action}ed label '{slug}' for paper #{paper_id}", paper_id)

    def resolve_unverified(self, paper_id: int, doi: str | None) -> str:
        """Review action for doi_unverified papers: use a DOI the user typed,
        or treat the paper as having no DOI. Then process it normally."""
        row = self.registry.get_paper(paper_id)
        if row is None or row["status"] != "doi_unverified":
            return "skipped"
        with self._lock:
            if doi:
                d = doimod.normalize(doi)
                if not d:
                    raise ValueError("not a valid DOI")
                existing = self.registry.paper_by_doi(d)
                if existing is not None:
                    self.registry.execute("UPDATE files SET paper_id=? WHERE paper_id=?", (existing["id"], paper_id))
                    self.registry.execute("DELETE FROM papers WHERE id=?", (paper_id,))
                    self.registry.log("info", f"User-confirmed DOI {d} is already in the library; files merged", existing["id"])
                    self._refresh_card(existing["id"])
                    return "duplicate_doi"
                meta = self.metadata.lookup(d)
                if not meta:
                    raise ValueError(f"DOI {d} was not found in Crossref/OpenAlex")
                self.registry.update_paper(
                    paper_id, doi=d, doi_candidate=None, openalex_id=meta.get("openalex_id"),
                    title=meta.get("title") or row["title"], year=meta.get("year"), journal=meta.get("journal"),
                    first_author=_first_author(meta), authors_json=meta.get("authors", []),
                    abstract=meta.get("abstract"), meta_json=meta, status="new", status_detail="DOI confirmed by user",
                )
                self.registry.add_doi(paper_id, d, "preprint" if meta.get("type") == "posted-content" else "published")
            else:
                meta = {"title": row["title"], "authors": [], "year": None, "journal": "", "doi": None}
                self.registry.update_paper(paper_id, doi_candidate=None, meta_json=meta, status="new",
                                           status_detail="No DOI (confirmed by user)")
            self.registry.log("info", f"Review: paper #{paper_id} {'DOI ' + doimod.normalize(doi) if doi else 'marked as no-DOI'}", paper_id)
            return self.process_paper(paper_id)


def _first_author(meta: dict) -> str | None:
    authors = meta.get("authors") or []
    return (authors[0].get("family") or authors[0].get("name")) if authors else None


def _is_transient_llm_error(exc: Exception) -> bool:
    from .llm import LLMHTTPError

    if isinstance(exc, LLMHTTPError):
        return exc.status_code in (0, 401, 402, 429) or exc.status_code >= 500
    try:
        import openai
    except ImportError:  # pragma: no cover
        return False
    if isinstance(exc, (openai.APIConnectionError, openai.RateLimitError, openai.APITimeoutError)):
        return True
    if isinstance(exc, openai.APIStatusError):
        # 401 = bad key, 402 = insufficient balance: wait for the user to fix it
        return exc.status_code >= 500 or exc.status_code in (401, 402)
    return False
