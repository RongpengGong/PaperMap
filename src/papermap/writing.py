"""Write a paragraph for the user's manuscript from their own library only.

Sources are the cards of the chosen papers (a purpose tag, a group, starred, or a hand-picked set).
Every claim carries [P#] markers that are checked against the sources and turned into author-year
or numbered citations with a reference list; markers pointing nowhere are dropped. The result can
be copied as Markdown, downloaded as .docx and .bib, and sent to the citation check.
"""
from __future__ import annotations

import json
import re

from .config import Config
from .llm import LLMClient
from .registry import Registry, now_iso

MAX_PAPERS = 40
SCHEMA = """
CREATE TABLE IF NOT EXISTS write_drafts (
    id INTEGER PRIMARY KEY,
    topic TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '',
    style TEXT NOT NULL DEFAULT 'author_year',
    language TEXT NOT NULL DEFAULT 'en',
    raw TEXT NOT NULL,                -- with [P#] markers
    sources_json TEXT NOT NULL,       -- [{id, paper_id}]
    model TEXT, cost_usd REAL,
    created_at TEXT NOT NULL
);
"""
LENGTHS = {"short": "one paragraph of about 120 words", "medium": "one or two paragraphs, about 250 words",
           "long": "three to four paragraphs, about 500 words"}
LANGS = {"en": "English (academic register)", "zh": "Simplified Chinese (keep gene, method and software names in English)",
         "de": "German (academic register)"}


def ensure_schema(registry: Registry) -> None:
    with registry._lock:  # noqa: SLF001
        registry.conn.executescript(SCHEMA)


def scope_papers(registry: Registry, scope: str, ids: list[int] | None = None) -> list:
    sql = "SELECT * FROM papers WHERE status='done'"
    params: list = []
    if ids:
        sql += f" AND id IN ({','.join('?' * len(ids))})"
        params += ids
    elif scope == "star":
        sql += " AND starred=1"
    elif scope.startswith("u:"):
        sql += " AND id IN (SELECT paper_id FROM paper_purposes WHERE tag=?)"
        params.append(scope[2:])
    elif scope.isdigit():
        sql += " AND id IN (SELECT paper_id FROM paper_collections WHERE collection_id=?)"
        params.append(int(scope))
    elif scope.startswith("f:"):
        sql += (" AND id IN (SELECT pf.paper_id FROM paper_fields pf JOIN fields f ON f.id=pf.field_id "
                "WHERE f.slug=? AND pf.status='accepted')")
        params.append(scope[2:])
    else:
        raise ValueError("请选择要用的论文范围")
    return registry.query(sql + " ORDER BY year DESC, id DESC LIMIT ?", (*params, MAX_PAPERS))


def _who(row) -> tuple[str, str]:
    """(first author family name, 'et al.' / '& Second' suffix) for author-year citations."""
    try:
        authors = json.loads(row["authors_json"] or "[]")
    except ValueError:
        authors = []
    fams = [a.get("family") or (a.get("name") or "").split(" ")[-1] for a in authors if isinstance(a, dict)]
    fams = [f for f in fams if f] or [row["first_author"] or "Anon"]
    if len(fams) == 1:
        return fams[0], ""
    if len(fams) == 2:
        return fams[0], f" & {fams[1]}"
    return fams[0], " et al."


def build_sources(rows) -> tuple[list[dict], str]:
    sources, blocks = [], []
    for i, r in enumerate(rows, 1):
        c = json.loads(r["card_json"] or "{}")
        d = c.get("design") or {}
        fam, rest = _who(r)
        sid = f"P{i}"
        sources.append({"id": sid, "paper_id": r["id"]})
        blocks.append(
            f"[{sid}] {fam}{rest} {r['year'] or ''} — {r['title']} ({r['journal'] or ''})\n"
            f"Conclusion: {c.get('one_sentence', '')}\nQuestion: {c.get('research_question', '')}\n"
            f"Design: {d.get('summary', '')} (n: {d.get('sample_size', '')}; species: {', '.join(d.get('species', []))}; "
            f"platforms: {', '.join(d.get('platforms', []))})\n"
            f"Findings: {' | '.join(c.get('key_findings', [])[:5])}\nLimitations: {' | '.join(c.get('limitations', [])[:3])}")
    return sources, "\n\n".join(blocks)


