from html import escape

from papermap import evidence, gaps
from papermap.registry import Registry

from .test_evidence import capture  # noqa: F401


def test_comparison_keeps_quotes_separate_from_card_and_reuses_gaps(capture, registry, pdfs):
    web, pid, pdf, data = capture
    saved = web.post(f"/api/reader/{pid}/evidence", json={**data, "dimension": "findings"}).json()
    qid, eid = saved["question_id"], saved["id"]
    registry.update_paper(pid, card_json={
        "design": {"cohorts": ["A"], "species": ["human"], "sample_size": "3000 cells"},
        "methods": ["Model-inferred method"], "key_findings": ["A card claim without a source"],
    })
    second = registry.create_paper(title="Second independent study", card_json="broken legacy card")
    other = pdfs["refs_only"]
    revision = evidence.document_revision(other)
    registry.upsert_file(revision, str(other), other.stat().st_size, other.stat().st_mtime, second)
    old = web.post(f"/api/reader/{second}/evidence", json={
        **data, "question_id": qid, "revision": revision, "page": 0, "source": "native",
        "quote": "We observed the opposite", "stance": "opposes",
    }).json()
    assert old["created"]
    gid = gaps.add_gap(registry, qid, "Independent validation", ["validation"])

    value = evidence.question_comparison(registry, qid)
    papers = {p["paper_id"]: p for p in value["papers"]}
    assert set(papers) == {pid, second}
    assert value["gaps"] == [{"id": gid, "text": "Independent validation", "status": "open"}]
    assert papers[pid]["card_context"] == {
        "sample_source": ["A", "human"], "analysis_unit": [], "methods": ["Model-inferred method"],
        "findings": ["A card claim without a source"], "limitations": [],
    }
    entry = papers[pid]["evidence"][0]
    assert (entry["page"], entry["dimension"], entry["revision"], entry["stance"], entry["source"]) == (
        2, "findings", data["revision"], "supports", "ocr",
    )
    assert entry["url"] == f"/reader/{pid}?evidence={eid}&page=2"
    assert papers[second]["evidence"][0]["dimension"] == "general"
    assert not any(papers[second]["card_context"].values())

    html = web.get(f"/evidence?question={qid}&view=compare").text
    assert 'class="ev-comparison"' in html and 'class="ev-card-context"' in html
    assert escape(entry["url"]) in html and escape(data["quote"]) in html
    assert "A card claim without a source" in html and "卡片摘要 · 待核对" in html and "暂无资料" in html
    assert "未分项摘录" in html and 'data-stance="opposes"' in html and "Independent validation" in html
    assert web.get(entry["url"]).status_code == 200
    anchor = web.get(f"/api/reader/{pid}/evidence/{eid}").json()
    assert anchor["page"] + 1 == entry["page"] and anchor["rects"] == data["rects"]

    # Reclassification is optional for previously saved excerpts and older clients.
    edit = f"/api/evidence/entries/{old['id']}/update"
    args = {"csrf": data["csrf"], "note": "", "stance": "pending"}
    assert web.post(edit, json={**args, "dimension": "invalid"}).status_code == 400
    assert web.post(edit, json={**args, "dimension": "analysis_unit"}).status_code == 200
    assert web.post(edit, json=args).status_code == 200
    revised = {p["paper_id"]: p for p in evidence.question_comparison(registry, qid)["papers"]}
    assert revised[second]["evidence"][0]["dimension"] == "analysis_unit"
    assert revised[second]["evidence"][0]["stance"] == "pending"

    registry.update_paper(second, status="ignored")
    assert [p["paper_id"] for p in evidence.question_comparison(registry, qid)["papers"]] == [pid]
    with pdf.open("ab") as f:
        f.write(b"\n% New revision with no surviving original\n")
    assert web.get(f"/api/reader/{pid}/evidence/{eid}").status_code == 409


def test_judgment_handles_conflict_and_duplicate_save(capture, registry):
    web, pid, _, data = capture
    qid = web.post(f"/api/reader/{pid}/evidence", json=data).json()["question_id"]
    path = f"/api/evidence/questions/{qid}/judgment"
    update = {"csrf": data["csrf"], "judgment": "  Need more <replicates>  ", "previous": ""}
    assert web.post(path, json={**update, "csrf": "bad"}).status_code == 403
    assert web.post(path, json=update).json()["judgment"] == "Need more <replicates>"
    assert web.post(path, json=update).json()["ok"]
    assert web.post(path, json={**update, "judgment": "A stale draft"}).status_code == 409
    assert web.post(path, json={**update, "judgment": "a" * 12001}).status_code == 400
    assert evidence.question_comparison(registry, qid)["judgment"] == "Need more <replicates>"
    assert "Need more &lt;replicates&gt;" in web.get(f"/evidence?question={qid}&view=compare").text
    assert web.post(path, json={**update, "previous": "Need more <replicates>", "judgment": ""}).json()["ok"]
    registry.execute("UPDATE evidence_questions SET status='pending' WHERE id=?", (qid,))
    assert evidence.question_comparison(registry, qid) is None
    assert web.get(f"/evidence?question={qid}&view=compare").status_code == 404


def test_old_library_migrates_without_manufacturing_judgments_or_categories(tmp_path):
    path = tmp_path / "legacy.sqlite"
    old = Registry(path)
    qid = evidence.create_question(old, "Legacy question")
    pid = old.create_paper(title="Legacy study")
    evidence.add_entry(old, qid, "", pid, 0, "test-revision", [[1, 1, 2, 2]], "Old quotation",
                       "native", "pending", "Kept note")
    old.execute("ALTER TABLE evidence_questions DROP COLUMN judgment")
    old.execute("ALTER TABLE evidence_entries DROP COLUMN dimension")
    old.close()
    migrated = Registry(path)
    try:
        migrated.execute("PRAGMA query_only=ON")
        value = evidence.question_comparison(migrated, qid)
        assert value["judgment"] == "" and value["gaps"] == []
        entry = value["papers"][0]["evidence"][0]
        assert entry["dimension"] == "general" and entry["note"] == "Kept note"
        assert not migrated.one("SELECT 1 FROM sqlite_master WHERE name='question_gaps'")
    finally:
        migrated.close()
