import json
from urllib.parse import parse_qsl

import httpx
import pytest

from papermap import niutrans


def form(request):
    return dict(parse_qsl(request.content.decode()))


def test_v2_signs_utf8_fields_and_keeps_key_off_the_wire(monkeypatch):
    monkeypatch.setattr(niutrans.time, "time_ns", lambda: 1760000000123000000)

    def handler(request):
        assert request.method == "POST" and str(request.url) == niutrans.ENDPOINT
        assert b"niutrans-test-key" not in request.content
        assert form(request) == {
            "appId": "demo-app", "from": "auto", "to": "zh", "srcText": "R&D α+β",
            "timestamp": "1760000000123", "authStr": "43eb0c06d84a625ea6a6e5560927b641",
        }
        return httpx.Response(200, json={"tgtText": "研发 α+β"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert niutrans.translate("R&D α+β", "zh", "demo-app", "niutrans-test-key",
                                  client=client) == "研发 α+β"


def test_long_text_preserves_every_character_and_applies_relevant_glossary():
    text = "Xenium " + "甲" * 4992 + "。中文" + "乙" * 5100
    seen = []

    def handler(request):
        params = form(request)
        assert 0 < len(params["srcText"]) <= 5000
        if "Xenium" in params["srcText"]:
            assert params["isSpecifiedTerm"] == "true"
            assert json.loads(params["specifiedTerm"]) == {"Xenium": "Xenium"}
        else:
            assert "specifiedTerm" not in params
        seen.append(params["srcText"])
        return httpx.Response(200, json={"errorCode": "13005"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert niutrans.translate(text, "zh", "app", "key", terms=["Xenium", "Seurat"], client=client) == text
    assert len(seen) >= 3


@pytest.mark.parametrize(("status", "body", "message"), [
    (200, {"errorCode": "13001", "errorMsg": "test-secret"}, "余额"),
    (403, {"errorCode": "20001", "errorMsg": "test-secret"}, "鉴权失败"),
    (200, {"errorCode": "test-secret", "errorMsg": "test-secret"}, "未能处理"),
    (200, {"tgtText": ""}, "有效译文"),
    (200, ["test-secret"], "有效译文"),
    (502, None, "HTTP 502"),
])
def test_response_errors_are_actionable_without_echoing_payloads(status, body, message):
    def handler(request):
        if body is None:
            return httpx.Response(status, text="test-secret")
        return httpx.Response(status, json=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(niutrans.NiuTransError, match=message) as error:
            niutrans.translate("text", "en", "app", "test-secret", client=client)
        assert "test-secret" not in str(error.value)


def test_empty_text_credentials_and_network_errors():
    def handler(request):
        raise httpx.ConnectError("test-secret")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert niutrans.translate(" ", "en", "", "", client=client) == " "
        with pytest.raises(niutrans.NiuTransError, match="App ID"):
            niutrans.translate("text", "zh", " ", "key", client=client)
        with pytest.raises(niutrans.NiuTransError, match="无法连接") as error:
            niutrans.translate("text", "zh", "app", "test-secret", client=client)
        assert "test-secret" not in str(error.value)
