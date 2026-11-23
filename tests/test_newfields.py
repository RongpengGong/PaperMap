import json
import shutil

import pytest
import yaml

from papermap import newfields
from papermap.taxonomy import parse_text

from .conftest import CARD, HANDBOOK
from .test_pipeline import make_pipeline


def add_paper(registry, i, doi=None):
    pid = registry.create_paper(doi=doi or f"10.1/p{i}", title=f"Paper {i}", year=2020 + i, journal="J",
                                first_author=f"Auth{i}", status="done", card_json=CARD, meta_json={"title": f"Paper {i}"})
    return pid


def prop(name_en, name_zh="", **kw):
    return {"axis": "topic", "name_en": name_en, "name_zh": name_zh, "description": f"{name_en} desc",
            "reason": "not covered", "pending_slug": "", **kw}


@pytest.fixture
def pipe(config, registry):
    p, comp = make_pipeline(config, registry)
    p.taxonomy()
    p.comp = comp
    return p


def test_variants_cluster_and_threshold(config, registry, pipe):
    tax = pipe.taxonomy()
    ids = [add_paper(registry, i) for i in range(3)]
    newfields.record_proposals(registry, config, tax, ids[0], [prop("Glioblastoma", "胶质母细胞瘤")])
    newfields.record_proposals(registry, config, tax, ids[1], [prop("Glioblastoma (GBM)")])
    slug = registry.pending_fields()[0]["slug"]
    assert slug == "glioblastoma"
    # model explicitly reuses the pending slug, even with another name
    newfields.record_proposals(registry, config, tax, ids[2], [prop("High-grade glioma", pending_slug=slug)])
    rows = registry.pending_fields()
    assert len(rows) == 1 and rows[0]["n_papers"] == 3
    assert "High-grade glioma" in json.loads(rows[0]["synonyms_json"])
    pipe.refresh_pending()
    page = (config.knowledge_dir / "_pending" / "glioblastoma.md").read_text()
    assert "3 / 3" in page
    events = [e["message"] for e in registry.recent_events() if "now has 3 papers" in e["message"]]
    pipe.refresh_pending()
    assert len(events) == 1 and len([e for e in registry.recent_events() if "now has 3 papers" in e["message"]]) == 1


def test_proposal_matching_existing_category_becomes_suggestion(config, registry, pipe):
    pid = add_paper(registry, 1)
    notes = newfields.record_proposals(registry, config, pipe.taxonomy(), pid, [prop("Hepatocellular carcinoma")])
    assert "existing 'hcc'" in notes[0]
    assert not registry.pending_fields()
    labels = {r["slug"]: r["status"] for r in registry.paper_field_rows(pid)}
    assert labels == {"hcc": "suggested"}


def test_rejected_cluster_absorbs_later_proposals(config, registry, pipe):
    tax = pipe.taxonomy()
    a, b = add_paper(registry, 1), add_paper(registry, 2)
    newfields.record_proposals(registry, config, tax, a, [prop("Breast cancer", "乳腺癌")])
    pipe.reject_pending(registry.pending_fields()[0]["id"])
    newfields.record_proposals(registry, config, tax, b, [prop("breast cancer")])
    assert not registry.pending_fields("open")
    assert not list((config.knowledge_dir / "_pending").glob("*.md"))


def test_establish_adds_category_and_attaches_papers(config, registry, pipe):
    tax = pipe.taxonomy()
    ids = [add_paper(registry, i) for i in range(2)]
    for i in ids:
        newfields.record_proposals(registry, config, tax, i, [prop("Glioblastoma", "胶质母细胞瘤")])
    pending_id = registry.pending_fields()[0]["id"]
    slug = pipe.establish_pending(pending_id, name_zh="胶质母细胞瘤")
    assert slug == "glioblastoma"
    assert config.raw["taxonomy_file"] == "taxonomy.yaml"
    text = (config.data_dir / "taxonomy.yaml").read_text()
    assert "# PaperMap taxonomy" in text  # comments kept
    assert parse_text(text).by_slug["glioblastoma"].name_zh == "胶质母细胞瘤"
    for i in ids:
        labels = {r["slug"]: (r["status"], r["source"]) for r in registry.paper_field_rows(i)}
        assert labels["glioblastoma"] == ("accepted", "user")
    assert (config.knowledge_dir / "fields" / "glioblastoma.md").exists()
    assert registry.pending_by_id(pending_id)["status"] == "established"
    with pytest.raises(ValueError):
        pipe.establish_pending(pending_id)


