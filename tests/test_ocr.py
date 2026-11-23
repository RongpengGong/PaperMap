"""The scan fixture contains bitmap pixels only. Nothing touches a user library."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pymupdf
import pytest
from fastapi.testclient import TestClient

from papermap import ocr, pdf_extract, reader, search, translate
from papermap.evidence import document_revision
from papermap.web.app import create_app

from .test_pipeline import make_pipeline
from .test_web import _token


def scan(path, *, header="", native_body="", rotated_crop=False):
    with pymupdf.open() as canvas:
        page = canvas.new_page(width=420, height=520)
        page.insert_text((36, 90), "Scanned evidence on the printed page.", fontsize=16)
        page.insert_text((36, 148), "扫描原文证据", fontname="china-s", fontsize=22)
        matrix = pymupdf.Matrix(2, 2).prerotate(-90 if rotated_crop else 0)
        bitmap = page.get_pixmap(matrix=matrix).tobytes("png")
    with pymupdf.open() as doc:
        if rotated_crop:
            page = doc.new_page(width=560, height=460)
            page.insert_image(pymupdf.Rect(20, 20, 540, 440), stream=bitmap)
            page.set_cropbox(pymupdf.Rect(20, 20, 540, 440))
            page.set_rotation(90)
        else:
            page = doc.new_page(width=420, height=520)
            page.insert_image(page.rect, stream=bitmap)
        if header:
            page.insert_text((32, 28), header)
        if native_body:
            page.insert_textbox(pymupdf.Rect(30, 190, 390, 300), native_body)
        doc.save(path)
    return path


def set_engine(monkeypatch, words):
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "_vision", lambda *_: words)


@pytest.mark.skipif(not ocr.available(), reason="requires Apple Vision")
@pytest.mark.parametrize("rotated_crop", [False, True])
def test_real_vision_bilingual_scan_and_pdf_coordinates(tmp_path, rotated_crop):
    path = scan(tmp_path / "scan.pdf", rotated_crop=rotated_crop)
    cache = ocr.cache_for(path, tmp_path)
    revision = document_revision(path)
    layer = reader.page_layer(path, 0, cache)
    assert layer["source"] == "ocr"
    assert ocr.lines(layer["words"]) == ["Scanned evidence on the printed page.", "扫描原文证据"]
    first, last = layer["words"][0], layer["words"][-1]
    assert first[0] == pytest.approx(36, abs=4) and first[1] < 90 < first[3]
    assert last[0] == pytest.approx(36 + 22 * 5, abs=5) and last[1] < 148 < last[3]
    hits = reader.search(path, "原文", cache=cache)
    assert hits[0]["rect"][0] == pytest.approx(80, abs=6)
    assert "扫描原文证据" in pdf_extract.extract_full_text(path, cache=cache)
    assert "Scanned evidence" in pdf_extract.extract_for_llm(path, 1000, cache=cache)
    assert "Scanned evidence" in pdf_extract.read_info(path, cache=cache).first_pages_text
    assert any("Scanned evidence" in p["src"] for p in translate.paragraphs(path, cache))
    assert pdf_extract.sha256_file(path) == revision


def test_mixed_page_retry_index_and_cache(config, registry, tmp_path, monkeypatch):
    path = scan(tmp_path / "mixed.pdf", native_body="This embedded text layer already has a long caption with enough letters.")
    pid = registry.create_paper(title="test", status="new")
    st = path.stat()
    registry.upsert_file(document_revision(path), str(path), st.st_size, st.st_mtime, pid)
    pipe, _ = make_pipeline(config, registry)
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    base = f"/api/reader/{pid}/page/0"
    words = [[16, 81, 92, 95, "recognized", "ocr-0"], [94, 81, 143, 95, "fragment", "ocr-0"]]
    set_engine(monkeypatch, [])
    assert c.get(base + "/words?details=1").json()["source"] == "pdf"
    assert c.post(base + "/ocr", json={}).status_code == 403
    csrf = {"csrf": _token(c)}
    assert c.post(base + "/ocr", json=csrf).json()["message"]
    set_engine(monkeypatch, words)
    result = c.post(base + "/ocr", json=csrf).json()
    assert result == {"ok": True, "words": words, "source": "ocr"}
    monkeypatch.setattr(ocr, "_vision", lambda *_: pytest.fail("repeated OCR on a cached page"))
    assert c.get(base + "/words").json() == words
    assert c.post(base + "/ocr", json=csrf).json() == result
    assert c.get(f"/api/reader/{pid}/info").json()["revision"] == document_revision(path)
    assert c.get(f"/api/reader/{pid}/search?q=recognized%20fragment").json()[0]["rect"] == [16, 81, 143, 95]
    assert [hit["page"] for hit in search.search(registry, "fragment")] == [0]
    assert c.get(f"/api/reader/{pid}/page/2/words").status_code == 404


def test_concurrent_ocr_and_availability(tmp_path, monkeypatch):
    path = scan(tmp_path / "scan.pdf", header="Printed copy / page 1")
    cache = ocr.cache_for(path, tmp_path)
    monkeypatch.setattr(ocr, "available", lambda: False)
    assert reader.page_layer(path, 0, cache)["source"] == "pdf"
    with pytest.raises(ocr.OCRError, match="macOS"):
        reader.page_layer(path, 0, cache, force=True)
    calls = []
    lock = Lock()
    def recognize(*args):
        with lock:
            calls.append(1)
        return [[50, 80, 110, 92, "Evidence", "ocr-0"]]
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "_vision", recognize)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: reader.page_layer(path, 0, cache), range(8)))
    assert all(layer["source"] == "ocr" for layer in results)
    assert len(calls) == 1


def test_ocr_completing_during_full_index(config, registry, tmp_path, monkeypatch):
    path = scan(tmp_path / "mixed.pdf", native_body="Native text is already long enough to skip automatic scan detection.")
    pid = registry.create_paper(title="Index race")
    words = [[10, 12, 50, 22, "withheld", "ocr-0"]]
    set_engine(monkeypatch, words)
    cache = ocr.cache_for(path, config.data_dir)
    paused, resume = Event(), Event()
    docs = search._docs
    def pause_after_read(*args):
        rows = docs(*args)
        paused.set()
        assert resume.wait(5)
        return rows
    monkeypatch.setattr(search, "_docs", pause_after_read)
    with ThreadPoolExecutor() as pool:
        task = pool.submit(search.index_paper, config.knowledge_dir, registry, pid, path)
        try:
            assert paused.wait(5)
            recognized = reader.page_layer(path, 0, cache, force=True)
            search.index_ocr_page(registry, pid, 0, recognized["words"])
        finally:
            resume.set()
        assert task.result()
    assert len(search.search(registry, "withheld")) == 1
    assert len(registry.query("SELECT * FROM search_index WHERE paper_id=? AND kind='page'", (pid,))) == 1


def test_imported_scan_searchable_while_waiting_for_model(config, registry, monkeypatch):
    inbox = config.inbox_dirs[0]
    inbox.mkdir(parents=True, exist_ok=True)
    path = scan(inbox / "printed.pdf")
    words = [[10, 12, 310, 26, "A scanned chapter on polar expeditions", "ocr-0"]]
    set_engine(monkeypatch, words)
    pipe, _ = make_pipeline(config, registry, with_key=False)
    assert pipe.handle_path(path) == "waiting_llm"
    assert search.search(registry, "polar expeditions")[0]["page"] == 0
