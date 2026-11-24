import json

import httpx
import pytest

from papermap import llm as llm_module, qa
from papermap.chatgpt_plan import ChatGPTPlanClient
from papermap.llm import BudgetExceeded, LLMClient, LLMHTTPError
from papermap.pipeline import Pipeline
from papermap.config import ConfigError, effective_llm, load, normalize_reasoning_effort

from .conftest import CARD, CLASSIFY, FakeMetadata, OWN_DOI


class Auth:
    def access_token(self, rejected_token=None):
        return "renewed" if rejected_token else "initial"


def stream(*events):
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          text="".join("data: " + json.dumps(e) + "\n\n" for e in events))


def test_account_model_catalog_renews_rejected_token():
    seen = []

    def handler(req):
        assert str(req.url) == "https://api.openai.com/v1/models"
        seen.append(req.headers["authorization"])
        if len(seen) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"models": [
            {"slug": "alpha", "visibility": "list"}, {"slug": "internal", "visibility": "hide"}]})

    assert ChatGPTPlanClient(Auth(), transport=httpx.MockTransport(handler)).model_ids() == ["alpha"]
    assert seen == ["Bearer initial", "Bearer renewed"]


@pytest.mark.parametrize("terminal,error", [
    (None, LLMHTTPError),
    ({"type": "response.incomplete"}, LLMHTTPError),
    ({"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}},
     BudgetExceeded),
])
def test_partial_card_is_never_returned(terminal, error):
    events = [{"type": "response.output_text.delta", "delta": '{"incomplete":true}'}]
    if terminal:
        events.append(terminal)
    client = ChatGPTPlanClient(Auth(), transport=httpx.MockTransport(lambda _: stream(*events)))
    with pytest.raises(error):
        client.create(model="m", messages=[{"role": "user", "content": "A card"}])


def test_import_uses_chatgpt_contract_and_records_plan_usage(config, registry, pdfs, monkeypatch):
    config.llm.update(provider="chatgpt_plan", presets={"chatgpt_plan": {"model": "account-model"}},
                      monthly_budget_usd=0.01)
    registry.record_llm_call(None, "old", "api", 10, 5, 1.0, True)
    results = iter([CARD, CLASSIFY, {"keywords": ["Visium", "spatial"]}, "The study used Visium [S1]."])

    def handler(req):
        assert str(req.url) == "https://api.openai.com/v1/responses"
        body = json.loads(req.content)
        assert set(body) == {"input", "instructions", "model", "store", "stream"}
        assert body["model"] == "account-model" and body["input"][0]["role"] == "user"
        assert body["stream"] is True and body["store"] is False
        result = next(results)
        return stream({"type": "response.output_text.delta", "delta": result if isinstance(result, str) else json.dumps(result)},
                      {"type": "response.completed", "response": {
                          "status": "completed", "model": "account-model",
                          "usage": {"input_tokens": 21, "output_tokens": 14},
                      }})

    client = ChatGPTPlanClient(Auth(), transport=httpx.MockTransport(handler))
    monkeypatch.setattr(llm_module, "_chatgpt_plan_client", lambda eff, timeout: client)
    pipe = Pipeline(config, registry, metadata=FakeMetadata(), llm=LLMClient(config, registry))
    monkeypatch.setattr(pipe, "_fetch_citations", lambda _: None)
    assert pipe.handle_path(pdfs["paper"]) == "processed"
    paper = registry.paper_by_doi(OWN_DOI)
    assert (config.knowledge_dir / paper["kb_path"]).is_file()
    rows = registry.query("SELECT * FROM llm_calls WHERE paper_id=? ORDER BY id", (paper["id"],))
    assert [r["purpose"] for r in rows] == ["extract", "classify"]
    assert all(r["billing"] == "subscription" and r["cost_usd"] == 0 and r["completion_tokens"] == 14 for r in rows)
    answer = qa.ask(config, registry, pipe.llm, "Which paper used Visium?")
    assert answer["sources"][0]["paper_id"] == paper["id"]
    assert answer["model"] == "account-model" and answer["cost"] == 0
    assert paper["kb_path"].removesuffix(".md") in (config.knowledge_dir / answer["kb_path"]).read_text(encoding="utf-8")


def completed(text="OK"):
    return stream({"type": "response.output_text.delta", "delta": text},
                  {"type": "response.completed", "response": {"status": "completed",
                    "usage": {"input_tokens": 12, "output_tokens": 7}}})


def test_model_options_preserve_provider_capabilities_and_missing_metadata(monkeypatch):
    catalog = {"models": [
        {"slug": "gpt-a", "visibility": "list", "display_name": "GPT A", "default_reasoning_level": "medium",
         "supported_reasoning_levels": [{"effort": x, "description": "D"}
                                        for x in ("low", "high", "xhigh", "max", "ultra", "persistent", "future-mode")]},
        {"slug": "gpt-b", "visibility": "list", "defaultReasoningEffort": "high",
         "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}]},
        {"slug": "gpt-c", "visibility": "list", "supported_reasoning_efforts": ["low", "high", "high"]},
        {"slug": "no-metadata", "visibility": "list"},
        {"slug": "no-controls", "visibility": "list", "supported_reasoning_levels": []},
        {"slug": "malformed", "visibility": "list", "supported_reasoning_levels": [{"missing": "effort"}]},
        {"slug": "hidden", "visibility": "hide", "supported_reasoning_levels": ["ultra"]},
    ]}
    client = ChatGPTPlanClient(Auth(), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=catalog)))
    monkeypatch.setattr(llm_module, "_chatgpt_plan_client", lambda eff, timeout: client)
    options = llm_module.list_chatgpt_model_options({"provider": "chatgpt_plan"})
    assert options[0] == {"id": "gpt-a", "label": "GPT A", "default_reasoning_effort": "medium",
                          "reasoning_efforts_known": True,
                          "reasoning_efforts": ["low", "high", "xhigh", "max", "ultra", "persistent", "future-mode"]}
    assert options[1]["reasoning_efforts"] == ["low", "high"] and options[1]["default_reasoning_effort"] == "high"
    assert options[2]["reasoning_efforts"] == ["low", "high"]
    assert options[3]["reasoning_efforts_known"] is False and options[3]["reasoning_efforts"] == []
    assert options[4]["reasoning_efforts_known"] is True and options[4]["reasoning_efforts"] == []
    assert options[5]["reasoning_efforts_known"] is False
    assert llm_module.list_models({"provider": "chatgpt_plan"}) == [o["id"] for o in options]
    options[0]["reasoning_efforts"].clear()
    assert client.model_options()[0]["reasoning_efforts"]  # callers cannot modify cached capabilities


