"""Build a standalone PaperMap app with PyInstaller.

    python packaging/build.py          # -> dist/PaperMap/ (folder app)

macOS: dist/PaperMap.app is also produced. Windows: dist/PaperMap/PaperMap.exe.
Build on each target OS (PyInstaller does not cross-compile).
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    subprocess.check_call([sys.executable, str(ROOT / "packaging" / "make_icon.py")])
    args = [
        sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / "launcher.py"),
        "--name", "PaperMap", "--noconfirm", "--clean", "--onedir",
        "--paths", str(ROOT / "src"),
        "--collect-data", "papermap",          # templates, static, prompts, taxonomies
        "--collect-submodules", "papermap",
        "--collect-submodules", "uvicorn",     # uvicorn imports loops/protocols dynamically
        "--hidden-import", "watchdog.observers",
        "--hidden-import", "pystray", "--collect-submodules", "pystray",
        "--collect-submodules", "webview", "--collect-data", "webview",
        "--osx-bundle-identifier", "com.papermap.app",
        "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
    ]
    # No console window: the tray icon is the UI (quit, open, autostart).
    args.append("--windowed")
    if sys.platform == "darwin":
        args += ["--icon", str(ROOT / "build" / "PaperMap.icns")]
    elif sys.platform == "win32":
        args += ["--icon", str(ROOT / "build" / "PaperMap.ico")]
    rc = subprocess.call(args, cwd=ROOT)
    if rc == 0 and sys.platform == "darwin":
        _patch_info_plist(ROOT / "dist" / "PaperMap.app")
    return rc


def _patch_info_plist(app: Path) -> None:
    """Regular Dock app that accepts PDFs dropped on its icon; name and version."""
    import plistlib

    sys.path.insert(0, str(ROOT / "src"))
    from papermap import __version__

    info = app / "Contents" / "Info.plist"
    with open(info, "rb") as fh:
        data = plistlib.load(fh)
    data.pop("LSUIElement", None)
    data.update({
        "CFBundleDocumentTypes": [{
            "CFBundleTypeName": "PDF document", "CFBundleTypeRole": "Viewer", "LSHandlerRank": "Alternate",
            "LSItemContentTypes": ["com.adobe.pdf"],
        }],
        "CFBundleDisplayName": "PaperMap",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "NSHumanReadableCopyright": "PaperMap",
        "NSDocumentsFolderUsageDescription": "PaperMap reads the PDFs in the inbox folders you choose.",
        "NSAppleEventsUsageDescription": "PaperMap opens Terminal when you choose to sign in with Claude Code.",
        "NSDownloadsFolderUsageDescription": "PaperMap reads the PDFs in the inbox folders you choose.",
    })
    with open(info, "wb") as fh:
        plistlib.dump(data, fh)
    # the bundle was signed before the plist changed: re-sign ad hoc so macOS accepts it
    subprocess.call(["codesign", "--force", "--deep", "--sign", "-", str(app)])


if __name__ == "__main__":
    sys.exit(main())
