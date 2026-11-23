"""Optional version control of the knowledge base (knowledge_git: true)."""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from pathlib import Path

log = logging.getLogger(__name__)
_lock = threading.Lock()


def available() -> bool:
    return shutil.which("git") is not None


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)


def commit(repo: Path, message: str) -> bool:
    """git add -A && commit in the knowledge folder. Never raises."""
    if not available():
        return False
    with _lock:
        try:
            if not (repo / ".git").exists():
                repo.mkdir(parents=True, exist_ok=True)
                _git(repo, "init", "-q")
                # repo-local identity so commits work without global git config
                _git(repo, "config", "user.name", "PaperMap")
                _git(repo, "config", "user.email", "papermap@localhost")
                (repo / ".gitignore").write_text("*.tmp\n.obsidian/workspace*\n", "utf-8")
            _git(repo, "add", "-A")
            if _git(repo, "diff", "--cached", "--quiet").returncode == 0:
                return False
            r = _git(repo, "commit", "-q", "-m", message)
            if r.returncode != 0:
                log.warning("knowledge git commit failed: %s", r.stderr.strip())
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("knowledge git commit failed: %s", exc)
            return False
