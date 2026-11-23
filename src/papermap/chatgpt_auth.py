"""Official Sign in with ChatGPT for a local, public OAuth client.

Credentials belong to PaperMap, never to the Codex CLI. No token is returned
to the web UI. See developers.openai.com/siwc/token-sharing-open-source.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
import uuid
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
LOGIN_TIMEOUT = 600
REFRESH_MARGIN = 60
_TOKEN_KEYS = ("access_token", "refresh_token", "id_token", "expires_at", "earliest_refresh_at", "scopes")
_TERMINAL_REFRESH_ERRORS = {
    "invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
    "refresh_token_invalidated", "refresh_token_reused",
}


class ChatGPTAuthError(Exception):
    """A public, credential-free error with a stable machine-readable code."""

    def __init__(self, code: str, message: str, status_code: int = 0):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data
    except (OSError, ValueError) as exc:
        raise ChatGPTAuthError("storage_error", "ChatGPT 本地登录记录无法读取，请重新登录。") from exc


def _write_json(path: Path, data: dict) -> None:
    """Replace the complete token set atomically, with owner-only permissions."""
    fd, name = tempfile.mkstemp(prefix=".oauth-", dir=path.parent)
    try:
        os.chmod(name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def _file_lock(path: Path):
    """Serialize rotating refresh tokens across threads and PaperMap processes."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            deadline = time.monotonic() + 90
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ChatGPTAuthError("session_busy", "ChatGPT 登录记录正在更新，请稍后重试。")
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        # Closing releases the OS lock even after an exception.
        os.close(fd)


