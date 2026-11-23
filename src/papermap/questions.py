"""Research questions on the knowledge map (CLAUDE.md §16.2 task 2).

A question can hang under several fields. The model may *propose* questions
(status 'suggested', from a field's recorded disputes and the papers' own
research questions); only the user's confirmation makes one 'active', and
only active questions appear on the map and in the evidence notebook.
"""

from __future__ import annotations

import json
from rapidfuzz import fuzz

from .extract import LANGUAGE_RULES
from .extract import load_prompt as _load_prompt
from .llm import LLMClient
from .registry import Registry, now_iso

PROMPT_VERSION = "questions_v1"
MAX_SUGGESTIONS = 3


def field_questions(registry: Registry, field_id: int, status: str = "active") -> list[dict]:
    rows = registry.query(
        "SELECT q.*, "
        "(SELECT count(*) FROM evidence_entries e WHERE e.question_id=q.id) AS total, "
        "(SELECT count(*) FROM evidence_entries e WHERE e.question_id=q.id AND e.stance='supports') AS supports, "
        "(SELECT count(*) FROM evidence_entries e WHERE e.question_id=q.id AND e.stance='opposes') AS opposes, "
        "(SELECT count(*) FROM evidence_entries e WHERE e.question_id=q.id AND e.stance='pending') AS pending, "
        "(SELECT count(DISTINCT e.paper_id) FROM evidence_entries e WHERE e.question_id=q.id) AS papers "
        "FROM evidence_questions q JOIN evidence_question_fields qf ON qf.question_id=q.id "
        "WHERE qf.field_id=? AND q.status=? ORDER BY q.id DESC", (field_id, status))
    out = []
    for r in rows:
        d = dict(r)
        d["source"] = json.loads(r["source_json"] or "{}")
        out.append(d)
    return out


def question_fields(registry: Registry, qid: int) -> list[dict]:
    return [dict(r) for r in registry.query(
        "SELECT f.id, f.slug, f.name_zh, f.name_en, f.axis FROM evidence_question_fields qf "
        "JOIN fields f ON f.id=qf.field_id WHERE qf.question_id=? ORDER BY f.axis, f.name_zh", (qid,))]


def link_field(registry: Registry, qid: int, field_id: int, on: bool = True) -> None:
    if not registry.one("SELECT 1 FROM evidence_questions WHERE id=?", (qid,)):
        raise ValueError("这个研究问题已不存在。")
    if not registry.one("SELECT 1 FROM fields WHERE id=?", (field_id,)):
        raise ValueError("这个学科不存在。")
    if on:
        registry.execute("INSERT OR IGNORE INTO evidence_question_fields(question_id, field_id) VALUES (?,?)",
                         (qid, field_id))
    else:
        registry.execute("DELETE FROM evidence_question_fields WHERE question_id=? AND field_id=?", (qid, field_id))


def set_status(registry: Registry, qid: int, status: str, title: str | None = None) -> None:
    if status not in ("active", "dismissed"):
        raise ValueError("状态无效")
    row = registry.one("SELECT * FROM evidence_questions WHERE id=?", (qid,))
    if not row:
        raise ValueError("这个研究问题已不存在。")
    title = (title or "").strip()
    if title and title != row["title"]:
        if len(title) > 300:
            raise ValueError("研究问题最多 300 个字符。")
        if registry.one("SELECT 1 FROM evidence_questions WHERE title=? AND id!=?", (title, qid)):
            raise ValueError("已经有同名的研究问题。")
        registry.execute("UPDATE evidence_questions SET title=? WHERE id=?", (title, qid))
    registry.execute("UPDATE evidence_questions SET status=? WHERE id=?", (status, qid))


def graph_parts(registry: Registry) -> tuple[list[dict], list[dict]]:
    """Map nodes for active questions linked to at least one field, and their edges."""
    rows = registry.query(
        "SELECT q.id, q.title, qf.field_id, "
        "(SELECT count(*) FROM evidence_entries e WHERE e.question_id=q.id) AS total "
        "FROM evidence_questions q JOIN evidence_question_fields qf ON qf.question_id=q.id "
        "WHERE q.status='active'")
    nodes: dict[int, dict] = {}
    edges = []
    for r in rows:
        nodes.setdefault(r["id"], {"id": f"q{r['id']}", "kind": "question", "url": f"/evidence?question={r['id']}",
                                   "label": r["title"] if len(r["title"]) <= 18 else r["title"][:17] + "…",
                                   "title": r["title"], "count": r["total"]})
        edges.append({"source": f"q{r['id']}", "target": f"f{r['field_id']}"})
    return list(nodes.values()), edges


