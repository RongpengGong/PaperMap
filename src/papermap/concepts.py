"""Tool / concept pages (concepts/<slug>.md), built deterministically from cards."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import yaml

from .kb_fields import paper_link, tool_name
from .kb_writer import USER_BEGIN, USER_END, atomic_write, read_user_section
from .registry import Registry


def concept_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "tool"


def collect(registry: Registry) -> dict[str, dict]:
    """slug -> {name, papers: [row], co: Counter} over all finished papers."""
    rows = registry.query("SELECT id AS paper_id, title, year, journal, first_author, kb_path, card_json "
                          "FROM papers WHERE status='done' ORDER BY year DESC, id DESC")
    tools: dict[str, dict] = {}
    per_paper: dict[int, set[str]] = {}
    for r in rows:
        slugs = set()
        for t in json.loads(r["card_json"] or "{}").get("tools", []):
            name = tool_name(t)
            if not name or len(name) < 2:
                continue
            s = concept_slug(name)
            entry = tools.setdefault(s, {"name": name, "papers": [], "co": Counter()})
            if r["paper_id"] not in {p["paper_id"] for p in entry["papers"]}:
                entry["papers"].append(r)
            slugs.add(s)
        per_paper[r["paper_id"]] = slugs
    for slugs in per_paper.values():
        for s in slugs:
            tools[s]["co"].update(slugs - {s})
    return tools


def write_all(knowledge_dir: Path, registry: Registry, language: str, min_papers: int = 2) -> set[str]:
    """Write pages for tools used by >= min_papers papers; return their slugs."""
    d = knowledge_dir / "concepts"
    d.mkdir(parents=True, exist_ok=True)
    tools = collect(registry)
    keep = {s for s, e in tools.items() if len(e["papers"]) >= min_papers}
    zh = language != "en"
    for s in keep:
        e = tools[s]
        path = d / f"{s}.md"
        fields: Counter[tuple[str, str]] = Counter()
        for p in e["papers"]:
            for fr in registry.paper_field_rows(p["paper_id"]):
                if fr["status"] == "accepted":
                    fields[(fr["slug"], fr["name_zh"] if zh else fr["name_en"])] += 1
        front = {"name": e["name"], "slug": s, "kind": "tool", "papers": len(e["papers"]),
                 "updated": date.today().isoformat()}
        out = ["---", yaml.safe_dump(front, allow_unicode=True, sort_keys=False, width=10**6).strip(), "---", "",
               f"# {e['name']}", "",
               (f"在你的文献库中有 {len(e['papers'])} 篇论文用到了 `{e['name']}`。" if zh
                else f"`{e['name']}` is used by {len(e['papers'])} papers in your library."), "",
               f"## {'我的笔记' if zh else 'My notes'}", "", USER_BEGIN,
               read_user_section(path) if path.exists() else ("（写下用法、参数心得、踩过的坑…）" if zh else "(usage notes)"),
               USER_END, "",
               f"## {'用到它的论文' if zh else 'Papers using it'}", ""]
        out += [f"- {p['year'] or ''} {paper_link(p)} — {p['title']}" for p in e["papers"]]
        out += ["", f"## {'出现在这些学科' if zh else 'Fields'}", ""]
        out += [f"- [[fields/{fs}|{fn}]] ×{n}" for (fs, fn), n in fields.most_common()] or ["—"]
        co = [(c, n) for c, n in e["co"].most_common(15) if c in keep]
        out += ["", f"## {'经常一起使用' if zh else 'Often used with'}", ""]
        out += [", ".join(f"[[concepts/{c}|{tools[c]['name']}]] ×{n}" for c, n in co)] if co else ["—"]
        atomic_write(path, "\n".join(out) + "\n")
    defaults = {"（写下用法、参数心得、踩过的坑…）", "(usage notes)"}
    for f in d.glob("*.md"):
        # drop pages of tools that fell below the threshold, unless the user wrote notes there
        if f.stem not in keep and (read_user_section(f) or "").strip() in defaults:
            f.unlink()
    return keep
