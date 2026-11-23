"""Configuration loading and validation."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_DATA_DIR = "~/PaperMap"
CONFIG_ENV = "PAPERMAP_CONFIG"

PROTOCOLS = {
    "openai": "OpenAI Chat Completions",
    "responses": "OpenAI Responses",
    "anthropic": "Anthropic Messages",
    "gemini": "Gemini API",
}

# Official provider presets. Values follow the provider's official docs
# (DeepSeek: api-docs.deepseek.com, checked 2026-11). Prices are USD per 1M
# tokens at the off-peak rate; peak hours cost 2x. Used only for the local
# cost estimate / monthly budget.
PROVIDERS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "label": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com",
        "protocol": "openai",
        "api_key_env": "DEEPSEEK_API_KEY",
        "key_url": "https://platform.deepseek.com/api_keys",
        "docs_url": "https://api-docs.deepseek.com/quick_start/pricing",
        "peak_schedule": "deepseek",  # off-peak prices below, doubled at peak hours
        # Thinking mode is on by default; structured extraction does not need it.
        "extra_body": {"thinking": {"type": "disabled"}},
        "models": {
            "deepseek-flash": {"label": "deepseek-flash（快、便宜，推荐）", "in": 0.15, "hit": 0.003, "out": 0.6},
            "deepseek-v4-pro": {"label": "deepseek-v4-pro（更强、更贵）", "in": 0.66, "hit": 0.022, "out": 1.98},
        },
        "default_model": "deepseek-flash",
    },
}


# One-click presets for other providers (checked against each provider's docs,
# 2026-11-23). "subscription" = flat-rate plan whose API key may be used by
# third-party apps; calls are logged with tokens but cost $0. Coding plans that
# forbid use outside their listed coding tools (GLM Coding Plan, Alibaba Cloud
# Coding Plan) or point app integrations to the pay-as-you-go platform (Kimi
# Code) are offered through those providers' pay-as-you-go APIs instead.
# Browser sign-in providers use their own authorized client, not a stored API key.
# Prices: USD per 1M tokens; an empty models dict means "type or fetch the
# model name, fill in the price yourself".
PRESETS: dict[str, dict[str, Any]] = {
    "chatgpt_plan": {
        "label": "ChatGPT 订阅", "group": "subscription",
        "base_url": "https://api.openai.com/v1", "protocol": "responses",
        "docs_url": "https://developers.openai.com/siwc/token-sharing-open-source",
        "models": {}, "default_model": "", "json_mode": False, "no_temperature": True,
        "note": "在系统浏览器授权 PaperMap 使用套餐，再获取账号可用的模型。",
    },
    "claude_cli": {
        "label": "Claude 订阅（Claude Code）", "group": "subscription",
        "base_url": "", "protocol": "claude_cli",
        "docs_url": "https://code.claude.com/docs/en/authentication",
        "models": {}, "default_model": "sonnet", "json_mode": False, "no_temperature": True,
        "note": "使用本机官方 Claude Code 登录的订阅账号。实际用量以 Claude 账号记录为准。",
    },
    "minimax_mplan": {
        "label": "MiniMax M Plan", "group": "subscription",
        "base_url": "https://api.minimax.cn/v1", "protocol": "openai",
        "key_url": "https://platform.minimax.cn/console/plan",
        "docs_url": "https://platform.minimaxi.com/docs/m-plan/other-tools",
        "models": {"MiniMax-M3.1-Flash": {"label": "MiniMax-M3.1-Flash"}},
        "default_model": "MiniMax-M3.1-Flash",
        "json_mode": False, "min_max_tokens": 16000,
        "note": "包月订阅（Go / Explore / Build），有 5 小时和每周额度。模型总是先思考，回复会慢一些。",
    },
    "ollama_cloud": {
        "label": "Ollama Cloud", "group": "subscription",
        "base_url": "https://ollama.com/v1", "protocol": "openai",
        "key_url": "https://ollama.com/settings/keys",
        "docs_url": "https://docs.ollama.com/cloud",
        "models": {}, "default_model": "gemma4:31b",
        "json_mode": False,  # cloud models do not support structured outputs
        "note": "免费额度 + 付费订阅 / 额度包。模型名用「获取模型列表」选，例如 gemma4:31b。",
    },
    "anthropic": {
        "label": "Anthropic Claude API", "group": "api",
        "base_url": "https://api.anthropic.com", "protocol": "anthropic",
        "key_url": "https://platform.claude.com/settings/keys",
        "docs_url": "https://platform.claude.com/docs/en/about-claude/pricing",
        "models": {
            "claude-opus-5-5": {"label": "claude-opus-5-5（最强）", "in": 4.0, "hit": 0.2, "out": 20.0},
            "claude-sonnet-5-5": {"label": "claude-sonnet-5-5（均衡）", "in": 2.0, "hit": 0.1, "out": 10.0},
            "claude-haiku-5-5": {"label": "claude-haiku-5-5（便宜、快）", "in": 0.1, "hit": 0.01, "out": 0.5},
        },
        "default_model": "claude-opus-5-5",
        "no_temperature": True,  # current models reject sampling parameters
        "min_max_tokens": 16000,
        "note": "按量付费的 API Key（不是 Claude Pro / Max 订阅）。",
    },
    "openai": {
        "label": "OpenAI API", "group": "api",
        "base_url": "https://api.openai.com/v1", "protocol": "responses",
        "key_url": "https://platform.openai.com/api-keys",
        "docs_url": "https://openai.com/api/pricing/",
        "models": {}, "default_model": "",
        "note": "按量付费的 API Key（不是 ChatGPT Plus / Pro 订阅）。",
    },
    "gemini": {
        "label": "Google Gemini API", "group": "api",
        "base_url": "https://generativelanguage.googleapis.com/v1beta", "protocol": "gemini",
        "key_url": "https://aistudio.google.com/apikey",
        "docs_url": "https://ai.google.dev/gemini-api/docs/pricing",
        "models": {}, "default_model": "",
        "note": "Google AI Studio 的 API Key，有免费层级。",
    },
    "moonshot": {
        "label": "Kimi 开放平台", "group": "api",
        "base_url": "https://api.moonshot.cn/v1", "protocol": "openai",
        "key_url": "https://platform.kimi.com/console/api-keys",
        "docs_url": "https://platform.kimi.com/docs/pricing/chat",
        "models": {}, "default_model": "",
        "note": "按量付费。Kimi Code 会员 Key 官方只用于编程工具，第三方应用请用开放平台。",
    },
    "zhipu": {
        "label": "智谱开放平台（GLM）", "group": "api",
        "base_url": "https://open.bigmodel.cn/api/paas/v4", "protocol": "openai",
        "key_url": "https://bigmodel.cn/usercenter/proj-mgmt/apikeys",
        "docs_url": "https://bigmodel.cn/pricing",
        "models": {}, "default_model": "",
        "note": "按量付费。GLM Coding Plan 只在官方指定的编程工具里抵扣套餐，在 PaperMap 里调用会扣账户余额。",
    },
    "dashscope": {
        "label": "阿里云百炼（通义千问）", "group": "api",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "protocol": "openai",
        "key_url": "https://bailian.console.aliyun.com/?tab=model#/api-key",
        "docs_url": "https://help.aliyun.com/zh/model-studio/models",
        "models": {}, "default_model": "",
        "note": "按量付费的 sk- Key。百炼 Coding Plan（sk-sp-）明确禁止用于自定义应用，用了可能被封。",
    },
}


def preset_models(pid: str) -> dict[str, dict[str, Any]]:
    return (PRESETS.get(pid) or {}).get("models") or {}


# Peak-hour schedules: (weekday Mon-Fri, UTC hour ranges) where prices double.
# DeepSeek: 01:00-04:00 and 06:00-10:00 UTC on weekdays (= Beijing 09-12, 14-18).
# Chinese public holidays are off-peak per DeepSeek, but are not modelled here,
# so estimates on those days err on the high side.
PEAK_SCHEDULES = {"none": [], "deepseek": [(1, 4), (6, 10)]}


def is_peak(schedule: str, when=None) -> bool:
    from datetime import datetime, timezone

    windows = PEAK_SCHEDULES.get(schedule or "none", [])
    if not windows:
        return False
    t = when or datetime.now(timezone.utc)
    if t.tzinfo is not None:
        t = t.astimezone(timezone.utc)
    return t.weekday() < 5 and any(a <= t.hour < b for a, b in windows)


def normalize_reasoning_effort(value: Any) -> str:
    """Keep provider-defined effort values, using an empty value for its default.

    Do not enumerate levels here: models can publish new values before a
    PaperMap update. In particular, ``ultra``, ``max`` and ``xhigh`` are distinct.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError("Reasoning effort must be a string")
    value = value.strip()
    if value in ("", "default"):
        return ""
    if len(value) > 128 or any(c.isspace() or not c.isprintable() for c in value):
        raise ConfigError("Reasoning effort must be a single printable value without spaces")
    return value


