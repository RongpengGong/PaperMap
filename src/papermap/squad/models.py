"""Which model does each squad role use?

The roster lists every model the researcher can use: all subscriptions (also ones that are signed
in but not chosen as the main model) and every API or custom endpoint with a key. Each entry knows
its maker (vendor: Google, Anthropic, OpenAI, DeepSeek...), the channel it comes through, how it is
billed (subscription quota vs. paid per token) and its price.

Roles are sorted into three tiers. Complex roles (lead, critic, writer) should get the
highest-rated models; simple roles (searcher, screener) only need a model that can do the job, so
the cheapest one wins and subscription quota is kept for the hard work. Each role has an ordered
list of candidates: when one is not available (removed, signed out, quota used up) the next one
takes over, and the main model is the last resort.
"""
from __future__ import annotations

import copy
import re
import threading
import time
from typing import Any

from .. import chat
from ..config import PRESETS, PROVIDERS, SIGN_IN_PROVIDERS
from ..i18n import L

TIERS = {"complex": L("复杂"), "medium": L("中等"), "simple": L("简单")}
ROLE_TIERS = {"lead": "complex", "critic": "complex", "writer": "complex",
              "reader": "medium", "connector": "medium", "searcher": "simple", "screener": "simple"}
ROLE_ORDER = ["lead", "searcher", "screener", "reader", "connector", "critic", "writer"]
OPTIONAL = {"critic", "connector"}  # may be switched off per squad type
BUDGETS = {"economy": L("省钱"), "balanced": L("均衡"), "quality": L("质量优先")}
DEFAULTS = {"budget": "balanced", "parallel": 4, "default_cap_usd": 0.5, "max_calls": 200, "external": True,
            "handoff_quotes": True, "roles": {}, "disabled": {}}
CLI_PROVIDERS = {"claude_cli": 2, "antigravity_acp": 2}  # local CLI per call: at most this many at once
VENDORS = [(r"gemini|orchestrator", "Google"), (r"claude|opus|sonnet|haiku", "Anthropic"),
           (r"gpt|o\d|chatgpt", "OpenAI"), (r"deepseek", "DeepSeek"), (r"glm|zhipu", "智谱"),
           (r"kimi|moonshot", "Moonshot"), (r"qwen|dashscope", "阿里云"), (r"doubao|seed", "字节跳动"),
           (r"minimax|abab", "MiniMax"), (r"llama", "Meta"), (r"mistral", "Mistral")]
PROVIDER_VENDOR = {"anthropic": "Anthropic", "openai": "OpenAI", "gemini": "Google", "deepseek": "DeepSeek",
                   "moonshot": "Moonshot", "zhipu": "智谱", "dashscope": "阿里云", "chatgpt_plan": "OpenAI",
                   "claude_cli": "Anthropic", "minimax_mplan": "MiniMax"}
CACHE_SECONDS = 600
_cache: dict[str, Any] = {"at": 0.0, "probed": False, "choices": None}
_lock = threading.Lock()


def vendor_of(provider: str, model: str) -> str:
    """The maker of the model, not the channel: Claude through Antigravity is still Anthropic."""
    m = model.lower()
    for pattern, name in VENDORS:
        if re.search(pattern, m):
            return name
    return PROVIDER_VENDOR.get(provider, provider or "?")


def _channel(provider: str) -> str:
    if provider in PRESETS:
        return PRESETS[provider]["label"]
    if provider in PROVIDERS:
        return PROVIDERS[provider].get("label") or provider
    return {"custom": "自定义接口"}.get(provider, provider)


