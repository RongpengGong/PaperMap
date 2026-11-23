"""Study handbooks for fields (handbooks/<slug>.md).

Part A is grounded in the user's library: every item must cite papers that
were given to the model, otherwise it is dropped. Part B is general
background, clearly labelled as LLM-generated and to be verified.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import yaml

from .config import Config
from .extract import LANGUAGE_RULES
from .extract import load_prompt as _load_prompt
from .kb_writer import USER_BEGIN, USER_END, atomic_write, read_user_section
from .llm import LLMClient, replace_ref_ids
from .registry import Registry

PROMPT_VERSION = "handbook_v1"
MAX_PAPERS = 30

H = {
    "zh": {
        "a": "A. 来自你的文献库", "b": "B. 背景知识补充（LLM 生成，需核实）",
        "a_note": "本部分每条都引用了你库中的论文（DOI 链接）；没有出处的内容不会写在这里。",
        "b_note": "⚠️ 本部分由模型根据通用知识生成，没有逐条出处，可能有错误或过时之处，使用前请核实。",
        "overview": "概述", "rep": "代表研究", "methods": "用到的方法与工具", "findings": "主要发现",
        "concepts": "核心概念", "workflow": "标准分析流程", "tools": "常用工具包", "pitfalls": "常见陷阱",
        "reading": "建议阅读顺序", "notes": "我的笔记",
        "notes_hint": "（在这里写你自己的笔记；重新生成手册时这一块不会被改动。）",
        "papers": "本手册依据的论文", "field": "学科页",
    },
    "en": {
        "a": "A. From your library", "b": "B. Background (LLM-generated, verify)",
        "a_note": "Every item cites papers in your library (DOI links); nothing without a source is written here.",
        "b_note": "⚠️ Generated from the model's general knowledge without per-item sources; may be wrong or outdated. Verify before relying on it.",
        "overview": "Overview", "rep": "Representative studies", "methods": "Methods & tools used",
        "findings": "Main findings", "concepts": "Core concepts", "workflow": "Standard workflow",
        "tools": "Common tools", "pitfalls": "Common pitfalls", "reading": "Suggested reading order",
        "notes": "My notes", "notes_hint": "(Your notes; kept when the handbook is regenerated.)",
        "papers": "Papers this handbook is based on", "field": "Field page",
    },
}


def paper_refs(registry: Registry, field_id: int) -> list[dict]:
    rows = registry.field_paper_rows(field_id)[:MAX_PAPERS]
    refs = []
    for i, r in enumerate(rows, 1):
        card = json.loads(r["card_json"] or "{}")
        refs.append({
            "ref": f"P{i}", "paper_id": r["paper_id"], "doi": r["doi"], "title": r["title"],
            "year": r["year"], "journal": r["journal"], "first_author": r["first_author"],
            "kb_path": r["kb_path"], "card": card,
        })
    return refs


def build_user(field, refs: list[dict]) -> str:
    lines = [f"FIELD: {field['name_en']} / {field['name_zh']}",
             f"Definition: {field['description'] or ''}", "", "PAPERS IN THE LIBRARY:"]
    for r in refs:
        c = r["card"]
        d = c.get("design") or {}
        lines += [
            f"[{r['ref']}] {r['title']} ({r['first_author']} {r['year']}, {r['journal']}; DOI {r['doi'] or 'none'})",
            f"  Conclusion: {c.get('one_sentence', '')}",
            f"  Design: {d.get('summary', '')} | n: {d.get('sample_size', '')} | platforms: {', '.join(d.get('platforms', []))}",
            f"  Methods: {'; '.join(c.get('methods', [])[:8])}",
            f"  Tools: {', '.join(c.get('tools', [])[:20])}",
            "  Findings: " + " | ".join(c.get("key_findings", [])[:5]),
        ]
    return "\n".join(lines)


def make_validator(valid_refs: set[str]):
    def validate(data: dict) -> list[str]:
        problems = []
        if not isinstance(data.get("overview"), str):
            data["overview"] = ""
        a = data.get("A")
        if not isinstance(a, dict):
            return ["'A' must be an object"]
        kept = 0
        for key in ("representative", "methods", "findings"):
            items = a.get(key) or []
            clean = []
            for it in items if isinstance(items, list) else []:
                if not isinstance(it, dict) or not str(it.get("text", "")).strip():
                    continue
                refs = [x for x in (it.get("refs") or []) if isinstance(x, str) and x in valid_refs]
                if refs:  # uncited / mis-cited items are dropped, never shown as library facts
                    clean.append({"text": str(it["text"]).strip(), "refs": refs})
            a[key] = clean
            kept += len(clean)
        if kept == 0:
            problems.append(f"part A has no item citing a valid reference id ({', '.join(sorted(valid_refs))})")
        b = data.get("B")
        if not isinstance(b, dict):
            data["B"] = b = {}
        for key in ("workflow", "pitfalls", "reading_order"):
            b[key] = [str(x).strip() for x in (b.get(key) or []) if str(x).strip()]
        b["core_concepts"] = [x for x in (b.get("core_concepts") or []) if isinstance(x, dict) and x.get("term")]
        b["tools"] = [x for x in (b.get("tools") or []) if isinstance(x, dict) and x.get("name")]
        return problems

    return validate


def _cite(refs_by_id: dict[str, dict], ids: list[str]) -> str:
    parts = []
    for i in ids:
        r = refs_by_id[i]
        label = f"{r['first_author'] or '?'} {r['year'] or ''}".strip()
        if r["doi"]:
            parts.append(f"[{label}](https://doi.org/{r['doi']})")
        else:
            parts.append(f"{label}（无 DOI）")
    return "（" + "；".join(parts) + "）"


def render(field, data: dict, refs: list[dict], language: str, model: str, user_section: str | None) -> str:
    h = H["en" if language == "en" else "zh"]
    by_id = {r["ref"]: r for r in refs}
    labels = {r["ref"]: f"{r['first_author'] or '?'} {r['year'] or ''}".strip() for r in refs}
    for key in ("representative", "methods", "findings"):
        for it in data["A"].get(key) or []:
            it["text"] = replace_ref_ids(it["text"], labels)
    if data.get("overview"):
        data["overview"] = replace_ref_ids(data["overview"], labels)
    name = field["name_en"] if language == "en" else field["name_zh"]
    front = {"slug": field["slug"], "name_zh": field["name_zh"], "name_en": field["name_en"],
             "papers": len(refs), "dois": [r["doi"] for r in refs if r["doi"]],
             "generated": date.today().isoformat(), "model": model, "prompt_version": PROMPT_VERSION}
    out = ["---", yaml.safe_dump(front, allow_unicode=True, sort_keys=False, width=10**6).strip(), "---", "",
           f"# {name} · 学习手册" if language != "en" else f"# {name} · Handbook", "",
           f"{h['field']}: [[fields/{field['slug']}|{name}]]", ""]
    if data.get("overview"):
        out += [f"## {h['overview']}", "", data["overview"], ""]
    out += [f"## {h['notes']}", "", USER_BEGIN, user_section if user_section is not None else h["notes_hint"], USER_END, ""]

    out += [f"## {h['a']}", "", f"> {h['a_note']}", ""]
    for key, title in (("representative", h["rep"]), ("methods", h["methods"]), ("findings", h["findings"])):
        items = data["A"].get(key) or []
        if items:
            out += [f"### {title}", ""]
            out += [f"- {it['text']} {_cite(by_id, it['refs'])}" for it in items]
            out.append("")
    out += [f"### {h['papers']}", ""]
    for r in refs:
        link = f"[[{(r['kb_path'] or '').removesuffix('.md')}|{r['first_author'] or '?'} {r['year'] or ''}]]"
        doi = f" · [{r['doi']}](https://doi.org/{r['doi']})" if r["doi"] else ""
        out.append(f"- {link} — {r['title']}{doi}")
    out.append("")

    b = data.get("B") or {}
    out += [f"## {h['b']}", "", f"> {h['b_note']}", ""]
    if b.get("core_concepts"):
        out += [f"### {h['concepts']}", ""] + [f"- **{c['term']}**：{c.get('explanation', '')}" for c in b["core_concepts"]] + [""]
    if b.get("workflow"):
        out += [f"### {h['workflow']}", ""] + [f"{i}. {s}" for i, s in enumerate(b["workflow"], 1)] + [""]
    if b.get("tools"):
        out += [f"### {h['tools']}", ""] + [f"- `{t['name']}`：{t.get('purpose', '')}" for t in b["tools"]] + [""]
    if b.get("pitfalls"):
        out += [f"### {h['pitfalls']}", ""] + [f"- {s}" for s in b["pitfalls"]] + [""]
    if b.get("reading_order"):
        out += [f"### {h['reading']}", ""] + [f"{i}. {s}" for i, s in enumerate(b["reading_order"], 1)] + [""]
    return "\n".join(out).rstrip() + "\n"


def handbook_rel_path(slug: str) -> str:
    return f"handbooks/{slug}.md"


def generate(llm: LLMClient, config: Config, registry: Registry, slug: str) -> str:
    """Generate (or regenerate) the handbook for a field. Returns the rel path."""
    field = registry.field_by_slug(slug)
    if field is None:
        raise ValueError(f"unknown field {slug}")
    refs = paper_refs(registry, field["id"])
    if not refs:
        raise ValueError(f"field '{slug}' has no processed papers yet")
    rule = LANGUAGE_RULES.get(config.output_language, LANGUAGE_RULES["zh"])
    system = _load_prompt(PROMPT_VERSION).replace("{language_rule}", rule)
    res = llm.chat_json(system, build_user(field, refs), make_validator({r["ref"] for r in refs}),
                        purpose="handbook", max_tokens=8192)
    rel = handbook_rel_path(slug)
    path: Path = config.knowledge_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render(field, res.data, refs, config.output_language, res.model, read_user_section(path))
    atomic_write(path, text)
    return rel
