"""Let a paired phone open the computer's PaperMap pages (map, fields, evidence notes, recap…).

Off unless the user allows it for a share. The phone's native client asks for a one-time ticket
over its authenticated pairing API; the phone's web view redeems the ticket for a session cookie on
the pairing listener, which then forwards requests to the loopback web app. Forwarded requests carry
a per-process secret header, so the web app knows they come from the phone: it refuses the settings,
credentials, backups, updates and pages that act on this computer's files, and lays pages out for
touch without the desktop sidebar (the phone shows its own).
"""
from __future__ import annotations

import hmac
import http.client
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit

HEADER = "X-PaperMap-Remote"
SECRET = secrets.token_urlsafe(32)  # never leaves this process
COOKIE = "pm_phone"
TICKET_SECONDS = 60
SESSION_SECONDS = 12 * 3600
MAX_UPLOAD = 64 * 1024 * 1024

# Never reachable from the phone: keys and accounts, settings, backups and restore, app updates and
# shutdown, plugins, the loopback MCP and import APIs, and actions on this computer's own files.
DENIED = re.compile(r"""^/(
    settings | api/settings | api/accounts | api/llm | api/app\b | api/appearance
  | api/backup | api/update | api/updates | shutdown
  | api/extensions | extensions | plugins | api/plugins | mcp
  | api/import | import | bulk | api/demo | zotero | api/zotero | events/stream
  | paper/[0-9]+/reveal | api/skills/install | api/knowledge
)(/|$|\?|\.)""", re.X)

_HOP = {"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer", "upgrade",
        "host", "origin", "referer", "cookie", "authorization", HEADER.lower(), "content-length"}


def denied(path: str) -> bool:
    return bool(DENIED.match(path))


def is_remote(headers) -> bool:
    given = headers.get(HEADER)
    return bool(given) and hmac.compare_digest(given.encode(), SECRET.encode())


def safe_next(value: str) -> str:
    """Only a path on this server: no scheme, host or protocol-relative URL."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value or denied(value):
        return "/"
    return value


class Sessions:
    """One-time tickets and the cookies they turn into; all forgotten when the share stops."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tickets: dict[str, float] = {}
        self._sessions: dict[str, float] = {}

    def ticket(self) -> str:
        value = secrets.token_urlsafe(32)
        with self._lock:
            self._prune()
            self._tickets[value] = time.monotonic() + TICKET_SECONDS
        return value

    def redeem(self, ticket: str) -> str | None:
        with self._lock:
            self._prune()
            if self._tickets.pop(ticket, None) is None:
                return None
            session = secrets.token_urlsafe(32)
            self._sessions[session] = time.monotonic() + SESSION_SECONDS
            return session

    def valid(self, session: str | None) -> bool:
        if not session:
            return False
        with self._lock:
            self._prune()
            return session in self._sessions

    def clear(self) -> None:
        with self._lock:
            self._tickets.clear()
            self._sessions.clear()

    def _prune(self) -> None:
        now = time.monotonic()
        for store in (self._tickets, self._sessions):
            for key in [k for k, until in store.items() if until < now]:
                del store[key]


def cookie_value(header: str | None) -> str | None:
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            return value
    return None


def ticket_from(path: str) -> tuple[str, str]:
    query = parse_qs(urlsplit(path).query)
    return (query.get("t") or [""])[0], safe_next((query.get("next") or ["/"])[0])


def forward(handler, port: int) -> None:
    """Relay one request from the phone's web view to the loopback web app and stream the answer back."""
    path = handler.path
    if denied(urlsplit(path).path):
        body = "这一页只能在电脑上打开。".encode()
        handler._headers(403, "text/plain; charset=utf-8", len(body))
        handler.wfile.write(body)
        return
    payload = None
    if handler.command == "POST":
        lengths = handler.headers.get_all("Content-Length", [])
        if handler.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isdigit():
            handler._error(411, "length_required")
            return
        size = int(lengths[0])
        if size > MAX_UPLOAD:
            handler._error(413, "too_large")
            return
        payload = handler.rfile.read(size)
        if len(payload) != size:
            raise ConnectionError("incomplete request body")
    origin = f"http://127.0.0.1:{port}"
    headers = {k: v for k, v in handler.headers.items() if k.lower() not in _HOP}
    headers.update({"Host": f"127.0.0.1:{port}", HEADER: SECRET})
    if handler.headers.get("Origin"):
        headers["Origin"] = origin  # the web view's own pages, already authenticated by the session cookie
    upstream = http.client.HTTPConnection("127.0.0.1", port, timeout=600)
    try:
        upstream.request(handler.command, path, body=payload, headers=headers)
        reply = upstream.getresponse()
        handler.send_response(reply.status)
        for key, value in reply.getheaders():
            low = key.lower()
            if low in ("connection", "keep-alive", "transfer-encoding", "set-cookie"):
                continue
            if low == "location" and value.startswith(origin):
                value = value[len(origin):] or "/"
            handler.send_header(key, value)
        handler.send_header("Connection", "close")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.end_headers()
        while True:
            chunk = reply.read(64 * 1024)
            if not chunk:
                break
            handler.wfile.write(chunk)
    finally:
        upstream.close()
