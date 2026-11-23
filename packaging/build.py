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
    args = [
        sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / "launcher.py"),
        "--name", "PaperMap", "--noconfirm", "--clean", "--onedir",
        "--paths", str(ROOT / "src"),
        "--collect-data", "papermap",          # templates, static, prompts, taxonomies
        "--collect-submodules", "papermap",
        "--collect-submodules", "uvicorn",     # uvicorn imports loops/protocols dynamically
        "--hidden-import", "watchdog.observers",
        "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
    ]
    if sys.platform == "darwin":
        args.append("--windowed")  # .app bundle without a terminal window
    elif sys.platform == "win32":
        args.append("--console")   # keep a console: shows status and Ctrl+C quits
    return subprocess.call(args, cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
