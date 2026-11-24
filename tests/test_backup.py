"""Backups of database + settings, restore, and the post-restore library check."""
import zipfile

from papermap import backup

from .test_evidence import capture  # noqa: F401 - fixture


def test_backup_restore_brings_back_a_quote_that_still_opens(capture, config, registry):  # noqa: F811
    web, pid, pdf, data = capture
    saved = web.post(f"/api/reader/{pid}/evidence", json=data).json()
    eid = saved["id"]
    config.raw["output_language"] = "zh"
    config.save()

    r = web.post("/api/backup/create", json={"csrf": data["csrf"]}).json()
    assert r["ok"]
    name = r["name"]
    with zipfile.ZipFile(backup.backup_dir(config) / name) as z:
        assert {"papermap.db", "config.yaml", "manifest.json"} <= set(z.namelist())

    # lose the quote and change a setting after the backup
    assert web.post(f"/api/evidence/entries/{eid}/delete", json={"csrf": data["csrf"]}).json()["ok"]
    config.raw["output_language"] = "en"
    moved_kb = config.raw.get("knowledge_dir")
    assert "备份与恢复" in web.get("/settings?section=backup").text

    res = web.post("/api/backup/restore", json={"csrf": data["csrf"], "name": name}).json()
    assert res["ok"], res
    assert res["undo"].endswith("-before-restore.zip")
    assert config.raw["output_language"] == "zh" and config.raw.get("knowledge_dir") == moved_kb
    # the old quote is back and opens at its original place in the same PDF revision
    entry = web.get(f"/api/reader/{pid}/evidence/{eid}").json()
    assert (entry["page"], entry["quote"], entry["rects"]) == (data["page"], data["quote"], data["rects"])
    rep = res["report"]
    assert rep["evidence"] == 1 and rep["evidence_ok"] == 1 and rep["pdfs"] >= 1

    # a changed PDF is reported, not silently treated as the same source
    pdf.write_bytes(pdf.read_bytes() + b"\n%changed")
    rep = web.get("/api/backup/check").json()["report"]
    assert rep["evidence_changed"] == 1 and rep["evidence_ok"] == 0
    assert not web.post("/api/backup/restore", json={"csrf": data["csrf"], "name": "../x.zip"}).json()["ok"]


def test_schedule_prune_and_catch_up(config, registry):
    config.raw["backup"] = {"enabled": True, "keep": 2, "every_hours": 24}
    assert backup.due(config)  # never backed up -> back up at once (e.g. after days switched off)
    d = backup.backup_dir(config)
    d.mkdir(parents=True)
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
        backup.create(config, registry)
        latest = sorted(d.glob("papermap-backup-2*.zip"))[-1]
        latest.rename(d / f"papermap-backup-{stamp}.zip")
    assert backup.due(config)  # newest is months old
    backup.create(config, registry)
    assert not backup.due(config)
    autos = [b for b in backup.list_backups(config) if b["kind"] == "auto"]
    assert len(autos) == 2 and autos[-1]["name"] == "papermap-backup-20260103-000000.zip"
    config.raw["backup"]["enabled"] = False
    assert not backup.due(config)


def test_backup_while_writing_is_consistent(config, registry):
    import threading

    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            registry.log("info", f"event {i}")
            i += 1
    t = threading.Thread(target=writer)
    t.start()
    try:
        path = backup.create(config, registry)
    finally:
        stop.set()
        t.join()
    assert backup.list_backups(config)[0]["name"] == path.name and not backup.list_backups(config)[0]["meta"].get("broken")


def test_restore_migrates_checkpoints_and_preserves_current_mcp_access(config, registry, tmp_path):
    import hashlib
    import sqlite3

    from papermap.web import mcp_api

    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    token_at_backup = mcp_api.rotate_token(registry)
    archive = backup.create(config, registry, kind="manual")
    mcp_api.revoke_token(registry)
    backup.restore(config, registry, archive.name)
    assert not mcp_api.token_enabled(registry)

    # Simulate a database from before the MCP and durable translation tables existed.
    old_db = tmp_path / "old.db"
    with zipfile.ZipFile(archive) as z:
        old_db.write_bytes(z.read("papermap.db"))
    with sqlite3.connect(old_db) as db:
        for table in ("mcp_read_credentials", "translation_parts", "translation_inputs"):
            db.execute(f"DROP TABLE {table}")
    with zipfile.ZipFile(archive, "w") as z:
        z.write(old_db, "papermap.db")
    current_token = mcp_api.rotate_token(registry)
    backup.restore(config, registry, archive.name)
    restored = registry.one("SELECT digest FROM mcp_read_credentials")["digest"]
    assert restored == digest(current_token) and restored != digest(token_at_backup)
    assert registry.query("SELECT * FROM translation_parts") == []
    assert registry.query("SELECT * FROM translation_inputs") == []
