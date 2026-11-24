import json

import pytest

from papermap import evidence, mobile_companion, mobile_sync, translate
from .conftest import make_pdf
from .test_plugins import symlink


@pytest.fixture
def materials(config, registry, tmp_path):
    pdf = make_pdf(tmp_path / "article.pdf", [
        "A paragraph begins here and continues on the following page",
        "with its final observations reported in the next paragraph.",
    ])
    card = config.knowledge_dir / "papers" / "Article" / "Article.md"
    card.parent.mkdir(parents=True)
    card.write_text("---\nprivate_path: local\n---\n# Article\n\n## Finding\n\n"
                    "Edited finding.\n\n## My notes\nprivate personal notes")
    pid = registry.create_paper(title="Article", status="done")
    registry.update_paper(pid, kb_path=card.relative_to(config.knowledge_dir).as_posix())
    revision = evidence.document_revision(pdf)
    registry.upsert_file(revision, str(pdf), pdf.stat().st_size, pdf.stat().st_mtime, pid)
    artifact = card.with_name("Article.translation.json")
    paras = [dict(p, tr="已译", via="a private provider name") for p in translate.paragraphs(pdf)]
    assert len(paras) == 1 and (paras[0]["page"], paras[0]["endPage"]) == (0, 1)
    return pid, revision, card, artifact, paras


def test_card_and_translation_replacement_with_historical_page_mapping(config, registry, materials, monkeypatch):
    pid, rev, card, artifact, paras = materials
    def read():
        return mobile_sync.read_companion(config, registry, pid, rev)

    first = read()
    assert first["version"] == mobile_sync.capture(config, registry, pid)["companionVersion"]
    assert first["card"] == "# Article\n\n## Finding\n\nEdited finding."
    assert first["translation"] == {"state": "missing", "paragraphs": []}
    monkeypatch.setattr(translate.ocr, "scanned_document", lambda *_: pytest.fail("No automatic OCR"))
    legacy = [{k: p[k] for k in ("kind", "src", "tr", "via")} for p in paras]
    artifact.write_text(json.dumps({"paragraphs": legacy, "service": "private", "notes": ["private warning"]}))
    historical = read()
    assert historical["version"] != first["version"]
    assert historical["translation"] == {"state": "ready", "paragraphs": [
        {k: v for k, v in paras[0].items() if k != "via"}]}
    assert "private" not in json.dumps(historical)
    artifact.write_text(json.dumps({"revision": rev, "paragraphs": [dict(legacy[0], src="Unlocated")]}))
    assert read()["translation"]["paragraphs"][0]["page"] == -1
    artifact.write_text(json.dumps({"paragraphs": [dict(legacy[0], src="Other PDF")]}))
    assert read()["translation"] == {"state": "unverified", "paragraphs": []}
    artifact.write_text(json.dumps({"revision": "0" * 64, "paragraphs": paras}))
    assert read()["translation"] == {"state": "differentRevision", "paragraphs": []}
    monkeypatch.setattr(translate, "paragraphs", lambda *_a, **_kw: pytest.fail("Saved anchors are sufficient"))
    artifact.write_text(json.dumps({"revision": rev, "paragraphs": paras}))
    assert read()["translation"] == historical["translation"]
    artifact.write_text("{")
    assert read()["translation"] == {"state": "unavailable", "paragraphs": []}
    card.write_text("---\nfile_paths: [private]\n# Incomplete card")
    assert read()["card"] == ""
    card.write_text("# Revised card")
    assert read()["card"] == "# Revised card"
    assert read()["version"] == mobile_sync.capture(config, registry, pid)["companionVersion"]


def test_symlinked_artifacts_are_not_shared(config, registry, materials):
    pid, rev, card, artifact, _ = materials
    private = config.knowledge_dir / "private.md"
    private.write_text("a private file")
    card.unlink()
    symlink(card, private)
    symlink(artifact, private)
    result = mobile_sync.read_companion(config, registry, pid, rev)
    assert result["card"] == "" and result["translation"]["state"] == "missing"


def test_only_in_scope_bounded_artifacts_are_returned(config, registry, materials, monkeypatch):
    pid, rev, card, artifact, paras = materials
    registry.update_paper(pid, kb_path="../private.md")
    assert mobile_sync.read_companion(config, registry, pid, rev)["card"] == ""
    registry.update_paper(pid, kb_path="papers/Article/Article.md")
    card.write_text("# Normal card")
    artifact.write_text(json.dumps({"revision": rev, "paragraphs": paras}))
    monkeypatch.setattr(mobile_companion, "_TRANSLATION_BYTES", 40)
    result = mobile_sync.read_companion(config, registry, pid, rev)
    assert result["card"] == "# Normal card" and result["translation"]["state"] == "unavailable"
    path = mobile_sync.pdf_path(config, registry, pid, rev)
    with path.open("ab") as f:
        f.write(b"\n% updated")
    with pytest.raises(mobile_sync.RevisionConflict):
        mobile_sync.read_companion(config, registry, pid, rev)
