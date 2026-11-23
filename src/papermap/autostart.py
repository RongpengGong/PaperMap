"""Start PaperMap at login: macOS launchd, Windows Task Scheduler, Linux systemd --user."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.papermap.app"
TASK = "PaperMap"


def command(config_path: Path) -> list[str]:
    """The command that starts the watcher + web UI without opening a browser."""
    if getattr(sys, "frozen", False):  # packaged app: desktop mode with tray, no browser
        return [sys.executable, "--config", str(config_path), "app", "--background"]
    exe = Path(sys.executable)
    if os.name == "nt":  # pythonw.exe avoids a console window at login
        pyw = exe.with_name("pythonw.exe")
        exe = pyw if pyw.exists() else exe
    return [str(exe), "-m", "papermap", "--config", str(config_path), "run"]


def _mac_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _linux_unit() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "papermap.service"


def describe(config_path: Path, log_dir: Path) -> str:
    cmd = command(config_path)
    if sys.platform == "darwin":
        return f"macOS LaunchAgent {_mac_plist()} running: {' '.join(cmd)}"
    if os.name == "nt":
        return f"Windows scheduled task '{TASK}' (at logon) running: {subprocess.list2cmdline(cmd)}"
    return f"systemd user service {_linux_unit()} running: {' '.join(cmd)}"


def install(config_path: Path, log_dir: Path, start_now: bool = True) -> str:
    cmd = command(config_path)
    log_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        plist = _mac_plist()
        plist.parent.mkdir(parents=True, exist_ok=True)
        with open(plist, "wb") as fh:
            plistlib.dump({
                "Label": LABEL, "ProgramArguments": cmd, "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False},  # restart after a crash, not after a normal quit
                "StandardOutPath": str(log_dir / "autostart.log"),
                "StandardErrorPath": str(log_dir / "autostart.log"),
                "ProcessType": "Background",
            }, fh)
        if not start_now:
            return f"Installed {plist} (starts at next login)"
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(plist)], capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"launchctl failed: {r.stderr.strip()}")
        return f"Installed and started {plist}"
    if os.name == "nt":
        r = subprocess.run(["schtasks", "/Create", "/TN", TASK, "/TR", subprocess.list2cmdline(cmd),
                            "/SC", "ONLOGON", "/RL", "LIMITED", "/F"], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"schtasks failed: {r.stderr.strip() or r.stdout.strip()}")
        return f"Scheduled task '{TASK}' created (runs at logon)"
    unit = _linux_unit()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        "[Unit]\nDescription=PaperMap literature watcher\n\n[Service]\n"
        f"ExecStart={' '.join(cmd)}\nRestart=on-failure\n\n[Install]\nWantedBy=default.target\n", "utf-8")
    r = subprocess.run(["systemctl", "--user", "enable", "--now", "papermap.service"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"systemctl failed: {r.stderr.strip()}")
    return f"Enabled systemd user service {unit}"


def uninstall() -> str:
    if sys.platform == "darwin":
        plist = _mac_plist()
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)], capture_output=True)
        if plist.exists():
            plist.unlink()
            return f"Removed {plist}"
        return "Not installed"
    if os.name == "nt":
        r = subprocess.run(["schtasks", "/Delete", "/TN", TASK, "/F"], capture_output=True, text=True)
        return "Scheduled task removed" if r.returncode == 0 else "Not installed"
    subprocess.run(["systemctl", "--user", "disable", "--now", "papermap.service"], capture_output=True)
    unit = _linux_unit()
    if unit.exists():
        unit.unlink()
        return f"Removed {unit}"
    return "Not installed"


def status() -> str:
    if sys.platform == "darwin":
        return "installed" if _mac_plist().exists() else "not installed"
    if os.name == "nt":
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK], capture_output=True, text=True)
        return "installed" if r.returncode == 0 else "not installed"
    return "installed" if _linux_unit().exists() else "not installed"