@pytest.mark.parametrize("effort", ["low", "high", "xhigh", "max", "ultra", "persistent", "future-mode"])
def test_reasoning_effort_uses_exact_model_value_on_wire(effort):
    seen = []
    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json={"models": [{"slug": "m", "visibility": "list",
                "supported_reasoning_levels": [{"effort": effort}]}]})
        body = json.loads(req.content)
        seen.append(body)
        return completed()
    client = ChatGPTPlanClient(Auth(), transport=httpx.MockTransport(handler))
    client.create(model="m", messages=[{"role": "user", "content": "hello"}], reasoning_effort=effort,
                  temperature=0.2, max_tokens=50)
    assert seen[0]["reasoning"] == {"effort": effort}
    assert seen[0]["store"] is False and seen[0]["stream"] is True
    assert "max_output_tokens" not in seen[0] and "temperature" not in seen[0]


@pytest.mark.parametrize("effort", [None, "", "default"])
def test_model_default_never_sends_reasoning_or_fetches_catalog(effort):
    def handler(req):
        assert req.method == "POST"
        assert "reasoning" not in json.loads(req.content)
        return completed()
    client = ChatGPTPlanClient(Auth(), transport=httpx.MockTransport(handler))
    client.create(model="m", messages=[{"role": "user", "content": "hello"}], reasoning_effort=effort)


