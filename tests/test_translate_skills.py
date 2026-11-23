import json
import re
import time
from pathlib import Path

import httpx
import pytest

from papermap import skills_runtime as sr
from papermap import translate as tr

from .conftest import OWN_DOI, make_pdf
from .test_pipeline import make_pipeline


def test_paragraphs_drop_references_and_find_headings(pdfs):
    paras = tr.paragraphs(pdfs["paper"])
    text = " ".join(p["src"] for p in paras)
    assert "doi:10.1038/s41586-020-0001-1" not in text and "Smith, J. et al." not in text
    assert any(p["kind"] == "heading" for p in paras)
    assert all(len(p["src"]) >= 5 for p in paras)


def test_prompt_follows_index_translate_format():
    p = tr.build_prompt("[1] Seurat was used.\n\n[2] ABCA8 fibroblasts.", "zh", "translate", "", ["Seurat", "ABCA8"], True)
    assert p.startswith("请将以下论文文本翻译成中文，并且严格遵循所有约束要求。")
    assert "【源文】" in p and "【硬性要求】专名/术语对照: Seurat→Seurat、ABCA8→ABCA8" in p
    assert "保留每段开头的方括号编号" in p and p.endswith("只输出译文，不要有任何额外说明。")
    assert tr.build_prompt("x", "zh", "custom", "Translate to {target}: {text}", [], False) == "Translate to 中文: x"
    assert tr.parse_numbered("[1] 甲\n\n[2] 乙", [1, 2]) == {1: "甲", 2: "乙"}
    assert tr.parse_numbered("[1] 甲 乙", [1, 2]) is None


def test_glossary_collects_tools_and_genes():
    card = {"tools": ["Seurat v5", "CellChat（v2）"], "design": {"platforms": ["Visium"]}}
    text = "We used Seurat and CellChat on Visium. ABCA8 and ABCA8 and CD44 CD44 were high. DNA DNA."
    g = tr.glossary(card, None, text)
    assert {"Seurat", "CellChat", "Visium", "ABCA8", "CD44"} <= set(g) and "DNA" not in g


def _bilibili(handler):
    return httpx.MockTransport(handler)


