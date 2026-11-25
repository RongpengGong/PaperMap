"""Use the official Antigravity CLI as an ACP v1 client over private stdio pipes."""

from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager, suppress
from copy import deepcopy
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

from .llm import BudgetExceeded, LLMError, LLMNotConfigured


INSTALL_URL = "https://antigravity.google/docs/getting-started?tab=cli"
_LOGIN_HINT = "请在官方终端运行 agy acp --login，完成 Google 账号登录后刷新。"
_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
_API_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI",
            "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT",
            "GOOGLE_GEMINI_BASE_URL", "GOOGLE_VERTEX_BASE_URL")
_NO_ACTIONS = ("Answer the PaperMap literature task using only the supplied messages and images. "
               "Do not use tools, read files, run commands, or browse the web.")


def find_agy(executable: str | None = None) -> str | None:
    name = "agy.exe" if os.name == "nt" else "agy"
    if executable:
        candidates = [Path(executable).expanduser()]
    else:
        found = shutil.which(name)
        candidates = ([Path(found)] if found else []) + [
            Path.home() / ".local/bin" / name, Path.home() / ".gemini/bin" / name,
            Path("/opt/homebrew/bin") / name, Path("/usr/local/bin") / name,
        ]
        if os.name == "nt" and os.getenv("LOCALAPPDATA"):
            local = Path(os.environ["LOCALAPPDATA"])
            candidates.extend([local / "agy/bin" / name, local / "Microsoft/WinGet/Links" / name])
    for path in candidates:
        if path.suffix.lower() not in {".cmd", ".bat"} and path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
    return None


def _env():
    return {key: value for key, value in os.environ.items() if key not in _API_ENV}


def _rpc_error(error: dict, method: str):
    # Some CLI errors include the request payload. Never expose raw output in the UI.
    message = str(error.get("message", "")).lower()
    if error.get("code") == -32000 or any(x in message for x in ("authentication required", "unauthenticated", "not logged in", "token has expired")):
        raise LLMNotConfigured(_LOGIN_HINT)
    if any(x in message for x in ("resource_exhausted", "quota", "rate limit", "429")):
        raise BudgetExceeded("Antigravity 额度或速率已达上限，请在官方客户端检查额度后重试。")
    if method in ("session/set_config_option", "session/set_model"):
        raise LLMError("Antigravity 拒绝了所选模型或思考档位，请刷新模型列表后重新选择。")
    if any(x in message for x in ("forbidden", "permission_denied", "403")):
        raise LLMError("此账号暂时无法完成该请求，请检查官方 Antigravity 中的模型权限。")
    raise LLMError("Antigravity CLI 未完成请求，请检查官方 CLI 的网络、账号及版本。")


def _kill(proc):
    if os.name == "posix":
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    else:
        with suppress(OSError):
            proc.kill()


class _Connection:
    def __init__(self, proc, cwd):
        self.proc = proc
        self.cwd = cwd
        self.last_id = 0

    def _send(self, frame):
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", **frame}, ensure_ascii=False) + "\n").encode())
        self.proc.stdin.flush()

    def frames(self, method, params):
        """Yield session notifications and the matching result in their wire order."""
        self.last_id += 1
        request_id = self.last_id
        self._send({"id": request_id, "method": method, "params": params})
        for line in self.proc.stdout:
            try:
                frame = json.loads(line)
            except (UnicodeError, ValueError):
                raise LLMError("Antigravity CLI 返回了无效的 ACP 消息，请检查 CLI 版本。") from None
            if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0":
                raise LLMError("Antigravity CLI 返回了无法识别的 ACP 消息。")
            if "method" in frame:
                if "id" in frame:
                    if frame["method"] == "session/request_permission":
                        reply = {"result": {"outcome": {"outcome": "cancelled"}}}
                    else:
                        reply = {"error": {"code": -32601, "message": "Method not supported by PaperMap"}}
                    self._send({"id": frame["id"], **reply})
                elif frame["method"] == "session/update":
                    yield frame
            elif frame.get("id") == request_id:
                if isinstance(frame.get("error"), dict):
                    _rpc_error(frame["error"], method)
                if not isinstance(frame.get("result"), dict):
                    raise LLMError("Antigravity CLI 缺少 ACP 响应结果。")
                yield frame
                return
        raise LLMError("Antigravity CLI 提前退出，请检查 CLI 版本和账号状态。")

    def call(self, method, params):
        for frame in self.frames(method, params):
            if "result" in frame:
                return frame["result"]

    def open(self):
        init = self.call("initialize", {
            "protocolVersion": 1, "clientInfo": {"name": "PaperMap", "version": "1"},
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
        })
        if init.get("protocolVersion") != 1:
            raise LLMError("Antigravity CLI 使用了不兼容的 ACP 版本。")
        session = self.call("session/new", {"cwd": self.cwd, "mcpServers": []})
        if not isinstance(session.get("sessionId"), str) or not session["sessionId"]:
            raise LLMError("Antigravity CLI 未返回会话标识。")
        return init, session


