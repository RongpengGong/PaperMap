import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pymupdf
import pytest

from papermap import evidence, mobile_sync, ocr, reading_progress
from papermap.registry import Registry
from .test_reader import client


@pytest.fixture
def shared(config, registry, pdfs):
    web, _, _ = client(config, registry, pdfs)
    paper = mobile_sync.capture(config, registry, 1)
    page = mobile_sync.read_page(config, registry, 1, paper["revision"], 1)
    segment = page["segments"][0]
    return web, paper, segment


def note(segment, text="", page=1):
    return {"id": str(uuid.uuid4()), "page": page, "segmentId": segment["id"], "note": text}


def test_sync_with_existing_readers_and_original_page(config, registry, shared):
    web, paper, segment = shared
    rev = paper["revision"]
    assert paper["title"] and paper["pageCount"] == 2
    assert paper["progress"] == {"page": 0, "offset": 0, "version": 0}
    path = mobile_sync.pdf_path(config, registry, 1, rev)
    assert paper["byteSize"] == path.stat().st_size
    assert evidence.document_revision(path) == rev
    assert paper["notes"] == []
    first = note(segment, "compare the cohorts")
    second = note(segment, "recheck the inclusion criteria")
    changes = {"notes": [first, second], "progress": {"baseVersion": 0, "page": 1, "offset": 0.45}}
    response = mobile_sync.apply_changes(config, registry, 1, rev, changes)
    assert not response["progressConflict"] and set(response["acknowledgedNoteIds"]) == {first["id"], second["id"]}
    assert response["progress"]["page"] == 1 and response["progress"]["offset"] == .45
    assert response["progress"]["version"] > 0
    assert len(response["notes"]) == 2 and response["notes"][0]["quote"] == segment["text"]
    assert mobile_sync.apply_changes(config, registry, 1, rev, changes) == response

    desktop = registry.query("SELECT * FROM evidence_entries WHERE paper_id=1 ORDER BY id")
    assert len(desktop) == 2 and desktop[0]["revision"] == rev
    assert desktop[0]["page"] == 1 and len(json.loads(desktop[0]["rects_json"])) > 0
    assert web.get(f"/api/reader/1/evidence/{desktop[0]['id']}").json()["quote"] == segment["text"]
    assert str(desktop[0]["note"]) in web.get(f"/evidence?question={desktop[0]['question_id']}").text
    assert web.get("/api/reading/recent").json()["recent"][0]["page"] == 1

    registry.execute("UPDATE evidence_entries SET note='edited on desktop' WHERE id=?", (desktop[0]["id"],))
    registry.execute("DELETE FROM evidence_entries WHERE id=?", (desktop[1]["id"],))
    result = mobile_sync.apply_changes(config, registry, 1, rev, changes)
    assert [entry["note"] for entry in result["notes"]] == ["edited on desktop"]
    assert set(result["acknowledgedNoteIds"]) == {first["id"], second["id"]}
    assert mobile_sync.capture(config, registry, 1)["notes"] == result["notes"]


