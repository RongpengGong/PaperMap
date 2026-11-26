"""Regenerate existing paper cards in the current card language (Settings -> Paper processing).

Only the card extraction runs again (no reclassification), one background job per paper, so the
batch resumes after the app is closed. Cards the user edited by hand are left alone, and a failed
regeneration keeps the old card. The cost is estimated before anything starts.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .i18n import OUTPUT_LANGUAGES
from .registry import Registry, now_iso

_CJK = re.compile(r"[一-鿿]")
_TRAD = set("這們與說為發於個實體點會過對應學關變區種從條網圖細進")
_SIMP = set("这们与说为发于个实体点会过对应学关变区种从条网图细进")
_GERMAN = re.compile(r"\b(und|der|die|das|mit|nicht|wurde[n]?|eine[rsnm]?|bei|zur|zum|durch|sowie)\b|[äöüß]", re.I)


def guess_language(card_json: str | None) -> str:
    """Language of a card made before PaperMap recorded it, from its text."""
    try:
        card = json.loads(card_json or "{}")
    except ValueError:
        return "zh"
    text = json.dumps(card, ensure_ascii=False)
    cjk = len(_CJK.findall(text))
    if cjk > 20:
        trad = sum(text.count(c) for c in _TRAD)
        simp = sum(text.count(c) for c in _SIMP)
        return "zh-TW" if trad > simp else "zh"
    return "de" if len(_GERMAN.findall(text)) >= 8 else "en"


def card_language(row) -> tuple[str, bool]:
    """(language, recorded): recorded is False when it was guessed from the text."""
    lang = row["card_language"] if "card_language" in row.keys() else None
    if lang in OUTPUT_LANGUAGES:
        return lang, True
    return guess_language(row["card_json"]), False


def matches(lang: str, recorded: bool, target: str) -> bool:
    if lang == target:
        return True
    # "Chinese + English terms" cannot be told apart from Chinese by its text
    return not recorded and lang == "zh" and target == "bilingual"


def overview(pipeline) -> dict:
    registry, config = pipeline.registry, pipeline.config
    target = config.content_language
    rows = registry.query("SELECT id, card_json, card_language, card_override FROM papers "
                          "WHERE status='done' AND card_json IS NOT NULL")
    by_lang: dict[str, int] = {}
    todo, edited = [], 0
    for r in rows:
        lang, recorded = card_language(r)
        by_lang[lang] = by_lang.get(lang, 0) + 1
        if matches(lang, recorded, target):
            continue
        if (r["card_override"] or "").strip():
            edited += 1
        else:
            todo.append(r["id"])
    return {"target": target, "target_label": OUTPUT_LANGUAGES.get(target, target), "total": len(rows),
            "by_lang": [(OUTPUT_LANGUAGES.get(k, k), n) for k, n in sorted(by_lang.items(), key=lambda kv: -kv[1])],
            "todo": todo, "edited": edited, "estimate": estimate(pipeline, len(todo)), "batch": batch_status(registry)}


def estimate(pipeline, n: int) -> dict:
    """Cost of n card extractions, from the average of earlier ones (or a default)."""
    from .llm import compute_cost
    from .pipeline import DEFAULT_TOKENS

    din, dout = DEFAULT_TOKENS["extract"]
    row = pipeline.registry.one("SELECT count(*) AS c, avg(prompt_tokens) AS i, avg(completion_tokens) AS o "
                                "FROM llm_calls WHERE purpose='extract' AND ok=1")
    if row and row["c"] >= 3:
        din, dout = int(row["i"]), int(row["o"])
    eff = pipeline.config.effective_llm()
    per = compute_cost(dict(eff, peak_schedule="none"), din, dout)
    subscription = eff.get("billing") == "subscription"
    return {"papers": n, "calls": n, "tokens_in": din * n, "tokens_out": dout * n, "cost": 0.0 if subscription else per * n,
            "subscription": subscription, "model": eff.get("model") or "",
            "budget": float(pipeline.config.llm.get("monthly_budget_usd") or 0), "spent": pipeline.registry.month_cost()}


def _latest_batch(registry: Registry) -> str | None:
    r = registry.one("SELECT arg FROM jobs WHERE kind='recard' ORDER BY id DESC LIMIT 1")
    return r["arg"] if r else None


def batch_status(registry: Registry) -> dict | None:
    arg = _latest_batch(registry)
    if not arg:
        return None
    counts = {r["status"]: r["n"] for r in registry.query(
        "SELECT status, count(*) AS n FROM jobs WHERE kind='recard' AND arg=? GROUP BY status", (arg,))}
    errors = [dict(r) for r in registry.query(
        "SELECT j.paper_id, j.message, p.title FROM jobs j LEFT JOIN papers p ON p.id=j.paper_id "
        "WHERE j.kind='recard' AND j.arg=? AND j.status='error' ORDER BY j.id LIMIT 10", (arg,))]
    total = sum(counts.values())
    left = counts.get("queued", 0) + counts.get("running", 0)
    return {"language": arg.split("|", 1)[0], "total": total, "done": counts.get("done", 0), "left": left,
            "failed": counts.get("error", 0), "stopped": counts.get("cancelled", 0), "running": bool(left),
            "errors": errors}


def start(pipeline) -> dict:
    """Queue one job per card that is not in the current language (edited cards are skipped)."""
    info = overview(pipeline)
    if not info["todo"]:
        return {"queued": 0, "edited": info["edited"]}
    current = batch_status(pipeline.registry)
    if current and current["running"]:
        raise ValueError("已有一批卡片正在重新生成")
    batch = f"{info['target']}|{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    for pid in info["todo"]:
        pipeline.jobs.submit("recard", pid, batch)
    pipeline.registry.log("info", f"Regenerating {len(info['todo'])} card(s) in {info['target']}")
    return {"queued": len(info["todo"]), "edited": info["edited"]}


def stop(registry: Registry) -> int:
    return registry.execute("UPDATE jobs SET status='cancelled', message='stopped by the user', updated_at=? "
                            "WHERE kind='recard' AND status='queued'", (now_iso(),)).rowcount


def run_job(pipeline, job: dict) -> str:
    """Regenerate one card. Raises on failure; the old card stays as it was."""
    from . import ocr
    from .extract import PROMPT_VERSION, extract_paper
    from .llm import BudgetExceeded, LLMNotConfigured
    from .pdf_extract import extract_for_llm

    registry, config = pipeline.registry, pipeline.config
    pid = job["paper_id"]
    target = (job["arg"] or "").split("|", 1)[0]
    row = registry.get_paper(pid)
    if row is None or row["status"] != "done":
        return "skipped: no finished card"
    if (row["card_override"] or "").strip():
        return "skipped: card edited by hand"
    lang, recorded = card_language(row)
    if matches(lang, recorded, config.content_language):
        return "skipped: already in this language"
    if target and target != config.content_language:  # the setting changed again: follow the new one
        target = config.content_language
    path = pipeline.pdf_for(pid) or next((Path(x) for x in registry.paper_file_paths(pid) if Path(x).exists()), None)
    if path is None:
        raise ValueError("找不到 PDF")
    try:
        pipeline.llm.check_budget()
        pipeline.llm._get_client()  # noqa: SLF001
    except (LLMNotConfigured, BudgetExceeded):
        stop(registry)  # every other card would fail the same way
        raise
    meta = json.loads(row["meta_json"] or "{}")
    meta.setdefault("title", row["title"])
    meta["doi"] = row["doi"]
    text = extract_for_llm(path, int(config.llm.get("max_input_chars", 60000)),
                           cache=ocr.cache_for(path, config.data_dir))
    result = extract_paper(pipeline.llm, config, meta, text, pid)
    with pipeline._lock:  # noqa: SLF001 - not at the same time as an import writing this card
        now = registry.get_paper(pid)
        if now is None or now["status"] != "done" or (now["card_override"] or "").strip():
            return "skipped: the card changed meanwhile"
        registry.update_paper(pid, card_json=result.data, prompt_version=PROMPT_VERSION, model=result.model,
                              card_language=config.content_language)
        pipeline._refresh_card(pid)  # noqa: SLF001
    pipeline.reindex(pid)
    pipeline.kb_commit(f"Regenerate card #{pid} in {config.content_language}")
    return f"card regenerated ({config.content_language}, ${result.cost_usd:.4f})"
