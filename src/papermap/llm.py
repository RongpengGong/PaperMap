"""OpenAI-compatible LLM client: JSON output, retry, token/cost accounting."""

from __future__ import annotations

import json
import logging
import re
import time
from types import SimpleNamespace
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from .config import Config
from .registry import Registry

log = logging.getLogger(__name__)


class LLMNotConfigured(Exception):
    """No API key: processing waits until the user sets one."""


class BudgetExceeded(Exception):
    """Monthly budget reached: LLM calls paused."""


class LLMError(Exception):
    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw


@dataclass
class LLMResult:
    data: dict
    raw: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


class LLMClient:
    def __init__(self, config: Config, registry: Registry, client: Any | None = None):
        self.config = config
        self.registry = registry
        self._client = client  # injectable for tests

    @property
    def model(self) -> str:
        return self.config.effective_llm()["model"]

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        eff = self.config.effective_llm()
        self._client = make_client(eff, float(self.config.llm.get("timeout_s", 180)))
        return self._client

    def check_budget(self) -> None:
        budget = float(self.config.llm.get("monthly_budget_usd") or 0)
        if budget > 0:
            spent = self.registry.month_cost()
            if spent >= budget:
                raise BudgetExceeded(
                    f"Monthly LLM budget reached (${spent:.2f} of ${budget:.2f}); "
                    "raise llm.monthly_budget_usd in Settings to continue."
                )

    def _cost(self, usage: Any, when=None) -> tuple[int, int, float]:
        if usage is None:
            return 0, 0, 0.0
        pt = int(getattr(usage, "prompt_tokens", 0) or 0)
        ct = int(getattr(usage, "completion_tokens", 0) or 0)
        hit = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
        if not hit:  # OpenAI-style usage: prompt_tokens_details.cached_tokens
            details = getattr(usage, "prompt_tokens_details", None)
            hit = int(getattr(details, "cached_tokens", 0) or 0) if details is not None else 0
        return pt, ct, compute_cost(self.config.effective_llm(), pt, ct, hit, when)

    def chat_text(self, system: str, user: str, purpose: str, paper_id: int | None = None,
                  max_tokens: int = 8192, eff: dict | None = None, client: Any = None) -> LLMResult:
        """Free-text answer (Markdown). ``eff``/``client`` select another endpoint (custom services)."""
        eff = eff or self.config.effective_llm()
        client = client or self._get_client()
        self.check_budget()
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
        kwargs: dict[str, Any] = {"model": eff["model"], "messages": messages, "max_tokens": max_tokens,
                                  "temperature": 0.3}
        if eff.get("extra_body"):
            kwargs["extra_body"] = eff["extra_body"]
        resp = client.chat.completions.create(**kwargs)
        raw = strip_think((resp.choices[0].message.content or "") if resp.choices else "")
        usage = getattr(resp, "usage", None)
        pt = int(getattr(usage, "prompt_tokens", 0) or 0)
        ct = int(getattr(usage, "completion_tokens", 0) or 0)
        hit = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
        cost = compute_cost(eff, pt, ct, hit)
        self.registry.record_llm_call(paper_id, purpose, getattr(resp, "model", eff["model"]) or eff["model"],
                                      pt, ct, cost, bool(raw))
        if not raw:
            raise LLMError("empty answer from the model")
        return LLMResult({}, raw, eff["model"], pt, ct, cost)

    def chat_json(
        self,
        system: str,
        user: str,
        validate: Callable[[dict], list[str]],
        purpose: str,
        paper_id: int | None = None,
        max_tokens: int = 4096,
    ) -> LLMResult:
        """Ask for a JSON object; on invalid JSON/schema, retry once with feedback."""
        client = self._get_client()
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_raw, last_err = "", ""
        for attempt in range(2):
            self.check_budget()
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "max_tokens": max_tokens,
                "temperature": 0.2,
            }
            extra = self.config.effective_llm().get("extra_body")
            if extra:
                kwargs["extra_body"] = extra
            resp = client.chat.completions.create(**kwargs)
            raw = (resp.choices[0].message.content or "") if resp.choices else ""
            pt, ct, cost = self._cost(getattr(resp, "usage", None))
            data, err = _parse_json(raw)
            if data is not None:
                problems = validate(data)
                if problems:
                    err = "; ".join(problems)
                    data = None
            self.registry.record_llm_call(
                paper_id, purpose, getattr(resp, "model", self.model) or self.model,
                pt, ct, cost, data is not None,
            )
            if data is not None:
                return LLMResult(data, raw, self.model, pt, ct, cost)
            last_raw, last_err = raw, err
            log.warning("LLM output invalid (attempt %d): %s", attempt + 1, err)
            messages = messages + [
                {"role": "assistant", "content": raw[:20000]},
                {
                    "role": "user",
                    "content": f"Your previous answer was invalid: {err}. "
                    "Reply again with ONLY one valid JSON object that follows the schema.",
                },
            ]
        raise LLMError(f"LLM returned invalid JSON twice: {last_err}", raw=last_raw)


