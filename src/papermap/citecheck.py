"""Citation check for the user's manuscript: does each cited paper really say what the sentence claims?

A .docx (or a draft from the writing page) is split into sentences; citations are recognised
([3], [1-4], superscript digits, (Smith et al., 2020)) and resolved through the manuscript's own
reference list to papers in the library (DOI first, then title). For every citing sentence the
model sees the passages of each cited paper that best match the sentence, and must answer with a
verdict and a quotation; a quotation that is not found word for word in those passages is not
accepted. Nothing is changed in the manuscript.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pymupdf
from rapidfuzz import fuzz

from . import docx_out
from .doi import normalize as norm_doi
from .llm import LLMClient
from .registry import Registry, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS cite_runs (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    source TEXT NOT NULL,           -- docx / draft
    status TEXT NOT NULL,           -- parsed / running / done / error / stopped
    total INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0,
    refs_json TEXT NOT NULL DEFAULT '[]',
    message TEXT, model TEXT, cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS cite_items (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES cite_runs(id) ON DELETE CASCADE,
    para INTEGER NOT NULL, sent INTEGER NOT NULL,
    sentence TEXT NOT NULL,
    ref_label TEXT NOT NULL, ref_text TEXT NOT NULL DEFAULT '',
    paper_id INTEGER,
    verdict TEXT NOT NULL DEFAULT 'pending',  -- pending / supports / partial / not_found / contradicts / unclear / not_in_library / no_text
    quote TEXT NOT NULL DEFAULT '', page INTEGER, explanation TEXT NOT NULL DEFAULT '',
    decided INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cite_items_run ON cite_items(run_id, para, sent);
"""
VERDICTS = ("supports", "partial", "not_found", "contradicts")
PASSAGES = 4
PASSAGE_CHARS = 900
_threads: dict[int, threading.Event] = {}


def ensure_schema(registry: Registry) -> None:
    with registry._lock:  # noqa: SLF001
        registry.conn.executescript(SCHEMA)


# ------------------------------------------------------------------ parsing the manuscript
REF_HEAD = re.compile(r"^\s*(references?|bibliography|literature( cited)?|works cited|参考文献|引用文献|literaturverzeichnis)\s*:?\s*$", re.I)
NUM_CIT = re.compile(r"\[(\d+(?:\s*[-–,]\s*\d+)*)\]")
AY_CIT = re.compile(r"\(([^()]*?(?:19|20)\d\d[a-z]?[^()]*)\)")
AY_ONE = re.compile(r"([A-Z][\w'’\-]+(?:\s+(?:and|&)\s+[A-Z][\w'’\-]+|\s+et\s+al\.?)?),?\s+((?:19|20)\d\d)[a-z]?")
# English sentences end with . ! ? and a space; Chinese ones with 。！？； and no space at all.
SENT_SPLIT = re.compile(r"(?<=[。！？；])\s*|(?<=[.!?])\s+(?=[A-Z\[(“\"0-9\u4e00-\u9fff])")


