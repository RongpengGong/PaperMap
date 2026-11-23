"""Comparison tables across papers (CLAUDE.md §15.6)."""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime
from pathlib import Path

from .config import Config
from .extract import LANGUAGE_RULES
from .extract import load_prompt as _load_prompt
from .kb_writer import atomic_write
from .llm import LLMClient
from .registry import Registry

COLUMNS = [("question", "研究问题"), ("species_tissue", "物种 / 组织"), ("n", "样本量"), ("platforms", "平台"),
           ("cohorts", "队列 / 数据来源"), ("methods", "核心方法"), ("tools", "工具"), ("findings", "主要发现"),
           ("limitations", "局限"), ("data", "本研究产生的数据")]


def rows(registry: Registry, paper_ids: list[int]) -> list[dict]:
    from . import resources

    resources.ensure_schema(registry)
    out = []
    for pid in paper_ids:
        p = registry.get_paper(pid)
        if p is None or not p["card_json"]:
            continue
        c = json.loads(p["card_json"])
        d = c.get("design") or {}
        data = [r["accession"] for r in registry.query(
            "SELECT accession FROM data_resources WHERE paper_id=? AND role='generated' ORDER BY kind", (pid,))]
        out.append({
            "paper": p, "ref": f"P{len(out) + 1}", "who": f"{p['first_author'] or '?'} {p['year'] or ''}".strip(),
            "question": c.get("research_question", ""),
            "species_tissue": " / ".join(x for x in [", ".join(d.get("species", [])), ", ".join(d.get("tissues", [])[:4])] if x),
            "n": d.get("sample_size", ""), "platforms": "; ".join(d.get("platforms", [])[:6]),
            "cohorts": "; ".join(d.get("cohorts", [])[:5]), "methods": c.get("methods", [])[:5],
            "tools": ", ".join(c.get("tools", [])[:12]), "findings": c.get("key_findings", [])[:3],
            "limitations": c.get("limitations", [])[:2], "data": ", ".join(data[:8]),
        })
    return out


def _cell(v) -> str:
    return "; ".join(v) if isinstance(v, list) else (v or "")


def to_markdown(items: list[dict], title: str, summary: str = "") -> str:
    head = "| 论文 | " + " | ".join(lbl for _, lbl in COLUMNS) + " |"
    sep = "|" + "---|" * (len(COLUMNS) + 1)
    lines = [f"# {title}", ""]
    if summary:
        lines += ["## 对比总结", "", summary, ""]
    lines += ["## 对比表", "", head, sep]
    for it in items:
        target = (it["paper"]["kb_path"] or "").removesuffix(".md")
        name = f"[[{target}\\|{it['who']}]]" if target else it["who"]
        cells = [_cell(it[k]).replace("|", "/").replace("\n", " ") for k, _ in COLUMNS]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def to_csv(items: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["paper", "doi"] + [lbl for _, lbl in COLUMNS])
    for it in items:
        w.writerow([it["who"] + " " + (it["paper"]["title"] or ""), it["paper"]["doi"] or ""] + [_cell(it[k]) for k, _ in COLUMNS])
    return "﻿" + buf.getvalue()


def summarize(config: Config, llm: LLMClient, items: list[dict]) -> tuple[str, float]:
    blocks = []
    for it in items:
        blocks.append(f"[{it['ref']}] {it['who']} — {it['paper']['title']}\n" + "\n".join(
            f"{lbl}: {_cell(it[k])}" for k, lbl in COLUMNS))
    rule = LANGUAGE_RULES.get(config.output_language, LANGUAGE_RULES["zh"]).replace("all string values", "the text")
    res = llm.chat_text(_load_prompt("compare_v1").replace("{language_rule}", rule), "\n\n".join(blocks),
                        purpose="compare", max_tokens=3000)
    valid = {it["ref"]: it["who"] for it in items}
    text = re.sub(r"\[(P\d+)\]", lambda m: f"（{valid[m.group(1)]}）" if m.group(1) in valid else "", res.raw)
    return text.strip(), res.cost_usd


def save(knowledge_dir: Path, items: list[dict], title: str, summary: str) -> str:
    slug = re.sub(r"[^\w一-鿿]+", "-", title)[:40].strip("-") or "comparison"
    rel = f"comparisons/{datetime.now().strftime('%Y%m%d-%H%M')}-{slug}.md"
    atomic_write(knowledge_dir / rel, to_markdown(items, title, summary))
    return rel
