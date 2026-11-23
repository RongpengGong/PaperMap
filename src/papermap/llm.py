"""OpenAI-compatible LLM client: JSON output, retry, token/cost accounting."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

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
        return self.config.llm["model"]

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        key = self.config.api_key()
        if not key:
            env = self.config.llm.get("api_key_env") or "DEEPSEEK_API_KEY"
            raise LLMNotConfigured(
                f"No LLM API key configured. Set the {env} environment variable, "
                "or enter a key on the web Settings page (papermap serve)."
            )
        from openai import OpenAI

        self._client = OpenAI(
            api_key=key,
            base_url=self.config.llm["base_url"],
            timeout=float(self.config.llm.get("timeout_s", 180)),
            max_retries=3,  # SDK retries connection errors, 429 and 5xx with backoff
        )
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

    def _cost(self, usage: Any) -> tuple[int, int, float]:
        if usage is None:
            return 0, 0, 0.0
        pt = int(getattr(usage, "prompt_tokens", 0) or 0)
        ct = int(getattr(usage, "completion_tokens", 0) or 0)
        hit = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
        cfg = self.config.llm
        cost = (
            (pt - hit) * float(cfg.get("price_input_per_1m", 0))
            + hit * float(cfg.get("price_cache_hit_per_1m", 0))
            + ct * float(cfg.get("price_output_per_1m", 0))
        ) / 1e6
        return pt, ct, cost

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
            extra = self.config.llm.get("extra_body")
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
