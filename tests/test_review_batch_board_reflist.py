"""Review-page batch actions, reference-figure boards, reference-list import and the dataset view."""
import time

from papermap import figures as figmod, reflist, resources, translation_audit
from .conftest import OWN_DOI
from .test_reader import client
from .test_web import _token


def test_translation_gaps_can_be_dismissed_until_the_translation_changes(config, registry, pdfs):
    c, _, _ = client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    translation_audit.ensure_schema(registry)
    registry.execute("INSERT INTO translation_audits(paper_id, signature, pages_json, checked_at) VALUES (?,?,?,?)",
                     (pid, "sig1", "[2, 3]", "now"))
    assert len(translation_audit.flagged(registry)) == 1
    assert "data-patch-all" in c.get("/review").text and 'id="rv-translations"' in c.get("/review").text
    tok = _token(c)
    assert c.post("/api/review/translations/dismiss", json={"csrf": tok, "all": True}).json()["dismissed"] == 1
    assert translation_audit.flagged(registry) == []
    # a new check with a different signature (translation or PDF changed) brings it back
    registry.execute("UPDATE translation_audits SET signature='sig2' WHERE paper_id=?", (pid,))
    assert len(translation_audit.flagged(registry)) == 1
    assert c.post("/api/review/translations/dismiss", json={"csrf": tok, "paper_ids": [pid]}).json()["dismissed"] == 1
    assert translation_audit.flagged(registry) == []


def test_batch_label_decisions(config, registry, pdfs):
    c, pipe, _ = client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    rows = registry.paper_field_rows(pid)
    assert rows
    registry.execute("UPDATE paper_fields SET status='suggested', confidence=0.5 WHERE paper_id=? AND field_id=?",
                     (pid, rows[0]["field_id"]))
    if len(rows) > 1:
        registry.execute("UPDATE paper_fields SET status='suggested', confidence=0.3 WHERE paper_id=? AND field_id=?",
                         (pid, rows[1]["field_id"]))
    registry.update_paper(pid, needs_review=1)
    tok = _token(c)
    assert not c.post("/api/review/labels", json={"csrf": tok, "action": "nope"}).json()["ok"]
    assert c.post("/api/review/labels", json={"csrf": tok, "action": "accept", "min_confidence": 0.45}).json()["ok"]
    status = {r["field_id"]: r["status"] for r in registry.paper_field_rows(pid, include_rejected=True)}
    assert status[rows[0]["field_id"]] == "accepted"
    if len(rows) > 1:
        assert status[rows[1]["field_id"]] == "suggested"  # below the threshold: still waiting
        assert registry.get_paper(pid)["needs_review"] == 1
        assert c.post("/api/review/labels", json={"csrf": tok, "action": "reject"}).json()["ok"]
        assert {r["field_id"]: r["status"] for r in registry.paper_field_rows(pid, include_rejected=True)}[
            rows[1]["field_id"]] == "rejected"
    assert registry.get_paper(pid)["needs_review"] == 0


def test_export_reference_board(config, registry, pdfs):
    c, pipe, _ = client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    pdf = pipe.pdf_for(pid)
    figmod.sync(registry, pid, pdf)
    figmod.ensure_schema(registry)
    if not registry.query("SELECT 1 FROM figures WHERE paper_id=?", (pid,)):
        from papermap.evidence import document_revision

        registry.execute("INSERT OR REPLACE INTO figures_state VALUES (?, ?)", (pid, document_revision(pdf)))
        registry.execute("INSERT INTO figures(paper_id,label,page,x0,y0,x1,y1,legend,created_at) "
                         "VALUES (?,'Fig. 1',0,30,30,400,300,'Fig. 1 | UMAP of cells','now')", (pid,))
    label = registry.query("SELECT label FROM figures WHERE paper_id=?", (pid,))[0]["label"]
    tok = _token(c)
    assert not c.post("/api/figures/refs/export", json={"csrf": tok, "reveal": False}).json()["ok"]
    figmod.set_ref(registry, pid, label, True, "配色好看")
    r = c.post("/api/figures/refs/export", json={"csrf": tok, "reveal": False}).json()
    assert r["ok"] and r["count"] == 1 and r["relative"].startswith("figure-boards/")
    folder = config.knowledge_dir / r["relative"]
    pngs = list((folder / "images").glob("*.png"))
    assert len(pngs) == 1 and pngs[0].read_bytes()[:4] == b"\x89PNG"
    board = (folder / "board.html").read_text("utf-8")
    assert "配色好看" in board and f"images/{pngs[0].name}" in board and OWN_DOI in board
    md = next(folder.glob("*.md")).read_text("utf-8")
    assert "配色好看" in md and f"images/{pngs[0].name}" in md
    assert "data-export-board" in c.get("/figures?ref=1").text


