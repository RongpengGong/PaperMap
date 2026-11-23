import json

import pytest

from papermap import taxonomy as taxmod

from .conftest import CLASSIFY, OWN_DOI, FakeMetadata
from .test_pipeline import make_pipeline


def test_builtin_taxonomies_load():
    for name in taxmod.BUILTIN:
        t = taxmod.parse_text(taxmod.builtin_text(name))
        assert t.axis("method") and t.axis("topic")
    liver = taxmod.parse_text(taxmod.builtin_text("liver_omics"))
    assert {"scrna-seq", "spatial-transcriptomics", "hcc", "cca", "psc"} <= set(liver.by_slug)


def test_taxonomy_validation_messages():
    bad = """
method:
  - {slug: Bad Slug, name_zh: a, name_en: a, description: d}
  - {slug: ok, name_zh: a, name_en: a, description: d}
  - {slug: ok, name_zh: a, name_en: a}
topic: []
"""
    with pytest.raises(taxmod.TaxonomyError) as e:
        taxmod.parse_text(bad)
    msg = str(e.value)
    assert "slug must be" in msg and "duplicate slug" in msg and "missing description" in msg
    assert "'topic' must be a non-empty list" in msg


def test_legacy_taxonomy_path_maps_to_builtin(tmp_path):
    t = taxmod.load("taxonomies/default.yaml", tmp_path)
    assert "bulk-omics" in t.by_slug


def classified(config, registry, pdfs, **kw):
    pipe, comp = make_pipeline(config, registry, **kw)
    assert pipe.handle_path(pdfs["paper"]) == "processed"
    return pipe, comp, registry.paper_by_doi(OWN_DOI)


def test_classification_assigns_labels_and_flags_review(config, registry, pdfs):
    pipe, comp, row = classified(config, registry, pdfs)
    assert comp.classify_calls == 1
    labels = {r["slug"]: r for r in registry.paper_field_rows(row["id"])}
    assert labels["spatial-transcriptomics"]["status"] == "accepted" and labels["spatial-transcriptomics"]["is_primary"]
    assert labels["tumor-microenvironment"]["status"] == "accepted"
    assert labels["wet-lab-mechanism"]["status"] == "suggested"  # 0.45 < 0.6
    assert row["needs_review"] == 1
    props = registry.query("SELECT * FROM field_proposals")
    assert props[0]["name_en"] == "Glioma"
    # taxonomy is in the prompt
    assert "spatial-transcriptomics |" in comp.last_system


def test_card_and_hub_pages_link_each_other(config, registry, pdfs):
    pipe, comp, row = classified(config, registry, pdfs)
    card = (config.knowledge_dir / row["kb_path"]).read_text()
    assert "[[fields/spatial-transcriptomics|空间转录组]]（主要）" in card
    assert "fields:\n- spatial-transcriptomics\n- scrna-seq" in card
    assert "topics:\n- tumor-microenvironment" in card
    hub = (config.knowledge_dir / "fields" / "spatial-transcriptomics.md").read_text()
    stem = row["kb_path"].removesuffix(".md")
    assert f"[[{stem}|Zhang 2024" in hub and "★" in hub
    assert "Visium niches" in hub  # progress note from the classifier
    assert "`Seurat`" in hub and "[[fields/tumor-microenvironment|肿瘤微环境]] ×1" in hub
    # suggested-only field gets no paper entry
    wet = (config.knowledge_dir / "fields" / "wet-lab-mechanism.md")
    assert not wet.exists() or stem not in wet.read_text()


def test_hub_user_block_survives(config, registry, pdfs):
    pipe, comp, row = classified(config, registry, pdfs)
    hub = config.knowledge_dir / "fields" / "spatial-transcriptomics.md"
    text = hub.read_text()
    a = text.index("<!-- user:begin -->") + len("<!-- user:begin -->\n")
    hub.write_text(text[:a] + "MY FIELD NOTE\n" + text[text.index("<!-- user:end -->"):])
    pipe.classify_papers(mode="all")
    assert "MY FIELD NOTE" in hub.read_text()


def test_user_decisions_stick_across_reclassify(config, registry, pdfs):
    pipe, comp, row = classified(config, registry, pdfs)
    pipe.set_label(row["id"], "wet-lab-mechanism", "accept")
    pipe.set_label(row["id"], "scrna-seq", "reject")
    assert registry.get_paper(row["id"])["needs_review"] == 0
    pipe.classify_papers(mode="all")
    labels = {r["slug"]: r for r in registry.paper_field_rows(row["id"], include_rejected=True)}
    assert labels["wet-lab-mechanism"]["status"] == "accepted" and labels["wet-lab-mechanism"]["source"] == "user"
    assert labels["scrna-seq"]["status"] == "rejected"
    card = (config.knowledge_dir / row["kb_path"]).read_text()
    assert "scrna-seq" not in card.split("---")[1]


def test_taxonomy_change_marks_stale_and_reclassify(config, registry, pdfs):
    pipe, comp, row = classified(config, registry, pdfs)
    assert pipe.stale_classification_count() == 0
    text = taxmod.builtin_text("liver_omics").replace(
        "  - slug: aging\n", "  - slug: glioma\n    name_zh: 胶质瘤\n    name_en: Glioma\n    description: Gliomas incl. glioblastoma.\n  - slug: aging\n")
    (config.data_dir / "taxonomy.yaml").write_text(text)
    config.raw["taxonomy_file"] = "taxonomy.yaml"
    assert pipe.stale_classification_count() == 1
    new = json.loads(json.dumps(CLASSIFY))
    new["topics"].append({"slug": "glioma", "confidence": 0.9, "reason": "r"})
    pipe2, comp2 = make_pipeline(config, registry, classify_outputs=[json.dumps(new)])
    assert pipe2.classify_papers(mode="stale") == {"classified": 1}
    assert "glioma" in {r["slug"] for r in registry.paper_field_rows(row["id"])}
    assert pipe2.stale_classification_count() == 0
    assert (config.knowledge_dir / "fields" / "glioma.md").exists()


def test_unknown_slug_is_retried(config, registry, pdfs):
    bad = json.loads(json.dumps(CLASSIFY))
    bad["methods"][0]["slug"] = "made-up-method"
    pipe, comp, row = classified(config, registry, pdfs, classify_outputs=[json.dumps(bad)])
    assert comp.classify_calls == 2
    assert registry.get_paper(row["id"])["classify_key"]


def test_resolve_unverified_with_user_doi(config, registry, pdfs):
    meta = FakeMetadata()
    real_title = meta.works[OWN_DOI]["title"]
    meta.works[OWN_DOI]["title"] = "Completely different title about kidneys and transplant"
    pipe, comp = make_pipeline(config, registry, meta=meta)
    assert pipe.handle_path(pdfs["paper"]) == "doi_unverified"
    pid = registry.list_papers()[0]["id"]
    meta.works[OWN_DOI]["title"] = real_title
    assert pipe.resolve_unverified(pid, "https://doi.org/" + OWN_DOI) == "processed"
    row = registry.get_paper(pid)
    assert row["doi"] == OWN_DOI and row["status"] == "done"


def test_tool_name_normalization():
    from papermap.kb_fields import tool_name
    assert tool_name("CellPhoneDB v5.0.0") == "CellPhoneDB"
    assert tool_name("SCENIC v1.3.1（GRNBoost2、RcisTarget）") == "SCENIC"
    assert tool_name("R 4.1.0") == "R"
    assert tool_name("Seurat") == "Seurat"
