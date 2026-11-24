"""Purpose tags (stage 7 task 7): toggle, sidebar entries, list filter, export by tag."""
from .conftest import OWN_DOI
from .test_reader import client as reader_client
from .test_web import _token


def test_purpose_tags_filter_and_export(config, registry, pdfs):
    c, pipe, _ = reader_client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    tok = _token(c)
    panel = c.get(f"/paper/{pid}/panel").text
    assert 'data-purpose="intro"' in panel and 'data-purpose-add' in panel
    assert c.post("/api/purposes", json={"csrf": "x", "paper_ids": [pid], "tag": "intro"}).status_code == 403
    assert c.post("/api/purposes", json={"csrf": tok, "paper_ids": [pid], "tag": "intro", "on": True}).json()["changed"] == 1
    assert c.post("/api/purposes", json={"csrf": tok, "paper_ids": [pid], "tag": "  基金 #申请 ", "on": True}).json()["ok"]
    assert not c.post("/api/purposes", json={"csrf": tok, "paper_ids": [pid], "tag": " "}).json()["ok"]
    home = c.get("/").text
    assert 'data-cid="u:intro"' in home and "引言可引用" in home and 'data-cid="u:基金 申请"' in home
    assert 'class="purpose-chip on" data-purpose="intro"' in c.get(f"/paper/{pid}/panel").text
    listed = c.get("/papers/list", params={"c": "u:intro"}).text
    assert f'data-id="{pid}"' in listed
    assert f'data-id="{pid}"' not in c.get("/papers/list", params={"c": "u:methods"}).text
    bib = c.get("/export.bib", params={"c": "u:intro"}).text
    assert "@article" in bib.lower() and OWN_DOI.lower() in bib.lower()
    assert c.get("/export.bib", params={"c": "u:methods"}).text.strip() == ""
    # starred list works the same way
    c.post(f"/paper/{pid}/meta", json={"csrf": tok, "starred": True})
    assert 'data-cid="star"' in c.get("/").text and f'data-id="{pid}"' in c.get("/papers/list", params={"c": "star"}).text
    assert c.post("/api/purposes", json={"csrf": tok, "paper_ids": [pid], "tag": "intro", "on": False}).json()["changed"] == 1
    assert 'data-cid="u:intro"' not in c.get("/").text
