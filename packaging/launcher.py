"""Entry point for the PyInstaller build: double-click starts PaperMap."""

import sys

from papermap.cli import main

if __name__ == "__main__":
    sys.exit(main())
