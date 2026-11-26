"""Drawing references in the figure gallery, and regenerating cards in a new card language."""
import json

from papermap import figures as figmod, recard
from .conftest import OWN_DOI
from .test_reader import client
from .test_web import _token


def _figures(registry, pid):
    figmod.ensure_schema(registry)
    registry.execute("DELETE FROM figures")
    registry.execute("INSERT OR REPLACE INTO figures_state VALUES (?, 'rev')", (pid,))
    for i, legend in enumerate(["Fig. 1 | UMAP of cells", "Fig. 2 | Volcano plot of genes"]):
        registry.execute("INSERT INTO figures(id,paper_id,label,page,x0,y0,x1,y1,legend,created_at) "
                         "VALUES (?,?,?,0,0,0,10,10,?,'now')", (i + 1, pid, legend.split(" |")[0], legend))


def test_keep_a_figure_with_a_note_and_correct_its_types(config, registry, pdfs):
    c, _, _ = client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    _figures(registry, pid)
    tok = _token(c)
    assert c.get("/figures?ref=1").text.count('class="fig-card') == 0
    assert c.post("/api/figure/2/ref", json={"csrf": tok, "on": True}).json()["ok"]
    assert c.post("/api/figure/2/ref", json={"csrf": tok, "on": True, "note": "配色好"}).json()["ok"]
    page = c.get("/figures?ref=1").text
    assert page.count('class="fig-card') == 1 and "配色好" in page and "fig-ref-mark" in page
    # the legend says volcano; the user adds heatmap and removes volcano
    assert c.post("/api/figure/2/type", json={"csrf": tok, "kind": "heatmap", "on": True}).json()["types"] == \
        ["heatmap", "volcano"]
    r = c.post("/api/figure/2/type", json={"csrf": tok, "kind": "volcano", "on": False}).json()
    assert r["types"] == ["heatmap"]
    assert c.get("/figures?kind=heatmap").text.count('class="fig-card') == 1
    assert c.get("/figures?kind=volcano").text.count('class="fig-card') == 0
    assert figmod.refs(registry) == {(pid, "Fig. 2"): "配色好"}
    assert figmod.effective_types("Fig. 2 | Volcano plot of genes", figmod.type_edits(registry)[(pid, "Fig. 2")]) == ["heatmap"]
    # turning a type back to what the legend says removes the correction
    c.post("/api/figure/2/type", json={"csrf": tok, "kind": "volcano", "on": True})
    assert registry.one("SELECT count(*) AS n FROM figure_type_edits WHERE kind='volcano'")["n"] == 0
    assert not c.post("/api/figure/2/type", json={"csrf": tok, "kind": "nope", "on": True}).json()["ok"]
    # references survive a re-detection of the figures (new PDF revision keeps labels)
    registry.execute("DELETE FROM figures")
    _figures(registry, pid)
    assert c.get("/figures?ref=1").text.count('class="fig-card') == 1
    assert c.post("/api/figure/2/ref", json={"csrf": tok, "on": False}).json()["ok"]
    assert c.get("/figures?ref=1").text.count('class="fig-card') == 0
    assert c.post("/api/figure/2/ref", json={"csrf": "x", "on": True}).status_code == 403


def test_guess_card_language():
    assert recard.guess_language(json.dumps({"one_sentence": "这项研究发现了肝脏中的成纤维细胞小生境，并且进一步说明其作用机制与免疫细胞相关。"}, ensure_ascii=False)) == "zh"
    assert recard.guess_language(json.dumps({"one_sentence": "這項研究發現了肝臟中的成纖維細胞小生境，並且進一步說明其作用機制與免疫細胞相關。"}, ensure_ascii=False)) == "zh-TW"
    de = "Die Studie zeigt, dass die Fibroblasten und die Immunzellen in der Leber mit der Nische verbunden sind, und wurde bei Patienten mit Krebs durchgeführt."
    assert recard.guess_language(json.dumps({"one_sentence": de})) == "de"
    assert recard.guess_language(json.dumps({"one_sentence": "We mapped fibroblast niches in liver cancer."})) == "en"


def test_regenerate_cards_in_new_language_skips_edited_and_estimates(config, registry, pdfs, monkeypatch):
    c, pipe, comp = client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    config.raw["output_language"] = "zh"
    registry.update_paper(pid, card_language="zh")
    tok = _token(c)
    assert c.get("/api/recard").json()["todo"] == 0
    config.raw["output_language"] = "de"
    info = c.get("/api/recard").json()
    assert info["todo"] == 1 and info["target"] == "de" and info["estimate"]["calls"] == 1
    # a card the user edited is left alone
    registry.update_paper(pid, card_override="mein Text")
    info = c.get("/api/recard").json()
    assert info["todo"] == 0 and info["edited"] == 1
    registry.update_paper(pid, card_override=None)

    calls = []

    def fake_extract(llm, cfg, meta, text, paper_id):
        calls.append(cfg.content_language)
        from types import SimpleNamespace
        return SimpleNamespace(data={"one_sentence": "Kernaussage auf Deutsch.", "key_findings": ["x"]},
                               model="m", cost_usd=0.001)

    monkeypatch.setattr("papermap.extract.extract_paper", fake_extract)
    monkeypatch.setattr(type(pipe.llm), "check_budget", lambda self, eff=None: None)
    monkeypatch.setattr(type(pipe.llm), "_get_client", lambda self, *a, **k: object())
    r = c.post("/api/recard/start", json={"csrf": tok}).json()
    assert r["ok"] and r["queued"] == 1
    import time
    for _ in range(100):
        b = c.get("/api/recard").json()["batch"]
        if not b["running"]:
            break
        time.sleep(0.05)
    assert b["done"] == 1 and calls == ["de"]
    row = registry.get_paper(pid)
    assert row["card_language"] == "de" and "Deutsch" in row["card_json"]
    card = (config.knowledge_dir / row["kb_path"]).read_text("utf-8")
    assert "## Kernaussage" in card and "Kernaussage auf Deutsch." in card
    assert c.get("/api/recard").json()["todo"] == 0
    assert c.post("/api/recard/start", json={"csrf": tok}).json()["queued"] == 0


def test_stop_cancels_queued_and_recard_jobs_resume(config, registry, pdfs):
    c, pipe, _ = client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    registry.execute("INSERT INTO jobs(kind,paper_id,arg,options_json,status,created_at,updated_at) "
                     "VALUES ('recard',?,'de|1','','queued','now','now')", (pid,))
    assert recard.stop(registry) == 1
    assert registry.one("SELECT status FROM jobs WHERE kind='recard'")["status"] == "cancelled"
