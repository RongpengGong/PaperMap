"""Find where a phone excerpt belongs in the computer's copy of a paper.

Used when a phone excerpt cannot be placed by exact text: the PDF on the computer was replaced
by a new version, or a phone import's text layer differs from the computer's. Text similarity is
tried first and costs nothing; the model is asked only when allowed and only to choose among the
closest paragraphs. The phone shows the suggestion and the user confirms before anything is saved.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

from . import mobile_sync

_TEXT_SURE = 92      # similarity at which a paragraph is taken without asking the model
_SHOW_AT_LEAST = 60  # below this a text match is not worth suggesting
_NEAR = 2            # pages searched on each side of the excerpt's old page
_MAX_PAGES = 80


def _norm(text: str) -> str:
    text = re.sub(r"-\s*\n\s*", "", text or "")
    return re.sub(r"\s+", " ", text).strip().lower()


def _pages_to_search(page: int, count: int) -> list[int]:
    near = [p for p in range(page - _NEAR, page + _NEAR + 1) if 0 <= p < count]
    rest = [p for p in range(min(count, _MAX_PAGES)) if p not in near]
    return near + rest


def _score(quote: str, text: str) -> float:
    a, b = _norm(quote), _norm(text)
    if not a or not b:
        return 0.0
    return float(fuzz.partial_ratio(a, b) if len(a) <= len(b) else fuzz.partial_ratio(b, a))


def suggest(config, registry, paper_id: int, revision: str, quote: str, page: int, llm=None) -> dict:
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 4000:
        raise ValueError("摘录原文无效。")
    if type(page) is not int or page < 0:
        raise ValueError("页码无效。")
    count = mobile_sync.capture(config, registry, paper_id)["pageCount"]
    candidates = []
    for p in _pages_to_search(min(page, count - 1), count):
        for seg in mobile_sync.read_page(config, registry, paper_id, revision, p)["segments"]:
            candidates.append({"page": p, "segmentId": seg["id"], "text": seg["text"], "score": _score(quote, seg["text"])})
        if candidates and max(c["score"] for c in candidates) >= 99:
            break  # an identical sentence was found; no need to read the rest of the paper
    candidates.sort(key=lambda c: (-c["score"], abs(c["page"] - page)))
    best = candidates[0] if candidates else None
    if best and best["score"] >= _TEXT_SURE:
        return {"found": True, "by": "text", **_public(best), "reason": "原文几乎相同"}
    top = [c for c in candidates[:5] if c["score"] >= 30]
    if llm is not None and top:
        choice, reason = _ask_model(llm, quote, top, paper_id)
        if choice is not None:
            return {"found": True, "by": "model", **_public(top[choice]), "reason": reason}
        return {"found": False, "by": "model", "reason": reason or "模型认为新版本里没有对应的段落"}
    if best and best["score"] >= _SHOW_AT_LEAST:
        return {"found": True, "by": "text", **_public(best), "reason": "最相近的段落，请核对"}
    return {"found": False, "by": "text", "reason": "新版本里没有找到相近的段落"}


def _public(c: dict) -> dict:
    return {"page": c["page"], "segmentId": c["segmentId"], "text": c["text"][:2000], "score": round(c["score"])}


def _ask_model(llm, quote: str, top: list[dict], paper_id: int):
    system = ("You match a reader's saved excerpt to a paragraph of a newer or differently extracted copy of the same "
              "paper. Choose the paragraph that states the same thing (wording may differ). If none does, choose -1. "
              'Reply as JSON: {"choice": <index or -1>, "reason": "<one short sentence in Chinese>"}')
    lines = [f"Saved excerpt:\n{quote.strip()[:1500]}", "", "Paragraphs:"]
    for i, c in enumerate(top):
        lines.append(f"[{i}] (page {c['page'] + 1}) {c['text'][:1200]}")

    def validate(d: dict) -> list[str]:
        ok = isinstance(d.get("choice"), int) and -1 <= d["choice"] < len(top)
        return [] if ok else ["choice must be an integer index or -1"]

    out = llm.chat_json(system, "\n".join(lines), validate, purpose="mobile_match", paper_id=paper_id, max_tokens=300)
    reason = str(out.data.get("reason") or "")[:200]
    choice = out.data["choice"]
    return (None if choice < 0 else choice), reason
