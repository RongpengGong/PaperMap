"""Offline OAuth tests: generated signing keys, fake issuer, temporary storage."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from papermap import chatgpt_auth as authmod
from papermap.chatgpt_auth import ChatGPTAuth, ChatGPTAuthError


@pytest.fixture
def issuer():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="test-signing-key", use="sig", alg="RS256")
    state = {"nonce": "", "claims": {}, "scope": authmod.SCOPES, "calls": [], "refresh_calls": 0,
             "refresh_error": None, "refresh_network_error": False, "revoke_status": 200, "code_error": None}

    def token(**overrides):
        claims = {"iss": authmod.ISSUER, "aud": "oaiapp_papermap_test", "sub": "test-subject",
                  "iat": int(time.time()), "exp": int(time.time()) + 3600,
                  "nonce": state["nonce"], "email": "researcher@example.invalid", "email_verified": True}
        claims.update(state["claims"])
        claims.update(overrides)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": jwk["kid"]})

    def handle(request):
        state["calls"].append(request)
        if str(request.url) == authmod.DISCOVERY_URL:
            return httpx.Response(200, json={"issuer": authmod.ISSUER,
                "jwks_uri": authmod.ISSUER + "/.well-known/jwks.json",
                "revocation_endpoint": authmod.ISSUER + "/api/accounts/oauth/revoke"})
        if request.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, json={"keys": [jwk]})
        if str(request.url) == authmod.TOKEN_URL:
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["refresh_token"]:
                state["refresh_calls"] += 1
                if state["refresh_network_error"]:
                    raise httpx.ConnectError("offline", request=request)
                if state["refresh_error"]:
                    return httpx.Response(400, json={"error": state["refresh_error"]})
                return httpx.Response(200, json={"access_token": "new-test-access", "refresh_token": "new-test-refresh",
                    "token_type": "Bearer", "expires_in": 3600, "scope": state["scope"]})
            if state["code_error"]:
                return httpx.Response(400, json={"error": state["code_error"]})
            return httpx.Response(200, json={"access_token": "test-access", "refresh_token": "test-refresh",
                "id_token": token(), "token_type": "Bearer", "expires_in": 3600, "scope": state["scope"]})
        if request.url.path.endswith("/revoke"):
            return httpx.Response(state["revoke_status"])
        raise AssertionError(f"Unexpected test endpoint: {request.url}")

    state.update(client=httpx.Client(transport=httpx.MockTransport(handle)), token=token)
    yield state
    state["client"].close()


@pytest.fixture
def auth(tmp_path, issuer):
    instance = ChatGPTAuth(tmp_path, http=issuer["client"], browser_open=lambda _: True)
    yield instance
    instance.close()


def begin(auth, issuer, **kwargs):
    started = auth.start(**kwargs)
    query = parse_qs(urlsplit(started["url"]).query)
    issuer["nonce"] = query["nonce"][0]
    return started, query


def complete(auth, issuer, **kwargs):
    _, q = begin(auth, issuer, **kwargs)
    assert auth._callback({"state": q["state"], "code": ["test-code"], "client_id": ["oaiapp_papermap_test"]})
    return q


def update_record(auth, **updates):
    data = json.loads(auth.credentials_path.read_text(encoding="utf-8"))
    data.update(updates)
    authmod._write_json(auth.credentials_path, data)


def test_loopback_login_pkce_verified_identity_and_private_storage(auth, issuer):
    started, q = begin(auth, issuer)
    assert q["client_id"] == ["dynamic_agent_client"]
    assert q["agent_name_hint"] == ["PaperMap"]
    assert q["resource"] == [authmod.RESOURCE]
    assert set(q["scope"][0].split()) == set(authmod.SCOPES.split())
    assert q["code_challenge_method"] == ["S256"]
    verifier = auth._pending["verifier"]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert q["code_challenge"] == [challenge]
    callback = q["redirect_uri"][0]
    assert urlsplit(callback).hostname == "127.0.0.1" and callback.endswith("/auth/callback")
    with httpx.Client(trust_env=False) as local:
        response = local.get(callback, params={"state": q["state"][0], "code": "test-code", "client_id": "oaiapp_papermap_test"})
    assert response.status_code == 200
    assert auth.status()["ready"] and not auth.status()["pending"]
    assert auth.access_token() == "test-access"
    assert "test-access" not in json.dumps(auth.status()) and "test-refresh" not in response.text
    record = json.loads(auth.credentials_path.read_text(encoding="utf-8"))
    assert record["subject"] == "test-subject" and record["client_id"] == "oaiapp_papermap_test"
    exchange = next(r for r in issuer["calls"] if str(r.url) == authmod.TOKEN_URL)
    form = parse_qs(exchange.content.decode())
    assert form["redirect_uri"] == [callback] and form["code_verifier"] == [verifier]
    assert "client_secret" not in form
    if os.name != "nt":
        assert auth.credentials_path.stat().st_mode & 0o777 == 0o600
        assert auth.host_path.stat().st_mode & 0o777 == 0o600
    host = json.loads(auth.host_path.read_text(encoding="utf-8"))["ext_agent_host_id"]
    assert host.startswith("urn:uuid:")
    _, returning = begin(auth, issuer)
    assert returning["client_id"] == ["oaiapp_papermap_test"]
    assert returning["ext_agent_host_id"] == [host]
    assert "agent_name_hint" not in returning and "id_token_hint" not in returning


@pytest.mark.parametrize("change, expected", [
    ({"state": ["wrong-state"]}, "invalid_state"),
    ({"client_id": []}, "invalid_callback"),
    ({"error": ["access_denied"]}, "access_denied"),
])
def test_invalid_callbacks_never_exchange_code(auth, issuer, change, expected):
    _, q = begin(auth, issuer)
    params = {"state": q["state"], "code": ["test-code"], "client_id": ["oaiapp_papermap_test"]}
    params.update(change)
    assert auth._callback(params) is False
    assert auth.status()["error_code"] == expected
    assert not any(str(r.url) == authmod.TOKEN_URL for r in issuer["calls"])
    assert not auth.status()["ready"]
    assert auth._callback(params) is False  # one-time consumption


@pytest.mark.parametrize("claims", [
    {"nonce": "wrong-nonce"}, {"aud": "different-client"}, {"iss": "https://untrusted.invalid"},
    {"exp": 1}, {"sub": ""}, {"aud": ["oaiapp_papermap_test", "another-client"]}, {"azp": "different-client"},
])
def test_identity_checks_before_saving(auth, issuer, claims):
    issuer["claims"] = claims
    _, q = begin(auth, issuer)
    assert not auth._callback({"state": q["state"], "code": ["test-code"], "client_id": ["oaiapp_papermap_test"]})
    assert auth.status()["error_code"] == "invalid_identity"
    assert not auth.credentials_path.exists()


def test_signature_rejection(auth, issuer):
    _, q = begin(auth, issuer)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad = jwt.encode({"iss": authmod.ISSUER, "aud": "oaiapp_papermap_test", "sub": "test-subject",
                      "iat": int(time.time()), "exp": int(time.time()) + 3600, "nonce": issuer["nonce"]},
                     other, algorithm="RS256", headers={"kid": "test-signing-key"})
    with pytest.raises(ChatGPTAuthError, match="验证失败"):
        auth._validate_id(bad, "oaiapp_papermap_test", q["nonce"][0])


def test_granted_scope_is_required_and_can_request_consent(auth, issuer):
    issuer["scope"] = "openid profile email offline_access resource.invoke"
    complete(auth, issuer)
    assert auth.status()["connected"] and auth.status()["needs_consent"] and not auth.status()["ready"]
    with pytest.raises(ChatGPTAuthError) as err:
        auth.access_token()
    assert err.value.code == "scope_missing"
    _, q = begin(auth, issuer, consent=True)
    assert q["prompt"] == ["consent"] and q["client_id"] == ["oaiapp_papermap_test"]


def test_expired_code_retains_new_client_without_trusting_identity(auth, issuer):
    issuer["code_error"] = "invalid_grant"
    _, q = begin(auth, issuer)
    assert not auth._callback({"state": q["state"], "code": ["expired-code"], "client_id": ["oaiapp_papermap_test"]})
    assert auth.status()["error_code"] == "invalid_grant" and not auth.status()["ready"]
    assert json.loads(auth.credentials_path.read_text(encoding="utf-8")) == {
        "client_id": "oaiapp_papermap_test", "issuer": authmod.ISSUER}
    _, retry = begin(auth, issuer)
    assert retry["client_id"] == ["oaiapp_papermap_test"] and "agent_name_hint" not in retry


def test_callback_client_mismatch_keeps_existing_registration(auth, issuer):
    complete(auth, issuer)
    before = auth.credentials_path.read_bytes()
    _, q = begin(auth, issuer)
    assert not auth._callback({"state": q["state"], "code": ["test-code"], "client_id": ["other-client"]})
    assert auth.status()["error_code"] == "invalid_callback" and auth.credentials_path.read_bytes() == before


def test_cancelled_attempt_cannot_sign_in(auth, issuer):
    _, q = begin(auth, issuer)
    auth.cancel()
    assert not auth._callback({"state": q["state"], "code": ["test-code"], "client_id": ["oaiapp_papermap_test"]})
    assert not auth.status()["pending"] and not auth.status()["ready"]


def test_cancelled_http_listener_cannot_consume_restarted_login(auth, issuer, monkeypatch):
    _, old_query = begin(auth, issuer)
    old_attempt = auth._pending
    entered, release = threading.Event(), threading.Event()
    original_callback = auth._callback

    def paused_callback(params, *, expected_attempt=None):
        if expected_attempt is old_attempt:
            entered.set()
            assert release.wait(5)
        return original_callback(params, expected_attempt=expected_attempt)

    monkeypatch.setattr(auth, "_callback", paused_callback)

    def request_callback(query):
        with httpx.Client(trust_env=False) as local:
            return local.get(query["redirect_uri"][0], params={
                "state": query["state"][0], "code": "test-code", "client_id": "oaiapp_papermap_test"})

    with ThreadPoolExecutor(max_workers=1) as pool:
        old_response = pool.submit(request_callback, old_query)
        try:
            assert entered.wait(5)
            auth.cancel()
            _, new_query = begin(auth, issuer)
            new_attempt = auth._pending
        finally:
            release.set()
        assert old_response.result(timeout=5).status_code == 400
    assert auth._pending is new_attempt
    assert auth.status()["pending"] and auth.status()["error_code"] == ""
    assert not any(str(r.url) == authmod.TOKEN_URL for r in issuer["calls"])
    assert request_callback(new_query).status_code == 200
    assert auth.status()["ready"]


def test_reauthorization_cannot_replace_another_identity(auth, issuer):
    complete(auth, issuer)
    original = auth.credentials_path.read_bytes()
    issuer["claims"] = {"sub": "another-subject"}
    _, q = begin(auth, issuer)
    assert not auth._callback({"state": q["state"], "code": ["test-code"]})
    assert auth.status()["error_code"] == "account_mismatch"
    assert auth.credentials_path.read_bytes() == original


def test_rotating_refresh_is_serialized_even_across_instances(auth, issuer, tmp_path):
    complete(auth, issuer)
    update_record(auth, expires_at=time.time() - 1)
    other = ChatGPTAuth(tmp_path, http=issuer["client"], browser_open=lambda _: True)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: (auth if i % 2 else other).access_token(), range(8)))
        assert results == ["new-test-access"] * 8 and issuer["refresh_calls"] == 1
        record = json.loads(auth.credentials_path.read_text(encoding="utf-8"))
        assert record["refresh_token"] == "new-test-refresh"
        refresh = next(r for r in issuer["calls"] if b"grant_type=refresh_token" in r.content)
        form = parse_qs(refresh.content.decode())
        assert form["client_id"] == ["oaiapp_papermap_test"] and "scope" not in form
    finally:
        other.close()


def test_rejected_access_token_can_refresh_but_stale_rejection_does_not(auth, issuer):
    complete(auth, issuer)
    assert auth.access_token(rejected_token="test-access") == "new-test-access"
    assert auth.access_token(rejected_token="test-access") == "new-test-access"
    assert issuer["refresh_calls"] == 1


def test_terminal_refresh_clears_tokens_and_keeps_registration(auth, issuer):
    complete(auth, issuer)
    update_record(auth, expires_at=time.time() - 1)
    issuer["refresh_error"] = "refresh_token_reused"
    with pytest.raises(ChatGPTAuthError) as error:
        auth.access_token()
    assert error.value.code == "refresh_token_reused"
    record = json.loads(auth.credentials_path.read_text(encoding="utf-8"))
    assert record["client_id"] == "oaiapp_papermap_test" and record["subject"] == "test-subject"
    assert not any(k in record for k in ("access_token", "refresh_token", "id_token"))
    assert not auth.status()["ready"]


def test_temporary_refresh_error_preserves_credentials(auth, issuer):
    complete(auth, issuer)
    update_record(auth, expires_at=time.time() - 1)
    before = auth.credentials_path.read_bytes()
    issuer["refresh_network_error"] = True
    with pytest.raises(ChatGPTAuthError) as error:
        auth.access_token()
    assert error.value.code == "network_error"
    assert auth.credentials_path.read_bytes() == before


@pytest.mark.parametrize("revoke_status", [200, 503])
def test_logout_revokes_and_always_clears_only_tokens(auth, issuer, revoke_status):
    complete(auth, issuer)
    host = auth.host_path.read_bytes()
    issuer["revoke_status"] = revoke_status
    result = auth.logout()
    assert result["remote_revocation_confirmed"] is (revoke_status == 200)
    assert not auth.status()["ready"] and auth.host_path.read_bytes() == host
    record = json.loads(auth.credentials_path.read_text(encoding="utf-8"))
    assert record["client_id"] == "oaiapp_papermap_test" and "id_token" not in record
    revoke = next(r for r in issuer["calls"] if r.url.path.endswith("/revoke"))
    assert parse_qs(revoke.content.decode())["token_type_hint"] == ["refresh_token"]


def test_browser_launch_failure_retains_manual_link(auth, issuer):
    def cannot_open(_):
        raise OSError("no browser")
    auth._browser_open = cannot_open
    result, _ = begin(auth, issuer)
    assert result["browser_opened"] is False and result["url"].startswith(authmod.AUTHORIZE_URL)
    assert auth.status()["pending"]


def test_singleton_shares_active_login(tmp_path, monkeypatch):
    monkeypatch.setattr(authmod.webbrowser, "open", lambda _: False)
    a = authmod.get_auth(tmp_path)
    try:
        assert authmod.get_auth(tmp_path / ".") is a
        a.start()
        with pytest.raises(ChatGPTAuthError) as error:
            authmod.get_auth(tmp_path).start()
        assert error.value.code == "login_pending"
    finally:
        authmod.close_all_auth()
