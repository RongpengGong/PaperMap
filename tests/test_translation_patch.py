"""补译 and the library-wide omission check: old translations with gaps are listed under 待审核,
and repairing one only sends the missing paragraphs to the translation service."""
import json

from papermap import translate, translation_audit
from .test_translation_completion import _paper
from .test_translation_resume import eventually
from .test_web import _token, client


def _old_translation(pdf, card, drop_page: int):
    """A saved translation that, like the pre-fix extractor, lost the text of one PDF page."""
    rows = translate.paragraphs(pdf)
    old = [{**p, "tr": "" if p["kind"] != "meta" and p["page"] == drop_page else
            (p["src"] if p["kind"] == "meta" else "旧译：" + p["src"][:20])} for p in rows]
    data = {"paper_id": 1, "target": "zh", "model": "old-model",
            "paragraphs": [p for p in old if p["tr"]]}
    card.with_suffix(".translation.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return rows


def test_audit_lists_gaps_and_patch_translates_only_missing_paragraphs(config, registry, pdfs, monkeypatch):
    pipe, pdf, card = _paper(config, registry, pdfs)
    rows = _old_translation(pdf, card, drop_page=1)
    missing = {i for i, p in enumerate(rows) if p["kind"] != "meta" and p["page"] == 1}
    assert missing

    assert translation_audit.audit_library(pipe) == 1
    flagged = translation_audit.flagged(registry)
    assert flagged[0]["paper_id"] == 1 and 2 in flagged[0]["pages"]
    # an unchanged file is not checked again
    monkeypatch.setattr(translate, "saved_translation_gaps", lambda *a: (_ for _ in ()).throw(AssertionError("rechecked")))
    assert translation_audit.check_paper(pipe, 1) == flagged[0]["pages"]
    monkeypatch.undo()

    web = client(config, registry)
    page = web.get("/review").text
    assert "可能漏译的全文译文" in page and 'data-patch-translation="1"' in page

    sent = []

    def fake_chunk(self, paras, ids, target, glossary, paper, save):
        sent.extend(ids)
        save({i: (f"新译{i}", "mock") for i in ids})

    monkeypatch.setattr(translate.Translator, "translate_chunk", fake_chunk)
    r = web.post("/paper/1/translate", json={"csrf": _token(web), "service": "main", "reuse": True}).json()
    assert r["ok"]
    eventually(lambda: registry.one("SELECT status FROM jobs WHERE kind='translate' ORDER BY id DESC")[0]
               in ("done", "needs_review", "error"))
    job = registry.one("SELECT status, message FROM jobs WHERE kind='translate' ORDER BY id DESC")
    assert job["status"] in ("done", "needs_review"), job["message"]
    assert set(sent) == missing  # only the lost page went to the service
    new = json.loads(card.with_suffix(".translation.json").read_text(encoding="utf-8"))
    by_src = {p["src"]: p["tr"] for p in new["paragraphs"]}
    kept = next(p for p in rows if p["kind"] != "meta" and p["page"] == 0)
    assert by_src[kept["src"]].startswith("旧译：")
    assert any("补译" in n for n in new["notes"])
    # the new translation is checked again right after the job, so the review list empties
    assert translation_audit.flagged(registry) == []
    assert "可能漏译的全文译文" not in web.get("/review").text


def test_full_retranslation_still_translates_everything(config, registry, pdfs, monkeypatch):
    pipe, pdf, card = _paper(config, registry, pdfs)
    rows = _old_translation(pdf, card, drop_page=1)
    sent = []
    monkeypatch.setattr(translate.Translator, "translate_chunk",
                        lambda self, paras, ids, *a: (sent.extend(ids), a[-1]({i: ("x", "mock") for i in ids})))
    translate.translate_paper(config, registry, pipe.llm, 1, pdf, card, None, "main")
    assert set(sent) == {i for i, p in enumerate(rows) if p["kind"] != "meta"}


def test_patch_ignores_a_translation_into_another_language(config, registry, pdfs, monkeypatch):
    pipe, pdf, card = _paper(config, registry, pdfs)
    rows = _old_translation(pdf, card, drop_page=1)
    data = json.loads(card.with_suffix(".translation.json").read_text(encoding="utf-8"))
    data["target"] = "en"
    card.with_suffix(".translation.json").write_text(json.dumps(data), encoding="utf-8")
    results = {}
    options = {"revision": "r", "target": "zh"}
    assert translate._reuse_previous(card.with_suffix(".translation.json"), 1, options, rows, results) == 0
