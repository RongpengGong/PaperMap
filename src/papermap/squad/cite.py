"""Citation markers in squad output: keep only markers that point at something real, drop prose
sentences that cite nothing, and turn markers into readable links."""
from __future__ import annotations

import re

from ..registry import Registry
from . import store

MARK = re.compile(r"\[([PECW])(\d+)\]")
GROUP = re.compile(r"(?:\s*\[[PECW]\d+\])+")
_SENT = re.compile(r"(?<=[。！？!?])|(?<=[.;])\s+(?=[A-Z\[\(“\"])")
KEEP_SHORT = 24  # short connective lines ("Summary:") stay without a citation


def _exists(registry: Registry, run_id: int, kind: str, n: int) -> bool:
    if kind == "P":
        return registry.one("SELECT 1 FROM papers WHERE id=? AND status!='ignored'", (n,)) is not None
    if kind == "E":
        return registry.one("SELECT 1 FROM evidence_entries WHERE id=?", (n,)) is not None
    if kind == "C":
        return registry.one("SELECT 1 FROM sqlite_master WHERE name='track_candidates'") is not None and \
            registry.one("SELECT 1 FROM track_candidates WHERE id=?", (n,)) is not None
    return store.get_work(registry, run_id, n) is not None


def markers(text: str) -> list[tuple[str, int]]:
    return [(k, int(n)) for k, n in MARK.findall(text)]


def _size(text: str) -> int:
    """Length where a CJK character counts double (it carries about a word)."""
    s = text.strip()
    return len(s) + sum(1 for ch in s if "\u3400" <= ch <= "\u9fff")


def _prose(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith(("#", "|", ">", "```", "---")) and _size(s) >= KEEP_SHORT


def check(registry: Registry, run_id: int, text: str) -> tuple[str, int]:
    """Remove unknown markers, then prose sentences without any marker. Returns (text, dropped)."""
    cache: dict[tuple[str, int], bool] = {}

    def ok(k: str, n: int) -> bool:
        if (k, n) not in cache:
            cache[(k, n)] = _exists(registry, run_id, k, n)
        return cache[(k, n)]

    text = MARK.sub(lambda m: m.group(0) if ok(m.group(1), int(m.group(2))) else "", text)
    dropped, out = 0, []
    for line in text.splitlines():
        if not _prose(line):
            out.append(line)
            continue
        prefix = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", line)
        lead = prefix.group(0) if prefix else ""
        body = line[len(lead):]
        kept = []
        for sent in (s for s in _SENT.split(body) if s and s.strip()):
            if MARK.search(sent) or _size(sent) < KEEP_SHORT:
                kept.append(sent)
            else:
                dropped += 1
        joined = ""
        for sent in (x.strip() for x in kept):
            joined += sent if not joined or joined[-1] in "。！？" else " " + sent
        if joined and (MARK.search(joined) or not lead):
            out.append(lead + joined)
        elif joined:
            dropped += 0 if MARK.search(joined) else 1
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip(), dropped


def readable(registry: Registry, run_id: int, text: str) -> str:
    """[P12] -> [Zhang 2024](/paper/12), [E5] -> excerpt link, [C7] / [W3] -> titles."""

    def sub(m: re.Match) -> str:
        k, n = m.group(1), int(m.group(2))
        if k == "P":
            r = registry.get_paper(n)
            return f"[{_label(r)}](/paper/{n})" if r else ""
        if k == "E":
            r = registry.one("SELECT e.id, e.paper_id, e.page, p.first_author, p.year, p.title FROM evidence_entries e JOIN papers p "
                             "ON p.id=e.paper_id WHERE e.id=?", (n,))
            if not r:
                return ""
            from .. import evidence

            return f"[{_label(r)}, p.{r['page'] + 1}]({evidence.anchor_path(r)})"
        if k == "C":
            r = registry.one("SELECT title, year FROM track_candidates WHERE id=?", (n,))
            return f"[{(r['title'] or '')[:60]}… {r['year'] or ''}](/tracking)" if r else ""
        w = store.get_work(registry, run_id, n)
        if not w:
            return ""
        link = f"https://doi.org/{w['doi']}" if w.get("doi") else ""
        label = f"{(w.get('title') or '')[:60]}… {w.get('year') or ''}"
        return f"[{label}]({link})" if link else f"({label})"

    def group(m: re.Match) -> str:
        parts = [x for x in (sub(x) for x in MARK.finditer(m.group(0))) if x]
        raw = m.group(0)
        lead = raw[: len(raw) - len(raw.lstrip())]
        return f"{lead}({'; '.join(parts)})" if parts else ""

    return GROUP.sub(group, text)


def _label(row) -> str:
    """Author and year, or the start of the title when the paper has no author (theses, reports)."""
    if row["first_author"]:
        return f"{row['first_author']} {row['year'] or ''}".strip()
    title = (row["title"] or "?").strip()
    return (title[:40] + "…" if len(title) > 40 else title) + (f" {row['year']}" if row["year"] else "")