# ------------------------------------------------------------------ suggestions
def _inputs(registry: Registry, field) -> tuple[str, dict[str, int]]:
    rows = registry.field_paper_rows(field["id"])[:30]
    refs = {f"P{i}": r["paper_id"] for i, r in enumerate(rows, 1)}
    lines = [f"FIELD: {field['name_en']} / {field['name_zh']} ({field['axis']})",
             f"Definition: {field['description'] or ''}", ""]
    syn = registry.one("SELECT data_json FROM field_syntheses WHERE field_id=? ORDER BY id DESC LIMIT 1",
                       (field["id"],))
    by_pid = {v: k for k, v in refs.items()}
    if syn:
        data = json.loads(syn["data_json"])
        for key, title in (("disputes", "RECORDED DISAGREEMENTS"), ("progress", "RECORDED PROGRESS")):
            if data.get(key):
                lines.append(title + ":")
                for it in data[key]:
                    ids = [by_pid[p] for p in it.get("paper_ids", []) if p in by_pid]
                    lines.append(f"- {it['text']} {ids}")
                lines.append("")
    lines.append("PAPERS (their own research questions):")
    for ref, r in zip(refs, rows):
        card = json.loads(r["card_json"] or "{}")
        lines.append(f"[{ref}] {r['title']} ({r['first_author']} {r['year']}) - question: "
                     f"{card.get('research_question', '')} | conclusion: {card.get('one_sentence', '')}")
    existing = registry.query("SELECT title FROM evidence_questions")
    if existing:
        lines += ["", "QUESTIONS THE USER ALREADY HAS (do not repeat):"] + [f"- {q['title']}" for q in existing]
    return "\n".join(lines), refs


def suggest(llm: LLMClient, config, registry: Registry, slug: str) -> list[dict]:
    """Ask the model for up to 3 cross-paper questions; store them as 'suggested'."""
    field = registry.field_by_slug(slug)
    if field is None:
        raise ValueError(f"unknown field {slug}")
    user, refs = _inputs(registry, field)
    if len(refs) < 2:
        raise ValueError("这个学科至少要有 2 篇论文才能提议研究问题")
    slugs = {r["slug"]: r["id"] for r in registry.query("SELECT id, slug FROM fields WHERE status='active'")}
    user += "\n\nAVAILABLE FIELD SLUGS: " + ", ".join(sorted(slugs))

    def validate(data: dict) -> list[str]:
        items = data.get("questions")
        if not isinstance(items, list):
            return ["'questions' must be a list"]
        clean = []
        for it in items[:MAX_SUGGESTIONS]:
            if not isinstance(it, dict) or not str(it.get("title", "")).strip():
                continue
            prefs = [x for x in (it.get("refs") or []) if x in refs]
            if len(set(prefs)) < 2:  # a question must connect at least two papers
                continue
            fs = [x for x in (it.get("fields") or []) if x in slugs]
            clean.append({"title": str(it["title"]).strip()[:300], "rationale": str(it.get("rationale", "")).strip(),
                          "refs": list(dict.fromkeys(prefs)), "fields": list(dict.fromkeys([slug, *fs]))})
        data["questions"] = clean
        return []

    rule = LANGUAGE_RULES.get(config.output_language, LANGUAGE_RULES["zh"])
    res = llm.chat_json(_load_prompt(PROMPT_VERSION).replace("{language_rule}", rule), user, validate,
                        purpose="questions", max_tokens=2048)
    existing = []  # current titles plus what the model first proposed (user may have rewritten it)
    for r in registry.query("SELECT title, source_json FROM evidence_questions"):
        existing.append(r["title"])
        proposed = json.loads(r["source_json"] or "{}").get("proposed_title")
        if proposed:
            existing.append(proposed)
    created = []
    for q in res.data["questions"]:
        if any(fuzz.ratio(q["title"], t) >= 85 for t in existing):
            continue
        source = {"field": slug, "paper_ids": [refs[x] for x in q["refs"]], "model": res.model,
                  "proposed_title": q["title"],
                  "prompt_version": PROMPT_VERSION}
        with registry._lock, registry.conn:  # noqa: SLF001
            cur = registry.conn.execute(
                "INSERT OR IGNORE INTO evidence_questions(title, created_at, status, origin, rationale, source_json) "
                "VALUES (?,?,'suggested','suggested',?,?)",
                (q["title"], now_iso(), q["rationale"], json.dumps(source, ensure_ascii=False)))
            if not cur.rowcount:
                continue
            qid = cur.lastrowid
            for s in q["fields"]:
                registry.conn.execute("INSERT OR IGNORE INTO evidence_question_fields VALUES (?,?)", (qid, slugs[s]))
        existing.append(q["title"])
        created.append({"id": qid, **q})
    return created


def paper_labels(registry: Registry, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = {r["id"]: r for r in registry.query(
        f"SELECT id, first_author, year, title FROM papers WHERE id IN ({marks})", tuple(ids))}
    return [{"id": i, "label": f"{rows[i]['first_author'] or '?'} {rows[i]['year'] or ''}".strip(),
             "title": rows[i]["title"] or ""} for i in ids if i in rows]

