"""Research questions hung under fields and shown on the knowledge map (stage 7, task 2)."""
from fastapi.testclient import TestClient

from papermap import evidence
from papermap import questions as qmod
from papermap.web.app import create_app

from .test_pipeline import make_pipeline
from .test_stage4 import field_with_papers
from .test_web import _token


def web(config, registry):
    pipe, comp = make_pipeline(config, registry)
    return TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765"), pipe, comp


def test_link_question_to_fields_and_map(config, registry):
    c, pipe, _ = web(config, registry)
    st, ids = field_with_papers(registry, pipe, "spatial-transcriptomics", 2)
    sc = registry.field_by_slug("scrna-seq")
    tok = _token(c)
    qid = evidence.create_question(registry, "两类细胞相邻是否意味着功能联系？")
    for fid in (st["id"], sc["id"]):
        assert c.post(f"/api/evidence/questions/{qid}/fields", json={"csrf": tok, "field_id": fid}).json()["ok"]
    g = c.get("/api/graph").json()
    q = [n for n in g["nodes"] if n["kind"] == "question"]
    assert len(q) == 1 and q[0]["url"] == f"/evidence?question={qid}"
    assert {e["target"] for e in g["question_edges"]} == {f"f{st['id']}", f"f{sc['id']}"}
    page = c.get("/field/spatial-transcriptomics").text
    assert "两类细胞相邻是否意味着功能联系？" in page and "提议研究问题" in page
    assert "挂到学科" in c.get(f"/evidence?question={qid}").text
    # unlink one field
    r = c.post(f"/api/evidence/questions/{qid}/fields", json={"csrf": tok, "field_id": sc["id"], "on": False}).json()
    assert [f["slug"] for f in r["fields"]] == ["spatial-transcriptomics"]
    assert c.post(f"/api/evidence/questions/{qid}/fields", json={"csrf": tok, "field_id": 99999}).status_code == 400


def test_model_proposes_user_confirms(config, registry):
    c, pipe, comp = web(config, registry)
    field_with_papers(registry, pipe, "spatial-transcriptomics", 3)
    evidence.create_question(registry, "已有的问题？")
    tok = _token(c)
    r = c.post("/api/fields/spatial-transcriptomics/suggest-questions", json={"csrf": tok}).json()
    assert r == {"ok": True, "created": 1}  # single-paper and duplicate proposals are dropped
    q = registry.one("SELECT * FROM evidence_questions WHERE origin='suggested'")
    assert q["status"] == "suggested" and q["title"] == "空间相邻是否意味着功能联系？"
    assert {f["slug"] for f in qmod.question_fields(registry, q["id"])} == {"spatial-transcriptomics", "scrna-seq"}
    # a proposal is not yet knowledge: not on the map, not in the notebook
    assert not [n for n in c.get("/api/graph").json()["nodes"] if n["kind"] == "question"]
    assert all(x["id"] != q["id"] for x in c.get("/api/evidence/questions").json()["questions"])
    page = c.get("/field/spatial-transcriptomics").text
    assert "模型提议的问题" in page and "A0 2020" in page
    # the user rewrites and confirms it
    assert c.post(f"/api/evidence/questions/{q['id']}/status",
                  json={"csrf": tok, "status": "active", "title": "空间相邻能否推出功能联系？"}).json()["ok"]
    assert registry.one("SELECT title, status FROM evidence_questions WHERE id=?", (q["id"],))["status"] == "active"
    assert [n["title"] for n in c.get("/api/graph").json()["nodes"] if n["kind"] == "question"] == ["空间相邻能否推出功能联系？"]
    # a dismissed proposal is not proposed again
    c.post(f"/api/evidence/questions/{q['id']}/status", json={"csrf": tok, "status": "dismissed"})
    assert c.post("/api/fields/spatial-transcriptomics/suggest-questions", json={"csrf": tok}).json()["created"] == 0
    assert comp.questions_calls == 2
    assert c.post(f"/api/evidence/questions/{q['id']}/status", json={"csrf": tok, "status": "x"}).status_code == 400
