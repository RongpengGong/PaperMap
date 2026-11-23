"""Use the user's unmodified Claude Code CLI, without handling its credentials.

The user signs in through ``claude auth login`` in their own terminal. PaperMap
only asks the CLI for public authentication status and sends text on stdin.
Policy and CLI references checked 2026-11-23:
https://code.claude.com/docs/en/legal-and-compliance
https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
https://code.claude.com/docs/en/cli-reference

``--safe-mode`` preserves official authentication, unlike ``--bare``. Managed
organization policies still apply; this module does not try to bypass them.
"""

from __future__ import annotations

import json
import base64
import binascii
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any


MODELS = [{"id": name, "label": name.title()} for name in ("sonnet", "opus", "haiku")]
INSTALL_URL = "https://code.claude.com/docs/en/setup"
_STATUS_TTL = 5.0
_SAFE_ARGS = ["--safe-mode", "--setting-sources", ""]


def find_claude(executable: str | None = None) -> str | None:
    """Find the official native installation even with Finder's minimal PATH."""
    if executable:
        candidates = [Path(executable).expanduser()]
    else:
        name = "claude.exe" if sys.platform == "win32" else "claude"
        found = shutil.which(name)
        candidates = ([Path(found)] if found else []) + [Path.home() / ".local" / "bin" / name]
        if sys.platform == "win32":
            local = os.environ.get("LOCALAPPDATA")
            if local:
                candidates.append(Path(local) / "Microsoft" / "WinGet" / "Links" / name)
        else:
            candidates += [Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")]
    for p in candidates:
        # Windows batch shims invoke cmd.exe. Use Anthropic's native installer
        # instead, so neither model names nor paths ever pass through a shell.
        if p.suffix.lower() in (".cmd", ".bat"):
            continue
        if p.is_file() and (sys.platform == "win32" or os.access(p, os.X_OK)):
            return str(p.resolve())
    return None


def _mode_conflict() -> bool:
    """Do not silently turn a subscription selection into API/third-party spend.

    Values stay in the process environment; we never change the CLI's login
    methods or log secrets. The user can keep using those methods in the CLI.
    """
    credentials = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_PROFILE")
    if any(os.environ.get(k) for k in credentials):
        return True
    for key in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_SIMPLE"):
        if os.environ.get(key, "").lower() not in ("", "0", "false", "no"):
            return True
    base = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    return bool(base and base != "https://api.anthropic.com")


class ClaudeCliClient:
    """Small OpenAI-style facade for PaperMap text and inline image tasks."""

    def __init__(self, data_dir: Path | None = None, *, executable: str | None = None, timeout_s: float = 180):
        # data_dir is accepted for the settings integration; credentials always
        # remain in Claude Code's own storage, never in PaperMap's data directory.
        self.executable = executable
        self.timeout_s = max(1.0, float(timeout_s))
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self._status_lock = threading.Lock()
        self._status_at = 0.0
        self._status: dict | None = None

    def _run(self, args: list[str], *, prompt: str = "", timeout: float = 5, env: dict | None = None):
        with tempfile.TemporaryDirectory(prefix="papermap-claude-") as cwd:
            return subprocess.run(args, input=prompt, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout,
                                  cwd=cwd, env=env, shell=False)

    def status(self, refresh: bool = False) -> dict:
        with self._status_lock:
            if not refresh and self._status is not None and time.monotonic() - self._status_at < _STATUS_TTL:
                return dict(self._status)
            exe = find_claude(self.executable)
            result = {"available": bool(exe), "ready": False, "authenticated": False,
                      "auth_method": "none", "subscription_type": "", "billing": "subscription",
                      "executable": exe or "", "models": [dict(m) for m in MODELS],
                      "install_url": INSTALL_URL,
                      "message": "请先安装官方原生 Claude Code，再点击登录。"}
            if exe and _mode_conflict():
                result["message"] = ("检测到 Claude CLI 的 API、Token、自定义地址或第三方配置；"
                                     "此订阅通道不会使用这些配置计费。请在官方 CLI 中确认自己的 Claude 订阅登录，"
                                     "并在启动 PaperMap 的环境中取消相关覆盖后重试。原配置不会被修改。")
                result["auth_method"] = "external_configuration"
            elif exe:
                try:
                    proc = self._run([exe, *_SAFE_ARGS, "auth", "status"])
                    data = json.loads(proc.stdout)
                    if not isinstance(data, dict):
                        raise ValueError("invalid status")
                    method = data.get("authMethod")
                    method = method if method in ("none", "claude.ai", "oauth_token", "api_key", "api_key_helper", "third_party") else "unknown"
                    authenticated = proc.returncode == 0 and data.get("loggedIn") is True
                    plan = data.get("subscriptionType")
                    plan = plan if isinstance(plan, str) and re.fullmatch(r"[\w -]{1,50}", plan) else ""
                    ready = authenticated and method == "claude.ai" and data.get("apiProvider", "firstParty") == "firstParty"
                    result.update(authenticated=authenticated, auth_method=method,
                                  subscription_type=plan, ready=ready)
                    if ready:
                        result["message"] = "已通过官方 Claude Code 登录；请求使用该用户的订阅额度。"
                    elif authenticated:
                        result["message"] = ("Claude Code 当前使用 API 或第三方认证，此订阅通道已暂停以免误计费。"
                                             "可在官方 CLI 中选择自己的 Claude 订阅；其他认证方式仍可正常使用。")
                    else:
                        result["message"] = "请点击登录，在官方 Claude Code 打开的终端和浏览器中完成自己的订阅登录。"
                except (OSError, subprocess.SubprocessError, ValueError, TypeError):
                    # CLI stderr/status may contain account details. Only return
                    # a fixed diagnostic, never raw subprocess output.
                    result["message"] = "暂时无法验证 Claude Code 登录。请更新官方 CLI，完成 claude auth login 后重试。"
            self._status, self._status_at = result, time.monotonic()
            return dict(result)

    def _invalidate(self) -> None:
        with self._status_lock:
            self._status = None

    def login(self) -> dict:
        """Only called after an explicit user click; the official CLI owns login."""
        exe = find_claude(self.executable)
        if not exe:
            return {"ok": False, "message": "未找到官方原生 Claude Code，请先安装。"}
        command = [exe, "auth", "login"]
        try:
            if sys.platform == "darwin":
                script = "tell application \"Terminal\"\nactivate\ndo script " + json.dumps(shlex.join(command), ensure_ascii=False) + "\nend tell"
                proc = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True,
                                      text=True, timeout=10, shell=False)
                if proc.returncode:
                    raise OSError("terminal did not open")
            elif sys.platform == "win32":
                subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_CONSOLE, cwd=Path.home())
            else:
                options = [("x-terminal-emulator", "-e"), ("gnome-terminal", "--"), ("konsole", "-e"), ("xterm", "-e")]
                terminal = next(((shutil.which(name), arg) for name, arg in options if shutil.which(name)), None)
                if terminal is None:
                    raise OSError("no terminal")
                subprocess.Popen([terminal[0], terminal[1], *command], cwd=Path.home())
        except (OSError, subprocess.SubprocessError):
            return {"ok": False, "message": "无法打开系统终端，请自行在终端运行 claude auth login，再回到 PaperMap 刷新状态。"}
        self._invalidate()
        return {"ok": True, "message": "已打开官方登录终端。完成登录后回到 PaperMap 刷新状态。"}

    def logout(self) -> dict:
        """Explicitly signs out the shared official CLI, without touching tokens."""
        exe = find_claude(self.executable)
        if not exe:
            return {"ok": False, "message": "未找到官方原生 Claude Code。"}
        try:
            proc = self._run([exe, *_SAFE_ARGS, "auth", "logout"], timeout=10)
            ok = proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        self._invalidate()
        return {"ok": ok, "message": "已退出官方 Claude Code 登录（本机 CLI 共用此登录）。" if ok else "退出失败，请在终端运行 claude auth logout。"}

    def create(self, model: str, messages: list[dict], max_tokens: int = 4096,
               response_format: dict | None = None, **_: Any) -> Any:
        from .llm import LLMError, LLMNotConfigured

        # Structured image input uses the documented SDKUserMessage protocol:
        # https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode
        # Images stay on stdin; no image files or file-reading tools are used.
        turns = []
        images = []
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                        parts.append(block["text"])
                    elif isinstance(block, dict) and block.get("type") == "image_url":
                        url = block.get("image_url", {}).get("url") if isinstance(block.get("image_url"), dict) else None
                        match = re.fullmatch(r"data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/=]+)", url or "")
                        if not match:
                            raise LLMError("Claude Code 看图仅支持内嵌 PNG/JPEG/GIF/WebP；不读取远程地址或本机文件路径。")
                        try:
                            if not base64.b64decode(match[2], validate=True):
                                raise ValueError("empty image")
                        except (ValueError, binascii.Error):
                            raise LLMError("Claude Code 图片的 base64 数据无效。") from None
                        images.append({"type": "image", "source": {"type": "base64", "media_type": match[1], "data": match[2]}})
                        parts.append(f"[Attached image {len(images)}]")
                    else:
                        raise LLMError("Claude Code 请求包含不支持的消息内容；仅接受文本和内嵌图片。")
                content = "\n".join(parts)
            elif not isinstance(content, str):
                raise LLMError("Claude Code 请求包含不支持的消息内容；仅接受文本和内嵌图片。")
            turns.append({"role": str(m.get("role", "user")), "content": content})
        if not turns:
            raise LLMError("Claude Code 请求没有文本内容。")
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+\[\]-]{0,150}", model):
            raise LLMError("Claude Code 模型名无效；可使用 sonnet、opus、haiku 或官方完整模型名。")
        state = self.status(refresh=True)
        if not state["ready"]:
            raise LLMNotConfigured(state["message"])
        # All request content goes on stdin, including system instructions. It
        # never appears in process arguments, shell history, or a prompt file.
        prompt = "Complete this PaperMap literature task using only the supplied messages.\n"
        if response_format and response_format.get("type") == "json_object":
            prompt += "Return ONLY a valid JSON object, without Markdown fences or commentary.\n"
        prompt += "Messages (JSON; obey the system message as task instructions):\n" + json.dumps(turns, ensure_ascii=False)
        if images:
            prompt = json.dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": prompt}, *images]}, "parent_tool_use_id": None}, ensure_ascii=False) + "\n"
        args = [state["executable"], *_SAFE_ARGS, "-p", "--input-format", "stream-json" if images else "text",
                "--output-format", "stream-json" if images else "json",
                "--tools", "", "--disallowedTools", "mcp__*", "--disable-slash-commands",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--settings", '{"disableAllHooks":true}', "--no-session-persistence", "--max-turns", "1",
                "--model", model]
        if images:
            args.append("--verbose")
        env = dict(os.environ)
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max(1, int(max_tokens)))
        try:
            proc = self._run(args, prompt=prompt, timeout=self.timeout_s, env=env)
        except subprocess.TimeoutExpired:
            raise LLMError("Claude Code 请求超时；本次进程已停止，可稍后重试。") from None
        except OSError:
            raise LLMNotConfigured("无法启动 Claude Code；请检查官方 CLI 安装后重试。") from None
        try:
            if images:
                # The CLI's final result is authoritative. Do not fall back to
                # partial assistant text when a request fails or is interrupted.
                data = None
                for line in proc.stdout.splitlines():
                    event = json.loads(line) if line.strip() else None
                    if isinstance(event, dict) and event.get("type") == "result":
                        data = event
            else:
                data = json.loads(proc.stdout)
            if not isinstance(data, dict):
                raise ValueError("invalid result")
        except (ValueError, TypeError):
            self._raise_error(proc.stderr or "")
            raise LLMError("Claude Code 未返回有效 JSON。请更新官方 CLI 后重试。") from None
        text = data.get("result")
        if proc.returncode or data.get("is_error") or data.get("subtype", "success") != "success":
            details = " ".join(str(x) for x in (text or "", data.get("errors", ""), proc.stderr or ""))
            self._raise_error(details)
            raise LLMError("Claude Code 未完成请求；请在官方 CLI 检查账户、模型和网络状态。")
        if not isinstance(text, str) or not text.strip():
            raise LLMError("Claude Code 返回空结果，可稍后重试。")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        def tokens(key):
            try:
                return max(0, int(usage.get(key) or 0))
            except (TypeError, ValueError):
                return 0
        cache = tokens("cache_read_input_tokens")
        pt = tokens("input_tokens") + tokens("cache_creation_input_tokens") + cache
        served = data.get("model")
        if not isinstance(served, str) or not served:
            model_usage = data.get("modelUsage")
            served = next(iter(model_usage)) if isinstance(model_usage, dict) and len(model_usage) == 1 else model
        return SimpleNamespace(model=served, choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
                               usage=SimpleNamespace(prompt_tokens=pt, completion_tokens=tokens("output_tokens"),
                                                     prompt_cache_hit_tokens=cache))

    @staticmethod
    def _raise_error(details: str) -> None:
        from .llm import BudgetExceeded, LLMNotConfigured

        lower = details.lower()
        if any(x in lower for x in ("rate limit", "usage limit", "hit your limit", "limit reached", "quota exceeded", "out of extra usage")):
            raise BudgetExceeded("Claude Code 订阅额度暂不可用；请在官方 CLI 查看额度与重置时间。")
        if any(x in lower for x in ("not logged in", "please log in", "please run /login", "invalid authentication", "login expired")):
            raise LLMNotConfigured("Claude Code 登录已失效，请通过官方终端重新登录自己的订阅。")


@lru_cache(maxsize=8)
def _client(data_dir: str, executable: str | None, timeout_s: float) -> ClaudeCliClient:
    return ClaudeCliClient(Path(data_dir) if data_dir else None, executable=executable, timeout_s=timeout_s)


def get_cli(data_dir: Path | None = None, *, executable: str | None = None, timeout_s: float = 180) -> ClaudeCliClient:
    return _client(str(data_dir or ""), executable, float(timeout_s))
