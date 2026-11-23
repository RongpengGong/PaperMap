"""Versioned local plugins. Explicit approvals are bound to the complete package."""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import Config

PLUGIN_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")
SUPPORTED_EVENTS = ("paper.ready", "field.proposed")
MAX_FILES = 256
MAX_BYTES = 8 * 1024 * 1024
MAX_MESSAGE = 1024 * 1024
TIMEOUT = 10
_WORKERS = threading.BoundedSemaphore(4)
_IGNORED = {".DS_Store", ".git", "__pycache__"}
_STATE_SCHEMA = "CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, digest TEXT NOT NULL)"


class PluginError(ValueError):
    pass


@dataclass
class Plugin:
    id: str
    root: Path
    name: str = ""
    description: str = ""
    version: str = ""
    python: str = ""
    permissions: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    contributions: dict = field(default_factory=dict)
    prompt_files: dict[str, str] = field(default_factory=dict, repr=False)
    digest: str = ""
    enabled: bool = False
    trusted: bool = False
    error: str = ""

    @property
    def path(self) -> Path:
        return self.root


def _linked(path: Path) -> bool:
    # NTFS junctions and other reparse points can leave the package just like symlinks.
    if path.is_symlink():
        return True
    if os.name == "nt" and path.exists():
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def _plugin_dir(config: Config) -> Path:
    root = config.data_dir / "plugins"
    if _linked(root) or (root.exists() and not root.is_dir()):
        raise PluginError("插件目录必须是本机文件夹。")
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    """Capture the exact bytes used for both approval and execution or install."""
    if _linked(root) or not root.is_dir():
        raise PluginError("插件必须是文件夹，不能使用符号链接。")
    files: dict[str, bytes] = {}
    total = entries = 0

    def visit(folder: Path) -> None:
        nonlocal total, entries
        for path in folder.iterdir():
            if path.name in _IGNORED:
                continue
            entries += 1
            if entries > MAX_FILES:
                raise PluginError(f"插件超过 {MAX_FILES} 个文件或子目录。")
            mode = path.lstat().st_mode
            if path.name.startswith(".") or _linked(path):
                raise PluginError("插件包中不能包含隐藏文件或符号链接。")
            if stat.S_ISDIR(mode):
                visit(path)
                continue
            if not stat.S_ISREG(mode):
                raise PluginError("插件包中只能包含普通文件。")
            with path.open("rb") as f:
                data = f.read(MAX_BYTES - total + 1)
            total += len(data)
            if total > MAX_BYTES:
                raise PluginError("插件包超过 8 MiB。")
            files[path.relative_to(root).as_posix()] = data

    visit(root)
    return files


def _digest(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode("utf8") + b"\x00")
        h.update(hashlib.sha256(files[name]).digest())
    return h.hexdigest()


