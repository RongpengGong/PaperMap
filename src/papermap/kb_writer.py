"""Write Markdown paper cards into the knowledge base."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml

from .doi import slug as doi_slug

def atomic_write(path: Path, text: str, attempts: int = 5) -> None:
    """Write via temp file + rename. On Windows the rename fails while another
    program (e.g. an editor) holds the file, so retry briefly."""
    import time

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, "utf-8")
    for i in range(attempts):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if i == attempts - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.3 * (i + 1))


USER_BEGIN = "<!-- user:begin -->"
USER_END = "<!-- user:end -->"

HEADINGS = {
    "zh": {
        "one": "一句话结论",
        "question": "研究问题",
        "design": "研究设计 / 数据",
        "methods": "核心方法与工具",
        "tools": "软件 / 工具包",
        "findings": "主要发现",
        "limits": "局限性",
        "mine": "我的笔记",
        "fields": "所属学科",
        "nodoi": "⚠️ 无 DOI",
        "sample": "样本量",
        "species": "物种",
        "tissues": "组织",
        "platforms": "平台",
        "cohorts": "队列",
        "mine_hint": "（在这里写你自己的笔记，程序更新卡片时不会改动这一节。）",
        "fields_todo": "（尚未分类）",
        "method_axis": "方法学", "topic_axis": "主题", "suggested": "待审核", "primary": "主要",
    },
    "en": {
        "one": "One-sentence takeaway",
        "question": "Research question",
        "design": "Study design / data",
        "methods": "Core methods",
        "tools": "Software / tools",
        "findings": "Key findings",
        "limits": "Limitations",
        "mine": "My notes",
        "fields": "Fields",
        "nodoi": "⚠️ No DOI",
        "sample": "Sample size",
        "species": "Species",
        "tissues": "Tissues",
        "platforms": "Platforms",
        "cohorts": "Cohorts",
        "mine_hint": "(Your own notes go here; PaperMap never overwrites this section.)",
        "fields_todo": "(not classified yet)",
        "method_axis": "Methods", "topic_axis": "Topics", "suggested": "needs review", "primary": "primary",
    },
}


def card_filename(meta: dict, fallback_id: str) -> str:
    year = meta.get("year") or "nd"
    authors = meta.get("authors") or []
    first = (authors[0].get("family") or authors[0].get("name", "")) if authors else "Anon"
    first = _ascii_slug(first, title_case=True)[:30] or "Anon"
    journal = meta.get("journal_abbrev") or meta.get("journal") or "NA"
    journal = _ascii_slug(journal, title_case=True)[:25] or "NA"
    tail = doi_slug(meta["doi"]) if meta.get("doi") else f"nodoi-{fallback_id}"
    return f"{year}_{first}_{journal}_{tail}.md"


def _ascii_slug(text: str, title_case: bool = False) -> str:
    import unicodedata

    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = re.findall(r"[A-Za-z0-9]+", t)
    if title_case:
        return "".join(w[:1].upper() + w[1:] for w in words)
    return "-".join(w.lower() for w in words)


def render_card(
    meta: dict,
    card: dict,
    file_paths: list[str],
    all_dois: list[dict],
    language: str,
    user_section: str | None = None,
    added: str | None = None,
    fields: list[dict] | None = None,
    stem: str | None = None,
    has_pdf: bool = False,
    collections: list[str] | None = None,
    reading: dict | None = None,
    override: str | None = None,
) -> str:
    h = HEADINGS["en" if language == "en" else "zh"]
    authors = [a.get("name", "") for a in meta.get("authors") or [] if a.get("name")]
    front = {
        "doi": meta.get("doi"),
        "dois": [d["doi"] for d in all_dois] or ([meta["doi"]] if meta.get("doi") else []),
        "title": meta.get("title", ""),
        "authors": authors,
        "year": meta.get("year"),
        "journal": meta.get("journal", ""),
        "openalex_id": meta.get("openalex_id"),
        "fields": [f["slug"] for f in fields or [] if f["axis"] == "method" and f["status"] == "accepted"],
        "topics": [f["slug"] for f in fields or [] if f["axis"] == "topic" and f["status"] == "accepted"],
        "tools": card.get("tools", []),
        "collections": collections or [],
        "read_status": (reading or {}).get("read_status", "unread"),
        "starred": bool((reading or {}).get("starred")),
        "tags": (reading or {}).get("tags", []),
        "pdf": f"{stem}.pdf" if stem and has_pdf else None,
        "notes": f"{stem}.notes.md" if stem else None,
        "file_paths": file_paths,
        "added": added or date.today().isoformat(),
    }
    out = ["---", yaml.safe_dump(front, allow_unicode=True, sort_keys=False, width=10**6).strip(), "---", ""]
    if override is not None and override.strip():
        # the user edited the card: keep their text; notes embed and field list stay automatic
        if "edited" not in front:
            out[1] += "\nedited: true"
        out.extend([override.strip(), ""])
        _tail(out, h, stem, has_pdf, user_section, language, fields)
        return "\n".join(out).rstrip() + "\n"
    out.append(f"# {meta.get('title') or '(untitled)'}")
    out.append("")
    cite = ", ".join(x for x in [_short_authors(authors), meta.get("journal", ""), str(meta.get("year") or "")] if x)
    out.append(f"*{cite}*  ")
    if meta.get("doi"):
        out.append(f"DOI: [{meta['doi']}](https://doi.org/{meta['doi']})")
        for d in all_dois:
            if d["doi"] != meta["doi"]:
                out.append(f"  · {d['kind']}: [{d['doi']}](https://doi.org/{d['doi']})")
    else:
        out.append(f"**{h['nodoi']}**")
    out.append("")

    def section(title: str, body: str) -> None:
        out.extend([f"## {title}", "", body.strip() or "—", ""])

    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {x}" for x in items) if items else "—"

    section(h["one"], card.get("one_sentence", ""))
    section(h["question"], card.get("research_question", ""))
    d = card.get("design") or {}
    design_lines = [d.get("summary", "")] if d.get("summary") else []
    for key in ("sample_size",):
        if d.get(key):
            design_lines.append(f"- **{h['sample']}**: {d[key]}")
    for key in ("species", "tissues", "platforms", "cohorts"):
        if d.get(key):
            design_lines.append(f"- **{h[key]}**: {', '.join(d[key])}")
    section(h["design"], "\n".join(design_lines))
    section(h["methods"], bullets(card.get("methods", [])))
    section(h["tools"], ", ".join(f"`{t}`" for t in card.get("tools", [])) or "—")
    section(h["findings"], "\n".join(f"{i}. {x}" for i, x in enumerate(card.get("key_findings", []), 1)))
    section(h["limits"], bullets(card.get("limitations", [])))
    _tail(out, h, stem, has_pdf, user_section, language, fields)
    return "\n".join(out).rstrip() + "\n"


def editable_part(text: str, language: str) -> str:
    """The part of a card the user may edit: everything above the "My notes" section."""
    h = HEADINGS["en" if language == "en" else "zh"]
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        body = text[end + 4:] if end != -1 else text
    m = re.search(rf"^##\s+({re.escape(h['mine'])}|My notes|我的笔记)\s*$", body, flags=re.M)
    return (body[:m.start()] if m else body).strip()


def _tail(out: list[str], h: dict, stem, has_pdf, user_section, language, fields) -> None:
    def section(title: str, body: str) -> None:
        out.extend([f"## {title}", "", body.strip() or "—", ""])

    if stem:  # notes live in their own file next to the card; embedded for Obsidian
        hint = "（在 {f} 里写，PaperMap 永远不会改动它）" if language != "en" else "(edit {f}; PaperMap never changes it)"
        out.extend([f"## {h['mine']}", "", f"![[{stem}.notes]]", "", hint.format(f=f"{stem}.notes.md"), ""])
        if has_pdf:
            out.extend([f"📄 [[{stem}.pdf]]", ""])
    else:
        out.extend([f"## {h['mine']}", "", USER_BEGIN, user_section if user_section is not None else h["mine_hint"], USER_END, ""])
    section(h["fields"], _fields_block(fields or [], h, language))


def _fields_block(fields: list[dict], h: dict, language: str) -> str:
    if not fields:
        return h["fields_todo"]
    lines = []
    for axis, title in (("method", h["method_axis"]), ("topic", h["topic_axis"])):
        items = []
        for f in fields:
            if f["axis"] != axis or f["status"] == "rejected":
                continue
            name = f["name_en"] if language == "en" else f["name_zh"]
            tag = []
            if f.get("is_primary") and axis == "method":
                tag.append(h["primary"])
            if f["status"] == "suggested":
                tag.append(h["suggested"])
            suffix = f"（{'，'.join(tag)}）" if tag and language != "en" else (f" ({', '.join(tag)})" if tag else "")
            items.append(f"[[fields/{f['slug']}|{name}]]{suffix}")
        if items:
            lines.append(f"- **{title}**: " + " · ".join(items))
    return "\n".join(lines) or h["fields_todo"]


def _short_authors(authors: list[str]) -> str:
    if not authors:
        return ""
    if len(authors) <= 2:
        return " & ".join(authors)
    return f"{authors[0]} et al."


def read_user_section(path: Path) -> str | None:
    if not path.exists():
        return None
    text = path.read_text("utf-8")
    m = re.search(re.escape(USER_BEGIN) + r"\n?(.*?)\n?" + re.escape(USER_END), text, re.DOTALL)
    return m.group(1) if m else None


def write_user_section(path: Path, text: str) -> None:
    """Replace only the user's block (edited from the app); everything else stays as generated."""
    body = path.read_text("utf-8")
    pattern = re.escape(USER_BEGIN) + r"\n?.*?\n?" + re.escape(USER_END)
    if not re.search(pattern, body, re.DOTALL):
        raise ValueError("这个页面没有「我的笔记」区域")
    text = text.replace(USER_BEGIN, "").replace(USER_END, "").strip("\n")
    atomic_write(path, re.sub(pattern, lambda _: f"{USER_BEGIN}\n{text}\n{USER_END}", body, count=1, flags=re.DOTALL))


