"""Structured paper extraction with the LLM (prompt + schema validation)."""

from __future__ import annotations

from importlib import resources

from .config import Config
from .llm import LLMClient, LLMResult

PROMPT_VERSION = "extract_v1"

LANGUAGE_RULES = {
    "zh": "Write all string values in Simplified Chinese; keep gene names, software, "
    "platform and technical terms in English.",
    "en": "Write all string values in English.",
    "bilingual": "Write all string values in Simplified Chinese, adding the English "
    "term in parentheses for key concepts; keep gene names and software in English.",
}

_STR_LIST_KEYS = ("methods", "tools", "key_findings", "limitations")
_DESIGN_LIST_KEYS = ("species", "tissues", "platforms", "cohorts")


def load_prompt(version: str = PROMPT_VERSION) -> str:
    text = resources.files("papermap.prompts").joinpath(f"{version}.md").read_text("utf-8")
    # drop the leading version comment
    return "\n".join(ln for ln in text.splitlines() if not ln.startswith("<!--")).strip()


def validate(data: dict) -> list[str]:
    """Return a list of schema problems (empty = valid). Coerces light issues."""
    problems: list[str] = []
    for key in ("one_sentence", "research_question"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            problems.append(f"'{key}' must be a non-empty string")
    design = data.get("design")
    if not isinstance(design, dict):
        problems.append("'design' must be an object")
    else:
        design.setdefault("summary", "")
        design.setdefault("sample_size", "")
        for k in ("summary", "sample_size"):
            if not isinstance(design[k], str):
                design[k] = str(design[k])
        for k in _DESIGN_LIST_KEYS:
            design[k] = _coerce_list(design.get(k))
    for key in _STR_LIST_KEYS:
        if key not in data:
            problems.append(f"missing key '{key}'")
            continue
        data[key] = _coerce_list(data[key])
    if isinstance(data.get("key_findings"), list) and not data["key_findings"]:
        problems.append("'key_findings' must contain at least one item")
    return problems


def _coerce_list(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


def build_user_message(meta: dict, paper_text: str) -> str:
    authors = ", ".join(a.get("name", "") for a in (meta.get("authors") or [])[:8])
    lines = [
        f"Title: {meta.get('title', '')}",
        f"Journal: {meta.get('journal', '')} ({meta.get('year', '')})",
        f"Authors: {authors}",
        f"DOI: {meta.get('doi') or 'none'}",
    ]
    if meta.get("abstract"):
        lines.append(f"Abstract (from Crossref/OpenAlex): {meta['abstract']}")
    lines.append("\n=== PAPER TEXT (key sections, references removed) ===\n")
    lines.append(paper_text)
    return "\n".join(lines)


def extract_paper(
    llm: LLMClient, config: Config, meta: dict, paper_text: str, paper_id: int | None
) -> LLMResult:
    rule = LANGUAGE_RULES.get(config.output_language, LANGUAGE_RULES["zh"])
    system = load_prompt().replace("{language_rule}", rule)
    user = build_user_message(meta, paper_text)
    return llm.chat_json(system, user, validate, purpose="extract", paper_id=paper_id)