def _expand(spec: str) -> list[int]:
    out = []
    for part in re.split(r"\s*,\s*", spec):
        m = re.match(r"(\d+)\s*[-–]\s*(\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if 0 < b - a < 60:
                out += list(range(a, b + 1))
        elif part.strip().isdigit():
            out.append(int(part))
    return out


def parse_docx(data: bytes) -> tuple[list[str], list[str]]:
    """(body paragraphs with superscript citations written as [n], reference entries)."""
    paras = []
    for runs in docx_out.paragraphs(data):
        text = ""
        for r in runs:
            t = r["text"]
            if r["sup"] and re.fullmatch(r"[\d\s,–\-]+", t.strip() or "x"):
                text += f"[{t.strip().strip(',')}]"
            else:
                text += t
        paras.append(text.strip())
    body, refs, in_refs = [], [], False
    for p in paras:
        if not p:
            continue
        if REF_HEAD.match(p):
            in_refs = True
            continue
        (refs if in_refs else body).append(p)
    return body, refs


def ref_number(entry: str, i: int) -> int:
    m = re.match(r"\s*\[?(\d{1,4})[\].)]?\s+", entry)
    return int(m.group(1)) if m else i + 1


def match_reference(registry: Registry, entry: str):
    """The library paper a reference-list entry points to (DOI, then title), or None."""
    m = re.search(r"10\.\d{4,9}/[^\s\"<>]+", entry)
    if m:
        doi = norm_doi(m.group(0).rstrip(".,;)"))
        if doi:
            row = registry.paper_by_doi(doi)
            if row is None:
                alt = registry.one("SELECT paper_id FROM paper_dois WHERE doi=?", (doi,))
                row = registry.get_paper(alt["paper_id"]) if alt else None
            if row is not None and row["status"] != "ignored":
                return row
    low = entry.lower()
    best, score = None, 0.0
    for r in registry.query("SELECT id, title FROM papers WHERE status != 'ignored' AND title IS NOT NULL"):
        t = (r["title"] or "").lower().strip(" .")
        if len(t) < 20:
            continue
        s = fuzz.partial_ratio(t, low)
        if s > score:
            best, score = r, s
    return registry.get_paper(best["id"]) if best is not None and score >= 92 else None


def _ay_key(name: str, year: str) -> str:
    return re.sub(r"[^a-z]", "", name.split()[0].lower()) + year


def citations_in(sentence: str) -> tuple[list[str], str]:
    """Citation labels in a sentence ("12", or "smith2020") and the sentence without them."""
    labels: list[str] = []
    for m in NUM_CIT.finditer(sentence):
        labels += [str(n) for n in _expand(m.group(1))]
    for m in AY_CIT.finditer(sentence):
        for one in AY_ONE.finditer(m.group(1)):
            labels.append(_ay_key(one.group(1), one.group(2)))
    clean = NUM_CIT.sub("", sentence)
    clean = AY_CIT.sub(lambda m: "" if AY_ONE.search(m.group(1)) else m.group(0), clean)
    return list(dict.fromkeys(labels)), re.sub(r"\s+([.,;:])", r"\1", re.sub(r"\s{2,}", " ", clean)).strip()


def create_from_docx(registry: Registry, name: str, data: bytes) -> dict:
    ensure_schema(registry)
    body, refs = parse_docx(data)
    numbered = {ref_number(e, i): e for i, e in enumerate(refs)}
    by_ay: dict[str, str] = {}
    for e in refs:
        m = re.match(r"\s*(?:\[?\d+[\].)]?\s+)?([A-Z][\w'’\-]+).*?\b((?:19|20)\d\d)", e)
        if m:
            by_ay.setdefault(_ay_key(m.group(1), m.group(2)), e)
    cache: dict[str, object] = {}

    def resolve(label: str):
        entry = numbered.get(int(label)) if label.isdigit() else by_ay.get(label)
        if entry is None and not label.isdigit():  # no reference list: first author + year in the library
            name_, year = re.match(r"([a-z]+)(\d{4})", label).groups()
            row = registry.one("SELECT * FROM papers WHERE status != 'ignored' AND year=? AND lower(first_author) LIKE ?",
                               (int(year), f"%{name_}%"))
            return ("", row)
        if entry is None:
            return ("", None)
        if entry not in cache:
            cache[entry] = match_reference(registry, entry)
        return (entry, cache[entry])

    cur = registry.execute("INSERT INTO cite_runs(name, source, status, created_at) VALUES (?,?,?,?)",
                           (name[:200] or "manuscript.docx", "docx", "parsed", now_iso()))
    run_id = int(cur.lastrowid)
    n_items = 0
    for pi, para in enumerate(body):
        for si, sent in enumerate(SENT_SPLIT.split(para)):
            labels, clean = citations_in(sent)
            for label in labels:
                entry, row = resolve(label)
                verdict = "pending" if row is not None else "not_in_library"
                registry.execute(
                    "INSERT INTO cite_items(run_id, para, sent, sentence, ref_label, ref_text, paper_id, verdict) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (run_id, pi, si, clean or sent, label, entry[:600], row["id"] if row is not None else None, verdict))
                n_items += 1
    _update_totals(registry, run_id)
    registry.execute("UPDATE cite_runs SET refs_json=? WHERE id=?",
                     (json.dumps({"entries": len(refs), "matched": sum(1 for v in cache.values() if v is not None)}), run_id))
    return summary(registry, run_id)


def create_from_draft(registry: Registry, draft: dict) -> dict:
    """A draft from the writing page: its [P#] markers already name the papers."""
    ensure_schema(registry)
    by_sid = {s["id"]: s["paper_id"] for s in draft["sources"]}
    cur = registry.execute("INSERT INTO cite_runs(name, source, status, created_at) VALUES (?,?,?,?)",
                           (("写作草稿：" + draft["topic"])[:200], "draft", "parsed", now_iso()))
    run_id = int(cur.lastrowid)
    for pi, para in enumerate(p for p in draft["raw"].split("\n\n") if p.strip()):
        for si, sent in enumerate(SENT_SPLIT.split(para)):
            sids = list(dict.fromkeys(re.findall(r"\[(P\d+)\]", sent)))
            clean = re.sub(r"\s*\[P\d+\]", "", sent).strip()
            for sid in sids:
                pid = by_sid.get(sid)
                row = registry.get_paper(pid) if pid else None
                label = f"{row['first_author'] or '?'} {row['year'] or ''}".strip() if row else sid
                registry.execute(
                    "INSERT INTO cite_items(run_id, para, sent, sentence, ref_label, paper_id, verdict) VALUES (?,?,?,?,?,?,?)",
                    (run_id, pi, si, clean, label, pid if row else None, "pending" if row else "not_in_library"))
    _update_totals(registry, run_id)
    return summary(registry, run_id)


def _update_totals(registry: Registry, run_id: int) -> None:
    n = registry.one("SELECT count(DISTINCT para || '-' || sent) AS n FROM cite_items WHERE run_id=? AND verdict='pending'",
                     (run_id,))["n"]
    registry.execute("UPDATE cite_runs SET total=? WHERE id=?", (n, run_id))


# ------------------------------------------------------------------ evidence passages
_STOP = set("""the a an and or of in on for to with by from as at is are was were be been this that these those
which who whom whose it its their our we they he she than then into via using used use also both between among
such may might can could would should not no more most less other others however although while within without""".split())


def _terms(s: str) -> set[str]:
    return {w[:6] for w in re.findall(r"[a-z][a-z0-9\-]{2,}", s.lower()) if w not in _STOP}


_PAGES: dict[tuple[str, float], list[str]] = {}


def page_texts(pdf: Path) -> list[str]:
    key = (str(pdf), pdf.stat().st_mtime)
    if key not in _PAGES:
        with pymupdf.open(pdf) as doc:
            # PDFs sometimes map symbols such as ≈ to control characters; they must not end up in quotations
            _PAGES[key] = [re.sub(r"\s+", " ", re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", doc[i].get_text())).strip()
                           for i in range(min(doc.page_count, 80))]
        if len(_PAGES) > 20:
            _PAGES.pop(next(iter(_PAGES)))
    return _PAGES[key]


def passages(pdf: Path | None, sentence: str, card: dict) -> list[dict]:
    """The windows of the paper most similar to the claim (page-numbered), plus the card summary."""
    out = []
    if pdf is not None and pdf.exists():
        want = _terms(sentence)
        scored = []
        for pi, text in enumerate(page_texts(pdf)):
            if re.match(r"(references|bibliography)\b", text[:40], re.I):
                continue
            sents = re.split(r"(?<=[.!?])\s+", text)
            for i in range(len(sents)):
                win = " ".join(sents[max(0, i - 1): i + 2])[:PASSAGE_CHARS]
                overlap = len(want & _terms(win))
                if overlap:
                    scored.append((overlap + fuzz.token_set_ratio(sentence, win) / 50, pi, win))
        scored.sort(key=lambda x: -x[0])
        seen: set[str] = set()
        for _, pi, win in scored:
            key = win[:120]
            if key in seen:
                continue
            seen.add(key)
            out.append({"page": pi, "text": win})
            if len(out) >= PASSAGES:
                break
    summary = " | ".join([card.get("one_sentence", "")] + list(card.get("key_findings", [])[:5]))
    if summary.strip(" |"):
        out.append({"page": None, "text": "(PaperMap card summary) " + summary})
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


# ------------------------------------------------------------------ running
def check_sentence(llm: LLMClient, language: str, sentence: str, refs: list[dict]) -> list[dict]:
    """refs: [{rid, label, paper, passages}] -> [{rid, verdict, quote, page, explanation}]"""
    blocks = []
    for r in refs:
        p = r["paper"]
        blocks.append(f"### {r['rid']}: {p['first_author'] or '?'} {p['year'] or ''} — {p['title']}")
        for j, ps in enumerate(r["passages"], 1):
            where = f"p.{ps['page'] + 1}" if ps["page"] is not None else "card"
            blocks.append(f"[{r['rid']}.{j}] ({where}) {ps['text']}")
    system = "\n".join([
        "You check citations in a scientific manuscript. For each cited paper, decide whether the passages given from "
        "that paper support the manuscript sentence.",
        "Verdicts: supports (the paper states it), partial (only part of it, or weaker / narrower than claimed), "
        "contradicts (the paper says otherwise), not_found (these passages do not contain it).",
        "Quote the single most relevant sentence copied word for word from that paper's passages (not from the card "
        "summary unless nothing else fits), and give its passage id. Judge only from the passages; do not use outside knowledge.",
        f"Write the explanation in {language}, one or two short sentences.",
        'Reply with JSON: {"results": [{"ref": "R1", "verdict": "...", "passage": "R1.2", "quote": "...", "explanation": "..."}]}',
    ])
    user = f"MANUSCRIPT SENTENCE:\n{sentence}\n\nCITED PAPERS AND PASSAGES:\n" + "\n".join(blocks)

    def validate(d: dict) -> list[str]:
        res = d.get("results")
        if not isinstance(res, list):
            return ["results must be a list"]
        return [f"bad verdict {x.get('verdict')}" for x in res if isinstance(x, dict) and x.get("verdict") not in VERDICTS]

    out = llm.chat_json(system, user, validate, purpose="citecheck", max_tokens=2000)
    by_ref = {r["rid"]: r for r in refs}
    results = []
    for x in out.data.get("results", []):
        r = by_ref.get(str(x.get("ref")))
        if r is None:
            continue
        verdict, quote, page = x["verdict"], str(x.get("quote") or "").strip(), None
        found = None
        for ps in r["passages"]:
            if quote and _norm(quote) and _norm(quote) in _norm(ps["text"]):
                found = ps
                break
        if found is not None:
            page = found["page"]
        elif verdict in ("supports", "partial", "contradicts"):
            verdict, quote = "unclear", ""  # a judgement without a verifiable quotation is not accepted
        else:
            quote = ""
        results.append({"rid": r["rid"], "verdict": verdict, "quote": quote, "page": page,
                        "explanation": str(x.get("explanation") or "")[:600], "cost": out.cost_usd})
    return results


def start(pipeline, run_id: int) -> bool:
    registry = pipeline.registry
    row = registry.one("SELECT * FROM cite_runs WHERE id=?", (run_id,))
    if row is None or row["status"] == "running":
        return False
    stop = threading.Event()
    _threads[run_id] = stop
    registry.execute("UPDATE cite_runs SET status='running', message=NULL WHERE id=?", (run_id,))
    threading.Thread(target=_run, args=(pipeline, run_id, stop), daemon=True, name=f"citecheck-{run_id}").start()
    return True


def stop(run_id: int) -> None:
    ev = _threads.get(run_id)
    if ev:
        ev.set()


def _run(pipeline, run_id: int, stop_ev: threading.Event) -> None:
    registry, config = pipeline.registry, pipeline.config
    language = {"en": "English", "zh": "Simplified Chinese", "bilingual": "Simplified Chinese"}.get(
        config.output_language, "Simplified Chinese")
    try:
        groups = registry.query("SELECT para, sent, sentence FROM cite_items WHERE run_id=? AND verdict='pending' "
                                "GROUP BY para, sent ORDER BY para, sent", (run_id,))
        for g in groups:
            if stop_ev.is_set():
                registry.execute("UPDATE cite_runs SET status='stopped' WHERE id=?", (run_id,))
                return
            items = registry.query("SELECT * FROM cite_items WHERE run_id=? AND para=? AND sent=? AND verdict='pending'",
                                   (run_id, g["para"], g["sent"]))
            refs = []
            for k, it in enumerate(items, 1):
                paper = registry.get_paper(it["paper_id"])
                card = json.loads(paper["card_json"] or "{}") if paper else {}
                ps = passages(pipeline.pdf_for(it["paper_id"]), g["sentence"], card) if paper else []
                if not ps:
                    registry.execute("UPDATE cite_items SET verdict='no_text' WHERE id=?", (it["id"],))
                    continue
                refs.append({"rid": f"R{k}", "item": it, "paper": paper, "passages": ps})
            if refs:
                try:
                    results = check_sentence(pipeline.llm, language, g["sentence"], refs)
                except Exception as exc:  # noqa: BLE001 - one bad answer does not stop the run
                    results = []
                    registry.log("warning", f"Citation check: sentence skipped ({exc})")
                by_rid = {r["rid"]: r for r in refs}
                for res in results:
                    it = by_rid[res["rid"]]["item"]
                    registry.execute("UPDATE cite_items SET verdict=?, quote=?, page=?, explanation=? WHERE id=?",
                                     (res["verdict"], res["quote"], res["page"], res["explanation"], it["id"]))
                    registry.execute("UPDATE cite_runs SET cost_usd=cost_usd+? WHERE id=?", (res["cost"] / max(1, len(results)), run_id))
                for r in refs:  # no answer for this paper
                    registry.execute("UPDATE cite_items SET verdict='unclear' WHERE id=? AND verdict='pending'", (r["item"]["id"],))
            registry.execute("UPDATE cite_runs SET done=done+1 WHERE id=?", (run_id,))
        registry.execute("UPDATE cite_runs SET status='done', finished_at=?, model=? WHERE id=?",
                         (now_iso(), pipeline.llm.model, run_id))
        registry.log("info", f"Citation check #{run_id} finished")
    except Exception as exc:  # noqa: BLE001
        registry.execute("UPDATE cite_runs SET status='error', message=? WHERE id=?", (str(exc)[:500], run_id))
        registry.log("error", f"Citation check #{run_id} failed: {exc}")
    finally:
        _threads.pop(run_id, None)


# ------------------------------------------------------------------ reading results
def summary(registry: Registry, run_id: int) -> dict:
    ensure_schema(registry)
    run = registry.one("SELECT * FROM cite_runs WHERE id=?", (run_id,))
    if run is None:
        raise ValueError("找不到这次核对")
    counts = {r["verdict"]: r["n"] for r in registry.query(
        "SELECT verdict, count(*) AS n FROM cite_items WHERE run_id=? GROUP BY verdict", (run_id,))}
    papers = registry.one("SELECT count(DISTINCT paper_id) AS n FROM cite_items WHERE run_id=? AND paper_id IS NOT NULL",
                          (run_id,))["n"]
    return {**dict(run), "counts": counts, "papers": papers, "refs": json.loads(run["refs_json"] or "{}"),
            "items": sum(counts.values())}


def items(registry: Registry, run_id: int) -> list[dict]:
    out = []
    for r in registry.query("SELECT i.*, p.first_author, p.year, p.title FROM cite_items i LEFT JOIN papers p ON p.id=i.paper_id "
                            "WHERE i.run_id=? ORDER BY i.para, i.sent, i.id", (run_id,)):
        out.append(dict(r))
    return out


def runs(registry: Registry) -> list:
    ensure_schema(registry)
    return registry.query("SELECT id, name, status, total, done, created_at FROM cite_runs ORDER BY id DESC LIMIT 50")


def delete(registry: Registry, run_id: int) -> None:
    stop(run_id)
    registry.execute("DELETE FROM cite_items WHERE run_id=?", (run_id,))
    registry.execute("DELETE FROM cite_runs WHERE id=?", (run_id,))


def report_md(registry: Registry, run_id: int, labels: dict) -> str:
    s = summary(registry, run_id)
    lines = [f"# {s['name']}", ""]
    for it in items(registry, run_id):
        lines.append(f"- **{labels.get(it['verdict'], it['verdict'])}** · [{it['ref_label']}] "
                     f"{(it['first_author'] or '')} {it['year'] or ''} — {it['sentence']}")
        if it["quote"]:
            lines.append(f"  > {it['quote']}" + (f" (p.{it['page'] + 1})" if it["page"] is not None else ""))
        if it["explanation"]:
            lines.append(f"  {it['explanation']}")
    return "\n".join(lines) + "\n"
