import time
from urllib.parse import urlsplit

import pymupdf

from papermap import evidence, versions
from .test_evidence import capture  # noqa: F401


def test_supplement_evidence_returns_to_attachment_and_keeps_main_pdf(capture, config, registry):
    web, pid, main, fields = capture
    upload = f"/paper/{pid}/supplements"
    page = "Supplemental Table S2 contains a separate cohort."
    with pymupdf.open() as doc:
        doc.new_page().insert_text((60, 80), page)
        data = doc.tobytes()
    def attach(filedata, token=fields["csrf"]):
        return web.post(upload, data={"csrf": token}, files={"file": ("Table S2.pdf", filedata, "application/pdf")})

    assert attach(data, "incorrect").status_code == 403
    assert attach(b"not a PDF").status_code == 400
    assert attach(main.read_bytes()).status_code == 400
    url = attach(data).json()["url"]
    assert attach(data).json()["url"] == url
    assert len(registry.query("SELECT * FROM paper_supplements")) == 1
    pv = int(urlsplit(url).query.split("=")[1])
    assert pv < 0
    assert "Table S2.pdf" in web.get(f"/paper/{pid}/panel").text
    assert "Table S2.pdf" in web.get(url).text
    api = f"/api/reader/{pid}"
    main_revision = web.get(api + "/info").json()["revision"]
    other = web.get(api + "/info", params={"pv": pv}).json()
    assert main_revision == fields["revision"] != other["revision"]
    assert other["pdf_version"] == pv
    assert web.get(api + "/info", params={"pv": pv - 952}).status_code == 404
    fid = web.post(api + "/evidence", params={"pv": pv},
                   json={**fields, "page": 0, "source": "native", "quote": page, "revision": other["revision"]}).json()["id"]
    direct = web.get(f"/reader/{pid}?evidence={fid}&page=1", follow_redirects=False)
    assert direct.status_code == 307 and f"pv={pv}" in direct.headers["location"]
    assert web.get(api + f"/evidence/{fid}").json()["pv"] == pv
    assert web.get(api + f"/evidence/{fid}", params={"pv": pv}).json()["quote"] == page
    assert registry.get_paper(pid)["reading_at"] is None
    assert web.post(api + "/progress", params={"pv": pv}, json={
        "csrf": fields["csrf"], "page": 0, "offset": 0.5, "at": time.time_ns() // 1000}).json()["saved"]
    assert registry.get_paper(pid)["reading_at"] is None
    assert 'data-resume-offset="0.5"' in web.get(url).text
    registry.add_annotation(pid, 0, "[[10,10,11,11]]", "supp note", "yellow", "", pv)
    registry.add_annotation(pid, 0, "[[10,10,11,11]]", "main note", "yellow", "", None)
    assert [r["text"] for r in web.get(api + "/info").json()["annotations"]] == ["main note"]
    assert [r["text"] for r in web.get(api + "/info", params={"pv": pv}).json()["annotations"]] == ["supp note"]
    path = versions.file_path(config.knowledge_dir, registry, pid, pv)
    path.write_bytes(main.read_bytes())
    assert evidence.document_revision(path) == main_revision
    assert web.get(api + f"/evidence/{fid}").status_code == 409
