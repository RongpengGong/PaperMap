"""Coverage and provider-response checks through the translation job and reader."""
import hashlib
import json

import pytest

from papermap import translate
from .test_pipeline import make_pipeline
from .test_translation_resume import eventually
from .test_web import _token, client


def _paper(config, registry, pdfs):
    pipe, _ = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    config.raw["llm"]["api_key"] = "test-only"
    pdf, card = pipe._paper_files(1)
    return pipe, pdf, card


def test_review_is_visible_and_retranslation_preserves_better_coverage(config, registry, pdfs, monkeypatch):
    pipe, pdf, card = _paper(config, registry, pdfs)
    monkeypatch.setattr(translate, "coverage_gaps", lambda *args, **kwargs: [2])
    jid = pipe.submit_translation(1, "main")
    eventually(lambda: registry.one("SELECT status FROM jobs WHERE id=?", (jid,))[0] == "needs_review")
    output = card.with_suffix(".translation.json")
    saved = output.read_bytes()
    assert json.loads(saved)["review_pages"] == [2]
    assert "第 2 页可能漏译" in card.with_suffix(".translation.md").read_text()
    assert "第 2 页可能漏译" in client(config, registry).get("/paper/1/panel").text

    # Rebuilding would omit a page that the previous translation covered.
    monkeypatch.setattr(translate, "coverage_gaps", lambda _, rows, **kw: [] if "tr" in rows[0] else [1])
    monkeypatch.setattr(translate.Translator, "translate_chunk", lambda *a: pytest.fail("unexpected provider request"))
    with pytest.raises(translate.TranslationError, match="保留原译文"):
        translate.translate_paper(config, registry, pipe.llm, 1, pdf, card)
    assert output.read_bytes() == saved


@pytest.mark.parametrize("response", ["missing", "empty"])
def test_job_cannot_publish_missing_or_empty_paragraphs(config, registry, pdfs, monkeypatch, response):
    pipe, pdf, card = _paper(config, registry, pdfs)

    def incomplete(self, paras, ids, target, glossary, paper, save):
        for i in ids:
            if response == "empty":
                save({i: ("  ", "mock")})
            elif i != ids[0]:
                save({i: ("translated", "mock")})

    monkeypatch.setattr(translate.Translator, "translate_chunk", incomplete)
    jid = pipe.submit_translation(1, "main")
    eventually(lambda: registry.one("SELECT status FROM jobs WHERE id=?", (jid,))[0] == "error")
    assert not card.with_suffix(".translation.json").exists()


def test_reader_checks_legacy_text_on_request_without_translation(config, registry, pdfs):
    _, pdf, card = _paper(config, registry, pdfs)
    rows = translate.paragraphs(pdf)
    old = {"paragraphs": [{"kind": p["kind"], "src": p["src"], "tr": "已有译文"} for p in rows]}
    out = card.with_suffix(".translation.json")
    browser = client(config, registry)
    token = _token(browser)

    def check():
        raw = json.dumps(old).encode()
        out.write_bytes(raw)
        response = browser.post("/paper/1/translation-coverage",
                                json={"csrf": token, "version": hashlib.sha256(raw).hexdigest()})
        assert response.status_code == 200 and out.read_bytes() == raw
        return response.json()

    assert check()["pages"] == []
    lost = next(i for i, p in enumerate(rows) if p["page"] == 1 and p["kind"] != "meta")
    old["paragraphs"][lost]["tr"] = ""
    assert 2 in check()["pages"]
    assert not registry.one("SELECT id FROM jobs WHERE kind='translate'")
    old["revision"] = "different"
    assert not check()["ok"]


def test_duplicate_numbered_responses_require_realigning():
    assert translate.parse_numbered("[1] first\n[1] again\n[2] second", [1, 2]) is None
