"""What a paired phone may see of the desktop's "new papers" and "to review" lists.

Only titles, venues and the reasons a paper was suggested leave the computer: no PDFs,
cards or notes of papers that were not shared in this session. The phone can file a
suggestion away ("later" / "not interested"). When the share also allows it, the phone can
take a suggestion in and settle the simple review items (labels, new-field candidates, gap
reminders, version checks); DOI checks and update cards still need the computer.
"""
from __future__ import annotations

import threading

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

    def add(kind: str, label: str, title: str, detail: str = "", ref: int = 0, actions=(), **extra) -> None:
        if len(items) < limit:
            items.append({"kind": kind, "label": label, "title": _short(title, 300), "detail": _short(detail, 300),
                          "id": int(ref), "actions": list(actions), **extra})

    for p in registry.list_papers(status="doi_unverified", limit=limit):
        add("doi", "DOI 待核对", p["title"] or "", p["journal"] or "", p["id"])
    for p in registry.query("SELECT id, title, journal FROM papers WHERE status='done' AND needs_review=1 "
                            "ORDER BY id DESC LIMIT ?", (limit,)):
        labels = [{"slug": r["slug"], "name": r["name_zh"] or r["name_en"] or r["slug"], "axis": r["axis"],
                   "confidence": round(float(r["confidence"] or 0), 2), "suggested": r["status"] == "suggested"}
                  for r in registry.paper_field_rows(p["id"])]
        add("label", "分类置信度低", p["title"] or "", p["journal"] or "", p["id"],
            ("accept", "keep") if any(x["suggested"] for x in labels) else ("keep",), labels=labels)
    for r in registry.pending_fields("open"):
        add("field", "可能的新学科", r["name_zh"] or r["name_en"] or r["slug"], f"{r['n_papers']} 篇论文", r["id"],
            ("establish", "merge", "reject"), papers=int(r["n_papers"]))
    for u in upd.pending(registry):
        add("update", "更新卡待核对", u.get("paper_title") or "", u.get("question") or "", u.get("id") or 0)
    for h in gapmod.new_hits(registry):
        add("gap", "可能回答了缺口", h.get("paper_title") or "", h.get("gap") or "", h["id"], ("answered", "dismissed"))
    for c in versions.checks(registry, open_only=True):
        add("version", "新版本有改动", c.get("title") or "", c.get("question") or "", c["id"], ("checked",))
    return items


def review_total(registry: Registry) -> int:
    from . import gaps as gapmod, updates as upd, versions

    row = registry.one("SELECT (SELECT count(*) FROM papers WHERE status='doi_unverified') + "
                       "(SELECT count(*) FROM papers WHERE status='done' AND needs_review=1) AS n")
    return (int(row["n"]) if row else 0) + len(registry.pending_fields("open")) + upd.pending_count(registry) \
        + gapmod.new_count(registry) + versions.open_count(registry)


def overview(registry: Registry, actions: bool = False) -> dict:
    from . import gaps as gapmod, versions

    gapmod.ensure_schema(registry)  # candidates are matched against evidence gaps
    versions.ensure_schema(registry)
    out = {"v": 1, "newPapers": new_papers(registry), "review": review_items(registry), "actions": actions,
           "totals": {"newPapers": int(tracking.counts(registry).get("new", 0)), "review": review_total(registry)}}
    if actions:  # merge targets for new-field candidates
        out["fields"] = [{"slug": f["slug"], "name": f["name_zh"] or f["name_en"] or f["slug"], "axis": f["axis"]}
                         for f in registry.active_fields()]
    return out


def decide(registry: Registry, candidate_id: int, action: str, pipeline=None) -> dict:
    """File a suggestion away; with ``pipeline`` (the share allows actions) also take it in."""
    if action == "take" and pipeline is not None:
        return _take(pipeline, candidate_id)
    if action not in ("later", "dismissed"):
        raise ValueError("invalid action")
    tracking.decide(registry, candidate_id, action)
    return {"ok": True}


def _take(pipeline, candidate_id: int, wait: float = 25.0) -> dict:
    """Look for a legal open-access PDF; a slow publisher site finishes in the background."""
    result: dict = {}

    def run():
        try:
            result.update(tracking.take_in(pipeline, candidate_id))
        except ValueError:
            result.update({"ok": False, "error": "not_found"})
        except Exception:  # noqa: BLE001 - network trouble ends in the computer's own log
            result.update({"ok": False, "error": "download_failed"})

    worker = threading.Thread(target=run, daemon=True, name="papermap-mobile-take")
    worker.start()
    worker.join(wait)
    if worker.is_alive():
        return {"ok": True, "state": "working"}
    if not result.get("ok"):
        if result.get("error") == "not_found":
            raise LookupError("candidate")
        return {"ok": False, "state": "failed"}
    if result.get("already"):
        return {"ok": True, "state": "already"}
    # Without an open-access copy the candidate waits on the computer for a PDF dragged in by hand.
    return {"ok": True, "state": "importing" if result.get("downloaded") else "needs_pdf"}


def act(pipeline, field_tasks, body: dict) -> dict:
    """One review decision from the phone. Same effects as the buttons on the computer's review page."""
    from . import gaps as gapmod, versions

    registry = pipeline.registry
    kind, action, ref = body.get("kind"), body.get("action"), body.get("id")
    if type(ref) is not int or ref <= 0 or not isinstance(kind, str) or not isinstance(action, str):
        raise ValueError("invalid review action")
    allowed = {item["id"]: item["actions"] for item in review_items(registry, limit=10_000) if item["kind"] == kind}
    if action not in allowed.get(ref, ()):
        raise LookupError("review item")  # settled elsewhere already, or not something the phone may decide
    if kind == "label":
        rows = registry.paper_field_rows(ref)
        for r in rows:
            if r["status"] == "suggested":
                pipeline.set_label(ref, r["slug"], "accept" if action == "accept" else "reject")
        registry.update_paper(ref, needs_review=0)
    elif kind == "field":
        if action == "establish":
            if field_tasks is None:
                raise ValueError("not available")
            field_tasks.establish(ref, {"slug": None, "name_zh": None, "name_en": None, "description": None,
                                        "axis": None}, body.get("handbook") is not False)
            return {"ok": True, "state": "working"}
        if action == "merge":
            target = body.get("target")
            if not isinstance(target, str) or registry.field_by_slug(target) is None:
                raise ValueError("invalid target")
            pipeline.merge_pending(ref, target)
        else:
            pipeline.reject_pending(ref)
    elif kind == "gap":
        gapmod.decide_hit(registry, ref, action)
    elif kind == "version":
        versions.decide(registry, ref)
    registry.log("info", f"Phone: {action} on review item {kind} #{ref}")
    return {"ok": True, "state": "done"}
