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


def effective_llm(llm: dict[str, Any]) -> dict[str, Any]:
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
        }
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
    }


DEFAULTS: dict[str, Any] = {
    "inbox_dirs": [],
    "data_dir": DEFAULT_DATA_DIR,
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
        },
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
    def inbox_dirs(self) -> list[Path]:
        return [_expand(p) for p in self.raw.get("inbox_dirs") or []]

    @property
    def data_dir(self) -> Path:
        return _expand(self.raw["data_dir"])

    @property
    def db_path(self) -> Path:
        return self.data_dir / "papermap.db"

    @property
    def knowledge_dir(self) -> Path:
        return self.data_dir / "knowledge"

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

    def effective_llm(self) -> dict[str, Any]:
        return effective_llm(self.llm)

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
        for sub in ("papers", "fields", "handbooks", "concepts", "_pending"):
            (self.knowledge_dir / sub).mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        if self.output_language not in ("zh", "en", "bilingual"):
            raise ConfigError("output_language must be zh, en or bilingual")
        if not isinstance(self.raw.get("inbox_dirs"), list):
            raise ConfigError("inbox_dirs must be a list of folders")
        if self.llm.get("provider", "deepseek") not in (*PROVIDERS, "custom"):
            raise ConfigError(f"llm.provider must be one of {', '.join([*PROVIDERS, 'custom'])}")
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
