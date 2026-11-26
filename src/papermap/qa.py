"""Ask-your-library: retrieval over the full-text index + cited answer (CLAUDE.md §15.2)."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from . import search as srch
from .config import Config
from .extract import LANGUAGE_RULES
from .extract import load_prompt as _load_prompt
from .kb_writer import atomic_write
from .llm import LLMClient
from .registry import Registry, now_iso

PROMPT_VERSION = "qa_v1"
MAX_PAPERS = 12
PASSAGES_PER_PAPER = 3
PASSAGE_CHARS = 700

SCHEMA = """
CREATE TABLE IF NOT EXISTS qa_history (
    id INTEGER PRIMARY KEY,
    question TEXT NOT NULL,
    scope TEXT,
    answer TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    model TEXT, cost_usd REAL,
    kb_path TEXT,
    created_at TEXT NOT NULL
);
"""


def ensure_schema(registry: Registry) -> None:
    with registry._lock:  # noqa: SLF001
        registry.conn.executescript(SCHEMA)
        registry.conn.commit()


def keywords(llm: LLMClient, question: str) -> list[str]:
    """Search terms in English and Chinese (one small call)."""
    def validate(d: dict) -> list[str]:
        d["keywords"] = [str(k).strip() for k in d.get("keywords", []) if str(k).strip()][:16]
        return [] if d["keywords"] else ["'keywords' must be a non-empty list"]

    system = ("Turn the researcher's question into search keywords for a full-text index of biomedical papers. "
              "Give 4-12 short terms: key entities (genes, cell types, diseases, methods, platforms, tools) in English, "
              "plus Chinese equivalents when the question is Chinese. No sentences. "
              'Return JSON: {"keywords": ["...", "..."]}')
    res = llm.chat_json(system, question, validate, purpose="qa", max_tokens=400)
    return res.data["keywords"]


def scope_paper_ids(registry: Registry, scope: str) -> list[int] | None:
    """'' = whole library; 'c:<id>' a group (with subgroups); 'f:<slug>' a field;
    'star' starred papers; 'u:<tag>' a purpose tag."""
    if scope == "star":
        return [r["id"] for r in registry.query("SELECT id FROM papers WHERE starred=1")] or [-1]
    if scope.startswith("u:"):
        from . import purposes

        purposes.ensure_schema(registry)
        return [r["paper_id"] for r in registry.query("SELECT paper_id FROM paper_purposes WHERE tag=?", (scope[2:],))] or [-1]
    if scope.startswith("c:") and scope[2:].isdigit():
        ids = registry.collection_descendants(int(scope[2:]))
        rows = registry.query(f"SELECT DISTINCT paper_id FROM paper_collections WHERE collection_id IN ({','.join('?' * len(ids))})", tuple(ids))
        return [r["paper_id"] for r in rows] or [-1]
    if scope.startswith("f:"):
        f = registry.field_by_slug(scope[2:])
        if f is None:
            return [-1]
        return [r["paper_id"] for r in registry.field_paper_rows(f["id"])] or [-1]
    return None


def retrieve(registry: Registry, terms: list[str], paper_ids: list[int] | None) -> list[dict]:
    """Rank papers by hits and collect their card summary + best passages."""
    hits = srch.search(registry, "", paper_ids=paper_ids, limit=2000, any_term=True, terms=terms)
    score: dict[int, float] = {}
    for h in hits:
        score[h["paper_id"]] = score.get(h["paper_id"], 0) + {"card": 3, "notes": 3, "annotation": 2}.get(h["kind"], 1)
    if not score and paper_ids:  # nothing matched inside a small scope: use the whole scope
        score = {pid: 0 for pid in paper_ids if pid > 0}
    ranked = sorted(score, key=lambda k: -score[k])[:MAX_PAPERS]
    out = []
    for pid in ranked:
        p = registry.get_paper(pid)
        if p is None or p["status"] != "done":
            continue
        card = json.loads(p["card_json"] or "{}")
        passages = [h for h in hits if h["paper_id"] == pid and h["kind"] in ("page", "notes", "annotation")]
        passages.sort(key=lambda h: -sum(h["body"].lower().count(t.lower()) for t in terms))
        out.append({"paper": p, "card": card, "passages": [
            {"kind": h["kind"], "page": h["page"], "text": srch.snippet(h["body"], terms, PASSAGE_CHARS // 3)}
            for h in passages[:PASSAGES_PER_PAPER]]})
    return out


def build_sources(items: list[dict]) -> tuple[list[dict], str]:
    """Number sources S1..Sn (a card summary and each passage are separate sources)."""
    sources, blocks = [], []
    for it in items:
        p, c = it["paper"], it["card"]
        who = f"{p['first_author'] or '?'} {p['year'] or ''}".strip()
        sid = f"S{len(sources) + 1}"
        design = (c.get("design") or {})
        summary = (f"Conclusion: {c.get('one_sentence', '')}\nDesign: {design.get('summary', '')} "
                   f"(n: {design.get('sample_size', '')}; platforms: {', '.join(design.get('platforms', []))})\n"
                   f"Methods: {'; '.join(c.get('methods', [])[:6])}\nFindings: {' | '.join(c.get('key_findings', [])[:5])}\n"
                   f"Limitations: {' | '.join(c.get('limitations', [])[:3])}")
        sources.append({"id": sid, "paper_id": p["id"], "who": who, "doi": p["doi"], "title": p["title"],
                        "kind": "card", "page": None})
        blocks.append(f"[{sid}] {who} — {p['title']} ({p['journal'] or ''}) — paper summary\n{summary}")
        for ps in it["passages"]:
            sid = f"S{len(sources) + 1}"
            label = {"page": f"text p.{(ps['page'] or 0) + 1}", "notes": "the user's own notes",
                     "annotation": f"the user's annotation p.{(ps['page'] or 0) + 1}"}[ps["kind"]]
            sources.append({"id": sid, "paper_id": p["id"], "who": who, "doi": p["doi"], "title": p["title"],
                            "kind": ps["kind"], "page": ps["page"]})
            blocks.append(f"[{sid}] {who} — {label}\n{ps['text']}")
    return sources, "\n\n".join(blocks)


def clean_answer(answer: str, valid: set[str]) -> tuple[str, list[str]]:
    """Drop citation markers that point to no source; return (answer, cited ids in order)."""
    cited: list[str] = []

    def repl(m: re.Match) -> str:
        sid = m.group(1)
        if sid in valid:
            if sid not in cited:
                cited.append(sid)
            return f"[{sid}]"
        return ""

    return re.sub(r"\[(S\d+)\]", repl, answer).strip(), cited


def ask(config: Config, registry: Registry, llm: LLMClient, question: str, scope: str = "") -> dict:
    ensure_schema(registry)
    question = question.strip()
    if not question:
        raise ValueError("问题是空的")
    terms = keywords(llm, question)
    items = retrieve(registry, terms, scope_paper_ids(registry, scope))
    if not items:
        answer, sources, cited, model, cost = "在你的文献库里没有找到和这个问题相关的内容。", [], [], "", 0.0
    else:
        sources, context = build_sources(items)
        rule = LANGUAGE_RULES.get(config.content_language, LANGUAGE_RULES["zh"]).replace("all string values", "the answer")
        system = _load_prompt(PROMPT_VERSION).replace("{language_rule}", rule)
        res = llm.chat_text(system, f"QUESTION: {question}\n\nSOURCES:\n\n{context}", purpose="qa", max_tokens=4000)
        answer, cited = clean_answer(res.raw, {s["id"] for s in sources})
        model, cost = res.model, res.cost_usd
        if not cited:
            answer = "⚠️ 模型的回答没有引用任何来源，已隐藏。可以换个问法，或把问题范围限定到某个分组/学科。"
    result = {"question": question, "scope": scope, "answer": answer, "keywords": terms,
              "sources": [s for s in sources if s["id"] in cited] if sources else [], "model": model, "cost": cost}
    cur = registry.execute(
        "INSERT INTO qa_history(question, scope, answer, sources_json, model, cost_usd, created_at) VALUES (?,?,?,?,?,?,?)",
        (question, scope, answer, json.dumps(result["sources"], ensure_ascii=False), model, cost, now_iso()))
    result["id"] = int(cur.lastrowid)
    result["kb_path"] = write_md(config.knowledge_dir, registry, result)
    return result


def write_md(knowledge_dir: Path, registry: Registry, r: dict) -> str:
    slug = re.sub(r"[^\w一-鿿]+", "-", r["question"])[:40].strip("-") or "question"
    rel = f"qa/{datetime.now().strftime('%Y%m%d-%H%M')}-{slug}.md"
    links = {}
    for s in r["sources"]:
        p = registry.get_paper(s["paper_id"])
        target = (p["kb_path"] or "").removesuffix(".md") if p else ""
        where = f" p.{s['page'] + 1}" if s["page"] is not None else ""
        links[s["id"]] = f"[[{target}|{s['who']}{where}]]" if target else f"{s['who']}{where}"
    body = re.sub(r"\[(S\d+)\]", lambda m: f"（{links.get(m.group(1), m.group(1))}）", r["answer"])
    text = (f"---\nquestion: {json.dumps(r['question'], ensure_ascii=False)}\nmodel: {r['model']}\n"
            f"created: {datetime.now().isoformat(timespec='minutes')}\n---\n\n# {r['question']}\n\n{body}\n")
    atomic_write(knowledge_dir / rel, text)
    registry.execute("UPDATE qa_history SET kb_path=? WHERE id=?", (rel, r["id"]))
    return rel
