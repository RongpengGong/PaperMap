"""Deleting papers (to the trash, restorable) and Q&A entries."""
from papermap import library

from .conftest import OWN_DOI
from .test_reader import client as reader_client
from .test_web import _token


def test_delete_and_restore_paper(config, registry, pdfs):
    c, pipe, _ = reader_client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    row = registry.get_paper(pid)
    folder = (config.knowledge_dir / row["kb_path"]).parent
    notes = library.notes_path(config.knowledge_dir, row["kb_path"])
    notes.write_text("读过，没用\n", "utf-8")
    n_fields = registry.one("SELECT count(*) n FROM paper_fields WHERE paper_id=?", (pid,))["n"]
    assert n_fields
    tok = _token(c)
    assert c.post("/api/papers/delete", json={"csrf": "x", "paper_ids": [pid]}).status_code == 403
    r = c.post("/api/papers/delete", json={"csrf": tok, "paper_ids": [pid]}).json()
    assert r["ok"] and r["deleted"] == 1
    row = registry.get_paper(pid)
    assert row["status"] == "ignored" and row["trash_path"] and not folder.exists()
    assert (config.data_dir / row["trash_path"] / folder.name / notes.name).exists()  # nothing is destroyed
    assert registry.one("SELECT count(*) n FROM paper_fields WHERE paper_id=?", (pid,))["n"] == 0
    assert f'data-paper="{pid}"' not in c.get("/").text.split('class="list')[1] if 'class="list' in c.get("/").text else True
    assert pipe.handle_path(pdfs["paper"]) != "new"  # the same PDF found again is not re-imported
    assert "已删除" in c.get(f"/paper/{pid}/panel").text or "restore-paper" in c.get(f"/paper/{pid}/panel").text

    r = c.post(f"/paper/{pid}/restore", json={"csrf": tok}).json()
    assert r["ok"]
    row = registry.get_paper(pid)
    assert row["status"] == "done" and not row["trash_path"] and folder.exists()
    assert notes.read_text("utf-8") == "读过，没用\n"
    assert registry.one("SELECT count(*) n FROM paper_fields WHERE paper_id=?", (pid,))["n"] == n_fields


def test_delete_qa_entry(config, registry, pdfs):
    c, pipe, _ = reader_client(config, registry, pdfs)
    tok = _token(c)
    r = c.post("/api/qa", json={"csrf": tok, "question": "成纤维细胞在哪里？"}).json()
    assert r["ok"]
    from papermap import qa

    qa.ensure_schema(registry)
    row = registry.one("SELECT * FROM qa_history WHERE id=?", (r["id"],))
    md = config.knowledge_dir / row["kb_path"]
    assert md.exists()
    assert c.post("/api/qa/delete", json={"csrf": tok, "ids": [r["id"]]}).json()["deleted"] == 1
    assert registry.one("SELECT * FROM qa_history WHERE id=?", (r["id"],)) is None
    assert not md.exists() and (config.data_dir / "trash" / "qa" / md.name).exists()