def test_ultra_is_rejected_for_model_without_ultra_and_not_downgraded():
    requests = []
    def handler(req):
        requests.append(req)
        assert req.method == "GET"  # invalid choice never reaches inference
        return httpx.Response(200, json={"models": [{"slug": "m", "visibility": "list",
            "supported_reasoning_levels": [{"effort": x} for x in ("low", "high", "xhigh", "max")]}]})
    client = ChatGPTPlanClient(Auth(), transport=httpx.MockTransport(handler))
    with pytest.raises(LLMHTTPError, match="不支持所选推理强度"):
        client.create(model="m", messages=[{"role": "user", "content": "hello"}], reasoning_effort="ultra")
    with pytest.raises(LLMHTTPError, match="不支持所选推理强度"):
        client.create(model="m", messages=[{"role": "user", "content": "hello"}], reasoning_effort="ultra")
    assert len(requests) == 1  # one authoritative listing, not one per failed paper


@pytest.mark.parametrize("catalog_response", [
    httpx.Response(200, json={"models": [{"slug": "m", "visibility": "list"}]}),
    httpx.Response(503),
])
def test_unknown_capability_preserves_explicit_effort_for_server_validation(catalog_response):
    def handler(req):
        if req.method == "GET":
            return catalog_response
        assert json.loads(req.content)["reasoning"] == {"effort": "ultra"}
        return completed()
    client = ChatGPTPlanClient(Auth(), transport=httpx.MockTransport(handler))
    client.create(model="m", messages=[{"role": "user", "content": "hello"}], reasoning_effort="ultra")


def test_model_explicitly_publishing_no_efforts_rejects_override():
    def handler(req):
        assert req.method == "GET"
        return httpx.Response(200, json={"models": [{"slug": "m", "visibility": "list", "supported_reasoning_levels": []}]})
    with pytest.raises(LLMHTTPError, match="不支持所选推理强度"):
        ChatGPTPlanClient(Auth(), transport=httpx.MockTransport(handler)).create(
            model="m", messages=[{"role": "user", "content": "hello"}], reasoning_effort="high")


def test_saved_effort_reaches_connection_json_text_and_vision_requests(config, registry, monkeypatch):
    config.llm.update(provider="chatgpt_plan", presets={"chatgpt_plan": {"model": "m", "reasoning_effort": "ultra"}})
    config.save()
    assert load(config.path).effective_llm()["reasoning_effort"] == "ultra"
    bodies, catalog_calls = [], []
    answers = iter(["OK", "not JSON", '{"ok":true}', "text answer", "image answer"])
    def handler(req):
        if req.method == "GET":
            catalog_calls.append(req)
            return httpx.Response(200, json={"models": [{"slug": "m", "visibility": "list",
                "supported_reasoning_levels": [{"effort": "ultra"}]}]})
        body = json.loads(req.content)
        bodies.append(body)
        return completed(next(answers))
    client = ChatGPTPlanClient(Auth(), transport=httpx.MockTransport(handler))
    monkeypatch.setattr(llm_module, "_chatgpt_plan_client", lambda eff, timeout: client)
    assert llm_module.check_connection(config.effective_llm())["ok"]
    llm = LLMClient(config, registry)
    assert llm.chat_json("Return JSON", "Question", lambda data: [], "extract").data == {"ok": True}
    assert llm.chat_text("System", "Question", "qa").raw == "text answer"
    assert llm.chat_text("Describe", "Figure", "figure", images=[b"png-test"]).raw == "image answer"
    assert len(bodies) == 5 and all(body["reasoning"] == {"effort": "ultra"} for body in bodies)
    assert len(catalog_calls) == 1  # the JSON-repair attempt and reader tools reuse capabilities
    assert any(part["type"] == "input_image" for part in bodies[-1]["input"][0]["content"])
    assert bodies[2]["input"][-2]["role"] == "assistant"  # JSON retry keeps its explicit effort


