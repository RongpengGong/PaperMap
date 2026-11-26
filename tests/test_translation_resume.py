"""Interrupted paid work, parallel responses and ownership of the queue."""

import json
import multiprocessing
import threading
import time

import pytest

from papermap import translate as tr
from papermap.config import load
from papermap.jobs import JobRunner
from papermap.pipeline import Pipeline
from papermap.registry import Registry, now_iso

from .test_pipeline import make_pipeline


def eventually(predicate):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.02)
    raise AssertionError("timed out waiting for worker")


def _paras(*args, **kwargs):
    return [{"kind": "body", "src": p} for p in ("one", "two", "three", "four")]


def _groups(_paras):
    return [[0, 1], [2, 3]]


def _interrupted_app(config_path, quit_requested):
    """This app instance is forcibly stopped while its first group is in flight."""
    config = load(config_path)
    reg = Registry(config.db_path)
    pipe = Pipeline(config, reg)
    tr.paragraphs, tr.chunks = _paras, _groups

    def translate(self, text, target, terms, paper_id, *, numbered=False):
        if "one" in text:
            threading.Event().wait(60)
        return "[1] old three\n[2] old four", self.model

    tr.Translator.translate_text = translate
    pipe.submit_translation(1, "main")
    quit_requested.wait(60)


@pytest.mark.parametrize("termination", ["quit", "kill"])
def test_restart_resumes_durable_out_of_order_group(config, registry, pdfs, monkeypatch, termination):
    pipe, _ = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    config.raw["llm"].update(api_key="fake-private-key", model="old-model")
    # the child process sees the real system language; fix the languages so both sides agree
    config.raw["output_language"] = "zh"
    config.raw["translation"]["target_language"] = "zh"
    config.save()
    ctx = multiprocessing.get_context("spawn")
    quit_requested = ctx.Event()
    child = ctx.Process(target=_interrupted_app, args=(config.path, quit_requested))
    child.start()
    try:
        eventually(lambda: len(registry.query("SELECT * FROM translation_parts")) == 2)
        row = registry.one("SELECT * FROM jobs WHERE kind='translate'")
        jid = row["id"]
        assert "fake-private-key" not in row["options_json"]
        # A second live application can inspect and submit into the same queue. It cannot
        # steal the interrupted work while the first process still holds its worker lock.
        pipe.jobs.recover()
        assert pipe.submit_translation(1, "main") == jid
        assert registry.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "running"
        assert [p["paragraph"] for p in registry.query("SELECT paragraph FROM translation_parts ORDER BY 1")] == [2, 3]
        if termination == "quit":
            quit_requested.set()
            child.join(4)
            assert child.exitcode == 0  # a normal exit does not wait for the stalled request
    finally:
        if child.is_alive():
            child.terminate()
        child.join(8)
        assert not child.is_alive()

    config.raw["llm"]["model"] = "new-model"
    config.raw["translation"]["target_language"] = "en"
    for svc in config.raw["translation"]["services"]:
        if svc["id"] == "main":
            svc.update(mode="custom", prompt="Changed prompt")
    monkeypatch.setattr(tr, "paragraphs", _paras)
    monkeypatch.setattr(tr, "chunks", _groups)
    calls = []

    def translated(self, text, target, terms, paper_id, *, numbered=False):
        calls.append((self.model, target, self.service["mode"], text))
        return "[1] resumed one\n[2] resumed two", self.model

    monkeypatch.setattr(tr.Translator, "translate_text", translated)
    pipe.jobs.recover()
    eventually(lambda: registry.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "done")
    assert calls == [("old-model", "zh", "translate", "[1] one\n\n[2] two")]
    assert not registry.query("SELECT * FROM translation_parts")
    _, card = pipe._paper_files(1)
    out = json.loads(card.with_suffix(".translation.json").read_text(encoding="utf-8"))
    assert [p["tr"] for p in out["paragraphs"]] == ["resumed one", "resumed two", "old three", "old four"]
    assert out["revision"] and out["model"] == "old-model"

    fresh = pipe.submit_translation(1, "main")
    eventually(lambda: registry.one("SELECT status FROM jobs WHERE id=?", (fresh,))["status"] == "done")
    assert fresh != jid and len(calls) == 3
    assert all(c[:3] == ("new-model", "en", "custom") for c in calls[1:])


def test_partial_native_group_retry_and_new_pdf(config, registry, pdfs, monkeypatch):
    pipe, _ = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    service = tr.service_by_id(config, "tencent")
    service["api_key"] = "translation-secret"
    config.raw["translation"]["fallback_to_main"] = False
    monkeypatch.setattr(tr, "paragraphs", _paras)
    monkeypatch.setattr(tr, "chunks", lambda paras: [[0, 1, 2, 3]])
    calls = []

    def sometimes_fails(self, text, target, terms, paper_id, *, numbered=False):
        calls.append(text)
        if text == "three" and calls.count("three") == 1:
            raise OSError("service offline")
        return text.upper(), self.model

    monkeypatch.setattr(tr.Translator, "translate_text", sometimes_fails)
    jid = pipe.submit_translation(1, "tencent")
    eventually(lambda: registry.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "error")
    job = registry.one("SELECT progress,total FROM jobs WHERE id=?", (jid,))
    assert (job["progress"], job["total"]) == (2, 4)
    assert [p["text"] for p in registry.query("SELECT * FROM translation_parts ORDER BY paragraph")] == ["ONE", "TWO"]
    assert pipe.submit_translation(1, "tencent") == jid
    eventually(lambda: registry.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "done")
    assert calls == ["one", "two", "three", "three", "four"]

    pdf, _ = pipe._paper_files(1)
    options = tr.translation_options(config, service, pdf)
    pdf.write_bytes(pdf.read_bytes() + b"\n%changed\n")
    # A pending job pinned to an older revision must fail before contacting its service.
    stale = pipe.jobs.submit("translate", 1, "tencent", options=options)
    eventually(lambda: registry.one("SELECT status FROM jobs WHERE id=?", (stale,))["status"] == "error")
    assert "PDF" in registry.one("SELECT message FROM jobs WHERE id=?", (stale,))["message"]
    assert len(calls) == 5
    fresh = pipe.submit_translation(1, "tencent")
    eventually(lambda: registry.one("SELECT status FROM jobs WHERE id=?", (fresh,))["status"] == "done")
    assert fresh != stale and len(calls) == 9


def test_legacy_queue_and_inspection_are_side_effect_free(registry):
    for kind, status in [("skill", "running"), ("translate", "running"), ("translate", "queued")]:
        registry.execute("INSERT INTO jobs(kind, paper_id, arg, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                         (kind, 1, "main", status, now_iso(), now_iso()))
    calls = []
    other = Registry(registry.db_path)
    try:
        runner = JobRunner(other, {"translate": lambda row, progress: calls.append(row["id"])})
        assert [r["status"] for r in other.query("SELECT status FROM jobs ORDER BY id")] == [
            "running", "running", "queued"]
        runner.recover()
        eventually(lambda: len(other.query("SELECT id FROM jobs WHERE status='done'")) == 2)
        assert calls == [2, 3]
        assert other.one("SELECT status FROM jobs WHERE id=1")["status"] == "error"
    finally:
        other.close()
