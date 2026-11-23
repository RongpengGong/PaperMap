"""BibTeX / RIS export from stored Crossref/OpenAlex metadata (CLAUDE.md §15.4)."""

from __future__ import annotations

import json
import re
import unicodedata

from .registry import Registry

_TYPES_BIB = {"journal-article": "article", "posted-content": "misc", "book-chapter": "incollection",
              "proceedings-article": "inproceedings", "book": "book", "dissertation": "phdthesis"}
_TYPES_RIS = {"journal-article": "JOUR", "posted-content": "UNPB", "book-chapter": "CHAP",
              "proceedings-article": "CPAPER", "book": "BOOK", "dissertation": "THES"}


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()


def _bib_escape(s: str) -> str:
    s = (s or "").replace("\\", "\\textbackslash{}")
    return re.sub(r"([&%$#_{}])", r"\\\1", s)


def _meta(row) -> dict:
    meta = json.loads(row["meta_json"] or "{}")
    meta.setdefault("title", row["title"])
    meta["doi"] = row["doi"]
    meta.setdefault("year", row["year"])
    meta.setdefault("journal", row["journal"])
    return meta


def _key(meta: dict, used: set[str]) -> str:
    authors = meta.get("authors") or []
    first = _ascii((authors[0].get("family") or authors[0].get("name", "")) if authors else "anon")
    first = re.sub(r"[^A-Za-z]", "", first).lower() or "anon"
    word = next((w for w in re.findall(r"[A-Za-z]+", _ascii(meta.get("title", ""))) if len(w) > 3), "paper").lower()
    base = f"{first}{meta.get('year') or ''}{word}"
    key, i = base, 0
    while key in used:
        i += 1
        key = f"{base}{chr(96 + i)}"
    used.add(key)
    return key


def _authors(meta: dict) -> list[tuple[str, str]]:
    out = []
    for a in meta.get("authors") or []:
        fam, giv = a.get("family") or "", a.get("given") or ""
        if not fam and a.get("name"):
            parts = a["name"].split()
            fam, giv = parts[-1], " ".join(parts[:-1])
        if fam:
            out.append((fam, giv))
    return out


def bibtex(registry: Registry, paper_ids: list[int]) -> str:
    used: set[str] = set()
    entries = []
    for pid in paper_ids:
        row = registry.get_paper(pid)
        if row is None:
            continue
        m = _meta(row)
        typ = _TYPES_BIB.get(m.get("type", ""), "article")
        fields = [("title", "{" + _bib_escape(m.get("title", "")) + "}"),
                  ("author", " and ".join(f"{_bib_escape(f)}, {_bib_escape(g)}".rstrip(", ") for f, g in _authors(m)))]
        if m.get("journal"):
            fields.append(("journal" if typ == "article" else "howpublished", _bib_escape(m["journal"])))
        for k, v in (("year", m.get("year")), ("volume", m.get("volume")), ("number", m.get("issue")),
                     ("pages", (m.get("page") or "").replace("-", "--")), ("publisher", m.get("publisher")),
                     ("doi", m.get("doi"))):
            if v:
                fields.append((k, _bib_escape(str(v)) if k != "doi" else str(v)))
        if m.get("doi"):
            fields.append(("url", f"https://doi.org/{m['doi']}"))
        body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields if v)
        entries.append(f"@{typ}{{{_key(m, used)},\n{body}\n}}")
    return "\n\n".join(entries) + ("\n" if entries else "")


def ris(registry: Registry, paper_ids: list[int]) -> str:
    out = []
    for pid in paper_ids:
        row = registry.get_paper(pid)
        if row is None:
            continue
        m = _meta(row)
        lines = [f"TY  - {_TYPES_RIS.get(m.get('type', ''), 'JOUR')}", f"TI  - {m.get('title', '')}"]
        lines += [f"AU  - {f}, {g}".rstrip(", ") for f, g in _authors(m)]
        for tag, v in (("JO", m.get("journal")), ("PY", m.get("year")), ("VL", m.get("volume")),
                       ("IS", m.get("issue")), ("DO", m.get("doi")), ("PB", m.get("publisher"))):
            if v:
                lines.append(f"{tag}  - {v}")
        page = m.get("page") or ""
        if page:
            sp, _, ep = page.partition("-")
            lines.append(f"SP  - {sp}")
            if ep:
                lines.append(f"EP  - {ep}")
        if m.get("abstract"):
            lines.append(f"AB  - {m['abstract'][:4000]}")
        if m.get("doi"):
            lines.append(f"UR  - https://doi.org/{m['doi']}")
        lines.append("ER  - ")
        out.append("\n".join(lines))
    return "\n\n".join(out) + ("\n" if out else "")
