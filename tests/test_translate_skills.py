import json
import re
import time
from pathlib import Path

import httpx
import pymupdf
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


@pytest.mark.parametrize("labels", [("References", "Dataset"), ("Dataset", "References")])
def test_table_reference_column_does_not_hide_later_pages(tmp_path, labels):
    path = tmp_path / "table.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        for x, label in zip((50, 210), labels):
            page.insert_text((x, 70), label)
        page.insert_text((50, 100), "Smith, J. and Olsen, K. (2020). A study in the table.")
        page = doc.new_page()
        page.insert_text((50, 70), "The model improved on Jones et al., 2020 and retained every sample.")
        page.insert_text((50, 95), "A second paragraph follows the table on the next page.")
        page = doc.new_page()
        page.insert_text((50, 70), "References")
        page.insert_text((50, 100), "Brown, A. and Li, I. (2010). Exclude this bibliography entry.")
        doc.save(path)
    paras = tr.paragraphs(path)
    assert any(p["page"] == 1 and "et al., 2020 and retained every sample" in p["src"] for p in paras)
    assert any(p["page"] == 1 and "A second paragraph" in p["src"] for p in paras)
    assert all(p["page"] < 2 for p in paras)


@pytest.mark.parametrize("lead", ["Extended Data Fig. 1 | Further measurements",
                                  "Supplementary Information for the printed study"])
def test_unnumbered_bibliography_ends_before_appendix(tmp_path, lead):
    path = tmp_path / "appendix.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 70), "References")
        page.insert_text((50, 100), "Anonymous handbook of selected papers. Archive.")
        page.insert_text((50, 130), lead)
        page.insert_text((50, 160), "The measurements in this appendix should be retained.")
        doc.save(path)
    text = " ".join(p["src"] for p in tr.paragraphs(path))
    assert "Anonymous handbook" not in text
    assert lead in text and "measurements in this appendix" in text


def test_ocr_reference_section_inside_page_preserves_both_sides(tmp_path, monkeypatch):
    path = make_pdf(tmp_path / "recognized.pdf", ["placeholder", "placeholder"])
    recognized = [
        ["The text before the references is part of the article.", "References"],
        ["[1] Smith, A. This citation wraps over several lines.", "Methods in the journal title.",
         "Other bibliography text that should also be hidden.", "Online Methods",
         "The text after the references is also part of the article."],
    ]
    def words(page, *_args, **_kwargs):
        return [[0, i * 12., 300, i * 12. + 10., t, i] for i, t in enumerate(recognized[page.number])]
    monkeypatch.setattr(tr.ocr, "page_words", words)
    paras = tr.paragraphs(path, cache=tmp_path)
    assert [(p["page"], p["src"]) for p in paras] == [
        (0, recognized[0][0]), (1, " ".join(recognized[1][-2:])),
    ]


def test_margin_line_number_and_out_of_order_caption(tmp_path):
    path = tmp_path / "proof.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((60, 50), "The observations agree with Smith et al., 2023 in the article.")
        page.insert_text((62, 400), "REFERENCES AND NOTES", fontsize=15, fontname="hebo")
        page.insert_text((38, 400), "498", fontsize=6)
        page.insert_text((60, 430), "1 Smith, J. The first bibliography entry is excluded.")
        page.insert_text((60, 220), "Figure 10. This caption was drawn above the reference list.")
        page = doc.new_page()
        page.insert_text((60, 70), "2 The reference list continues on the following page.")
        doc.save(path)
    text = " ".join(p["src"] for p in tr.paragraphs(path))
    assert "et al., 2023" in text and "Figure 10. This caption" in text
    assert "first bibliography entry" not in text and "following page" not in text


def test_mixed_ocr_bibliography_preserves_star_methods_and_only_caption(tmp_path, monkeypatch):
    path = make_pdf(tmp_path / "ocr.pdf", ["placeholder"])
    text = ["The prose with a citation (Lee et al., 2022) stays intact.", "References", "1.",
            "Brown, A. A citation in a numbered bibliography.", "Figure 8. An actual caption.",
            "Its second line also belongs to the figure.", "[2] Another hidden reference.",
            "STAR★Methods", "The observations were compared across both groups."]
    words = [[0, i*15, 280, i*15+10, ln, i] for i, ln in enumerate(text)]
    monkeypatch.setattr(tr.ocr, "page_words", lambda *_args, **_kwargs: words)
    result = " ".join(p["src"] for p in tr.paragraphs(path, cache=tmp_path))
    assert all(t in result for t in (text[0], *text[4:6], *text[-2:]))
    assert "numbered bibliography" not in result and "hidden reference" not in result


def test_supplemental_captions_in_one_bibliography_block(tmp_path, monkeypatch):
    path = make_pdf(tmp_path / "supplement.pdf", ["placeholder"])
    text = ["References", "[1] First bibliography entry that should be omitted.",
            "SUPPLEMENTARY FIGURE 1", "The full caption continues in a separate line.",
            "Fig. S10. A second caption has its own paragraph.", "Table S2. Additional measurements.",
            "[2] The bibliography resumes here and should be omitted."]
    words = [[0, i*15, 300, i*15+10, ln, i] for i, ln in enumerate(text)]
    monkeypatch.setattr(tr.ocr, "page_words", lambda *_args, **_kwargs: words)
    assert [p["src"] for p in tr.paragraphs(path, cache=tmp_path)] == [" ".join(text[2:4]), *text[4:6]]


