import json

import httpx

from papermap import config as cfgmod
from papermap.llm import LLMNotConfigured, _AnthropicClient, check_connection, make_client


def test_env_key_wins_and_legacy_config_migrates(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    cfg = cfgmod.from_dict({"llm": {"api_key": "local"}})
    assert cfg.effective_llm()["api_key"] == "from-env" and cfg.effective_llm()["key_from_env"]
    legacy = cfgmod.from_dict({"llm": {"base_url": "https://other.example/v1", "model": "x", "api_key": "k"}})
    eff = legacy.effective_llm()
    assert (eff["provider"], eff["base_url"], eff["model"]) == ("custom", "https://other.example/v1", "x")


def test_missing_key_is_clear(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    eff = cfgmod.from_dict({}).effective_llm()
    try:
        make_client(eff)
    except LLMNotConfigured as exc:
        assert "API key" in str(exc)
    else:
        raise AssertionError("expected LLMNotConfigured")
    assert check_connection(eff)["ok"] is False


def test_anthropic_client_maps_messages_api():
    captured = {}

    def handler(req: httpx.Request):
        captured["url"] = str(req.url)
        captured["headers"] = req.headers
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"model": "m", "content": [{"type": "text", "text": "{\"a\": 1}"}],
                                         "usage": {"input_tokens": 10, "output_tokens": 5}})

    c = _AnthropicClient("https://api.deepseek.com/anthropic", "k", 10, transport=httpx.MockTransport(handler))
    resp = c.chat.completions.create(model="m", max_tokens=50, temperature=0.2,
                                     messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
                                     response_format={"type": "json_object"}, extra_body={"thinking": {"type": "disabled"}})
    assert captured["url"] == "https://api.deepseek.com/anthropic/v1/messages"
    assert captured["headers"]["x-api-key"] == "k"
    assert captured["body"]["system"] == "sys" and captured["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["body"]["thinking"] == {"type": "disabled"} and "response_format" not in captured["body"]
    assert resp.choices[0].message.content == '{"a": 1}' and resp.usage.prompt_tokens == 10
