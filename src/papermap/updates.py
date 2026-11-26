"""Update cards: what does this paper change for one of my research questions?
(CLAUDE.md §16.2 task 3)

Three separate things, kept apart in data and UI:
- the model proposes a judgment (a card in state 'pending' = 待核对);
- every claim on the card is anchored to sentences found verbatim in the new
  paper's PDF, and comparisons cite the user's earlier evidence quotes (which
  are themselves anchored), so both sides can be clicked back to the source;
- only the user's confirmation / rewrite moves a card into the field page's
  "recently confirmed changes"; "keep my current judgment" is also recorded.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

import pymupdf

from . import evidence
from .extract import LANGUAGE_RULES
from .extract import load_prompt as _load_prompt
from .llm import LLMClient
from .registry import Registry, now_iso
from .i18n import L

PROMPT_VERSION = "update_v1"
OUTCOMES = {
    "unchanged": L("未改变当前判断"),
    "support": L("又一篇支持证据"),
    "first_intervention": L("本库在这个问题下收录的首条干预证据"),
    "contradicts": L("结论相反"),
    "extends": L("扩展到新人群 / 物种 / 组织"),
    "method": L("方法改进"),
    "unrelated": L("这篇没有涉及这个问题"),
}
NEEDS_OLD = {"support", "contradicts", "extends", "method"}  # comparisons need anchors on both sides
STATUSES = {"pending": L("待核对"), "confirmed": L("已确认"), "rewritten": L("已改写确认"), "kept": L("保留原判断"),
            "dismissed": L("已忽略")}
DECIDED_CHANGES = ("confirmed", "rewritten")
# a confirmed card adds its new quotations to the question's evidence with this stance
STANCE = {"support": "supports", "first_intervention": "supports", "extends": "supports",
          "contradicts": "opposes"}
MAX_OLD = 30
MAX_QUOTES = 8


# ------------------------------------------------------------------ anchoring
def _norm(text: str) -> str:
    """Lower-case alphanumerics only; NFKC folds ligatures (ﬁ -> fi) and full-width forms."""
    return "".join(ch.lower() for ch in unicodedata.normalize("NFKC", text) if ch.isalnum())


def locate(page: pymupdf.Page, quote: str) -> list[list[float]] | None:
    """Rectangles (one per text line) of a quotation on a page, tolerant of line breaks,
    hyphenation and punctuation; None when the words are not on the page."""
    words = page.get_text("words")
    if not words or not quote.strip():
        return None
    flat, owner = [], []
    for k, w in enumerate(words):
        n = _norm(w[4])
        flat.append(n)
        owner.extend([k] * len(n))
    hay = "".join(flat)
    q = _norm(quote.replace("-\n", ""))
    if len(q) < 12:
        return None
    start = hay.find(q)
    end = start + len(q)
    if start < 0 and len(q) > 120:  # long sentence: accept a verbatim head and tail
        head, tail = q[:80], q[-40:]
        start = hay.find(head)
        if start >= 0:
            t = hay.find(tail, start + len(head), start + int(len(q) * 1.3))
            end = t + len(tail) if t >= 0 else -1
            if end < 0:
                start = -1
    if start < 0:
        return None
    lines: dict[tuple[int, int], list[float]] = {}
    for k in sorted(set(owner[start:end])):
        x0, y0, x1, y1, _, block, line, _ = words[k][:8]
        r = lines.setdefault((block, line), [x0, y0, x1, y1])
        r[0], r[1], r[2], r[3] = min(r[0], x0), min(r[1], y0), max(r[2], x1), max(r[3], y1)
    return [[round(v, 2) for v in r] for r in lines.values()]


# ------------------------------------------------------------------ inputs
def paper_text(pdf: Path, limit: int) -> str:
    """Full text with [p.N] markers; pages that are mostly a reference list are skipped."""
    parts, used = [], 0
    with pymupdf.open(pdf) as doc:
        for i, page in enumerate(doc):
            text = page.get_text().strip()
            if not text:
                continue
            if i > 1 and len(re.findall(r"et al\.|doi\.org|\bdoi:", text, re.I)) > 14:
                continue
            block = f"[p.{i + 1}]\n{text}"
            if used + len(block) > limit:
                parts.append(block[: max(0, limit - used)])
                break
            parts.append(block)
            used += len(block)
    return "\n\n".join(parts)


def old_evidence(registry: Registry, qid: int, paper_id: int) -> list:
    return registry.query(
        "SELECT e.*, p.title, p.first_author, p.year, p.doi FROM evidence_entries e JOIN papers p ON p.id=e.paper_id "
        "WHERE e.question_id=? AND e.paper_id!=? AND e.source!='region' ORDER BY e.id DESC LIMIT ?", (qid, paper_id, MAX_OLD))


def candidate_questions(registry: Registry, paper_id: int) -> list[dict]:
    """Active questions; those hung under this paper's fields first."""
    mine = {r["question_id"] for r in registry.query(
        "SELECT DISTINCT qf.question_id FROM evidence_question_fields qf "
        "JOIN paper_fields pf ON pf.field_id=qf.field_id WHERE pf.paper_id=?", (paper_id,))}
    rows = [dict(r) for r in evidence.questions(registry)]
    for r in rows:
        r["related"] = r["id"] in mine
    return sorted(rows, key=lambda r: not r["related"])


