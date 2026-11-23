import shutil
import sqlite3
import time

from fastapi.testclient import TestClient

from papermap import library, zotero
from papermap.web.app import create_app

from .conftest import OWN_DOI
from .test_pipeline import make_pipeline
from .test_web import _token


def fake_zotero(root, pdf):
    z = root / "Zotero"
    (z / "storage" / "ATT1").mkdir(parents=True)
    shutil.copy(pdf, z / "storage" / "ATT1" / "paper.pdf")
    con = sqlite3.connect(z / "zotero.sqlite")
    con.executescript("""
    CREATE TABLE itemTypes(itemTypeID INTEGER, typeName TEXT);
    INSERT INTO itemTypes VALUES (1,'journalArticle'),(2,'attachment'),(3,'note');
    CREATE TABLE items(itemID INTEGER, itemTypeID INTEGER, key TEXT);
    INSERT INTO items VALUES (10,1,'ITEMA'),(11,2,'ATT1'),(12,3,'NOTE1'),(20,1,'NOPDF'),(30,1,'GONE');
    CREATE TABLE deletedItems(itemID INTEGER); INSERT INTO deletedItems VALUES (30);
    CREATE TABLE fields(fieldID INTEGER, fieldName TEXT); INSERT INTO fields VALUES (1,'title'),(2,'DOI');
    CREATE TABLE itemDataValues(valueID INTEGER, value TEXT);
    INSERT INTO itemDataValues VALUES (1,'Spatial paper'),(2,'10.1234/demo.2024.001'),(3,'No pdf paper');
    CREATE TABLE itemData(itemID INTEGER, fieldID INTEGER, valueID INTEGER);
    INSERT INTO itemData VALUES (10,1,1),(10,2,2),(20,1,3);
    CREATE TABLE itemAttachments(itemID INTEGER, parentItemID INTEGER, linkMode INTEGER, contentType TEXT, path TEXT);
    INSERT INTO itemAttachments VALUES (11,10,1,'application/pdf','storage:paper.pdf');
    CREATE TABLE itemNotes(itemID INTEGER, parentItemID INTEGER, note TEXT);
    INSERT INTO itemNotes VALUES (12,10,'<p>Zotero <b>重点</b>：CAF</p>');
    CREATE TABLE tags(tagID INTEGER, name TEXT); INSERT INTO tags VALUES (1,'HCC'),(2,'to-read');
    CREATE TABLE itemTags(itemID INTEGER, tagID INTEGER); INSERT INTO itemTags VALUES (10,1),(10,2);
    CREATE TABLE collections(collectionID INTEGER, collectionName TEXT, parentCollectionID INTEGER);
    INSERT INTO collections VALUES (1,'肝癌',NULL),(2,'空间组学',1);
    CREATE TABLE collectionItems(collectionID INTEGER, itemID INTEGER); INSERT INTO collectionItems VALUES (2,10);
    """)
    con.commit()
    con.close()
    return z


def test_preview_and_import(config, registry, pdfs, tmp_path):
    z = fake_zotero(tmp_path, pdfs["paper"])
    p = zotero.preview(z)
    assert (p["items"], p["with_pdf"], p["collections"], p["tags"], p["notes"]) == (2, 1, 2, 2, 1)
    assert p["without_pdf"][0]["title"] == "No pdf paper"
    pipe, comp = make_pipeline(config, registry)
    r = zotero.run_import(pipe, z)
    assert r["linked"] == 1 and r["without_pdf"] == 1
    paper = registry.paper_by_doi(OWN_DOI)
    assert paper["status"] == "done" and paper["zotero_key"] == "ITEMA"   # 1 < threshold: processed directly
    assert sorted(eval(paper["tags_json"])) == ["HCC", "to-read"]
    paths = [registry.collection_path(c["id"]) for c in registry.paper_collections(paper["id"])]
    assert paths == ["Zotero / 肝癌 / 空间组学"]
    notes = library.notes_path(config.knowledge_dir, paper["kb_path"]).read_text()
    assert "来自 Zotero 的笔记" in notes and "**重点**" in notes
    card = (config.knowledge_dir / paper["kb_path"]).read_text()
    assert "- HCC" in card
    assert (z / "storage" / "ATT1" / "paper.pdf").exists()  # Zotero's file untouched
    # re-import: no duplicates
    r2 = zotero.run_import(pipe, z)
    assert r2["new"] == 0 and len(registry.list_papers()) == 1 and len(registry.collections()) == 3
    assert notes.count("来自 Zotero 的笔记") == library.notes_path(config.knowledge_dir, paper["kb_path"]).read_text().count("来自 Zotero 的笔记")


def test_zotero_page_and_job(config, registry, pdfs, tmp_path):
    z = fake_zotero(tmp_path, pdfs["paper"])
    pipe, _ = make_pipeline(config, registry)
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    page = c.get(f"/zotero?dir={z}").text
    assert "导入 1 篇有 PDF 的论文" in page and "No pdf paper" in page
    assert c.post("/api/zotero/import", json={"csrf": _token(c), "dir": str(z)}).json()["ok"]
    for _ in range(100):
        if registry.one("SELECT status FROM jobs WHERE kind='zotero'")["status"] == "done":
            break
        time.sleep(0.05)
    assert registry.paper_by_doi(OWN_DOI) is not None
    assert "#HCC" in c.get(f"/paper/{registry.paper_by_doi(OWN_DOI)['id']}/panel").text
    assert "没有找到" in c.get(f"/zotero?dir={tmp_path / 'nope'}").text or "zotero.sqlite" in c.get(f"/zotero?dir={tmp_path / 'nope'}").text
