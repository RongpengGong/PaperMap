"""What squad members may do: read the library, search outside it, and propose changes.

Read tools return text the model sees. Propose tools never change the library: they validate the
proposal (a quotation must be found verbatim in the PDF, ids must exist) and file it for the user,
who accepts or rejects it on the run page (CLAUDE.md §16.1: the model proposes, the user confirms).
Citation markers members may use: [P12] library paper, [E5] evidence excerpt, [C7] tracking
candidate, [W3] an outside work found during this run.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pymupdf

from .. import evidence, search
from ..registry import Registry
from . import store

OBS_LIMIT = 12000
PAGES_LIMIT = 24000


class ToolError(Exception):
    """A tool refused the call; the message goes back to the model so it can correct itself."""


@dataclass
class Ctx:
    pipeline: Any
    run_id: int
    task_key: str = ""
    member: str = ""

    @property
    def registry(self) -> Registry:
        return self.pipeline.registry

    @property
    def config(self):
        return self.pipeline.config


@dataclass
class Tool:
    name: str
    kind: str          # read / propose
    doc: str
    fn: Callable[[Ctx, dict], str]


TOOLS: dict[str, Tool] = {}


def tool(name: str, kind: str, doc: str):
    def deco(fn):
        TOOLS[name] = Tool(name, kind, doc, fn)
        return fn
    return deco


# ------------------------------------------------------------------ argument helpers
def _int(args: dict, key: str, default: int | None = None, lo: int = 0, hi: int = 10**9) -> int:
    v = args.get(key, default)
    if isinstance(v, str) and re.fullmatch(r"[A-Za-z]?\d+", v.strip()):
        v = int(re.sub(r"\D", "", v))
    if not isinstance(v, int) or isinstance(v, bool):
        raise ToolError(f"'{key}' must be an integer")
    return max(lo, min(hi, v))


def _str(args: dict, key: str, required: bool = True, limit: int = 4000) -> str:
    v = args.get(key, "")
    if not isinstance(v, str):
        raise ToolError(f"'{key}' must be a string")
    v = v.strip()
    if required and not v:
        raise ToolError(f"'{key}' is required")
    return v[:limit]


def _who(row) -> str:
    return f"{row['first_author'] or '?'} {row['year'] or 'n.d.'}"


def _paper(ctx: Ctx, args: dict, key: str = "paper_id"):
    pid = _int(args, key)
    row = ctx.registry.get_paper(pid)
    if row is None or row["status"] == "ignored":
        raise ToolError(f"no library paper P{pid}")
    return row


def _question(ctx: Ctx, args: dict):
    qid = _int(args, "question_id")
    row = ctx.registry.one("SELECT * FROM evidence_questions WHERE id=? AND status='active'", (qid,))
    if row is None:
        raise ToolError(f"no active research question Q{qid}")
    return row


def _pdf(ctx: Ctx, pid: int) -> Path:
    pdf = ctx.pipeline.pdf_for(pid)
    if pdf is None or not Path(pdf).exists():
        raise ToolError(f"P{pid} has no PDF on this computer; use read_card instead")
    return Path(pdf)


def _cap(text: str, limit: int = OBS_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n…[truncated, {len(text) - limit} more characters]"


# ------------------------------------------------------------------ read tools
@tool("library_overview", "read",
      "library_overview() — counts, fields with paper counts, active research questions (Q ids) with evidence "
      "counts, and open evidence gaps.")
def library_overview(ctx: Ctx, args: dict) -> str:
    reg = ctx.registry
    n = reg.one("SELECT count(*) AS n FROM papers WHERE status='done'")["n"]
    lines = [f"Library: {n} papers with cards.", "", "FIELDS (slug — name — papers):"]
    for r in reg.query("SELECT f.slug, f.name_en, f.name_zh, f.axis, count(pf.paper_id) AS n FROM fields f "
                       "LEFT JOIN paper_fields pf ON pf.field_id=f.id AND pf.status='accepted' "
                       "WHERE f.status='active' GROUP BY f.id HAVING n>0 ORDER BY n DESC LIMIT 40"):
        lines.append(f"- {r['slug']} — {r['name_en'] or r['name_zh']} ({r['axis']}) — {r['n']}")
    lines += ["", "RESEARCH QUESTIONS:"]
    for q in evidence.questions(reg):
        lines.append(f"- Q{q['id']}: {q['title']} — {q['total']} excerpts")
    if reg.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='question_gaps'"):
        gaps = reg.query("SELECT g.id, g.text, g.question_id FROM question_gaps g JOIN evidence_questions q "
                         "ON q.id=g.question_id WHERE g.status='open' AND q.status='active' LIMIT 40")
        if gaps:
            lines += ["", "OPEN GAPS:"] + [f"- gap {g['id']} (Q{g['question_id']}): {g['text']}" for g in gaps]
    return "\n".join(lines)


@tool("list_papers", "read",
      "list_papers(field?: slug, limit?: int ≤ 80) — library papers (P ids) with year, journal and one-line "
      "conclusion; optionally only one field.")
def list_papers(ctx: Ctx, args: dict) -> str:
    limit = _int(args, "limit", 40, 1, 80)
    field = _str(args, "field", required=False, limit=100)
    sql = "SELECT p.* FROM papers p WHERE p.status='done'"
    params: list = []
    if field:
        sql += (" AND p.id IN (SELECT pf.paper_id FROM paper_fields pf JOIN fields f ON f.id=pf.field_id "
                "WHERE f.slug=? AND pf.status='accepted')")
        params.append(field)
    rows = ctx.registry.query(sql + " ORDER BY p.year DESC, p.id DESC LIMIT ?", (*params, limit))
    if not rows:
        return "No papers." + (" Check the field slug with library_overview." if field else "")
    out = []
    for r in rows:
        one = ""
        try:
            one = (json.loads(r["card_json"] or "{}").get("one_sentence") or "")[:200]
        except ValueError:
            pass
        out.append(f"[P{r['id']}] {_who(r)} — {r['title']} — {r['journal'] or ''}" + (f"\n    {one}" if one else ""))
    return "\n".join(out)


@tool("search_library", "read",
      "search_library(query: str, limit?: int ≤ 15) — full-text search over PDF pages, cards, notes and "
      "translations. Space-separated terms are ANDed; put phrases in double quotes; set any=true to OR them. "
      "Returns papers (P ids) with page-numbered snippets.")
def search_library(ctx: Ctx, args: dict) -> str:
    q = _str(args, "query", limit=300)
    limit = _int(args, "limit", 10, 1, 15)
    hits = search.search(ctx.registry, q, limit=300, any_term=bool(args.get("any")), visible_only=True)
    groups = search.grouped(ctx.registry, hits)[:limit]
    if not groups:
        return "No hits. Try fewer or broader terms, English terms, or any=true."
    out = []
    for g in groups:
        p = g["paper"]
        out.append(f"[P{p['id']}] {_who(p)} — {p['title']} ({len(g['hits'])} hits)")
        for h in g["hits"][:3]:
            where = f"p.{h['page'] + 1}" if h["page"] is not None else h["kind"]
            out.append(f"    {where}: {h['snippet'][:220]}")
    return "\n".join(out)


@tool("read_card", "read", "read_card(paper_id: int) — the paper's structured card (design, methods, findings, "
      "limitations) as Markdown.")
def read_card(ctx: Ctx, args: dict) -> str:
    row = _paper(ctx, args)
    text = ""
    if row["kb_path"]:
        path = ctx.config.knowledge_dir / row["kb_path"]
        if path.exists():
            text = re.sub(r"^---\n.*?\n---\n", "", path.read_text(encoding="utf-8"), count=1, flags=re.S)
    if not text:
        text = json.dumps(json.loads(row["card_json"] or "{}"), ensure_ascii=False, indent=1)
    head = f"[P{row['id']}] {_who(row)} — {row['title']} — {row['journal'] or ''} — DOI {row['doi'] or 'none'}\n\n"
    return _cap(head + text, 9000)


@tool("read_pages", "read",
      "read_pages(paper_id: int, start?: int, end?: int) — the PDF's text with [p.N] page markers (1-based). "
      "Read the pages you need rather than the whole paper; at most about 24 000 characters per call.")
def read_pages(ctx: Ctx, args: dict) -> str:
    row = _paper(ctx, args)
    pdf = _pdf(ctx, row["id"])
    parts, used = [], 0
    with pymupdf.open(pdf) as doc:
        n = len(doc)
        start = _int(args, "start", 1, 1, n)
        end = _int(args, "end", n, start, n)
        for i in range(start - 1, end):
            block = f"[p.{i + 1}]\n{doc[i].get_text().strip()}"
            if used + len(block) > PAGES_LIMIT:
                parts.append(block[: max(0, PAGES_LIMIT - used)] + f"\n…[stopped here; continue with start={i + 1}]")
                break
            parts.append(block)
            used += len(block)
    return f"[P{row['id']}] {row['title']} — {n} pages\n\n" + "\n\n".join(parts)


@tool("find_in_paper", "read", "find_in_paper(paper_id: int, query: str) — page-numbered snippets of one paper "
      "that contain the terms (use before read_pages to find the right pages).")
def find_in_paper(ctx: Ctx, args: dict) -> str:
    row = _paper(ctx, args)
    q = _str(args, "query", limit=300)
    hits = search.search(ctx.registry, q, kinds={"page"}, paper_ids=[row["id"]], limit=12, any_term=True)
    if not hits:
        return "No page contains these terms."
    return "\n".join(f"p.{h['page'] + 1}: {h['snippet'][:300]}" for h in hits)


@tool("question_evidence", "read",
      "question_evidence(question_id: int) — the researcher's current judgment, open gaps, and every excerpt "
      "already collected (E ids, stance, page, quote, paper), plus dataset overlaps between papers.")
def question_evidence(ctx: Ctx, args: dict) -> str:
    q = _question(ctx, args)
    comp = evidence.question_comparison(ctx.registry, q["id"])
    lines = [f"Q{q['id']}: {q['title']}", f"CURRENT JUDGMENT: {comp['judgment'] or '(none yet)'}", ""]
    for g in comp["gaps"]:
        lines.append(f"GAP {g['id']} [{g['status']}]: {g['text']}")
    lines.append("")
    for p in comp["papers"]:
        lines.append(f"[P{p['paper_id']}] {p['first_author'] or '?'} {p['year'] or ''} — {p['title']}")
        for e in p["evidence"]:
            body = e["quote"] or f"(figure/table: {e['region_label']})"
            lines.append(f"  [E{e['id']}] {e['stance']} · {e['dimension']} · p.{e['page']}: \"{body[:500]}\""
                         + (f" — note: {e['note'][:200]}" if e["note"] else ""))
    if not comp["papers"]:
        lines.append("(no excerpts yet)")
    try:
        from .. import evidence_links

        groups = evidence_links.overlap_groups(ctx.registry, q["id"])
    except Exception:  # noqa: BLE001 - optional context
        groups = []
    if groups:
        lines += ["", "SHARED DATASET ACCESSIONS (possible sample overlap):"]
        for grp in groups[:20]:
            lines.append("- " + json.dumps(grp, ensure_ascii=False, default=str)[:400])
    return _cap("\n".join(lines))


@tool("tracking_candidates", "read",
      "tracking_candidates(limit?: int ≤ 40, offset?: int) — newly found papers not yet in the library "
      "(C ids), best free score first, with abstract and why they were found.")
def tracking_candidates(ctx: Ctx, args: dict) -> str:
    from .. import tracking

    limit = _int(args, "limit", 15, 1, 40)
    offset = _int(args, "offset", 0, 0, 1000)
    rows = tracking.candidates(ctx.registry, "new", limit=offset + limit)[offset:]
    if not rows:
        return "No new candidates."
    out = []
    for c in rows:
        reasons = "; ".join(r.get("text", "") + (f" {r['label']}" if r.get("label") else "") for r in c["reasons"])
        out.append(f"[C{c['id']}] {c['title']} — {c['journal']} {c['year'] or ''}"
                   f"{' (preprint)' if c['is_preprint'] else ''} — score {c['score']:.1f}"
                   + (f" — found because: {reasons}" if reasons else "")
                   + f"\n    {(c['abstract'] or '(no abstract)')[:900]}")
    return _cap("\n".join(out))


@tool("external_search", "read",
      "external_search(query: str, limit?: int ≤ 15, since_year?: int) — OpenAlex search over titles and "
      "abstracts of published work outside the library. Returns W ids with abstracts; whether each is already "
      "in the library is shown.")
def external_search(ctx: Ctx, args: dict) -> str:
    from .. import tracking

    q = _str(args, "query", limit=200).replace(",", " ")
    limit = _int(args, "limit", 10, 1, 15)
    flt = f"title_and_abstract.search:{q},type:article|preprint|review"
    if args.get("since_year"):
        flt += f",from_publication_date:{_int(args, 'since_year', 2000, 1900, 2100)}-01-01"
    try:
        recs = tracking.OpenAlexSource(ctx.pipeline.metadata)._get(flt, True, limit)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"OpenAlex search failed: {exc}") from exc
    if not recs:
        return "No results."
    lib_dois = {r["doi"] for r in ctx.registry.query("SELECT doi FROM papers WHERE doi IS NOT NULL AND status!='ignored'")}
    out = []
    for rec in recs:
        inlib = rec.get("doi") and rec["doi"] in lib_dois
        wid = store.add_work(ctx.registry, ctx.run_id, rec)
        first = (rec.get("authors") or [""])[0]
        first = first.get("name", "") if isinstance(first, dict) else str(first)
        out.append(f"[W{wid}] {first} {rec.get('year') or ''} — {rec['title']} — {rec.get('journal') or ''}"
                   + (" — ALREADY IN LIBRARY" if inlib else "")
                   + f"\n    {(rec.get('abstract') or '(no abstract)')[:700]}")
    return _cap("\n".join(out))


@tool("missing_key_papers", "read",
      "missing_key_papers(limit?: int ≤ 30) — works cited by several library papers that are not in the "
      "library yet (W ids), most-cited first.")
def missing_key_papers(ctx: Ctx, args: dict) -> str:
    from .. import citations

    rows = citations.missing_key_papers(ctx.registry, limit=_int(args, "limit", 15, 1, 30))
    if not rows:
        return "None known (citation data may not have been fetched yet)."
    out = []
    for r in rows:
        rec = {"doi": r["doi"], "openalex_id": r["ref_id"], "title": r["title"] or r["ref_id"], "year": r["year"],
               "journal": r["journal"] or "", "authors": [r["first_author"]] if r["first_author"] else []}
        wid = store.add_work(ctx.registry, ctx.run_id, rec)
        cited = ", ".join(f"P{x}" for x in r["citing"][:8])
        out.append(f"[W{wid}] {r['first_author'] or '?'} {r['year'] or ''} — {rec['title']} — cited by {r['n']} "
                   f"library papers ({cited})")
    return "\n".join(out)


# ------------------------------------------------------------------ propose tools
def _norm(text: str) -> str:
    return re.sub(r"\W+", "", text.lower())


def _propose(ctx: Ctx, kind: str, payload: dict, dedupe: str, summary: str) -> str:
    pid, new = store.add_proposal(ctx.registry, ctx.run_id, ctx.task_key, kind, payload, dedupe)
    store.log(ctx.registry, ctx.run_id, ctx.member, "proposal", summary, ctx.task_key, {"proposal_id": pid, "kind": kind})
    return f"Filed for the researcher's review (proposal {pid})." if new else f"Already proposed (proposal {pid}); updated."


@tool("propose_evidence", "propose",
      "propose_evidence(question_id, paper_id, quote, stance: supports|opposes, dimension: sample_source|"
      "analysis_unit|methods|findings|limitations|general, note, page?) — file one excerpt for a research "
      "question. 'quote' must be copied VERBATIM from the PDF (one to three sentences); it is checked against "
      "the page text and refused otherwise. 'note' says in one sentence why it bears on the question.")
def propose_evidence(ctx: Ctx, args: dict) -> str:
    from .. import updates

    q = _question(ctx, args)
    row = _paper(ctx, args)
    quote = _str(args, "quote", limit=2000)
    stance = args.get("stance")
    if stance not in ("supports", "opposes"):
        raise ToolError("'stance' must be 'supports' or 'opposes'")
    dimension = args.get("dimension") or "general"
    if dimension not in evidence.DIMENSIONS:
        raise ToolError(f"'dimension' must be one of {sorted(evidence.DIMENSIONS)}")
    note = _str(args, "note", required=False, limit=600)
    pdf = _pdf(ctx, row["id"])
    revision = evidence.document_revision(pdf)
    with pymupdf.open(pdf) as doc:
        n = len(doc)
        hint = args.get("page")
        order = [hint - 1] if isinstance(hint, int) and 0 < hint <= n else []
        order += [p for p in range(n) if p not in order]
        for p in order:
            rects = updates.locate(doc[p], quote)
            if rects:
                break
        else:
            raise ToolError("the quote was not found verbatim in the PDF. Copy the sentence exactly as printed "
                            "(use read_pages), without paraphrasing or joining distant sentences.")
    payload = {"question_id": q["id"], "question": q["title"], "paper_id": row["id"], "paper": row["title"],
               "who": _who(row), "page": p, "revision": revision, "rects": rects, "quote": quote,
               "stance": stance, "dimension": dimension, "note": note}
    return _propose(ctx, "evidence", payload, f"ev:{q['id']}:{row['id']}:{_norm(quote)[:160]}",
                    f"Q{q['id']} ← P{row['id']} p.{p + 1} ({stance}): {quote[:160]}")


@tool("propose_judgment", "propose",
      "propose_judgment(question_id, text) — a draft of the researcher's current judgment on a question "
      "(2–6 sentences). Every sentence must cite [E#] excerpts or [P#] papers; uncited sentences are removed.")
def propose_judgment(ctx: Ctx, args: dict) -> str:
    from . import cite

    q = _question(ctx, args)
    text, dropped = cite.check(ctx.registry, ctx.run_id, _str(args, "text", limit=3000))
    if not cite.markers(text):
        raise ToolError("no sentence carries a valid [E#] or [P#] citation")
    payload = {"question_id": q["id"], "question": q["title"], "text": text, "readable": cite.readable(ctx.registry, ctx.run_id, text),
               "previous": q["judgment"] or "", "dropped": dropped}
    return _propose(ctx, "judgment", payload, f"judg:{q['id']}", f"Q{q['id']} judgment draft") + (
        f" {dropped} uncited sentence(s) were removed." if dropped else "")


@tool("propose_gap", "propose",
      "propose_gap(question_id, text, terms?: [str]) — an evidence gap for a question: what is still missing "
      "(≤ 300 characters), with 2–5 English search terms.")
def propose_gap(ctx: Ctx, args: dict) -> str:
    q = _question(ctx, args)
    text = _str(args, "text", limit=300)
    terms = [t for t in (args.get("terms") or []) if isinstance(t, str) and t.strip()][:6]
    payload = {"question_id": q["id"], "question": q["title"], "text": text, "terms": terms}
    return _propose(ctx, "gap", payload, f"gap:{q['id']}:{_norm(text)[:120]}", f"Q{q['id']} gap: {text[:160]}")


@tool("propose_question", "propose",
      "propose_question(title, rationale) — a new research question worth tracking (one sentence), with why "
      "the library raises it (cite [P#]).")
def propose_question(ctx: Ctx, args: dict) -> str:
    title = _str(args, "title", limit=300)
    if ctx.registry.one("SELECT 1 FROM evidence_questions WHERE title=? AND status='active'", (title,)):
        raise ToolError("this question already exists")
    payload = {"title": title, "rationale": _str(args, "rationale", required=False, limit=800)}
    return _propose(ctx, "question", payload, f"q:{_norm(title)[:160]}", f"new question: {title[:160]}")


def _candidate(ctx: Ctx, args: dict):
    cid = _int(args, "candidate_id")
    row = ctx.registry.one("SELECT * FROM track_candidates WHERE id=?", (cid,)) \
        if ctx.registry.one("SELECT 1 FROM sqlite_master WHERE name='track_candidates'") else None
    if row is None:
        raise ToolError(f"no tracking candidate C{cid}")
    return row


@tool("propose_take_in", "propose",
      "propose_take_in(candidate_id, reason, read_first?) — suggest adding a tracking candidate to the library. "
      "'reason' names the research question or gap it serves; 'read_first' says which part to read first.")
def propose_take_in(ctx: Ctx, args: dict) -> str:
    c = _candidate(ctx, args)
    payload = {"candidate_id": c["id"], "title": c["title"], "journal": c["journal"], "year": c["year"],
               "doi": c["doi"], "reason": _str(args, "reason", limit=600),
               "read_first": _str(args, "read_first", required=False, limit=400)}
    return _propose(ctx, "take_in", payload, f"cand:{c['id']}", f"take in C{c['id']}: {c['title'][:140]}")


@tool("propose_skip", "propose",
      "propose_skip(candidate_id, reason) — suggest marking a tracking candidate as not interesting.")
def propose_skip(ctx: Ctx, args: dict) -> str:
    c = _candidate(ctx, args)
    payload = {"candidate_id": c["id"], "title": c["title"], "reason": _str(args, "reason", limit=400)}
    return _propose(ctx, "skip", payload, f"cand:{c['id']}", f"skip C{c['id']}: {c['title'][:140]}")


@tool("propose_collect", "propose",
      "propose_collect(work_id, reason) — suggest adding an outside work (W id from external_search or "
      "missing_key_papers) to the library.")
def propose_collect(ctx: Ctx, args: dict) -> str:
    wid = _int(args, "work_id")
    rec = store.get_work(ctx.registry, ctx.run_id, wid)
    if rec is None:
        raise ToolError(f"no outside work W{wid} in this run")
    if rec.get("doi") and ctx.registry.paper_by_doi(rec["doi"]):
        raise ToolError("this work is already in the library")
    payload = {"work_id": wid, "title": rec.get("title", ""), "year": rec.get("year"), "doi": rec.get("doi"),
               "journal": rec.get("journal", ""), "reason": _str(args, "reason", limit=600)}
    return _propose(ctx, "collect", payload, f"work:{(rec.get('doi') or rec.get('title', '')).lower()[:160]}",
                    f"collect W{wid}: {payload['title'][:140]}")


def catalog(names: list[str]) -> str:
    return "\n".join(f"- {TOOLS[n].doc}" for n in names if n in TOOLS)


def call(ctx: Ctx, name: str, args: Any, allowed: list[str]) -> str:
    if name not in allowed or name not in TOOLS:
        return f"ERROR: tool '{name}' is not available to you. Available: {', '.join(allowed)}"
    if not isinstance(args, dict):
        return "ERROR: 'args' must be an object"
    try:
        return TOOLS[name].fn(ctx, args)
    except ToolError as exc:
        return f"ERROR: {exc}"