# ------------------------------------------------------------------ generation
def generate(config, registry: Registry, llm: LLMClient, paper_id: int, pdf: Path, qid: int) -> dict:
    question = registry.one("SELECT * FROM evidence_questions WHERE id=? AND status='active'", (qid,))
    if question is None:
        raise ValueError("这个研究问题不存在或还没有确认")
    paper = registry.get_paper(paper_id)
    olds = old_evidence(registry, qid, paper_id)
    tags = {f"E{e['id']}": e for e in olds}
    revision = evidence.document_revision(pdf)
    lines = [f"RESEARCH QUESTION: {question['title']}", "", "OLD EVIDENCE (already collected by the researcher):"]
    for tag, e in tags.items():
        who = f"{e['first_author'] or '?'} {e['year'] or ''}".strip()
        stance = {"supports": "supports", "opposes": "opposes", "pending": "unclassified"}[e["stance"]]
        lines.append(f"[{tag}] {who} — {e['title']} — p.{e['page'] + 1} — the researcher marked it: {stance}\n"
                     f"\"{e['quote'][:700]}\"" + (f"\nResearcher's note: {e['note'][:300]}" if e["note"] else ""))
    if not tags:
        lines.append("(none yet - only 'unchanged', 'first_intervention' or 'unrelated' are possible)")
    limit = int(config.llm.get("max_input_chars", 60000))
    lines += ["", f"NEW PAPER: {paper['title']} ({paper['first_author'] or '?'} {paper['year'] or ''})", "",
              paper_text(pdf, limit)]

    with pymupdf.open(pdf) as doc:
        n_pages = len(doc)

        def validate(data: dict) -> list[str]:
            outcome = data.get("outcome")
            if outcome not in OUTCOMES:
                return [f"'outcome' must be one of {sorted(OUTCOMES)}"]
            if outcome == "unrelated":
                data["claims"] = []
            claims, missing = [], []
            for c in (data.get("claims") or [])[:4]:
                if not isinstance(c, dict) or not str(c.get("text", "")).strip():
                    continue
                anchors = []
                for nq in (c.get("new") or [])[:3]:
                    if not isinstance(nq, dict):
                        continue
                    quote = str(nq.get("quote", "")).strip()
                    page = nq.get("page")
                    pages = [page - 1] if isinstance(page, int) and 0 < page <= n_pages else []
                    pages += [p for p in range(n_pages) if p not in pages]  # wrong page numbers happen
                    for p in pages:
                        rects = locate(doc[p], quote)
                        if rects:
                            anchors.append({"page": p, "quote": quote[:2000], "rects": rects})
                            break
                    else:
                        missing.append(quote[:120])
                old = list(dict.fromkeys(x for x in (c.get("old") or []) if x in tags))
                if anchors:
                    claims.append({"text": str(c["text"]).strip()[:600], "new": anchors, "old": old})
            if outcome != "unrelated" and not claims:
                return ["None of the 'new' quotations could be found verbatim in the paper"
                        + (f" (e.g. {missing[0]!r})" if missing else "")
                        + ". Copy sentences exactly as they appear on the page."]
            if outcome in NEEDS_OLD and not any(c["old"] for c in claims):
                if not tags:
                    return [f"'{outcome}' compares with old evidence but there is none; "
                            "use 'unchanged', 'first_intervention' or 'unrelated'."]
                return [f"'{outcome}' needs at least one claim citing old evidence in 'old' (E tags)."]
            data["claims"] = claims
            data["headline"] = str(data.get("headline", "")).strip()[:600] or (claims[0]["text"] if claims else "")
            data["comparability"] = "check" if data.get("comparability") == "check" else "comparable"
            data["comparability_note"] = str(data.get("comparability_note", "")).strip()[:600]
            return []

        rule = LANGUAGE_RULES.get(config.content_language, LANGUAGE_RULES["zh"])
        res = llm.chat_json(_load_prompt(PROMPT_VERSION).replace("{language_rule}", rule), "\n".join(lines),
                            validate, purpose="update", paper_id=paper_id, max_tokens=3000)
    d = res.data
    claims = [{**c, "old": [_old_ref(tags[t]) for t in c["old"]]} for c in d["claims"]]
    with registry._lock, registry.conn:  # noqa: SLF001 - a regenerated card replaces the undecided one
        registry.conn.execute("DELETE FROM update_cards WHERE paper_id=? AND question_id=? AND status='pending'",
                              (paper_id, qid))
        cur = registry.conn.execute(
            "INSERT INTO update_cards(paper_id, question_id, outcome, headline, claims_json, comparability, "
            "comparability_note, status, revision, model, prompt_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (paper_id, qid, d["outcome"], d["headline"], json.dumps(claims, ensure_ascii=False), d["comparability"],
             d["comparability_note"], "pending", revision, res.model, PROMPT_VERSION, now_iso()))
    return {"id": int(cur.lastrowid), "outcome": d["outcome"]}


