"""Periodic consolidation of field hub pages ("整合").

Every ``consolidate_every`` new papers in a field, the LLM rewrites the
field's overview, progress summary and disagreements - grounded in the
library and with per-item citations. Each version is stored in the database
(field_syntheses), so older versions stay available even without git.
"""

from __future__ import annotations

import json

from .config import Config
from .extract import LANGUAGE_RULES
from .extract import load_prompt as _load_prompt
from .llm import LLMClient, replace_ref_ids
from .registry import Registry, now_iso

PROMPT_VERSION = "consolidate_v2"
MAX_PAPERS = 40
# Fixed places for what the user confirmed, so a large field cannot crowd it out (CLAUDE.md §16.2 task 4).
CONTRARY_SLOTS = 6   # user-confirmed contrary evidence
KEY_SLOTS = 8        # other user-confirmed evidence
REASONS = {"contrary": "相反证据", "key": "关键证据", "added": "最近入库", "published": "最近发表"}
_REASON_EN = {"contrary": "user-confirmed CONTRARY evidence", "key": "user-confirmed key evidence",
              "added": "recently added to the library", "published": "recently published"}


def latest(registry: Registry, field_id: int):
    return registry.one("SELECT * FROM field_syntheses WHERE field_id=? ORDER BY id DESC LIMIT 1", (field_id,))


def due(registry: Registry, field_id: int, every: int) -> bool:
    n = len(registry.field_paper_rows(field_id))
    last = latest(registry, field_id)
    return n >= 2 and n - (last["n_papers"] if last else 0) >= every


def confirmed_judgments(registry: Registry, paper_ids: list[int]) -> dict[int, list[dict]]:
    """What the user confirmed about these papers: decided update cards and evidence quotes
    whose stance they set. A star is *not* evidence and is ignored here."""
    out: dict[int, list[dict]] = {}
    if not paper_ids:
        return out
    marks = ",".join("?" * len(paper_ids))
    for r in registry.query(
            "SELECT u.paper_id, q.title AS question, COALESCE(u.user_outcome, u.outcome) AS outcome, "
            "COALESCE(u.user_text, u.headline) AS text, u.decided_at AS at FROM update_cards u "
            "JOIN evidence_questions q ON q.id=u.question_id "
            f"WHERE u.status IN ('confirmed','rewritten') AND u.paper_id IN ({marks}) "
            "AND COALESCE(u.user_outcome, u.outcome)!='unrelated'", tuple(paper_ids)):
        out.setdefault(r["paper_id"], []).append({**dict(r), "contrary": r["outcome"] == "contradicts"})
    for r in registry.query(
            "SELECT e.paper_id, q.title AS question, e.stance, e.quote, e.created_at AS at FROM evidence_entries e "
            f"JOIN evidence_questions q ON q.id=e.question_id WHERE e.stance IN ('supports','opposes') "
            f"AND e.paper_id IN ({marks})", tuple(paper_ids)):
        out.setdefault(r["paper_id"], []).append({"question": r["question"], "outcome": r["stance"],
                                                  "text": "quote: " + r["quote"][:300], "at": r["at"],
                                                  "contrary": r["stance"] == "opposes"})
    for items in out.values():
        items.sort(key=lambda x: x["at"] or "", reverse=True)
    return out


def select(registry: Registry, field_id: int, limit: int = MAX_PAPERS) -> list[tuple[object, str]]:
    """Pick at most `limit` papers: confirmed contrary evidence and key evidence first (fixed slots),
    then alternately the most recently added and the most recently published."""
    rows = registry.field_paper_rows(field_id)
    by_id = {r["paper_id"]: r for r in rows}
    judged = confirmed_judgments(registry, list(by_id))
    latest = lambda pid: judged[pid][0]["at"] or ""  # noqa: E731
    contrary = sorted((p for p, j in judged.items() if any(x["contrary"] for x in j)), key=latest, reverse=True)
    key = sorted((p for p in judged if p not in contrary), key=latest, reverse=True)
    added_ids = [r["id"] for r in registry.query(
        "SELECT p.id FROM papers p JOIN paper_fields pf ON pf.paper_id=p.id WHERE pf.field_id=? "
        "AND pf.status='accepted' AND p.status='done' ORDER BY p.created_at DESC, p.id DESC", (field_id,))]
    published_ids = [r["paper_id"] for r in rows]  # year DESC
    chosen: list[tuple[int, str]] = []
    seen: set[int] = set()

    def take(pid: int, why: str) -> None:
        if pid not in seen and pid in by_id and len(chosen) < limit:
            seen.add(pid)
            chosen.append((pid, why))

    for pid in contrary[:CONTRARY_SLOTS]:
        take(pid, "contrary")
    for pid in key[:KEY_SLOTS]:
        take(pid, "key")
    ia = iter(added_ids)
    ip = iter(published_ids)
    while len(chosen) < min(limit, len(rows)):
        before = len(chosen)
        for it, why in ((ia, "added"), (ip, "published")):
            for pid in it:
                if pid not in seen:
                    take(pid, why)
                    break
        if len(chosen) == before:  # both lists exhausted
            break
    return [(by_id[pid], why) for pid, why in chosen]


