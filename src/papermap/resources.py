"""Data & code resources mentioned in papers (CLAUDE.md §15.5).

Regex extraction from the PDF text (no LLM cost). Each hit gets the
repository, accession, a link, and a role guessed from its sentence:
'generated' (data of this study), 'reused' (public data used), or ''.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

import pymupdf

from .registry import Registry, now_iso

# (kind, label, regex, url template)
PATTERNS: list[tuple[str, str, str, str]] = [
    ("geo", "GEO", r"\bGSE\d{3,9}", "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={}"),
    ("sra", "SRA / BioProject", r"\b(?:PRJNA|PRJEB|PRJDB|SRP|ERP|DRP)\d{4,9}\b", "https://www.ncbi.nlm.nih.gov/bioproject/?term={}"),
    ("ega", "EGA", r"\bEGA[SD]\d{11}\b", "https://ega-archive.org/search/{}"),
    ("arrayexpress", "ArrayExpress / BioStudies", r"\bE-(?:MTAB|GEOD|MEXP|PROT)-\d{2,7}\b", "https://www.ebi.ac.uk/biostudies/arrayexpress/studies/{}"),
    ("cngb", "CNGB", r"\bCNP\d{7}\b", "https://db.cngb.org/search/project/{}/"),
    ("gsa", "GSA (NGDC)", r"\b(?:HRA|CRA|PRJCA)\d{6,8}\b", "https://ngdc.cncb.ac.cn/search/?dbId=&q={}"),
    ("dbgap", "dbGaP", r"\bphs\d{6}(?:\.v\d+\.p\d+)?\b", "https://www.ncbi.nlm.nih.gov/projects/gap/cgi-bin/study.cgi?study_id={}"),
    ("pride", "PRIDE", r"\bPXD\d{6}\b", "https://www.ebi.ac.uk/pride/archive/projects/{}"),
    ("zenodo", "Zenodo", r"\b10\.5281/zenodo\.\d+\b", "https://doi.org/{}"),
    ("figshare", "figshare", r"\b10\.6084/m9\.figshare\.[\d.v]+\b", "https://doi.org/{}"),
    ("synapse", "Synapse", r"\bsyn\d{7,9}\b", "https://www.synapse.org/#!Synapse:{}"),
    ("github", "GitHub", r"github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", "https://{}"),
    ("gitlab", "GitLab", r"gitlab\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", "https://{}"),
    ("cellxgene", "CELLxGENE", r"cellxgene\.cziscience\.com/[\w/.-]+", "https://{}"),
    ("tcga", "TCGA", r"\bTCGA-[A-Z]{2,5}\b", "https://portal.gdc.cancer.gov/projects/{}"),
]
GENERATED = re.compile(r"\b(deposited|have been (?:made )?available|are available (?:at|in|from|under|through|via)|"
                       r"generated (?:in|during) (?:this|the current|the present) study|accession (?:code|number)s?|"
                       r"we (?:have )?(?:uploaded|released|submitted)|code (?:is|are) available|available on github)", re.I)
REUSED = re.compile(r"\b(obtained from|downloaded from|retrieved from|publicly available|public(?:ly)? (?:data|dataset)s?|"
                    r"previously (?:published|reported|described)|accessed (?:from|through|via)|re-?analy[sz]ed|"
                    r"from the (?:GEO|TCGA)|external (?:data|cohort))", re.I)
ROLE_LABELS = {"generated": "本研究产生", "reused": "引用的公开数据", "": "未注明"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS data_resources (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER NOT NULL REFERENCES papers(id),
    kind TEXT NOT NULL, label TEXT NOT NULL, accession TEXT NOT NULL, url TEXT,
    role TEXT NOT NULL DEFAULT '', page INTEGER, context TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (paper_id, accession)
);
"""


def ensure_schema(registry: Registry) -> None:
    with registry._lock:  # noqa: SLF001
        registry.conn.executescript(SCHEMA)
        registry.conn.commit()


