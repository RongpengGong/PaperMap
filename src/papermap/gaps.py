"""Evidence gaps under a research question and "this paper may answer it" reminders (stage 7 task 5).

A gap is something the user still lacks evidence for ("no interventional study in humans yet").
Each gap carries search terms; matching a paper against them uses only the local full-text index
(no model cost). A match is a *reminder to read*, nothing more (CLAUDE.md §16.1): it lists the
passages worth reading first. Judging what the paper changes is an update card; closing the gap is
the user's own decision.
"""
from __future__ import annotations

import json
import re

from . import search
from .i18n import L
from .registry import Registry, now_iso

GAP_STATUSES = {"open": L("待补"), "closed": L("已补上")}
HIT_STATUSES = {"new": L("新提醒"), "seen": L("已看"), "dismissed": L("不相关"), "answered": L("补上了缺口")}
TERMS_PROMPT_VERSION = "gap_terms_v1"
MAX_TERMS = 12
PASSAGES = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS question_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES evidence_questions(id) ON DELETE CASCADE,
    text TEXT NOT NULL, terms_json TEXT NOT NULL DEFAULT '[]',
    terms_origin TEXT NOT NULL DEFAULT 'user',          -- user / model / auto
    status TEXT NOT NULL DEFAULT 'open',                -- open / closed
    closed_by INTEGER REFERENCES papers(id), closed_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gap_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gap_id INTEGER NOT NULL REFERENCES question_gaps(id) ON DELETE CASCADE,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    score REAL NOT NULL, matched_json TEXT NOT NULL, passages_json TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'import',              -- import (new paper) / library (gap added later)
    status TEXT NOT NULL DEFAULT 'new',                 -- new / seen / dismissed / answered
    created_at TEXT NOT NULL, decided_at TEXT,
    UNIQUE(gap_id, paper_id)
);
CREATE INDEX IF NOT EXISTS idx_gap_hits_paper ON gap_hits(paper_id, status);
"""


def ensure_schema(registry: Registry) -> None:
    with registry._lock:  # noqa: SLF001
        registry.conn.executescript(SCHEMA)
        registry.conn.commit()


# ------------------------------------------------------------------ terms
def clean_terms(terms) -> list[str]:
    out: list[str] = []
    if isinstance(terms, str):
        terms = re.split(r"[\n,;，；、]+", terms)
    for t in terms or []:
        t = " ".join(str(t).replace('"', " ").split())[:60]
        if len(t) >= 2 and t.lower() not in (x.lower() for x in out):
            out.append(t)
    return out[:MAX_TERMS]


_STOP = {"the", "and", "for", "with", "from", "that", "this", "are", "was", "were", "not", "yet", "any", "into",
         "have", "has", "does", "how", "what", "which", "who", "why", "when", "study", "studies", "evidence", "data"}


def auto_terms(text: str) -> list[str]:
    """Free fallback: the Latin words of the gap text itself (gene names, methods, diseases)."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-+/]{2,}", text)
    return clean_terms([w for w in words if w.lower() not in _STOP])


def model_terms(llm, question: str, gap: str) -> list[str]:
    """English search phrases for a gap (papers are mostly English, gaps are often written in Chinese)."""
    system = (
        "You turn a researcher's evidence gap into search terms for finding passages in English scientific papers.\n"
        "Return JSON {\"terms\": [..]} with 4-10 short terms: specific nouns and phrases as they would literally "
        "appear in a paper (gene / protein / cell type / disease names, techniques, species, study designs), "
        "including common synonyms and abbreviations. No generic words like 'study', 'role', 'mechanism'.")
    user = f"RESEARCH QUESTION: {question}\nEVIDENCE GAP: {gap}"

    def validate(data: dict) -> list[str]:
        raw = data.get("terms") if isinstance(data.get("terms"), list) else []
        terms = clean_terms([t for t in raw if str(t).strip().lower() not in _STOP])
        if not terms:
            return ["'terms' must be a non-empty list of strings"]
        data["terms"] = terms
        return []

    return llm.chat_json(system, user, validate, purpose="gap_terms", max_tokens=400).data["terms"]


# ------------------------------------------------------------------ gaps
def _row(r) -> dict:
    d = dict(r)
    d["terms"] = json.loads(d.pop("terms_json") or "[]")
    d["status_label"] = GAP_STATUSES.get(d["status"], d["status"])
    return d


def get_gap(registry: Registry, gid: int) -> dict | None:
    ensure_schema(registry)
    r = registry.one("SELECT * FROM question_gaps WHERE id=?", (gid,))
    return _row(r) if r else None


def for_question(registry: Registry, qid: int) -> list[dict]:
    ensure_schema(registry)
    gaps = [_row(r) for r in registry.query(
        "SELECT g.*, p.first_author AS closed_author, p.year AS closed_year FROM question_gaps g "
        "LEFT JOIN papers p ON p.id=g.closed_by WHERE g.question_id=? ORDER BY g.status='closed', g.id", (qid,))]
    hits = hits_where(registry, "g.question_id=? AND h.status IN ('new','seen','answered')", (qid,))
    for g in gaps:
        g["hits"] = [h for h in hits if h["gap_id"] == g["id"]]
    return gaps


