import json
import shutil

from papermap.llm import LLMClient
from papermap.pipeline import Pipeline, is_candidate_file
from papermap.registry import Registry

from .conftest import CARD, OWN_DOI, FakeMetadata, fake_llm_client


def make_pipeline(config, registry, outputs=(), with_key=True, meta=None, classify_outputs=(), handbook_outputs=()):
    client, completions = fake_llm_client(outputs, classify_outputs, handbook_outputs)
    llm = LLMClient(config, registry, client=client if with_key else None)
    return Pipeline(config, registry, metadata=meta or FakeMetadata(), llm=llm), completions


def test_new_paper_gets_card(config, registry, pdfs):
    pipe, comp = make_pipeline(config, registry)
    assert pipe.handle_path(pdfs["paper"]) == "processed"
    row = registry.paper_by_doi(OWN_DOI)
    assert row["status"] == "done" and row["prompt_version"] == "extract_v1"
    card = (config.knowledge_dir / row["kb_path"]).read_text()
    assert row["kb_path"].startswith("papers/2024_Zhang_JDemoHepatol_10-1234-demo-2024-001")
    assert "Finding two" in card and "doi: 10.1234/demo.2024.001" in card
    assert comp.calls == 1
    assert registry.month_cost() > 0


def test_duplicates_do_not_call_llm(config, registry, pdfs, tmp_path):
    pipe, comp = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    # same content, new name and subfolder
    sub = pdfs["paper"].parent / "zotero"
    sub.mkdir()
    copy = shutil.copy(pdfs["paper"], sub / "renamed copy.pdf")
    assert pipe.handle_path(copy) == "duplicate_file"
    assert pipe.handle_path(pdfs["paper"]) == "known"
    assert comp.calls == 1
    row = registry.paper_by_doi(OWN_DOI)
    card = (config.knowledge_dir / row["kb_path"]).read_text()
    assert "renamed copy.pdf" in card  # file paths refreshed without LLM
    assert len(registry.list_papers()) == 1


def test_restart_is_idempotent(config, registry, pdfs):
    pipe, comp = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    registry.close()
    reg2 = Registry(config.db_path)
    pipe2, comp2 = make_pipeline(config, reg2)
    assert pipe2.handle_path(pdfs["paper"]) == "known"
    assert comp2.calls == 0
    assert len([d for d in (config.knowledge_dir / "papers").iterdir() if d.is_dir()]) == 1
    reg2.close()


def test_notes_file_survives_rewrite(config, registry, pdfs):
    pipe, _ = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    row = registry.paper_by_doi(OWN_DOI)
    card = config.knowledge_dir / row["kb_path"]
    notes = card.with_name(card.stem + ".notes.md")
    assert notes.exists() and f"![[{card.stem}.notes]]" in card.read_text()
    notes.write_text("MY OWN NOTE\n")
    registry.update_paper(row["id"], status="new")
    assert pipe.process_paper(row["id"], force=True) == "processed"
    assert notes.read_text() == "MY OWN NOTE\n"


def test_no_key_waits_without_crash(config, registry, pdfs):
    pipe, _ = make_pipeline(config, registry, with_key=False)
    assert pipe.handle_path(pdfs["paper"]) == "waiting_llm"
    row = registry.paper_by_doi(OWN_DOI)
    assert row["status"] == "new" and "API key" in row["status_detail"]
    assert any("API key" in e["message"] for e in registry.recent_events())
    # once a key is available, pending papers are processed
    pipe2, comp = make_pipeline(config, registry)
    assert pipe2.process_pending() == {"processed": 1}


def test_reference_dois_are_not_taken(config, registry, pdfs):
    pipe, comp = make_pipeline(config, registry)
    outcome = pipe.handle_path(pdfs["refs_only"])
    assert outcome != "processed" or registry.list_papers()[0]["doi"] is None
    for row in registry.list_papers():
        assert row["doi"] is None


