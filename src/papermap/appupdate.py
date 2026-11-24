"""Tell the user when a newer PaperMap release is out (one anonymous request to GitHub per day).

Nothing about the user or their library is sent: the request is a plain public GitHub API call
with a generic user agent. It can be turned off in Settings (``update_check: false``).
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone

import httpx

from . import __version__

REPO = "RongpengGong/PaperMap"
RELEASES = f"https://github.com/{REPO}/releases"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
STATE = "update_check.json"
EVERY = timedelta(hours=24)
_lock = threading.Lock()


def _ver(tag: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", tag or "")[:3]) or (0,)


def enabled(config) -> bool:
    return config.raw.get("update_check", True) is not False


def state(config) -> dict:
    try:
        st = json.loads((config.data_dir / STATE).read_text("utf-8"))
    except (OSError, ValueError):
        st = {}
    latest = st.get("latest") or ""
    st["current"] = __version__
    st["newer"] = bool(latest) and _ver(latest) > _ver(__version__)
    st.setdefault("url", RELEASES)
    return st


def check(config, force: bool = False, client: httpx.Client | None = None) -> dict:
    """Refresh the cached answer when it is older than a day (or ``force``)."""
    with _lock:
        st = state(config)
        last = st.get("checked_at")
        if not force and last and datetime.now(timezone.utc) - datetime.fromisoformat(last) < EVERY:
            return st
        try:
            c = client or httpx.Client(timeout=10)
            r = c.get(API, headers={"User-Agent": "PaperMap-update-check", "Accept": "application/vnd.github+json"})
            if r.status_code == 200:
                body = r.json()
                st.update(latest=str(body.get("tag_name") or "").lstrip("v"), url=body.get("html_url") or RELEASES)
            st["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            st.pop("error", None)
        except (httpx.HTTPError, ValueError) as exc:
            st["error"] = str(exc)[:200]
            st["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        keep = {k: st[k] for k in ("latest", "url", "checked_at", "error") if k in st}
        (config.data_dir / STATE).write_text(json.dumps(keep), "utf-8")
        return state(config)


def check_in_background(config) -> None:
    if enabled(config):
        threading.Thread(target=check, args=(config,), daemon=True, name="update-check").start()
