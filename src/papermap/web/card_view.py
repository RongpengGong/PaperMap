"""Present rendered Markdown as tiles: one tile per "## section".

Used for the paper card (takeaway banner, study-design facts, method and tool chips, numbered
findings, quieter limitations), the "My notes" tab, skill readings and the field, handbook and tool
pages.

Display only. The Markdown files stay as written (Obsidian, the editors and `card_override` read
them), and the visible text is not changed: right-click formatting finds a selection by counting
its earlier occurrences in the rendered card, so only wrappers and class names are added here.
"""
from __future__ import annotations

import re

from ..kb_writer import HEADINGS

# paper-card heading text (both card languages) -> tile kind
_KINDS = {}
for _h in HEADINGS.values():
    for _key in ("one", "question", "design", "methods", "tools", "findings", "limits", "fields", "mine"):
        _KINDS[_h[_key]] = _key
# headings of field, handbook and tool pages, matched by their start
_PREFIXES = (
    ("概述", "overview"), ("Overview", "overview"),
    ("我的笔记", "mine"), ("My notes", "mine"),
    ("A. ", "library"), ("B. ", "background"),
    ("争议", "disputes"), ("Disagreements", "disputes"),
)

_H2 = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S)
_TAGS = re.compile(r"<[^>]+>")


def _kind(heading_html: str) -> str:
    text = re.sub(r"\s+", " ", _TAGS.sub("", heading_html)).strip()
    if text in _KINDS:
        return _KINDS[text]
    return next((kind for prefix, kind in _PREFIXES if text.startswith(prefix)), "other")


def _empty(body: str) -> bool:
    text = _TAGS.sub("", re.sub(r"<!--.*?-->", "", body, flags=re.S)).strip()
    return text in ("", "—", "-")


_FACT = re.compile(r"<strong>(.*?)</strong>(\s*[:：]\s*)(.*)", re.S)


def _fact(item: str, marker: str = "") -> str | None:
    m = _FACT.fullmatch(item.strip("\n"))
    if not m:
        return None
    sep = f'<span class="cc-sep">{marker}</span>' if marker else ""
    return (f'<li>{sep}<strong class="cc-k">{m.group(1)}</strong>'
            f'<span class="cc-sep">{m.group(2)}</span><span class="cc-v">{m.group(3)}</span></li>')


def _design(body: str) -> str:
    """Study-design facts ("**Sample size**: n=12") become label-over-value tiles. The generated card
    puts the list right under the summary line, which Markdown renders as one paragraph."""
    body = re.sub(r"<li><strong>.*?</li>", lambda m: _fact(m.group(0)[4:-5]) or m.group(0), body, flags=re.S)

    def para(m: re.Match) -> str:
        pieces = re.split(r"((?:^|\n)- )(?=<strong>)", m.group(1))
        if len(pieces) < 3:
            return m.group(0)
        facts = [_fact(pieces[i + 1], pieces[i]) for i in range(1, len(pieces) - 1, 2)]
        if not all(facts):
            return m.group(0)
        lead = f"<p>{pieces[0]}</p>" if pieces[0].strip() else ""
        return f'{lead}<ul class="cc-facts">{"".join(facts)}</ul>'

    return re.sub(r"<p>(.*?)</p>", para, body, flags=re.S)


def _chips(body: str) -> str:
    """`Seurat`, `cell2location` -> separate code chips; the separators stay in the text."""
    body = re.sub(r"</code>(\s*[,，、]\s*)<code>", r'</code><span class="cc-sep">\1</span><code>', body)
    # a paragraph that is only such a list is laid out as a row of chips
    return re.sub(r'<p>((?:<code>[^<]*</code>(?:<span class="cc-sep">[^<]*</span>)?)+)</p>', r'<p class="cc-chips">\1</p>', body)


def cardify(html: str, whole: bool = False) -> str:
    """Wrap each "## section" of rendered Markdown in a tile; unknown sections become plain tiles.
    Text without sections is returned as is, or as one tile with `whole`."""
    if not html or "<h2" not in html:
        if whole and html and html.strip():
            return f'<div class="card-tiles"><section class="cc cc-other"><div class="cc-b">{html}</div></section></div>'
        return html
    parts = _H2.split(html)
    lead, rest = parts[0], parts[1:]
    out = []
    if lead.strip():
        out.append(f'<div class="cc-lead">{lead}</div>')
    for i in range(0, len(rest), 2):
        head, body = rest[i], rest[i + 1] if i + 1 < len(rest) else ""
        kind = _kind(head)
        if kind == "design":
            body = _design(body)
        body = _chips(body)
        cls = f"cc cc-{kind}" + (" cc-empty" if _empty(body) else "")
        out.append(f'<section class="{cls}"><h2 class="cc-h">{head}</h2><div class="cc-b">{body}</div></section>')
    return '<div class="card-tiles">' + "".join(out) + "</div>"


_NOTE_BOILERPLATE = re.compile(r"^\s*[（(][^）)]*(PaperMap|程序)[^）)]*[）)]\s*$")


def notes_written(text: str | None) -> bool:
    """Whether a notes file holds anything the user wrote, beyond headings and the file's own hint."""
    for line in (text or "").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and not _NOTE_BOILERPLATE.match(s) and s not in ("-", "*", "—"):
            return True
    return False