def test_conflicting_bookmarks_and_invalid_batches(config, registry, shared):
    _, paper, segment = shared
    rev = paper["revision"]
    reading_progress.save(registry, 1, {"page": 0, "offset": .9, "at": time.time_ns() // 1000}, 2)
    first = note(segment, "phone note")
    pending = {"baseVersion": 0, "page": 1, "offset": 0}
    result = mobile_sync.apply_changes(config, registry, 1, rev, {"progress": pending, "notes": [first]})
    assert result["progressConflict"] and result["progress"]["offset"] == .9
    assert result["acknowledgedNoteIds"] == [first["id"]]
    accepted = mobile_sync.apply_changes(config, registry, 1, rev, {
        "progress": dict(pending, baseVersion=result["progress"]["version"])})
    assert not accepted["progressConflict"] and accepted["progress"]["page"] == 1
    for invalid in (dict(first, note="changed"), dict(first, id=str(uuid.uuid4()), segmentId="f" * 64),
                    dict(first, id=""), dict(first, id=str(uuid.uuid4()), page=True)):
        with pytest.raises(ValueError):
            mobile_sync.apply_changes(config, registry, 1, rev, {"notes": [note(segment), invalid]})
    with pytest.raises(ValueError):
        mobile_sync.apply_changes(config, registry, 1, rev, {"progress": dict(pending, offset=float("nan"))})
    assert len(mobile_sync.capture(config, registry, 1)["notes"]) == 1


def test_shared_revision_change_and_trash(config, registry, shared):
    _, paper, segment = shared
    rev = paper["revision"]
    path = mobile_sync.pdf_path(config, registry, 1, rev)
    with path.open("ab") as f:
        f.write(b"\n% changed\n")
    for fn, args in [(mobile_sync.pdf_path, ()), (mobile_sync.read_page, (1,)),
                     (mobile_sync.apply_changes, ({"notes": [note(segment)]},))]:
        with pytest.raises(mobile_sync.RevisionConflict):
            fn(config, registry, 1, rev, *args)
    registry.update_paper(1, status="ignored")
    with pytest.raises(LookupError):
        mobile_sync.capture(config, registry, 1)


def test_scanned_page_uses_existing_ocr_only(config, registry, tmp_path, monkeypatch):
    path = tmp_path/"scan.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(path)
    pid = registry.create_paper(title="Scan")
    revision = evidence.document_revision(path)
    registry.upsert_file(revision, str(path), path.stat().st_size, path.stat().st_mtime, pid)
    monkeypatch.setattr(ocr, "_vision", lambda *_: pytest.fail("sync must not trigger an OCR job"))
    assert mobile_sync.read_page(config, registry, pid, revision, 0)["segments"] == []
    cache = ocr.cache_for(path, config.data_dir)
    cache.mkdir(parents=True, exist_ok=True)
    (cache/"0.json").write_text(json.dumps([[12,10,30,20,"原","ocr-0"],[30,10,48,20,"句","ocr-0"]]))
    seg = mobile_sync.read_page(config, registry, pid, revision, 0)["segments"][0]
    assert seg["text"] == "原句"
    mobile_sync.apply_changes(config, registry, pid, revision, {"notes": [note(seg, page=0)]})
    saved = registry.one("SELECT rects_json,source FROM evidence_entries")
    assert saved["source"] == "ocr" and json.loads(saved["rects_json"]) == [[12.,10.,48.,20.]]


def test_library_identity_and_two_connections(config, registry, shared, tmp_path):
    _, paper, seg = shared
    other = Registry(registry.db_path)
    independent = Registry(tmp_path/"separate.sqlite")
    try:
        assert mobile_sync.library_id(registry) == mobile_sync.library_id(other)
        assert mobile_sync.library_id(other) != mobile_sync.library_id(independent)
        op = note(seg)
        with ThreadPoolExecutor() as pool:
            responses = list(pool.map(lambda db: mobile_sync.apply_changes(
                config, db, 1, paper["revision"], {"notes": [op]}), (registry, other)))
        assert responses[0]["notes"] == responses[1]["notes"] and len(responses[0]["notes"]) == 1
    finally:
        independent.close()
        other.close()


def test_backup_retains_note_receipts_and_migrates_pre_sync_library(config, registry, shared):
    from papermap import backup

    _, paper, segment = shared
    rev = paper["revision"]
    identity = mobile_sync.library_id(registry)
    op = note(segment)
    mobile_sync.apply_changes(config, registry, 1, rev, {"notes": [op]})
    snapshot = backup.create(config, registry, "mobile")
    registry.execute("DELETE FROM mobile_note_receipts")
    registry.execute("DELETE FROM mobile_library")
    backup.restore(config, registry, snapshot.name)
    assert mobile_sync.library_id(registry) == identity
    assert len(mobile_sync.apply_changes(config, registry, 1, rev, {"notes": [op]})["notes"]) == 1

    registry.execute("DROP TABLE mobile_library")
    registry.execute("DROP TABLE mobile_note_receipts")
    older = backup.create(config, registry, "legacy")
    backup.restore(config, registry, older.name)
    assert mobile_sync.library_id(registry) != identity
    notes = mobile_sync.capture(config, registry, 1)["notes"]
    assert len(notes) == 1 and notes[0]["quote"] == segment["text"]


def test_highlights_sync_both_ways_and_deletions_do_not_come_back(config, registry, shared):
    web, paper, _ = shared
    rev = paper["revision"]
    desk = registry.add_annotation(1, 0, json.dumps([[10, 20, 60, 30]]), "desktop words", "green", "")
    snap = mobile_sync.capture(config, registry, 1, marks=True)
    [from_desk] = snap["marks"]
    assert from_desk["color"] == "green" and from_desk["style"] == "highlight" and from_desk["page"] == 0
    uid = from_desk["id"]
    assert registry.one("SELECT uid FROM annotations WHERE id=?", (desk,))["uid"] == uid  # stable from now on
    assert "marks" not in mobile_sync.capture(config, registry, 1)  # the quick catalog listing leaves them out

    phone = {"id": str(uuid.uuid4()), "page": 1, "style": "underline", "color": "pink",
             "rects": [[5, 40, 90, 52]], "text": "phone words"}
    recolored = dict(from_desk, color="blue")  # both sides know it: the computer's colour stays
    result = mobile_sync.apply_changes(config, registry, 1, rev, {"marks": {"upserts": [phone, recolored]}})
    assert set(result["acknowledgedMarkIds"]) == {phone["id"], uid}
    stored = {m["id"]: m for m in result["marks"]}
    assert stored[phone["id"]]["style"] == "underline" and stored[phone["id"]]["rects"] == [[5, 40, 90, 52]]
    assert stored[uid]["color"] == "green"
    reader = web.get("/api/reader/1/info").json()["annotations"]
    assert {a["uid"]: a["style"] for a in reader}[phone["id"]] == "underline"  # the desktop reader shows it

    registry.delete_annotation(desk)  # deleted on the computer
    again = mobile_sync.apply_changes(config, registry, 1, rev, {"marks": {"upserts": [from_desk]}})
    assert uid in again["markTombstones"] and uid not in {m["id"] for m in again["marks"]}
    assert not registry.query("SELECT 1 FROM annotations WHERE uid=?", (uid,))  # not re-added by the phone

    gone = mobile_sync.apply_changes(config, registry, 1, rev, {"marks": {"deletes": [phone["id"]]}})
    assert gone["marks"] == [] and phone["id"] in gone["markTombstones"]
    assert mobile_sync.apply_changes(config, registry, 1, rev, {"marks": {"upserts": [phone]}})["marks"] == []

    for bad in ({"marks": {"upserts": [dict(phone, id="nope")]}}, {"marks": {"upserts": [dict(phone, page=9)]}},
                {"marks": {"upserts": [dict(phone, color="red")]}}, {"marks": {"upserts": [dict(phone, rects=[])]}},
                {"marks": []}, {"marks": {"deletes": ["x"]}}):
        with pytest.raises(ValueError):
            mobile_sync.apply_changes(config, registry, 1, rev, bad)