def write(config: Config, registry: Registry, llm: LLMClient, topic: str, scope: str = "", ids: list[int] | None = None,
          style: str = "author_year", language: str = "en", length: str = "medium") -> dict:
    ensure_schema(registry)
    topic = " ".join((topic or "").split())[:2000]
    if not topic:
        raise ValueError("请写下这段要讲什么")
    rows = scope_papers(registry, scope, ids)
    if not rows:
        raise ValueError("这个范围里还没有已生成卡片的论文")
    sources, context = build_sources(rows)
    system = "\n".join([
        "You draft academic prose for the user's own manuscript, using ONLY the numbered sources below "
        "(summaries of papers in the user's library).",
        f"Write {LENGTHS.get(length, LENGTHS['medium'])} in {LANGS.get(language, LANGS['en'])}.",
        "Every sentence that reports a finding, method, number or claim from the literature must end with one or "
        "more markers such as [P3] or [P1][P4], citing only sources that actually say it. Never cite anything else, "
        "never invent numbers, sample sizes or results. Linking sentences of your own may stay uncited.",
        "Synthesise (compare, contrast, show agreement and disagreement) rather than listing papers one by one. "
        "Prefer the sources most relevant to the topic; it is fine to leave sources unused.",
        "If the sources cannot support part of the requested point, end with one line: [缺少证据: …] naming what is missing.",
        "Output Markdown paragraphs only: no heading, no reference list.",
    ])
    res = llm.chat_text(system, f"TOPIC / INSTRUCTIONS: {topic}\n\nSOURCES:\n\n{context}", purpose="write", max_tokens=4000)
    valid = {s["id"] for s in sources}
    raw = re.sub(r"\[(P\d+)\]", lambda m: m.group(0) if m.group(1) in valid else "", res.raw).strip()
    cur = registry.execute(
        "INSERT INTO write_drafts(topic, scope, style, language, raw, sources_json, model, cost_usd, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (topic, scope or ("ids:" + ",".join(map(str, ids or []))), style, language, raw, json.dumps(sources),
         res.model, res.cost_usd, now_iso()))
    return get(registry, int(cur.lastrowid))


def render(registry: Registry, raw: str, sources: list[dict], style: str) -> dict:
    """[P#] markers -> citations; returns text, the cited paper ids in order and a reference list."""
    by_sid = {s["id"]: s["paper_id"] for s in sources}
    order: list[int] = []
    for sid in re.findall(r"\[(P\d+)\]", raw):
        pid = by_sid.get(sid)
        if pid and pid not in order:
            order.append(pid)
    rows = {pid: registry.get_paper(pid) for pid in order}
    rows = {k: v for k, v in rows.items() if v is not None}
    order = [p for p in order if p in rows]

    def cite(group: str) -> str:
        pids = []
        for sid in re.findall(r"P\d+", group):
            pid = by_sid.get(sid)
            if pid in rows and pid not in pids:
                pids.append(pid)
        if not pids:
            return ""
        if style == "numbered":
            nums = sorted(order.index(p) + 1 for p in pids)
            return "[" + ",".join(map(str, nums)) + "]"
        parts = []
        for p in pids:
            fam, rest = _who(rows[p])
            parts.append(f"{fam}{rest}, {rows[p]['year'] or 'n.d.'}")
        return "(" + "; ".join(parts) + ")"

    text = re.sub(r"(?:\s*\[P\d+\])+", lambda m: " " + cite(m.group(0)) if cite(m.group(0)) else "", raw)
    text = re.sub(r"\n?\[缺少证据[:：][^\]]*\]", "", text)  # shown separately on the page
    text = re.sub(r" +([.,;:])", r"\1", text).strip()
    refs = []
    listed = order if style == "numbered" else sorted(order, key=lambda p: (_who(rows[p])[0].lower(), rows[p]["year"] or 0))
    for i, p in enumerate(listed, 1):
        r = rows[p]
        try:
            authors = json.loads(r["authors_json"] or "[]")
        except ValueError:
            authors = []
        names = [f"{a.get('family', '')} {''.join(x[0] for x in (a.get('given') or '').split() if x)}".strip()
                 for a in authors[:3] if isinstance(a, dict)] or [r["first_author"] or "Anon"]
        au = ", ".join(names) + (", et al." if len(authors) > 3 else "")
        entry = f"{au} ({r['year'] or 'n.d.'}). {r['title']}. *{r['journal'] or ''}*." + (f" https://doi.org/{r['doi']}" if r["doi"] else "")
        refs.append((f"[{i}] " if style == "numbered" else "") + entry)
    return {"text": text, "cited": order, "refs": refs}


def get(registry: Registry, draft_id: int) -> dict:
    ensure_schema(registry)
    row = registry.one("SELECT * FROM write_drafts WHERE id=?", (draft_id,))
    if row is None:
        raise ValueError("找不到这段草稿")
    sources = json.loads(row["sources_json"])
    out = dict(row)
    out.update(render(registry, row["raw"], sources, row["style"]))
    out["sources"] = sources
    out["gaps"] = re.findall(r"\[缺少证据[:：]\s*([^\]]+)\]", row["raw"])
    return out


def history(registry: Registry, limit: int = 50) -> list:
    ensure_schema(registry)
    return registry.query("SELECT id, topic, created_at FROM write_drafts ORDER BY id DESC LIMIT ?", (limit,))


def markdown(d: dict) -> str:
    refs = "\n".join(f"- {r}" if not r.startswith("[") else r for r in d["refs"])
    return f"{d['text']}\n\n## References\n\n{refs}\n" if refs else d["text"] + "\n"


def to_docx(d: dict) -> bytes:
    from . import docx_out

    blocks = [("p", p.strip()) for p in d["text"].split("\n\n") if p.strip()]
    if d["refs"]:
        blocks.append(("h2", "References"))
        blocks += [("p", r) for r in d["refs"]]
    return docx_out.build(blocks)
