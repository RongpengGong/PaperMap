from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from papermap import antigravity_cli, chat, llm, translate


@pytest.fixture
def agy(config, monkeypatch):
    config.llm.update(provider="antigravity_acp", presets={"antigravity_acp": {
        "model": "paper-pro", "reasoning_effort": "high", "api_key": "unused",
    }})
    cli = Mock()
    cli.status.return_value = {"ready": True}
    cli.model_options.return_value = [
        {"id": "paper-pro", "label": "Paper Pro", "reasoning_efforts": ["low", "high"], "is_current": True},
        {"id": "paper-fast", "label": "Paper Fast", "reasoning_efforts": ["low"]},
    ]
    monkeypatch.setattr(antigravity_cli, "get_cli", lambda *args, **kwargs: cli)
    return cli


def test_main_model_selects_account_models_and_efforts(config, agy):
    eff = config.effective_llm()
    assert config.model_ready()
    assert (eff["protocol"], eff["api_key"], eff["billing"]) == ("antigravity_acp", "", "subscription")
    assert llm.list_models(eff) == ["paper-pro", "paper-fast"]
    choices = {row["key"]: row for row in chat.model_choices(config)}
    assert choices["main"]["efforts"] == ["low", "high"]
    assert choices["antigravity_acp|paper-fast"]["label"].endswith("Paper Fast")
    assert chat.resolve(config, "antigravity_acp|paper-fast")["reasoning_effort"] == ""
    assert chat.resolve(config, "antigravity_acp|paper-fast", "low")["reasoning_effort"] == "low"
    config.llm["presets"]["antigravity_acp"]["model"] = "auto"
    assert chat.model_choices(config)[0]["efforts"] == ["low", "high"]
    assert chat.resolve(config, "main", "low")["reasoning_effort"] == "low"
    agy.status.return_value = {"ready": False}
    assert not config.model_ready()
    assert all(row["key"] != "antigravity_acp|paper-fast" for row in chat.model_choices(config))


def test_cards_and_resumed_translation_use_selected_model_and_subscription(config, registry, pdfs, agy):
    client = llm.LLMClient(config, registry)
    usage = SimpleNamespace(prompt_tokens=19, completion_tokens=8)
    outputs = ['{"title":"Paper"}', "译文"]
    create = agy.chat.completions.create
    create.side_effect = [
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=output))],
                        model="paper-pro", usage=usage) for output in outputs
    ]
    card = client.chat_json("JSON", "paper", lambda _: [], "extract")
    assert card.data == {"title": "Paper"}
    service = translate.service_by_id(config, "main")
    options = translate.translation_options(config, service, pdfs["paper"])
    config.llm["provider"] = "deepseek"
    translator = translate.Translator(config, registry, client, service, options=options)
    try:
        assert translator.call("translate", None) == ("译文", "paper-pro")
    finally:
        translator.http.close()
    assert all(call.kwargs["model"] == "paper-pro" and call.kwargs["reasoning_effort"] == "high"
               for call in create.call_args_list)
    rows = registry.query("SELECT purpose, billing, cost_usd FROM llm_calls ORDER BY id")
    assert [(r["purpose"], r["billing"], r["cost_usd"]) for r in rows] == [
        ("extract", "subscription", 0.0), ("translate", "subscription", 0.0),
    ]
