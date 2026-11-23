import json

import httpx
import pytest

from papermap.llm import LLMHTTPError
from papermap.translation_models import tencent_translate, zhipu_translate


def _result(content, reason="stop"):
    return {"choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": reason}],
            "usage": {"prompt_tokens": 29, "completion_tokens": 8}}


def test_zhipu_uses_free_model_and_preserves_rendered_prompt():
    prompt = "请将以下论文文本翻译成中文。\n[1] Seurat was used."

    def handler(req):
        assert req.method == "POST"
        assert str(req.url) == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        assert req.headers["Authorization"] == "Bearer test-key"
        payload = json.loads(req.content)
        assert payload["model"] == "glm-4.7-flash"
        assert payload["messages"] == [{"role": "user", "content": prompt}]
        assert payload["thinking"] == {"type": "disabled"}
        return httpx.Response(200, json=_result("<think>推理</think>\n[1] 使用了 Seurat。"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert zhipu_translate(client, prompt, " test-key ") == ("[1] 使用了 Seurat。", 29, 8)


def test_tencent_sends_raw_text_and_ten_matching_terms():
    terms = [f"TERM{i:02d}" for i in range(14)]
    text = " ".join(terms)

    def handler(req):
        assert req.method == "POST"
        assert str(req.url) == "https://tokenhub.tencentmaas.com/v1/api/translations"
        assert req.headers["Authorization"] == "Bearer test-key"
        assert json.loads(req.content) == {
            "model": "hy-mt2-lite", "text": text, "target": "zh",
            "references": [{"type": "term", "text": t, "translation": t} for t in terms[:10]],
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": "中译文"}}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert tencent_translate(client, text, "zh", "test-key",
                                 ["", "absent", terms[0], *terms]) == ("中译文", 0, 0)


def test_tencent_long_segment_respects_multibyte_sentence_boundaries():
    first = "甲" * 900 + "。"
    rest = "乙" * 2900 + "。"
    original = first + "\n\n" + rest
    calls = []

    def handler(req):
        payload = json.loads(req.content)
        part = payload["text"]
        assert len(part.encode("utf-8")) <= 2800
        assert payload["references"] == [
            {"type": "term", "text": t, "translation": t} for t in ["甲", "乙"] if t in part
        ]
        calls.append(part)
        return httpx.Response(200, json=_result(part))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = tencent_translate(client, original, "en", "test-key", iter(["甲", "乙"]))
    assert calls[0] == first and len(calls) > 2
    assert result == (original, 29 * len(calls), 8 * len(calls))


def test_adjacent_chinese_sentences_are_separated_in_english():
    source = "甲" * 910 + "。" + "乙" * 90 + "。"
    responses = iter(["First sentence.", "Second sentence."])
    with httpx.Client(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json=_result(next(responses))))) as client:
        assert tencent_translate(client, source, "en", "key")[0] == "First sentence. Second sentence."


@pytest.mark.parametrize("response", [
    httpx.Response(401, json={"message": "private-token and private paper text"}),
    httpx.Response(403, json={"error": "private-token"}),
    httpx.Response(429, text="private paper text"),
    httpx.Response(302, headers={"location": "https://example.com/private-token"}),
    httpx.Response(200, text="invalid JSON: private-token"),
    httpx.Response(200, json={"error": {"message": "private-token"}}),
    httpx.Response(200, json={"choices": [{"message": {"reasoning_content": "private-token"}}]}),
    httpx.Response(200, json=_result(" ")),
    httpx.Response(200, json=_result("private-token", "sensitive")),
    httpx.Response(200, json=_result("partial private paper text", "length")),
    httpx.Response(200, json=_result("<think>private-token")),
])
def test_incomplete_outputs_and_provider_errors_are_safe(response):
    with httpx.Client(transport=httpx.MockTransport(lambda req: response)) as client:
        with pytest.raises(LLMHTTPError) as exc:
            zhipu_translate(client, "private paper text", "private-token")
    assert "private-token" not in str(exc.value)
    assert "private paper text" not in str(exc.value)


def test_transport_error_cannot_echo_key():
    def disconnect(req):
        raise httpx.ConnectError("private-token in lower-level error")

    with httpx.Client(transport=httpx.MockTransport(disconnect)) as client:
        with pytest.raises(LLMHTTPError) as exc:
            tencent_translate(client, "test", "zh", "private-token")
    assert "private-token" not in str(exc.value)


@pytest.mark.parametrize("key", ["", " ", "api\nkey", "中文"])
def test_invalid_key_detected_before_http(key):
    with httpx.Client(transport=httpx.MockTransport(lambda req: pytest.fail("unexpected call"))) as client:
        with pytest.raises(LLMHTTPError, match="API Key"):
            tencent_translate(client, "test", "zh", key)
