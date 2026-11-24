import shutil

from papermap import library
from papermap.kb_writer import USER_BEGIN, USER_END

from .conftest import OWN_DOI
from .test_pipeline import make_pipeline


def test_inbox_pdf_is_cloned_not_moved(config, registry, pdfs):
    pipe, _ = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    row = registry.paper_by_doi(OWN_DOI)
    folder = (config.knowledge_dir / row["kb_path"]).parent
    stem = folder.name
    assert row["kb_path"] == f"papers/{stem}/{stem}.md"
    assert (folder / f"{stem}.pdf").read_bytes() == pdfs["paper"].read_bytes()
    assert pdfs["paper"].exists()  # original in the watched folder untouched
    assert f"[[{stem}.pdf]]" in (folder / f"{stem}.md").read_text(encoding="utf-8")


def test_dropped_pdf_is_moved_into_folder(config, registry, pdfs):
    pipe, _ = make_pipeline(config, registry)
    config.ensure_dirs()
    dropped = config.imported_dir / "my download.pdf"
    shutil.copy(pdfs["paper"], dropped)
    assert pipe.handle_path(dropped) == "processed"
    row = registry.paper_by_doi(OWN_DOI)
    folder = (config.knowledge_dir / row["kb_path"]).parent
    assert not dropped.exists() and (folder / f"{folder.name}.pdf").exists()
    assert registry.paper_file_paths(row["id"]) == [str(folder / f"{folder.name}.pdf")]
    # dropping the same file again: recognised, and the redundant copy is removed
    again = config.imported_dir / "again.pdf"
    shutil.copy(pdfs["paper"], again)
    assert pipe.handle_path(again) in ("known", "duplicate_file")
    assert not again.exists()
    assert len(list(folder.glob("*.pdf"))) == 1


def test_migrate_flat_cards(config, registry, pdfs):
    pipe, _ = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    row = registry.paper_by_doi(OWN_DOI)
    folder = (config.knowledge_dir / row["kb_path"]).parent
    stem = folder.name
    # simulate the stage-4 flat layout with a user note in the card
    flat = config.knowledge_dir / "papers" / f"{stem}.md"
    flat.write_text(f"# t\n\n## 与我相关的点\n\n{USER_BEGIN}\nOLD NOTE\n{USER_END}\n", encoding="utf-8")
    shutil.rmtree(folder)
    registry.update_paper(row["id"], kb_path=f"papers/{stem}.md")
    assert pipe.migrate_library() == 1
    row = registry.get_paper(row["id"])
    assert row["kb_path"] == f"papers/{stem}/{stem}.md"
    assert "OLD NOTE" in (folder / f"{stem}.notes.md").read_text(encoding="utf-8")
    assert (folder / f"{stem}.pdf").exists()
    assert pipe.migrate_library() == 0  # idempotent
    hub = (config.knowledge_dir / "fields" / "spatial-transcriptomics.md").read_text(encoding="utf-8")
    assert f"[[papers/{stem}/{stem}|" in hub


def test_imported_dir_is_scanned(config):
    assert config.imported_dir in config.inbox_dirs
    assert config.imported_dir not in config.watched_inboxes