@pytest.mark.parametrize("provider", ["chatgpt_plan", "claude_cli"])
def test_subscription_effort_configuration_defaults_and_future_values(provider):
    assert effective_llm({"provider": provider})["reasoning_effort"] == ""
    assert effective_llm({"provider": provider, "presets": {provider: {"reasoning_effort": "default"}}})["reasoning_effort"] == ""
    assert effective_llm({"provider": provider, "presets": {provider: {"reasoning_effort": "future-mode"}}})["reasoning_effort"] == "future-mode"
    assert effective_llm({"provider": "anthropic", "presets": {"anthropic": {"reasoning_effort": "ultra"}}})["reasoning_effort"] == ""


@pytest.mark.parametrize("bad", [23, [], "two words", "high\nlow", "\x00", "a" * 129])
def test_effort_format_validation_does_not_use_a_fixed_enum(bad):
    with pytest.raises(ConfigError):
        normalize_reasoning_effort(bad)
    assert normalize_reasoning_effort("ultra") == "ultra" and normalize_reasoning_effort("very-future-v3") == "very-future-v3"


def test_expired_catalog_network_failure_keeps_known_restrictions_and_backs_off():
    catalog_calls, posted = [], []
    def handler(req):
        if req.method == "GET":
            catalog_calls.append(req)
            if len(catalog_calls) > 1:
                raise httpx.ConnectError("test network outage", request=req)
            return httpx.Response(200, json={"models": [{"slug": "m", "visibility": "list",
                "supported_reasoning_levels": [{"effort": "high"}]}]})
        posted.append(json.loads(req.content))
        return completed()
    client = ChatGPTPlanClient(Auth(), transport=httpx.MockTransport(handler))
    client.model_options()
    client._catalog_at -= 301
    for _ in range(3):
        with pytest.raises(LLMHTTPError, match="不支持所选推理强度"):
            client.create(model="m", messages=[{"role": "user", "content": "hi"}], reasoning_effort="ultra")
    assert len(catalog_calls) == 2 and not posted
    client.create(model="m", messages=[{"role": "user", "content": "hi"}], reasoning_effort="high")
    assert len(catalog_calls) == 2 and posted[0]["reasoning"] == {"effort": "high"}


def test_stale_effort_does_not_prevent_reading_existing_pdf(config, registry, pdfs, monkeypatch):
    from fastapi.testclient import TestClient
    from papermap import chatgpt_auth
    from papermap.web.app import create_app
    from .test_pipeline import make_pipeline

    pipe, _ = make_pipeline(config, registry)
    pipe.handle_path(pdfs["paper"])
    paper = registry.paper_by_doi(OWN_DOI)
    card = config.knowledge_dir / paper["kb_path"]
    before = card.read_bytes()
    config.llm.update(provider="chatgpt_plan", presets={"chatgpt_plan": {"model": "old-model", "reasoning_effort": "ultra"}})
    class ReadyAuth(Auth):
        def status(self):
            return {"ready": True, "client_id": "test-account"}
    monkeypatch.setattr(chatgpt_auth, "get_auth", lambda _: ReadyAuth())
    def no_inference(*_):
        raise AssertionError("Reading existing local documents must not fetch an LLM or model catalog")
    monkeypatch.setattr(llm_module, "_chatgpt_plan_client", no_inference)
    with TestClient(create_app(config, registry, pipe), base_url="http://127.0.0.1:8765") as web:
        assert web.get(f"/reader/{paper['id']}").status_code == 200
        assert web.get(f"/?id={paper['id']}").status_code == 200
        assert web.get("/settings").status_code == 200
    assert card.read_bytes() == before
