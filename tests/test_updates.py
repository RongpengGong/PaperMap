"""Update cards: what does this paper change for a research question (stage 7, task 3)."""
import json

import pymupdf
import pytest

from papermap import evidence, library, updates
from .conftest import OWN_DOI
from .test_reader import client
from .test_web import _token

SENTENCE = "We profiled tumours with single-cell RNA sequencing and Visium spatial transcriptomics."


def test_locate_tolerates_line_breaks_but_not_paraphrase(pdfs):
    with pymupdf.open(pdfs["paper"]) as doc:
        # BODY repeats SENTENCE; at 9 pt the second copy wraps across two lines
        rects = updates.locate(doc[0], SENTENCE + " " + SENTENCE)
        assert rects and len(rects) >= 2 and all(r[0] < r[2] and r[1] < r[3] for r in rects)
        assert updates.locate(doc[0], "We profiled tumours using bulk proteomics only.") is None
        assert updates.locate(doc[0], "We") is None  # too short to be an anchor


@pytest.fixture
def setup(config, registry, pdfs):
    web, pipe, comp = client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    qid = evidence.create_question(registry, "成纤维细胞小生境是否促进肿瘤进展？")
    field = registry.paper_field_rows(pid)[0]
    registry.execute("INSERT INTO evidence_question_fields VALUES (?,?)", (qid, field["field_id"]))
    other = pdfs["refs_only"]
    old_pid = registry.create_paper(title="An earlier organoid study", first_author="Li", year=2021, status="done")
    rev = evidence.document_revision(other)
    registry.upsert_file(rev, str(other), other.stat().st_size, other.stat().st_mtime, old_pid)
    old = evidence.add_entry(registry, qid, "", old_pid, 0, rev, [[50, 140, 300, 160]],
                             "Fibroblast niches did not change tumour growth.", "native", "supports", "")
    return web, pipe, comp, pid, qid, old["id"], field


def run_job(pipe, pid, qid):
    return pipe._job_update({"paper_id": pid, "arg": str(qid)}, lambda *a: None)


def test_card_needs_verbatim_quotes_and_old_anchor_then_user_confirms(setup, registry, config):
    web, pipe, comp, pid, qid, old_id, field = setup
    comp.update_outputs = [
        # paraphrased quote: rejected, the model is asked again
        {"outcome": "contradicts", "headline": "x", "claims": [{"text": "x", "new": [{"page": 1, "quote": "We used bulk proteomics."}], "old": [f"E{old_id}"]}]},
        {"outcome": "contradicts", "headline": "本文显示小生境与进展相关，和旧证据相反。", "comparability": "check",
         "comparability_note": "旧研究是小鼠类器官，本文是人肝癌组织",
         "claims": [{"text": "空间转录组定位到小生境。", "new": [{"page": 9, "quote": SENTENCE}], "old": [f"E{old_id}", "E99999"]}]},
    ]
    assert run_job(pipe, pid, qid) == "结论相反"
    assert comp.update_calls == 2
    card = updates.for_paper(registry, pid)[0]
    assert card["status"] == "pending" and card["comparability"] == "check"
    claim = card["claims"][0]
    assert claim["new"][0]["page"] == 0  # wrong page number from the model is corrected
    assert [o["id"] for o in claim["old"]] == [old_id]  # unknown E tags are dropped
    assert registry.one("SELECT purpose FROM llm_calls ORDER BY id DESC")["purpose"] == "update"

    panel = web.get(f"/paper/{pid}/panel").text
    assert 'data-tab="updates"' in panel and "可比性待核对" in panel and f"?update={card['id']}&amp;anchor=0" in panel
    assert f"?evidence={old_id}" in panel
    a = web.get(f"/api/reader/{pid}/update/{card['id']}/0").json()
    assert a["page"] == 0 and a["rects"] and a["back"] == f"/?id={pid}"
    assert web.get(f"/api/reader/{pid}/update/{card['id']}/5").status_code == 404
    assert "待核对的更新卡" in web.get("/review").text
    assert web.get("/api/state").json()["review"] >= 1
    field_page = web.get(f"/field/{field['slug']}").text
    assert "最近确认的变化" in field_page and "本文显示小生境" not in field_page  # not yet confirmed

    tok = _token(web)
    r = web.post(f"/api/updates/{card['id']}", json={"csrf": tok, "action": "confirm"}).json()
    assert r["ok"] and r["status"] == "confirmed" and r["added"] == 1
    added = [e for e in evidence.get_entries(registry, qid) if e["paper_id"] == pid]
    assert added[0]["stance"] == "opposes" and added[0]["note"].startswith("来自更新卡")
    assert "本文显示小生境" in web.get(f"/field/{field['slug']}").text
    assert "新论文带来的变化" in web.get(f"/evidence?question={qid}").text

    # rewrite: the user's sentence and outcome win; reopening goes back to 待核对
    r = web.post(f"/api/updates/{card['id']}", json={"csrf": tok, "action": "rewrite", "text": "只在人组织中相关",
                                                       "outcome": "extends"}).json()
    assert r["status"] == "rewritten" and r["added"] == 0  # same quotation is not filed twice
    page = web.get(f"/field/{field['slug']}").text
    assert "只在人组织中相关" in page and "扩展到新人群" in page
    assert web.post(f"/api/updates/{card['id']}", json={"csrf": tok, "action": "rewrite", "text": ""}).json()["ok"] is False
    web.post(f"/api/updates/{card['id']}", json={"csrf": tok, "action": "reopen"})
    assert "只在人组织中相关" not in web.get(f"/field/{field['slug']}").text


