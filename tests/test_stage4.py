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


def test_autostart_command_for_packaged_app(monkeypatch, tmp_path):
    import sys

    from papermap import autostart
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/Applications/PaperMap.app/Contents/MacOS/PaperMap")
    cmd = autostart.command(tmp_path / "config.yaml")
    assert cmd[0].endswith("MacOS/PaperMap") and cmd[-2:] == ["app", "--background"]


def test_tray_icon_assets_exist():
    from importlib import resources
    static = resources.files("papermap.web").joinpath("static")
    assert static.joinpath("icon.png").is_file() and static.joinpath("tray.png").is_file()


def test_consolidation_selection_keeps_confirmed_and_old_papers(config, registry):
    """Task 4: a large field samples recent-added + recent-published, and confirmed evidence
    (especially contrary evidence) always gets a place; stars do not count."""
    from papermap import consolidate, evidence

    pipe, comp = make_pipeline(config, registry)
    f, ids = field_with_papers(registry, pipe, "spatial-transcriptomics", 60)  # years 2020..2079
    # a classic old paper imported last: oldest year, newest created_at
    classic = registry.create_paper(doi="10.1/classic", title="Classic", year=1990, journal="J", first_author="Old",
                                    status="done", card_json=CARD, meta_json={})
    registry.set_paper_field(classic, f["id"], status="accepted", source="llm", confidence=0.9, is_primary=1)
    registry.execute("UPDATE papers SET created_at='2999-01-01T00:00:00' WHERE id=?", (classic,))
    old_contrary, old_key, starred = ids[0], ids[1], ids[2]  # low years: would never be in the top 40 by year
    registry.execute("UPDATE papers SET starred=1 WHERE id=?", (starred,))
    q = evidence.create_question(registry, "问题")
    evidence.add_entry(registry, q, "", old_contrary, 0, "r", [[1, 1, 2, 2]], "contrary quote", "native", "opposes", "")
    registry.execute("INSERT INTO update_cards(paper_id, question_id, outcome, headline, claims_json, status, revision, "
                     "created_at, decided_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (old_key, q, "support", "又一篇支持", "[]", "confirmed", "r", "2026-01-01", "2026-01-02"))
    registry.execute("INSERT INTO update_cards(paper_id, question_id, outcome, headline, claims_json, status, revision, "
                     "created_at) VALUES (?,?,?,?,?,?,?,?)", (ids[3], q, "contradicts", "未确认", "[]", "pending", "r", "x"))
    picked = consolidate.select(registry, f["id"])
    why = {r["paper_id"]: w for r, w in picked}
    assert len(picked) == consolidate.MAX_PAPERS
    assert why[old_contrary] == "contrary" and why[old_key] == "key" and why[classic] == "added"
    assert starred not in why and ids[3] not in why  # a star or an unconfirmed card is not evidence
    assert {w for w in why.values()} == {"contrary", "key", "added", "published"}
    user = consolidate.build_user(registry.field_by_slug("spatial-transcriptomics"), consolidate.refs_for(registry, f["id"]))
    assert "user-confirmed CONTRARY evidence" in user and 'User-confirmed for question "问题": opposes' in user
    stored = consolidate.run(pipe.llm, config, registry, "spatial-transcriptomics")
    assert stored["selection"]["used"] == 40 and stored["selection"]["total"] == 61
    pipe.refresh_fields({"spatial-transcriptomics"})
    hub = (config.knowledge_dir / "fields" / "spatial-transcriptomics.md").read_text("utf-8")
    assert "最近一次整合参考了 40/61 篇" in hub and "相反证据 1" in hub
