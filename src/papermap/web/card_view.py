"""Present a rendered paper card as tiles: a takeaway banner, study-design facts, method and tool
chips, numbered findings and a quieter limitations block.

Display only. The card Markdown stays as written (Obsidian, the card editor and `card_override`
read it), and the visible text is not changed: right-click formatting finds a selection by counting
its earlier occurrences in the rendered card, so only wrappers and class names are added here.
"""
from __future__ import annotations

import re

from ..kb_writer import HEADINGS

# heading text (both card languages) -> tile kind
_KINDS = {}
for _h in HEADINGS.values():
    for _key in ("one", "question", "design", "methods", "tools", "findings", "limits", "fields", "mine"):
        _KINDS[_h[_key]] = _key

_H2 = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S)
_TAGS = re.compile(r"<[^>]+>")


def _kind(heading_html: str) -> str:
    text = re.sub(r"\s+", " ", _TAGS.sub("", heading_html)).strip()
    return _KINDS.get(text, "other")


def _empty(body: str) -> bool:
    return _TAGS.sub("", body).strip() in ("", "—", "-")


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


def _tools(body: str) -> str:
    return re.sub(r"</code>(\s*[,，、]\s*)<code>", r'</code><span class="cc-sep">\1</span><code>', body)


def cardify(html: str) -> str:
    """Wrap each "## section" of the rendered card in a tile; unknown sections become plain tiles."""
    if not html or "<h2" not in html:
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
        elif kind == "tools":
            body = _tools(body)
        cls = f"cc cc-{kind}" + (" cc-empty" if _empty(body) else "")
        out.append(f'<section class="{cls}"><h2 class="cc-h">{head}</h2><div class="cc-b">{body}</div></section>')
    return '<div class="card-tiles">' + "".join(out) + "</div>"