def _strings(data: dict, key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(not isinstance(s, str) or not s.strip() for s in value):
        raise PluginError(f"{key} 需要是非空文本组成的列表。")
    return tuple(dict.fromkeys(value))


def _inspect(root: Path, approved: str = "") -> tuple[Plugin, dict[str, bytes]]:
    plugin = Plugin(id=root.name, root=root, name=root.name)
    try:
        if not PLUGIN_ID.fullmatch(root.name):
            raise PluginError("插件目录名必须是小写英文标识符。")
        files = _snapshot(root)
        if "plugin.yaml" not in files or len(files["plugin.yaml"]) > 65536:
            raise PluginError("需要提供小于等于 64 KiB 的 plugin.yaml。")
        raw = files["plugin.yaml"].decode("utf8")
        # A manifest is a small JSON-like tree. Reject alias expansion and recursion.
        if any(isinstance(t, yaml.tokens.AliasToken) for t in yaml.scan(raw)):
            raise PluginError("plugin.yaml 不支持 YAML 别名。")
        data = yaml.safe_load(raw)
        if not isinstance(data, dict) or type(data.get("api_version")) is not int or data["api_version"] != 1:
            raise PluginError("需要 api_version: 1 格式的插件清单。")
        if data.get("id") != root.name:
            raise PluginError("清单 id 必须与插件文件夹名称相同。")
        for key in ("name", "description", "version", "python"):
            value = data.get(key, "")
            if not isinstance(value, str) or (key == "name" and not value.strip()):
                raise PluginError(f"{key} 需要是文本。")
            setattr(plugin, key, value.strip())
        if plugin.python and (plugin.python not in files or not plugin.python.endswith(".py")):
            raise PluginError("python 入口必须是包内存在的 .py 文件。")
        plugin.permissions = _strings(data, "permissions")
        plugin.events = _strings(data, "events")
        if plugin.events and (not plugin.python or not set(plugin.events).issubset(SUPPORTED_EVENTS)):
            raise PluginError("events 需要声明 Python 入口，并使用受支持的事件名称。")
        contributions = data.get("contributions", {})
        if not isinstance(contributions, dict) or any(not isinstance(k, str) for k in contributions):
            raise PluginError("contributions 需要是以文本为键的映射。")
        plugin.contributions = contributions
        json.dumps(plugin.contributions, allow_nan=False)
        if plugin.contributions:
            from .plugin_contributions import validate_contributions

            plugin.prompt_files = validate_contributions(root, plugin.contributions, files=files)
        plugin.digest = _digest(files)
        plugin.enabled = bool(approved and approved == plugin.digest)
        plugin.trusted = plugin.enabled and bool(plugin.python)
        return plugin, files
    except (PluginError, OSError, ValueError, TypeError, RecursionError, yaml.YAMLError) as exc:
        plugin.error = str(exc)[:600]
        return plugin, {}


def _state_path(config: Config) -> Path:
    path = config.data_dir / "plugins-state.sqlite3"
    if _linked(path):
        raise PluginError("插件状态文件不能使用符号链接。")
    return path


@contextmanager
def _connection(config: Config):
    config.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(_state_path(config), timeout=5)) as conn, conn:
            conn.execute(_STATE_SCHEMA)
            yield conn
    except sqlite3.Error as exc:
        raise PluginError("无法读写插件启用记录，请检查 plugins-state.sqlite3。") from exc


def _approvals(config: Config) -> dict[str, str]:
    if not _state_path(config).exists():
        return {}
    with _connection(config) as conn:
        return dict(conn.execute("SELECT id, digest FROM approvals"))


def discover(config: Config) -> list[Plugin]:
    try:
        root = _plugin_dir(config)
        if not root.exists():
            return []
        approved = _approvals(config)
        folders = sorted(root.iterdir())
    except (OSError, PluginError) as exc:
        # Keep the library and its built-in providers usable when optional storage is damaged.
        return [Plugin(id="", root=config.data_dir / "plugins", name="插件目录", error=str(exc))]
    return [_inspect(p, approved.get(p.name, ""))[0] for p in folders
            if not p.name.startswith(".") and (p.is_dir() or p.is_symlink())]


# A rendered page may ask separately for its providers, translations, skills and
# exports. Reuse approved *data* for that render only. Execution always obtains
# a fresh verified snapshot through _find, even during a render.
_render_cache: ContextVar[dict | None] = ContextVar("plugin_render_cache", default=None)


@contextmanager
def render_snapshot():
    token = _render_cache.set({})
    try:
        yield
    finally:
        _render_cache.reset(token)


def enabled_plugins(config: Config, only: str | None = None) -> list[Plugin]:
    try:
        approved = _approvals(config)
    except (OSError, PluginError):
        return []
    cache = _render_cache.get()
    result = []
    for pid in sorted(approved) if only is None else (only,):
        if pid not in approved:
            continue
        key = (config.data_dir, pid, approved[pid])
        if cache is not None and key in cache:
            plugin = cache[key]
        else:
            try:
                plugin = _find(config, pid, approved[pid])[0]
            except (OSError, PluginError):
                plugin = None
            if cache is not None:
                cache[key] = plugin
        if plugin is not None and plugin.enabled:
            result.append(plugin)
    return result


def _find(config: Config, plugin_id: str, approved: str = "") -> tuple[Plugin, dict[str, bytes]]:
    if not isinstance(plugin_id, str) or not PLUGIN_ID.fullmatch(plugin_id):
        raise PluginError("插件 id 不合法。")
    plugin, files = _inspect(_plugin_dir(config) / plugin_id, approved)
    if plugin.error:
        raise PluginError(plugin.error)
    return plugin, files


