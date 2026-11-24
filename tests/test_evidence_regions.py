from html import escape

import pymupdf

from papermap import evidence
from .test_evidence import capture  # noqa: F401 - shared reader fixture


def test_region_is_a_revision_bound_color_crop_and_survives_source_loss(capture, registry):
    web, pid, pdf, data = capture
    with pymupdf.open() as doc:
        pg = doc.new_page(width=200, height=300)
        pg.draw_rect((10, 50, 40, 70), color=(1, 0, 0), fill=(1, 0, 0))
        pg.set_rotation(90)
        rect = list(pymupdf.Rect(15, 55, 30, 65) * pg.rotation_matrix)
        pdf.write_bytes(doc.tobytes())
    args = {**data, "page": 0, "rects": [rect], "revision": evidence.document_revision(pdf),
            "source": "region", "region_label": "Fig. 3B", "quote": "", "note": "My interpretation",
            "dimension": "findings"}
    add = f"/api/reader/{pid}/evidence"
    for bad in ({"rects": [rect, rect]}, {"region_label": ""}, {"rects": [[1, 1, 1.2, 3]]}):
        assert web.post(add, json={**args, **bad}).status_code == 400
    saved = web.post(add, json=args).json()
    assert saved["created"]
    eid, qid = saved["id"], saved["question_id"]
    assert not web.post(add, json=args).json()["created"]
    q2 = web.post(add, json={**args, "question_title": "Other question", "note": ""}).json()
    assert q2["created"] and len(registry.query("SELECT 1 FROM evidence_region_previews")) == 1
    preview = f"/api/evidence/entries/{eid}/preview.png"
    image = web.get(preview)
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    bitmap = pymupdf.Pixmap(image.content)
    assert (bitmap.width, bitmap.height, bitmap.pixel(4, 4)) == (20, 30, (255, 0, 0))
    reader_url = f"/reader/{pid}?evidence={eid}&page=1"
    for view in ("", "&view=compare"):
        html = web.get(f"/evidence?question={qid}{view}").text
        assert preview in html and escape(reader_url) in html and "Fig. 3B" in html and args["note"] in html
    md = web.get(f"/evidence/{qid}.md").text
    assert preview in md and reader_url in md and "Fig. 3B" in md and "> My interpretation" not in md
    entry, = evidence.question_comparison(registry, qid)["papers"][0]["evidence"]
    assert entry["source"] == "region" and entry["region_label"] == "Fig. 3B" and entry["quote"] == ""
    assert web.get(f"{add}/{eid}").json()["rects"] == [rect]
    pdf.write_bytes(pdf.read_bytes() + b"\n% a different revision\n")
    assert web.post(add, json=args).status_code == 409
    assert web.get(f"{add}/{eid}").status_code == 409
    assert web.get(preview).content == image.content
    remove = lambda i: web.post(f"/api/evidence/entries/{i}/delete", json={"csrf": data["csrf"]})
    assert remove(eid).json()["ok"] and web.get(preview).status_code == 404
    assert web.get(f"/api/evidence/entries/{q2['id']}/preview.png").content == image.content
    assert remove(q2["id"]).json()["ok"] and not registry.query("SELECT 1 FROM evidence_region_previews")


def test_figure_label_in_new_pdf_never_confirms_a_visual_match(capture, registry, config, tmp_path):
    from papermap import versions
    web, pid, pdf, data = capture
    eid = web.post(f"/api/reader/{pid}/evidence", json={
        **data, "source": "region", "region_label": "Figure 3B", "quote": "",
    }).json()["id"]
    old_file = registry.one("SELECT id FROM files WHERE sha256=?", (data["revision"],))["id"]
    versions.register(registry, pid, old_file, "preprint", None)
    revised = tmp_path / "new version.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((50, 100), "Figure 3B")
        doc.save(revised)
    sha = evidence.document_revision(revised)
    new_file = registry.upsert_file(sha, str(revised), revised.stat().st_size, revised.stat().st_mtime, pid)
    versions.register(registry, pid, new_file, "published", None)
    stats = versions.compare(config.knowledge_dir, registry, pid, old_file, new_file)
    assert stats["visual_review"] == 1
    check, = versions.checks(registry, pid, open_only=True)
    assert (check["status"], check["old_text"], check["new_page"]) == ("visual_review", "Figure 3B", None)
    anchor = web.get(f"/api/reader/{pid}/evidence/{eid}").json()
    assert anchor["pv"] == old_file and anchor["page"] == data["page"]