def add_gap(registry: Registry, qid: int, text, terms=None, origin: str = "user") -> int:
    ensure_schema(registry)
    if not isinstance(text, str) or not text.strip() or len(text) > 300:
        raise ValueError("缺口描述需要 1–300 个字符。")
    if not registry.one("SELECT 1 FROM evidence_questions WHERE id=? AND status='active'", (qid,)):
        raise ValueError("这个研究问题不存在或还没有确认")
    terms = clean_terms(terms) or auto_terms(text)
    with registry._lock, registry.conn:  # noqa: SLF001
        cur = registry.conn.execute(
            "INSERT INTO question_gaps(question_id, text, terms_json, terms_origin, created_at) VALUES (?,?,?,?,?)",
            (qid, text.strip(), json.dumps(terms, ensure_ascii=False), origin if terms else "user", now_iso()))
    return int(cur.lastrowid)


def update_gap(registry: Registry, gid: int, text=None, terms=None, origin: str = "user") -> None:
    g = get_gap(registry, gid)
    if g is None:
        raise ValueError("这个缺口不存在。")
    sets, params = [], []
    if text is not None:
        if not isinstance(text, str) or not text.strip() or len(text) > 300:
            raise ValueError("缺口描述需要 1–300 个字符。")
        sets.append("text=?"); params.append(text.strip())  # noqa: E702
    if terms is not None:
        sets += ["terms_json=?", "terms_origin=?"]
        params += [json.dumps(clean_terms(terms), ensure_ascii=False), origin]
    if sets:
        registry.execute(f"UPDATE question_gaps SET {', '.join(sets)} WHERE id=?", (*params, gid))
    if terms is not None:  # new terms: forget reminders nobody acted on, then look again
        registry.execute("DELETE FROM gap_hits WHERE gap_id=? AND status='new'", (gid,))


def set_gap_status(registry: Registry, gid: int, status: str, paper_id: int | None = None) -> None:
    if status not in GAP_STATUSES or get_gap(registry, gid) is None:
        raise ValueError("无效的状态")
    with registry._lock, registry.conn:  # noqa: SLF001
        registry.conn.execute("UPDATE question_gaps SET status=?, closed_by=?, closed_at=? WHERE id=?",
                              (status, paper_id if status == "closed" else None,
                               now_iso() if status == "closed" else None, gid))
        if status == "closed" and paper_id:
            registry.conn.execute("UPDATE gap_hits SET status='answered', decided_at=? WHERE gap_id=? AND paper_id=?",
                                  (now_iso(), gid, paper_id))
        if status == "open":
            registry.conn.execute("UPDATE gap_hits SET status='seen' WHERE gap_id=? AND status='answered'", (gid,))


def delete_gap(registry: Registry, gid: int) -> None:
    ensure_schema(registry)
    with registry._lock, registry.conn:  # noqa: SLF001
        registry.conn.execute("DELETE FROM gap_hits WHERE gap_id=?", (gid,))
        registry.conn.execute("DELETE FROM question_gaps WHERE id=?", (gid,))


# ------------------------------------------------------------------ matching
def _is_reference_list(body: str) -> bool:
    return len(re.findall(r"et al\.|doi\.org|\bdoi:", body, re.I)) > 14


def _matched(body: str, terms: list[str]) -> list[str]:
    low = body.lower()
    return [t for t in terms if t.lower() in low]


def _best_snippet(body: str, terms: list[str], width: int = 160) -> str:
    """The window of the page where most different terms occur close together."""
    low, best, best_n = body.lower(), 0, -1
    spots = sorted(m.start() for t in terms for m in re.finditer(re.escape(t.lower()), low))
    for s in spots[:400]:
        window = low[max(0, s - width): s + width * 2]
        n = sum(1 for t in terms if t.lower() in window)
        if n > best_n:
            best, best_n = s, n
    start = max(0, best - width)
    end = best + width * 2
    text = ("…" if start else "") + body[start:end] + ("…" if end < len(body) else "")
    return " ".join(text.split())


def score_paper(registry: Registry, paper_id: int, terms: list[str]) -> dict | None:
    """Passages of one paper that hit several gap terms on the same page; None when it doesn't qualify."""
    if not terms:
        return None
    rows = search.search(registry, "", kinds={"page", "card"}, paper_ids=[paper_id], limit=400, any_term=True,
                         terms=terms)
    need = min(2, len(terms))
    pages, card = [], []
    for r in rows:
        m = _matched(r["body"], terms)
        if r["kind"] == "card":
            card = m
        elif len(m) >= need and not (r["page"] and r["page"] > 1 and _is_reference_list(r["body"])):
            pages.append({"page": int(r["page"]), "terms": m, "snippet": _best_snippet(r["body"], m)})
    if not pages:
        return None
    pages.sort(key=lambda p: (-len(p["terms"]), p["page"]))
    matched = sorted({t for p in pages for t in p["terms"]} | set(card), key=terms.index)
    score = len(pages[0]["terms"]) + 0.5 * len(card) + 0.1 * min(len(pages), 10) + len(matched) / len(terms)
    return {"score": round(score, 2), "matched": matched, "passages": pages[:PASSAGES]}