def _sentence(text: str, start: int, end: int) -> str:
    """The sentence around a match (text is already flattened to one line)."""
    a = max(text.rfind(". ", 0, start), start - 300) + 1
    b = text.find(". ", end)
    b = min(b if b != -1 else len(text), end + 300)
    return text[a:b + 1].strip()


def _geo_fix(acc: str) -> str:
    """Citation superscripts glue onto accessions in PDF text ('GSE139495' + '51').
    GEO series ids have at most 6 digits (1xxxxx/2xxxxx today)."""
    digits = acc[3:]
    if len(digits) > 6:
        digits = digits[:6] if digits[0] in "12" else digits[:5]
    return "GSE" + digits


def extract_text(pages: list[str]) -> list[dict]:
    found: dict[str, dict] = {}
    for pno, text in enumerate(pages):
        flat = " ".join(re.sub(r"-\n(?=\w)", "", text).split())
        for kind, label, rx, url in PATTERNS:
            for m in re.finditer(rx, flat):
                acc = m.group(0).rstrip(".,;)")
                if kind == "geo":
                    acc = _geo_fix(acc)
                if kind in ("github", "gitlab"):
                    acc = re.sub(r"\.git$", "", acc)
                    if acc.lower().endswith(("/issues", "/pulls")):
                        continue
                ctx = _sentence(flat, m.start(), m.end())
                role = "generated" if GENERATED.search(ctx) else "reused" if REUSED.search(ctx) else ""
                prev = found.get(acc)
                if prev is None:
                    found[acc] = {"kind": kind, "label": label, "accession": acc, "url": url.format(acc),
                                  "role": role, "page": pno, "context": ctx[:400]}
                elif not prev["role"] and role:
                    prev.update(role=role, context=ctx[:400], page=pno)
    return list(found.values())


def extract_pdf(pdf: Path) -> list[dict]:
    with pymupdf.open(pdf) as doc:
        return extract_text([p.get_text() for p in doc])


def store(registry: Registry, paper_id: int, items: list[dict]) -> int:
    ensure_schema(registry)
    with registry._lock:  # noqa: SLF001
        registry.conn.execute("DELETE FROM data_resources WHERE paper_id=?", (paper_id,))
        registry.conn.executemany(
            "INSERT OR IGNORE INTO data_resources(paper_id, kind, label, accession, url, role, page, context, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [(paper_id, i["kind"], i["label"], i["accession"], i["url"], i["role"], i["page"], i["context"], now_iso())
             for i in items])
        registry.conn.commit()
    return len(items)


def rows(registry: Registry, kind: str = "", role: str = "", paper_ids: list[int] | None = None, q: str = "") -> list:
    ensure_schema(registry)
    sql = ("SELECT r.*, p.title, p.first_author, p.year, p.journal FROM data_resources r "
           "JOIN papers p ON p.id=r.paper_id WHERE p.status != 'ignored'")
    params: list = []
    if kind:
        sql += " AND r.kind=?"
        params.append(kind)
    if role in ("generated", "reused"):
        sql += " AND r.role=?"
        params.append(role)
    elif role == "_none":  # "not stated"
        sql += " AND r.role=''"
    if paper_ids is not None:
        sql += f" AND r.paper_id IN ({','.join('?' * len(paper_ids)) or 'NULL'})"
        params += paper_ids
    for w in [w for w in (q or "").split() if w][:6]:  # accession, title, author, journal or sentence
        sql += (" AND (r.accession LIKE ? OR r.context LIKE ? OR p.title LIKE ? OR p.first_author LIKE ? "
                "OR p.journal LIKE ?)")
        params += [f"%{w}%"] * 5
    return registry.query(sql + " ORDER BY p.year DESC, p.id, r.kind, r.accession", tuple(params))


def to_csv(items) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["paper", "year", "journal", "repository", "accession", "role", "page", "url", "context"])
    for r in items:
        w.writerow([r["title"], r["year"], r["journal"], r["label"], r["accession"], ROLE_LABELS[r["role"]],
                    (r["page"] or 0) + 1, r["url"], r["context"]])
    return "﻿" + buf.getvalue()  # BOM so Excel opens Chinese text correctly