@contextmanager
def _connection(executable, timeout):
    exe = find_agy(executable)
    if not exe:
        raise LLMNotConfigured("未找到 Google 官方 Antigravity CLI（agy），请先安装。")
    with tempfile.TemporaryDirectory(prefix="papermap-agy-") as cwd:
        try:
            proc = subprocess.Popen(
                [exe, "acp", "--sandbox", "--include-default-customizations=false"],
                cwd=cwd, env=_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=os.name == "posix",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError:
            raise LLMNotConfigured("无法启动 Antigravity CLI，请检查官方 CLI 安装。") from None
        deadline = time.monotonic() + timeout
        timer = threading.Timer(timeout, _kill, args=(proc,))
        timer.daemon = True
        timer.start()
        try:
            yield _Connection(proc, cwd)
        except (OSError, LLMError) as exc:
            if time.monotonic() >= deadline:
                raise LLMError("Antigravity 请求超时，本次 CLI 进程已停止。") from None
            if isinstance(exc, OSError):
                raise LLMError("Antigravity CLI 通信中断，请检查 CLI 后重试。") from None
            raise
        finally:
            timer.cancel()
            timer.join()
            with suppress(OSError):
                proc.stdin.close()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                _kill(proc)
                proc.wait()
            proc.stdout.close()


def _option(session, name):
    options = session.get("configOptions") or []
    return next((row for row in options if isinstance(row, dict) and row.get("id") == name), {})


def _values(option):
    return [row for row in option.get("options", []) if isinstance(row, dict) and isinstance(row.get("value"), str)]


def _tier(session):
    return ((session.get("_meta") or {}).get("antigravity") or {}).get("tier") or {}


def _check_tier(tier):
    name = str(tier.get("name", "")).lower()
    tier_id = str(tier.get("id", "")).lower()
    if not name and not tier_id:
        raise LLMNotConfigured("官方 CLI 尚未报告个人账号的套餐。" + _LOGIN_HINT)
    if any(word in name + " " + tier_id for word in (
        "enterprise", "business", "vertex", "pay as you go", "pay-as-you-go", "gemini-api", "api key", "workspace",
    )):
        raise LLMNotConfigured("Antigravity 正在使用企业或 API 账号，请在官方 CLI 切换到个人 Google 账号。")


def _catalog(session):
    advertised = (session.get("models") or {}).get("availableModels", session.get("availableModels", []))
    variants = {row["modelId"]: row.get("name") or row["modelId"] for row in advertised
                if isinstance(row, dict) and isinstance(row.get("modelId"), str)}
    model_option = _option(session, "model")
    choices = _values(model_option) or [{"value": key, "name": name} for key, name in variants.items()]
    thought = _option(session, "thought_level")
    current = model_option.get("currentValue") or (session.get("models") or {}).get("currentModelId")
    rows = []
    for choice in choices:
        model = choice["value"]
        levels = [level for level in _EFFORTS if model + "-" + level in variants]
        default = ""
        is_current = current == model or any(current == model + "-" + level for level in levels)
        if is_current:
            levels = list(dict.fromkeys([*levels, *(row["value"] for row in _values(thought))]))
            default = thought.get("currentValue") or ""
        rows.append({"id": model, "label": choice.get("name") or model, "is_current": is_current, "reasoning_efforts": levels,
                     "reasoning_efforts_known": True, "default_reasoning_effort": default})
    return rows


def _select(conn, session, model, effort):
    params = {"sessionId": session["sessionId"]}
    model_option = _option(session, "model")
    current = model_option.get("currentValue") or (session.get("models") or {}).get("currentModelId") or "auto"
    selected = current if model == "auto" else model
    updated = session
    if selected != current:
        if model_option:
            updated = conn.call("session/set_config_option", {**params, "configId": "model", "value": selected})
        else:
            updated = conn.call("session/set_model", {**params, "modelId": selected})
    if effort:
        allowed = _values(_option(updated, "thought_level"))
        if allowed and effort not in {entry["value"] for entry in allowed}:
            row = next((row for row in _catalog(session) if row["id"] == selected), {})
            if effort not in row.get("reasoning_efforts", []):
                raise LLMError("所选 Antigravity 模型不支持这个思考档位。")
            conn.call("session/set_config_option", {**params, "configId": "model", "value": selected + "-" + effort})
        else:
            conn.call("session/set_config_option", {**params, "configId": "thought_level", "value": effort})
    return selected


def _prompt(messages, response_format, images_allowed):
    if not messages:
        raise LLMError("Antigravity 请求没有消息。")
    turns, images = [], []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                    continue
                image_url = block.get("image_url") if isinstance(block, dict) and block.get("type") == "image_url" else None
                url = image_url.get("url") if isinstance(image_url, dict) else ""
                match = re.fullmatch(r"data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/=]+)", url or "")
                if not images_allowed or not match:
                    raise LLMError("此 Antigravity 请求仅支持文本及 CLI 允许的内嵌图片。")
                try:
                    base64.b64decode(match[2], validate=True)
                except (ValueError, binascii.Error):
                    raise LLMError("内嵌图片的 base64 内容无效。") from None
                images.append({"type": "image", "mimeType": match[1], "data": match[2]})
                parts.append(f"[Attached image {len(images)}]")
            content = "\n".join(parts)
        if not isinstance(content, str):
            raise LLMError("Antigravity 请求中有不支持的消息内容。")
        turns.append({"role": message.get("role", "user"), "content": content})
    instruction = _NO_ACTIONS
    if (response_format or {}).get("type") == "json_object":
        instruction += " Return exactly one JSON object, without Markdown fences or commentary."
    return [{"type": "text", "text": instruction + "\nTask messages (JSON):\n" + json.dumps(turns, ensure_ascii=False)}, *images]


class AntigravityCliClient:
    def __init__(self, data_dir: Path | None = None, *, executable=None, timeout_s=180):
        self.executable = executable
        self.timeout_s = max(0.1, float(timeout_s))
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self._lock = threading.Lock()
        self._cached_status = None
        self._expires = 0.0

    def status(self, refresh=False):
        with self._lock:
            if not refresh and self._cached_status and time.monotonic() < self._expires:
                return deepcopy(self._cached_status)
            exe = find_agy(self.executable)
            state = {"available": bool(exe), "executable": exe or "", "ready": False, "authenticated": False,
                     "billing": "subscription", "models": [], "subscription_type": "", "install_url": INSTALL_URL,
                     "message": "请先安装 Google 官方 Antigravity CLI（agy）。"}
            if exe:
                try:
                    with _connection(exe, min(self.timeout_s, 20)) as conn:
                        _, session = conn.open()
                        models = _catalog(session)
                        plan = _tier(session)
                        _check_tier(plan)
                        state.update(ready=True, models=models, subscription_type=plan.get("name", ""),
                                     authenticated=True, message="已连接官方 CLI，可测试当前账号的模型。")
                except (LLMError, LLMNotConfigured, BudgetExceeded) as exc:
                    state["message"] = str(exc)
            self._cached_status = state
            self._expires = time.monotonic() + (120 if state["ready"] else 5)
            return deepcopy(state)

    def model_options(self, refresh=False):
        state = self.status(refresh=refresh)
        if not state["ready"]:
            raise LLMNotConfigured(state["message"])
        if not state["models"]:
            raise LLMError("Antigravity CLI 没有返回模型目录，请刷新或更新官方 CLI。")
        return state["models"]

    def start_login(self):
        exe = find_agy(self.executable)
        if not exe:
            return {"ok": False, "message": "请先安装 Google 官方 Antigravity CLI（agy）。"}
        cmd = [exe, "acp", "--login"]
        try:
            if sys.platform == "darwin":
                # Open a user-visible terminal for Google's interactive login flow.
                with tempfile.NamedTemporaryFile(prefix="PaperMap-Antigravity-", suffix=".command", delete=False) as file:
                    script = Path(file.name)
                    file.write(("#!/bin/sh\nrm -f \"$0\"\nunset " + " ".join(_API_ENV) +
                                "\nexec " + shlex.join(cmd) + "\n").encode())
                try:
                    script.chmod(0o700)
                    subprocess.run(["/usr/bin/open", "-a", "Terminal", str(script)], check=True,
                                   capture_output=True, timeout=10)
                except BaseException:
                    script.unlink(missing_ok=True)
                    raise
            elif os.name == "nt":
                subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE, cwd=Path.home(), env=_env())
            else:
                terminal = next(((shutil.which(name), flag) for name, flag in
                                 [("x-terminal-emulator", "-e"), ("gnome-terminal", "--"), ("xterm", "-e")]
                                 if shutil.which(name)), None)
                if not terminal:
                    raise OSError("no terminal")
                subprocess.Popen([*terminal, *cmd], cwd=Path.home(), env=_env())
        except (OSError, subprocess.SubprocessError):
            return {"ok": False, "message": _LOGIN_HINT}
        with self._lock:
            self._expires = 0
        return {"ok": True, "message": "已打开 Google 官方登录终端。完成登录后请刷新状态。"}

    def _chunks(self, model, messages, reasoning_effort, response_format):
        with _connection(self.executable, self.timeout_s) as conn:
            init, session = conn.open()
            _check_tier(_tier(session))
            image_ok = (init.get("agentCapabilities", {}).get("promptCapabilities", {}).get("image") is True)
            prompt = _prompt(messages, response_format, image_ok)
            # Explicit review mode also overrides a user's "auto" ACP setting.
            mode = _option(session, "mode")
            if mode.get("currentValue") != "default" and any(row["value"] == "default" for row in _values(mode)):
                conn.call("session/set_config_option", {"sessionId": session["sessionId"], "configId": "mode", "value": "default"})
            selected = _select(conn, session, model, reasoning_effort)
            params = {"sessionId": session["sessionId"], "prompt": prompt}
            had_text = False
            for frame in conn.frames("session/prompt", params):
                if "result" in frame:
                    result = frame["result"]
                    stop = result.get("stopReason")
                    if stop != "end_turn":
                        raise LLMError("Antigravity 回答未完成（" + {
                            "max_tokens": "达到输出上限", "cancelled": "已取消", "refusal": "服务拒绝了请求",
                            "max_turn_requests": "达到会话请求上限",
                        }.get(stop, "未正常结束") + "）。")
                    if not had_text:
                        raise LLMError("Antigravity 返回空回答，请在官方 CLI 检查所选模型。")
                    usage = result.get("usage") or {}
                    yield SimpleNamespace(model=selected, choices=[],
                                          usage=SimpleNamespace(prompt_tokens=usage.get("inputTokens") or 0,
                                                                completion_tokens=usage.get("outputTokens") or 0))
                    return
                event = frame.get("params") or {}
                if event.get("sessionId") != session["sessionId"]:
                    continue
                update = event.get("update") or {}
                if update.get("sessionUpdate") in {"tool_call", "tool_call_update"}:
                    raise LLMError("模型尝试使用工具，本次文献请求已停止。")
                if update.get("sessionUpdate") == "agent_message_chunk":
                    block = update.get("content") or {}
                    if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"]:
                        had_text = had_text or bool(block["text"].strip())
                        yield SimpleNamespace(model=selected, choices=[SimpleNamespace(delta=SimpleNamespace(content=block["text"]))])

    def create(self, model, messages, max_tokens=4096, response_format=None, reasoning_effort="", stream=False, **_):
        # ACP has no per-turn max output token setting. The CLI owns its model's limit.
        chunks = self._chunks(model, messages, reasoning_effort, response_format)
        if stream:
            return chunks
        parts = []
        for chunk in chunks:
            if chunk.choices:
                parts.append(chunk.choices[0].delta.content)
            else:
                return SimpleNamespace(model=chunk.model, usage=chunk.usage,
                                       choices=[SimpleNamespace(message=SimpleNamespace(content="".join(parts)))])


@lru_cache(maxsize=8)
def get_cli(data_dir: Path | None = None, *, executable=None, timeout_s=180):
    return AntigravityCliClient(data_dir, executable=executable, timeout_s=timeout_s)