def _store(registry: Registry, gid: int, paper_id: int, res: dict, origin: str) -> bool:
    with registry._lock, registry.conn:  # noqa: SLF001
        cur = registry.conn.execute(
            "INSERT OR IGNORE INTO gap_hits(gap_id, paper_id, score, matched_json, passages_json, origin, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (gid, paper_id, res["score"], json.dumps(res["matched"], ensure_ascii=False),
             json.dumps(res["passages"], ensure_ascii=False), origin, now_iso()))
    return bool(cur.rowcount)


def match_paper(registry: Registry, paper_id: int, origin: str = "import") -> list[int]:
    """Check one (newly imported) paper against every open gap. Returns ids of gaps it may answer."""
    ensure_schema(registry)
    found = []
    for g in registry.query("SELECT g.id, g.terms_json FROM question_gaps g JOIN evidence_questions q "
                            "ON q.id=g.question_id WHERE g.status='open' AND q.status='active'"):
        res = score_paper(registry, paper_id, json.loads(g["terms_json"] or "[]"))
        if res and _store(registry, g["id"], paper_id, res, origin):
            found.append(g["id"])
    return found


def scan_library(registry: Registry, gid: int, limit: int = 8) -> int:
    """After a gap is added or its terms change: papers already in the library that may answer it."""
    g = get_gap(registry, gid)
    if g is None or g["status"] != "open" or not g["terms"]:
        return 0
    rows = search.search(registry, "", kinds={"page", "card"}, limit=3000, any_term=True, terms=g["terms"])
    candidates = list(dict.fromkeys(r["paper_id"] for r in rows))
    scored = []
    for pid in candidates:
        res = score_paper(registry, pid, g["terms"])
        if res:
            scored.append((res["score"], pid, res))
    scored.sort(key=lambda x: -x[0])
    return sum(_store(registry, gid, pid, res, "library") for _, pid, res in scored[:limit])


# ------------------------------------------------------------------ reminders
def hits_where(registry: Registry, where: str, params: tuple = ()) -> list[dict]:
    ensure_schema(registry)
    rows = registry.query(
        "SELECT h.*, g.text AS gap, g.question_id, g.status AS gap_status, q.title AS question, "
        "p.title AS paper_title, p.first_author, p.year, p.journal FROM gap_hits h "
        "JOIN question_gaps g ON g.id=h.gap_id JOIN evidence_questions q ON q.id=g.question_id "
        f"JOIN papers p ON p.id=h.paper_id WHERE p.status!='ignored' AND ({where}) ORDER BY h.status!='new', h.score DESC, h.id DESC", params)
    out = []
    for r in rows:
        d = dict(r)
        d["matched"] = json.loads(d.pop("matched_json"))
        d["passages"] = json.loads(d.pop("passages_json"))
        d["status_label"] = HIT_STATUSES.get(d["status"], d["status"])
        d["paper_label"] = f"{d['first_author'] or ''} {d['year'] or ''}".strip() or f"#{d['paper_id']}"
        out.append(d)
    return out


def for_paper(registry: Registry, paper_id: int) -> list[dict]:
    return hits_where(registry, "h.paper_id=? AND h.status!='dismissed' AND q.status='active'", (paper_id,))


def new_hits(registry: Registry) -> list[dict]:
    return hits_where(registry, "h.status='new' AND g.status='open' AND q.status='active'")


def new_count(registry: Registry) -> int:
    ensure_schema(registry)
    r = registry.one("SELECT count(*) AS n FROM gap_hits h JOIN question_gaps g ON g.id=h.gap_id "
                     "JOIN evidence_questions q ON q.id=g.question_id JOIN papers p ON p.id=h.paper_id "
                     "WHERE p.status!='ignored' AND h.status='new' AND g.status='open' AND q.status='active'")
    return int(r["n"]) if r else 0


def decide_hit(registry: Registry, hid: int, action: str) -> dict:
    ensure_schema(registry)
    h = registry.one("SELECT * FROM gap_hits WHERE id=?", (hid,))
    if h is None:
        raise ValueError("这条提醒不存在。")
    if action == "answered":
        set_gap_status(registry, h["gap_id"], "closed", h["paper_id"])
    elif action in ("seen", "dismissed", "new"):
        registry.execute("UPDATE gap_hits SET status=?, decided_at=? WHERE id=?", (action, now_iso(), hid))
    else:
        raise ValueError("无效的操作")
    return dict(registry.one("SELECT * FROM gap_hits WHERE id=?", (hid,)))
