import hashlib
import ipaddress
import json
import time
import uuid

import pytest

from papermap import mobile_lan, mobile_work, translate
from .conftest import OWN_DOI
from .test_mobile_lan import client
from .test_pipeline import make_pipeline


@pytest.fixture
def desktop(config, registry, monkeypatch):
    pipe, completions = make_pipeline(config, registry)
    monkeypatch.setattr(mobile_lan, "_networks", lambda: {"127.0.0.1": ipaddress.ip_network("127.0.0.0/8")})
    service = mobile_lan.MobileLan(config, registry, pipe)
    try:
        yield service, completions
    finally:
        service.stop()


def upload(web, pdf, rev=None):
    rev = rev or hashlib.sha256(pdf).hexdigest()
    url = "/v1/uploads/" + rev
    assert web.post(url, json={"byteSize": len(pdf)}).json() == {"offset": 0}
    for offset in range(0, len(pdf), 900):
        chunk = pdf[offset:offset+900]
        dest = url + "/chunks/" + str(offset)
        for _ in range(2):
            response = web.post(dest, content=chunk, headers={"Content-Type": "application/octet-stream"})
            assert response.json() == {"offset": offset + len(chunk)}
        assert web.post(url, json={"byteSize": len(pdf)}).json()["offset"] == offset + len(chunk)
    return rev


def run(web, kind, rev, pid=None):
    task = {"id": str(uuid.uuid4()), "kind": kind, "revision": rev}
    if pid is not None:
        task["paperId"] = pid
    start = web.post("/v1/tasks", json=task)
    assert start.status_code == 200, start.text
    for _ in range(200):
        reply = web.get("/v1/tasks/" + task["id"]).json()
        if reply["state"] in ("failed", "done"):
            assert web.post("/v1/tasks", json=task).json() == reply
            return reply
        time.sleep(.025)
    pytest.fail("The desktop job did not complete")


def test_mobile_import_and_independent_generation(desktop, config, registry, pdfs, monkeypatch):
    service, comp = desktop
    pdf = pdfs["paper"].read_bytes()
    config.raw["translation"]["privacy_ack"] = True
    monkeypatch.setattr(translate.Translator, "_bilibili", lambda self, prompt, paper_id: "Translated text")
    pair = service.start("127.0.0.1", processing=True)
    with client(pair) as web:
        assert web.get("/v1/papers").json()["processing"]
        rev = upload(web, pdf)
        new = run(web, "import", rev)
        assert new["state"] == "done", new
        pid = new["paperId"]
        assert web.get("/v1/papers").json()["papers"][0]["id"] == pid
        assert comp.calls == 0 and not registry.get_paper(pid)["card_json"]
        assert run(web, "card", rev, pid)["state"] == "failed"
        assert "主模型" in run(web, "card", rev, pid)["message"]
        assert run(web, "translation", rev, pid)["state"] == "done"
        assert run(web, "translation", rev, pid)["state"] == "done"
        config.raw["llm"]["api_key"] = "test"
        config.raw["translation"]["auto_new_papers"] = True
        assert run(web, "card", rev, pid)["state"] == "done"
        assert run(web, "card", rev, pid)["state"] == "done"
        out = web.get(f"/v1/papers/{pid}/{rev}/companion").json()
        assert "Finding two" in out["card"] and out["translation"]["state"] == "ready"
        assert len(registry.query("SELECT id FROM jobs WHERE kind='translate'")) == 1
        assert len(registry.query("SELECT id FROM jobs WHERE kind='mobile_card'")) == 1
        assert not list(config.imported_dir.glob(".papermap-phone-*.part"))


def test_mobile_processing_scope_bounds_and_revocation(desktop, config, registry, pdfs):
    service, _ = desktop
    existing = pdfs["paper"]
    rev = hashlib.sha256(existing.read_bytes()).hexdigest()
    pid = registry.create_paper(title="Unshared")
    registry.upsert_file(rev, str(existing), existing.stat().st_size, existing.stat().st_mtime, pid)
    ro = service.start("127.0.0.1")
    with client(ro) as web:
        assert not web.get("/v1/papers").json()["processing"]
        assert web.post("/v1/tasks", json={}).status_code == 403
        assert web.post("/v1/uploads/" + rev, json={"byteSize": 100}).status_code == 403
    service.stop()
    pair = service.start("127.0.0.1", processing=True)
    with client(pair) as web:
        assert web.post("/v1/tasks", json={"id": str(uuid.uuid4()), "kind": "card", "paperId": pid,
                                           "revision": rev}).status_code == 403
        url = "/v1/uploads/" + rev
        assert web.post(url, json={"byteSize": 300 * 1024**2}).status_code == 413
        assert web.post(url + "/chunks/0", content=b"body", headers={"Content-Type": "text/plain"}).status_code == 415
        upload(web, existing.read_bytes())
        duplicate = run(web, "import", rev)
        assert duplicate["state"] == "failed" and duplicate["paperId"] == 0
        assert web.get("/v1/papers").json()["papers"] == []
        assert web.get(f"/v1/papers/{pid}/{rev}/companion").status_code == 404
        assert not registry.query("SELECT id FROM jobs WHERE kind IN ('translate','mobile_card')")
        registry.update_paper(pid, doi=OWN_DOI)
        other_revision = upload(web, existing.read_bytes() + b"\n% second version")
        assert run(web, "import", other_revision)["state"] == "failed"
        assert registry.file_by_sha(other_revision) is None and not web.get("/v1/papers").json()["papers"]
        for content, digest in ((b"wrong digest", rev), (b"invalid PDF", hashlib.sha256(b"invalid PDF").hexdigest())):
            upload(web, content, digest)
            assert run(web, "import", digest)["state"] == "failed"
        assert web.post(url, json={"byteSize": 100}).json() == {"offset": 0}
        assert list(config.imported_dir.glob(".papermap-phone-*.part"))
    service.stop()
    assert not list(config.imported_dir.glob(".papermap-phone-*.part"))
    pair = service.start("127.0.0.1", processing=True)
    with client(pair) as web:
        assert web.get("/v1/tasks/"+duplicate["id"]).status_code == 404
        assert web.get("/v1/papers").json()["papers"] == []


def test_recover_interrupted_upload_without_removing_active_transfers(config, registry, monkeypatch):
    pipeline, _ = make_pipeline(config, registry)
    names = [f".papermap-phone-{n}-123abc.part" for n in (321, 322, 323)]
    blobs = [config.imported_dir / name for name in names]
    for path in blobs:
        path.touch()
    registry.execute("""INSERT INTO jobs(kind,paper_id,status,created_at,updated_at,options_json)
                        VALUES ('mobile_import',0,'queued','','',?)""", (json.dumps({"upload": names[2]}),))
    monkeypatch.setattr(mobile_work.psutil, "pid_exists", lambda n: n == 322)
    mobile_work.reap_uploads(pipeline)
    assert [p.exists() for p in blobs] == [False, True, True]
