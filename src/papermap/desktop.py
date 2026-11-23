"""Native desktop window (pywebview) around the local web UI (CLAUDE.md §14.1).

- macOS: regular Dock app. Closing the window hides it (watching continues);
  clicking the Dock icon brings it back; dropping PDFs on the Dock icon
  imports them; Cmd+Q quits.
- Windows: closing hides the window to the notification-area icon.
- A second launch asks the running instance to show its window.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

_state = {"quitting": False, "window": None, "on_files": None}


def available() -> bool:
    try:
        import webview  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


class JsApi:
    """Functions callable from the page as window.pywebview.api.*"""

    def pick_folder(self) -> str | None:
        import webview

        w = _state["window"]
        if w is None:
            return None
        res = w.create_file_dialog(webview.FileDialog.FOLDER)
        return res[0] if res else None


def show_window() -> None:
    w = _state["window"]
    if w is not None:
        w.show()
        w.restore()


def import_files(paths: list[str], imported_dir: Path, handle: Callable[[Path], object]) -> int:
    """Copy PDFs (from a Dock drop / menu) into the app's import folder and process them.
    The user's original files are never moved."""
    from .library import clone_copy
    from .web.app import _safe_name, _unique

    saved = []
    for p in paths:
        src = Path(p)
        if src.is_file() and src.suffix.lower() == ".pdf":
            dest = _unique(imported_dir / _safe_name(src.name))
            clone_copy(src, dest)
            saved.append(dest)
    if saved:
        threading.Thread(target=lambda: [handle(x) for x in saved], daemon=True).start()
    return len(saved)


def _install_mac_delegate() -> None:
    """Extend pywebview's NSApplication delegate: Dock reopen, Dock file drop, Cmd+Q."""
    from webview.platforms import cocoa

    base = cocoa.BrowserView.AppDelegate
    if getattr(base, "_papermap", False):
        return
    import objc

    class PaperMapAppDelegate(base):
        _papermap = True

        def applicationShouldTerminate_(self, app):
            _state["quitting"] = True  # let the window's close handler allow closing
            return objc.super(PaperMapAppDelegate, self).applicationShouldTerminate_(app)

        def applicationShouldHandleReopen_hasVisibleWindows_(self, app, flag):
            show_window()
            return True

        def application_openFiles_(self, app, filenames):
            cb = _state["on_files"]
            if cb is not None:
                cb([str(f) for f in filenames])
            show_window()
            app.replyToOpenOrPrint_(0)  # NSApplicationDelegateReplySuccess

    cocoa.BrowserView.AppDelegate = PaperMapAppDelegate


def _wait_for(url: str, timeout: float = 20) -> None:
    import httpx

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.15)


def run(cfg, reg, pipe, background: bool, start_page: str = "/") -> int:
    """Start watcher + local server, then show the native window (blocks)."""
    import uvicorn
    import webview
    from webview.menu import Menu, MenuAction, MenuSeparator

    from .tray import open_folder
    from .watcher import Watcher
    from .web.app import create_app

    base = f"http://{cfg.web['host']}:{int(cfg.web['port'])}"
    watcher = Watcher(cfg, pipe)
    # The folder scan can block on macOS privacy prompts (e.g. an inbox inside
    # Documents): never let that hold up the window and the web server.
    threading.Thread(target=watcher.start, name="watcher-start", daemon=True).start()
    app = create_app(cfg, reg, pipe)
    app.state.show_window = show_window
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.web["host"], port=int(cfg.web["port"]), log_level="warning"))
    threading.Thread(target=server.run, name="web", daemon=True).start()
    _wait_for(base + "/static/style.css")

    _state["on_files"] = lambda paths: import_files(paths, cfg.imported_dir, pipe.handle_path)
    if sys.platform == "darwin":
        _install_mac_delegate()

    window = webview.create_window(
        "PaperMap", base + start_page, width=1320, height=860, min_size=(900, 600),
        js_api=JsApi(), hidden=background, text_select=True, background_color="#FFFFFF",
    )
    _state["window"] = window

    def on_closing():
        if _state["quitting"]:
            return True
        window.hide()  # keep running in the background
        return False

    window.events.closing += on_closing

    def import_dialog():
        res = window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True,
                                        file_types=("PDF (*.pdf)",))
        if res:
            n = import_files(list(res), cfg.imported_dir, pipe.handle_path)
            window.evaluate_js(f"window.pmToast && pmToast('已导入 {n} 篇，正在处理…')")

    def quit_app():
        _state["quitting"] = True
        window.destroy()

    menu = [
        Menu("文件", [
            MenuAction("导入 PDF…", import_dialog),
            MenuSeparator(),
            MenuAction("打开知识库文件夹", lambda: open_folder(cfg.knowledge_dir)),
            MenuAction("打开数据文件夹", lambda: open_folder(cfg.data_dir)),
        ]),
    ]

    tray_icon = None
    if os.name == "nt":  # Windows: hidden window lives in the notification area
        tray_icon = _windows_tray(base, cfg, quit_app)

    try:
        webview.start(menu=menu, private_mode=False, storage_path=str(cfg.data_dir / "webview"))
    finally:
        _state["quitting"] = True
        server.should_exit = True
        watcher.stop()
        if tray_icon is not None:
            tray_icon.stop()
    return 0


def _windows_tray(base: str, cfg, quit_app):
    try:
        import pystray
        from importlib import resources

        from PIL import Image
    except Exception:  # noqa: BLE001
        return None
    with resources.as_file(resources.files("papermap.web").joinpath("static", "icon.png")) as p:
        image = Image.open(p).copy()
    from .tray import open_folder

    icon = pystray.Icon("PaperMap", image, "PaperMap", menu=pystray.Menu(
        pystray.MenuItem("打开 PaperMap", lambda: show_window(), default=True),
        pystray.MenuItem("打开知识库文件夹", lambda: open_folder(cfg.knowledge_dir)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出 PaperMap", lambda: (icon.stop(), quit_app())),
    ))
    icon.run_detached()
    return icon