def _old_ref(e) -> dict:
    """A snapshot of the earlier evidence, so the card still reads right if the entry is edited later."""
    return {"id": e["id"], "paper_id": e["paper_id"], "page": e["page"], "quote": e["quote"][:1000],
            "stance": e["stance"], "label": f"{e['first_author'] or '?'} {e['year'] or ''}".strip(),
            "title": e["title"] or ""}


# ------------------------------------------------------------------ reading & deciding
def _decorate(row) -> dict:
    d = dict(row)
    d["claims"] = json.loads(row["claims_json"])
    d["outcome_label"] = OUTCOMES.get(row["outcome"], row["outcome"])
    final = row["user_outcome"] or row["outcome"]
    d["final_outcome"] = final
    d["final_label"] = OUTCOMES.get(final, final)
    d["status_label"] = STATUSES.get(row["status"], row["status"])
    d["paper_label"] = f"{row['first_author'] or '?'} {row['year'] or ''}".strip() if "first_author" in row.keys() else ""
    k = 0
    for c in d["claims"]:
        for a in c["new"]:
            a["k"] = k
            k += 1
    return d


_SELECT = ("SELECT u.*, q.title AS question, p.title AS paper_title, p.first_author, p.year, p.doi "
           "FROM update_cards u JOIN evidence_questions q ON q.id=u.question_id JOIN papers p ON p.id=u.paper_id ")


def for_paper(registry: Registry, paper_id: int) -> list[dict]:
    return [_decorate(r) for r in registry.query(
        _SELECT + "WHERE u.paper_id=? AND u.status!='dismissed' ORDER BY u.id DESC", (paper_id,))]


def for_question(registry: Registry, qid: int) -> list[dict]:
    return [_decorate(r) for r in registry.query(
        _SELECT + "WHERE u.question_id=? AND u.status!='dismissed' ORDER BY u.id DESC", (qid,))]


