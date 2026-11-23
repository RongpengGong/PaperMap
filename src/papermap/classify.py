"""Multi-label classification of papers into the taxonomy (method + topic axes)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from .config import Config
from .extract import LANGUAGE_RULES
from .extract import load_prompt as _load_prompt
from .llm import LLMClient
from .registry import Registry, now_iso
from .taxonomy import Taxonomy

log = logging.getLogger(__name__)

PROMPT_VERSION = "classify_v1"


def classify_key(taxonomy: Taxonomy) -> str:
    """Identifies the prompt + taxonomy a classification was made with."""
    return f"{PROMPT_VERSION}:{taxonomy.fingerprint}"


def render_taxonomy(taxonomy: Taxonomy) -> str:
    out = []
    for axis in ("method", "topic"):
        out.append(f"{axis.upper()} categories (slug | name | description | synonyms):")
        for c in taxonomy.axis(axis):
            syn = ", ".join(c.synonyms)
            out.append(f"- {c.slug} | {c.name_en} / {c.name_zh} | {c.description} | {syn}")
        out.append("")
    return "\n".join(out).strip()


def build_system(taxonomy: Taxonomy, language: str) -> str:
    rule = LANGUAGE_RULES.get(language, LANGUAGE_RULES["zh"]).replace(
        "all string values", "the 'note', 'reason', 'description' and 'name_zh' values"
    )
    return (
        _load_prompt(PROMPT_VERSION)
        .replace("{language_rule}", rule)
        .replace("{taxonomy}", render_taxonomy(taxonomy))
    )


def build_user(paper: dict, meta: dict, card: dict) -> str:
    lines = [
        f"Title: {paper.get('title') or meta.get('title', '')}",
        f"Journal: {meta.get('journal', '')} ({meta.get('year', '')})",
    ]
    if meta.get("abstract"):
        lines.append(f"Abstract: {meta['abstract'][:4000]}")
    if meta.get("topics") or meta.get("keywords"):
        lines.append(f"OpenAlex topics (prior): {', '.join(meta.get('topics', [])[:8])}")
        lines.append(f"OpenAlex keywords (prior): {', '.join(meta.get('keywords', [])[:12])}")
    if card:
        design = card.get("design") or {}
        lines += [
            "",
            "Structured summary of the full text:",
            f"- Main conclusion: {card.get('one_sentence', '')}",
            f"- Research question: {card.get('research_question', '')}",
            f"- Design: {design.get('summary', '')}",
            f"- Sample size: {design.get('sample_size', '')}",
            f"- Species / tissues: {', '.join(design.get('species', []))} / {', '.join(design.get('tissues', []))}",
            f"- Platforms: {', '.join(design.get('platforms', []))}",
            f"- Methods: {'; '.join(card.get('methods', []))}",
            f"- Tools: {', '.join(card.get('tools', []))}",
            "- Key findings: " + " | ".join(card.get("key_findings", [])),
        ]
    return "\n".join(lines)


def make_validator(taxonomy: Taxonomy):
    methods = {c.slug for c in taxonomy.axis("method")}
    topics = {c.slug for c in taxonomy.axis("topic")}

    def validate(data: dict) -> list[str]:
        problems: list[str] = []
        for key, allowed in (("methods", methods), ("topics", topics)):
            items = data.get(key)
            if items is None:
                data[key] = []
                continue
            if not isinstance(items, list):
                problems.append(f"'{key}' must be a list")
                continue
            clean, seen = [], set()
            for it in items:
                if not isinstance(it, dict):
                    problems.append(f"items of '{key}' must be objects")
                    continue
                slug = str(it.get("slug", "")).strip()
                if slug not in allowed:
                    problems.append(f"unknown {key[:-1]} slug '{slug}' (allowed: {', '.join(sorted(allowed))})")
                    continue
                if slug in seen:
                    continue
                seen.add(slug)
                try:
                    conf = float(it.get("confidence", 0))
                except (TypeError, ValueError):
                    conf = 0.0
                it["slug"], it["confidence"] = slug, min(1.0, max(0.0, conf))
                it["primary"] = bool(it.get("primary")) if key == "methods" else False
                it["note"] = str(it.get("note") or "").strip()
                it["reason"] = str(it.get("reason") or "").strip()
                clean.append(it)
            data[key] = clean
        if not data.get("methods") and not problems:
            problems.append("at least one method label is required")
        # exactly one primary: keep the most confident flagged one, else the most confident overall
        ms = data.get("methods") or []
        if ms:
            flagged = [m for m in ms if m["primary"]] or ms
            best = max(flagged, key=lambda m: m["confidence"])
            for m in ms:
                m["primary"] = m is best
        props = data.get("proposed_new") or []
        data["proposed_new"] = [
            {k: str(p.get(k) or "").strip() for k in ("axis", "name_en", "name_zh", "description", "reason")}
            for p in props if isinstance(p, dict) and (p.get("name_en") or p.get("name_zh"))
        ]
        for p in data["proposed_new"]:
            if p["axis"] not in ("method", "topic"):
                p["axis"] = "topic"
        return problems

    return validate


@dataclass
class Classification:
    data: dict
    cost_usd: float


def classify(llm: LLMClient, config: Config, taxonomy: Taxonomy, paper: dict, meta: dict,
             card: dict, paper_id: int | None) -> Classification:
    res = llm.chat_json(
        build_system(taxonomy, config.output_language), build_user(paper, meta, card),
        make_validator(taxonomy), purpose="classify", paper_id=paper_id, max_tokens=2048,
    )
    return Classification(res.data, res.cost_usd)


def apply(registry: Registry, config: Config, taxonomy: Taxonomy, paper_id: int, data: dict) -> dict:
    """Store labels. User decisions (source='user') are never overwritten.

    Returns {"accepted": [...], "suggested": [...], "needs_review": bool}.
    """
    accept = float(config.classification.get("accept_confidence", 0.6))
    review = float(config.classification.get("review_confidence", 0.35))
    user_rows = {r["slug"]: r for r in registry.paper_field_rows(paper_id, include_rejected=True)
                 if r["source"] == "user"}
    # Drop previous LLM labels; they are fully replaced by this run.
    registry.execute("DELETE FROM paper_fields WHERE paper_id=? AND source='llm'", (paper_id,))
    accepted, suggested = [], []
    for item in data.get("methods", []) + data.get("topics", []):
        if item["slug"] in user_rows or item["confidence"] < review:
            continue
        field = registry.field_by_slug(item["slug"])
        if field is None:
            continue
        status = "accepted" if item["confidence"] >= accept else "suggested"
        registry.set_paper_field(
            paper_id, field["id"], confidence=item["confidence"], is_primary=int(item.get("primary", False)),
            status=status, source="llm", note=item.get("note", ""), reason=item.get("reason", ""),
        )
        (accepted if status == "accepted" else suggested).append(item["slug"])
    registry.execute("DELETE FROM field_proposals WHERE paper_id=? AND status='open'", (paper_id,))
    for p in data.get("proposed_new", []):
        registry.add_proposal(paper_id, p["axis"], p["name_zh"], p["name_en"], p["description"], p["reason"])
    has_method = any(
        r["axis"] == "method" and r["status"] == "accepted" for r in registry.paper_field_rows(paper_id)
    )
    needs_review = bool(suggested) or not has_method
    registry.update_paper(paper_id, classify_key=classify_key(taxonomy), classified_at=now_iso(),
                          needs_review=int(needs_review))
    return {"accepted": accepted, "suggested": suggested, "needs_review": needs_review}


def recompute_review_flag(registry: Registry, paper_id: int) -> None:
    rows = registry.paper_field_rows(paper_id)
    has_method = any(r["axis"] == "method" and r["status"] == "accepted" for r in rows)
    pending = any(r["status"] == "suggested" for r in rows)
    registry.update_paper(paper_id, needs_review=int(pending or not has_method))


def paper_inputs(row) -> tuple[dict, dict, dict]:
    meta = json.loads(row["meta_json"] or "{}")
    card = json.loads(row["card_json"] or "{}")
    return {"title": row["title"]}, meta, card