def choices(config, probe: bool = False) -> list[dict]:
    """Model choices including every signed-in subscription; cached because probing CLIs is slow.
    Without ``probe`` a cold cache gives the quick list (no CLI probing) and does not cache it."""
    with _lock:
        fresh = _cache["choices"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS
        if fresh and (_cache["probed"] or not probe):
            return _cache["choices"]
    if not probe:
        return chat.model_choices(config)
    found = chat.model_choices(config, probe_all=True)
    with _lock:
        _cache.update(at=time.monotonic(), probed=True, choices=found)
    return found


def forget() -> None:
    with _lock:
        _cache.update(at=0.0, probed=False, choices=None)


def _provider_of(key: str, config) -> tuple[str, str]:
    if key in ("", "main"):
        eff = config.effective_llm()
        return eff.get("provider", ""), eff.get("model", "")
    pid, _, model = key.partition("|")
    return pid, model


def roster(config, probe: bool = False) -> list[dict]:
    """Every usable model with maker, channel, billing and price."""
    out = []
    found = choices(config, probe)
    for c in found:
        if not c.get("ready"):
            continue
        pid, model = _provider_of(c["key"], config)
        try:
            eff = chat.resolve(config, c["key"], "", choices=found)
        except ValueError:
            continue
        out.append({"key": c["key"], "label": c["label"], "provider": pid, "model": model,
                    "channel": _channel(pid), "vendor": vendor_of(pid, model),
                    "billing": eff.get("billing", "token"),
                    "price_in": float(eff.get("price_input_per_1m") or 0),
                    "price_out": float(eff.get("price_output_per_1m") or 0),
                    "efforts": c.get("efforts") or [], "main": c["key"] == "main"})
    return out


# ------------------------------------------------------------------ settings
def settings(config) -> dict:
    raw = config.raw.get("squad") or {}
    out = copy.deepcopy(DEFAULTS)
    out.update({k: raw[k] for k in DEFAULTS if k in raw})
    if out["budget"] not in BUDGETS:
        out["budget"] = "balanced"
    roles = {}
    for role in ROLE_ORDER:
        r = (raw.get("roles") or {}).get(role) or {}
        cands = [c for c in (r.get("candidates") or []) if isinstance(c, dict) and isinstance(c.get("model"), str)]
        roles[role] = {"candidates": cands or [{"model": "main", "effort": ""}],
                       "extra": str(r.get("extra") or "")[:2000],
                       "max_steps": int(r["max_steps"]) if str(r.get("max_steps") or "").isdigit() else None}
    out["roles"] = roles
    out["disabled"] = {k: [x for x in v if x in OPTIONAL] for k, v in (raw.get("disabled") or {}).items()
                       if isinstance(v, list)}
    return out


def validate(config, body: dict) -> dict:
    """Clean a settings form; candidates must be models from the roster."""
    known = {m["key"]: m for m in roster(config)}
    out: dict[str, Any] = {}
    budget = body.get("budget", "balanced")
    out["budget"] = budget if budget in BUDGETS else "balanced"
    try:
        out["parallel"] = max(1, min(4, int(body.get("parallel", 4))))
        out["default_cap_usd"] = max(0.0, min(50.0, float(body.get("default_cap_usd", 0.5))))
        out["max_calls"] = max(10, min(1000, int(body.get("max_calls", 200))))
    except (TypeError, ValueError) as exc:
        raise ValueError("请检查数字设置。") from exc
    out["external"] = bool(body.get("external", True))
    out["handoff_quotes"] = bool(body.get("handoff_quotes", True))
    roles = {}
    for role in ROLE_ORDER:
        r = (body.get("roles") or {}).get(role) or {}
        cands = []
        for c in (r.get("candidates") or [])[:5]:
            key, effort = str(c.get("model") or ""), str(c.get("effort") or "")
            if key not in known:
                raise ValueError("有一个角色选了还没有配置好的模型，请重新检查订阅后再选。")
            if effort and effort not in known[key]["efforts"]:
                effort = ""
            if not any(x["model"] == key and x["effort"] == effort for x in cands):
                cands.append({"model": key, "effort": effort})
        steps = r.get("max_steps")
        roles[role] = {"candidates": cands or [{"model": "main", "effort": ""}],
                       "extra": str(r.get("extra") or "").strip()[:2000],
                       "max_steps": max(2, min(15, int(steps))) if str(steps or "").isdigit() else None}
    out["roles"] = roles
    out["disabled"] = {k: [x for x in v if x in OPTIONAL] for k, v in (body.get("disabled") or {}).items()
                       if isinstance(v, list)}
    return out


def recommend(config, budget: str | None = None) -> dict[str, list[dict]]:
    """A starting point without benchmark data: simple roles take the cheapest paid model (so the
    subscription quota is kept), complex roles the main model at high effort with a second model
    from another maker as backup; the critic prefers a maker other than the writer's."""
    budget = budget or settings(config)["budget"]
    models = roster(config)
    main = next((m for m in models if m["main"]), None)
    if main is None:
        return {}
    paid = sorted((m for m in models if m["billing"] != "subscription" and (m["price_in"] or m["price_out"])),
                  key=lambda m: m["price_in"] + m["price_out"])

    def eff(m, level):
        return level if level in m["efforts"] else ""

    def other_maker(vendor):
        subs = [m for m in models if m["vendor"] != vendor and not m["main"]]
        subs.sort(key=lambda m: (m["billing"] != "subscription", -(m["price_in"] + m["price_out"])))
        return subs[0] if subs else None

    out: dict[str, list[dict]] = {}
    cheap = paid[0] if paid else None
    for role in ROLE_ORDER:
        tier = ROLE_TIERS[role]
        if tier == "simple":
            first = cheap if (cheap and budget != "quality") else main
            out[role] = [{"model": first["key"], "effort": eff(first, "low")}]
        elif tier == "medium":
            first = cheap if (cheap and budget == "economy") else main
            out[role] = [{"model": first["key"], "effort": eff(first, "medium")}]
        else:
            out[role] = [{"model": main["key"], "effort": eff(main, "high")}]
        if out[role][0]["model"] != "main":
            out[role].append({"model": "main", "effort": ""})
    alt = other_maker(main["vendor"])
    if alt is not None:  # a second opinion from another maker
        out["critic"] = [{"model": alt["key"], "effort": eff(alt, "high")}, {"model": "main", "effort": eff(main, "high")}]
    return out


def same_maker_warning(config, s: dict | None = None) -> str:
    s = s or settings(config)
    known = {m["key"]: m for m in roster(config)}
    first = {r: (known.get(s["roles"][r]["candidates"][0]["model"]) or {}).get("vendor") for r in ("critic", "writer")}
    if first["critic"] and first["critic"] == first["writer"]:
        return "质疑员和执笔员是同一家（{vendor}）的模型，挑错时的第二意见会弱一些。"
    return ""


class RoleModels:
    """Per-run model choice for each role: first usable candidate, then the next, then the main model."""

    def __init__(self, config, s: dict):
        self.config = config
        self.s = s
        self.picked: dict[str, dict] = {}
        self.failed: set[str] = set()
        self._lock = threading.Lock()
        self._gates: dict[str, threading.Semaphore] = {p: threading.Semaphore(n) for p, n in CLI_PROVIDERS.items()}

    def _candidates(self, role: str) -> list[dict]:
        r = self.s["roles"].get(role) or {"candidates": []}
        out = list(r["candidates"])
        if not any(c["model"] == "main" for c in out):
            out.append({"model": "main", "effort": ""})
        return out

    def pick(self, role: str) -> dict:
        """{'key','eff','label','provider','fallback'}; 'main' with eff None means the default model."""
        with self._lock:
            if role in self.picked and self.picked[role]["key"] not in self.failed:
                return self.picked[role]
            found = None
            cands = self._candidates(role)
            for i, c in enumerate(cands):
                if c["model"] in self.failed:
                    continue
                if c["model"] == "main" and not c["effort"]:
                    found = {"key": "main", "eff": None, "label": "", "provider": self.config.effective_llm().get("provider", ""),
                             "fallback": i > 0}
                    break
                probe = c["model"].split("|", 1)[0] in SIGN_IN_PROVIDERS
                try:
                    eff = chat.resolve(self.config, c["model"], c["effort"], choices=choices(self.config, probe))
                except ValueError:
                    try:  # the model may have dropped that reasoning level: use its default instead
                        eff = chat.resolve(self.config, c["model"], "", choices=choices(self.config, probe))
                        c = {**c, "effort": ""}
                    except ValueError:
                        self.failed.add(c["model"])
                        continue
                label = next((x["label"] for x in choices(self.config) if x["key"] == c["model"]), c["model"])
                found = {"key": c["model"], "eff": eff, "label": label + (f" · {c['effort']}" if c["effort"] else ""),
                         "provider": eff.get("provider", ""), "fallback": i > 0}
                break
            if found is None:
                found = {"key": "main", "eff": None, "label": "", "provider": self.config.effective_llm().get("provider", ""),
                         "fallback": True}
            self.picked[role] = found
            return found

    def fail(self, key: str) -> None:
        with self._lock:
            if key != "main":
                self.failed.add(key)

    def gate(self, provider: str):
        return self._gates.get(provider) or _NoGate()


class _NoGate:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def vendor_channel(pick: dict, config) -> dict:
    """{label, billing} of the model a call actually used (for usage lines and scorecards)."""
    eff = pick["eff"] or config.effective_llm()
    return {"label": f"{_channel(eff.get('provider', ''))} · {eff.get('model', '')}", "billing": eff.get("billing", "token")}
