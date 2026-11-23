from papermap.pdf_extract import extract_for_llm, read_info

from .conftest import OWN_TITLE, REF_DOIS


def test_title_and_supplement(pdfs):
    info = read_info(pdfs["paper"])
    assert info.title_guess == OWN_TITLE
    assert not info.is_supplement
    assert read_info(pdfs["supp"]).is_supplement


def test_llm_text_drops_references(pdfs):
    text = extract_for_llm(pdfs["paper"], 60000)
    assert "RESULTS" in text and "METHODS" in text
    assert not any(d in text for d in REF_DOIS)
    assert len(extract_for_llm(pdfs["paper"], 2000)) <= 2000


def test_corrupt_pdf_does_not_raise(tmp_path):
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"%PDF-1.4 garbage")
    info = read_info(bad)
    assert info.error
