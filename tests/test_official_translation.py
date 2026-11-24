"""End-to-end routing and persistence for built-in translation providers."""

import json
import re
from collections import Counter
from functools import partial
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
import yaml

from papermap import config as config_module
from papermap import translate

from .conftest import OWN_DOI
from .test_pipeline import make_pipeline
from .test_reader import client as reader_client
from .test_web import _token


PROVIDERS = [
    ("niutrans", "通用文本 Flash", "external"),
    ("zhipu", "glm-4.7-flash", "token"),
    ("tencent", "hy-mt2-lite", "external"),
]


def _transport(provider, sources):
    """Return the input next to a translation marker so paragraph mixups are visible."""
    def handle(request):
        if provider == "niutrans":
            assert request.url.host == "api.niutrans.com"
            form = parse_qs(request.content.decode())
            source = form["srcText"][0]
            assert form["to"] == ["zh"]
            sources.append(source)
            return httpx.Response(200, json={"tgtText": "译文 " + source})
        body = json.loads(request.content)
        if provider == "tencent":
            assert request.url.host == "tokenhub.tencentmaas.com" and body["target"] == "zh"
            source = body["text"]
            sources.append(source)
            answer = "译文 " + source
        else:
            assert request.url.host == "open.bigmodel.cn"
            prompt = body["messages"][0]["content"]
            source = prompt.split("【源文】\n")[1].split("\n\n【约束要求】")[0]
            indexed = re.findall(r"^\[(\d+)\] (.+)$", source, re.M)
            if indexed:
                sources.extend(text for _, text in indexed)
                # Reverse the reply order to exercise paragraph-number alignment.
                answer = "\n\n".join(f"[{n}] 译文 {s}" for n, s in reversed(indexed))
            else:
                sources.append(source)
                answer = "译文 " + source
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}],
                                        "usage": {"prompt_tokens": 18, "completion_tokens": 9}})

    return httpx.MockTransport(handle)


def _configure(config, provider):
    config.raw["translation"]["default"] = provider
    service = translate.service_by_id(config, provider)
    service["api_key"] = "integration-test-key"
    if provider == "niutrans":
        service["app_id"] = "integration-test-app"
    return service


def _assert_accounting(registry, pid, model, billing, token_counts):
    rows = registry.query("SELECT * FROM llm_calls WHERE purpose='translate'")
    assert rows
    for row in rows:
        assert (row["paper_id"], row["model"], row["billing"], row["ok"]) == (pid, model, billing, 1)
        assert (row["prompt_tokens"], row["completion_tokens"]) == token_counts
        assert row["cost_usd"] == 0
    return len(rows)


@pytest.mark.parametrize("provider,model,billing", PROVIDERS)
def test_official_selection_route(config, registry, pdfs, monkeypatch, provider, model, billing):
    web, _, _ = reader_client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    _configure(config, provider)
    sources = []
    monkeypatch.setattr(translate, "Translator", partial(translate.Translator, transport=_transport(provider, sources)))
    token = _token(web)
    result = web.post("/api/translate-text", json={
        "csrf": token, "paper_id": pid, "text": "Seurat   identified\nclusters.",
    }).json()
    assert result == {"ok": True, "model": model, "translation": "译文 Seurat identified clusters."}
    assert sources == ["Seurat identified clusters."]
    assert _assert_accounting(registry, pid, model, billing, (0, 0) if provider == "niutrans" else (18, 9)) == 1
    too_long = web.post("/api/translate-text", json={"csrf": token, "text": "甲" * 4001}).json()
    assert not too_long["ok"] and "全文" in too_long["message"]
    assert sources == ["Seurat identified clusters."]


