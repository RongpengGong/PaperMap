"""What a paired phone may see of the desktop's "new papers" and "to review" lists.

Only titles, venues and the reasons a paper was suggested leave the computer: no PDFs,
cards or notes of papers that were not shared in this session. The phone can file a
suggestion away ("later" / "not interested"); everything else is decided on the computer.
"""
from __future__ import annotations

from . import tracking
from .registry import Registry

_MAX_NEW = 60
_MAX_REVIEW = 40


def _short(text: str | None, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _reason_text(reason: dict) -> str:
    kind = reason.get("kind")
    if kind == "cites":
        return f"{reason['text']} {reason.get('n', 0)} 篇"
    if kind == "gapmatch":
        return f"{reason['text']}：{_short(reason.get('label'), 60)}"
    label = _short(reason.get("label"), 60)
    return f"{reason['text']}：{label}" if label else str(reason.get("text") or "")


def new_papers(registry: Registry, limit: int = _MAX_NEW) -> list[dict]:
    out = []
    for c in tracking.candidates(registry, "new", limit):
        url = f"https://doi.org/{c['doi']}" if c.get("doi") else (c.get("landing") or "")
        authors = [a.get("name", "") if isinstance(a, dict) else str(a) for a in c.get("authors") or []]
        out.append({
            "id": int(c["id"]), "title": _short(c["title"], 400), "journal": _short(c.get("journal"), 120),
            "year": c.get("year"), "date": c.get("pub_date") or "", "doi": c.get("doi") or "", "url": url,
            "authors": [a for a in authors if a][:3], "preprint": bool(c.get("is_preprint")),
            "score": round(float(c.get("score") or 0), 1), "abstract": _short(c.get("abstract"), 900),
            "modelNote": _short(c.get("model_reason"), 300),
            "reasons": [t for t in (_reason_text(r) for r in c.get("reasons") or []) if t][:5],
            "sources": c.get("sources") or [],
        })
    return out


def review_items(registry: Registry, limit: int = _MAX_REVIEW) -> list[dict]:
    from . import gaps as gapmod, updates as upd, versions

    items: list[dict] = []

    def add(kind: str, label: str, title: str, detail: str = "") -> None:
        if len(items) < limit:
            items.append({"kind": kind, "label": label, "title": _short(title, 300), "detail": _short(detail, 300)})

    for p in registry.list_papers(status="doi_unverified", limit=limit):
        add("doi", "DOI 待核对", p["title"] or "", p["journal"] or "")
    for p in registry.query("SELECT title, journal FROM papers WHERE status='done' AND needs_review=1 "
                            "ORDER BY id DESC LIMIT ?", (limit,)):
        add("label", "分类置信度低", p["title"] or "", p["journal"] or "")
    for r in registry.pending_fields("open"):
        add("field", "可能的新学科", r["name_zh"] or r["name_en"] or r["slug"], f"{r['n_papers']} 篇论文")
    for u in upd.pending(registry):
        add("update", "更新卡待核对", u.get("paper_title") or "", u.get("question") or "")
    for h in gapmod.new_hits(registry):
        add("gap", "可能回答了缺口", h.get("paper_title") or "", h.get("gap") or "")
    for c in versions.checks(registry, open_only=True):
        add("version", "新版本有改动", c.get("title") or "", c.get("question") or "")
    return items


def overview(registry: Registry) -> dict:
    from . import gaps as gapmod, versions

    gapmod.ensure_schema(registry)  # candidates are matched against evidence gaps
    versions.ensure_schema(registry)
    return {"v": 1, "newPapers": new_papers(registry), "review": review_items(registry)}


def decide(registry: Registry, candidate_id: int, action: str) -> dict:
    if action not in ("later", "dismissed"):
        raise ValueError("invalid action")
    tracking.decide(registry, candidate_id, action)
    return {"ok": True}
