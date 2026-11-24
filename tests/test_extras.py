"""⌘K jump, what to read today, status check, guide."""
from .conftest import OWN_DOI
from .test_reader import client as reader_client
from .test_web import _token


def test_jump_today_status_guide(config, registry, pdfs):
    c, pipe, _ = reader_client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    tok = _token(c)
    items = c.get("/api/jump", params={"q": "fibroblast"}).json()["items"]
    assert any(i["kind"] == "paper" and i["url"] == f"/?id={pid}" for i in items)
    assert c.get("/api/jump", params={"q": "<script>"}).json()["ok"]
    # today: starred but unread, and marked "to read"
    c.post(f"/paper/{pid}/meta", json={"csrf": tok, "starred": True})
    c.post("/api/purposes", json={"csrf": tok, "paper_ids": [pid], "tag": "toread"})
    home = c.get("/").text
    assert 'id="today_box"' in home and "待精读" in home
    assert 'id="today_box"' not in c.get("/", params={"q": "x"}).text  # hidden while searching
    st = c.get("/status")
    assert st.status_code == 200 and "数据库备份" in st.text and "全文索引" in st.text
    assert c.get("/guide").status_code == 200
    assert 'data-jump' in home and "jump.js" in home
