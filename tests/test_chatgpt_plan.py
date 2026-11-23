import json

import httpx
import pytest

from papermap import llm as llm_module, qa
from papermap.chatgpt_plan import ChatGPTPlanClient
from papermap.llm import BudgetExceeded, LLMClient, LLMHTTPError
from papermap.pipeline import Pipeline

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
    assert paper["kb_path"].removesuffix(".md") in (config.knowledge_dir / answer["kb_path"]).read_text()