def test_label_only_small_captions_and_later_small_type_methods(tmp_path):
    path = tmp_path / "typesetting.pdf"
    with pymupdf.open() as doc:
        for i in range(4):
            page = doc.new_page()
            main = ("The main article is set in larger type. " * 10) + chr(65+i)
            page.insert_textbox(pymupdf.Rect(60, 40, 480, 200), main, fontsize=12)
            page.insert_text((60, 220), f"FIGURE {i+1}", fontsize=7)
            page.insert_text((60, 233), f"Small caption describing individual observations in sample {chr(65+i)}.", fontsize=7)
        page = doc.new_page()
        page.insert_text((60, 60), "Methods", fontsize=8)
        page.insert_text((60, 85), "Measurements were grouped by site and sampling interval.", fontsize=8)
        page = doc.new_page()
        page.insert_text((60, 60), "Samples from the remaining sites were assessed in smaller type.", fontsize=8)
        page.insert_text((60, 240), "(See figure on previous page.)", fontsize=7)
        page.insert_text((60, 248), "Fig. 6 Stability across the sites is shown in this caption.", fontsize=7)
        doc.save(path)
    result = tr.paragraphs(path)
    assert all(any(p["page"] == i and f"FIGURE {i+1}" in p["src"] and "Small caption" in p["src"]
                   for p in result) for i in range(4))
    assert any(p["page"] == 5 and "remaining sites" in p["src"] for p in result)
    assert any("Fig. 6 Stability" in p["src"] for p in result)


def test_same_type_caption_continues_across_native_blocks(tmp_path):
    path = tmp_path / "wrapped-caption.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        main = "The article provides detailed observations in this larger typeface. " * 7
        page.insert_textbox(pymupdf.Rect(60, 40, 480, 200), main, fontsize=12)
        page.insert_text((60, 250), "Fig. 8. The two groups showed variation in", fontsize=8)
        page.insert_text((60, 264), "measurements collected over several months.", fontsize=8)
        doc.save(path)
    result = " ".join(p["src"] for p in tr.paragraphs(path))
    assert "variation in measurements collected over several months." in result


def test_reporting_form_and_full_translation_past_page_eighty(tmp_path):
    path = tmp_path / "long.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((60, 60), "Reporting summary")
        page.insert_text((60, 90), "Further information on the study is available in the reporting summary.")
        page = doc.new_page()
        page.insert_text((60, 60), "nature portfolio | reporting summary")
        page.insert_text((60, 90), "This publisher questionnaire is not part of the article.")
        for _ in range(78):
            doc.new_page()
        doc.new_page().insert_text((60, 60), "This last page of the article is part of the full-text translation.")
        doc.save(path)
    result = tr.paragraphs(path)
    assert any(p["page"] == 0 and "Further information" in p["src"] for p in result)
    assert not any(p["page"] == 1 for p in result)
    assert any(p["page"] == 80 and "last page" in p["src"] for p in result)


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
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["paragraphs"] and all(p["tr"] for p in data["paragraphs"])
    assert data["model"] == "Index-Translate-35B-A3B" and seen[0]["chat_template_kwargs"] == {"enable_thinking": False}
    md = card.with_name(card.stem + ".translation.md").read_text(encoding="utf-8")
    assert "译文对照" in md and re.search(r"^> (译文\d+|单段译文)", md, re.M)
    assert registry.one("SELECT count(*) AS n FROM llm_calls WHERE purpose='translate'")["n"] == len(seen)


def test_misaligned_answer_falls_back_to_single_paragraphs(config, registry, pdfs):
    pipe, comp = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    pdf, card = pipe._paper_files(registry.paper_by_doi(OWN_DOI)["id"])
    handler = lambda req: httpx.Response(200, text="data: " + json.dumps({"choices": [{"delta": {"content": "全部混在一起的译文"}}]}) + "\n\ndata: [DONE]\n\n")
    out = tr.translate_paper(config, registry, pipe.llm, 1, pdf, card, transport=_bilibili(handler))
    assert all(p["tr"] == "全部混在一起的译文" for p in json.loads(out.read_text(encoding="utf-8"))["paragraphs"] if p["kind"] != "meta")


def test_service_failure_falls_back_to_main_model(config, registry, pdfs, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    config.raw["llm"]["api_key"] = "sk-test"
    pipe, comp = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    pdf, card = pipe._paper_files(1)
    out = tr.translate_paper(config, registry, pipe.llm, 1, pdf, card,
                             transport=_bilibili(lambda req: httpx.Response(503)))
    data = json.loads(out.read_text(encoding="utf-8"))
    assert all(p["tr"].startswith("主模型译文") for p in data["paragraphs"] if p["kind"] != "meta")
    assert data["notes"] and "(fallback)" in data["model"]
    config.raw["translation"]["fallback_to_main"] = False
    with pytest.raises(Exception):
        tr.translate_paper(config, registry, pipe.llm, 1, pdf, card, transport=_bilibili(lambda req: httpx.Response(503)))


def test_skill_discovery_and_safety(config, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    local = config.data_dir / "skills"
    (local / "reader").mkdir(parents=True)
    (local / "reader" / "SKILL.md").write_text("---\nname: reader\ndescription: reads\n---\nRead it. See ../shared/core/x.md\n", encoding="utf-8")
    (local / "shared" / "core").mkdir(parents=True)
    (local / "shared" / "core" / "x.md").write_text("SHARED REFERENCE", encoding="utf-8")
    (local / "scripted" / "scripts").mkdir(parents=True)
    (local / "scripted" / "SKILL.md").write_text("---\nname: scripted\n---\nRun python scripts/x.py", encoding="utf-8")
    (local / "scripted" / "scripts" / "x.py").write_text("print(1)", encoding="utf-8")
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
    installed = sr.install(config, local / "reader")
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
    out = card.with_name(card.stem + ".papermap-close-reading.md").read_text(encoding="utf-8")
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
