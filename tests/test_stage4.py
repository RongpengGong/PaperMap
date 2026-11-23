import json

from papermap import consolidate as consmod

from .conftest import CARD
from .test_newfields import add_paper
from .test_pipeline import make_pipeline
from .test_web import _token


def field_with_papers(registry, pipe, slug, n, tools=None):
    pipe.taxonomy()
    f = registry.field_by_slug(slug)
    ids = []
    for i in range(n):
        card = dict(CARD, tools=tools or CARD["tools"])
        pid = registry.create_paper(doi=f"10.1/{slug}{i}", title=f"P{i}", year=2020 + i, journal="J",
                                    first_author=f"A{i}", status="done", card_json=card, meta_json={})
        registry.set_paper_field(pid, f["id"], status="accepted", source="llm", confidence=0.9, is_primary=1,
                                 note=f"note {i}")
        ids.append(pid)
    return f, ids


def test_consolidation_cited_and_versioned(config, registry):
    pipe, comp = make_pipeline(config, registry)
    f, ids = field_with_papers(registry, pipe, "spatial-transcriptomics", 3)
    assert consmod.due(registry, f["id"], every=3) and not consmod.due(registry, f["id"], every=4)
    pipe.consolidate_field("spatial-transcriptomics")
    hub = (config.knowledge_dir / "fields" / "spatial-transcriptomics.md").read_text()
    assert "Fields combine Visium with scRNA-seq" in hub and "Made-up overview" not in hub
    assert "finds A at margins while" in hub and "(P1" not in hub and "Single-paper" not in hub
    assert "整合于" in hub and "基于 3 篇论文" in hub
    assert "逐篇记录" in hub and "note 0" in hub  # per-paper notes kept
    assert not consmod.due(registry, f["id"], every=3)
    pipe.consolidate_field("spatial-transcriptomics")
    assert registry.one("SELECT count(*) AS n FROM field_syntheses")["n"] == 2  # history kept


def test_auto_consolidation_after_classification(config, registry, pdfs):
    config.raw["consolidate_every"] = 1
    pipe, comp = make_pipeline(config, registry)
    field_with_papers(registry, pipe, "spatial-transcriptomics", 1)
    pipe.handle_path(pdfs["paper"])  # classified into spatial-transcriptomics -> 2 papers, due
    assert getattr(comp, "consolidate_calls", 0) >= 1
    assert registry.one("SELECT count(*) AS n FROM field_syntheses")["n"] >= 1


def test_concept_pages(config, registry):
    pipe, comp = make_pipeline(config, registry)
    field_with_papers(registry, pipe, "scrna-seq", 2, tools=["Seurat v5.0.1", "CellChat"])
    pid = add_paper(registry, 9)  # single use tool
    registry.update_paper(pid, card_json=dict(CARD, tools=["RareTool"]))
    pipe.refresh_concepts()
    pipe.refresh_fields({"scrna-seq"})
    d = config.knowledge_dir / "concepts"
    assert {p.stem for p in d.glob("*.md")} == {"seurat", "cellchat"}
    page = (d / "seurat.md").read_text()
    assert "2 篇" in page and "[[fields/scrna-seq|单细胞转录组]] ×2" in page and "[[concepts/cellchat|CellChat]] ×2" in page
    hub = (config.knowledge_dir / "fields" / "scrna-seq.md").read_text()
    assert "[[concepts/seurat|Seurat]] ×2" in hub


def test_hold_and_estimate(config, registry, pdfs):
    pipe, comp = make_pipeline(config, registry)
    pipe.hold = True
    assert pipe.handle_path(pdfs["paper"]) == "held"
    assert comp.calls == 0 and pipe.pending_count() == 1
    config.raw["llm"]["custom"] = {}
    est = pipe.estimate()
    assert est["papers"] == 1 and est["tokens_in"] == 20000 and est["cost_peak"] >= est["cost_offpeak"] > 0
    pipe.hold = False
    assert pipe.process_pending().get("processed") == 1


def test_scan_noninteractive_needs_yes(config, registry, pdfs, monkeypatch, capsys):
    from papermap import cli
    config.raw["bulk_confirm_threshold"] = 1
    config.raw["llm"]["api_key"] = "sk-test"
    config.save()
    pipe, comp = make_pipeline(config, registry)
    monkeypatch.setattr(cli, "_load", lambda args: (config, registry, pipe))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    cli.main(["scan"])
    out = capsys.readouterr().out
    assert "re-run with --yes" in out and comp.calls == 0
    cli.main(["scan", "--yes"])
    assert comp.calls >= 1


def test_web_costs_and_bulk(config, registry, pdfs):
    from fastapi.testclient import TestClient

    from papermap.web.app import create_app
    pipe, comp = make_pipeline(config, registry)
    pipe.hold = True
    pipe.handle_path(pdfs["paper"])
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    home = c.get("/").text
    assert "篇新论文等待处理" in home
    assert c.get("/costs").status_code == 200
    r = c.post("/bulk/start", data={"csrf": _token(c)}, follow_redirects=False)
    assert r.status_code == 303 and pipe.hold is False
    import time
    for _ in range(50):
        if registry.list_papers()[0]["status"] == "done":
            break
        time.sleep(0.05)
    assert registry.list_papers()[0]["status"] == "done"
    assert c.get("/concepts").status_code == 200


def test_ref_ids_in_prose_become_author_year():
    from papermap.llm import replace_ref_ids
    labels = {"P1": "Cai 2026", "P2": "de Jong 2026"}
    assert replace_ref_ids("51 samples (P1) vs P2; P10 stays", labels) == "51 samples (Cai 2026) vs de Jong 2026; P10 stays"