BIB = """@article{a, title={Spatial {T}ranscriptomics of Liver}, author={Smith, J and Lee, K}, year={2024},
 journal={Nature}, doi={10.1234/AAA.1}}
@article{b, title = "Single cell atlas of fibroblasts", author = "Wang, X", year = 2023}
"""
RIS = """TY  - JOUR
TI  - A cohort study of hepatic fibrosis
AU  - Müller, A
PY  - 2022/05/01
DO  - https://doi.org/10.1234/ris.9
ER  -
TY  - JOUR
TI  - Another paper
AN  - 33087930
ER  -
"""
PLAIN = """1. Smith J, Lee K. Spatial transcriptomics of liver. Nature 2024;600:1-10. doi:10.1234/aaa.1
2. Wang X. Single cell atlas of fibroblasts in the
   human liver. Cell 2023;180:20-30.
3. Vaswani A et al. Attention is all you need. arXiv:1706.03762 (2017).
"""


def test_parse_reference_lists():
    bib = reflist.parse(BIB)
    assert [b["doi"] for b in bib] == ["10.1234/aaa.1", None] and bib[1]["title"] == "Single cell atlas of fibroblasts"
    ris = reflist.parse(RIS)
    assert ris[0]["doi"] == "10.1234/ris.9" and ris[0]["year"] == "2022" and ris[1]["pmid"] == "33087930"
    plain = reflist.parse(PLAIN)
    assert len(plain) == 3 and plain[0]["doi"] == "10.1234/aaa.1" and "human liver" in plain[1]["raw"]
    assert plain[2]["arxiv"] == "1706.03762"
    assert reflist.looks_like_references(PLAIN) and reflist.looks_like_references(BIB) and reflist.looks_like_references(RIS)
    assert not reflist.looks_like_references("10.1234/x\nPMID: 123\nPMC77")


def test_reference_list_lookup_marks_library_and_free_pdfs(config, registry, pdfs, monkeypatch):
    c, pipe, _ = client(config, registry, pdfs)

    def fake_resolve(md, kind, value):
        if value == "10.1234/aaa.1":
            return {"doi": value, "title": "Spatial transcriptomics of liver", "oa_urls": ["https://x/a.pdf"], "authors": ["J Smith"]}
        if value == "10.1234/found":
            return {"doi": value, "title": "Single cell atlas of fibroblasts in the human liver", "oa_urls": [], "authors": ["X Wang"]}
        if value == OWN_DOI:
            return {"doi": value, "title": "own", "authors": []}
        return None

    monkeypatch.setattr("papermap.id_import.resolve", fake_resolve)
    monkeypatch.setattr(pipe.metadata, "crossref_search", lambda q, rows=3: [
        {"title": "Single cell atlas of fibroblasts in the human liver", "doi": "10.1234/found"}], raising=False)
    tok = _token(c)
    text = PLAIN.replace("3. Vaswani A et al. Attention is all you need. arXiv:1706.03762 (2017).",
                         f"3. Own paper here. doi:{OWN_DOI}")
    assert c.post("/api/import-ids", json={"csrf": tok, "text": text, "preview": True}).json()["mode"] == "refs"
    r = c.post("/api/reflist", json={"csrf": tok, "text": text}).json()
    assert r["ok"] and r["total"] == 3
    for _ in range(100):
        r = c.get(f"/api/reflist/{r['token']}").json()
        if r["finished"]:
            break
        time.sleep(0.05)
    st = [(i["status"], i.get("oa")) for i in r["items"]]
    assert st == [("found", True), ("found", False), ("already", False)]
    assert r["items"][1]["doi"] == "10.1234/found"  # matched by a bibliographic search on the reference text
    assert not c.post("/api/reflist", json={"csrf": tok, "text": "x"}).json()["ok"]


def test_dataset_view_groups_papers_by_accession(config, registry, pdfs):
    c, _, _ = client(config, registry, pdfs)
    resources.ensure_schema(registry)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    other = registry.execute("INSERT INTO papers(doi, title, status, created_at, updated_at) "
                             "VALUES ('10.1234/other', 'Other paper', 'done', 'now', 'now')").lastrowid
    for p, role in ((pid, "generated"), (other, "reused")):
        registry.execute("INSERT INTO data_resources(paper_id,kind,label,accession,url,role,page,context,created_at) "
                         "VALUES (?,'geo','GEO','GSE111111','https://geo',?,0,'data','now')", (p, role))
    registry.execute("INSERT INTO data_resources(paper_id,kind,label,accession,url,role,page,context,created_at) "
                     "VALUES (?,'pride','PRIDE','PXD000001','https://pride','',0,'data','now')", (pid,))
    page = c.get("/resources?view=datasets").text
    assert "2 个数据集" in page and "1 个被不止一篇论文提到" in page
    assert page.index("GSE111111") < page.index("PXD000001")  # shared datasets first
    assert "ds-shared" in page
    assert "GSE111111" not in c.get("/resources?view=datasets&kind=pride").text
