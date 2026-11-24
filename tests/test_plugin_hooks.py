import asyncio
import threading

import pytest
import yaml

from papermap import plugins
from papermap.plugin_hooks import PluginHooks


def install_hooks(config, *ids):
    for plugin_id in ids:
        root = config.data_dir / "plugins" / plugin_id
        root.mkdir(parents=True)
        manifest = dict(api_version=1, id=plugin_id, name=plugin_id, version="1.0", python="main.py", events=["paper.ready"])
        (root / "plugin.yaml").write_text(yaml.safe_dump(manifest), encoding="utf8")
        (root / "main.py").write_text("def handle(event): return event['id']\n", encoding="utf8")
    return {p.id: p for p in plugins.discover(config)}


def emit(registry, kind="paper.ready"):
    return registry.execute("INSERT INTO extension_events(type,object_id) VALUES (?,1)", (kind,)).lastrowid


def test_subscribers_have_independent_cursors_and_retries(config, registry, monkeypatch):
    hooks = PluginHooks(config, registry)
    specs = install_hooks(config, "broken", "good")
    old = emit(registry)
    for plugin in specs.values():
        hooks.enable(plugin.id, True, plugin.digest)
    first = emit(registry)
    second = emit(registry)
    calls = []
    fail = True

    def work(cfg, plugin_id, event, *, expected_digest):
        assert expected_digest == specs[plugin_id].digest
        calls.append((plugin_id, event["id"]))
        if plugin_id == "broken" and fail:
            raise ValueError("private paper material")
        return {"handled": event["id"]}

    monkeypatch.setattr(plugins, "run_python", work)
    hooks.deliver()
    states = hooks.states()
    assert states["good"]["cursor"] == first and states["broken"]["cursor"] == old
    assert str(first) in states["broken"]["error"] and "private" not in states["broken"]["error"]
    hooks.deliver()
    assert hooks.states()["good"]["cursor"] == second
    assert calls.count(("broken", first)) == 1
    fail = False
    hooks.retry("broken")
    hooks.deliver()
    hooks.deliver()
    states = hooks.states()
    assert calls.count(("broken", first)) == 2 and ("good", old) not in calls
    assert states["broken"]["cursor"] == states["good"]["cursor"] == second
    assert states["good"]["last_result"] == f'{{"handled": {second}}}'
    skipped = emit(registry, "field.proposed")
    hooks.deliver()
    assert hooks.states()["good"]["cursor"] == skipped
    assert hooks.states()["good"]["last_result"] == states["good"]["last_result"]


def test_reconcile_refreshes_paused_modified_and_removed_packages(config, registry):
    hooks = PluginHooks(config, registry)
    plugin = install_hooks(config, "watcher")["watcher"]
    hooks.enable("watcher", True, plugin.digest)
    registry.execute("UPDATE plugin_cursors SET error='old failure'")
    (plugin.root / "main.py").write_text("def handle(event): return 2", encoding="utf-8")
    hooks.reconcile()
    assert hooks.states() == {}
    current, = plugins.discover(config)
    baseline = emit(registry)
    plugins.set_enabled(config, "watcher", True, trust_digest=current.digest)
    # Recovery also handles grants made through the core API before startup.
    hooks.reconcile()
    assert hooks.states()["watcher"]["cursor"] == baseline
    first = emit(registry)
    PluginHooks(config, registry).deliver()
    assert hooks.states()["watcher"]["cursor"] == first
    assert hooks.states()["watcher"]["last_result"] == "2"
    plugins.set_enabled(config, "watcher", False)
    hooks.reconcile()
    assert not hooks.states()


def test_worker_interruption_replays_unacknowledged_event(config, registry, monkeypatch):
    class Interrupted(BaseException):
        pass

    hooks = PluginHooks(config, registry)
    plugin = install_hooks(config, "watcher")["watcher"]
    hooks.enable("watcher", True, plugin.digest)
    first = emit(registry)
    seen = []

    def work(cfg, plugin_id, event, **kwargs):
        seen.append(event["id"])
        if len(seen) == 1:
            raise Interrupted
        return "ok"

    monkeypatch.setattr(plugins, "run_python", work)
    with pytest.raises(Interrupted):
        hooks.deliver()
    assert not hooks.running.is_set() and hooks.states()["watcher"]["cursor"] == 0
    restarted = PluginHooks(config, registry)
    restarted.reconcile()
    restarted.deliver()
    assert seen == [first, first]
    assert restarted.states()["watcher"]["cursor"] == first


def test_running_delivery_cannot_advance_replaced_subscription(config, registry, monkeypatch):
    hooks = PluginHooks(config, registry)
    old = install_hooks(config, "watcher")["watcher"]
    hooks.enable("watcher", True, old.digest)
    first = emit(registry)
    started, finish = threading.Event(), threading.Event()
    seen = []

    def work(cfg, plugin_id, event, *, expected_digest):
        seen.append(event["id"])
        started.set()
        assert finish.wait(5)
        return "old result"

    monkeypatch.setattr(plugins, "run_python", work)
    thread = threading.Thread(target=hooks.deliver)
    thread.start()
    try:
        assert started.wait(3) and hooks.running.is_set()
        hooks.deliver()
        (old.root / "main.py").write_text("def handle(event): return 'new'", encoding="utf-8")
        updated, = plugins.discover(config)
        hooks.enable(updated.id, True, updated.digest)
    finally:
        finish.set()
        thread.join(3)
    row = hooks.states()["watcher"]
    assert not hooks.running.is_set() and not thread.is_alive()
    assert seen == [first] and row["cursor"] == first and not row["last_result"]
    assert row["digest"] == updated.digest


def test_lifespan_stops_worker_without_delivering_new_events(config, registry):
    hooks = PluginHooks(config, registry)
    plugin = install_hooks(config, "watcher")["watcher"]
    hooks.enable(plugin.id, True, plugin.digest)

    async def start_and_stop():
        async with hooks.lifespan(None):
            await asyncio.sleep(0.015)

    asyncio.run(start_and_stop())
    emit(registry)
    hooks.deliver()
    assert hooks.stopping.is_set() and hooks.states()[plugin.id]["cursor"] == 0