def pending(registry: Registry) -> list[dict]:
    return [_decorate(r) for r in registry.query(_SELECT + "WHERE u.status='pending' ORDER BY u.id DESC")]


def pending_count(registry: Registry) -> int:
    return registry.one("SELECT count(*) AS n FROM update_cards WHERE status='pending'")["n"]


def confirmed_for_field(registry: Registry, field_id: int, limit: int = 12) -> tuple[list[dict], int]:
    """Recently confirmed changes for questions hung under this field (or about its papers),
    plus how many confirmed cards said 'nothing changes'."""
    where = ("(u.question_id IN (SELECT question_id FROM evidence_question_fields WHERE field_id=?) "
             "OR u.paper_id IN (SELECT paper_id FROM paper_fields WHERE field_id=?)) AND u.status IN ('confirmed','rewritten')")
    rows = registry.query(_SELECT + f"WHERE {where} AND COALESCE(u.user_outcome,u.outcome) NOT IN ('unchanged','unrelated') "
                          "ORDER BY u.decided_at DESC LIMIT ?", (field_id, field_id, limit))
    same = registry.one(f"SELECT count(*) AS n FROM update_cards u WHERE {where} "
                        "AND COALESCE(u.user_outcome,u.outcome)='unchanged'", (field_id, field_id))["n"]
    return [_decorate(r) for r in rows], same


def anchor(registry: Registry, card_id: int, paper_id: int, k: int) -> dict | None:
    row = registry.one("SELECT * FROM update_cards WHERE id=? AND paper_id=?", (card_id, paper_id))
    if row is None:
        return None
    flat = [a for c in json.loads(row["claims_json"]) for a in c["new"]]
    if not 0 <= k < len(flat):
        return None
    return {"page": flat[k]["page"], "rects": flat[k]["rects"], "quote": flat[k]["quote"],
            "revision": row["revision"], "question_id": row["question_id"]}


def decide(registry: Registry, card_id: int, action: str, text: str = "", outcome: str = "") -> dict:
    """confirm / rewrite / keep (保留原判断) / dismiss. Confirming or rewriting files the card's
    new quotations as evidence under the question (stance from the outcome)."""
    row = registry.one("SELECT * FROM update_cards WHERE id=?", (card_id,))
    if row is None:
        raise ValueError("这张更新卡已不存在。")
    if action not in ("confirm", "rewrite", "keep", "dismiss", "reopen"):
        raise ValueError("操作无效")
    if action == "reopen":
        registry.execute("UPDATE update_cards SET status='pending', decided_at=NULL WHERE id=?", (card_id,))
        return {"status": "pending", "added": 0}
    status = {"confirm": "confirmed", "rewrite": "rewritten", "keep": "kept", "dismiss": "dismissed"}[action]
    user_text, user_outcome = row["user_text"], row["user_outcome"]
    if action == "rewrite":
        user_text = evidence.valid_text(text, "改写后的判断", 2000)
        if outcome:
            if outcome not in OUTCOMES or outcome == "unrelated":
                raise ValueError("影响类型无效")
            if outcome in NEEDS_OLD and not any(c["old"] for c in json.loads(row["claims_json"])):
                raise ValueError(f"「{OUTCOMES[outcome]}」需要和旧证据对比，这张卡没有引用旧证据。")
            user_outcome = outcome
    registry.execute("UPDATE update_cards SET status=?, user_text=?, user_outcome=?, decided_at=? WHERE id=?",
                     (status, user_text, user_outcome, now_iso(), card_id))
    added = 0
    stance = STANCE.get(user_outcome or row["outcome"])
    if status in DECIDED_CHANGES and stance:
        note = "来自更新卡：" + (user_text or row["headline"])
        for c in json.loads(row["claims_json"]):
            for a in c["new"][:MAX_QUOTES]:
                r = evidence.add_entry(registry, row["question_id"], "", row["paper_id"], a["page"], row["revision"],
                                       a["rects"], a["quote"], "native", stance, note)
                added += r["created"]
    return {"status": status, "added": added}