def test_keep_judgment_and_unchanged_are_not_changes(setup, registry):
    web, pipe, comp, pid, qid, _, field = setup
    comp.update_outputs = [{"outcome": "unchanged", "headline": "没有改变判断",
                            "claims": [{"text": "只是描述了平台。", "new": [{"page": 1, "quote": SENTENCE}], "old": []}]}]
    run_job(pipe, pid, qid)
    cid = updates.for_paper(registry, pid)[0]["id"]
    tok = _token(web)
    assert web.post(f"/api/updates/{cid}", json={"csrf": tok, "action": "confirm"}).json()["added"] == 0
    page = web.get(f"/field/{field['slug']}").text
    assert "没有改变判断" not in page and "未改变当前判断" in page
    # keep my current judgment
    web.post(f"/api/updates/{cid}", json={"csrf": tok, "action": "reopen"})
    assert web.post(f"/api/updates/{cid}", json={"csrf": tok, "action": "keep"}).json()["status"] == "kept"
    assert "未改变当前判断" not in web.get(f"/field/{field['slug']}").text.split("最近确认的变化")[1].split("</section>")[0]


def test_comparison_without_old_evidence_is_refused(config, registry, pdfs):
    web, pipe, comp = client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    qid = evidence.create_question(registry, "一个还没有证据的问题")
    bad = {"outcome": "support", "headline": "x", "claims": [{"text": "x", "new": [{"page": 1, "quote": SENTENCE}], "old": []}]}
    comp.update_outputs = [bad, dict(bad)]
    with pytest.raises(Exception):
        run_job(pipe, pid, qid)
    assert not updates.for_paper(registry, pid)
    comp.update_outputs = [{"outcome": "first_intervention", "headline": "首条干预证据",
                            "claims": [{"text": "x", "new": [{"page": 1, "quote": SENTENCE}], "old": []}]}]
    assert run_job(pipe, pid, qid) == "本库在这个问题下收录的首条干预证据"
    # regenerating replaces the undecided card instead of piling up
    comp.update_outputs = [{"outcome": "unrelated", "headline": "无关", "claims": []}]
    run_job(pipe, pid, qid)
    assert [c["outcome"] for c in updates.for_paper(registry, pid)] == ["unrelated"]
    # a changed PDF makes the anchors unusable rather than pointing at the wrong place
    comp.update_outputs = [{"outcome": "unchanged", "headline": "x",
                            "claims": [{"text": "x", "new": [{"page": 1, "quote": SENTENCE}], "old": []}]}]
    run_job(pipe, pid, qid)
    cid = updates.for_paper(registry, pid)[0]["id"]
    pdf = library.pdf_path(config.knowledge_dir, registry.get_paper(pid)["kb_path"])
    with pdf.open("ab") as f:
        f.write(b"\n% altered\n")
    assert web.get(f"/api/reader/{pid}/update/{cid}/0").status_code == 409


def test_start_job_and_candidates(setup, registry):
    web, pipe, comp, pid, qid, _, _ = setup
    other = evidence.create_question(registry, "无关学科的问题")
    cands = updates.candidate_questions(registry, pid)
    assert [c["id"] for c in cands][:1] == [qid] and cands[0]["related"] and other in [c["id"] for c in cands]
    tok = _token(web)
    assert web.post(f"/paper/{pid}/update", json={"csrf": tok, "question_id": 99999}).json()["ok"] is False
    assert web.post(f"/paper/{pid}/update", json={"csrf": tok, "question_id": qid}).json()["ok"]
    import time
    for _ in range(100):  # let the background job finish before the database closes
        if registry.one("SELECT status FROM jobs WHERE kind='update'")["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert [c["outcome"] for c in updates.for_paper(registry, pid)] == ["unrelated"]


def test_locate_folds_ligatures():
    # PDFs often store ligature glyphs; the model writes plain letters
    assert updates._norm("Signi\ufb01cant \ufb01brosis (n\u2009=\u200912)") == "significantfibrosisn12"
