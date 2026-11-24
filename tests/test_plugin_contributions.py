"""Declarative packages resolve through normal user-facing entry points."""
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from papermap import plugins, skills_runtime, translate
from papermap.config import PRESETS, load
from papermap.plugin_contributions import export_templates, translation_services, validate_contributions
from papermap.web.app import create_app

from .test_pipeline import make_pipeline
from .test_settings_sections import Document, field_data
from .test_web import _token


@pytest.fixture
def package(config):
    root = config.data_dir / "plugins" / "research"
    root.mkdir(parents=True)
    (root / "read.md").write_text("Trace conclusions back to the evidence.", encoding="utf-8")
    manifest = {"api_version": 1, "id": "research", "name": "Research",
                "contributions": {
                    "providers": [{"id": "lab", "name": "Lab model", "default_model": "test-model",
                                   "base_url": "https://example.invalid/v1"}],
                    "translations": [{"id": "terms", "name": "Lab translation", "provider": "lab",
                                      "mode": "custom", "prompt": "Translate {text} for {target}"}],
                    "skills": [{"id": "read", "name": "Evidence check", "prompt_file": "read.md"}],
                    "exports": [{"id": "citations", "name": "Reading notes", "template": "$title ($year) $doi"}],
                }}
    path = root / "plugin.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    plugins.set_enabled(config, "research", True)
    return path, manifest


def test_end_to_end_config_plugin(config, registry, package, monkeypatch):
    plugin_id, = set(config.model_presets()) - set(PRESETS)
    service = next(s for s in translation_services(config) if s.get("plugin"))
    assert service["provider"] == plugin_id
    pipe, _ = make_pipeline(config, registry)
    monkeypatch.setattr(pipe, "process_pending", lambda: None)
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    csrf = _token(c)
    html = c.get("/settings").text
    assert plugin_id in html and service["name"] in html
    assert c.post("/settings", data={"csrf": csrf, "provider": plugin_id, "tr_default": service["id"],
                                     f"p_{plugin_id}_key": "only-for-lab"}).status_code == 200
    loaded = load(config.path)
    assert loaded.effective_llm()["api_key"] == "only-for-lab"
    assert loaded.effective_llm()["base_url"] == "https://example.invalid/v1"
    assert translate.service_by_id(loaded, None)["id"] == service["id"]

    calls = []
    def chat(*args, **kwargs):
        calls.append(kwargs["eff"])
        return SimpleNamespace(raw="translated")

    monkeypatch.setattr(pipe.llm, "chat_text", chat)
    worker = translate.Translator(config, registry, pipe.llm, service)
    assert worker.call("test", None) == ("translated", "test-model")
    assert calls[0]["api_key"] == "only-for-lab"
    assert "Evidence check" in c.get("/skills").text
    sk = skills_runtime.discover(config)["ext.research.read"]
    assert sk.body.startswith("Trace conclusions") and not skills_runtime._references(sk)

    pid = registry.create_paper(title="Study", year=2023, doi="10.1234/paper", status="done")
    selector, = export_templates(config)
    assert "Reading notes" in c.get("/").text
    downloaded = c.get(f"/export.{selector}", params={"ids": pid})
    assert downloaded.status_code == 200 and downloaded.text == "Study (2023) 10.1234/paper\n"
    assert "papermap.md" in downloaded.headers["content-disposition"]

    plugins.set_enabled(config, "research", False)
    assert not loaded.model_ready() and not loaded.effective_llm()["api_key"]
    with pytest.raises(translate.TranslationError):
        translate.service_by_id(loaded, None)
    assert selector not in export_templates(config) and c.get(f"/export.{selector}").status_code == 404
    processing = Document(c.get("/settings?section=processing").text)
    options = field_data(next(n for n in processing.root.walk() if n.attrs.get("id") == "settings"))
    assert options["provider"] == plugin_id and options["tr_default"] == service["id"]
    options["monthly_budget_usd"] = "19"
    assert c.post("/settings", data=options, follow_redirects=False).status_code == 303
    saved = load(config.path)
    assert saved.llm["provider"] == plugin_id and saved.raw["translation"]["default"] == service["id"]
    assert not saved.effective_llm()["api_key"] and saved.llm["monthly_budget_usd"] == 19


def test_changed_destination_cannot_reuse_old_credentials(config, package):
    path, manifest = package
    old, = set(config.model_presets()) - set(PRESETS)
    config.llm.update(provider=old, presets={old: {"api_key": "private-key"}})
    config.llm["custom"]["api_key"] = "another-private-key"
    manifest["contributions"]["providers"][0]["base_url"] = "https://different.invalid/v1"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    plugins.set_enabled(config, "research", True)
    new, = set(config.model_presets()) - set(PRESETS)
    assert old != new
    assert not config.effective_llm()["api_key"]
    assert not config.resolve_llm({**config.llm, "provider": new})["api_key"]


@pytest.mark.parametrize("section,data", [
    ("exports", {"template": "${unknown}"}), ("skills", {"prompt_file": "../report.md"}),
    ("providers", {"default_model": "m", "base_url": "http://example.com"}),
    ("providers", {"default_model": "m", "base_url": "https://u:p@example.com"}),
    ("providers", {"default_model": "m", "base_url": "https://example.com", "api_key": "secret"}),
    ("translations", {"provider": "unknown"}),
])
def test_invalid_contribution_is_rejected(tmp_path, section, data):
    with pytest.raises(ValueError):
        validate_contributions(tmp_path, {section: [{"id": "demo", "name": "Demo", **data}]})

def test_render_hashes_only_relevant_enabled_plugin_once(config, registry, package, monkeypatch):
    for name in ("second", "disabled"):
        p = config.data_dir / "plugins" / name
        p.mkdir()
        (p / "plugin.yaml").write_text(yaml.safe_dump({"api_version": 1, "id": name, "name": name}), encoding="utf-8")
    plugins.set_enabled(config, "second", True)
    config.llm["provider"], = set(config.model_presets()) - set(PRESETS)
    pipe, _ = make_pipeline(config, registry)
    c = TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")
    snapshots = []
    snapshot = plugins._snapshot

    def count(folder):
        snapshots.append(folder.name)
        return snapshot(folder)

    monkeypatch.setattr(plugins, "_snapshot", count)
    assert c.get("/qa").status_code == 200
    assert snapshots == ["research"]
    snapshots.clear()
    assert c.get("/settings").status_code == 200
    assert sorted(snapshots) == ["research", "second"]
