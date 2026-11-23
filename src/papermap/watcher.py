"""Folder watching: watchdog events + debounce + startup full scan."""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import Config
from .pipeline import Pipeline, is_candidate_file

log = logging.getLogger(__name__)


def iter_inbox_pdfs(config: Config) -> Iterator[Path]:
    """Recursively yield candidate PDFs in all inbox folders (startup scan)."""
    excluded = {d.lower() for d in config.exclude_dir_names}
    for inbox in config.inbox_dirs:
        try:
            os.listdir(inbox)
        except OSError:
            continue
        if not inbox.is_dir():
            log.warning("Inbox folder does not exist: %s", inbox)
            continue
        for root, dirs, files in os.walk(inbox):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d.lower() not in excluded)
            for name in sorted(files):
                p = Path(root) / name
                if is_candidate_file(p, config):
                    yield p


class _Handler(FileSystemEventHandler):
    def __init__(self, on_path: Callable[[str], None]):
        self.on_path = on_path

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.on_path(str(event.src_path))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.on_path(str(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        # Browsers download to x.pdf.crdownload, then rename to x.pdf
        if not event.is_directory:
            self.on_path(str(event.dest_path))


class Debouncer:
    """Emit a path once its size/mtime has been stable for `quiet_s` seconds."""

    def __init__(self, emit: Callable[[Path], None], quiet_s: float = 3.0, poll_s: float = 1.0):
        self.emit = emit
        self.quiet_s = quiet_s
        self.poll_s = poll_s
        self._pending: dict[str, tuple[tuple[int, float] | None, float]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="debouncer", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def touch(self, path: str) -> None:
        with self._lock:
            self._pending[path] = (None, time.monotonic())

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_s):
            self.check_once()

    def check_once(self) -> None:
        ready: list[str] = []
        now = time.monotonic()
        with self._lock:
            for path, (last_sig, since) in list(self._pending.items()):
                try:
                    st = os.stat(path)
                    sig = (st.st_size, st.st_mtime)
                except OSError:
                    self._pending.pop(path, None)  # deleted or renamed away
                    continue
                if sig != last_sig:
                    self._pending[path] = (sig, now)
                elif now - since >= self.quiet_s and st.st_size > 0:
                    ready.append(path)
                    self._pending.pop(path, None)
        for path in ready:
            self.emit(Path(path))


class Watcher:
    """Runs the observer, the debouncer and a single worker thread."""

    def __init__(self, config: Config, pipeline: Pipeline):
        self.config = config
        self.pipeline = pipeline
        self.queue: queue.Queue[Path | None] = queue.Queue()
        self.debouncer = Debouncer(self._enqueue, quiet_s=float(config.raw.get("debounce_seconds", 3)))
        self.observer = Observer()
        self._worker = threading.Thread(target=self._work, name="papermap-worker", daemon=True)
        self._queued: set[str] = set()
        self._qlock = threading.Lock()

    def _enqueue(self, path: Path) -> None:
        if not is_candidate_file(path, self.config):
            return
        key = str(path)
        with self._qlock:
            if key in self._queued:
                return
            self._queued.add(key)
        self.queue.put(path)

    def _on_event(self, path: str) -> None:
        if path.lower().endswith(".pdf"):
            self.debouncer.touch(path)

    def _work(self) -> None:
        while True:
            path = self.queue.get()
            if path is None:
                break
            with self._qlock:
                self._queued.discard(str(path))
            outcome = self.pipeline.handle_path(path)
            log.info("%s -> %s", path.name, outcome)

    def start(self) -> None:
        handler = _Handler(self._on_event)
        self.denied: list[Path] = []
        for inbox in self.config.inbox_dirs:
            # Touch the folder first with a plain listdir: on macOS this is where a
            # privacy prompt (Documents/Downloads/Desktop) waits for the user, and
            # listdir does it without holding Python's global lock. watchdog's
            # FSEvents module would block every thread of the app instead.
            try:
                os.listdir(inbox)
            except FileNotFoundError:
                self.pipeline.registry.log("warning", f"Inbox folder does not exist: {inbox}")
                continue
            except PermissionError:
                self.denied.append(inbox)
                self.pipeline.registry.log(
                    "error", f"No permission to read {inbox}. On macOS allow it in System Settings → Privacy & "
                    "Security → Files and Folders → PaperMap (文件与文件夹), then restart PaperMap.")
                continue
            self.observer.schedule(handler, str(inbox), recursive=True)
            log.info("Watching %s", inbox)
        self.observer.start()
        self.debouncer.start()
        self._worker.start()
        # Startup catch-up scan (files added while we were not running). Papers
        # are only registered first; if many are new, the LLM step waits for
        # the user to confirm the cost estimate on the web page.
        self.pipeline.hold = True
        for p in iter_inbox_pdfs(self.config):
            self._enqueue(p)
        threading.Thread(target=self._pending_after_scan, daemon=True).start()

    def _pending_after_scan(self) -> None:
        while not self.queue.empty():
            time.sleep(1)
        with self.pipeline._lock:  # wait for the file being processed
            pass
        threshold = int(self.config.raw.get("bulk_confirm_threshold", 5))
        n = self.pipeline.pending_count()
        if n >= threshold:
            est = self.pipeline.estimate(n)
            self.pipeline.registry.log(
                "warning",
                f"{n} new papers are waiting. Estimated LLM cost ${est['cost_offpeak']:.3f}-${est['cost_peak']:.3f}; "
                "confirm on the home page to start.")
            return  # stays on hold until confirmed
        self.pipeline.hold = False
        self.pipeline.process_pending()

    def stop(self) -> None:
        self.debouncer.stop()
        self.observer.stop()
        self.observer.join(timeout=5)
        self.queue.put(None)
