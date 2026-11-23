from fastapi.testclient import TestClient

from papermap import citations_export as ce
from papermap.web.app import create_app

from .conftest import OWN_DOI
from .test_pipeline import make_pipeline
from .test_web import _token


def test_reading_status_star_and_filters(config, registry, pdfs):
    pipe, _ = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    tok = _token(c)
    assert 'class="unread-dot"' in c.get("/").text
    c.get(f"/api/reader/{pid}/info")  # opening the reader marks it as reading
    assert registry.get_paper(pid)["read_status"] == "reading"
    assert c.post(f"/paper/{pid}/meta", json={"csrf": tok, "starred": True, "read_status": "read"}).json()["ok"]
    row = registry.get_paper(pid)
    assert row["starred"] == 1 and row["read_status"] == "read"
    assert f'data-id="{pid}"' in c.get("/papers/list?status=starred").text
    assert f'data-id="{pid}"' not in c.get("/papers/list?status=r:unread").text
    pipe._refresh_card(pid)
    card = (config.knowledge_dir / row["kb_path"]).read_text()
    assert "read_status: read" in card and "starred: true" in card


def test_bibtex_and_ris(config, registry, pdfs):
    pipe, _ = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    bib = ce.bibtex(registry, [pid, pid])
    assert bib.count("@article{") == 2 and "zhang2024spatial," in bib and "zhang2024spatiala," in bib
    assert "author = {Zhang, Wei}" in bib and "doi = {10.1234/demo.2024.001}" in bib
    ris = ce.ris(registry, [pid])
    assert ris.startswith("TY  - JOUR") and "AU  - Zhang, Wei" in ris and "DO  - 10.1234/demo.2024.001" in ris and "ER  - " in ris
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    r = c.get(f"/export.bib?ids={pid}")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"] and "@article" in r.text
    gid = registry.create_collection("G")
    assert c.get(f"/export.ris?c={gid}").text == ""
    registry.add_to_collection(gid, [pid])
    assert "TY  - JOUR" in c.get(f"/export.ris?c={gid}").text
    assert c.get("/export.txt").status_code == 404
