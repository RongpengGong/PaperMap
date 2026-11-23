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

PROMPT_VERSION = "consolidate_v1"
MAX_PAPERS = 40


def latest(registry: Registry, field_id: int):
    return registry.one("SELECT * FROM field_syntheses WHERE field_id=? ORDER BY id DESC LIMIT 1", (field_id,))


def due(registry: Registry, field_id: int, every: int) -> bool:
    n = len(registry.field_paper_rows(field_id))
    last = latest(registry, field_id)
    return n >= 2 and n - (last["n_papers"] if last else 0) >= every


def refs_for(registry: Registry, field_id: int) -> list[dict]:
    rows = registry.field_paper_rows(field_id)[:MAX_PAPERS]
    return [{"ref": f"P{i}", "paper_id": r["paper_id"], "doi": r["doi"], "year": r["year"],
             "first_author": r["first_author"], "journal": r["journal"], "title": r["title"],
             "kb_path": r["kb_path"], "note": r["note"], "card": json.loads(r["card_json"] or "{}")}
            for i, r in enumerate(rows, 1)]


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
    registry.execute(
        "INSERT INTO field_syntheses(field_id, n_papers, data_json, model, prompt_version, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (field["id"], len(registry.field_paper_rows(field["id"])), json.dumps(stored, ensure_ascii=False),
         res.model, PROMPT_VERSION, now_iso()),
    )
    return stored
