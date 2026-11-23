from papermap import doi


def test_normalize_variants():
    assert doi.normalize("https://doi.org/10.1038/S41586-026-11234-8.") == "10.1038/s41586-026-11234-8"
    assert doi.normalize("doi: 10.1000/ABC);") == "10.1000/abc"
    assert doi.normalize("http://dx.doi.org/10.1000/xyz") == "10.1000/xyz"
    # balanced parentheses are part of the DOI
    assert doi.normalize("10.1016/S0140-6736(20)30183-5") == "10.1016/s0140-6736(20)30183-5"
    assert doi.normalize("(see 10.1016/S0140-6736(20)30183-5)") == "10.1016/s0140-6736(20)30183-5"
    assert doi.normalize("not a doi") is None
    assert doi.normalize("") is None


def test_from_filename_nature_and_springer():
    assert doi.from_filename("s41586-026-11234-8.pdf") == ["10.1038/s41586-026-11234-8"]
    assert doi.from_filename("s41698-026-01718-1_reference.pdf") == ["10.1038/s41698-026-01718-1"]
    assert "10.1186/s13059-020-1234-5" in doi.from_filename("s13059-020-1234-5.pdf")
    assert doi.from_filename("10.1101_2024.01.01.123456.pdf") == ["10.1101/2024.01.01.123456"]
    assert doi.from_filename("my paper.pdf") == []


def test_from_text_marks_own_doi_and_joins_line_breaks():
    text = "Nature | Article\nhttps://doi.org/10.1038/s41586-\n026-11234-8\nReceived: 1 Jan"
    assert doi.from_text(text) == [("10.1038/s41586-026-11234-8", True)]


def test_from_pdf_metadata_xmp():
    xmp = '<rdf:Description><prism:doi>10.1038/s41588-026-02779-5</prism:doi></rdf:Description>'
    assert doi.from_pdf_metadata({"subject": "Nature, doi:10.1038/s41588-026-02779-5"}, xmp) == [
        "10.1038/s41588-026-02779-5"
    ]


def test_ranking_prefers_metadata_then_marked_text():
    ranked = doi.rank_candidates(
        ["10.1/own"],
        [("10.2/ref-a", False), ("10.1/own", True), ("10.3/ref-b", False)],
        ["10.9/fromname"],
    )
    assert ranked[0].doi == "10.1/own"
    assert ranked[-1].doi == "10.9/fromname"


def test_reference_dois_rank_below_marked(pdfs):
    from papermap.pdf_extract import read_info
    info = read_info(pdfs["paper"])
    ranked = doi.rank_candidates(doi.from_pdf_metadata(info.info, info.xmp),
                                 doi.from_pages(info.first_pages), doi.from_filename(info.path))
    assert ranked[0].doi == "10.1234/demo.2024.001"


def test_reference_line_with_doi_prefix_is_penalized():
    hits = doi.from_pages([
        "A study\nReceived 2024",
        "1. Smith, J. et al. Some title. Cell (2019). doi:10.1016/j.cell.2019.01.002",
    ])
    assert hits[0].ref_like and hits[0].page == 1
    ranked = doi.rank_candidates([], hits, ["10.1038/s41586-026-11234-8"])
    assert ranked[0].doi == "10.1038/s41586-026-11234-8"
