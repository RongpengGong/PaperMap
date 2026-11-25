import json

import pymupdf

from papermap.translation_coverage import coverage_gaps


def _pdf(path, pages, toc=()):
    with pymupdf.open() as doc:
        for blocks in pages:
            page = doc.new_page()
            y = 60
            for lines in blocks:
                page.insert_text((55, y), "\n".join(lines), fontsize=10)
                y += len(lines) * 15 + 18
        if toc:
            doc.set_toc(toc)
        doc.save(path)
    return path


def _prose(subject):
    return [
        f"We followed the {subject} in the first set of observations for several months.",
        f"The measurements for each {subject} were consistent with our expectations.",
        f"These changes were similar between sites and the {subject} was analysed independently.",
        f"The findings for this {subject} were reported for the first time in our study.",
    ]


def test_bookmarked_section_and_unnumbered_sources(tmp_path):
    heading = "3.2 Analysis of the second cohort"
    other = "4 Independent validation"
    first, last = _prose("control"), _prose("replication")
    pdf = _pdf(tmp_path / "article.pdf", [[[heading], first], [[other], last]],
               [[1, heading, 1], [1, other, 2]])
    # Old translations lack page anchors. Only the second title was dropped.
    legacy = [{"src": heading}, {"src": " ".join(first)}, {"src": " ".join(last)}]
    assert coverage_gaps(pdf, legacy) == [2]
    legacy.append({"src": other})
    assert coverage_gaps(pdf, legacy) == []


def test_cited_line_missing_inside_retained_paragraph(tmp_path):
    lines = _prose("sample")
    lines.insert(2, "Several studies including Smith et al., 2022 confirmed the additional difference.")
    pdf = _pdf(tmp_path / "citations.pdf", [[["Introduction"], lines]])
    assert coverage_gaps(pdf, [{"src": " ".join(lines)}]) == []
    assert coverage_gaps(pdf, [{"src": " ".join(lines[:2] + lines[3:])}]) == [1]


def test_references_forms_and_post_bibliography_methods(tmp_path):
    refs = [["References", "1.", "Jones and White. A book published in 2021."]]
    methods = _prose("transcription") + _prose("seasonal variation")
    pdf = _pdf(tmp_path / "post.pdf", [
        [["1 Introduction"], _prose("control")], refs, [_prose("invented reference") * 2],
        [["Methods"], methods], [["Reporting summary"], ["Last updated by author: 10 March"], _prose("form") * 2],
    ], [[1, "1 Introduction", 1], [1, "References", 2], [1, "Methods", 4]])
    src = [{"page": 0, "src": "1 Introduction " + " ".join(_prose("control"))}]
    assert coverage_gaps(pdf, src) == [4]
    assert coverage_gaps(pdf, src + [{"page": 3, "src": "Methods " + " ".join(methods)}]) == []


def test_no_bookmarks_cross_page_prose_empty_page_cover_and_caption(tmp_path):
    a, b, c = _prose("training cohort"), _prose("validation group"), _prose("external cohort")
    caption = ["Figure 3. Assessment in the external cohort.", *_prose("figure")]
    pdf = _pdf(tmp_path / "plain.pdf", [
        [["Cover page"], _prose("download") * 2], [["Introduction"], a],
        [b], [], [["A   B   C", "50   100   150"]], [caption], [c * 2],
    ])
    merged = [{"page": 1, "endPage": 2, "src": " ".join(a + b)}]
    assert coverage_gaps(pdf, merged) == [6, 7]
    assert coverage_gaps(pdf, merged + [
        {"page": 5, "src": " ".join(caption)},
        {"page": 6, "src": " ".join(c * 2)},
    ]) == []


def test_table_reference_column_does_not_suppress_later_unbookmarked_prose(tmp_path):
    later = _prose("retained group") + _prose("missing group")
    pdf = _pdf(tmp_path / "table.pdf", [[["Introduction"], ["References"], ["Dataset"]],
                                        [later], [["Bibliography"]], [["1. Smith, D. (2019)."], later]])
    assert coverage_gaps(pdf, []) == [2]


def test_scanned_image_with_no_text_is_not_a_gap_and_cached_ocr_is_read_only(tmp_path):
    pdf = _pdf(tmp_path / "scan.pdf", [[], []])
    cache = tmp_path / "cached"
    assert coverage_gaps(pdf, [], cache=cache) == []
    assert not cache.exists()
    cache.mkdir()
    lines = _prose("participant") + _prose("independent group")
    cached = cache / "1.json"
    cached.write_text(json.dumps([[0, 0, 200, 20, line, f"line-{i}"] for i, line in enumerate(lines)]))
    assert coverage_gaps(pdf, [], cache=cache) == [2]
    assert coverage_gaps(pdf, [{"src": " ".join(lines)}], cache=cache) == []


def test_appended_peer_review_versus_cover_notice(tmp_path):
    review = _prose("review") * 2
    pdf = _pdf(tmp_path / "reviews.pdf", [[["Open Peer Review"]], [["Introduction"], _prose("study")],
                                          [["Reviewer Report 16 June 2022"]], [review]])
    assert coverage_gaps(pdf, [{"page": 1, "src": " ".join(_prose("study"))}]) == []


def test_ordinary_reporting_section_and_post_form_pages_are_checked(tmp_path):
    methods = _prose("second analysis") + _prose("comparison analysis")
    extra = _prose("appendix findings") + _prose("new results")
    pdf = _pdf(tmp_path / "form.pdf", [
        [["Introduction"], _prose("first cohort")],
        [["Reporting summary", "Further information is available in the reporting summary."], ["Methods"], methods],
        [["nature portfolio | reporting summary"], _prose("questionnaire") * 2],
        [["Appendix A"], extra],
    ])
    provided = [{"page": 0, "src": " ".join(_prose("first cohort"))}]
    assert coverage_gaps(pdf, provided) == [2, 4]
    assert coverage_gaps(pdf, provided + [{"page": 1, "src": " ".join(methods)},
                                          {"page": 3, "src": " ".join(extra)}]) == []


def test_caption_between_bibliography_entries(tmp_path):
    caption = ["Figure 10. Comparison across the independent cohorts.", *_prose("replicate")]
    refs = [["REFERENCES AND NOTES"], ["1. Smith D. A report from 2024."]]
    article = _prose("participants")
    pdf = _pdf(tmp_path / "notes.pdf", [
        [["Introduction"], article], refs,
        [caption, ["2. Jones A. Review of the 2022 report."], _prose("cited source") * 2],
        [_prose("cited source") * 2],
    ])
    provided = [{"page": 0, "src": " ".join(article)}]
    assert coverage_gaps(pdf, provided) == [3]
    assert coverage_gaps(pdf, provided + [{"page": 2, "src": " ".join(caption)}]) == []
