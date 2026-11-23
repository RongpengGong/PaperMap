"""NiuTrans Flash text translation using the signed v2 API."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable, Iterator

import httpx


ENDPOINT = "https://api.niutrans.com/v2/text/translate"
MAX_CHARS = 5000
_ERRORS = {
    "10001": "请求过于频繁，请稍后重试。",
    "10003": "原文字数超过小牛翻译的单次限制。",
    "13001": "请检查字符余额，以及是否已开通文本 API。",
    "13003": "服务商未接受这段文字。",
    "13007": "服务商暂不支持这个语言方向。",
    "13008": "服务商处理超时，请稍后重试。",
    "20001": "鉴权失败，请检查 App ID、API Key 和本机时间。",
    "20002": "服务商未接受请求参数。",
    "20003": "请求参数校验失败。",
}


class NiuTransError(RuntimeError):
    pass


def _segments(text: str) -> Iterator[tuple[str, str]]:
    """Preserve separators when a paragraph exceeds the service's length limit."""
    while len(text) > MAX_CHARS:
        prefix = text[:MAX_CHARS]
        breaks = list(re.finditer(r"\s+", prefix))
        if breaks:
            boundary = breaks[-1]
            yield prefix[:boundary.start()], boundary.group()
            text = text[boundary.end():]
        else:
            end = max(prefix.rfind(mark) for mark in "。！？.!?") + 1 or MAX_CHARS
            yield prefix[:end], ""
            text = text[end:]
    yield text, ""


def _translate_part(text: str, target: str, app_id: str, api_key: str, terms: Iterable[str],
                    client: httpx.Client) -> str:
    params = {"from": "auto", "to": target, "appId": app_id, "srcText": text,
              "timestamp": str(time.time_ns() // 1_000_000)}
    glossary = {term: term for term in terms if term and term in text}
    if glossary:
        specified = json.dumps(glossary, ensure_ascii=False, separators=(",", ":"))
        if len(specified) <= MAX_CHARS:
            params.update(isSpecifiedTerm="true", specifiedTerm=specified)
    signed = {**params, "apikey": api_key}
    canonical = "&".join(f"{name}={signed[name]}" for name in sorted(signed))
    params["authStr"] = hashlib.md5(canonical.encode("utf-8")).hexdigest()
    try:
        response = client.post(ENDPOINT, data=params, follow_redirects=False)
    except httpx.RequestError:
        raise NiuTransError("无法连接小牛翻译，请检查网络后重试。") from None
    try:
        result = response.json()
    except ValueError:
        result = None
    if isinstance(result, dict) and "errorCode" in result:
        code = str(result["errorCode"])
        if code == "13005" and response.is_success:
            return text
        if code in _ERRORS:
            raise NiuTransError(f"小牛翻译（{code}）：{_ERRORS[code]}")
        raise NiuTransError("小牛翻译未能处理这段文字。")
    if not response.is_success:
        raise NiuTransError(f"小牛翻译返回 HTTP {response.status_code}。")
    translated = result.get("tgtText") if isinstance(result, dict) else None
    if not isinstance(translated, str) or not translated.strip():
        raise NiuTransError("小牛翻译没有返回有效译文。")
    return translated


def translate(text: str, target: str, app_id: str, api_key: str, *, client: httpx.Client,
              terms: Iterable[str] = ()) -> str:
    """Translate raw source text without exposing the API key in the request."""
    if not text.strip():
        return text
    app_id, api_key = app_id.strip(), api_key.strip()
    if not app_id or not api_key:
        raise NiuTransError("请在翻译设置中填写小牛的 App ID 和 API Key。")
    glossary = tuple(terms)
    parts = []
    for part, separator in _segments(text):
        if part.strip():
            part = _translate_part(part, target, app_id, api_key, glossary, client)
        parts.extend((part, separator))
    return "".join(parts)