def effective_llm(llm: dict[str, Any], *, data_dir: Path | None = None,
                  presets: dict | None = None) -> dict[str, Any]:
    """Resolve the provider choice into concrete connection settings."""
    provider = llm.get("provider") or "deepseek"
    if provider in PROVIDERS:
        pre = PROVIDERS[provider]
        model = llm.get("model") or pre["default_model"]
        price = pre["models"].get(model) or pre["models"][pre["default_model"]]
        env = pre["api_key_env"]
        key = os.environ.get(env, "").strip() or (llm.get("api_key") or "").strip()
        return {
            "provider": provider, "base_url": pre["base_url"], "protocol": pre["protocol"],
            "model": model, "api_key": key, "api_key_env": env,
            "key_from_env": bool(os.environ.get(env, "").strip()),
            "extra_body": pre["extra_body"],
            "price_input_per_1m": price["in"], "price_cache_hit_per_1m": price["hit"],
            "price_output_per_1m": price["out"], "price_multiplier": 1.0,
            "peak_schedule": pre.get("peak_schedule", "none"),
            "billing": "token", "json_mode": True, "no_temperature": False, "min_max_tokens": 0,
        }
    presets = PRESETS if presets is None else presets
    if provider in presets:
        pre = presets[provider]
        st = (llm.get("presets") or {}).get(provider) or {}
        model = (st.get("model") or pre["default_model"] or "").strip()
        sub = pre["group"] == "subscription"
        price = pre["models"].get(model) or {}
        def num(key: str, fallback: Any) -> float:
            v = st.get(key)
            return float(v) if v not in (None, "") else float(fallback or 0)
        p_in = 0.0 if sub else num("price_input_per_1m", price.get("in"))
        return {
            "data_dir": str(data_dir or _expand(DEFAULT_DATA_DIR)),
            "provider": provider, "preset_label": pre["label"], "base_url": pre["base_url"],
            "protocol": pre["protocol"], "model": model,
            "api_key": "" if provider in ("chatgpt_plan", "claude_cli") else (st.get("api_key") or "").strip(),
            "api_key_env": "", "key_from_env": False,
            "extra_body": pre.get("extra_body") or {},
            "price_input_per_1m": p_in,
            "price_cache_hit_per_1m": 0.0 if sub else num("price_cache_hit_per_1m", price.get("hit", p_in)),
            "price_output_per_1m": 0.0 if sub else num("price_output_per_1m", price.get("out")),
            "price_multiplier": 1.0, "peak_schedule": "none",
            "billing": "subscription" if sub else "token",
            "json_mode": pre.get("json_mode", True), "no_temperature": pre.get("no_temperature", False),
            "min_max_tokens": int(pre.get("min_max_tokens") or 0),
            "reasoning_effort": normalize_reasoning_effort(st.get("reasoning_effort")) if provider in ("chatgpt_plan", "claude_cli") else "",
        }
    if provider.startswith("ext."):
        # A disabled, removed or changed plugin must never reuse custom credentials.
        eff = effective_llm({"provider": "custom"})
        eff["provider"] = provider
        return eff
    c = llm.get("custom") or {}
    env = (c.get("api_key_env") or "").strip()
    env_key = os.environ.get(env, "").strip() if env else ""
    return {
        "provider": "custom", "base_url": (c.get("base_url") or "").strip(),
        "protocol": c.get("protocol") if c.get("protocol") in PROTOCOLS else "openai",
        "model": (c.get("model") or "").strip(), "api_key": env_key or (c.get("api_key") or "").strip(),
        "api_key_env": env, "key_from_env": bool(env_key),
        "extra_body": c.get("extra_body") or {},
        "price_input_per_1m": float(c.get("price_input_per_1m") or 0),
        # cache-read price defaults to the normal input price when not given
        "price_cache_hit_per_1m": float(c["price_cache_hit_per_1m"]) if c.get("price_cache_hit_per_1m") not in (None, "")
        else float(c.get("price_input_per_1m") or 0),
        "price_output_per_1m": float(c.get("price_output_per_1m") or 0),
        "price_multiplier": float(c.get("price_multiplier") or 1.0),
        "peak_schedule": c.get("peak_schedule") if c.get("peak_schedule") in PEAK_SCHEDULES else "none",
        "billing": "subscription" if c.get("billing") == "subscription" else "token",
        "json_mode": True, "no_temperature": False, "min_max_tokens": 0,
    }


