"""Local settings controls for installing plugins, approvals, and API tokens."""
from __future__ import annotations

import ipaddress
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import plugins
from . import mcp_api, plugin_api

EXAMPLES = Path(__file__).resolve().parents[1] / "extension_examples"
EXAMPLE_IDS = {"reading-tools", "event-inspector"}
LABELS = {"providers": "模型服务", "translations": "翻译服务", "skills": "技能", "exports": "导出模板"}


def mount_admin(app: FastAPI, config, registry, hooks, csrf_token, templates):
    def guard(request: Request, body: dict):
        peer = request.client.host if request.client else ""
        try:
            local = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            local = False
        origin = request.headers.get("origin")
        if not local or (origin and origin != str(request.base_url).rstrip("/")):
            raise HTTPException(403, "仅限本机窗口访问")
        csrf = body.get("csrf")
        if not isinstance(csrf, str) or not secrets.compare_digest(csrf.encode(), csrf_token.encode()):
            raise HTTPException(403, "请刷新设置页后重试")

    @app.post("/api/extensions/list")
    def listing(request: Request, body: dict):
        guard(request, body)
        states = hooks.states()
        return templates.TemplateResponse(request, "_plugins_list.html", {
            "plugins": plugins.discover(config), "states": states,
            "labels": LABELS, "token_enabled": plugin_api.token_enabled(registry),
            "mcp_enabled": mcp_api.token_enabled(registry),
        }, headers={"Cache-Control": "no-store"})

    @app.post("/api/extensions/install")
    def install(request: Request, body: dict):
        guard(request, body)
        example = body.get("example")
        path = body.get("path")
        if example and isinstance(example, str) and example in EXAMPLE_IDS:
            source = EXAMPLES / example
        elif isinstance(path, str) and path.strip() and not example:
            source = Path(path.strip()).expanduser()
            if not source.is_absolute():
                return JSONResponse({"ok": False, "message": "请选择插件文件夹的完整路径。"}, status_code=400)
        else:
            raise HTTPException(400, "请选择插件文件夹")
        try:
            plugin = plugins.install_plugin(config, source)
        except (OSError, plugins.PluginError) as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return {"ok": True, "id": plugin.id}

    @app.post("/api/extensions/{plugin_id}/enabled")
    def enable(plugin_id: str, request: Request, body: dict):
        guard(request, body)
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(400, "缺少启用状态")
        try:
            hooks.enable(plugin_id, enabled, str(body.get("digest") or ""))
        except (OSError, plugins.PluginError) as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return {"ok": True}

    @app.post("/api/extensions/{plugin_id}/run")
    def run(plugin_id: str, request: Request, body: dict):
        guard(request, body)
        try:
            result = plugins.run_python(config, plugin_id, {"type": "plugin.run", "data": {}})
            return {"ok": True, "result": result}
        except plugins.PluginError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)

    @app.post("/api/extensions/{plugin_id}/retry")
    def retry(plugin_id: str, request: Request, body: dict):
        guard(request, body)
        hooks.retry(plugin_id)
        return {"ok": True}

    def change_token(service, action, request, body):
        guard(request, body)
        if action == "rotate":
            result = {"ok": True, "token": service.rotate_token(registry)}
        elif action == "revoke":
            service.revoke_token(registry)
            result = {"ok": True}
        else:
            raise HTTPException(404)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.post("/api/extensions/token/{action}")
    def token(action: str, request: Request, body: dict):
        return change_token(plugin_api, action, request, body)

    @app.post("/api/extensions/mcp-token/{action}")
    def mcp_token(action: str, request: Request, body: dict):
        return change_token(mcp_api, action, request, body)
