"""Periodic reviews: what I did today, weekly and monthly reports, gap pushes and a group-meeting outline.

Everything is gathered from the user's own records (reading progress, evidence excerpts, gaps, update
cards, notes, clippings, tracking candidates). Pushes ("worth following up") are built from fixed
signals - an open gap, supporting and opposing excerpts under one question, a question written in a
note - and each says why it was chosen, with links back to the source. The model is used for one
optional thing: a short summary of the period that may only cite the records it was given.

Feedback on a push reuses existing states where one exists ("resolved" on a gap closes the gap) and
down-weights similar pushes when the user marks them irrelevant. Nothing written by the user is
changed; reports and outlines are new files under knowledge/digests/.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from . import i18n
from .i18n import L
from .kb_writer import atomic_write
from .registry import Registry, now_iso
from .i18n import prose_language

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS digest_reports (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                  -- week / month
    period TEXT NOT NULL,                -- 2026-W48 / 2026-11
    start_at TEXT NOT NULL, end_at TEXT NOT NULL,
    data_json TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',    -- optional model summary (Markdown, [E#]-style refs resolved)
    kb_path TEXT, outline_path TEXT,
    model TEXT, cost_usd REAL,
    status TEXT NOT NULL DEFAULT 'ready',  -- ready / empty (nothing happened, nothing to push)
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(kind, period)
);
CREATE TABLE IF NOT EXISTS digest_feedback (
    key TEXT PRIMARY KEY,                -- gap:12 / conflict:5 / update:7 / noteq:<hash>
    kind TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,                -- followup / resolved / irrelevant
    signature TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS digest_goals (
    id INTEGER PRIMARY KEY,
    period TEXT NOT NULL,                -- the week the goal is for
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', -- open / done
    created_at TEXT NOT NULL
);
"""

KINDS = {"week": L("周报"), "month": L("月报")}
PUSH_KINDS = {"gap": L("没补上的缺口"), "conflict": L("结论冲突"), "update": L("可能相反的新论文"),
              "noteq": L("笔记里的问题")}
ACTIONS = {"followup": L("继续追问"), "resolved": L("已解决"), "irrelevant": L("不相关")}
WEEK_PUSHES = 3
MONTH_PUSHES = 1
BACKLOG = 5
MAX_GOALS = 3
SUMMARY_PROMPT_VERSION = "digest_summary_v1"
MIN_SUMMARY_FACTS = 3


def ensure_schema(registry: Registry) -> None:
    with registry._lock:  # noqa: SLF001
        registry.conn.executescript(SCHEMA)
        registry.conn.commit()


def settings(config) -> dict:
    d = config.raw.get("digest") or {}
    return {k: d.get(k, True) is not False for k in ("weekly", "monthly", "notify", "ai_summary", "daily")}


def save_settings(config, values: dict) -> dict:
    d = config.raw.setdefault("digest", {})
    for k in ("weekly", "monthly", "notify", "ai_summary", "daily"):
        if k in values:
            d[k] = bool(values[k])
    config.save()
    return settings(config)


# ------------------------------------------------------------------ periods
def _local_now() -> datetime:
    return datetime.now().astimezone()


def _at(day: date) -> datetime:
    return datetime.combine(day, time.min).astimezone()