def test_merge_adds_synonyms(config, registry, pipe):
    pid = add_paper(registry, 1)
    newfields.record_proposals(registry, config, pipe.taxonomy(), pid, [prop("Intrahepatic biliary neoplasm", "肝内胆管肿瘤")])
    pipe.merge_pending(registry.pending_fields()[0]["id"], "cca")
    tax = parse_text((config.data_dir / "taxonomy.yaml").read_text())
    assert "Intrahepatic biliary neoplasm" in tax.by_slug["cca"].synonyms
    assert {r["slug"] for r in registry.paper_field_rows(pid)} == {"cca"}
    # next time the same name routes straight to the existing category
    pid2 = add_paper(registry, 2)
    notes = newfields.record_proposals(registry, config, pipe.taxonomy(), pid2, [prop("Intrahepatic biliary neoplasm")])
    assert "existing 'cca'" in notes[0]


def test_handbook_keeps_only_cited_items(config, registry, pipe):
    field = registry.field_by_slug("spatial-transcriptomics")
    for i in range(2):
        pid = add_paper(registry, i)
        registry.set_paper_field(pid, field["id"], status="accepted", source="llm", confidence=0.9, is_primary=1)
    rel = pipe.generate_handbook("spatial-transcriptomics")
    text = (config.knowledge_dir / rel).read_text()
    a, b = text.split("## B.")
    assert "Study one mapped niches" in a and "(https://doi.org/10.1/p" in a
    assert "Invented claim" not in text and "Uncited claim" not in text
    assert "需核实" in text and "Spot is not a cell" in b
    # user notes survive regeneration
    path = config.knowledge_dir / rel
    t = path.read_text()
    s = t.index("<!-- user:begin -->") + len("<!-- user:begin -->\n")
    path.write_text(t[:s] + "MY HB NOTE\n" + t[t.index("<!-- user:end -->"):])
    pipe.generate_handbook("spatial-transcriptomics")
    assert "MY HB NOTE" in path.read_text()
    hub = (config.knowledge_dir / "fields" / "spatial-transcriptomics.md").read_text()
    assert "[[handbooks/spatial-transcriptomics|学习手册]]" in hub


def test_handbook_without_valid_citations_is_retried(config, registry):
    bad = json.loads(json.dumps(HANDBOOK))
    for k in ("representative", "methods", "findings"):
        bad["A"][k] = [{"text": "x", "refs": ["P99"]}]
    p, comp = make_pipeline(config, registry, handbook_outputs=[json.dumps(bad)])
    p.taxonomy()
    field = registry.field_by_slug("scrna-seq")
    pid = add_paper(registry, 1)
    registry.set_paper_field(pid, field["id"], status="accepted", source="llm", confidence=0.9, is_primary=1)
    p.generate_handbook("scrna-seq")
    assert comp.handbook_calls == 2


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_knowledge_git_commits(config, registry, pdfs):
    import subprocess
    config.raw["knowledge_git"] = True
    p, _ = make_pipeline(config, registry)
    p.handle_path(pdfs["paper"])
    log = subprocess.run(["git", "-C", str(config.knowledge_dir), "log", "--oneline"], capture_output=True, text=True)
    assert "Add paper" in log.stdout


def test_web_pending_flow(config, registry, pipe):
    from fastapi.testclient import TestClient

    from papermap.web.app import create_app

    from .test_web import _token
    ids = [add_paper(registry, i) for i in range(3)]
    for i in ids:
        newfields.record_proposals(registry, config, pipe.taxonomy(), i, [prop("Glioblastoma", "胶质母细胞瘤")])
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    review = c.get("/review").text
    assert "已达阈值" in review and "3 / 3 篇" in review
    assert '<span class="badge">1</span>' in review
    r = c.post("/review/pending", data={"csrf": _token(c), "pending_id": registry.pending_fields()[0]["id"],
                                        "action": "establish", "slug": "glioblastoma", "name_zh": "胶质母细胞瘤",
                                        "name_en": "Glioblastoma", "description": "GBM", "axis": "topic"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/field/glioblastoma")
    assert c.get("/field/glioblastoma").status_code == 200
    bad = c.post("/review/pending", data={"csrf": _token(c), "pending_id": 999, "action": "reject"}, follow_redirects=False)
    assert "error=" in bad.headers["location"]
