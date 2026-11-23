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

DEFAULTS: dict[str, Any] = {
    "inbox_dirs": [],
    "data_dir": DEFAULT_DATA_DIR,
    "exclude": {
        "dir_names": ["_private", "drafts"],
        "filename_patterns": ["*draft*", "*_SI.pdf", "*supplement*"],
    },
    "llm": {
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "api_key": "",  # optional; written by the web settings page, stays local
        "model": "deepseek-flash",
        # Extra request body; DeepSeek enables "thinking" by default, which is
        # slower and costlier and not needed for structured extraction.
        "extra_body": {"thinking": {"type": "disabled"}},
        "max_input_chars": 60000,
        "monthly_budget_usd": 5,
        # USD per 1M tokens, used only for the local cost estimate.
        "price_input_per_1m": 0.3,
        "price_cache_hit_per_1m": 0.006,
        "price_output_per_1m": 1.2,
        "timeout_s": 180,
    },
    "output_language": "zh",
    "metadata": {
        "contact_email": "",
        "openalex_api_key": "",
        "title_match_threshold": 85,
    },
    "taxonomy_file": "taxonomies/default.yaml",
    "new_field_threshold": 3,
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

    def api_key(self) -> str:
        """Environment variable wins over the locally stored key."""
        env_name = self.llm.get("api_key_env") or ""
        if env_name and os.environ.get(env_name):
            return os.environ[env_name].strip()
        return (self.llm.get("api_key") or "").strip()

    def ensure_dirs(self) -> None:
        for sub in ("papers", "fields", "handbooks", "concepts", "_pending"):
            (self.knowledge_dir / sub).mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        if self.output_language not in ("zh", "en", "bilingual"):
            raise ConfigError("output_language must be zh, en or bilingual")
        if not isinstance(self.raw.get("inbox_dirs"), list):
            raise ConfigError("inbox_dirs must be a list of folders")
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


def from_dict(data: dict[str, Any], path: Path | None = None) -> Config:
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