OFFICIAL_TRANSLATION_SERVICES = {
    "niutrans": {"id": "niutrans", "kind": "niutrans", "name": "小牛翻译", "model": "通用文本 Flash",
                 "app_id": "", "api_key": ""},
    "zhipu": {"id": "zhipu", "kind": "zhipu", "name": "智谱翻译", "model": "glm-4.7-flash", "api_key": ""},
    "tencent": {"id": "tencent", "kind": "tencent", "name": "腾讯混元翻译", "model": "hy-mt2-lite", "api_key": ""},
}


DEFAULTS: dict[str, Any] = {
    "inbox_dirs": [],
    "data_dir": DEFAULT_DATA_DIR,
    "knowledge_dir": "",  # empty = <data_dir>/knowledge
    "appearance": {"palette": "blue", "mode": "system"},  # see themes.py
    "backup": {"enabled": True, "keep": 14, "dir": "", "every_hours": 24},  # database + settings
    "exclude": {
        "dir_names": ["_private", "drafts"],
        "filename_patterns": ["*draft*", "*_SI.pdf", "*supplement*"],
    },
    "llm": {
        # "deepseek" = official preset (only key + model to choose);
        # "custom" = any OpenAI- or Anthropic-compatible endpoint.
        "provider": "deepseek",
        "model": "deepseek-flash",
        "api_key": "",  # official key, stored locally; env var wins if set
        "custom": {
            "base_url": "",
            "api_key": "",
            "api_key_env": "",
            "protocol": "openai",  # openai | responses | anthropic | gemini
            "model": "",
            "extra_body": {},
            "price_input_per_1m": 0,
            "price_cache_hit_per_1m": None,  # None = same as input price
            "price_output_per_1m": 0,
            "price_multiplier": 1.0,  # e.g. a relay's group ratio (分组倍率)
            "peak_schedule": "none",  # none | deepseek (2x at DeepSeek peak hours)
            "billing": "token",  # token | subscription (flat-rate plan: tokens logged, cost $0)
        },
        "presets": {},  # per-preset {api_key, model, price_*_per_1m}
        "max_input_chars": 60000,
        "monthly_budget_usd": 5,
        "timeout_s": 180,
    },
    "output_language": "zh",
    "metadata": {
        "contact_email": "",
        "openalex_api_key": "",
        "title_match_threshold": 85,
    },
    # Built-in preset name (liver_omics / default) or a YAML file path
    # (relative paths are resolved against data_dir).
    "taxonomy_file": "default",
    "classification": {
        "accept_confidence": 0.6,  # labels at/above this are assigned
        "review_confidence": 0.35,  # labels in [review, accept) go to the review page
    },
    "new_field_threshold": 3,  # papers needed before a proposed field is put to the user
    # Optional embedding model (OpenAI-compatible /embeddings on the same
    # endpoint) used to merge similar proposed fields; empty = names only.
    "embedding": {"model": ""},
    "knowledge_git": False,  # auto-commit knowledge/ to a local git repo
    "translation": {
        "default": "bilibili",
        "services": [
            {"id": "bilibili", "name": "bilibili Index-Translate（免费）", "kind": "bilibili",
             "model": "Index-Translate-35B-A3B", "mode": "translate", "prompt": ""},
            {"id": "main", "name": "用主模型翻译", "kind": "main", "mode": "translate", "prompt": ""},
            *OFFICIAL_TRANSLATION_SERVICES.values(),
        ],
        "target_language": "zh",
        "fallback_to_main": True,  # use the main LLM when the translation service fails
        "privacy_ack": False,  # user confirmed that text goes to the free service
        "auto_new_papers": False,
    },
    "skills": {"default": "", "auto_new_papers": False},
    "consolidate_every": 10,  # re-consolidate a field hub after this many new papers
    "concept_min_papers": 2,  # tool pages for tools used by at least this many papers
    "bulk_confirm_threshold": 5,  # ask before processing this many new papers at once
    "web": {"host": "127.0.0.1", "port": 8765},
    "debounce_seconds": 3,
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _expand(p: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(p)))).resolve()


