"""System notifications (macOS Notification Center). Elsewhere they are only logged."""
from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger(__name__)

_SCRIPT = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run"]


def send(title: str, body: str) -> bool:
    """Show a notification. Text goes in as arguments, never into the script itself."""
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(["osascript", *_SCRIPT, title[:120], body[:400]], check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("notification failed: %s", exc)
        return False
