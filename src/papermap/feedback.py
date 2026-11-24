"""A bug report the user reviews before sending: version, system and recent errors, with keys,
personal paths and long tokens removed. It opens as a pre-filled GitHub issue in the browser."""
from __future__ import annotations

import platform
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

from . import __version__
from .appupdate import REPO

MAX_URL_BODY = 6000


def _secrets(config) -> list[str]:
    out: list[str] = []

    def walk(x, k=""):
        if isinstance(x, dict):
            for kk, v in x.items():
                walk(v, f"{k}.{kk}")
        elif isinstance(x, list):
            for v in x:
                walk(v, k)
        elif isinstance(x, str) and len(x) >= 8 and re.search(r"key|token|secret|password|app_id", k, re.I):
            out.append(x)
    walk(config.raw)
    return sorted(set(out), key=len, reverse=True)


def redact(text: str, config) -> str:
    """Remove anything that could identify the user or unlock their accounts."""
    for s in _secrets(config):
        text = text.replace(s, "[key removed]")
    home = str(Path.home())
    for p in sorted({str(config.knowledge_dir), str(config.data_dir), home}, key=len, reverse=True):
        text = text.replace(p, "~" if p == home else "<data>")
    text = re.sub(r"\b(sk|ghp|gho|ghs|ghu|xox[abp])[-_][A-Za-z0-9_\-]{12,}", "[key removed]", text)
    text = re.sub(r"\b[A-Za-z0-9_\-]{32,}\b", "[long token removed]", text)
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email removed]", text)
    return text


def report(config, registry, description: str = "") -> dict:
    eff = config.effective_llm()
    errors = registry.query("SELECT level, message, created_at FROM events WHERE level IN ('error','warning') "
                            "ORDER BY id DESC LIMIT 12")
    lines = [
        "### What happened / 发生了什么",
        description.strip() or "<!-- describe the problem and the steps to reproduce it / 请描述问题和重现步骤 -->",
        "",
        "### Environment / 环境",
        f"- PaperMap {__version__} ({'app' if getattr(sys, 'frozen', False) else 'python'})",
        f"- {platform.system()} {platform.release()} ({platform.machine()}), Python {platform.python_version()}",
        f"- Model provider: {eff.get('provider')} · model: {eff.get('model') or '—'}",
        f"- Interface language: {(config.raw.get('appearance') or {}).get('language') or 'zh-CN'}",
        f"- Papers: {registry.one('SELECT count(*) AS n FROM papers')['n']}",
        "",
        "### Recent warnings and errors / 最近的警告和错误",
    ]
    if errors:
        lines += ["```", *[f"{r['created_at'][:19]} {r['level']}: {r['message'][:300]}" for r in errors], "```"]
    else:
        lines.append("(none)")
    body = redact("\n".join(lines), config)
    return {"title": "Bug: ", "body": body}


def issue_url(title: str, body: str) -> str:
    body = body[:MAX_URL_BODY]
    return f"https://github.com/{REPO}/issues/new?" + urlencode({"title": title[:200], "body": body, "labels": "bug"})