def set_enabled(config: Config, plugin_id: str, enabled: bool, *, trust_digest: str = "") -> Plugin:
    if not isinstance(plugin_id, str) or not PLUGIN_ID.fullmatch(plugin_id):
        raise PluginError("插件 id 不合法。")
    plugin, _ = _inspect(_plugin_dir(config) / plugin_id)
    if enabled and plugin.error:
        raise PluginError(plugin.error)
    if enabled and (plugin.python or trust_digest) and trust_digest != plugin.digest:
        raise PluginError("请核对权限，并重新确认要启用的插件版本。")
    with _connection(config) as conn:
        if enabled:
            conn.execute("INSERT OR REPLACE INTO approvals VALUES (?, ?)", (plugin_id, plugin.digest))
        else:
            conn.execute("DELETE FROM approvals WHERE id=?", (plugin_id,))
    plugin.enabled = bool(enabled)
    plugin.trusted = bool(enabled and plugin.python)
    return plugin


def _write_package(folder: Path, files: dict[str, bytes]) -> None:
    folder.mkdir(exist_ok=True)
    for name, data in files.items():
        dest = folder / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)


def install_plugin(config: Config, src: Path) -> Plugin:
    src = Path(src).expanduser()
    plugin, files = _inspect(src)
    if plugin.error:
        raise PluginError(plugin.error)
    root = _plugin_dir(config)
    root.mkdir(parents=True, exist_ok=True)
    dest = root / plugin.id
    with tempfile.TemporaryDirectory(dir=root, prefix=".install-") as tmp:
        staged = Path(tmp) / plugin.id
        _write_package(staged, files)
        with _connection(config) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if dest.exists() or dest.is_symlink():
                raise PluginError("已安装同名插件。")
            # Revoke any approval from a previously removed package before publication.
            # A crash between the database commit and rename leaves it disabled.
            conn.execute("DELETE FROM approvals WHERE id=?", (plugin.id,))
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            if dest.exists() or dest.is_symlink():
                raise PluginError("已安装同名插件。")
            staged.rename(dest)
    plugin.root = dest
    return plugin


def _terminate(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass
    proc.wait()


def run_python(config: Config, plugin_id: str, event: dict, *, expected_digest: str = ""):
    """Run a trusted snapshot in a child process. This is not an OS sandbox."""
    plugin, files = _find(config, plugin_id, _approvals(config).get(plugin_id, ""))
    if not plugin.trusted or not plugin.python:
        raise PluginError("Python 插件未启用，或文件已修改，需要重新信任。")
    if expected_digest and plugin.digest != expected_digest:
        raise PluginError("插件版本已变更，已跳过原版本排队的事件。")
    if not isinstance(event, dict):
        raise PluginError("插件事件需要是 JSON 对象。")
    body = json.dumps(event, ensure_ascii=False, allow_nan=False).encode("utf8")
    if len(body) > MAX_MESSAGE:
        raise PluginError("插件输入超过 1 MiB。")
    if not _WORKERS.acquire(blocking=False):
        raise PluginError("插件正在运行，请稍后重试。")
    try:
        return _execute(plugin, files, body)
    finally:
        _WORKERS.release()


def _execute(plugin: Plugin, files: dict[str, bytes], body: bytes):
    with tempfile.TemporaryDirectory(prefix="papermap-plugin-") as tmp:
        base = Path(tmp)
        package = base / "package"
        _write_package(package, files)
        request = base / "request.json"
        response = base / "response.json"
        request.write_bytes(body)
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--plugin-worker"]
        else:
            command = [sys.executable, "-I", str(Path(__file__).with_name("plugin_worker.py"))]
        command.extend([str(package / plugin.python), str(request), str(response)])
        env_keys = {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "APPDATA", "LOCALAPPDATA",
                    "SYSTEMDRIVE", "COMSPEC", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL"}
        env = {k: v for k, v in os.environ.items() if k.upper() in env_keys or k.startswith("_PYI_")}
        proc = subprocess.Popen(command, cwd=package, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=os.name == "posix")
        try:
            proc.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            _terminate(proc)
            raise PluginError("插件运行超时，已终止。") from exc
        if proc.returncode or response.is_symlink() or not response.is_file():
            raise PluginError("插件进程未正常返回。")
        with response.open("rb") as f:
            data = f.read(MAX_MESSAGE + 1)
        if len(data) > MAX_MESSAGE:
            raise PluginError("插件输出超过 1 MiB。")
        try:
            result = json.loads(data)
            if not isinstance(result, dict) or "ok" not in result:
                raise ValueError
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise PluginError("插件未返回有效的 JSON 结果。") from exc
        if not result["ok"]:
            raise PluginError(str(result.get("error", "插件执行失败。"))[:600])
        return result.get("value")
