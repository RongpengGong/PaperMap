"""Use the public Responses API with a user-authorized ChatGPT plan."""
from __future__ import annotations

import json
import copy
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import httpx

from .chatgpt_auth import ChatGPTAuthError
from .config import ConfigError, normalize_reasoning_effort
from .llm import BudgetExceeded, LLMHTTPError, LLMNotConfigured, _ResponsesClient

API = "https://api.openai.com/v1"
LIMIT_ERRORS = {"subscription_sharing_usage_limit_exceeded", "subscription_sharing_usage_unavailable"}
_EFFORT_KEYS = ("supported_reasoning_levels", "supported_reasoning_efforts", "supportedReasoningEfforts", "reasoning_efforts")
_DEFAULT_EFFORT_KEYS = ("default_reasoning_level", "default_reasoning_effort", "defaultReasoningEffort")


def _catalog_effort(value: object) -> str:
    try:
        return normalize_reasoning_effort(value)
    except ConfigError:
        return ""


def model_option(row: dict) -> dict:
    """Normalize official model-catalog metadata without guessing capabilities.

    Codex's ModelInfo uses supported_reasoning_levels[{effort,...}]. The
    app-server representation uses supportedReasoningEfforts[{reasoningEffort}].
    Future provider-defined effort strings must survive unchanged.
    """
    raw = next((row[k] for k in _EFFORT_KEYS if k in row and row[k] is not None), None)
    efforts = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                item = item.get("effort", item.get("reasoning_effort", item.get("reasoningEffort")))
            effort = _catalog_effort(item)
            if effort and effort not in efforts:
                efforts.append(effort)
    default = next((_catalog_effort(row[k]) for k in _DEFAULT_EFFORT_KEYS if row.get(k) is not None), "")
    label = row.get("display_name") or row.get("displayName")
    return {
        "id": row["slug"], "label": label if isinstance(label, str) and label else row["slug"],
        "reasoning_efforts": efforts, "default_reasoning_effort": default,
        "reasoning_efforts_known": isinstance(raw, list) and (not raw or bool(efforts)),
    }


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
        self._catalog: list[dict] | None = None
        self._catalog_at = 0.0
        self._catalog_account = None
        self._catalog_retry_at = 0.0
        self._catalog_failed_account = None
        self._catalog_lock = threading.RLock()

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

    def _account_id(self):
        status = getattr(self.auth, "status", None)
        return status().get("client_id") if callable(status) else None

    def model_options(self, *, force_refresh: bool = False) -> list[dict]:
        account = self._account_id()
        with self._catalog_lock:
            if (not force_refresh and self._catalog is not None and account == self._catalog_account
                    and time.monotonic() - self._catalog_at < 300):
                return copy.deepcopy(self._catalog)
            with self._request("GET", "/models") as response:
                response.read()
                try:
                    rows = response.json().get("models", [])
                except (ValueError, AttributeError) as exc:
                    raise LLMHTTPError(0, "ChatGPT 模型目录无法解析，请重新获取。") from exc
                if not isinstance(rows, list):
                    raise LLMHTTPError(0, "ChatGPT 模型目录格式不正确，请重新获取。")
                options, seen = [], set()
                for row in rows:
                    if (not isinstance(row, dict) or row.get("visibility") != "list"
                            or not isinstance(row.get("slug"), str) or not row["slug"] or row["slug"] in seen):
                        continue
                    seen.add(row["slug"])
                    options.append(model_option(row))
            self._catalog = options
            self._catalog_at, self._catalog_account = time.monotonic(), account
            self._catalog_retry_at = 0.0
            return copy.deepcopy(options)

    def model_ids(self) -> list[str]:
        return [row["id"] for row in self.model_options()]

    def _reasoning_effort(self, model: str, value: object) -> str:
        try:
            effort = normalize_reasoning_effort(value)
        except ConfigError as exc:
            raise LLMHTTPError(400, "ChatGPT 推理强度格式无效，请重新选择。") from exc
        if not effort:
            return ""  # Old settings and model-default selection never add a field.
        account = self._account_id()
        with self._catalog_lock:
            # An unavailable model-list endpoint must not be requested again
            # for every paragraph, paper or JSON-repair attempt in a batch.
            if account == self._catalog_failed_account and time.monotonic() < self._catalog_retry_at:
                options = self._catalog if self._catalog_account == account else []
            else:
                try:
                    options = self.model_options()
                except LLMHTTPError as exc:
                    if exc.status_code not in (0, 500, 502, 503, 504):
                        raise
                    self._catalog_failed_account = account
                    self._catalog_retry_at = time.monotonic() + 30
                    # Retain known restrictions for this account when a cached
                    # listing has expired. With no capability metadata, preserve
                    # the exact explicit value for the inference server to check.
                    options = self._catalog if self._catalog_account == account else []
            options = options or []
        option = next((row for row in options if row["id"] == model), None)
        if option and option["reasoning_efforts_known"] and effort not in option["reasoning_efforts"]:
            raise LLMHTTPError(400, "该 ChatGPT 模型不支持所选推理强度，请获取模型列表后重新选择。")
        return effort

    def create(self, *, model: str, messages: list[dict], reasoning_effort: str | None = None, **_):
        if not model:
            raise LLMNotConfigured("请在设置中获取并选择 ChatGPT 模型。")
        instructions = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = [dict(role=m["role"], content=_ResponsesClient.input_content(m["content"]))
                 for m in messages if m["role"] != "system"]
        body = {"model": model, "input": turns, "store": False, "stream": True}
        effort = self._reasoning_effort(model, reasoning_effort)
        if effort:
            body["reasoning"] = {"effort": effort}
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