def test_translate_paper_with_bilibili_mock(config, registry, pdfs):
    pipe, comp = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    row = registry.paper_by_doi(OWN_DOI)
    pdf, card = pipe._paper_files(row["id"])
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        nums = re.findall(r"^\[(\d+)\]", body["messages"][0]["content"], re.M)
        out = "\n\n".join(f"[{n}] 译文{n}" for n in nums) if nums else "单段译文"
        return httpx.Response(200, text="data: " + json.dumps({"choices": [{"delta": {"content": out}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}) + "\n\ndata: [DONE]\n\n")

    out = tr.translate_paper(config, registry, pipe.llm, row["id"], pdf, card, pipe.taxonomy(),
                             transport=_bilibili(handler))
    data = json.loads(out.read_text())
    assert data["paragraphs"] and all(p["tr"] for p in data["paragraphs"])
    assert data["model"] == "Index-Translate-35B-A3B" and seen[0]["chat_template_kwargs"] == {"enable_thinking": False}
    md = card.with_name(card.stem + ".translation.md").read_text()
    assert "译文对照" in md and re.search(r"^> (译文\d+|单段译文)", md, re.M)
    assert registry.one("SELECT count(*) AS n FROM llm_calls WHERE purpose='translate'")["n"] == len(seen)


def test_misaligned_answer_falls_back_to_single_paragraphs(config, registry, pdfs):
    pipe, comp = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    pdf, card = pipe._paper_files(registry.paper_by_doi(OWN_DOI)["id"])
    handler = lambda req: httpx.Response(200, text="data: " + json.dumps({"choices": [{"delta": {"content": "全部混在一起的译文"}}]}) + "\n\ndata: [DONE]\n\n")
    out = tr.translate_paper(config, registry, pipe.llm, 1, pdf, card, transport=_bilibili(handler))
    assert all(p["tr"] == "全部混在一起的译文" for p in json.loads(out.read_text())["paragraphs"] if p["kind"] != "meta")


def test_service_failure_falls_back_to_main_model(config, registry, pdfs, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    config.raw["llm"]["api_key"] = "sk-test"
    pipe, comp = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    pdf, card = pipe._paper_files(1)
    out = tr.translate_paper(config, registry, pipe.llm, 1, pdf, card,
                             transport=_bilibili(lambda req: httpx.Response(503)))
    data = json.loads(out.read_text())
    assert all(p["tr"].startswith("主模型译文") for p in data["paragraphs"] if p["kind"] != "meta")
    assert data["notes"] and "(fallback)" in data["model"]
    config.raw["translation"]["fallback_to_main"] = False
    with pytest.raises(Exception):
        tr.translate_paper(config, registry, pipe.llm, 1, pdf, card, transport=_bilibili(lambda req: httpx.Response(503)))


def test_skill_discovery_and_safety(config, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    claude = tmp_path / "home" / ".claude" / "skills"
    (claude / "reader").mkdir(parents=True)
    (claude / "reader" / "SKILL.md").write_text("---\nname: reader\ndescription: reads\n---\nRead it. See ../shared/core/x.md\n")
    (claude / "shared" / "core").mkdir(parents=True)
    (claude / "shared" / "core" / "x.md").write_text("SHARED REFERENCE")
    (claude / "scripted" / "scripts").mkdir(parents=True)
    (claude / "scripted" / "SKILL.md").write_text("---\nname: scripted\n---\nRun python scripts/x.py")
    (claude / "scripted" / "scripts" / "x.py").write_text("print(1)")
    skills = sr.discover(config)
    assert skills["papermap-close-reading"].source == "builtin" and skills["papermap-close-reading"].supported
    assert skills["reader"].supported and not skills["scripted"].supported and "x.py" in skills["scripted"].reason
    assert ("x.md", "SHARED REFERENCE") in sr._references(skills["reader"])
    assert sr.default_skill_id(config, skills) == "papermap-close-reading"
    config.raw["skills"]["default"] = "reader"
    assert sr.default_skill_id(config, skills) == "reader"
    config.raw["skills"]["default"] = "scripted"  # unsupported -> ignored
    assert sr.default_skill_id(config, skills) == "papermap-close-reading"
    with pytest.raises(ValueError):
        sr.install(config, tmp_path)
    installed = sr.install(config, claude / "reader")
    assert installed.source == "papermap" and (config.data_dir / "skills" / "reader" / "SKILL.md").exists()


def test_jobs_run_skill_and_translation(config, registry, pdfs, monkeypatch):
    config.raw["llm"]["api_key"] = "sk-test"
    config.raw["translation"]["default"] = "main"
    pipe, comp = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    pipe.submit_skill(pid, "papermap-close-reading")
    pipe.submit_translation(pid)
    for _ in range(200):
        if registry.one("SELECT count(*) AS n FROM jobs WHERE status='done'")["n"] == 2:
            break
        time.sleep(0.05)
    assert [r["status"] for r in registry.query("SELECT status FROM jobs ORDER BY id")] == ["done", "done"]
    _, card = pipe._paper_files(pid)
    out = card.with_name(card.stem + ".papermap-close-reading.md").read_text()
    assert "测试技能输出" in out and "skill: papermap-close-reading" in out
    assert card.with_name(card.stem + ".translation.json").exists()
    with pytest.raises(ValueError):
        pipe.submit_skill(999, "x")


def test_web_translate_ack_and_skills_page(config, registry, pdfs):
    from fastapi.testclient import TestClient

    from papermap.web.app import create_app

    from .test_web import _token
    pipe, comp = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    tok = _token(c)
    panel = c.get("/paper/1/panel").text
    assert "翻译全文" in panel and "用技能解读" in panel and "精读" in panel
    r = c.post("/paper/1/translate", json={"csrf": tok}).json()
    assert r == {"ok": False, "need_ack": True} and not config.raw["translation"]["privacy_ack"]
    assert c.get("/skills").status_code == 200 and "papermap-close-reading" in c.get("/skills").text
    j = c.post("/api/translation/services", json={"csrf": tok, "name": "X", "base_url": "https://x/v1", "model": "m"}).json()
    assert j["ok"] and any(s["id"] == j["id"] for s in config.raw["translation"]["services"])
    assert c.post(f"/api/translation/services/{j['id']}/delete", json={"csrf": tok}).json()["ok"]
    assert not c.post("/api/translation/services/bilibili/delete", json={"csrf": tok}).json()["ok"]
    assert "bilibili Index-Translate" in c.get("/settings").text


def test_author_lists_and_metadata_are_not_translated():
    assert tr.is_meta("Xing Cai1,5, Yuhang Xiang2,5, Guang Yang3,5, Hongbin Wang3,5 & Meisi Yan2")
    assert tr.is_meta("Grant de Jong1,22, Fani Memi1,22, Tannia Gracia1,22, Olga Lazareva1,2,3,22,")
    assert tr.is_meta("Richard Mair4,13 ✉, Sam Behjati1,5,7 ✉, Oliver Stegle1,2,3 ✉ & Omer Ali Bayraktar1,21 ✉")
    assert tr.is_meta("https://doi.org/10.1038/s41698-026-01718-1") and tr.is_meta("Received: 10 February 2025")
    assert not tr.is_meta("Public data were obtained from GEO: GSE139495, GSE148673, GSE161529, GSE176078 and GSE180286.")
    assert not tr.is_meta("Stromal cells are key components of the breast cancer tumor microenvironment, yet unclear.")
    paras = [{"kind": "meta", "src": "A B1, C D2 & E F3"}, {"kind": "para", "src": "x" * 50}]
    assert tr.chunks(paras) == [[1]]
