# Third-party notices

PaperMap depends on the following open-source components. Their licenses apply to those components;
the full license texts ship with each package.

## Python packages (installed with PaperMap)

| Package | License | Notes |
|---|---|---|
| [PyMuPDF](https://github.com/pymupdf/PyMuPDF) | **AGPL-3.0** (or a commercial license from Artifex) | PDF text, rendering and OCR layer. Because the packaged app includes PyMuPDF, distributing the app is subject to the AGPL-3.0. |
| [FastAPI](https://github.com/fastapi/fastapi) | MIT | local web server for the app window |
| [Uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | |
| [httpx](https://github.com/encode/httpx) | BSD-3-Clause | |
| [openai](https://github.com/openai/openai-python) | Apache-2.0 | OpenAI-compatible model client |
| [mcp](https://github.com/modelcontextprotocol/python-sdk) | MIT | local read-only MCP server |
| [Jinja2](https://github.com/pallets/jinja) | BSD-3-Clause | |
| [Markdown](https://github.com/Python-Markdown/markdown) | BSD-3-Clause | |
| [nh3](https://github.com/messense/nh3) | MIT | HTML sanitising |
| [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz) | MIT | |
| [watchdog](https://github.com/gorakhargosh/watchdog) | Apache-2.0 | |
| [PyYAML](https://github.com/yaml/pyyaml) | MIT | |
| [python-multipart](https://github.com/Kludex/python-multipart) | Apache-2.0 | |
| [PyJWT](https://github.com/jpadilla/pyjwt) | MIT | |
| [pyobjc-framework-Vision](https://github.com/ronaldoussoren/pyobjc) | MIT | macOS only, on-device OCR |
| [pywebview](https://github.com/r0x0r/pywebview) | BSD-3-Clause | desktop window (optional extra) |
| [pystray](https://github.com/moses-palmer/pystray) | LGPL-3.0 | tray icon (optional extra) |
| [Pillow](https://github.com/python-pillow/Pillow) | MIT-CMU (HPND) | (optional extra) |

Build-only: [PyInstaller](https://github.com/pyinstaller/pyinstaller) (GPL-2.0-or-later with the bootloader exception, which allows bundling applications under any license).

## Bundled JavaScript

The note editor bundle `src/papermap/web/static/vendor/papermap-editor/` contains Tiptap 3.32.2 and ProseMirror
packages (all MIT); the full list is in
[`src/papermap/web/static/vendor/papermap-editor/LICENSES.txt`](src/papermap/web/static/vendor/papermap-editor/LICENSES.txt).

## Data services

PaperMap queries public APIs at the user's request: Crossref, OpenAlex, NCBI E-utilities (PubMed), bioRxiv / medRxiv,
the PMC Open Access subset, and the translation services the user configures. Their terms of use apply to that data.

## Skills

The built-in `papermap-close-reading` skill is part of PaperMap. Third-party skills (for example `nature-paper-xray`)
are **not** distributed with PaperMap; users install them into their own data folder.
