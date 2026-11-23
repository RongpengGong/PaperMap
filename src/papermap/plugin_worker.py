"""Isolated entry point used only after the parent verifies the plugin's snapshot."""
from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path


def main(args: list[str]) -> int:
    if len(args) != 3:
        return 2
    entry, request, response = map(Path, args)
    try:
        event = json.loads(request.read_bytes())
        sys.dont_write_bytecode = True
        # Windowed executables on Windows expose no Python console streams.
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf8")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf8")
        sys.path[:0] = [str(entry.parent), str(Path.cwd())]
        handler = runpy.run_path(str(entry)).get("handle")
        if not callable(handler):
            raise ValueError("Python 入口需要定义 handle(event) 函数。")
        value = handler(event)
        result = json.dumps({"ok": True, "value": value}, ensure_ascii=False, allow_nan=False).encode("utf8")
        if len(result) > 1024 * 1024:
            raise ValueError("插件输出超过 1 MiB。")
    except Exception as exc:
        result = json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:600]}).encode("utf8")
    response.write_bytes(result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