def compute_cost(eff: dict[str, Any], prompt_tokens: int, completion_tokens: int,
                 cache_hit_tokens: int = 0, when=None) -> float:
    """USD cost of one call: (miss*in + hit*cache + out*out) * multiplier * peak(2x)."""
    from .config import is_peak

    hit = min(cache_hit_tokens, prompt_tokens)
    base = (
        (prompt_tokens - hit) * float(eff.get("price_input_per_1m") or 0)
        + hit * float(eff.get("price_cache_hit_per_1m") or 0)
        + completion_tokens * float(eff.get("price_output_per_1m") or 0)
    ) / 1e6
    factor = float(eff.get("price_multiplier") or 1.0)
    if is_peak(eff.get("peak_schedule", "none"), when):
        factor *= 2
    return base * factor


# --------------------------------------------------------------------- clients
class LLMHTTPError(Exception):
    """HTTP error from a provider reached without the OpenAI SDK."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code


def make_client(eff: dict[str, Any], timeout_s: float = 180) -> Any:
    """Build a client with an OpenAI-style ``chat.completions.create`` interface."""
    missing = [label for key, label in (("base_url", "Base URL"), ("model", "model name")) if not eff.get(key)]
    if not eff.get("api_key"):
        hint = f" (or set the {eff['api_key_env']} environment variable)" if eff.get("api_key_env") else ""
        raise LLMNotConfigured(
            f"No LLM API key configured. Enter it on the web Settings page (papermap serve){hint}."
        )
    if missing:
        raise LLMNotConfigured(f"LLM settings incomplete: missing {', '.join(missing)} (Settings page).")
    proto = eff["protocol"]
    if proto in HTTP_CLIENTS:
        return HTTP_CLIENTS[proto](eff["base_url"], eff["api_key"], timeout_s)
    from openai import OpenAI

    return OpenAI(api_key=eff["api_key"], base_url=eff["base_url"], timeout=timeout_s, max_retries=3)


def _versioned(base: str, path: str) -> str:
    """Join base + path, tolerating a base that already ends in /v1."""
    base = base.rstrip("/")
    if base.endswith("/v1") and path.startswith("/v1/"):
        path = path[3:]
    return base + path


class _HTTPClient:
    """Shared retry loop; subclasses build the request and parse the reply."""

    def __init__(self, base_url: str, api_key: str, timeout_s: float,
                 transport: httpx.BaseTransport | None = None):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.http = httpx.Client(timeout=timeout_s, transport=transport)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def request(self, model: str, system: str, turns: list[dict], max_tokens: int,
                temperature: float | None, json_mode: bool, extra: dict) -> tuple[str, dict, dict]:
        raise NotImplementedError  # -> (url, headers, body)

    def parse(self, data: dict) -> tuple[str, int, int, int]:
        raise NotImplementedError  # -> (text, prompt_tokens, completion_tokens, cache_hit_tokens)

    def create(self, model: str, messages: list[dict], max_tokens: int = 4096,
               temperature: float | None = None, extra_body: dict | None = None,
               response_format: dict | None = None, **_: Any) -> Any:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = [m for m in messages if m["role"] != "system"]
        json_mode = bool(response_format and response_format.get("type") == "json_object")
        url, headers, body = self.request(model, system, turns, max_tokens, temperature, json_mode, extra_body or {})
        delay = 2.0
        for attempt in range(4):
            try:
                resp = self.http.post(url, headers=headers, json=body)
            except httpx.HTTPError as exc:
                if attempt == 3:
                    raise LLMHTTPError(0, str(exc)) from exc
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code in (429, 500, 502, 503, 504, 529) and attempt < 3:
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code >= 400:
                raise LLMHTTPError(resp.status_code, _error_text(resp))
            data = resp.json()
            text, pt, ct, hit = self.parse(data)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
                usage=SimpleNamespace(prompt_tokens=pt, completion_tokens=ct, prompt_cache_hit_tokens=hit),
                model=data.get("model") or data.get("modelVersion") or model,
            )
        raise LLMHTTPError(0, "retries exhausted")


class _AnthropicClient(_HTTPClient):
    """Anthropic Messages API (POST /v1/messages)."""

    def request(self, model, system, turns, max_tokens, temperature, json_mode, extra):
        body: dict[str, Any] = {"model": model, "max_tokens": max_tokens,
                                "messages": [{"role": m["role"], "content": m["content"]} for m in turns]}
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature
        body.update(extra)
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        return _versioned(self.base, "/v1/messages"), headers, body

    def parse(self, data):
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        u = data.get("usage") or {}
        hit = int(u.get("cache_read_input_tokens") or 0)
        return text, int(u.get("input_tokens") or 0) + hit, int(u.get("output_tokens") or 0), hit


class _ResponsesClient(_HTTPClient):
    """OpenAI Responses API (POST /responses)."""

    def request(self, model, system, turns, max_tokens, temperature, json_mode, extra):
        body: dict[str, Any] = {"model": model, "max_output_tokens": max_tokens,
                                "input": [{"role": m["role"], "content": m["content"]} for m in turns]}
        if system:
            body["instructions"] = system
        if temperature is not None:
            body["temperature"] = temperature
        if json_mode:
            body["text"] = {"format": {"type": "json_object"}}
        body.update(extra)
        return self.base + "/responses", {"Authorization": f"Bearer {self.api_key}"}, body

    def parse(self, data):
        if isinstance(data.get("output_text"), str):
            text = data["output_text"]
        else:
            text = "".join(
                c.get("text", "")
                for item in data.get("output", []) if item.get("type") == "message"
                for c in item.get("content", []) if c.get("type") == "output_text"
            )
        u = data.get("usage") or {}
        hit = int((u.get("input_tokens_details") or {}).get("cached_tokens") or 0)
        return text, int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0), hit


class _GeminiClient(_HTTPClient):
    """Google Gemini API (POST /models/{model}:generateContent)."""

    def request(self, model, system, turns, max_tokens, temperature, json_mode, extra):
        gen: dict[str, Any] = {"maxOutputTokens": max_tokens}
        if temperature is not None:
            gen["temperature"] = temperature
        if json_mode:
            gen["responseMimeType"] = "application/json"
        body: dict[str, Any] = {
            "contents": [{"role": "model" if m["role"] == "assistant" else "user",
                          "parts": [{"text": m["content"]}]} for m in turns],
            "generationConfig": gen,
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        body.update(extra)
        name = model if model.startswith("models/") else f"models/{model}"
        return f"{self.base}/{name}:generateContent", {"x-goog-api-key": self.api_key}, body

    def parse(self, data):
        cands = data.get("candidates") or []
        parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        u = data.get("usageMetadata") or {}
        return (text, int(u.get("promptTokenCount") or 0),
                int(u.get("candidatesTokenCount") or 0) + int(u.get("thoughtsTokenCount") or 0),
                int(u.get("cachedContentTokenCount") or 0))


HTTP_CLIENTS: dict[str, type[_HTTPClient]] = {
    "anthropic": _AnthropicClient,
    "responses": _ResponsesClient,
    "gemini": _GeminiClient,
}


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or err)
        return str(err or resp.text[:300])
    except ValueError:
        return resp.text[:300]


def list_models(eff: dict[str, Any], timeout_s: float = 20) -> list[str]:
    """Ask the endpoint which models it offers."""
    if not eff.get("base_url") or not eff.get("api_key"):
        raise LLMNotConfigured("Base URL and API key are needed to list models")
    base = eff["base_url"].rstrip("/")
    proto = eff["protocol"]
    if proto == "anthropic":
        url, headers = _versioned(base, "/v1/models"), {"x-api-key": eff["api_key"], "anthropic-version": "2023-06-01"}
    elif proto == "gemini":
        url, headers = base + "/models", {"x-goog-api-key": eff["api_key"]}
    else:
        url, headers = base + "/models", {"Authorization": f"Bearer {eff['api_key']}"}
    resp = httpx.get(url, headers=headers, timeout=timeout_s, follow_redirects=True)
    if resp.status_code >= 400:
        raise LLMHTTPError(resp.status_code, _error_text(resp))
    body = resp.json()
    if proto == "gemini":
        return sorted({m["name"].removeprefix("models/") for m in body.get("models", []) if m.get("name")})
    return sorted({m.get("id") for m in body.get("data", []) if m.get("id")})


def check_connection(eff: dict[str, Any], timeout_s: float = 60) -> dict[str, Any]:
    """Send a tiny request; returns {ok, message, latency_ms, tokens}."""
    t0 = time.monotonic()
    try:
        client = make_client(eff, timeout_s)
        kwargs: dict[str, Any] = {
            "model": eff["model"], "max_tokens": 16,
            "messages": [{"role": "user", "content": "Reply with the single word: OK"}],
        }
        if eff.get("extra_body"):
            kwargs["extra_body"] = eff["extra_body"]
        resp = client.chat.completions.create(**kwargs)
        text = (resp.choices[0].message.content or "").strip()
        usage = getattr(resp, "usage", None)
        tokens = (getattr(usage, "prompt_tokens", 0) or 0) + (getattr(usage, "completion_tokens", 0) or 0)
        return {"ok": True, "message": f"连接成功，模型回复：{text[:40] or '(空)'}",
                "latency_ms": int((time.monotonic() - t0) * 1000), "tokens": tokens}
    except LLMNotConfigured:
        if not eff.get("api_key"):
            return {"ok": False, "message": "请先填写 API Key"}
        return {"ok": False, "message": "请填写 Base URL 和模型名"}
    except Exception as exc:  # noqa: BLE001 - report anything to the user
        status = getattr(exc, "status_code", None)
        hint = {401: "API Key 无效", 402: "账户余额不足", 403: "无权限", 404: "地址或模型名不对",
                400: "请求被拒绝（多半是模型名不对）", 429: "请求太频繁/限流"}.get(status or 0, "")
        return {"ok": False, "message": f"{hint + '：' if hint else ''}{type(exc).__name__}: {str(exc)[:300]}"}


def replace_ref_ids(text: str, labels: dict[str, str]) -> str:
    """'... 51 samples (P1) vs 13 (P2, P3)' -> '... (Cai 2026) vs (Kalfakakou 2026, de Jong 2026)'."""
    def sub(m: re.Match) -> str:
        return labels.get(m.group(0), m.group(0))

    return re.sub(r"\bP\d{1,3}\b", sub, text)


def strip_think(text: str) -> str:
    """Drop chain-of-thought blocks some models prepend."""
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.strip().removeprefix("<think>").strip()


def _parse_json(raw: str) -> tuple[dict | None, str]:
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON ({exc.msg} at char {exc.pos})"
    if not isinstance(data, dict):
        return None, "top-level JSON value must be an object"
    return data, ""
