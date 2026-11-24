"""Evidence gaps and "this paper may answer your question" reminders (stage 7, task 5)."""
from papermap import evidence, gaps
from .conftest import OWN_DOI
from .test_reader import client
from .test_web import _token


def setup(config, registry, pdfs):
    c, pipe, comp = client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    qid = evidence.create_question(registry, "成纤维细胞小生境是否促进肿瘤进展？")
    return c, pipe, comp, pid, qid


def test_gap_finds_passages_in_library_and_new_imports(config, registry, pdfs):
    c, pipe, comp, pid, qid = setup(config, registry, pdfs)
    tok = _token(c)
    r = c.post(f"/api/evidence/questions/{qid}/gaps",
               json={"csrf": tok, "text": "还缺空间层面的证据", "terms": "Visium, fibroblast, organoid"}).json()
    assert r["ok"] and r["terms"] == ["Visium", "fibroblast", "organoid"] and r["found"] == 1
    hit = gaps.for_paper(registry, pid)[0]
    assert hit["origin"] == "library" and hit["status"] == "new" and set(hit["matched"]) >= {"Visium", "fibroblast"}
    ps = hit["passages"][0]
    assert ps["page"] == 0 and "Visium" in ps["snippet"] and len(ps["terms"]) >= 2
    # a gap whose terms are nowhere in the paper gives no reminder (and costs no model call)
    calls = registry.one("SELECT count(*) AS n FROM llm_calls")["n"]
    r2 = c.post(f"/api/evidence/questions/{qid}/gaps",
                json={"csrf": tok, "text": "缺 CXCL12 的人体干预", "terms": "CXCL12, knockout mice"}).json()
    assert r2["ok"] and r2["found"] == 0
    assert registry.one("SELECT count(*) AS n FROM llm_calls")["n"] == calls
    # importing again (new paper path): the import hook stores a reminder once
    registry.execute("DELETE FROM gap_hits")
    pipe._match_gaps(pid)
    pipe._match_gaps(pid)
    hits = gaps.for_paper(registry, pid)
    assert len(hits) == 1 and hits[0]["origin"] == "import"
    assert gaps.new_count(registry) == 1
    panel = c.get(f"/paper/{pid}/panel").text
    assert "这篇可能回答了你的问题" in panel and f'/reader/{pid}?page=1&amp;q=' in panel
    assert "可能回答你问题的论文" in c.get("/review").text
    page = c.get(f"/evidence?question={qid}").text
    assert "还缺空间层面的证据" in page and "CXCL12" in page and "全文检索匹配，未经模型判断" in page


def test_reminder_actions_keep_discovery_and_decision_apart(config, registry, pdfs):
    c, pipe, comp, pid, qid = setup(config, registry, pdfs)
    tok = _token(c)
    gid = c.post(f"/api/evidence/questions/{qid}/gaps",
                 json={"csrf": tok, "text": "空间证据", "terms": "Visium, fibroblast"}).json()["id"]
    hid = gaps.for_paper(registry, pid)[0]["id"]
    # asking the model is a separate step: it starts an update card job, the gap stays open
    assert c.post(f"/api/gap-hits/{hid}", json={"csrf": tok, "action": "update"}).json()["ok"]
    assert registry.one("SELECT kind, arg FROM jobs ORDER BY id DESC")["kind"] == "update"
    assert gaps.get_gap(registry, gid)["status"] == "open" and gaps.new_count(registry) == 0
    # only the user closes the gap
    assert c.post(f"/api/gap-hits/{hid}", json={"csrf": tok, "action": "answered"}).json()["ok"]
    g = gaps.get_gap(registry, gid)
    assert g["status"] == "closed" and g["closed_by"] == pid
    assert gaps.match_paper(registry, pid) == []  # closed gaps are not matched any more
    assert c.post(f"/api/gaps/{gid}", json={"csrf": tok, "action": "reopen"}).json()["ok"]
    assert gaps.get_gap(registry, gid)["status"] == "open" and gaps.for_paper(registry, pid)[0]["status"] == "seen"
    assert c.post(f"/api/gap-hits/{hid}", json={"csrf": tok, "action": "dismissed"}).json()["ok"]
    assert gaps.for_paper(registry, pid) == []
    assert gaps.match_paper(registry, pid) == []  # a dismissed reminder doesn't come back
    assert not c.post(f"/api/gap-hits/{hid}", json={"csrf": tok, "action": "bogus"}).json()["ok"]
    assert c.post(f"/api/gaps/{gid}", json={"csrf": "x", "action": "delete"}).status_code == 403
    assert c.post(f"/api/gaps/{gid}", json={"csrf": tok, "action": "delete"}).json()["ok"]
    assert gaps.get_gap(registry, gid) is None


def test_terms_from_model_or_free_fallback(config, registry, pdfs, monkeypatch):
    c, pipe, comp, pid, qid = setup(config, registry, pdfs)
    tok = _token(c)
    monkeypatch.setattr(type(config), "model_ready", lambda self: True)
    r = c.post(f"/api/evidence/questions/{qid}/gaps", json={"csrf": tok, "text": "缺少空间转录组里成纤维细胞的定位"}).json()
    assert r["ok"] and comp.gap_calls == 1 and "Visium" in r["terms"] and "study" not in r["terms"]
    assert gaps.get_gap(registry, r["id"])["terms_origin"] == "model"
    assert registry.one("SELECT purpose FROM llm_calls ORDER BY id DESC")["purpose"] == "gap_terms"
    monkeypatch.setattr(type(config), "model_ready", lambda self: False)
    r = c.post(f"/api/evidence/questions/{qid}/gaps", json={"csrf": tok, "text": "no CXCL12 knockout in human organoids"}).json()
    assert r["ok"] and r["terms"] == ["CXCL12", "knockout", "human", "organoids"]
    # editing terms forgets untouched reminders and looks again
    gid = r["id"]
    r = c.post(f"/api/gaps/{gid}", json={"csrf": tok, "action": "edit", "terms": "Visium; fibroblast"}).json()
    assert r["ok"] and r["terms"] == ["Visium", "fibroblast"] and r["found"] == 1
    assert not c.post(f"/api/evidence/questions/{qid}/gaps", json={"csrf": tok, "text": " "}).json()["ok"]
    assert not c.post("/api/evidence/questions/99999/gaps", json={"csrf": tok, "text": "x", "terms": "a1, b2"}).json()["ok"]
