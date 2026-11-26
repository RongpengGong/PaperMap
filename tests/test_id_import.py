"""Adding papers by pasted identifiers: DOI, PMID, PMCID, arXiv, and their links."""
import time

from papermap import id_import, tracking
from .conftest import OWN_DOI
from .test_reader import client
from .test_web import _token


def test_parse_ids_reads_links_labels_and_bare_numbers():
    text = """https://doi.org/10.1038/S41586-020-2843-2.
    PMID: 33087930, https://pubmed.ncbi.nlm.nih.gov/12345678/
    PMC7581502；arXiv:1706.03762v5 https://arxiv.org/abs/2101.00001
    doi:10.1016/s0140-6736(20)30183-5 10.48550/arXiv.2203.02155 31415926"""
    assert id_import.parse_ids(text) == [
        ("doi", "10.1038/s41586-020-2843-2"), ("pmid", "33087930"), ("pmid", "12345678"),
        ("pmcid", "PMC7581502"), ("arxiv", "1706.03762"), ("arxiv", "2101.00001"),
        ("doi", "10.1016/s0140-6736(20)30183-5"), ("arxiv", "2203.02155"), ("pmid", "31415926")]
    assert id_import.parse_ids("just some words 2024") == []


def _work(doi, title, pmcid=None, pdf=None):
    w = {"id": "https://openalex.org/W9" + str(abs(hash(doi)) % 1000), "doi": f"https://doi.org/{doi}",
         "display_name": title, "publication_year": 2024, "publication_date": "2024-01-02",
         "authorships": [{"author": {"display_name": "Ada Lovelace"}}], "type": "article",
         "primary_location": {"source": {"display_name": "Nature"}, "landing_page_url": f"https://doi.org/{doi}"},
         "ids": {"pmcid": f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}"} if pmcid else {}, "locations": []}
    if pdf:
        w["best_oa_location"] = {"is_oa": True, "pdf_url": pdf}
    return w


def test_add_by_id_downloads_open_access_and_queues_the_rest(config, registry, pdfs, monkeypatch):
    c, pipe, comp = client(config, registry, pdfs)
    works = {
        "doi:10.1234/open": _work("10.1234/open", "An open access study of liver fibroblasts", pdf="https://x/open.pdf"),
        "pmid:111": _work("10.1234/closed", "A closed study of hepatic stellate cells"),
        f"doi:{OWN_DOI}": _work(OWN_DOI, "already here"),
    }

    def fake_get(url, params=None, cache=True):
        return works.get(url.rsplit("/works/", 1)[1])

    monkeypatch.setattr(pipe.metadata, "_get_json", fake_get, raising=False)
    monkeypatch.setattr(pipe.metadata, "_openalex_params", lambda: {}, raising=False)
    monkeypatch.setattr(pipe.metadata, "crossref_work", lambda doi: None, raising=False)
    got = []

    def fake_download(url, folder, name, timeout=90):
        got.append(url)
        dest = folder / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(pdfs["paper"].read_bytes())
        return dest

    monkeypatch.setattr(tracking, "_download", fake_download)
    monkeypatch.setattr(tracking, "_import_then_sync", lambda pipeline, path: None)
    tok = _token(c)
    assert c.post("/api/import-ids", json={"csrf": tok, "text": "PMID 111", "preview": True}).json()["ids"] == \
        [{"kind": "pmid", "id": "111"}]  # short numbers count only with a PMID label
    r = c.post("/api/import-ids", json={"csrf": tok, "text": f"10.1234/open\nPMID: 111\n{OWN_DOI}\n10.1234/missing"}).json()
    assert r["ok"] and r["total"] == 4
    for _ in range(100):
        r = c.get(f"/api/import-ids/{r['token']}").json()
        if r["finished"]:
            break
        time.sleep(0.05)
    st = {i["id"]: i for i in r["items"]}
    assert st["10.1234/open"]["status"] == "downloading" and got == ["https://x/open.pdf"]
    assert st["111"]["status"] == "wanted" and st["111"]["landing"] == "https://doi.org/10.1234/closed"
    assert st[OWN_DOI]["status"] == "already" and st[OWN_DOI]["paper_id"]
    assert st["10.1234/missing"]["status"] == "not_found"
    wanted = registry.one("SELECT * FROM track_candidates WHERE doi='10.1234/closed'")
    assert wanted["status"] == "wanted"
    # pasting the same DOI again reuses the candidate
    assert id_import.add_one(pipe, "pmid", "111")["candidate_id"] == wanted["id"]
    assert not c.post("/api/import-ids", json={"csrf": tok, "text": "nothing here"}).json()["ok"]
    assert c.post("/api/import-ids", json={"csrf": "x", "text": "10.1234/a"}).status_code == 403
