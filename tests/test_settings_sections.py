"""Settings navigation and full-form persistence, using temporary app data only."""

from __future__ import annotations

import copy
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from papermap.pipeline import Pipeline
from papermap.web.app import create_app

from .conftest import FakeMetadata

SECTIONS = ("models", "processing", "translation", "import", "library", "backup", "taxonomy", "appearance", "extensions", "application")
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class Node:
    def __init__(self, tag="root", attrs=()):
        self.tag, self.attrs, self.children, self.data = tag, dict(attrs), [], []

    def all(self, predicate):
        return [node for node in self.walk() if predicate(node)]

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def text(self):
        return "".join(self.data) + "".join(child.text() for child in self.children)


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].data.append(data)


def nodes(html):
    return Document(html).root


def field_data(form):
    """The successful controls submitted by the browser, including hidden panels."""
    data = {}
    for node in form.walk():
        name = node.attrs.get("name")
        if not name or "disabled" in node.attrs:
            continue
        if node.tag == "input":
            kind = node.attrs.get("type", "text")
            if kind in ("checkbox", "radio") and "checked" not in node.attrs:
                continue
            if kind not in ("submit", "button", "file"):
                data[name] = node.attrs.get("value", "on" if kind in ("checkbox", "radio") else "")
        elif node.tag == "textarea":
            data[name] = node.text()
        elif node.tag == "select":
            options = node.all(lambda item: item.tag == "option" and "disabled" not in item.attrs)
            selected = next((item for item in options if "selected" in item.attrs), options[0] if options else None)
            if selected:
                data[name] = selected.attrs.get("value", selected.text())
        elif node.tag == "button" and node.attrs.get("type", "submit") == "submit":
            data[name] = node.attrs.get("value", "")
    return data


@pytest.fixture
def settings_client(config, registry, monkeypatch):
    # No rendering or pending-processing path may inspect real account state.
    monkeypatch.setattr(type(config), "model_ready", lambda self: False)
    monkeypatch.setattr("papermap.web.app.kb_git.available", lambda: True)
    pipe = Pipeline(config, registry, metadata=FakeMetadata())
    monkeypatch.setattr(pipe, "process_pending", lambda: {})
    return TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765")


@pytest.mark.parametrize("section", SECTIONS)
def test_settings_deep_links_have_one_visible_section_and_one_form(settings_client, section):
    response = settings_client.get("/settings", params={"section": section})
    assert response.status_code == 200
    doc = nodes(response.text)
    panels = doc.all(lambda node: "data-settings-panel" in node.attrs)
    assert {panel.attrs["data-settings-panel"] for panel in panels} == set(SECTIONS)
    assert [panel.attrs["data-settings-panel"] for panel in panels if "hidden" not in panel.attrs] == [section]
    links = doc.all(lambda node: node.tag == "a" and "data-settings-link" in node.attrs)
    assert {link.attrs["data-settings-link"] for link in links} == set(SECTIONS)
    assert all(parse_qs(urlsplit(link.attrs["href"]).query)["section"] == [link.attrs["data-settings-link"]] for link in links)
    forms = doc.all(lambda node: node.tag == "form" and node.attrs.get("id") == "settings")
    assert len(forms) == 1
    assert field_data(forms[0])["settings_section"] == section
    pickers = forms[0].all(lambda node: node.attrs.get("name") == "provider")
    assert len(pickers) == 1 and pickers[0].tag == "select" and pickers[0].attrs.get("id") == "provider_picker"


@pytest.mark.parametrize("section", ["", "unknown", "../models", '<script>alert(1)</script>'])
def test_invalid_settings_section_falls_back_safely(settings_client, section):
    response = settings_client.get("/settings", params={"section": section})
    assert response.status_code == 200
    panels = nodes(response.text).all(lambda node: "data-settings-panel" in node.attrs and "hidden" not in node.attrs)
    assert [panel.attrs["data-settings-panel"] for panel in panels] == ["models"]
    assert '<script>alert(1)</script>' not in response.text


