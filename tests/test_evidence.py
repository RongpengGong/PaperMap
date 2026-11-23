from concurrent.futures import ThreadPoolExecutor
from html import escape

import pytest

from papermap import evidence, library
from .conftest import OWN_DOI
from .test_reader import client
from .test_web import _token


@pytest.fixture
def capture(config, registry, pdfs):
    web, _, _ = client(config, registry, pdfs)
    row = registry.paper_by_doi(OWN_DOI)
    pdf = library.pdf_path(config.knowledge_dir, row["kb_path"])
    data = {"csrf": _token(web), "question_title": "是否独立验证？", "revision": evidence.document_revision(pdf),
            "page": 1, "quote": "Exact value < 2 and 1.75 mg/mL", "rects": [[51.1, 50, 277.0, 65.0]],
            "source": "ocr", "stance": "supports", "note": "比较另一组样本"}
    return web, row["id"], pdf, data


def test_capture_page_link_export_and_edit(capture, registry):
    web, pid, _, data = capture
    add = f"/api/reader/{pid}/evidence"
    saved = web.post(add, json=data).json()
    eid, qid = saved["id"], saved["question_id"]
    assert saved["created"]
    assert web.get("/api/evidence/questions").json()["questions"][0]["total"] == 1
    entry = web.get(f"/api/reader/{pid}/evidence/{eid}").json()
    assert (entry["page"], entry["rects"], entry["quote"], entry["source"]) == (1, data["rects"], data["quote"], "ocr")
    assert entry["question_id"] == qid
    assert web.get(f"/api/reader/{pid+1}/evidence/{eid}").status_code == 404
    href = f"/reader/{pid}?evidence={eid}&page=2"
    html = web.get(f"/evidence?question={qid}").text
    assert escape(href, quote=True) in html and escape(data["quote"]) in html and f'id="evidence-{eid}"' in html
    export = web.get(f"/evidence/{qid}.md")
    assert ("text/markdown" in export.headers["content-type"] and OWN_DOI in export.text
            and "http://127.0.0.1:8765" + href in export.text and "第 2 页" in export.text and "本机 OCR" in export.text)
    edit = f"/api/evidence/entries/{eid}/update"
    assert web.post(edit, json={"csrf": data["csrf"], "note": "更新", "stance": "opposes"}).json()["ok"]
    assert not web.post(add, json=data).json()["created"]
    assert evidence.get_entries(registry, qid)[0]["note"] == "更新"
    assert web.post(edit, json={"csrf": data["csrf"], "note": "x", "stance": "?"}).status_code == 400
    assert web.post(f"/api/evidence/entries/{eid}/delete", json={"csrf": data["csrf"]}).json()["ok"]
    assert not evidence.get_entries(registry, qid)


def test_changed_source_blocks_capture_and_recall_but_keeps_evidence(capture):
    web, pid, pdf, data = capture
    url = f"/api/reader/{pid}/evidence"
    saved = web.post(url, json=data).json()
    with pdf.open("ab") as f:
        f.write(b"\n% altered PDF revision\n")
    assert web.post(url, json=data).status_code == 409
    assert web.get(f"{url}/{saved['id']}").status_code == 409
    assert escape(data["quote"]) in web.get("/evidence").text


def test_anchor_validation_and_duplicate_requests(capture):
    web, pid, _, data = capture
    url = f"/api/reader/{pid}/evidence"
    assert web.post(url, json=dict(data, csrf="invalid")).status_code == 403
    for bad in ({"page": 9}, {"page": False}, {"rects": [[1, 0, 0, 2]]},
                {"rects": [["x", 0, 1, 2]]}, {"quote": ""}, {"source": "other"}, {"question_id": 999}):
        assert web.post(url, json={**data, **bad}).status_code == 400
    assert not web.get("/api/evidence/questions").json()["questions"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: web.post(url, json=data).json(), range(3)))
    assert len({r["id"] for r in results}) == 1
    assert sum(r["created"] for r in results) == 1
    another = web.post("/api/evidence/questions", json={"title": "另一问题", "csrf": data["csrf"]}).json()["id"]
    assert web.post(url, json={**data, "question_id": another, "source": "native"}).json()["created"]
    assert sorted(q["total"] for q in web.get("/api/evidence/questions").json()["questions"]) == [1, 1]


def test_question_combines_quotations_from_two_papers(capture, registry, pdfs):
    web, first, _, data = capture
    qid = web.post(f"/api/reader/{first}/evidence", json=data).json()["question_id"]
    other = pdfs["refs_only"]
    second = registry.create_paper(title="An independent replication", first_author="Li", year=2025)
    revision = evidence.document_revision(other)
    registry.upsert_file(revision, str(other), other.stat().st_size, other.stat().st_mtime, second)
    result = web.post(f"/api/reader/{second}/evidence", json={**data, "question_id": qid, "page": 0,
                      "quote": "A different result", "revision": revision, "source": "native", "stance": "opposes"})
    assert result.json()["ok"]
    html = web.get(f"/evidence?question={qid}").text
    assert escape(data["quote"]) in html and "A different result" in html and "An independent replication" in html
    assert {x["paper_id"] for x in evidence.get_entries(registry, qid)} == {first, second}


def test_deleted_evidence_link_and_retry_never_target_a_new_entry(capture):
    web, pid, _, data = capture
    capture_url = f"/api/reader/{pid}/evidence"
    old = web.post(capture_url, json=data).json()["id"]
    delete_url = f"/api/evidence/entries/{old}/delete"
    web.post(delete_url, json={"csrf": data["csrf"]})
    new = web.post(capture_url, json={**data, "quote": "Second quotation"}).json()["id"]
    assert old != new and web.get(f"{capture_url}/{old}").status_code == 404
    web.post(delete_url, json={"csrf": data["csrf"]})
    assert web.get(f"{capture_url}/{new}").json()["quote"] == "Second quotation"


def test_doi_merge_keeps_personal_notes_when_anchors_coincide(capture, registry, config):
    from .test_pipeline import make_pipeline
    web, pid, _, data = capture
    result = web.post(f"/api/reader/{pid}/evidence", json=data).json()
    qid = result["question_id"]
    pending = registry.create_paper(title="待核对记录", status="doi_unverified")
    # Both records can refer to this passage. Keep separately written notes on merge.
    moved = evidence.add_entry(registry, qid, "", pending, data["page"], data["revision"],
                               evidence.validate_rects(data["rects"], 595, 842), data["quote"], "ocr", "opposes", "核对剂量")
    pipeline, _ = make_pipeline(config, registry)
    assert pipeline.resolve_unverified(pending, OWN_DOI) == "duplicate_doi"
    items = evidence.get_entries(registry, qid)
    assert len(items) == 2 and {e["paper_id"] for e in items} == {pid}
    assert {e["note"] for e in items} == {"核对剂量", data["note"]}
    assert not web.post(f"/api/reader/{pid}/evidence", json=data).json()["created"]
    entry_id = moved["id"]
    query = f"?evidence={entry_id}&page=2"
    for _ in range(2):
        redirected = web.get(f"/reader/{pending}{query}", follow_redirects=False)
        assert redirected.status_code == 307 and redirected.headers["location"].endswith(f"/reader/{pid}{query}")
        assert web.get(redirected.headers["location"]).status_code == 200
        registry.create_paper(title="Another paper")
