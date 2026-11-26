"""收藏簿: sentences the user keeps from anywhere (cards, PDFs, AI answers, pages), and AI digests of them.

A digest groups the scattered clips into themes and sums up the knowledge points; every point cites
the clips it comes from ([C#] -> link), so nothing is added that the user did not collect. Digests
run on request, and once a week on their own when enough new clips have come in.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

from .config import Config
from .kb_writer import atomic_write
from .llm import LLMClient
from .registry import Registry, now_iso
from .i18n import prose_language

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    paper_id INTEGER,
    page INTEGER,
    source TEXT NOT NULL DEFAULT 'page',   -- card / pdf / chat / page
    url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clip_digests (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    clip_ids_json TEXT NOT NULL,
    kb_path TEXT,
    model TEXT, cost_usd REAL,
    created_at TEXT NOT NULL
);
"""
MAX_TEXT = 4000
AUTO_EVERY_DAYS = 7
AUTO_MIN_NEW = 5
MAX_PER_DIGEST = 200


def ensure_schema(registry: Registry) -> None:
    with registry._lock:  # noqa: SLF001
        registry.conn.executescript(SCHEMA)


def add(registry: Registry, text: str, paper_id: int | None = None, page: int | None = None,
        source: str = "page", url: str = "", note: str = "") -> int:
    ensure_schema(registry)
    text = re.sub(r"[ \t]+", " ", (text or "").strip())[:MAX_TEXT]
    if len(text) < 2:
        raise ValueError("请先选中要收藏的文字")
    if paper_id is not None and registry.get_paper(paper_id) is None:
        paper_id = None
    url = url if url.startswith("/") and not url.startswith("//") else ""
    dup = registry.one("SELECT id FROM clips WHERE text=? AND coalesce(paper_id, 0)=?", (text, paper_id or 0))
    if dup:
        return int(dup["id"])
    cur = registry.execute(
        "INSERT INTO clips(text, note, paper_id, page, source, url, created_at) VALUES (?,?,?,?,?,?,?)",
        (text, (note or "")[:2000], paper_id, page, source if source in ("card", "pdf", "chat", "page") else "page",
         url[:500], now_iso()))
    return int(cur.lastrowid)


def listing(registry: Registry, limit: int = 500) -> list[dict]:
    ensure_schema(registry)
    return [dict(r) for r in registry.query(
        "SELECT c.*, p.first_author, p.year, p.title FROM clips c LEFT JOIN papers p ON p.id=c.paper_id "
        "ORDER BY c.id DESC LIMIT ?", (limit,))]


def set_note(registry: Registry, clip_id: int, note: str) -> None:
    registry.execute("UPDATE clips SET note=? WHERE id=?", ((note or "")[:2000], clip_id))


def delete(registry: Registry, clip_id: int) -> None:
    registry.execute("DELETE FROM clips WHERE id=?", (clip_id,))


def last_digest(registry: Registry):
    ensure_schema(registry)
    return registry.one("SELECT * FROM clip_digests ORDER BY id DESC LIMIT 1")


def new_since_digest(registry: Registry) -> int:
    last = last_digest(registry)
    return registry.one("SELECT count(*) AS n FROM clips WHERE created_at > ?", (last["created_at"] if last else "",))["n"]


def digest(config: Config, registry: Registry, llm: LLMClient, scope: str = "all") -> dict:
    """Group clips into themes and sum them up. scope: all / new (since the last digest)."""
    ensure_schema(registry)
    last = last_digest(registry)
    since = last["created_at"] if (scope == "new" and last) else ""
    rows = registry.query(
        "SELECT c.*, p.first_author, p.year FROM clips c LEFT JOIN papers p ON p.id=c.paper_id "
        "WHERE c.created_at > ? ORDER BY c.id DESC LIMIT ?", (since, MAX_PER_DIGEST))
    if not rows:
        raise ValueError("收藏簿里还没有新的内容")
    lines = []
    for r in rows:
        who = f" — {r['first_author'] or '?'} {r['year'] or ''}" if r["paper_id"] else ""
        note = f" (my note: {r['note']})" if r["note"] else ""
        lines.append(f"[C{r['id']}]{who}: {r['text']}{note}")
    language = prose_language(config)
    system = "\n".join([
        "You organise a researcher's clippings notebook: sentences they saved while reading papers.",
        f"Write in {language}, as Markdown.",
        "Group the clippings into a few themes (## heading each). Under each theme, write the knowledge points as "
        "short bullets that combine related clippings, ending each bullet with the clipping ids it rests on, e.g. "
        "[C12][C40]. Point out agreements, contradictions and open questions between clippings when they exist.",
        "Use ONLY what the clippings say; do not add outside facts. Every clipping should appear at least once.",
        "Finish with a section '## 值得追问' (or '## Open questions' in English) listing 2-5 questions these clippings raise.",
    ])
    res = llm.chat_text(system, "CLIPPINGS:\n" + "\n".join(lines), purpose="clips_digest", max_tokens=6000)
    valid = {r["id"] for r in rows}
    text = re.sub(r"\[C(\d+)\]", lambda m: m.group(0) if int(m.group(1)) in valid else "", res.raw).strip()
    rel = f"clips/收藏整理-{date.today().isoformat()}.md"
    ids = sorted(valid)
    md_text = re.sub(r"\[C(\d+)\]", lambda m: f"[^c{m.group(1)}]", text)
    notes = "\n".join(f"[^c{r['id']}]: {r['text'][:300]}" + (f" — {r['first_author']} {r['year'] or ''}" if r["paper_id"] else "")
                      for r in rows)
    atomic_write(config.knowledge_dir / rel, f"# 收藏整理 · {date.today().isoformat()}\n\n{md_text}\n\n{notes}\n")
    cur = registry.execute("INSERT INTO clip_digests(text, clip_ids_json, kb_path, model, cost_usd, created_at) VALUES (?,?,?,?,?,?)",
                           (text, json.dumps(ids), rel, res.model, res.cost_usd, now_iso()))
    return {"id": int(cur.lastrowid), "text": text, "kb_path": rel}


def auto_enabled(config: Config) -> bool:
    return (config.raw.get("clips") or {}).get("auto_digest", True) is not False


def maybe_auto_digest(pipeline) -> dict | None:
    """Once a week, when at least AUTO_MIN_NEW clips came in since the last digest."""
    config, registry = pipeline.config, pipeline.registry
    ensure_schema(registry)
    if not auto_enabled(config) or not config.model_ready():
        return None
    last = last_digest(registry)
    if last and datetime.now(timezone.utc) - datetime.fromisoformat(last["created_at"]) < timedelta(days=AUTO_EVERY_DAYS):
        return None
    if new_since_digest(registry) < AUTO_MIN_NEW:
        return None
    out = digest(config, registry, pipeline.llm, "all")
    registry.log("info", "Clippings notebook organised (weekly)")
    return out


def digests(registry: Registry, limit: int = 20) -> list:
    ensure_schema(registry)
    return registry.query("SELECT id, kb_path, model, created_at FROM clip_digests ORDER BY id DESC LIMIT ?", (limit,))
