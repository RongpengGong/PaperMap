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


def _capture(reply):
    captured = {}

    def handler(req: httpx.Request):
        captured["url"] = str(req.url)
        captured["headers"] = req.headers
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json=reply)

    return captured, httpx.MockTransport(handler)


MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]


def test_anthropic_base_ending_in_v1_is_not_doubled():
    from papermap.llm import _AnthropicClient
    cap, tr = _capture({"content": [{"type": "text", "text": "x"}], "usage": {}})
    _AnthropicClient("https://proxy.example/v1", "k", 10, transport=tr).create(model="m", messages=MSGS)
    assert cap["url"] == "https://proxy.example/v1/messages"


def test_responses_client():
    from papermap.llm import _ResponsesClient
    reply = {"model": "gpt-x", "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "content": [{"type": "output_text", "text": "{\"a\": 2}"}]}],
        "usage": {"input_tokens": 7, "output_tokens": 3, "input_tokens_details": {"cached_tokens": 2}}}
    cap, tr = _capture(reply)
    c = _ResponsesClient("https://api.moleapi.com/v1", "k", 10, transport=tr)
    r = c.chat.completions.create(model="gpt-x", messages=MSGS, max_tokens=50,
                                  response_format={"type": "json_object"})
    assert cap["url"] == "https://api.moleapi.com/v1/responses"
    assert cap["headers"]["authorization"] == "Bearer k"
    assert cap["body"]["instructions"] == "sys" and cap["body"]["input"] == [{"role": "user", "content": "hi"}]
    assert cap["body"]["text"] == {"format": {"type": "json_object"}} and cap["body"]["max_output_tokens"] == 50
    assert r.choices[0].message.content == '{"a": 2}'
    assert (r.usage.prompt_tokens, r.usage.completion_tokens, r.usage.prompt_cache_hit_tokens) == (7, 3, 2)


def test_gemini_client():
    from papermap.llm import _GeminiClient
    reply = {"candidates": [{"content": {"parts": [{"text": "thinking...", "thought": True}, {"text": "{\"b\": 1}"}]}}],
             "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 4}}
    cap, tr = _capture(reply)
    c = _GeminiClient("https://generativelanguage.googleapis.com/v1beta", "gk", 10, transport=tr)
    r = c.chat.completions.create(model="gemini-pro", messages=MSGS + [{"role": "assistant", "content": "prev"}],
                                  response_format={"type": "json_object"})
    assert cap["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent"
    assert cap["headers"]["x-goog-api-key"] == "gk"
    assert cap["body"]["systemInstruction"] == {"parts": [{"text": "sys"}]}
    assert [t["role"] for t in cap["body"]["contents"]] == ["user", "model"]
    assert cap["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert r.choices[0].message.content == '{"b": 1}' and r.usage.prompt_tokens == 11


def test_peak_schedule_and_cost():
    from datetime import datetime, timezone

    from papermap.config import is_peak
    from papermap.llm import compute_cost
    mon_10_bj = datetime(2026, 11, 23, 2, 30, tzinfo=timezone.utc)   # Monday 10:30 Beijing
    mon_13_bj = datetime(2026, 11, 23, 5, 0, tzinfo=timezone.utc)    # Monday 13:00 Beijing (lunch)
    sat = datetime(2026, 11, 28, 2, 30, tzinfo=timezone.utc)
    assert is_peak("deepseek", mon_10_bj) and not is_peak("deepseek", mon_13_bj) and not is_peak("deepseek", sat)
    assert not is_peak("none", mon_10_bj)
    # the moleapi bill: (1705*0.15 + 59*0.6 + 62208*0.003)/1e6 * 0.1 = $0.000048 (off-peak)
    eff = {"price_input_per_1m": 0.15, "price_cache_hit_per_1m": 0.003, "price_output_per_1m": 0.6,
           "price_multiplier": 0.1, "peak_schedule": "deepseek"}
    off = compute_cost(eff, 63913, 59, 62208, mon_13_bj)
    assert round(off, 6) == 0.000048
    assert compute_cost(eff, 63913, 59, 62208, mon_10_bj) == 2 * off


def test_custom_cache_price_defaults_to_input():
    from papermap import config as cfgmod
    cfg = cfgmod.from_dict({"llm": {"provider": "custom", "custom": {"price_input_per_1m": 0.5}}})
    assert cfg.effective_llm()["price_cache_hit_per_1m"] == 0.5
    cfg = cfgmod.from_dict({"llm": {"provider": "custom", "custom": {"price_input_per_1m": 0.5, "price_cache_hit_per_1m": 0}}})
    assert cfg.effective_llm()["price_cache_hit_per_1m"] == 0.0


def test_json_reply_with_remarks_around_it_is_still_read():
    from papermap.llm import _parse_json

    data, err = _parse_json('Here it is:\n{"roles": {"lead": {"x": 1}}, "n": [1, 2]}\nNote: roles like {lead} matter.')
    assert err == "" and data == {"roles": {"lead": {"x": 1}}, "n": [1, 2]}
    assert _parse_json('```json\n{"x": 1}\n```\n\nAlso {"y": 2}')[0] == {"x": 1}
    assert _parse_json("no json")[0] is None
