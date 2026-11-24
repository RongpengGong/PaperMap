from concurrent.futures import ThreadPoolExecutor

import pytest

from papermap import evidence, evidence_links, resources

from .test_evidence import capture  # noqa: F401


@pytest.mark.parametrize("source", ["native", "region"])
def test_reuse_retains_shared_anchor_and_independent_notes(capture, registry, source):
    web, pid, _, data = capture
    original = web.post(f"/api/reader/{pid}/evidence", json={
        **data, "source": source, "region_label": "Fig. 2B", "rects": [[51, 50, 180, 100]],
    }).json()
    qid, eid = original["question_id"], original["id"]
    target = evidence.create_question(registry, "Can the study generalize?")
    url = f"/api/evidence/entries/{eid}/reuse"
    payload = {"question_id": target, "csrf": data["csrf"], "stance": "opposes", "dimension": "limitations", "note": "Small n"}
    assert web.post(url, json={**payload, "csrf": "invalid"}).status_code == 403
    assert web.post(url, json={**payload, "question_id": qid}).status_code == 400
    assert web.post(url, json={**payload, "question_id": 999}).status_code == 400
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: web.post(url, json=payload).json(), range(2)))
    assert sum(result["created"] for result in results) == 1
    copy = evidence.get_entries(registry, target)[0]
    assert {r["id"] for r in results} == {copy["id"]}
    first = evidence.get_entries(registry, qid)[0]
    for col in ("paper_id", "anchor_key", "page", "rects_json", "revision", "source", "region_label", "quote"):
        assert first[col] == copy[col]
    assert copy["stance"] == "opposes" and copy["dimension"] == "limitations"
    assert first["note"] == data["note"] and copy["note"] == "Small n"
    edit = f"/api/evidence/entries/{copy['id']}/update"
    web.post(edit, json={**payload, "note": "Edited independently"})
    assert not web.post(url, json=payload).json()["created"]
    assert evidence.get_entries(registry, target)[0]["note"] == "Edited independently"
    assert web.post(f"/api/evidence/entries/{eid}/delete", json={"csrf": data["csrf"]}).json()["ok"]
    assert web.get(f"/api/reader/{pid}/evidence/{copy['id']}").json()["rects"] == [[51, 50, 180, 100]]
    if source == "region":
        assert web.get(f"/api/evidence/entries/{copy['id']}/preview.png").content.startswith(b"\x89PNG")
        assert registry.one("SELECT count(*) FROM evidence_region_previews")[0] == 1
        web.post(f"/api/evidence/entries/{copy['id']}/delete", json={"csrf": data["csrf"]})
        assert registry.one("SELECT count(*) FROM evidence_region_previews")[0] == 0
    else:
        result = web.post(f"/api/evidence/entries/{copy['id']}/reuse", json={
            "csrf": data["csrf"], "question_id": 0, "question_title": "Brand new question",
        }).json()
        assert result["created"] and evidence.get_entries(registry, result["question_id"])[0]["stance"] == "pending"


def test_possible_dataset_overlap_requires_review_per_pair(capture, registry):
    web, pid, _, data = capture
    saved = web.post(f"/api/reader/{pid}/evidence", json=data).json()
    qid = saved["question_id"]
    second = registry.create_paper(title="Second study")
    third = registry.create_paper(title="Third study")
    unrelated = registry.create_paper(title="Outside the question")
    for paper in (second, third):
        evidence.add_entry(registry, qid, "", paper, 0, "a"*64, [[0, 0, 1, 1]], "Another passage", "native", "pending", "")
    for paper in (pid, second, third, unrelated):
        resources.store(registry, paper, resources.extract_text([f"We used GSE12345 for sample {paper}. See github.com/demo/code."]))
    group, = evidence_links.overlap_groups(registry, qid)
    assert group["accession"] == "GSE12345" and group["reviews"] == []
    assert [r["paper_id"] for r in group["papers"]] == [pid, second, third]
    html = web.get(f"/evidence?view=compare&question={qid}").text
    assert "可能复用了同一数据集" in html and "GSE12345" in html and "Outside the question" not in html
    assert f"/reader/{second}?page=1&amp;q=GSE12345" in html
    api = f"/api/evidence/questions/{qid}/dataset-overlap"
    a,b,_ = group["papers"]
    body = {"csrf": data["csrf"], "kind": "geo", "accession": "GSE12345", "decision": "partial", "note": "See table 2",
            "paper_a": second, "paper_b": pid, "context_key": a["context_key"] + b["context_key"]}
    assert web.post(api, json={**body, "csrf": "other"}).status_code == 403
    assert web.post(api, json={**body, "paper_a": unrelated}).status_code == 400
    assert web.post(api, json={**body, "paper_a": pid}).status_code == 400
    assert web.post(api, json=body).json()["ok"]
    assert web.post(api, json=body).json()["ok"]
    assert web.post(api, json={**body, "decision": "same"}).status_code == 400  # stale open window
    review, = evidence_links.overlap_groups(registry, qid)[0]["reviews"]
    assert (review["paper_a"],review["paper_b"],review["decision"],review["stale"]) == (pid,second,"partial",False)
    resources.store(registry, second, resources.extract_text(["GSE12345 was cited for comparison only."]))
    assert evidence_links.overlap_groups(registry, qid)[0]["reviews"][0]["stale"]
    assert web.post(api, json=body).status_code == 400
    registry.update_paper(second, status="ignored")
    assert evidence_links.overlap_groups(registry, qid)[0]["reviews"] == []
