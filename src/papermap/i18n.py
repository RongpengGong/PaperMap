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


def from_locale(tag: str | None) -> str | None:
    """Map an OS / browser locale tag (zh-Hans-CN, zh_TW, de-DE, en-US...) to an interface language.
    Other languages get English, which more people read than Chinese."""
    t = (tag or "").replace("_", "-").strip().lower()
    if not t or t in ("c", "posix"):
        return None
    if t.startswith("zh"):
        return "zh-TW" if any(x in t for x in ("hant", "-tw", "-hk", "-mo")) else "zh-CN"
    if t.startswith("de"):
        return "de"
    return "en"


@lru_cache(maxsize=1)
def system_language() -> str:
    """The interface language the operating system prefers (first preferred language)."""
    import locale
    import os
    import subprocess
    import sys

    tags: list[str] = []
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True, text=True,
                                 timeout=3).stdout
            tags += [x.strip().strip('",') for x in out.splitlines() if x.strip().strip('",()')]
        except (OSError, subprocess.SubprocessError):
            pass
    elif sys.platform == "win32":
        try:
            import ctypes

            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()  # type: ignore[attr-defined]
            tags.append(locale.windows_locale.get(lcid, ""))
        except (OSError, AttributeError):
            pass
    tags += [os.environ.get(k, "") for k in ("LC_ALL", "LC_MESSAGES", "LANG")]
    try:
        tags.append(locale.getlocale()[0] or "")
    except ValueError:
        pass
    for tag in tags:
        code = from_locale(tag.split(".")[0])
        if code:
            return code
    return SOURCE


def config_language(config) -> str:
    """The chosen interface language; when none was chosen, the system's."""
    chosen = (config.raw.get("appearance") or {}).get("language")
    return chosen if chosen in LANGUAGES else system_language()


# ------------------------------------------------------------- generated content language
# Cards, hub pages and answers are written in `output_language`. "auto" follows the interface.
OUTPUT_LANGUAGES = {"auto": L("跟随界面语言"), "zh": "简体中文", "zh-TW": "繁體中文", "en": "English",
                    "de": "Deutsch", "bilingual": L("中文 + 英文术语")}
_UI_TO_OUTPUT = {"zh-CN": "zh", "zh-TW": "zh-TW", "en": "en", "de": "de"}
PROSE = {"zh": "Simplified Chinese", "zh-TW": "Traditional Chinese (Taiwan usage)", "en": "English",
         "de": "German", "bilingual": "Simplified Chinese with English key terms in parentheses"}


def content_language(config) -> str:
    """zh / zh-TW / en / de / bilingual: the language generated text is written in."""
    raw = config.raw.get("output_language") or "auto"
    if raw == "auto" or raw not in OUTPUT_LANGUAGES:
        return _UI_TO_OUTPUT.get(config_language(config), "zh")
    return raw


def prose_language(config) -> str:
    """Language instruction for model-written prose (technical terms stay in English)."""
    code = content_language(config)
    keep = "" if code in ("en", "bilingual") else " (keep gene, software and technical terms in English)"
    return PROSE.get(code, PROSE["zh"]) + keep


def family(code: str) -> str:
    """Page structure (headings, field names): Chinese for zh / zh-TW / bilingual, English for en / de."""
    return "en" if code in ("en", "de") else ("bilingual" if code == "bilingual" else "zh")


# ------------------------------------------------------------- translation target
TRANSLATION_TARGETS = {"auto": L("跟随界面语言"), "zh": "简体中文", "zh-TW": "繁體中文", "en": "English",
                       "de": "Deutsch", "ja": "日本語", "ko": "한국어", "fr": "Français", "es": "Español"}


def translation_target(config) -> str:
    raw = (config.raw.get("translation") or {}).get("target_language") or "auto"
    if raw == "auto" or raw not in TRANSLATION_TARGETS:
        return _UI_TO_OUTPUT.get(config_language(config), "zh")
    return raw
