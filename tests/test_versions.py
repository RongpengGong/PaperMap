"""§16.3 end to end: a preprint the user read and quoted, then its published version with new page
numbers and a changed conclusion. One paper, both PDFs kept, old excerpts open on the original page,
changed passages put to the user."""
from fastapi.testclient import TestClient

from papermap import evidence, versions
from papermap.web.app import create_app

from .conftest import BODY, OWN_DOI, OWN_TITLE, FakeMetadata, make_pdf
from .test_pipeline import make_pipeline
from .test_web import _token

PRE = "10.1101/2026.03.03.555555"
KEEP = "Fibroblasts sit at the invasive tumour margin in all twelve patients we examined."
OLD_CONCLUSION = "We conclude that hepatic stellate cells alone drive fibrosis through TGF-beta signalling in every cohort."
NEW_CONCLUSION = "We conclude that hepatic stellate cells partly drive fibrosis through TGF-beta and IL-6 signalling in most cohorts."
DROPPED = "A small pilot suggested that ursodeoxycholic acid reverses the stellate phenotype within two weeks."


def test_preprint_then_published_keeps_excerpts_and_flags_changes(config, registry, pdfs):
    meta = FakeMetadata()
    meta.works[PRE] = {"doi": PRE, "title": OWN_TITLE, "authors": [{"family": "Zhang", "given": "Wei", "name": "Wei Zhang"}],
                       "year": 2026, "journal": "bioRxiv", "type": "posted-content", "relations": {},
                       "abstract": "Preprint abstract: stellate cells alone drive fibrosis.", "openalex_id": "W77"}
    meta.works[OWN_DOI] = {**meta.works[OWN_DOI], "relations": {"has-preprint": [PRE]},
                           "abstract": "Published abstract: stellate cells partly drive fibrosis together with IL-6."}
    pipe, comp = make_pipeline(config, registry, meta=meta)
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    inbox = pdfs["paper"].parent
    pre_pdf = make_pdf(inbox / "preprint.pdf", [f"#{OWN_TITLE}\ndoi: https://doi.org/{PRE}\n{BODY}",
                                                f"Results\n{KEEP}\n{BODY}", f"Discussion\n{OLD_CONCLUSION}\n{DROPPED}"])
    assert pipe.handle_path(pre_pdf) == "processed"
    pid = registry.paper_by_doi(PRE)["id"]
    pre_copy = pipe.pdf_for(pid)
    pre_rev = evidence.document_revision(pre_copy)

    # the user read the preprint and confirmed evidence; also highlighted a sentence
    qid = evidence.create_question(registry, "星状细胞是否单独驱动纤维化？")
    rects = [[50, 50, 300, 70]]
    e_keep = evidence.add_entry(registry, qid, None, pid, 1, pre_rev, rects, KEEP, "native", "supports", "")["id"]
    e_conc = evidence.add_entry(registry, qid, None, pid, 2, pre_rev, rects, OLD_CONCLUSION, "native", "supports", "关键")["id"]
    tok = _token(c)
    assert c.post(f"/api/reader/{pid}/annotations", json={"csrf": tok, "page": 2, "rects": rects, "text": DROPPED,
                                                           "color": "yellow"}).json()["ok"]

    # months later: the published version, with shifted pages and a reworded conclusion
    pub_pdf = make_pdf(inbox / "published.pdf", [f"#{OWN_TITLE}\nhttps://doi.org/{OWN_DOI}\n{BODY}", f"Introduction\n{BODY}",
                                                 f"Results\n{BODY}\n{KEEP}", f"Discussion\n{NEW_CONCLUSION}"])
    assert pipe.handle_path(pub_pdf) == "same_work"
    assert len([p for p in registry.list_papers() if p["status"] != "ignored"]) == 1 and comp.calls == 1  # no second card
    row = registry.get_paper(pid)
    assert row["doi"] == OWN_DOI  # the published version is now the main record
    vs = versions.versions(config.knowledge_dir, registry, pid)
    assert [v["kind"] for v in vs] == ["published", "preprint"]
    pub_file, pre_file = vs[0]["file_id"], vs[1]["file_id"]
    assert pipe.pdf_for(pid) == vs[0]["path"] != pre_copy
    assert pre_copy.exists() and evidence.document_revision(pre_copy) == pre_rev  # the preprint copy is untouched

    # old excerpts still open on the original page of the preprint
    a = c.get(f"/api/reader/{pid}/evidence/{e_conc}").json()
    assert a["ok"] and a["pv"] == pre_file and a["page"] == 2
    assert c.get(f"/api/reader/{pid}/evidence/{e_conc}", params={"pv": pre_file}).json()["pv"] is None
    assert c.get(f"/api/reader/{pid}/info").json()["revision"] != pre_rev  # default view: published
    assert c.get(f"/api/reader/{pid}/info", params={"pv": pre_file}).json()["revision"] == pre_rev
    # highlights stay on the version they were made on
    assert c.get(f"/api/reader/{pid}/info", params={"pv": pre_file}).json()["annotations"]
    assert not c.get(f"/api/reader/{pid}/info").json()["annotations"]
    reader = c.get(f"/reader/{pid}").text
    assert 'id="pdf_version"' in reader and PRE in reader

    # every quoted / highlighted passage was looked up in the new version
    got = {(x["item"], x["ref_id"]): x for x in versions.checks(registry, pid)}
    keep = got[("evidence", e_keep)]
    assert keep["status"] == "moved" and keep["new_page"] == 2 and keep["decided"] == "auto"
    conc = got[("evidence", e_conc)]
    assert conc["status"] == "changed" and conc["new_page"] == 3 and "IL-6" in conc["new_text"].upper() and not conc["decided"]
    dropped = next(x for x in got.values() if x["item"] == "annotation")
    assert dropped["status"] == "missing"
    assert got[("abstract", 0)]["status"] == "changed"

    # shown on the paper and in the review list until the user has checked them
    panel = c.get(f"/paper/{pid}/panel").text
    assert "处要核对" in panel and 'data-vcheck="' in panel
    review = c.get("/review").text
    assert OLD_CONCLUSION[:40] in review
    for x in versions.checks(registry, pid, open_only=True):
        assert c.post(f"/api/version-checks/{x['id']}", json={"csrf": tok}).json()["ok"]
    assert versions.open_count(registry) == 0 and "处要核对" not in c.get(f"/paper/{pid}/panel").text