def test_unverified_doi_goes_to_review(config, registry, pdfs):
    meta = FakeMetadata()
    meta.works[OWN_DOI]["title"] = "A totally different paper about kidney transplantation outcomes"
    pipe, comp = make_pipeline(config, registry, meta=meta)
    assert pipe.handle_path(pdfs["paper"]) == "doi_unverified"
    row = registry.list_papers()[0]
    assert row["doi"] is None and row["doi_candidate"] == OWN_DOI
    assert comp.calls == 0


def test_no_doi_paper_processed_and_deduped_by_title(config, registry, pdfs):
    pipe, comp = make_pipeline(config, registry)
    assert pipe.handle_path(pdfs["nodoi"]) == "processed"
    row = registry.list_papers()[0]
    assert row["doi"] is None and "nodoi-" in row["kb_path"]
    assert "无 DOI" in (config.knowledge_dir / row["kb_path"]).read_text()
    # a re-saved copy (different bytes, same title) is recognized
    import pymupdf
    doc = pymupdf.open(pdfs["nodoi"])
    doc.set_metadata({"author": "someone"})
    other = pdfs["nodoi"].parent / "thesis_v2.pdf"
    doc.save(other)
    assert pipe.handle_path(other) == "duplicate_title"
    assert comp.calls == 1


def test_supplement_skipped(config, registry, pdfs):
    pipe, comp = make_pipeline(config, registry)
    assert pipe.handle_path(pdfs["supp"]) == "skipped_supplement"
    assert comp.calls == 0


def test_preprint_and_published_share_one_card(config, registry, pdfs, tmp_path):
    from .conftest import BODY, make_pdf
    pre = "10.1101/2023.05.05.123456"
    meta = FakeMetadata()
    meta.works[pre] = {"doi": pre, "title": meta.works[OWN_DOI]["title"], "authors": [], "year": 2023,
                       "journal": "bioRxiv", "type": "posted-content",
                       "relations": {"is-preprint-of": [OWN_DOI]}, "openalex_id": "W999"}
    pipe, comp = make_pipeline(config, registry, meta=meta)
    pipe.handle_path(pdfs["paper"])
    title = meta.works[OWN_DOI]["title"]
    pre_pdf = make_pdf(pdfs["paper"].parent / "biorxiv.pdf", [f"#{title}\ndoi: https://doi.org/{pre}\n{BODY}"])
    assert pipe.handle_path(pre_pdf) == "same_work"
    assert len(registry.list_papers()) == 1
    kinds = {r["doi"]: r["kind"] for r in registry.paper_dois(registry.list_papers()[0]["id"])}
    assert kinds == {OWN_DOI: "published", pre: "preprint"}
    assert comp.calls == 1


def test_invalid_json_retried_then_error(config, registry, pdfs):
    pipe, comp = make_pipeline(config, registry, outputs=["not json", "{\"one_sentence\": 1}"])
    assert pipe.handle_path(pdfs["paper"]) == "error"
    assert comp.calls == 2
    assert (config.logs_dir / "llm_raw").exists()
    pipe2, _ = make_pipeline(config, registry, outputs=["oops", json.dumps(CARD)])
    registry.update_paper(registry.list_papers()[0]["id"], status="new")
    assert pipe2.process_pending() == {"processed": 1}


def test_exclusion_rules(config, tmp_path):
    inbox = config.inbox_dirs[0]
    assert is_candidate_file(inbox / "a.pdf", config)
    assert not is_candidate_file(inbox / "_private" / "a.pdf", config)
    assert not is_candidate_file(inbox / "my_draft_v2.pdf", config)
    assert not is_candidate_file(inbox / "paper_SI.pdf", config)
    assert not is_candidate_file(inbox / "a.pdf.crdownload", config)
    assert not is_candidate_file(inbox / ".hidden.pdf", config)
    assert not is_candidate_file(inbox / "notes.txt", config)