def _number(value, default: float = 0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return default


class ChatGPTAuth:
    def __init__(self, data_dir: Path, *, http: httpx.Client | None = None,
                 browser_open: Callable[[str], object] | None = None):
        self.directory = Path(data_dir) / "chatgpt"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.directory.chmod(0o700)
        self.credentials_path = self.directory / "credentials.json"
        self.host_path = self.directory / "host.json"
        self.lock_path = self.directory / "session.lock"
        self._http = http or httpx.Client(timeout=20, follow_redirects=False, trust_env=True)
        self._owns_http = http is None
        self._browser_open = browser_open or webbrowser.open
        self._lock = threading.RLock()
        self._pending: dict | None = None
        self._server: HTTPServer | None = None
        self._stop: threading.Event | None = None
        self._generation = 0
        self._phase = "idle"
        self._last_error: ChatGPTAuthError | None = None
        self._discovery: dict = {}
        self._jwks: dict = {}
        self._jwks_at = 0.0

    def _host_id(self) -> str:
        with _file_lock(self.lock_path):
            host = _read_json(self.host_path)
            if not host.get("ext_agent_host_id"):
                host = {"ext_agent_host_id": "urn:uuid:" + str(uuid.uuid4())}
                _write_json(self.host_path, host)
            return str(host["ext_agent_host_id"])

    def start(self, *, consent: bool = False) -> dict:
        """Start a one-shot loopback listener, then open the system browser.

        ``consent=True`` is for an explicit retry after declining plan usage.
        Browser-launch failure leaves the returned URL usable for manual opening.
        """
        with self._lock:
            if self._phase in ("pending", "exchanging"):
                raise ChatGPTAuthError("login_pending", "ChatGPT 登录正在进行，请完成或取消后再试。")
            host_id = self._host_id()
            with _file_lock(self.lock_path):
                record = _read_json(self.credentials_path)
            owner = self

            class Callback(BaseHTTPRequestHandler):
                def do_GET(self):  # noqa: N802 - stdlib handler interface
                    u = urlsplit(self.path)
                    if u.path != "/auth/callback":
                        self.send_error(404)
                        return
                    expected_host = f"127.0.0.1:{self.server.server_port}"
                    if self.headers.get("Host") != expected_host:
                        self.send_error(400)
                        return
                    params = parse_qs(u.query, keep_blank_values=True)
                    # This listener belongs to exactly one attempt. A cancelled
                    # listener can still finish an already accepted request.
                    ok = owner._callback(params, expected_attempt=pending)
                    text = ("ChatGPT 登录完成。请回到 PaperMap。" if ok else
                            "ChatGPT 登录未完成。请回到 PaperMap 查看原因并重新登录。")
                    body = ("<!doctype html><meta charset=utf-8><title>PaperMap</title><p>" + text + "</p>").encode()
                    self.send_response(200 if ok else 400)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *args):
                    pass  # Callback query strings contain authorization codes.

            try:
                server = HTTPServer(("127.0.0.1", 0), Callback)
            except OSError as exc:
                raise ChatGPTAuthError("callback_unavailable", "无法启动本机 ChatGPT 登录回调。") from exc
            server.timeout = 0.25
            verifier = secrets.token_urlsafe(48)
            redirect_uri = f"http://127.0.0.1:{server.server_port}/auth/callback"
            stop = threading.Event()
            pending = {
                "state": secrets.token_urlsafe(32), "nonce": secrets.token_urlsafe(32),
                "verifier": verifier, "redirect_uri": redirect_uri,
                "client_id": record.get("client_id") or "dynamic_agent_client",
                "subject": record.get("subject"), "expires_at": time.time() + LOGIN_TIMEOUT,
                "generation": self._generation,
                "stop": stop,
            }
            self._pending, self._server = pending, server
            self._stop = stop
            self._phase, self._last_error = "pending", None
            params = {
                "client_id": pending["client_id"], "ext_agent_host_id": host_id,
                "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPES,
                "resource": RESOURCE, "state": pending["state"], "nonce": pending["nonce"],
                "code_challenge_method": "S256",
                "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("="),
            }
            if pending["client_id"] == "dynamic_agent_client":
                params["agent_name_hint"] = "PaperMap"
            else:
                # Omit optional id_token_hint: the manually openable URL is
                # returned to the UI and must not carry a retained ID token.
                if record.get("email"):
                    params["login_hint"] = record["email"]
            if consent:
                params["prompt"] = "consent"
            url = AUTHORIZE_URL + "?" + urlencode(params)
            threading.Thread(target=self._listen, args=(server, self._stop, pending), daemon=True).start()
        try:
            opened = bool(self._browser_open(url))
        except Exception:  # noqa: BLE001 - a manual URL is still usable
            opened = False
        return {"url": url, "authorization_url": url, "expires_at": pending["expires_at"],
                "status": "pending", "browser_opened": opened}

    def _listen(self, server: HTTPServer, stop: threading.Event, attempt: dict) -> None:
        try:
            while not stop.is_set() and time.time() < attempt["expires_at"]:
                server.handle_request()
            with self._lock:
                if self._pending is attempt:
                    self._pending = None
                    self._phase = "error"
                    self._last_error = ChatGPTAuthError("login_timeout", "ChatGPT 登录已超时，请重新登录。")
        finally:
            server.server_close()

    def _callback(self, params: dict[str, list[str]], *, expected_attempt: dict | None = None) -> bool:
        with self._lock:
            if expected_attempt is not None and self._pending is not expected_attempt:
                return False
            attempt, self._pending = self._pending, None
            if attempt is None:
                return False
            self._phase = "exchanging"
        try:
            state = params.get("state", [])
            if (len(state) != 1 or not secrets.compare_digest(state[0], attempt["state"])
                    or time.time() >= attempt["expires_at"]):
                raise ChatGPTAuthError("invalid_state", "ChatGPT 登录回调验证失败，请重新登录。")
            if "error" in params:
                raise ChatGPTAuthError("access_denied", "未授权 ChatGPT 登录或套餐使用，请重新登录授权。")
            codes, ids = params.get("code", []), params.get("client_id", [])
            cid = ids[0] if len(ids) == 1 else attempt["client_id"]
            if (len(codes) != 1 or not codes[0] or len(ids) > 1 or not cid
                    or cid == "dynamic_agent_client"
                    or (attempt["client_id"] != "dynamic_agent_client" and cid != attempt["client_id"])):
                raise ChatGPTAuthError("invalid_callback", "ChatGPT 注册信息不完整或客户端不匹配，请重新登录。")
            try:
                tokens = self._token_request({
                    "grant_type": "authorization_code", "client_id": cid, "code": codes[0],
                    "code_verifier": attempt["verifier"], "redirect_uri": attempt["redirect_uri"], "resource": RESOURCE,
                })
            except ChatGPTAuthError as exc:
                # The issued registration survives an expired/reused code; a
                # retry must not register another client. No identity is trusted
                # and no access is enabled until a later ID token is verified.
                if exc.code == "invalid_grant" and attempt["client_id"] == "dynamic_agent_client":
                    with self._lock, _file_lock(self.lock_path):
                        current = _read_json(self.credentials_path)
                        if attempt["generation"] == self._generation and not current.get("client_id"):
                            _write_json(self.credentials_path, {"client_id": cid, "issuer": ISSUER})
                raise
            claims = self._validate_id(tokens.get("id_token"), cid, nonce=attempt["nonce"])
            if attempt["subject"] and claims["sub"] != attempt["subject"]:
                raise ChatGPTAuthError("account_mismatch", "登录账号与已保存的 ChatGPT 账号不同，原登录记录未被替换。")
            record = self._credential_record(tokens, {
                "client_id": cid, "subject": claims["sub"], "issuer": ISSUER,
                "email": claims.get("email") or "", "email_verified": claims.get("email_verified") is True,
            })
            with self._lock, _file_lock(self.lock_path):
                if attempt["generation"] != self._generation:
                    raise ChatGPTAuthError("login_cancelled", "ChatGPT 登录已取消。")
                _write_json(self.credentials_path, record)
                self._phase = "connected" if PLAN_SCOPE in record["scopes"] else "permission_required"
                self._last_error = None if self._phase == "connected" else ChatGPTAuthError(
                    "scope_missing", "已登录，但尚未授权使用 ChatGPT 套餐，请点击重新授权。")
            return True
        except ChatGPTAuthError as exc:
            with self._lock:
                if attempt["generation"] == self._generation:
                    self._phase, self._last_error = "error", exc
            return False
        except Exception:  # noqa: BLE001 - never expose tokens or callback data
            with self._lock:
                if attempt["generation"] == self._generation:
                    self._phase = "error"
                    self._last_error = ChatGPTAuthError("login_failed", "ChatGPT 登录未完成，请重试。")
            return False
        finally:
            with self._lock:
                attempt["stop"].set()

    def _token_request(self, form: dict) -> dict:
        try:
            response = self._http.post(TOKEN_URL, data=form, headers={"Accept": "application/json"})
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ChatGPTAuthError("network_error", "无法连接 ChatGPT 登录服务，请稍后重试。") from exc
        if response.status_code != 200 or not isinstance(data, dict) or data.get("error"):
            error = data.get("error") if isinstance(data, dict) else None
            code = error.get("code") if isinstance(error, dict) else error
            code = code if isinstance(code, str) and len(code) < 80 and all(c.isalnum() or c in "_-" for c in code) else "token_error"
            raise ChatGPTAuthError(code, "ChatGPT 登录凭据未被接受，请重试或重新登录。", response.status_code)
        return data

    def _metadata(self) -> dict:
        if not self._discovery:
            try:
                response = self._http.get(DISCOVERY_URL)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or data.get("issuer") != ISSUER:
                    raise ValueError("issuer mismatch")
                self._discovery = data
            except (httpx.HTTPError, ValueError) as exc:
                raise ChatGPTAuthError("discovery_error", "无法验证 ChatGPT 身份服务，请稍后重试。") from exc
        return self._discovery

    @staticmethod
    def _trusted_endpoint(value) -> str:
        if not isinstance(value, str):
            raise ChatGPTAuthError("discovery_error", "ChatGPT 身份服务配置不完整。")
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.netloc != "auth.openai.com":
            raise ChatGPTAuthError("discovery_error", "ChatGPT 身份服务地址验证失败。")
        return value

    def _validate_id(self, token, client_id: str, nonce: str | None = None) -> dict:
        if not isinstance(token, str) or not token:
            raise ChatGPTAuthError("invalid_identity", "ChatGPT 未返回可验证的身份。")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in ("RS256", "ES256") or not header.get("kid"):
                raise ValueError("unsupported signing key")
            jwks_url = self._trusted_endpoint(self._metadata().get("jwks_uri"))
            for force in (False, True):
                if force or not self._jwks or time.time() - self._jwks_at > 3600:
                    response = self._http.get(jwks_url)
                    response.raise_for_status()
                    self._jwks, self._jwks_at = response.json(), time.time()
                keys = [k for k in self._jwks.get("keys", []) if k.get("kid") == header["kid"]]
                if keys:
                    break
            if len(keys) != 1 or keys[0].get("use", "sig") != "sig":
                raise ValueError("signing key not found")
            key = jwt.PyJWK.from_dict(keys[0], algorithm=header["alg"])
            claims = jwt.decode(token, key.key, algorithms=[header["alg"]], audience=client_id,
                                issuer=ISSUER, leeway=5, options={"require": ["sub", "exp", "iat", "iss", "aud"]})
            if not isinstance(claims["sub"], str) or not claims["sub"]:
                raise ValueError("invalid subject")
            if nonce is not None and (not isinstance(claims.get("nonce"), str)
                                      or not secrets.compare_digest(claims["nonce"], nonce)):
                raise ValueError("nonce mismatch")
            if (("azp" in claims or (isinstance(claims["aud"], list) and len(claims["aud"]) > 1))
                    and claims.get("azp") != client_id):
                raise ValueError("authorized party mismatch")
            return claims
        except ChatGPTAuthError:
            raise
        except (jwt.PyJWTError, httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ChatGPTAuthError("invalid_identity", "ChatGPT 身份签名或登录信息验证失败，请重新登录。") from exc

    @staticmethod
    def _credential_record(tokens: dict, previous: dict, *, refresh: bool = False) -> dict:
        if (not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]
                or str(tokens.get("token_type", "")).lower() != "bearer"
                or _number(tokens.get("expires_in")) <= 0
                or not isinstance(tokens.get("refresh_token"), str) or not tokens["refresh_token"]):
            raise ChatGPTAuthError("invalid_token_response", "ChatGPT 登录响应不完整，请重新登录。")
        result = dict(previous)
        scope = tokens.get("scope")
        if not isinstance(scope, str):
            if not refresh:
                raise ChatGPTAuthError("scope_missing", "ChatGPT 登录响应未确认套餐使用权限，请重新授权。")
            scopes = previous.get("scopes", [])
        else:
            scopes = scope.split()
        result.update({"access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"],
                       "token_type": "Bearer", "expires_at": time.time() + float(tokens["expires_in"]),
                       "scopes": scopes, "saved_at": datetime.now(timezone.utc).isoformat(),
                       "earliest_refresh_at": _number(tokens.get("earliest_refresh_at"))})
        if tokens.get("id_token"):
            result["id_token"] = tokens["id_token"]
        return result

    def status(self) -> dict:
        """Read only local status; never refresh or expose a credential."""
        with self._lock:
            try:
                with _file_lock(self.lock_path):
                    record = _read_json(self.credentials_path)
                permitted = PLAN_SCOPE in record.get("scopes", [])
                connected = bool(record.get("access_token"))
                ready = bool(permitted and connected and (record.get("refresh_token") or _number(record.get("expires_at")) > time.time()))
                phase = (self._phase if self._phase in ("pending", "exchanging", "error") else
                         "connected" if ready else "permission_required" if connected else "signed_out")
                error = self._last_error
                return {"ready": ready, "connected": connected, "pending": self._phase in ("pending", "exchanging"),
                        "status": phase, "email": record.get("email", ""), "client_id": record.get("client_id", ""),
                        "expires_at": record.get("expires_at"), "needs_consent": connected and not permitted,
                        "error": str(error) if error else "", "error_code": error.code if error else ""}
            except ChatGPTAuthError as exc:
                return {"ready": False, "connected": False, "pending": False, "status": "error",
                        "error": str(exc), "error_code": exc.code}

    def access_token(self, rejected_token: str | None = None) -> str:
        with self._lock, _file_lock(self.lock_path):
            record = _read_json(self.credentials_path)
            if PLAN_SCOPE not in record.get("scopes", []):
                raise ChatGPTAuthError("scope_missing", "请先登录并授权 PaperMap 使用 ChatGPT 套餐。")
            force_refresh = rejected_token is not None and record.get("access_token") == rejected_token
            if not force_refresh and _number(record.get("expires_at")) > time.time() + REFRESH_MARGIN:
                return record["access_token"]
            if not record.get("refresh_token"):
                raise ChatGPTAuthError("sign_in_required", "ChatGPT 登录已过期，请重新登录。")
            if _number(record.get("earliest_refresh_at")) > time.time():
                if not force_refresh and _number(record.get("expires_at")) > time.time():
                    return record["access_token"]
                raise ChatGPTAuthError("refresh_too_early", "ChatGPT 暂时无法续期，请稍后重试。")
            try:
                tokens = self._token_request({"grant_type": "refresh_token", "client_id": record["client_id"],
                                              "refresh_token": record["refresh_token"], "resource": RESOURCE})
                if tokens.get("id_token"):
                    claims = self._validate_id(tokens["id_token"], record["client_id"])
                    if claims["sub"] != record["subject"]:
                        raise ChatGPTAuthError("account_mismatch", "ChatGPT 续期返回了不同账号，请重新登录。")
                updated = self._credential_record(tokens, record, refresh=True)
                _write_json(self.credentials_path, updated)
                if PLAN_SCOPE not in updated["scopes"]:
                    raise ChatGPTAuthError("scope_missing", "ChatGPT 套餐使用权限已改变，请重新授权。")
                self._phase, self._last_error = "connected", None
                return updated["access_token"]
            except ChatGPTAuthError as exc:
                if exc.code in _TERMINAL_REFRESH_ERRORS:
                    _write_json(self.credentials_path, {k: v for k, v in record.items() if k not in _TOKEN_KEYS})
                self._phase, self._last_error = "error", exc
                raise

    def cancel(self) -> None:
        with self._lock:
            self._generation += 1
            self._pending = None
            if self._stop:
                self._stop.set()
            self._phase = "idle"
            self._last_error = None

    def logout(self) -> dict:
        self.cancel()
        confirmed = True
        with self._lock, _file_lock(self.lock_path):
            record = _read_json(self.credentials_path)
            if record.get("refresh_token"):
                confirmed = False
                try:
                    endpoint = self._trusted_endpoint(self._metadata().get("revocation_endpoint"))
                    for attempt in range(2):
                        try:
                            response = self._http.post(endpoint, data={"token": record["refresh_token"],
                                "token_type_hint": "refresh_token", "client_id": record["client_id"]})
                            confirmed = response.status_code == 200
                            if confirmed or response.status_code < 500:
                                break
                        except httpx.HTTPError:
                            pass
                        if attempt == 0:
                            time.sleep(0.2)
                except ChatGPTAuthError:
                    pass
            _write_json(self.credentials_path, {k: v for k, v in record.items() if k not in _TOKEN_KEYS})
            self._phase, self._last_error = "signed_out", None
        return {"ok": True, "remote_revocation_confirmed": confirmed,
                "message": "已退出 ChatGPT 登录。" if confirmed else
                "已退出本机登录；未确认远程撤销，可在 ChatGPT 设置中断开 PaperMap。"}

    def close(self) -> None:
        self.cancel()
        if self._owns_http:
            self._http.close()


_instances: dict[str, ChatGPTAuth] = {}
_instances_lock = threading.Lock()


def get_auth(data_dir: Path) -> ChatGPTAuth:
    """Share login attempts and refresh coordination across the local app."""
    key = str(Path(data_dir).expanduser().resolve())
    with _instances_lock:
        if key not in _instances:
            _instances[key] = ChatGPTAuth(Path(key))
        return _instances[key]


def close_all_auth() -> None:
    with _instances_lock:
        instances = list(_instances.values())
        _instances.clear()
    for instance in instances:
        instance.close()
