"""Periodic reviews: today's activity, weekly / monthly reports, gap pushes with feedback, goals, outline."""
import json
from datetime import date, datetime, timedelta, timezone

from papermap import digest, evidence, gaps, notify, tracking
from papermap.library import notes_path
from papermap.registry import now_iso

from .conftest import OWN_DOI
from .test_reader import client as reader_client
from .test_web import _token


def _entry(registry, qid, pid, stance, quote, created=None, page=0, source="native", rects="[[1,1,50,20]]", label=""):
    return registry.execute(
        "INSERT INTO evidence_entries(question_id, paper_id, page, revision, rects_json, quote, source, stance, note, "
        "anchor_key, created_at, region_label) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (qid, pid, page, "rev1", rects, quote, source, stance, "", f"{stance}-{quote[:20]}", created or now_iso(),
         label)).lastrowid


def _paper(registry, title, author):
    return registry.execute("INSERT INTO papers(title, first_author, year, status, created_at, updated_at) "
                            "VALUES (?,?,2023,'done',?,?)", (title, author, "2020-01-01T00:00:00+00:00",
                                                              "2020-01-01T00:00:00+00:00")).lastrowid


def _setup(config, registry, pdfs):
    c, pipe, comp = reader_client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    qid = evidence.create_question(registry, "Do fibroblast niches drive invasion?")
    other = _paper(registry, "Fibroblasts are bystanders at tumour margins", "Li")
    return c, pipe, comp, pid, qid, other


def test_periods():
    start, end = digest.period_range("week", "2026-W48")
    assert start.date() == date(2026, 11, 23) and end.date() == date(2026, 11, 30)
    assert digest.period_range("month", "2026-12")[1].date() == date(2027, 1, 1)
    now = datetime(2026, 11, 25, 12).astimezone()
    assert digest.current_period("week", now) == "2026-W48"
    assert digest.previous_period("week", now) == "2026-W47"
    assert digest.previous_period("month", now) == "2026-10"
    assert digest.next_period("week", "2026-W52") == "2026-W53"
    assert digest.period_label("week", "2026-W48").endswith("11/23–11/29")


def test_today_week_pushes_feedback_and_page(config, registry, pdfs):
    c, pipe, comp, pid, qid, other = _setup(config, registry, pdfs)
    tok = _token(c)
    sup = _entry(registry, qid, pid, "supports", "Fibroblast niches surround invading tumour cells.")
    _entry(registry, qid, other, "opposes", "Fibroblasts did not change invasion in organoids.")
    gid = gaps.add_gap(registry, qid, "No interventional study in humans", ["interventional"])
    row = registry.get_paper(pid)
    notes_path(config.knowledge_dir, row["kb_path"]).write_text("- Are the niches present before invasion?\n", "utf-8")
    c.post(f"/paper/{pid}/meta", json={"csrf": tok, "read_status": "read"})
    assert registry.get_paper(pid)["read_at"]

    # today: a plain list on the home page
    t = digest.today(config, registry)
    assert t["counts"]["excerpts"] == 2 and t["counts"]["finished"] == 1
    assert "Are the niches present before invasion?" in t["questions"]
    home = c.get("/").text
    assert "今天做了什么" in home and "Fibroblast niches surround" in home

    week = digest.current_period("week")
    data = digest.build(config, registry, "week", week)
    kinds = {p["kind"]: p for p in data["pushes"]}
    assert set(kinds) == {"conflict", "gap", "noteq"} and len(data["pushes"]) <= digest.WEEK_PUSHES
    assert kinds["conflict"]["args"]["a"].startswith("Zhang") and kinds["conflict"]["args"]["b"] == "Li 2023"
    assert any(f"evidence={sup}" in lk["url"] for lk in kinds["conflict"]["links"])
    assert data["excerpts"][0]["title"] == "Do fibroblast niches drive invasion?"

    page = c.get("/digest").text
    assert "值得追问" in page and "Are the niches present before invasion?" in page and 'data-key="conflict:' in page

    # irrelevant: gone, and other pushes about the same place lose weight
    r = c.post("/api/digest/feedback", json={"csrf": tok, "key": kinds["noteq"]["key"], "action": "irrelevant"}).json()
    assert r["ok"] and r["action"] == "irrelevant"
    assert "noteq" not in {p["kind"] for p in digest.build(config, registry, "week", week)["pushes"]}
    # resolved on a gap closes the gap itself
    c.post("/api/digest/feedback", json={"csrf": tok, "key": f"gap:{gid}", "action": "resolved"})
    assert gaps.get_gap(registry, gid)["status"] == "closed"
    # resolved conflict stays away until the evidence changes
    sig = kinds["conflict"]["signature"]
    c.post("/api/digest/feedback", json={"csrf": tok, "key": f"conflict:{qid}", "action": "resolved", "signature": sig})
    assert not digest.build(config, registry, "week", week)["pushes"]
    _entry(registry, qid, other, "opposes", "A second organoid line showed no change either.")
    again = digest.build(config, registry, "week", week)["pushes"]
    assert [p["kind"] for p in again] == ["conflict"]
    # follow-up is remembered; clearing undoes it
    c.post("/api/digest/feedback", json={"csrf": tok, "key": f"conflict:{qid}", "action": "followup"})
    assert digest.build(config, registry, "week", week)["pushes"][0]["followed"]
    assert c.post("/api/digest/feedback", json={"csrf": tok, "key": f"conflict:{qid}", "action": "clear"}).json()["action"] is None
    assert not c.post("/api/digest/feedback", json={"csrf": tok, "key": "x:1", "action": "resolved"}).json()["ok"]