class ConfigError(Exception):
    pass


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path | None = None
    _extra: dict[str, Any] = field(default_factory=dict)

    # -- convenience accessors -------------------------------------------
    @property
    def imported_dir(self) -> Path:
        """PDFs dragged into the app wait here until they are filed."""
        return self.data_dir / "imported"

    @property
    def watched_inboxes(self) -> list[Path]:
        """Folders the user chose (auto-import folders)."""
        return [_expand(p) for p in self.raw.get("inbox_dirs") or []]

    @property
    def inbox_dirs(self) -> list[Path]:
        """Everything that is scanned: the user's folders plus the app's import folder."""
        dirs = self.watched_inboxes
        if self.imported_dir not in dirs:
            dirs.append(self.imported_dir)
        return dirs

    @property
    def data_dir(self) -> Path:
        return _expand(self.raw["data_dir"])

    @property
    def db_path(self) -> Path:
        return self.data_dir / "papermap.db"

    @property
    def knowledge_dir(self) -> Path:
        """Markdown knowledge base; ``knowledge_dir`` in the config moves it
        elsewhere (iCloud, an Obsidian vault...). The database stays in data_dir."""
        custom = (self.raw.get("knowledge_dir") or "").strip()
        return _expand(custom) if custom else self.data_dir / "knowledge"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def exclude_dir_names(self) -> list[str]:
        return list(self.raw["exclude"].get("dir_names") or [])

    @property
    def exclude_patterns(self) -> list[str]:
        return list(self.raw["exclude"].get("filename_patterns") or [])

    @property
    def llm(self) -> dict[str, Any]:
        return self.raw["llm"]

    @property
    def output_language(self) -> str:
        return self.raw.get("output_language", "zh")

    @property
    def metadata(self) -> dict[str, Any]:
        return self.raw["metadata"]

    @property
    def web(self) -> dict[str, Any]:
        return self.raw["web"]

    def model_presets(self, only: str | None = None) -> dict:
        from .plugin_contributions import provider_presets

        return {**PRESETS, **provider_presets(self, only)}

    def resolve_llm(self, llm: dict) -> dict:
        provider = llm.get("provider") or ""
        presets = self.model_presets(only=provider.split(".", 2)[1]) if provider.startswith("ext.") else PRESETS
        return effective_llm(llm, data_dir=self.data_dir, presets=presets)

    def effective_llm(self) -> dict[str, Any]:
        return self.resolve_llm(self.llm)

    def model_ready(self) -> bool:
        eff = self.effective_llm()
        if eff["provider"] == "chatgpt_plan":
            from .chatgpt_auth import get_auth

            return bool(eff["model"] and get_auth(self.data_dir).status()["ready"])
        if eff["provider"] == "claude_cli":
            from .claude_cli import get_cli

            return bool(get_cli(self.data_dir).status()["ready"])
        return bool(eff["api_key"])

    def load_taxonomy(self):
        from . import taxonomy

        return taxonomy.load(str(self.raw.get("taxonomy_file") or "default"), self.data_dir)

    @property
    def classification(self) -> dict[str, Any]:
        return self.raw["classification"]

    def api_key(self) -> str:
        """Environment variable wins over the locally stored key."""
        return self.effective_llm()["api_key"]

    def ensure_dirs(self) -> None:
        self.imported_dir.mkdir(parents=True, exist_ok=True)
        for sub in ("papers", "fields", "handbooks", "concepts", "_pending"):
            (self.knowledge_dir / sub).mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        if self.output_language not in ("zh", "en", "bilingual"):
            raise ConfigError("output_language must be zh, en or bilingual")
        if not isinstance(self.raw.get("inbox_dirs"), list):
            raise ConfigError("inbox_dirs must be a list of folders")
        provider = self.llm.get("provider", "deepseek")
        if provider not in (*PROVIDERS, *PRESETS, "custom") and not (isinstance(provider, str) and provider.startswith("ext.")):
            raise ConfigError(f"llm.provider must be one of {', '.join([*PROVIDERS, *PRESETS, 'custom'])}")
        host = self.web.get("host")
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ConfigError("web.host must be a loopback address (127.0.0.1)")

    def save(self, path: Path | None = None) -> Path:
        target = path or self.path
        if target is None:
            raise ConfigError("no config path to save to")
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            yaml.safe_dump(self.raw, fh, allow_unicode=True, sort_keys=False)
        self.path = target
        return target