def test_saving_from_hidden_sections_preserves_all_settings(settings_client, config):
    raw, llm = config.raw, config.llm
    llm.update(provider="custom", api_key="test-official-secret", monthly_budget_usd=11)
    llm["custom"].update(base_url="https://model.invalid/v1", api_key="test-custom-secret", protocol="responses",
        model="custom-model", price_input_per_1m=2.5, price_cache_hit_per_1m=0.5, price_output_per_1m=7.5,
        price_multiplier=2, peak_schedule="deepseek", billing="subscription")
    llm["presets"] = {
        "chatgpt_plan": {"model": "gpt-6.2-sol", "reasoning_effort": "ultra"},
        "claude_cli": {"model": "opus", "reasoning_effort": "max"},
        "anthropic": {"model": "claude-opus-5-5", "api_key": "test-preset-secret",
                      "price_input_per_1m": 3, "price_cache_hit_per_1m": 0.3, "price_output_per_1m": 15},
    }
    raw.update(output_language="bilingual", inbox_dirs=["/tmp/test-inbox-a", "/tmp/test-inbox-b"], new_field_threshold=9, knowledge_git=True)
    raw["embedding"]["model"] = "test-embedding"
    raw["exclude"] = {"dir_names": ["_private", "drafts"], "filename_patterns": ["*draft*", "*private*"]}
    raw["metadata"]["contact_email"] = "reader@example.invalid"
    raw["translation"].update(default="main", target_language="en", fallback_to_main=True, auto_new_papers=True)
    config.save()
    expected = copy.deepcopy(raw)
    page = settings_client.get("/settings?section=processing")
    form = nodes(page.text).all(lambda node: node.tag == "form" and node.attrs.get("id") == "settings")[0]
    data = field_data(form)
    # Hidden panels remain in the same form and are not disabled or discarded.
    assert data["custom_base_url"] == "https://model.invalid/v1" and data["tr_auto"]
    assert data["p_chatgpt_plan_reasoning_effort"] == "ultra"
    data["monthly_budget_usd"] = "42"
    response = settings_client.post("/settings", data=data, follow_redirects=False)
    assert response.status_code == 303 and config.llm["monthly_budget_usd"] == 42
    for key in ("output_language", "inbox_dirs", "exclude", "metadata", "translation", "new_field_threshold", "embedding", "knowledge_git"):
        assert config.raw[key] == expected[key], key
    assert config.llm["provider"] == "custom" and config.llm["api_key"] == "test-official-secret"
    assert config.llm["custom"] == expected["llm"]["custom"]
    for provider in ("chatgpt_plan", "claude_cli", "anthropic"):
        assert config.llm["presets"][provider] == expected["llm"]["presets"][provider]
    for secret in ("test-official-secret", "test-custom-secret", "test-preset-secret"):
        assert secret not in page.text


@pytest.mark.parametrize("section", [None, "translation", "import", "bogus"])
def test_saved_redirect_keeps_valid_section_and_legacy_posts(settings_client, section):
    page = settings_client.get("/settings")
    form = nodes(page.text).all(lambda node: node.tag == "form" and node.attrs.get("id") == "settings")[0]
    data = field_data(form)
    data.pop("settings_section", None)
    if section is not None:
        data["settings_section"] = section
    response = settings_client.post("/settings", data=data, follow_redirects=False)
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    query = parse_qs(location.query)
    assert location.path == "/settings" and query["saved"] == ["1"]
    if section in SECTIONS:
        assert query["section"] == [section]
    else:
        assert query.get("section", ["models"]) == ["models"]


def test_library_and_settings_sidebars_are_exclusive_and_have_return_paths(settings_client):
    settings = nodes(settings_client.get("/settings?section=import").text)
    sidebar = settings.all(lambda node: node.tag == "aside" and "sidebar" in node.attrs.get("class", "").split())[0]
    assert sidebar.all(lambda node: "data-settings-link" in node.attrs)
    assert not sidebar.all(lambda node: node.attrs.get("id") in ("collections", "import_btn", "import_input"))
    assert sidebar.all(lambda node: node.tag == "a" and node.attrs.get("href") == "/")
    imports = settings.all(lambda node: node.attrs.get("data-settings-panel") == "import")[0]
    assert imports.all(lambda node: node.tag == "a" and urlsplit(node.attrs.get("href", "")).path == "/zotero")
    library = nodes(settings_client.get("/").text)
    assert library.all(lambda node: node.attrs.get("id") == "collections")
    assert not library.all(lambda node: "data-settings-link" in node.attrs)
    assert library.all(lambda node: node.tag == "a" and urlsplit(node.attrs.get("href", "")).path == "/settings")


def test_zotero_import_has_a_settings_import_return_link(settings_client, tmp_path):
    # An explicit nonexistent test directory avoids even probing the real Zotero library.
    response = settings_client.get("/zotero", params={"dir": str(tmp_path / "absent-zotero")})
    assert response.status_code == 200
    links = nodes(response.text).all(lambda node: node.tag == "a")
    assert any(urlsplit(link.attrs.get("href", "")).path == "/settings"
               and parse_qs(urlsplit(link.attrs["href"]).query).get("section") == ["import"] for link in links)
