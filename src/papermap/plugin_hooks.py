"""At-least-once delivery of library events to approved Python plugin versions."""
from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import asynccontextmanager

from . import plugins
from .extension_events import event_page, latest_event_id

log = logging.getLogger(__name__)


class PluginHooks:
    def __init__(self, config, registry):
        self.config = config
        self.registry = registry
        self.lock = threading.RLock()
        self.delivery_lock = threading.Lock()
        self.stopping = threading.Event()
        self.running = threading.Event()
        registry.execute("""
            CREATE TABLE IF NOT EXISTS plugin_cursors (
                plugin_id TEXT PRIMARY KEY, digest TEXT NOT NULL, cursor INTEGER NOT NULL,
                error TEXT NOT NULL DEFAULT '', last_result TEXT NOT NULL DEFAULT ''
            )
        """)

    def _track(self, plugin_id, digest, start):
        self.registry.execute("""
            INSERT INTO plugin_cursors(plugin_id, digest, cursor) VALUES (?, ?, ?)
            ON CONFLICT(plugin_id) DO UPDATE SET digest=excluded.digest,
                cursor=excluded.cursor, error='', last_result='' WHERE digest != excluded.digest
        """, (plugin_id, digest, start))

    def enable(self, plugin_id, enabled, digest):
        with self.lock:
            if enabled:
                candidate = next((p for p in plugins.discover(self.config) if p.id == plugin_id), None)
                if candidate and candidate.python and candidate.events and candidate.digest and candidate.digest == digest:
                    # Persist the starting position before the package becomes executable.
                    self._track(plugin_id, digest, latest_event_id(self.registry))
            plugin = plugins.set_enabled(self.config, plugin_id, enabled, trust_digest=digest)
            if not (plugin.trusted and plugin.events):
                self.registry.execute("DELETE FROM plugin_cursors WHERE plugin_id=?", (plugin_id,))

    def states(self):
        return {row["plugin_id"]: dict(row) for row in self.registry.query("SELECT * FROM plugin_cursors")}

    def retry(self, plugin_id):
        with self.lock:
            self.registry.execute("UPDATE plugin_cursors SET error='' WHERE plugin_id=?", (plugin_id,))

    def reconcile(self):
        """Repair interrupted enable/disable operations, including idle and paused hooks."""
        with self.lock:
            start = latest_event_id(self.registry)
            discovered = plugins.discover(self.config)
            if any(not p.id and p.error for p in discovered):
                return  # Preserve cursors until unavailable storage is readable again.
            active = {p.id: p.digest for p in discovered if p.trusted and p.events}
            for plugin_id, row in self.states().items():
                if plugin_id not in active:
                    self.registry.execute("DELETE FROM plugin_cursors WHERE plugin_id=? AND digest=?", (plugin_id, row["digest"]))
            for plugin_id, digest in active.items():
                self._track(plugin_id, digest, start)

    def deliver(self):
        if not self.delivery_lock.acquire(blocking=False):
            return
        try:
            eligible = self.registry.query("SELECT * FROM plugin_cursors WHERE error='' ORDER BY plugin_id")
            current = None
            for row in eligible:
                if self.stopping.is_set():
                    break
                events = event_page(self.registry, row["cursor"], 20)
                if not events:
                    continue
                if current is None:
                    current = {p.id: p for p in plugins.discover(self.config)}
                plugin = current.get(row["plugin_id"])
                if plugin and plugin.trusted and plugin.digest == row["digest"]:
                    self._deliver_page(row, plugin, events)
        finally:
            self.delivery_lock.release()

    def _deliver_page(self, row, plugin, events):
        cursor = row["cursor"]
        for event in events:
            if self.stopping.is_set():
                break
            result = None
            if event["type"] in plugin.events:
                self.running.set()
                try:
                    reply = plugins.run_python(self.config, plugin.id, event, expected_digest=plugin.digest)
                    result = json.dumps(reply, ensure_ascii=False)[:1000]
                except Exception:
                    # An arbitrary plugin exception can contain document text or credentials.
                    self.registry.execute("""
                        UPDATE plugin_cursors SET error=? WHERE plugin_id=? AND digest=? AND cursor=?
                    """, (f"事件 #{event['id']} 处理失败。检查插件后可以重试。", plugin.id, plugin.digest, cursor))
                    break
                finally:
                    self.running.clear()
            updated = self.registry.execute("""
                UPDATE plugin_cursors SET cursor=?, last_result=COALESCE(?, last_result)
                WHERE plugin_id=? AND digest=? AND cursor=?
            """, (event["id"], result, plugin.id, plugin.digest, cursor)).rowcount
            if not updated:
                break
            cursor = event["id"]
            if result is not None:
                # Give every other subscription a turn before starting more processes.
                break

    def _run(self):
        next_scan = 0.0
        while not self.stopping.is_set():
            try:
                if time.monotonic() >= next_scan:
                    self.reconcile()
                    next_scan = time.monotonic() + 30
                self.deliver()
            except Exception:
                log.exception("Could not deliver plugin events")
            self.stopping.wait(1)

    @asynccontextmanager
    async def lifespan(self, app):
        import asyncio

        self.stopping.clear()
        thread = threading.Thread(target=self._run, name="papermap-plugin-events", daemon=True)
        thread.start()
        try:
            yield
        finally:
            self.stopping.set()
            await asyncio.to_thread(thread.join)