def test_goals_summary_markdown_and_outline(config, registry, pdfs):
    c, pipe, comp, pid, qid, other = _setup(config, registry, pdfs)
    tok = _token(c)
    _entry(registry, qid, pid, "supports", "Fibroblast niches surround invading tumour cells.")
    _entry(registry, qid, other, "pending", "Margins were enriched for CAFs.", page=1, source="region",
           rects="[[0,0,10,10]]", label="Fig. 2b")
    registry.execute("INSERT INTO evidence_region_previews(revision, page, rects_json, png) VALUES (?,?,?,?)",
                     ("rev1", 1, "[[0,0,10,10]]", b"\x89PNG\r\n\x1a\nfake"))
    week = digest.current_period("week")
    # goals are written for a week; the report for that week checks them against the records
    assert c.post("/api/digest/goals", json={"csrf": tok, "period": week, "text": "比较 fibroblast niches 的样本"}).json()["ok"]
    for i in range(2):
        c.post("/api/digest/goals", json={"csrf": tok, "period": week, "text": f"goal {i} invasion"})
    assert not c.post("/api/digest/goals", json={"csrf": tok, "period": week, "text": "one too many"}).json()["ok"]
    prog = digest.goal_progress(registry, week, digest.activity(config, registry, *digest.period_range("week", week)))
    assert prog[0]["n"] >= 1 and "Fibroblast niches" in prog[0]["matched"][0]["label"]

    config.raw["llm"]["api_key"] = "sk-test"
    est = digest.estimate_summary(config, registry, digest.activity(config, registry, *digest.period_range("week", week)))
    assert est["calls"] == 1
    r = c.post("/api/digest/generate", json={"csrf": tok, "kind": "week", "period": week, "model": True}).json()
    assert r["ok"] and r["path"] == f"digests/{week}.md", r
    rep = digest.get(registry, r["id"])
    assert "](/reader/" in rep["summary"] and "E99999" not in rep["summary"] and rep["cost_usd"] > 0
    assert comp.digest_calls == 1
    md = (config.knowledge_dir / rep["kb_path"]).read_text("utf-8")
    assert md.startswith(f"# 周报 · {week}") and "Fibroblast niches surround" in md and "AI 小结" in md
    # rebuilding without the model keeps the earlier summary
    digest.generate(config, registry, pipe.llm, "week", week, use_model=False)
    assert digest.get(registry, r["id"])["summary"] == rep["summary"]

    out = c.post(f"/api/digest/{r['id']}/outline", json={"csrf": tok}).json()
    assert out["ok"]
    text = out["text"]
    assert text.count("\n---\n") >= 3 and "## 问题：Do fibroblast niches drive invasion?" in text
    assert "- 待核对 · Li 2023, p.2" in text and "> 备注：" in text and f"doi:{OWN_DOI}" in text
    assert f"figures/{week}/e" in text and list((config.knowledge_dir / "digests" / "figures" / week).glob("*.png"))
    assert "（还没写当前判断）" in text
    assert c.post(f"/api/digest/{r['id']}/outline", json={"csrf": tok, "text": "# mine\n"}).json()["ok"]
    assert c.get(f"/digest/{r['id']}/outline.md").text == "# mine\n"
    assert c.get(f"/digest/{r['id']}.md").status_code == 200
    assert "# mine" in c.get(f"/digest?kind=week&period={week}").text


