"""A paired phone opening the computer's pages: ticket -> cookie -> relay, and what the web app refuses."""
import ipaddress
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from papermap import mobile_lan, remote_web

from .test_reader import client as reader_client


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def stand_in(config):
    """Plays the loopback web app: echoes what reaches it."""
    port = _free_port()
    seen = []

    async def echo(request: Request):
        body = await request.body()
        seen.append({"path": request.url.path, "query": request.url.query, "headers": dict(request.headers),
                     "body": body.decode(), "method": request.method})
        if request.url.path == "/jump":
            return RedirectResponse(f"http://127.0.0.1:{port}/landed", status_code=303)
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/{rest:path}", echo, methods=["GET", "POST"])])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.02)
    config.raw.setdefault("web", {})["port"] = port
    yield seen
    server.should_exit = True
    thread.join(5)


@pytest.fixture
def lan(config, registry, monkeypatch):
    monkeypatch.setattr(mobile_lan, "_networks", lambda: {"127.0.0.1": ipaddress.IPv4Network("127.0.0.0/8")})
    sharing = mobile_lan.MobileLan(config, registry)
    try:
        yield sharing
    finally:
        sharing.stop()


def _phone(pair, **kw):
    import ssl
    return httpx.Client(base_url=pair["url"], verify=ssl.create_default_context(cadata=pair["ca"]),
                        trust_env=False, timeout=5, **kw)


def test_web_access_stays_off_unless_allowed(lan, stand_in):
    pair = lan.start("127.0.0.1", inbox=True)
    auth = {"Authorization": "Bearer " + pair["token"]}
    with _phone(pair) as phone:
        assert phone.post("/v1/web/ticket", headers=auth).status_code == 403
        assert phone.get("/v1/papers", headers=auth).json()["web"] is False
        assert phone.get("/map").status_code == 401  # the native API's rules: no pages at all
    assert stand_in == [] and "web" not in lan.status()


def test_ticket_becomes_a_cookie_and_pages_are_relayed(lan, stand_in):
    pair = lan.start("127.0.0.1", web=True)
    assert lan.status()["web"] is True and lan.remembered()["web"] is True
    auth = {"Authorization": "Bearer " + pair["token"]}
    with _phone(pair) as phone:
        assert phone.get("/v1/papers", headers=auth).json()["web"] is True
        assert phone.get("/map").status_code == 401  # no session yet
        path = phone.post("/v1/web/ticket", headers=auth).json()["path"]
        entered = phone.get(path + "&next=/map", follow_redirects=False)
        assert entered.status_code == 303 and entered.headers["location"] == "/map"
        cookie = entered.headers["set-cookie"]
        assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
        assert phone.get(path).status_code == 401  # a ticket works once

        r = phone.get("/map?x=1", headers={"Authorization": "Bearer leak"})
        assert r.status_code == 200 and r.json() == {"ok": True}
        got = stand_in[-1]
        assert got["path"] == "/map" and got["query"] == "x=1"
        assert got["headers"]["host"] == f"127.0.0.1:{lan.config.web['port']}"
        assert got["headers"][remote_web.HEADER.lower()] == remote_web.SECRET
        assert "cookie" not in got["headers"] and "authorization" not in got["headers"]

        r = phone.post("/api/qa", content=b'{"q":"x"}', headers={"Content-Type": "application/json",
                                                                 "Origin": pair["url"]})
        assert r.status_code == 200
        got = stand_in[-1]
        assert got["method"] == "POST" and got["body"] == '{"q":"x"}'
        assert got["headers"]["origin"].startswith("http://127.0.0.1:")  # same-origin for the loopback app

        jump = phone.get("/jump", follow_redirects=False)
        assert jump.headers["location"] == "/landed"  # never the loopback address
        before = len(stand_in)
        assert phone.get("/settings").status_code == 403
        assert phone.post("/api/backup/restore", content=b"{}", headers={"Content-Type": "application/json"}).status_code == 403
        assert len(stand_in) == before  # refused before reaching the web app
    lan.stop()
    with _phone(pair) as phone, pytest.raises(httpx.ConnectError):
        phone.get("/map")


def test_safe_next_only_allows_paths_on_this_server():
    for bad in ("//evil.test/x", "https://evil.test", "/\\evil", "", "map", "/settings?section=mobile", "/api/accounts"):
        assert remote_web.safe_next(bad) == "/"
    assert remote_web.safe_next("/field/x?y=1") == "/field/x?y=1"
    assert remote_web.denied("/paper/3/reveal") and remote_web.denied("/api/llm/test") and remote_web.denied("/mcp")
    # endpoints that take a folder on this computer
    assert remote_web.denied("/api/skills/install") and remote_web.denied("/api/knowledge/move")
    assert not remote_web.denied("/skills") and not remote_web.denied("/api/skills")
    assert not remote_web.denied("/map") and not remote_web.denied("/paper/3/panel") and not remote_web.denied("/api/qa")


def test_web_app_lays_pages_out_for_touch_and_refuses_settings(config, registry, pdfs):
    c, _, _ = reader_client(config, registry, pdfs)
    remote = {remote_web.HEADER: remote_web.SECRET}
    page = c.get("/map", headers=remote)
    assert page.status_code == 200 and 'class="app remote-touch"' in page.text
    assert 'class="app remote-touch"' not in c.get("/map").text
    assert 'remote-touch' not in c.get("/map", headers={remote_web.HEADER: "guess"}).text
    for path in ("/settings", "/settings/mobile/panel", "/costs/../settings"):
        assert c.get(path, headers=remote).status_code in (403, 404)
    assert c.get("/settings", headers=remote).status_code == 403
    assert c.post("/api/update/install", headers=remote, json={}).status_code == 403
    assert c.get("/settings").status_code == 200  # the computer itself is unaffected
