"""Menu-bar (macOS) / system-tray (Windows, Linux) icon for the desktop app.

Requires the optional ``pystray`` + ``Pillow`` packages (bundled in the
packaged app). Must run on the main thread on macOS.
"""

from __future__ import annotations

import os
import subprocess
import sys
import logging
import webbrowser
from importlib import resources
from pathlib import Path
from typing import Callable


log = logging.getLogger(__name__)


def open_url(url: str) -> None:
    """Open a URL in the default browser. Uses the OS opener directly: Python's
    webbrowser module is unreliable inside packaged (windowed) apps."""
    log.info("Opening browser: %s", url)
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["/usr/bin/open", url])
        elif os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            webbrowser.open(url)
    except Exception:  # noqa: BLE001
        log.exception("could not open %s", url)


def _install_reopen_handler(on_reopen: Callable[[], None]) -> object | None:
    """macOS: double-clicking an app that is already running does not start a
    second copy - LaunchServices sends a 'reopen' Apple event instead."""
    try:
        import objc
        from Foundation import NSAppleEventManager, NSObject
    except Exception:  # noqa: BLE001
        return None

    class _Reopen(NSObject):
        def handleReopen_withReplyEvent_(self, event, reply):  # noqa: N802 - ObjC selector
            on_reopen()

    handler = _Reopen.alloc().init()
    k_core, k_reopen = 0x61657674, 0x72617070  # 'aevt', 'rapp'
    NSAppleEventManager.sharedAppleEventManager().setEventHandler_andSelector_forEventClass_andEventID_(
        handler, objc.selector(handler.handleReopen_withReplyEvent_, signature=b"v@:@@"), k_core, k_reopen)
    return handler  # keep a reference alive


def available() -> bool:
    try:
        import PIL  # noqa: F401
        import pystray  # noqa: F401
    except Exception:  # noqa: BLE001 - missing GUI backend etc.
        return False
    return True


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


def run(url: str, knowledge_dir: Path, inbox_dirs: list[Path], config_path: Path, log_dir: Path,
        on_quit: Callable[[], None]) -> None:
    """Show the icon and block until the user chooses Quit."""
    import pystray
    from PIL import Image

    from . import autostart

    name = "tray.png" if sys.platform == "darwin" else "icon.png"
    with resources.as_file(resources.files("papermap.web").joinpath("static", name)) as p:
        image = Image.open(p).copy()

    def toggle_autostart(icon, item) -> None:
        try:
            if autostart.status() == "installed":
                autostart.uninstall()
            else:
                autostart.install(config_path, log_dir, start_now=False)
        except Exception as exc:  # noqa: BLE001
            icon.notify(f"自动启动设置失败：{exc}", "PaperMap")

    def quit_app(icon, item) -> None:
        icon.stop()
        on_quit()

    items = [
        pystray.MenuItem("打开 PaperMap", lambda: open_url(url), default=True),
        pystray.MenuItem("打开知识库文件夹", lambda: open_folder(knowledge_dir)),
    ]
    if inbox_dirs:
        items.append(pystray.MenuItem("打开入口文件夹", lambda: open_folder(inbox_dirs[0])))
    items += [
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("登录时自动启动", toggle_autostart, checked=lambda item: autostart.status() == "installed"),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出 PaperMap", quit_app),
    ]
    cls = pystray.Icon
    if sys.platform == "darwin":
        class _TemplateIcon(pystray.Icon):
            """Black-on-transparent 'template' image: macOS recolours it for
            light and dark menu bars (pystray does not expose this)."""

            def _assert_image(self):
                super()._assert_image()
                img = getattr(self, "_icon_image", None)
                if img is not None:
                    img.setTemplate_(True)
                    img.setSize_((18, 18))
                    self._status_item.button().setImage_(img)

        cls = _TemplateIcon
    icon = cls("PaperMap", image, "PaperMap", menu=pystray.Menu(*items))
    def setup(ic) -> None:
        ic.visible = True
        if sys.platform == "darwin":
            ic._reopen_handler = _install_reopen_handler(lambda: open_url(url))  # noqa: SLF001

    icon.run(setup=setup)