@pytest.mark.parametrize("provider,model,billing", PROVIDERS)
def test_official_translated_paper_alignment(config, registry, pdfs, provider, model, billing):
    pipe, _ = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    pdf, card = pipe._paper_files(pid)
    service = _configure(config, provider)
    sources = []
    path = translate.translate_paper(config, registry, pipe.llm, pid, pdf, card, pipe.taxonomy(),
                                     transport=_transport(provider, sources))
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["model"] == model and result["service"] == service["name"] and not result["notes"]
    original = translate.paragraphs(pdf)
    assert len(original) == len(result["paragraphs"])
    translated_sources = []
    for before, after in zip(original, result["paragraphs"]):
        assert after["src"] == before["src"]
        if before["kind"] == "meta":
            assert after["tr"] == before["src"] and not after["via"]
        else:
            assert after["tr"] == "译文 " + before["src"] and after["via"] == model
            translated_sources.append(before["src"])
    assert len(translated_sources) > 1 and Counter(sources) == Counter(translated_sources)
    calls = _assert_accounting(registry, pid, model, billing, (0, 0) if provider == "niutrans" else (18, 9))
    assert calls == (len(translate.chunks(original)) if provider == "zhipu" else len(translated_sources))


@pytest.mark.parametrize("provider,missing", [("niutrans", "App ID"), ("zhipu", "API Key"), ("tencent", "API Key")])
def test_missing_credentials_do_not_enqueue_or_fall_back(config, registry, pdfs, monkeypatch, provider, missing):
    config.raw["llm"]["api_key"] = "usable-fake-main-key"
    web, pipe, completions = reader_client(config, registry, pdfs)
    pid = registry.paper_by_doi(OWN_DOI)["id"]
    service = _configure(config, provider)
    service.pop("app_id" if provider == "niutrans" else "api_key")
    config.raw["translation"].update(auto_new_papers=True, fallback_to_main=True)
    assert config.model_ready()
    token = _token(web)
    queued = []
    monkeypatch.setattr(pipe.jobs, "submit", lambda *args: queued.append(args))
    for path, body in [(f"/paper/{pid}/translate", {"service": provider}),
                       ("/api/translate-text", {"text": "An original selection.", "paper_id": pid})]:
        result = web.post(path, json={"csrf": token, **body}).json()
        assert not result["ok"] and missing in result["message"]
    pdf, card = pipe._paper_files(pid)
    with pytest.raises(translate.TranslationError, match=missing):
        pipe.submit_translation(pid, provider)
    with pytest.raises(translate.TranslationError, match=missing):
        translate.translate_paper(config, registry, pipe.llm, pid, pdf, card)
    assert not list(card.parent.glob("*.translation.*"))
    assert not getattr(completions, "translate_calls", 0)
    assert not registry.query("SELECT id FROM llm_calls WHERE purpose='translate'")
    assert not queued
    config.raw["skills"].update(default="study", auto_new_papers=True)
    monkeypatch.setattr("papermap.skills_runtime.discover", lambda cfg: {"study": SimpleNamespace(supported=True)})
    pipe._auto_jobs(pid)
    assert queued == [("skill", pid, "study")]
    assert any(missing in row["message"] for row in
               registry.query("SELECT message FROM events WHERE level='warning' AND paper_id=?", (pid,)))


def test_legacy_services_migrate_once_across_saves(tmp_path):
    path = tmp_path / "old-config.yaml"
    previous = {"id": "custom-reader", "kind": "custom", "name": "读书会翻译", "mode": "polish"}
    path.write_text(yaml.safe_dump({"data_dir": str(tmp_path / "data"),
                                    "translation": {"services": [previous], "default": previous["id"]}}), encoding="utf-8")
    upgraded = config_module.load(path)
    assert upgraded.raw["translation"]["default"] == previous["id"]
    assert upgraded.raw["translation"]["services"][0] == previous
    upgraded_service = translate.service_by_id(upgraded, "niutrans")
    upgraded_service.update(api_key="saved-mock-key", app_id="saved-app")
    upgraded.save()
    for _ in range(2):
        reloaded = config_module.load(path)
        services = reloaded.raw["translation"]["services"]
        assert services[0] == previous and reloaded.raw["translation"]["default"] == previous["id"]
        assert Counter(s["id"] for s in services) == Counter([previous["id"], "niutrans", "zhipu", "tencent"])
        assert translate.service_by_id(reloaded, "niutrans") == upgraded_service
        reloaded.save()
