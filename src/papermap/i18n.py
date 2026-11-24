"""Interface language (menus, buttons, labels). Simplified Chinese is the source text;
other languages are JSON catalogs in papermap/locales/<code>.json mapping source -> translation.
Generated knowledge (cards, hub pages) follows `output_language`, a separate setting, and user
content is never translated."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources

SOURCE = "zh-CN"


class L(str):
    """A UI label defined in Python (status names, settings groups...). Templates translate
    values of this type when rendered; ordinary strings - papers, notes, titles - never are."""
    __slots__ = ()

LANGUAGES = {"zh-CN": "简体中文", "zh-TW": "繁體中文", "en": "English", "de": "Deutsch"}
HTML_LANG = {"zh-CN": "zh-Hans", "zh-TW": "zh-Hant", "en": "en", "de": "de"}


def normalize(code) -> str:
    return code if code in LANGUAGES else SOURCE


@lru_cache(maxsize=8)
def catalog(code: str) -> dict[str, str]:
    if code == SOURCE:
        return {}
    try:
        text = resources.files("papermap.locales").joinpath(f"{code}.json").read_text("utf-8")
    except (FileNotFoundError, OSError):
        return {}
    return {k: v for k, v in json.loads(text).items() if isinstance(v, str) and v}


def translate(code: str, text: str) -> str:
    if code == SOURCE or not text:
        return text
    cat = catalog(code)
    hit = cat.get(text)
    if hit is not None:
        return hit
    core = text.strip()  # keep surrounding whitespace of template text runs
    if core != text and core in cat:
        return text[: len(text) - len(text.lstrip())] + cat[core] + text[len(text.rstrip()):]
    return text


def config_language(config) -> str:
    return normalize((config.raw.get("appearance") or {}).get("language"))
