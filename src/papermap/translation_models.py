"""Official model APIs used by the Zhipu and Tencent translation services."""
import re
from collections.abc import Iterable, Iterator

import httpx

from .llm import LLMHTTPError, strip_think

ZHIPU_MODEL = "glm-4.7-flash"
TENCENT_MODEL = "hy-mt2-lite"
_TENCENT_INPUT_BYTES = 2800  # Leave room in the 4K-token input for language and glossary.
_SENTENCE_END = re.compile(r"""(?:[。！？]+|[.!?]+(?=[”"')\]]*(?:\s|$)))[”"')\]]*""")


def _completion(http: httpx.Client, url: str, api_key: str, payload: dict) -> tuple[str, int, int]:
    key = (api_key or "").strip()
    if not key:
        raise LLMHTTPError(401, "请在翻译服务设置中填写 API Key。")
    if not all("!" <= char <= "~" for char in key):
        raise LLMHTTPError(401, "API Key 格式有误，请重新复制完整密钥。")

    try:
        response = http.post(url, headers={"Authorization": f"Bearer {key}"},
                             json=payload, follow_redirects=False)
    except httpx.RequestError:
        raise LLMHTTPError(0, "无法连接翻译服务，请检查网络后重试。") from None

    if not response.is_success:
        # A provider or proxy can echo credentials and paper text in its error body.
        message = {
            400: "翻译服务未接受请求，请检查输入长度。",
            401: "API Key 无效，请检查翻译服务设置。",
            402: "翻译额度不足，请检查服务账户。",
            403: "没有调用权限，请检查翻译服务的密钥和账户权限。",
            429: "请求受到限制，请检查账户额度或稍后重试。",
        }.get(response.status_code, "翻译服务请求失败。")
        raise LLMHTTPError(response.status_code, message)

    try:
        data = response.json()
        choice = data["choices"][0]
        reason = choice.get("finish_reason")
        if reason in ("sensitive", "content_filter"):
            raise LLMHTTPError(422, "翻译服务的内容审核中止了翻译。")
        if reason not in (None, "stop"):
            raise LLMHTTPError(422, "译文未完整返回，请尝试缩短原文。")
        content = choice["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise LLMHTTPError(502, "翻译服务没有返回有效译文。") from None

    usage = data.get("usage")

    def count(field: str) -> int:
        value = usage.get(field) if isinstance(usage, dict) else None
        return value if type(value) is int and value >= 0 else 0

    return content.strip(), count("prompt_tokens"), count("completion_tokens")


def zhipu_translate(http: httpx.Client, prompt: str, api_key: str) -> tuple[str, int, int]:
    content, input_tokens, output_tokens = _completion(
        http, "https://open.bigmodel.cn/api/paas/v4/chat/completions", api_key,
        {"model": ZHIPU_MODEL, "messages": [{"role": "user", "content": prompt}],
         "thinking": {"type": "disabled"}, "stream": False, "max_tokens": 16384},
    )
    translation = strip_think(content)
    if not translation or (content.startswith("<think>") and "</think>" not in content):
        raise LLMHTTPError(502, "智谱没有返回有效译文。")
    return translation, input_tokens, output_tokens


def _tencent_segments(text: str) -> Iterator[tuple[str, str]]:
    """Split at a sentence or word boundary and retain whitespace between fragments."""
    start = 0
    while start < len(text):
        window = text[start:start + _TENCENT_INPUT_BYTES].encode("utf-8")
        prefix = window[:_TENCENT_INPUT_BYTES].decode("utf-8", errors="ignore")
        if start + len(prefix) == len(text):
            yield prefix, ""
            return
        ends = [match.end() for match in _SENTENCE_END.finditer(prefix)]
        if not ends:
            ends = [match.start() for match in re.finditer(r"\s+", prefix) if match.start() > 0]
        end = start + (ends[-1] if ends else len(prefix))
        next_start = end
        while next_start < len(text) and text[next_start].isspace():
            next_start += 1
        yield text[start:end], text[end:next_start]
        start = next_start


def tencent_translate(http: httpx.Client, text: str, target: str, api_key: str,
                      terms: Iterable[str] = ()) -> tuple[str, int, int]:
    terms = tuple(dict.fromkeys(terms))
    parts = []
    total_input = total_output = 0
    for part, whitespace in _tencent_segments(text):
        if not part.strip():
            parts.append(part + whitespace)
            continue
        payload = {"model": TENCENT_MODEL, "text": part, "target": target}
        references = []
        reference_bytes = 0
        for term in terms:
            if term and term in part:
                size = 2 * len(term.encode("utf-8"))
                if reference_bytes + size <= 800:
                    references.append({"type": "term", "text": term, "translation": term})
                    reference_bytes += size
                    if len(references) == 10:
                        break
        if references:
            payload["references"] = references
        translation, used_input, used_output = _completion(
            http, "https://tokenhub.tencentmaas.com/v1/api/translations", api_key, payload,
        )
        # Chinese sentences can be adjacent in the original but need a space in English.
        if target == "en" and parts and parts[-1].endswith((".", "!", "?")):
            parts.append(" ")
        parts.append(translation + whitespace)
        total_input += used_input
        total_output += used_output
    return "".join(parts), total_input, total_output
