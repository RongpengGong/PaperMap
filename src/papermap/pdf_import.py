"""Stage a PDF privately, then publish the complete file to the shared inbox."""

from __future__ import annotations

import os
import re
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

_publish_lock = threading.Lock()


@contextmanager
def staged_file(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(dir=folder, prefix=".upload-", suffix=".part")
    os.close(fd)
    tmp = Path(path)
    try:
        yield tmp
    finally:
        tmp.unlink(missing_ok=True)


def publish_pdf(tmp: Path, name: str) -> Path:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", Path(name).name).strip(" .") or "paper.pdf"
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    stem = stem.encode("utf-8")[:220].decode("utf-8", errors="ignore")
    with _publish_lock:
        dest = tmp.with_name(stem + ".pdf")
        index = 2
        while dest.exists():
            dest = tmp.with_name(f"{stem} ({index}).pdf")
            index += 1
        os.replace(tmp, dest)
    return dest