def test_month_revisits_and_ready_to_write(config, registry, pdfs):
    c, pipe, comp, pid, qid, other = _setup(config, registry, pdfs)
    tok = _token(c)
    third, fourth = _paper(registry, "Third", "Wu"), _paper(registry, "Fourth", "Ng")
    c.post(f"/api/evidence/questions/{qid}/judgment", json={"csrf": tok, "judgment": "Niches drive invasion.", "previous": ""})
    old = (datetime.now().astimezone() - timedelta(days=40)).astimezone().isoformat(timespec="seconds")
    registry.execute("UPDATE evidence_questions SET judgment_at=? WHERE id=?", (old, qid))
    for p, s in ((pid, "supports"), (other, "opposes"), (third, "supports"), (fourth, "supports")):
        _entry(registry, qid, p, s, f"quote from {p}")
    month = digest.current_period("month")
    data = digest.build(config, registry, "month", month)
    rv = data["revisits"][0]
    assert rv["id"] == qid and rv["new"] == 4 and rv["opposes"] == 1
    ready = data["ready"][0]
    assert ready["both"] and len(ready["papers"]) == 4 and ready["url"].startswith("/write?ids=")
    assert data["questions"][0]["supports"] == 3
    assert len(data["pushes"]) <= digest.MONTH_PUSHES
    page = c.get(f"/digest?kind=month&period={month}").text
    assert "旧判断回访" in page and "可以写成段落了" in page
    # editing the judgment again moves its timestamp; saving the same text does not
    before = registry.one("SELECT judgment_at FROM evidence_questions WHERE id=?", (qid,))["judgment_at"]
    c.post(f"/api/evidence/questions/{qid}/judgment", json={"csrf": tok, "judgment": "Niches drive invasion.",
                                                            "previous": "Niches drive invasion."})
    assert registry.one("SELECT judgment_at FROM evidence_questions WHERE id=?", (qid,))["judgment_at"] == before


def test_auto_reports_send_one_notification(config, registry, pdfs, monkeypatch):
    c, pipe, comp, pid, qid, other = _setup(config, registry, pdfs)
    _entry(registry, qid, pid, "supports", "Fibroblast niches surround invading tumour cells.")
    sent = []
    next_week = datetime.now().astimezone() + timedelta(days=7)
    made = digest.maybe_auto(pipe, now=next_week, send=lambda *a: sent.append(a))
    kinds = {r["kind"] for r in made}
    assert "week" in kinds and len([s for s in sent if "上周回顾" in s[0]]) == 1
    assert "1 条" in sent[0][1] or "新增 1" in sent[0][1]
    assert digest.maybe_auto(pipe, now=next_week, send=lambda *a: sent.append(a)) == []
    # the separate weekly new-literature notification is folded into the review
    tracking.ensure_schema(registry)
    calls = []
    monkeypatch.setattr(notify, "send", lambda *a: calls.append(a))
    registry.execute("INSERT INTO track_candidates(title, authors_json, oa_urls_json, lib_refs_json, gaps_json, score, "
                     "status, created_at, updated_at, title_key) VALUES ('New one','[]','[]','[]','[]',8,'new',?,?,'newone')",
                     (now_iso(), now_iso()))
    assert tracking.maybe_digest(config, registry, force=True) and calls == []
    digest.save_settings(config, {"weekly": False})
    later = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds")
    registry.execute("INSERT INTO track_candidates(title, authors_json, oa_urls_json, lib_refs_json, gaps_json, score, "
                     "status, created_at, updated_at, title_key) VALUES ('Another','[]','[]','[]','[]',8,'new',?,?,'another')",
                     (later, later))
    assert tracking.maybe_digest(config, registry, force=True) and len(calls) == 1
    # a quiet period is recorded as empty and sends nothing
    empty_week = datetime.now().astimezone() + timedelta(days=7 * 30)
    digest.save_settings(config, {"weekly": True, "monthly": False})
    sent.clear()
    made = digest.maybe_auto(pipe, now=empty_week, send=lambda *a: sent.append(a))
    assert made[0]["status"] == "empty" and sent == []
    st = c.post("/api/digest/settings", json={"csrf": _token(c), "ai_summary": False}).json()["settings"]
    assert st["ai_summary"] is False and json.loads(json.dumps(config.raw["digest"]))["ai_summary"] is False
