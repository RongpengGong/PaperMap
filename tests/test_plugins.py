import concurrent.futures
import os

import pytest
import yaml

from papermap import plugins


def symlink(path, target, *, directory=False):
    try:
        path.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        if os.name == "nt" and exc.winerror == 1314:
            pytest.skip("Windows requires Developer Mode or the create-symlink privilege")
        raise


def make_plugin(root, plugin_id="demo", *, source=None, **extra):
    folder = root / plugin_id
    folder.mkdir(parents=True)
    manifest = dict(api_version=1, id=plugin_id, name="演示", version="1.0", **extra)
    if source is not None:
        manifest["python"] = "main.py"
        (folder / "main.py").write_text(source, encoding="utf8")
    (folder / "plugin.yaml").write_text(yaml.safe_dump(manifest), encoding="utf8")
    return folder


def test_python_requires_trust_and_executes_only_approved_snapshot(config, tmp_path):
    marker = tmp_path / "executed"
    source = f"from pathlib import Path\nfrom helper import identity\nPath({str(marker)!r}).touch()\ndef handle(event): return identity(event)\n"
    folder = make_plugin(config.data_dir / "plugins", source=source, permissions=["读取论文元数据"], events=["paper.ready"])
    helper = folder / "helper.py"
    helper.write_text("def identity(event): return event\n", encoding="utf8")
    plugin, = plugins.discover(config)
    assert not marker.exists() and not plugin.enabled
    assert plugin.events == ("paper.ready",)
    with pytest.raises(plugins.PluginError):
        plugins.run_python(config, plugin.id, {})
    with pytest.raises(plugins.PluginError):
        plugins.set_enabled(config, plugin.id, True)
    assert plugins.set_enabled(config, plugin.id, True, trust_digest=plugin.digest).trusted
    assert not marker.exists()
    assert plugins.run_python(config, plugin.id, {"paper": "论文"}) == {"paper": "论文"}
    assert marker.is_file()
    helper.write_text("def identity(event): return 9\n", encoding="utf8")
    changed, = plugins.discover(config)
    assert changed.digest != plugin.digest and not changed.enabled and not changed.trusted
    with pytest.raises(plugins.PluginError):
        plugins.set_enabled(config, plugin.id, True, trust_digest=plugin.digest)
    with pytest.raises(plugins.PluginError):
        plugins.run_python(config, plugin.id, {})
    plugins.set_enabled(config, plugin.id, True, trust_digest=changed.digest)
    with pytest.raises(plugins.PluginError, match="版本已变更"):
        plugins.run_python(config, plugin.id, {}, expected_digest=plugin.digest)
    plugins.set_enabled(config, plugin.id, False)
    assert not plugins.discover(config)[0].enabled


def test_declarative_install_is_disabled_and_atomic(config, tmp_path):
    source = make_plugin(tmp_path)
    plugin = plugins.install_plugin(config, source)
    assert plugin.root == config.data_dir / "plugins" / "demo"
    assert not plugin.enabled
    (source / "plugin.yaml").unlink()
    enabled = plugins.set_enabled(config, "demo", True)
    assert enabled.enabled and not enabled.trusted
    assert plugins.discover(config)[0].enabled
    with pytest.raises(plugins.PluginError):
        plugins.install_plugin(config, plugin.root)
    assert plugins.discover(config)[0].enabled
    (plugin.root / "unexpected.txt").write_text("changed")
    assert not plugins.discover(config)[0].enabled


def test_invalid_tampered_plugin_can_be_explicitly_disabled(config):
    folder = make_plugin(config.data_dir / "plugins")
    plugins.set_enabled(config, "demo", True)
    manifest = folder / "plugin.yaml"
    old = manifest.read_bytes()
    manifest.write_text("broken: [")
    assert plugins.discover(config)[0].error
    plugins.set_enabled(config, "demo", False)
    manifest.write_bytes(old)
    assert not plugins.discover(config)[0].enabled


def test_install_checks_symlinks_and_path_escape(config, tmp_path):
    folder = make_plugin(tmp_path, source="def handle(event): return event")
    symlink(folder / "external.txt", tmp_path / "elsewhere")
    with pytest.raises(plugins.PluginError):
        plugins.install_plugin(config, folder)
    (folder / "external.txt").unlink()
    path = folder / "plugin.yaml"
    data = yaml.safe_load(path.read_text())
    data["python"] = "../main.py"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(plugins.PluginError):
        plugins.install_plugin(config, folder)
    assert plugins.discover(config) == []


def test_worker_failure_and_timeout_do_not_disable_other_extensions(config, monkeypatch):
    make_plugin(config.data_dir / "plugins", "slow", source="import time\ndef handle(event): time.sleep(5)")
    make_plugin(config.data_dir / "plugins", "bad", source="def handle(event): return set()")
    for plugin in plugins.discover(config):
        plugins.set_enabled(config, plugin.id, True, trust_digest=plugin.digest)
    monkeypatch.setattr(plugins, "TIMEOUT", 0.1)
    with pytest.raises(plugins.PluginError, match="超时"):
        plugins.run_python(config, "slow", {})
    monkeypatch.setattr(plugins, "TIMEOUT", 5)
    with pytest.raises(plugins.PluginError, match="JSON serializable"):
        plugins.run_python(config, "bad", {})


def test_concurrent_approvals_are_persistent(config):
    root = config.data_dir / "plugins"
    ids = [f"plug{i}" for i in range(6)]
    for name in ids:
        make_plugin(root, name)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        assert all(p.enabled for p in pool.map(lambda k: plugins.set_enabled(config, k, True), ids))
    assert {p.id for p in plugins.discover(config) if p.enabled} == set(ids)


def test_invalid_storage_only_disables_plugins(config, tmp_path):
    external = tmp_path / "elsewhere"
    external.mkdir()
    root = config.data_dir / "plugins"
    symlink(root, external, directory=True)
    invalid, = plugins.discover(config)
    assert invalid.error and not invalid.enabled
    root.unlink()
    make_plugin(root)
    (config.data_dir / "plugins-state.sqlite3").write_bytes(b"not a database")
    invalid, = plugins.discover(config)
    assert invalid.error and not invalid.enabled
    with pytest.raises(plugins.PluginError):
        plugins.set_enabled(config, "demo", True)
    assert list(external.iterdir()) == []


def test_simultaneous_folder_install_has_single_winner(config, tmp_path):
    source = make_plugin(tmp_path)

    def install():
        try:
            return plugins.install_plugin(config, source).id
        except plugins.PluginError:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        installed = [f.result() for f in [pool.submit(install) for _ in range(3)]]
    assert installed.count("demo") == 1
    assert [p.id for p in plugins.discover(config)] == ["demo"]
    assert not any(p.name.startswith(".install-") for p in (config.data_dir / "plugins").iterdir())
