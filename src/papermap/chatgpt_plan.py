"""Use the public Responses API with a user-authorized ChatGPT plan."""
from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import httpx

from .chatgpt_auth import ChatGPTAuthError
from .llm import BudgetExceeded, LLMHTTPError, LLMNotConfigured, _ResponsesClient

API = "https://api.openai.com/v1"
LIMIT_ERRORS = {"subscription_sharing_usage_limit_exceeded", "subscription_sharing_usage_unavailable"}


def _events(lines: Iterator[str]) -> Iterator[dict]:
    data: list[str] = []
    for line in lines:
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif not line and data:
            payload = "\n".join(data)
            data.clear()
            if payload == "[DONE]":
                return
            try:
                event = json.loads(payload)
            except ValueError as exc:
                raise LLMHTTPError(0, "ChatGPT 返回了无法解析的响应流，请重试。") from exc
            if isinstance(event, dict):
                yield event


def _raise_error(status: int, error: object) -> None:
    # Do not persist arbitrary server responses (which can contain request data).
    code = str(error.get("code") or "") if isinstance(error, dict) else ""
    if code in LIMIT_ERRORS or status == 429:
        raise BudgetExceeded("ChatGPT 套餐额度暂不可用或已达到限制，请检查套餐使用情况后重试。")
    if status in (401, 403):
        raise LLMNotConfigured("ChatGPT 授权或模型权限不足，请检查套餐授权并重新选择可用模型。")
    raise LLMHTTPError(status, f"ChatGPT 请求失败（{code or status}）。")


class ChatGPTPlanClient:
    def __init__(self, auth, timeout_s: float = 180, transport=None):
        self.auth = auth
        self.timeout_s = timeout_s
        self.transport = transport
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    @contextmanager
    def _request(self, method: str, path: str, body=None):
        """Yield a single authorized response, renewing a rejected token once."""
        rejected = None
        try:
            with httpx.Client(transport=self.transport, timeout=self.timeout_s) as client:
                for attempt in range(2):
                    token = self.auth.access_token(rejected_token=rejected)
                    headers = {"Authorization": f"Bearer {token}"}
                    with client.stream(method, API + path, headers=headers, json=body) as response:
                        if response.status_code == 401 and attempt == 0:
                            rejected = token
                            continue
                        if response.status_code != 200:
                            response.read()
                            try:
                                error = response.json().get("error", {})
                            except (ValueError, AttributeError):
                                error = {}
                            _raise_error(response.status_code, error if isinstance(error, dict) else {})
                        yield response
                        return
        except ChatGPTAuthError as exc:
            raise LLMNotConfigured(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise LLMHTTPError(0, "无法连接 ChatGPT，或响应流已中断。") from exc

    def model_ids(self) -> list[str]:
        with self._request("GET", "/models") as response:
            response.read()
            return [m["slug"] for m in response.json().get("models", [])
                    if m.get("visibility") == "list" and m.get("slug")]

    def create(self, *, model: str, messages: list[dict], **_):
        if not model:
            raise LLMNotConfigured("请在设置中获取并选择 ChatGPT 模型。")
        instructions = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = [dict(role=m["role"], content=_ResponsesClient.input_content(m["content"]))
                 for m in messages if m["role"] != "system"]
        body = {"model": model, "input": turns, "store": False, "stream": True}
        if instructions:
            body["instructions"] = instructions
        parts = []
        with self._request("POST", "/responses", body) as response:
            for event in _events(response.iter_lines()):
                kind = event.get("type")
                if kind == "response.output_text.delta":
                    parts.append(event.get("delta", ""))
                elif kind == "response.completed":
                    result = event.get("response") or {}
                    if result.get("status") not in (None, "completed"):
                        _raise_error(400, result.get("error") or {})
                    content, pt, ct, hit = _ResponsesClient.parse(result)
                    text = content or "".join(parts)
                    if not text.strip():
                        raise LLMHTTPError(422, "ChatGPT 没有返回可用文本。")
                    return SimpleNamespace(
                        model=result.get("model") or model,
                        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
                        usage=SimpleNamespace(prompt_tokens=pt, completion_tokens=ct, prompt_cache_hit_tokens=hit),
                    )
                elif kind in ("response.failed", "response.incomplete", "error"):
                    error = (event.get("response") or {}).get("error") or event.get("error") or event
                    _raise_error(400, error)
        raise LLMHTTPError(0, "ChatGPT 响应在完成前中断，部分内容未保存。")