def default_config_path() -> Path:
    if os.environ.get(CONFIG_ENV):
        return _expand(os.environ[CONFIG_ENV])
    return _expand(DEFAULT_DATA_DIR) / "config.yaml"


def _migrate(data: dict[str, Any]) -> dict[str, Any]:
    llm = (data or {}).get("llm") or {}
    if "provider" not in llm and llm.get("base_url"):
        if "deepseek.com" not in llm["base_url"]:
            llm["provider"] = "custom"
            llm["custom"] = {"base_url": llm.pop("base_url"), "api_key": llm.pop("api_key", ""),
                             "api_key_env": llm.pop("api_key_env", ""), "model": llm.pop("model", ""),
                             "protocol": "openai"}
        else:
            llm.pop("base_url")
            llm.pop("api_key_env", None)
    for legacy in ("base_url", "api_key_env", "extra_body", "price_input_per_1m",
                   "price_cache_hit_per_1m", "price_output_per_1m"):
        llm.pop(legacy, None)
    return data


def from_dict(data: dict[str, Any], path: Path | None = None) -> Config:
    data = _migrate(data or {})
    cfg = Config(raw=_deep_merge(DEFAULTS, data or {}), path=path)
    # Lists in older YAML files replace defaults. Keep their services and default selection.
    services = list(cfg.raw["translation"]["services"])
    existing = {svc.get("id") for svc in services}
    services.extend(copy.deepcopy(preset) for sid, preset in OFFICIAL_TRANSLATION_SERVICES.items() if sid not in existing)
    cfg.raw["translation"]["services"] = services
    cfg.validate()
    return cfg


def load(path: str | Path | None = None) -> Config:
    p = _expand(path) if path else default_config_path()
    if not p.exists():
        raise ConfigError(
            f"Config file not found: {p}\nRun `papermap init` first (or pass --config)."
        )
    with open(p, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {p} is not a YAML mapping")
    return from_dict(data, path=p)