def read_added(path: Path) -> str | None:
    if not path.exists():
        return None
    m = re.search(r"^added:\s*'?([0-9-]+)'?\s*$", path.read_text("utf-8"), re.MULTILINE)
    return m.group(1) if m else None


def write_card(
    knowledge_dir: Path,
    meta: dict,
    card: dict,
    file_paths: list[str],
    all_dois: list[dict],
    language: str,
    fallback_id: str,
    existing_rel_path: str | None = None,
    fields: list[dict] | None = None,
    collections: list[str] | None = None,
    reading: dict | None = None,
    override: str | None = None,
) -> str:
    """Write (or rewrite) a card in the paper's own folder. Returns rel path."""
    if existing_rel_path:
        rel = existing_rel_path
    else:
        stem = card_filename(meta, fallback_id).removesuffix(".md")
        rel = f"papers/{stem}/{stem}.md"
    path = knowledge_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    stem = path.stem if rel.count("/") >= 2 else None  # legacy flat cards keep the inline block
    if stem:
        from .library import ensure_notes

        ensure_notes(knowledge_dir, rel, meta.get("title", ""), language)
    text = render_card(
        meta, card, file_paths, all_dois, language,
        user_section=read_user_section(path), added=read_added(path), fields=fields,
        stem=stem, has_pdf=bool(stem) and (path.parent / f"{stem}.pdf").exists(), collections=collections,
        reading=reading, override=override,
    )
    atomic_write(path, text)
    return rel
