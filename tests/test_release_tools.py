"""Update check, redacted feedback report, sample library."""
import json
import time

import httpx

from papermap import appupdate, demo, feedback
from papermap.web.app import create_app
from fastapi.testclient import TestClient

from .conftest import make_pdf, OWN_TITLE, BODY
from .test_pipeline import make_pipeline
from .test_web import _token


def test_update_check_caches_and_compares(config, monkeypatch):
    calls = []

    def handler(req):
        calls.append(req.url)
        assert "Authorization" not in req.headers and "PaperMap-update-check" in req.headers["User-Agent"]
        return httpx.Response(200, json={"tag_name": "v9.9.0", "html_url": "https://github.com/x/y/releases/tag/v9.9.0"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    st = appupdate.check(config, client=client)
    assert st["newer"] and st["latest"] == "9.9.0" and st["url"].endswith("v9.9.0")
    appupdate.check(config, client=client)
    assert len(calls) == 1  # once a day
    appupdate.check(config, force=True, client=client)
    assert len(calls) == 2
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("offline"))))
    st = appupdate.check(config, force=True, client=bad)
    assert st["latest"] == "9.9.0" and "error" in json.loads((config.data_dir / appupdate.STATE).read_text("utf-8"))
    assert appupdate._ver("v0.10.0") > appupdate._ver("0.9.9")  # noqa: SLF001


def test_feedback_report_removes_keys_paths_and_email(config, registry):
    config.raw["llm"]["api_key"] = "sk-THIS-IS-A-SECRET-KEY-123456"
    config.raw.setdefault("metadata", {})["openalex_api_key"] = "openalex-secret-987654321"
    registry.log("error", f"Failed reading {config.knowledge_dir}/papers/x.pdf with sk-THIS-IS-A-SECRET-KEY-123456 for me@uni.de")
    rep = feedback.report(config, registry, "It crashed")
    body = rep["body"]
    assert "It crashed" in body and "PaperMap" in body and "Recent warnings" in body
    assert "sk-THIS" not in body and "openalex-secret" not in body and "me@uni.de" not in body
    assert str(config.knowledge_dir) not in body and "<data>/papers/x.pdf" in body
    url = feedback.issue_url("Bug: x", body)
    assert url.startswith("https://github.com/RongpengGong/PaperMap/issues/new?") and "sk-THIS" not in url


def test_feedback_page_and_url_route(config, registry):
    pipe, _ = make_pipeline(config, registry)
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    config.raw["llm"]["api_key"] = "sk-ROUTE-SECRET-00000000000000"
    assert "反馈问题" in c.get("/feedback").text
    r = c.post("/api/feedback/url", json={"csrf": _token(c), "title": "Bug: y", "body": "my key sk-ROUTE-SECRET-00000000000000"}).json()
    assert r["ok"] and "sk-ROUTE" not in r["url"] and "sk-ROUTE" not in r["body"]
    assert c.post("/api/feedback/url", json={"csrf": "x", "body": ""}).status_code == 403


def test_sample_library_imports_into_its_own_group(config, registry, pdfs, monkeypatch):
    pipe, _ = make_pipeline(config, registry)
    made = []

    def fake_download(url, folder, name, timeout=90):
        folder.mkdir(parents=True, exist_ok=True)
        i = len(made)
        p = make_pdf(folder / name, [f"#Sample paper number {i} about single-cell spatial omics\n{BODY}"])
        made.append(p)
        return p

    monkeypatch.setattr(demo.tracking, "pmc_pdf_urls", lambda pmcid: [f"https://example.org/{pmcid}.pdf"])
    monkeypatch.setattr(demo.tracking, "_download", fake_download)
    monkeypatch.setattr(demo, "PAPERS", [("10.5555/demo.1", "PMC1"), ("10.5555/demo.2", "PMC2")])
    assert demo.start(pipe)
    for _ in range(200):
        if not demo.status()["running"]:
            break
        time.sleep(0.05)
    st = demo.status()
    assert st["done"] == 2 and len(made) == 2
    group = registry.one("SELECT id FROM collections WHERE name=?", (demo.GROUP,))
    assert group is not None