def refs_for(registry: Registry, field_id: int) -> list[dict]:
    picked = select(registry, field_id)
    judged = confirmed_judgments(registry, [r["paper_id"] for r, _ in picked])
    return [{"ref": f"P{i}", "paper_id": r["paper_id"], "doi": r["doi"], "year": r["year"],
             "first_author": r["first_author"], "journal": r["journal"], "title": r["title"],
             "kb_path": r["kb_path"], "note": r["note"], "card": json.loads(r["card_json"] or "{}"),
             "why": why, "confirmed": judged.get(r["paper_id"], [])[:3]}
            for i, (r, why) in enumerate(picked, 1)]


def build_user(field, refs: list[dict]) -> str:
    lines = [f"FIELD: {field['name_en']} / {field['name_zh']} ({field['axis']})",
             f"Definition: {field['description'] or ''}", "", "PAPERS:"]
    for r in refs:
        c, d = r["card"], r["card"].get("design") or {}
        lines += [
            f"[{r['ref']}] {r['title']} ({r['first_author']} {r['year']}, {r['journal']})",
            f"  Conclusion: {c.get('one_sentence', '')}",
            f"  Design: {d.get('summary', '')} | n: {d.get('sample_size', '')} | platforms: {', '.join(d.get('platforms', []))}",
            f"  Contribution to this field: {r['note'] or '-'}",
            "  Findings: " + " | ".join(c.get("key_findings", [])[:5]),
            "  Limitations: " + " | ".join(c.get("limitations", [])[:3]),
        ]
        if r.get("why"):
            lines.append(f"  Included as: {_REASON_EN[r['why']]}")
        for j in r.get("confirmed", []):
            lines.append(f"  User-confirmed for question \"{j['question']}\": {j['outcome']} - {j['text']}")
    return "\n".join(lines)


def make_validator(valid: set[str]):
    def validate(data: dict) -> list[str]:
        for key, min_refs in (("overview", 1), ("progress", 1), ("disputes", 2)):
            items = data.get(key) if isinstance(data.get(key), list) else []
            clean = []
            for it in items:
                if not isinstance(it, dict) or not str(it.get("text", "")).strip():
                    continue
                refs = list(dict.fromkeys(x for x in (it.get("refs") or []) if isinstance(x, str) and x in valid))
                if len(refs) >= min_refs:
                    clean.append({"text": str(it["text"]).strip(), "refs": refs})
            data[key] = clean
        return [] if data["overview"] else [f"'overview' needs items citing valid ids ({', '.join(sorted(valid))})"]

    return validate


def run(llm: LLMClient, config: Config, registry: Registry, slug: str) -> dict:
    field = registry.field_by_slug(slug)
    if field is None:
        raise ValueError(f"unknown field {slug}")
    refs = refs_for(registry, field["id"])
    if len(refs) < 2:
        raise ValueError(f"field '{slug}' needs at least 2 papers to consolidate")
    rule = LANGUAGE_RULES.get(config.output_language, LANGUAGE_RULES["zh"])
    res = llm.chat_json(_load_prompt(PROMPT_VERSION).replace("{language_rule}", rule), build_user(field, refs),
                        make_validator({r["ref"] for r in refs}), purpose="consolidate", max_tokens=6144)
    # store refs as paper ids so later renders survive renumbering
    by_ref = {r["ref"]: r["paper_id"] for r in refs}
    labels = {r["ref"]: f"{r['first_author'] or '?'} {r['year'] or ''}".strip() for r in refs}
    stored = {k: [{"text": replace_ref_ids(it["text"], labels), "paper_ids": [by_ref[x] for x in it["refs"]]}
                  for it in res.data[k]]
              for k in ("overview", "progress", "disputes")}
    counts: dict[str, int] = {}
    for r in refs:
        counts[r["why"]] = counts.get(r["why"], 0) + 1
    stored["selection"] = {"used": len(refs), "total": len(registry.field_paper_rows(field["id"])), "counts": counts}
    registry.execute(
        "INSERT INTO field_syntheses(field_id, n_papers, data_json, model, prompt_version, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (field["id"], len(registry.field_paper_rows(field["id"])), json.dumps(stored, ensure_ascii=False),
         res.model, PROMPT_VERSION, now_iso()),
    )
    return stored