def week_key(day: date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def period_range(kind: str, period: str) -> tuple[datetime, datetime]:
    if kind == "week":
        m = re.fullmatch(r"(\d{4})-W(\d{2})", period)
        if not m:
            raise ValueError("无效的周")
        start = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
        return _at(start), _at(start + timedelta(days=7))
    if kind == "month":
        m = re.fullmatch(r"(\d{4})-(\d{2})", period)
        if not m or not 1 <= int(m.group(2)) <= 12:
            raise ValueError("无效的月份")
        y, mo = int(m.group(1)), int(m.group(2))
        start = date(y, mo, 1)
        nxt = date(y + (mo == 12), 1 if mo == 12 else mo + 1, 1)
        return _at(start), _at(nxt)
    raise ValueError("无效的报告类型")


def current_period(kind: str, now: datetime | None = None) -> str:
    d = (now or _local_now()).date()
    return week_key(d) if kind == "week" else f"{d.year}-{d.month:02d}"


def previous_period(kind: str, now: datetime | None = None) -> str:
    start, _ = period_range(kind, current_period(kind, now))
    return current_period(kind, start - timedelta(days=1))


def next_period(kind: str, period: str) -> str:
    _, end = period_range(kind, period)
    return current_period(kind, end + timedelta(hours=12))


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _micros(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000)


def period_label(kind: str, period: str) -> str:
    start, end = period_range(kind, period)
    last = (end - timedelta(days=1)).date()
    if kind == "week":
        return f"{period} · {start.month}/{start.day}–{last.month}/{last.day}"
    return period


# ------------------------------------------------------------------ activity
def _paper_label(r) -> str:
    who = (r["first_author"] or "").split(",")[0].strip() or "?"
    return f"{who} {r['year'] or ''}".strip()


def _q(registry: Registry, sql: str, params: tuple = ()) -> list:
    """Tables owned by optional modules may not exist yet in a fresh or restored database."""
    try:
        return registry.query(sql, params)
    except Exception as exc:  # noqa: BLE001
        if "no such table" in str(exc) or "no such column" in str(exc):
            return []
        raise


def _note_questions(text: str) -> list[str]:
    out = []
    for line in (text or "").splitlines():
        s = re.sub(r"^\s*(?:[-*+>]|\d+[.)])\s*(?:\[[ xX]\]\s*)?", "", line).strip()
        if 6 <= len(s) <= 200 and (s.endswith("?") or s.endswith("？")) and not s.startswith("#"):
            out.append(s)
    return out


def activity(config, registry: Registry, start: datetime, end: datetime) -> dict:
    """Everything the user did between start and end (local datetimes). No model calls."""
    from .library import notes_path

    s, e = _utc(start), _utc(end)
    papers_added = [dict(r) for r in registry.query(
        "SELECT id, title, first_author, year FROM papers WHERE created_at >= ? AND created_at < ? "
        "AND status NOT IN ('ignored','skipped_supplement') ORDER BY created_at", (s, e))]
    read_rows = _q(registry,
                   "SELECT id, title, first_author, year, read_status, read_at FROM papers WHERE status!='ignored' AND "
                   "((reading_at >= ? AND reading_at < ?) OR (read_at >= ? AND read_at < ?)) ORDER BY id",
                   (_micros(start), _micros(end), s, e))
    papers_read = [dict(r, finished=bool(r["read_at"] and s <= r["read_at"] < e)) for r in read_rows]
    excerpts = [dict(r) for r in registry.query(
        "SELECT e.id, e.question_id, q.title AS question, e.paper_id, e.page, e.quote, e.note, e.stance, e.source, "
        "e.region_label, e.revision, e.rects_json, e.created_at, p.first_author, p.year, p.doi, p.title AS paper_title "
        "FROM evidence_entries e JOIN evidence_questions q ON q.id=e.question_id JOIN papers p ON p.id=e.paper_id "
        "WHERE e.created_at >= ? AND e.created_at < ? AND q.status='active' AND p.status!='ignored' ORDER BY e.id",
        (s, e))]
    annotations = [dict(r) for r in registry.query(
        "SELECT a.id, a.paper_id, a.page, a.text, a.note, p.first_author, p.year, p.title FROM annotations a "
        "JOIN papers p ON p.id=a.paper_id WHERE a.created_at >= ? AND a.created_at < ? AND p.status!='ignored' "
        "ORDER BY a.id", (s, e))]
    clips = [dict(r) for r in _q(registry,
             "SELECT c.id, c.text, c.note, c.paper_id, c.page, p.first_author, p.year FROM clips c "
             "LEFT JOIN papers p ON p.id=c.paper_id WHERE c.created_at >= ? AND c.created_at < ? ORDER BY c.id", (s, e))]
    gaps_closed = [dict(r) for r in _q(registry,
                   "SELECT g.id, g.text, g.question_id, q.title AS question FROM question_gaps g "
                   "JOIN evidence_questions q ON q.id=g.question_id WHERE g.closed_at >= ? AND g.closed_at < ?", (s, e))]
    gaps_added = [dict(r) for r in _q(registry,
                  "SELECT g.id, g.text, g.question_id, q.title AS question FROM question_gaps g "
                  "JOIN evidence_questions q ON q.id=g.question_id WHERE g.created_at >= ? AND g.created_at < ?", (s, e))]
    updates = [dict(r) for r in _q(registry,
               "SELECT u.id, u.paper_id, u.question_id, q.title AS question, u.outcome, u.headline, u.status, "
               "u.user_text, u.decided_at, p.first_author, p.year FROM update_cards u "
               "JOIN evidence_questions q ON q.id=u.question_id JOIN papers p ON p.id=u.paper_id "
               "WHERE u.decided_at >= ? AND u.decided_at < ? AND u.status IN ('confirmed','rewritten','kept')", (s, e))]
    judgments = [dict(r) for r in _q(registry,
                 "SELECT id, title, judgment, judgment_at FROM evidence_questions WHERE status='active' "
                 "AND judgment_at >= ? AND judgment_at < ?", (s, e))]
    notes, note_questions = [], []
    lo, hi = start.timestamp(), end.timestamp()
    for r in registry.query("SELECT id, title, first_author, year, kb_path FROM papers "
                            "WHERE kb_path IS NOT NULL AND status!='ignored'"):
        try:
            f = notes_path(config.knowledge_dir, r["kb_path"])
            mt = f.stat().st_mtime
        except (OSError, ValueError):
            continue
        if lo <= mt < hi:
            text = f.read_text("utf-8", errors="replace")
            notes.append({"paper_id": r["id"], "title": r["title"], "label": _paper_label(r)})
            note_questions += [{"text": q, "paper_id": r["id"], "label": _paper_label(r), "origin": "note"}
                               for q in _note_questions(text)]
    for c in clips:
        note_questions += [{"text": q, "paper_id": c["paper_id"], "label": _paper_label(c) if c["paper_id"] else "",
                            "origin": "clip"} for q in _note_questions(c["note"])]
    for a in annotations:
        note_questions += [{"text": q, "paper_id": a["paper_id"], "label": _paper_label(a), "origin": "annotation"}
                           for q in _note_questions(a["note"])]
    return {"papers_added": papers_added, "papers_read": papers_read, "excerpts": excerpts,
            "annotations": annotations, "clips": clips, "notes": notes, "note_questions": note_questions,
            "gaps_closed": gaps_closed, "gaps_added": gaps_added, "updates": updates, "judgments": judgments}


def counts(act: dict) -> dict:
    ex = act["excerpts"]
    return {"added": len(act["papers_added"]), "read": len(act["papers_read"]),
            "finished": sum(1 for p in act["papers_read"] if p["finished"]),
            "excerpts": len(ex), "supports": sum(1 for x in ex if x["stance"] == "supports"),
            "opposes": sum(1 for x in ex if x["stance"] == "opposes"),
            "pending": sum(1 for x in ex if x["stance"] == "pending"),
            "annotations": len(act["annotations"]), "clips": len(act["clips"]), "notes": len(act["notes"]),
            "gaps_closed": len(act["gaps_closed"]), "gaps_added": len(act["gaps_added"]),
            "updates": len(act["updates"]), "judgments": len(act["judgments"])}


def is_empty(c: dict) -> bool:
    return not any(c[k] for k in ("added", "read", "excerpts", "annotations", "clips", "notes", "gaps_closed",
                                  "gaps_added", "updates", "judgments"))


def today(config, registry: Registry, now: datetime | None = None) -> dict | None:
    """What I did today, shown on the home page: a plain list, no model."""
    ensure_schema(registry)
    now = now or _local_now()
    start = _at(now.date())
    act = activity(config, registry, start, start + timedelta(days=1))
    c = counts(act)
    if is_empty(c):
        return None
    items: list[dict] = []
    seen: set = set()

    def add(kind, text, url, extra="", paper=None):
        # one line per paper (reading it, a note on it and adding it are the same paper)
        key = ("paper", paper) if paper is not None else (kind, url)
        if key in seen or len(items) >= 5:
            return
        seen.add(key)
        items.append({"kind": kind, "text": text, "url": url, "extra": extra})

    from .evidence import anchor_path

    for x in act["excerpts"][:2]:
        add("excerpt", x["quote"][:120], anchor_path(x), f"{_paper_label(x)} · p.{x['page'] + 1} · {x['question']}")
    for p in act["papers_read"]:
        add("read", p["title"] or "", f"/?id={p['id']}", _paper_label(p), p["id"])
    for n in act["notes"]:
        add("note", n["title"] or "", f"/?id={n['paper_id']}&tab=notes", n["label"], n["paper_id"])
    for p in act["papers_added"]:
        add("added", p["title"] or "", f"/?id={p['id']}", _paper_label(p), p["id"])
    for cl in act["clips"]:
        add("clip", cl["text"][:120], f"/clips#clip-{cl['id']}", "")
    return {"counts": c, "items": items, "questions": [q["text"] for q in act["note_questions"]][:3]}


# ------------------------------------------------------------------ feedback
def feedback_map(registry: Registry) -> dict[str, dict]:
    ensure_schema(registry)
    return {r["key"]: dict(r) for r in registry.query("SELECT * FROM digest_feedback")}


def give_feedback(registry: Registry, key: str, action: str, signature: str = "") -> dict:
    """followup / resolved / irrelevant / clear. 'resolved' on a gap closes the gap itself."""
    ensure_schema(registry)
    m = re.fullmatch(r"(gap|conflict|update|noteq):([\w-]{1,40})", key or "")
    if not m or action not in (*ACTIONS, "clear"):
        raise ValueError("无效的操作")
    kind, ref = m.groups()
    if action == "clear":
        registry.execute("DELETE FROM digest_feedback WHERE key=?", (key,))
        return {"key": key, "action": None}
    scope = ref
    if kind == "gap":
        from . import gaps

        g = gaps.get_gap(registry, int(ref)) if ref.isdigit() else None
        if g is None:
            raise ValueError("这个缺口已经不存在")
        scope = str(g["question_id"])
        if action == "resolved":
            gaps.set_gap_status(registry, g["id"], "closed")
    elif kind == "update":
        row = registry.one("SELECT question_id FROM update_cards WHERE id=?", (int(ref),)) if ref.isdigit() else None
        if row is None:
            raise ValueError("这张更新卡已经不存在")
        scope = str(row["question_id"])
    registry.execute(
        "INSERT INTO digest_feedback(key, kind, scope, action, signature, created_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET action=excluded.action, signature=excluded.signature, "
        "created_at=excluded.created_at", (key, kind, scope, action, (signature or "")[:200], now_iso()))
    return {"key": key, "action": action}


# ------------------------------------------------------------------ pushes
def _hash(text: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", text.strip().lower()).encode("utf-8")).hexdigest()[:12]


def push_candidates(config, registry: Registry, act: dict, start: datetime, end: datetime) -> list[dict]:
    """Every possible push, scored, before feedback. Each has key, kind, why (+args) and links."""
    from .evidence import anchor_path

    s, e = _utc(start), _utc(end)
    out: list[dict] = []
    active_q = {x["question_id"] for x in act["excerpts"]} | {g["question_id"] for g in act["gaps_added"]}
    open_track = []
    for t in _q(registry, "SELECT id, title, gaps_json FROM track_candidates WHERE status='new' AND gaps_json != '[]' "
                          "ORDER BY score DESC"):
        try:
            open_track.append({"id": t["id"], "title": t["title"], "gaps": set(json.loads(t["gaps_json"] or "[]"))})
        except ValueError:
            continue
    # 1. open gaps: papers that may answer them, tracking candidates that touched them
    for g in _q(registry, "SELECT g.id, g.text, g.question_id, q.title AS question FROM question_gaps g "
                          "JOIN evidence_questions q ON q.id=g.question_id WHERE g.status='open' AND q.status='active'"):
        hits = _q(registry, "SELECT h.paper_id, h.passages_json, p.first_author, p.year, p.title FROM gap_hits h "
                            "JOIN papers p ON p.id=h.paper_id WHERE h.gap_id=? AND h.status IN ('new','seen') "
                            "AND p.status!='ignored' ORDER BY h.score DESC LIMIT 2", (g["id"],))
        track = [t for t in open_track if g["id"] in t["gaps"]][:2]
        links = [{"label": L("打开证据笔记"), "text": "", "url": f"/evidence?question={g['question_id']}"}]
        for h in hits:
            page = None
            try:
                page = (json.loads(h["passages_json"] or "[]") or [{}])[0].get("page")
            except (ValueError, AttributeError, IndexError):
                page = None
            url = f"/reader/{h['paper_id']}" + (f"?page={int(page) + 1}" if isinstance(page, int) else "")
            links.append({"label": "", "text": _paper_label(h) + (f" · p.{int(page) + 1}" if isinstance(page, int) else ""), "url": url})
        for t in track:
            links.append({"label": L("新文献"), "text": (t["title"] or "")[:70], "url": f"/tracking#cand-{t['id']}"})
        score = 2.0 + (3 if hits else 0) + (1 if track else 0) + (1 if g["question_id"] in active_q else 0)
        if hits:
            why = L("你在「{q}」下记下的缺口「{gap}」还没补上；库里有 {n} 篇可能回答它，先读标出的段落。")
        elif track:
            why = L("你在「{q}」下记下的缺口「{gap}」还没补上；新文献里有 {n} 篇标题摘要碰到它。")
        else:
            why = L("你在「{q}」下记下的缺口「{gap}」还没补上，这段时间这个问题有新动静。")
            if g["question_id"] not in active_q:
                continue  # a quiet, unanswered gap is not news
        out.append({"key": f"gap:{g['id']}", "kind": "gap", "scope": str(g["question_id"]), "score": score,
                    "why": why, "args": {"q": g["question"], "gap": g["text"], "n": len(hits) or len(track)},
                    "signature": f"h{len(hits)}t{len(track)}", "links": links})
    # 2. supporting and opposing excerpts under one question
    for q in registry.query("SELECT id, title FROM evidence_questions WHERE status='active'"):
        rows = registry.query(
            "SELECT e.id, e.paper_id, e.page, e.quote, e.stance, e.created_at, p.first_author, p.year FROM evidence_entries e "
            "JOIN papers p ON p.id=e.paper_id WHERE e.question_id=? AND e.stance IN ('supports','opposes') "
            "AND p.status!='ignored' ORDER BY e.created_at DESC", (q["id"],))
        sup = [r for r in rows if r["stance"] == "supports"]
        opp = [r for r in rows if r["stance"] == "opposes"]
        if not sup or not opp:
            continue
        fresh = any(s <= r["created_at"] < e for r in rows)
        if not fresh:
            continue
        a, b = sup[0], opp[0]
        out.append({"key": f"conflict:{q['id']}", "kind": "conflict", "scope": str(q["id"]), "score": 4.0 + (len(opp) > 1),
                    "why": L("「{q}」下既有支持也有相反的摘录（{a} 与 {b}）。可以先对照两篇的方法段，看研究对象和条件是否可比。"),
                    "args": {"q": q["title"], "a": _paper_label(a), "b": _paper_label(b)},
                    "signature": f"s{len(sup)}o{len(opp)}",
                    "links": [{"label": L("证据对照"), "text": "", "url": f"/evidence?question={q['id']}&view=compare"},
                              {"label": L("支持"), "text": f"{_paper_label(a)} · p.{a['page'] + 1}", "url": anchor_path(a)},
                              {"label": L("相反"), "text": f"{_paper_label(b)} · p.{b['page'] + 1}", "url": anchor_path(b)}],
                    "quotes": [{"stance": "supports", "text": a["quote"][:220]}, {"stance": "opposes", "text": b["quote"][:220]}]})
    # 3. a new paper the model thinks contradicts the user's judgment, still unchecked
    for u in _q(registry, "SELECT u.id, u.paper_id, u.question_id, q.title AS question, u.headline, p.first_author, p.year "
                          "FROM update_cards u JOIN evidence_questions q ON q.id=u.question_id JOIN papers p ON p.id=u.paper_id "
                          "WHERE u.outcome='contradicts' AND u.status='pending' AND q.status='active' AND p.status!='ignored'"):
        out.append({"key": f"update:{u['id']}", "kind": "update", "scope": str(u["question_id"]), "score": 4.5,
                    "why": L("模型认为 {a} 可能与你在「{q}」下的判断相反，还待你核对：{h}"),
                    "args": {"a": _paper_label(u), "q": u["question"], "h": u["headline"]}, "signature": "",
                    "links": [{"label": L("核对更新卡"), "text": "", "url": f"/?id={u['paper_id']}&tab=updates"}]})
    # 4. questions the user wrote in notes, clippings and highlights
    titles = {_hash(r["title"]) for r in registry.query("SELECT title FROM evidence_questions")}
    seen: set = set()
    for nq in act["note_questions"]:
        h = _hash(nq["text"])
        if h in seen or h in titles:
            continue
        seen.add(h)
        links = [{"label": "" if nq["label"] else L("打开"), "text": nq["label"], "url": f"/?id={nq['paper_id']}&tab=notes" if nq["paper_id"] else "/clips"}]
        out.append({"key": f"noteq:{h}", "kind": "noteq", "scope": str(nq["paper_id"] or ""), "score": 3.0,
                    "why": L("你在笔记里问过：「{note}」。这个问题还没有对应的研究问题或缺口，可以把它记成缺口，导入新论文时会提醒你。"),
                    "args": {"note": nq["text"]}, "signature": "", "links": links})
    return out


def pick_pushes(registry: Registry, cands: list[dict], limit: int) -> list[dict]:
    """Apply feedback (skip irrelevant / resolved-and-unchanged, boost follow-ups, down-weight
    places marked irrelevant), then take the best few, no more than two of one kind."""
    fb = feedback_map(registry)
    irrelevant_scopes: dict[tuple, int] = {}
    for f in fb.values():
        if f["action"] == "irrelevant":
            irrelevant_scopes[(f["kind"], f["scope"])] = irrelevant_scopes.get((f["kind"], f["scope"]), 0) + 1
    ranked = []
    for c in cands:
        f = fb.get(c["key"])
        if f and f["action"] == "irrelevant":
            continue
        if f and f["action"] == "resolved" and f["signature"] == c["signature"]:
            continue
        score = c["score"] * (0.5 ** irrelevant_scopes.get((c["kind"], c["scope"]), 0))
        if f and f["action"] == "followup":
            score += 2
            c = dict(c, followed=True)
        ranked.append((score, c))
    ranked.sort(key=lambda x: -x[0])
    out, per_kind = [], {}
    for score, c in ranked:
        if per_kind.get(c["kind"], 0) >= 2:
            continue
        per_kind[c["kind"]] = per_kind.get(c["kind"], 0) + 1
        out.append(dict(c, score=round(score, 2)))
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ backlog, goals, extras
def backlog(registry: Registry, limit: int = BACKLOG) -> list[dict]:
    """Unread papers worth reading first: ranked by how they touch the user's open gaps and questions."""
    rows = registry.query("SELECT id, title, first_author, year, starred, read_status FROM papers "
                          "WHERE status='done' AND read_status != 'read'")
    if not rows:
        return []
    hits: dict[int, int] = {}
    for r in _q(registry, "SELECT h.paper_id, count(*) AS n FROM gap_hits h JOIN question_gaps g ON g.id=h.gap_id "
                          "WHERE h.status IN ('new','seen') AND g.status='open' GROUP BY h.paper_id"):
        hits[r["paper_id"]] = r["n"]
    toread = {r["paper_id"] for r in _q(registry, "SELECT paper_id FROM paper_purposes WHERE tag='toread'")}
    q_fields = {r["paper_id"] for r in _q(registry,
                "SELECT DISTINCT pf.paper_id FROM paper_fields pf JOIN evidence_question_fields qf ON qf.field_id=pf.field_id "
                "JOIN evidence_questions q ON q.id=qf.question_id WHERE q.status='active' AND pf.status='accepted'")}
    out = []
    for r in rows:
        reasons, score = [], 0.0
        if hits.get(r["id"]):
            score += 3 * hits[r["id"]]
            reasons.append(L("可能回答你记下的缺口"))
        if r["id"] in q_fields:
            score += 1
            reasons.append(L("属于你研究问题挂着的学科"))
        if r["id"] in toread:
            score += 1.5
            reasons.append(L("你标了「待精读」"))
        if r["starred"]:
            score += 1
            reasons.append(L("星标"))
        if r["read_status"] == "reading":
            score += 0.5
            reasons.append(L("读到一半"))
        if score:
            out.append({"id": r["id"], "title": r["title"], "label": _paper_label(r), "score": score, "reasons": reasons})
    out.sort(key=lambda x: (-x["score"], -x["id"]))
    return out[:limit]


def missed(registry: Registry, limit: int = 3) -> list[dict]:
    """Works cited by several library papers but not in the library yet (stage 6 citation network)."""
    try:
        from . import citations

        return [{"title": w["title"], "doi": w["doi"], "year": w["year"], "n": w["n"]}
                for w in citations.missing_key_papers(registry, 2, limit)]
    except Exception:  # noqa: BLE001 - citation data is optional
        return []


_WORD = re.compile(r"[A-Za-z][A-Za-z0-9\-+]{2,}")
_STOP = {"the", "and", "for", "with", "this", "that", "what", "how", "why", "are", "was", "from", "into", "does"}
_NOT_HAN = re.compile("[^" + chr(0x4E00) + "-" + chr(0x9FFF) + "]")


def _tokens(text: str) -> tuple[set[str], set[str]]:
    """Latin words (gene, method, disease names) and Chinese character pairs."""
    words = {w.lower() for w in _WORD.findall(text or "") if w.lower() not in _STOP}
    han = _NOT_HAN.sub(" ", text or "")
    grams = {seg[i:i + 2] for seg in han.split() for i in range(len(seg) - 1)}
    return words, grams


def _touches(goal: tuple[set[str], set[str]], text: str) -> bool:
    """A record touches a goal when it shares the goal's specific terms (up to two of them), or
    several Chinese word pairs. Goals are often Chinese with English terms, records often English."""
    words, grams = goal
    tw, tg = _tokens(text)
    if words and len(words & tw) >= min(2, len(words)):
        return True
    return bool(grams) and len(grams & tg) >= (2 if len(grams) <= 4 else 3)


def goals(registry: Registry, period: str) -> list[dict]:
    ensure_schema(registry)
    return [dict(r) for r in registry.query("SELECT * FROM digest_goals WHERE period=? ORDER BY id", (period,))]


def add_goal(registry: Registry, period: str, text: str) -> int:
    ensure_schema(registry)
    period_range("week", period)
    text = " ".join((text or "").split())[:300]
    if len(text) < 2:
        raise ValueError("请写下想弄清的事")
    if len(goals(registry, period)) >= MAX_GOALS:
        raise ValueError("每周最多写 3 件")
    return int(registry.execute("INSERT INTO digest_goals(period, text, created_at) VALUES (?,?,?)",
                                (period, text, now_iso())).lastrowid)


def set_goal(registry: Registry, gid: int, status: str | None = None, delete: bool = False) -> None:
    if delete:
        registry.execute("DELETE FROM digest_goals WHERE id=?", (gid,))
    elif status in ("open", "done"):
        registry.execute("UPDATE digest_goals SET status=? WHERE id=?", (status, gid))
    else:
        raise ValueError("无效的操作")


def goal_progress(registry: Registry, period: str, act: dict) -> list[dict]:
    """For each goal set for this week: the records of the week that touch it (word overlap, no model)."""
    from .evidence import anchor_path

    pool = []
    for x in act["excerpts"]:
        pool.append((x["question"] + " " + x["quote"] + " " + (x["note"] or ""), x["quote"][:100], anchor_path(x)))
    for p in act["papers_read"] + act["papers_added"]:
        pool.append((p["title"] or "", (p["title"] or "")[:100], f"/?id={p['id']}"))
    for c in act["clips"]:
        pool.append((c["text"] + " " + (c["note"] or ""), c["text"][:100], f"/clips#clip-{c['id']}"))
    for g in act["gaps_closed"]:
        pool.append((g["question"] + " " + g["text"], g["text"][:100], f"/evidence?question={g['question_id']}"))
    out = []
    for g in goals(registry, period):
        want = _tokens(g["text"])
        matched, urls = [], set()
        for text, label, url in pool:
            if url not in urls and _touches(want, text):
                urls.add(url)
                matched.append({"label": label, "url": url})
        out.append({**g, "matched": matched[:3], "n": len(matched)})
    return out


def tracking_items(registry: Registry, start: datetime, end: datetime, limit: int = 5) -> list[dict]:
    rows = _q(registry, "SELECT id, title, score, model_reason, pub_date FROM track_candidates WHERE status='new' "
                        "AND created_at >= ? AND created_at < ? ORDER BY score DESC, id DESC LIMIT ?",
              (_utc(start), _utc(end), limit))
    return [dict(r) for r in rows]


def clip_digests(registry: Registry, start: datetime, end: datetime) -> list[dict]:
    return [dict(r) for r in _q(registry, "SELECT id, kb_path, created_at FROM clip_digests WHERE created_at >= ? "
                                          "AND created_at < ? ORDER BY id", (_utc(start), _utc(end)))]


def todo(registry: Registry) -> dict:
    """Things waiting for the user's own check (links only)."""
    one = lambda sql: (_q(registry, sql) or [{"n": 0}])[0]["n"]  # noqa: E731
    return {"update_cards": one("SELECT count(*) AS n FROM update_cards WHERE status='pending'"),
            "version_checks": one("SELECT count(DISTINCT paper_id) AS n FROM version_checks WHERE decided IS NULL"),
            "gap_hits": one("SELECT count(*) AS n FROM gap_hits WHERE status='new'")}


# ------------------------------------------------------------------ monthly views
def question_progress(registry: Registry, act: dict) -> list[dict]:
    by_q: dict[int, dict] = {}

    def q(qid, title):
        return by_q.setdefault(qid, {"id": qid, "title": title, "supports": 0, "opposes": 0, "pending": 0,
                                     "gaps_closed": [], "gaps_added": [], "judgment": None, "updates": []})

    for x in act["excerpts"]:
        q(x["question_id"], x["question"])[x["stance"] if x["stance"] in ("supports", "opposes") else "pending"] += 1
    for g in act["gaps_closed"]:
        q(g["question_id"], g["question"])["gaps_closed"].append(g["text"])
    for g in act["gaps_added"]:
        q(g["question_id"], g["question"])["gaps_added"].append(g["text"])
    for j in act["judgments"]:
        q(j["id"], j["title"])["judgment"] = j["judgment"]
    for u in act["updates"]:
        q(u["question_id"], u["question"])["updates"].append({"label": _paper_label(u), "status": u["status"],
                                                               "text": u["user_text"] or u["headline"]})
    return sorted(by_q.values(), key=lambda x: -(x["supports"] + x["opposes"] + x["pending"] + len(x["gaps_closed"])))


def revisits(registry: Registry, end: datetime, min_age_days: int = 21) -> list[dict]:
    """Judgments confirmed a while ago that have new evidence since (opposing evidence first)."""
    out = []
    cutoff = _utc(end - timedelta(days=min_age_days))
    for q in _q(registry, "SELECT id, title, judgment, judgment_at FROM evidence_questions WHERE status='active' "
                          "AND judgment != ''"):
        since = q["judgment_at"]
        if not since:
            last = _q(registry, "SELECT max(decided_at) AS t FROM update_cards WHERE question_id=? "
                                "AND status IN ('confirmed','rewritten','kept')", (q["id"],))
            since = last[0]["t"] if last else None
        if not since or since > cutoff:
            continue
        rows = registry.query("SELECT stance, count(*) AS n FROM evidence_entries WHERE question_id=? AND created_at > ? "
                              "AND created_at < ? GROUP BY stance", (q["id"], since, _utc(end)))
        n = {r["stance"]: r["n"] for r in rows}
        total = sum(n.values())
        if total:
            out.append({"id": q["id"], "title": q["title"], "judgment": q["judgment"], "since": since[:10],
                        "new": total, "opposes": n.get("opposes", 0), "supports": n.get("supports", 0)})
    out.sort(key=lambda x: (-x["opposes"], -x["new"]))
    return out


def ready_to_write(registry: Registry) -> list[dict]:
    """Questions with enough checked evidence for a paragraph: >= 3 papers, both sides or >= 4 papers."""
    out = []
    for q in registry.query("SELECT id, title FROM evidence_questions WHERE status='active'"):
        rows = registry.query("SELECT DISTINCT e.paper_id, e.stance FROM evidence_entries e JOIN papers p ON p.id=e.paper_id "
                              "WHERE e.question_id=? AND e.stance IN ('supports','opposes') AND p.status!='ignored'", (q["id"],))
        papers = sorted({r["paper_id"] for r in rows})
        stances = {r["stance"] for r in rows}
        if len(papers) >= 3 and (len(stances) == 2 or len(papers) >= 4):
            out.append({"id": q["id"], "title": q["title"], "papers": papers, "both": len(stances) == 2,
                        "url": "/write?ids=" + ",".join(map(str, papers))})
    return out


# ------------------------------------------------------------------ model summary
def _facts(act: dict) -> tuple[list[str], dict[str, str]]:
    from .evidence import anchor_path

    lines, links = [], {}
    for x in act["excerpts"][:60]:
        k = f"E{x['id']}"
        stance = {"supports": "supports", "opposes": "opposes"}.get(x["stance"], "unchecked")
        lines.append(f"[{k}] question: {x['question']} | {stance} | {_paper_label(x)} p.{x['page'] + 1}: "
                     f"{x['quote'][:300]}" + (f" | my note: {x['note'][:200]}" if x["note"] else ""))
        links[k] = anchor_path(x)
    for c in act["clips"][:40]:
        k = f"C{c['id']}"
        lines.append(f"[{k}] clipping: {c['text'][:300]}" + (f" | my note: {c['note'][:200]}" if c["note"] else ""))
        links[k] = f"/clips#clip-{c['id']}"
    for p in act["papers_read"][:20]:
        k = f"P{p['id']}"
        lines.append(f"[{k}] read paper: {p['title']}")
        links[k] = f"/?id={p['id']}"
    for p in act["papers_added"][:20]:
        k = f"P{p['id']}"
        if k not in links:
            lines.append(f"[{k}] added to the library: {p['title']}")
            links[k] = f"/?id={p['id']}"
    for g in act["gaps_closed"]:
        k = f"G{g['id']}"
        lines.append(f"[{k}] closed evidence gap under '{g['question']}': {g['text']}")
        links[k] = f"/evidence?question={g['question_id']}"
    for u in act["updates"]:
        k = f"U{u['id']}"
        lines.append(f"[{k}] confirmed change for '{u['question']}' ({u['outcome']}): {u['user_text'] or u['headline']}")
        links[k] = f"/?id={u['paper_id']}&tab=updates"
    return lines, links


def summary_system(config) -> str:
    language = prose_language(config)
    return "\n".join([
        "You write a short periodic review of a researcher's own reading records.",
        f"Write in {language}, as Markdown: 3-6 bullets on what they worked on and learned in this period.",
        "End every bullet with the record ids it rests on, e.g. [E12][C3].",
        "Use ONLY the records given; add no outside facts. Do not tell the researcher what they lack or must learn; "
        "you may note where their own records disagree or leave a question open.",
    ])


def estimate_summary(config, registry: Registry, act: dict) -> dict:
    from .llm import compute_cost

    lines, _ = _facts(act)
    if len(lines) < MIN_SUMMARY_FACTS:
        return {"calls": 0, "cost": 0.0}
    tin = (len(summary_system(config)) + sum(len(x) for x in lines)) // 3 + 50
    try:
        cost = compute_cost(config.effective_llm(), tin, 600)
    except Exception:  # noqa: BLE001
        cost = 0.0
    return {"calls": 1, "cost": cost, "subscription": config.effective_llm().get("billing") == "subscription"}


def summarize(config, llm, act: dict) -> dict | None:
    lines, links = _facts(act)
    if len(lines) < MIN_SUMMARY_FACTS:
        return None
    res = llm.chat_text(summary_system(config), "RECORDS:\n" + "\n".join(lines), purpose="digest_summary", max_tokens=1500)
    text = re.sub(r"\[([ECPGU]\d+)\]", lambda m: f"[{m.group(1)}]({links[m.group(1)]})" if m.group(1) in links else "",
                  res.raw).strip()
    return {"text": text, "model": res.model, "cost": res.cost_usd}


# ------------------------------------------------------------------ build and store
def build(config, registry: Registry, kind: str, period: str) -> dict:
    """All sections of one report (no model)."""
    ensure_schema(registry)
    start, end = period_range(kind, period)
    act = activity(config, registry, start, end)
    c = counts(act)
    cands = push_candidates(config, registry, act, start, end)
    data = {"kind": kind, "period": period, "label": period_label(kind, period), "counts": c,
            "pushes": pick_pushes(registry, cands, WEEK_PUSHES if kind == "week" else MONTH_PUSHES),
            "excerpts": _excerpt_groups(act["excerpts"]),
            "read": [{"id": p["id"], "title": p["title"], "label": _paper_label(p), "finished": p["finished"]}
                     for p in act["papers_read"]],
            "added": [{"id": p["id"], "title": p["title"], "label": _paper_label(p)} for p in act["papers_added"]],
            "tracking": tracking_items(registry, start, end, 5),
            "clip_digests": clip_digests(registry, start, end),
            "todo": todo(registry), "backlog": backlog(registry), "missed": missed(registry)}
    if kind == "week":
        data["goals"] = goal_progress(registry, period, act)
        data["next_period"] = next_period("week", period)
    else:
        data["questions"] = question_progress(registry, act)
        data["revisits"] = revisits(registry, end)
        data["ready"] = ready_to_write(registry)
    data["empty"] = is_empty(c) and not data["pushes"]
    return data


def _excerpt_groups(excerpts: list[dict]) -> list[dict]:
    from .evidence import anchor_path

    groups: dict[int, dict] = {}
    for x in excerpts:
        g = groups.setdefault(x["question_id"], {"id": x["question_id"], "title": x["question"], "items": []})
        g["items"].append({"id": x["id"], "stance": x["stance"], "label": _paper_label(x), "page": x["page"] + 1,
                           "quote": x["quote"][:260], "note": (x["note"] or "")[:200], "url": anchor_path(x),
                           "region": x["source"] == "region", "region_label": x["region_label"]})
    return list(groups.values())


def get(registry: Registry, rid: int) -> dict | None:
    ensure_schema(registry)
    r = registry.one("SELECT * FROM digest_reports WHERE id=?", (rid,))
    return _report(r) if r else None


def find(registry: Registry, kind: str, period: str) -> dict | None:
    ensure_schema(registry)
    r = registry.one("SELECT * FROM digest_reports WHERE kind=? AND period=?", (kind, period))
    return _report(r) if r else None


def _report(r) -> dict:
    d = dict(r)
    d["data"] = json.loads(d.pop("data_json"))
    return d


def history(registry: Registry, limit: int = 40) -> list[dict]:
    ensure_schema(registry)
    return [dict(r) for r in registry.query("SELECT id, kind, period, status, created_at, kb_path, outline_path "
                                            "FROM digest_reports ORDER BY start_at DESC, kind LIMIT ?", (limit,))]


def generate(config, registry: Registry, llm, kind: str, period: str, use_model: bool | None = None) -> dict:
    """Build (or rebuild) one report, write its Markdown copy and store it."""
    ensure_schema(registry)
    start, end = period_range(kind, period)
    data = build(config, registry, kind, period)
    summary = None
    if use_model is None:
        use_model = settings(config)["ai_summary"]
    if use_model and not data["empty"] and config.model_ready():
        try:
            summary = summarize(config, llm, activity(config, registry, start, end))
        except Exception as exc:  # noqa: BLE001 - the summary is optional; the report stands without it
            log.warning("digest summary failed: %s", exc)
            registry.log("warning", f"Review summary skipped: {str(exc)[:200]}")
    rel = f"digests/{period}.md"
    atomic_write(config.knowledge_dir / rel, markdown(config, data, summary["text"] if summary else ""))
    now = now_iso()
    registry.execute(
        "INSERT INTO digest_reports(kind, period, start_at, end_at, data_json, summary, kb_path, model, cost_usd, status, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(kind, period) DO UPDATE SET "
        "data_json=excluded.data_json, summary=CASE WHEN excluded.model IS NULL THEN digest_reports.summary "
        "ELSE excluded.summary END, model=coalesce(excluded.model, digest_reports.model), "
        "cost_usd=coalesce(excluded.cost_usd, digest_reports.cost_usd), kb_path=excluded.kb_path, "
        "status=excluded.status, updated_at=excluded.updated_at",
        (kind, period, _utc(start), _utc(end), json.dumps(data, ensure_ascii=False), summary["text"] if summary else "",
         rel, summary["model"] if summary else None, summary["cost"] if summary else None,
         "empty" if data["empty"] else "ready", now, now))
    return find(registry, kind, period)


def maybe_auto(pipeline, now: datetime | None = None, send=None) -> list[dict]:
    """After a week or month has ended, write its report once and send one notification."""
    from . import notify

    config, registry = pipeline.config, pipeline.registry
    ensure_schema(registry)
    st = settings(config)
    made = []
    for kind in ("week", "month"):
        if not st["weekly" if kind == "week" else "monthly"]:
            continue
        period = previous_period(kind, now)
        if find(registry, kind, period):
            continue
        rep = generate(config, registry, pipeline.llm, kind, period)
        made.append(rep)
        registry.log("info", f"{'Weekly' if kind == 'week' else 'Monthly'} review written: {period}")
        if st["notify"] and rep["status"] == "ready":
            lang = i18n.config_language(config)
            tr = lambda s: i18n.translate(lang, s)  # noqa: E731
            c, n = rep["data"]["counts"], len(rep["data"]["pushes"])
            title = "PaperMap · " + tr(L("上周回顾") if kind == "week" else L("上月回顾"))
            body = tr(L("读了 {read} 篇，新增 {ex} 条摘录；{n} 条值得追问")).format(read=c["read"], ex=c["excerpts"], n=n)
            if kind == "week" and rep["data"]["tracking"]:
                body += tr(L("；{k} 篇新文献")).format(k=len(rep["data"]["tracking"]))
            (send or notify.send)(title, body)
    return made


def weekly_owns_notifications(config) -> bool:
    """With weekly reviews on, the separate weekly new-literature notification is folded into it."""
    st = settings(config)
    return st["weekly"] and st["notify"]


# ------------------------------------------------------------------ Markdown copies
def _ml(config):
    en = config.output_language == "en"
    def t(text, /, **kw):
        return (i18n.translate("en", text) if en else text).format(**kw)

    return t


def markdown(config, data: dict, summary: str = "") -> str:
    t = _ml(config)
    c = data["counts"]
    title = t("周报") if data["kind"] == "week" else t("月报")
    out = [f"# {title} · {data['label']}", "",
           t("读了 {read} 篇（读完 {fin} 篇），新入库 {added} 篇；新增摘录 {ex} 条（支持 {s} / 相反 {o} / 待核对 {p}）；"
             "收藏 {cl} 条；补上缺口 {gc} 个。", read=c["read"], fin=c["finished"], added=c["added"], ex=c["excerpts"],
             s=c["supports"], o=c["opposes"], p=c["pending"], cl=c["clips"], gc=c["gaps_closed"]), ""]
    if summary:
        out += [f"## {t('AI 小结（只根据本期记录）')}", "", summary, ""]
    if data["pushes"]:
        out += [f"## {t('值得追问')}", ""]
        for p in data["pushes"]:
            out.append(f"- **{t(PUSH_KINDS[p['kind']])}**：{t(p['why'], **p['args'])}")
            out += [f"  - [{' · '.join(x for x in (t(lk['label']) if lk['label'] else '', lk['text']) if x)}]({lk['url']})"
                    for lk in p["links"]]
        out.append("")
    if data["excerpts"]:
        out += [f"## {t('新摘录')}", ""]
        for g in data["excerpts"]:
            out.append(f"### {g['title']}")
            for x in g["items"]:
                out.append(f"- {t(_stance(x['stance']))} · {x['label']} p.{x['page']}：{x['quote']}")
            out.append("")
    if data.get("questions"):
        out += [f"## {t('研究问题进展')}", ""]
        for q in data["questions"]:
            out.append(f"- **{q['title']}**：" + t("支持 {s} / 相反 {o} / 待核对 {p}", s=q["supports"], o=q["opposes"],
                                                   p=q["pending"])
                       + (("；" + t("补上缺口 {n} 个", n=len(q["gaps_closed"]))) if q["gaps_closed"] else "")
                       + (("；" + t("判断已更新")) if q["judgment"] is not None else ""))
        out.append("")
    if data.get("revisits"):
        out += [f"## {t('旧判断回访')}", ""]
        for r in data["revisits"]:
            out.append("- " + t("「{q}」：{since} 确认的判断之后新增 {n} 条摘录，其中 {o} 条相反。", q=r["title"],
                                since=r["since"], n=r["new"], o=r["opposes"]))
        out.append("")
    if data.get("ready"):
        out += [f"## {t('可以写成段落了')}", ""]
        out += [f"- {r['title']}（{t('{n} 篇', n=len(r['papers']))}）" for r in data["ready"]]
        out.append("")
    if data["backlog"] or data["missed"]:
        out += [f"## {t('阅读积压')}", ""]
        out += [f"- {b['label']} · {b['title']}（{'、'.join(t(x) for x in b['reasons'])}）" for b in data["backlog"]]
        out += [f"- {t('库里 {n} 篇都引用、你还没收：', n=m['n'])}{m['title']}" + (f" doi:{m['doi']}" if m["doi"] else "")
                for m in data["missed"]]
        out.append("")
    if data["tracking"]:
        out += [f"## {t('新文献')}", ""] + [f"- {x['title']}" for x in data["tracking"]] + [""]
    if data.get("goals"):
        out += [f"## {t('这周打算的进展')}", ""]
        for g in data["goals"]:
            mark = "x" if g["status"] == "done" else " "
            out.append(f"- [{mark}] {g['text']} — " + (t("相关记录 {n} 条", n=g["n"]) if g["n"] else t("这段时间没有相关记录")))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _stance(s: str) -> str:
    return {"supports": L("支持"), "opposes": L("相反")}.get(s, L("待核对"))


# ------------------------------------------------------------------ group-meeting outline
def outline(config, registry: Registry, rid: int) -> dict:
    """A slide-by-slide Markdown outline for a group meeting (no model). Slides are separated by
    '---'; speaker notes are '> 备注' lines; only the user's own excerpts and judgments go in, and
    unchecked excerpts are marked as such. Saved figure-evidence crops are copied next to it."""
    rep = get(registry, rid)
    if rep is None:
        raise ValueError("找不到这份报告")
    t = _ml(config)
    data, period = rep["data"], rep["period"]
    start, end = period_range(rep["kind"], period)
    act = activity(config, registry, start, end)
    fig_dir = config.knowledge_dir / "digests" / "figures" / period
    by_q: dict[int, list[dict]] = {}
    for x in act["excerpts"]:
        by_q.setdefault(x["question_id"], []).append(x)
    qrows = {r["id"]: dict(r) for r in registry.query(
        "SELECT id, title, judgment, judgment_at FROM evidence_questions WHERE status='active'")}
    c = data["counts"]
    title = t("组会汇报") + f" · {data['label']}"
    slides = [[f"# {title}", "", t("研究问题：") + "；".join(qrows[q]["title"] for q in by_q if q in qrows)]]
    overview = [f"## {t('概览')}", "",
                "- " + t("读了 {read} 篇，新入库 {added} 篇", read=c["read"], added=c["added"]),
                "- " + t("新增摘录 {ex} 条（支持 {s} / 相反 {o} / 待核对 {p}）", ex=c["excerpts"], s=c["supports"],
                         o=c["opposes"], p=c["pending"]),
                "- " + t("补上缺口 {gc} 个；更新判断的问题 {j} 个", gc=c["gaps_closed"], j=c["judgments"])]
    slides.append(overview)
    refs: dict[int, dict] = {}
    for qid, items in by_q.items():
        q = qrows.get(qid)
        if not q:
            continue
        changed = bool(q["judgment_at"] and _utc(start) <= q["judgment_at"] < _utc(end))
        s = [f"## {t('问题：')}{q['title']}", ""]
        s.append(f"**{t('当前判断')}**：" + (q["judgment"].strip().splitlines()[0][:300] if q["judgment"].strip()
                                            else t("（还没写当前判断）")) + (f"（{t('本期更新')}）" if changed else ""))
        s += ["", f"**{t('新证据')}**"]
        notes = []
        for x in sorted(items, key=lambda x: {"supports": 0, "opposes": 1}.get(x["stance"], 2))[:6]:
            refs[x["paper_id"]] = x
            # the stance label in front already marks unchecked excerpts as 待核对
            s.append(f"- {t(_stance(x['stance']))} · {_paper_label(x)}, p.{x['page'] + 1} — {x['quote'][:140]}")
            notes.append(f"> {t('备注')}：“{x['quote'][:400]}”（{_paper_label(x)}, p.{x['page'] + 1}"
                         + (f", doi:{x['doi']}" if x["doi"] else "") + "）")
            if x["source"] == "region":
                png = registry.one("SELECT png FROM evidence_region_previews WHERE revision=? AND page=? AND rects_json=?",
                                   (x["revision"], x["page"], x["rects_json"]))
                if png:
                    fig_dir.mkdir(parents=True, exist_ok=True)
                    (fig_dir / f"e{x['id']}.png").write_bytes(png["png"])
                    s.append(f"![{x['region_label'] or _paper_label(x)}](figures/{period}/e{x['id']}.png)")
        slides.append(s + [""] + notes)
    conflicts = [p for p in data["pushes"] if p["kind"] in ("conflict", "update")]
    if conflicts:
        s = [f"## {t('冲突与疑问')}", ""]
        for p in conflicts:
            s.append(f"- {t(p['why'], **p['args'])}")
            s += [f"  - {t(_stance(qq['stance']))}：{qq['text']}" for qq in p.get("quotes", [])]
        s += ["", t("值得在组会上讨论：研究对象、样本和条件是否可比？")]
        slides.append(s)
    nxt = [f"## {t('下一步')}", ""]
    nxt_goals = goals(registry, next_period("week", period)) if rep["kind"] == "week" else []
    nxt += [f"- {g['text']}" for g in nxt_goals]
    nxt += [f"- {t('待补缺口：')}{p['args'].get('gap', '')}" for p in data["pushes"] if p["kind"] == "gap"]
    if len(nxt) == 2:
        nxt.append("- " + t("（在回顾页写下下周打算后，会出现在这里）"))
    slides.append(nxt)
    if refs:
        s = [f"## {t('参考文献')}", ""]
        for x in refs.values():
            s.append(f"- {_paper_label(x)}. {x['paper_title'] or ''}" + (f" doi:{x['doi']}" if x["doi"] else ""))
        slides.append(s)
    text = "\n\n---\n\n".join("\n".join(s).rstrip() for s in slides) + "\n"
    rel = f"digests/{period}-{t('组会大纲')}.md"
    atomic_write(config.knowledge_dir / rel, text)
    registry.execute("UPDATE digest_reports SET outline_path=?, updated_at=? WHERE id=?", (rel, now_iso(), rid))
    return {"path": rel, "text": text}


def save_outline(config, registry: Registry, rid: int, text: str) -> str:
    rep = get(registry, rid)
    if rep is None or not rep["outline_path"]:
        raise ValueError("请先生成组会大纲")
    if not isinstance(text, str) or len(text) > 200_000:
        raise ValueError("大纲太长")
    atomic_write(config.knowledge_dir / rep["outline_path"], text)
    return rep["outline_path"]


def read_outline(config, rep: dict) -> str:
    if not rep.get("outline_path"):
        return ""
    p: Path = config.knowledge_dir / rep["outline_path"]
    try:
        return p.read_text("utf-8")
    except OSError:
        return ""
