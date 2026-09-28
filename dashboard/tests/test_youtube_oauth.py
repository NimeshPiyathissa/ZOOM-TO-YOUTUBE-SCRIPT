"""Tests for app/youtube_oauth.py (Part 4: the YouTube Data API's own
OAuth connection - separate from accounts.py's session verification).
No test here ever contacts Google or YouTube: every httpx.get/post/
request call the module makes is monkeypatched to a scripted fake
response, so these exercise exactly the token-exchange, refresh,
error-classification and vault/state-persistence logic that runs
against Google's real responses on the VPS.

test_refresh_invalid_grant_flips_to_needs_reauth is the central "prove a
revoked/expired connection can't keep reporting connected" check -
mirrors test_accounts.py's false-green proof for the session-verification
feature.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app import config, crypto, db, secret_store, youtube_oauth as yo


# ---------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _isolated_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SECRET_STORE_FILE", tmp_path / "secrets.enc.json")
    monkeypatch.setattr(config, "MASTER_KEY_CACHE_DIR", tmp_path / "etc-zoom-stream")
    monkeypatch.setattr(config, "MASTER_KEY_CACHE_FILE", tmp_path / "etc-zoom-stream" / "master.key")
    monkeypatch.setattr(config, "VAULT_LOCK_SENTINEL_FILE", tmp_path / "vault.locked")
    monkeypatch.setattr(crypto, "ARGON2_TIME_COST", 1)
    monkeypatch.setattr(crypto, "ARGON2_MEMORY_COST_KIB", 8192)
    monkeypatch.setattr(crypto, "ARGON2_PARALLELISM", 1)
    secret_store._key = None
    secret_store._secrets = None
    yield
    secret_store._key = None
    secret_store._secrets = None


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def _reset_module_state():
    yo._pending.clear()
    yo._access_cache["token"] = None
    yo._access_cache["expires_at"] = 0.0
    yield
    yo._pending.clear()
    yo._access_cache["token"] = None
    yo._access_cache["expires_at"] = 0.0


@pytest.fixture
def unlocked_vault():
    secret_store.initialize("master password", unlock_mode="prompt")
    yield


@pytest.fixture
def configured(unlocked_vault):
    yo.set_client_credentials("client-id-123", "client-secret-abc")
    yield


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body
        self.text = text if text else (json.dumps(json_body) if json_body is not None else "")
        self.content = self.text.encode("utf-8")

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


TOKEN_OK = {
    "access_token": "access-tok-1", "refresh_token": "refresh-tok-1",
    "expires_in": 3600, "scope": yo.SCOPE, "token_type": "Bearer",
}
CHANNEL_OK = {"items": [{"id": "UC12345", "snippet": {"title": "My Channel"}}]}


# --------------------------------------------------------------- start()

def test_start_requires_configuration(unlocked_vault):
    with pytest.raises(yo.NotConfiguredError):
        yo.start("https://zoom.missakaart.lk/api/youtube/oauth/callback")


def test_start_builds_pkce_url_and_records_pending(configured):
    url = yo.start("https://zoom.missakaart.lk/api/youtube/oauth/callback")
    assert url.startswith(yo.AUTH_ENDPOINT + "?")
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert "client_id=client-id-123" in url
    assert len(yo._pending) == 1
    state = next(iter(yo._pending))
    assert f"state={state}" in url


# ------------------------------------------------------------- complete()

def test_complete_unknown_state_rejected(configured):
    with pytest.raises(yo.YouTubeOAuthError, match="expired or was already used"):
        yo.complete("some-code", "bogus-state", "https://zoom.missakaart.lk/cb")


def test_complete_success_stores_refresh_token_and_channel(configured, monkeypatch):
    url = yo.start("https://zoom.missakaart.lk/cb")
    state = next(iter(yo._pending))

    def fake_post(endpoint, data=None, timeout=None):
        assert endpoint == yo.TOKEN_ENDPOINT
        assert data["grant_type"] == "authorization_code"
        assert data["code_verifier"]
        return FakeResponse(200, TOKEN_OK)

    def fake_get(endpoint, params=None, headers=None, timeout=None):
        assert headers["Authorization"] == "Bearer access-tok-1"
        return FakeResponse(200, CHANNEL_OK)

    monkeypatch.setattr(yo.httpx, "post", fake_post)
    monkeypatch.setattr(yo.httpx, "get", fake_get)

    result = yo.complete("auth-code", state, "https://zoom.missakaart.lk/cb")

    assert result["status"] == "connected"
    assert result["channel_id"] == "UC12345"
    assert result["channel_title"] == "My Channel"
    assert secret_store.get_secret(yo.REFRESH_TOKEN_KEY) == "refresh-tok-1"
    # single-use: the state can't be replayed
    assert state not in yo._pending
    st = yo.status()
    assert st["status"] == "connected"
    assert st["channel_title"] == "My Channel"


def test_complete_without_refresh_token_raises_and_stores_nothing(configured, monkeypatch):
    yo.start("https://zoom.missakaart.lk/cb")
    state = next(iter(yo._pending))
    body = dict(TOKEN_OK)
    del body["refresh_token"]
    monkeypatch.setattr(yo.httpx, "post", lambda *a, **k: FakeResponse(200, body))
    with pytest.raises(yo.YouTubeOAuthError, match="didn't return a refresh token"):
        yo.complete("auth-code", state, "https://zoom.missakaart.lk/cb")
    assert secret_store.get_secret(yo.REFRESH_TOKEN_KEY) == ""
    assert yo.status()["status"] == "disconnected"


def test_complete_token_endpoint_error(configured, monkeypatch):
    yo.start("https://zoom.missakaart.lk/cb")
    state = next(iter(yo._pending))
    monkeypatch.setattr(
        yo.httpx, "post",
        lambda *a, **k: FakeResponse(400, {"error": "invalid_grant", "error_description": "Bad code"}),
    )
    with pytest.raises(yo.YouTubeOAuthError, match="expired or already used"):
        yo.complete("auth-code", state, "https://zoom.missakaart.lk/cb")


# ------------------------------------------------------- access token cache

def _connect(monkeypatch):
    yo.start("https://zoom.missakaart.lk/cb")
    state = next(iter(yo._pending))
    monkeypatch.setattr(yo.httpx, "post", lambda *a, **k: FakeResponse(200, TOKEN_OK))
    monkeypatch.setattr(yo.httpx, "get", lambda *a, **k: FakeResponse(200, CHANNEL_OK))
    yo.complete("auth-code", state, "https://zoom.missakaart.lk/cb")


def test_get_access_token_uses_cache_without_a_new_call(configured, monkeypatch):
    _connect(monkeypatch)
    calls = {"n": 0}

    def fail_if_called(*a, **k):
        calls["n"] += 1
        raise AssertionError("should not refresh - cache is still fresh")

    monkeypatch.setattr(yo.httpx, "post", fail_if_called)
    assert yo.get_access_token() == "access-tok-1"
    assert calls["n"] == 0


def test_get_access_token_refreshes_once_expired(configured, monkeypatch):
    _connect(monkeypatch)
    yo._access_cache["expires_at"] = 0.0  # force expiry

    def fake_refresh_post(endpoint, data=None, timeout=None):
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "refresh-tok-1"
        return FakeResponse(200, {"access_token": "access-tok-2", "expires_in": 3600})

    monkeypatch.setattr(yo.httpx, "post", fake_refresh_post)
    assert yo.get_access_token() == "access-tok-2"
    assert yo.status()["last_refreshed_at"] is not None


def test_refresh_invalid_grant_flips_to_needs_reauth(configured, monkeypatch):
    """The false-green proof for this feature: once Google rejects the
    refresh token, status must say needs_reauth, not connected - and the
    useless refresh token must be cleared so nothing keeps retrying it
    silently."""
    _connect(monkeypatch)
    yo._access_cache["expires_at"] = 0.0
    monkeypatch.setattr(
        yo.httpx, "post",
        lambda *a, **k: FakeResponse(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}),
    )
    with pytest.raises(yo.NeedsReauthError):
        yo.get_access_token()
    st = yo.status()
    assert st["status"] == "needs_reauth"
    assert "revoked or expired" in st["last_error"]
    assert secret_store.get_secret(yo.REFRESH_TOKEN_KEY) == ""


def test_refresh_without_connection_raises_not_connected(configured):
    with pytest.raises(yo.NotConnectedError):
        yo.get_access_token()


# ------------------------------------------------------------- disconnect()

def test_disconnect_revokes_clears_vault_and_resets_status(configured, monkeypatch):
    _connect(monkeypatch)
    revoked = {}

    def fake_revoke_post(endpoint, params=None, timeout=None):
        revoked["token"] = params["token"]
        return FakeResponse(200, {})

    monkeypatch.setattr(yo.httpx, "post", fake_revoke_post)
    yo.disconnect()
    assert revoked["token"] == "refresh-tok-1"
    assert secret_store.get_secret(yo.REFRESH_TOKEN_KEY) == ""
    st = yo.status()
    assert st["status"] == "disconnected"
    assert st["channel_title"] is None
    assert yo._access_cache["token"] is None


def test_disconnect_survives_revoke_network_failure(configured, monkeypatch):
    _connect(monkeypatch)

    def raise_network_error(*a, **k):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(yo.httpx, "post", raise_network_error)
    yo.disconnect()  # must not raise
    assert yo.status()["status"] == "disconnected"
    assert secret_store.get_secret(yo.REFRESH_TOKEN_KEY) == ""


def test_disconnect_when_vault_locked_is_a_safe_noop_for_the_vault(configured, monkeypatch):
    _connect(monkeypatch)
    secret_store.lock()
    monkeypatch.setattr(yo.httpx, "post", lambda *a, **k: FakeResponse(200, {}))
    yo.disconnect()  # must not raise despite the locked vault
    assert yo.status()["status"] == "disconnected"


# --------------------------------------------------------------- API calls

def test_create_unlisted_broadcast_full_flow(configured, monkeypatch):
    _connect(monkeypatch)
    calls = []

    def fake_request(method, url, headers=None, timeout=None, params=None, json=None):
        calls.append((method, url, params))
        if url.endswith("/liveBroadcasts") and method == "POST":
            return FakeResponse(200, {"id": "bcast-1"})
        if url.endswith("/liveStreams") and method == "POST":
            return FakeResponse(200, {
                "id": "stream-1",
                "cdn": {"ingestionInfo": {"ingestionAddress": "rtmp://a.rtmp.youtube.com/live2", "streamName": "abcd-1234"}},
            })
        if url.endswith("/liveBroadcasts/bind") and method == "POST":
            return FakeResponse(200, {"id": "bcast-1"})
        raise AssertionError(f"unexpected call {method} {url}")

    monkeypatch.setattr(yo.httpx, "request", fake_request)
    result = yo.create_unlisted_broadcast("My Stream", "desc")
    assert result["broadcast_id"] == "bcast-1"
    assert result["stream_id"] == "stream-1"
    assert result["watch_url"] == "https://www.youtube.com/watch?v=bcast-1"
    assert result["ingestion_address"] == "rtmp://a.rtmp.youtube.com/live2"
    assert result["stream_name"] == "abcd-1234"
    assert len(calls) == 3


def test_create_unlisted_broadcast_requires_title(configured, monkeypatch):
    _connect(monkeypatch)
    with pytest.raises(yo.YouTubeOAuthError, match="title"):
        yo.create_unlisted_broadcast("   ")


def test_quota_exceeded_does_not_disturb_connected_status(configured, monkeypatch):
    _connect(monkeypatch)

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        return FakeResponse(403, {"error": {"errors": [{"reason": "quotaExceeded"}], "message": "quota"}},
                             text='{"error": {"errors": [{"reason": "quotaExceeded"}]}}')

    monkeypatch.setattr(yo.httpx, "request", fake_request)
    with pytest.raises(yo.QuotaExceededError):
        yo.create_unlisted_broadcast("My Stream")
    # the connection itself is still fine - quota is not a reauth problem
    assert yo.status()["status"] == "connected"


def test_get_broadcast_health_combines_lifecycle_and_stream_health(configured, monkeypatch):
    _connect(monkeypatch)

    def fake_request(method, url, headers=None, timeout=None, params=None, **kwargs):
        if url.endswith("/liveBroadcasts"):
            return FakeResponse(200, {"items": [{
                "status": {"lifeCycleStatus": "live"},
                "contentDetails": {"boundStreamId": "stream-1"},
            }]})
        if url.endswith("/liveStreams"):
            return FakeResponse(200, {"items": [{
                "status": {"healthStatus": {"status": "good", "configurationIssues": []}},
            }]})
        raise AssertionError(f"unexpected call {url}")

    monkeypatch.setattr(yo.httpx, "request", fake_request)
    health = yo.get_broadcast_health("bcast-1")
    assert health["life_cycle_status"] == "live"
    assert health["stream_health"]["status"] == "good"


def test_get_broadcast_health_missing_broadcast_raises(configured, monkeypatch):
    _connect(monkeypatch)
    monkeypatch.setattr(yo.httpx, "request", lambda *a, **k: FakeResponse(200, {"items": []}))
    with pytest.raises(yo.YouTubeOAuthError, match="No broadcast found"):
        yo.get_broadcast_health("bcast-missing")


# ------------------------------------------------------------------ status()

def test_status_reports_vault_locked(configured):
    secret_store.lock()
    st = yo.status()
    assert st["vault_locked"] is True
    assert st["configured"] is False


def test_set_client_credentials_requires_both_fields(unlocked_vault):
    with pytest.raises(yo.YouTubeOAuthError):
        yo.set_client_credentials("", "secret")
    with pytest.raises(yo.YouTubeOAuthError):
        yo.set_client_credentials("id", "")


def test_masked_client_id(configured):
    assert yo.masked_client_id() == "client-i…-123"


# ------------------------------------------------------------------ routes

@pytest.fixture
def client():
    import time as _time
    from starlette.testclient import TestClient
    from app.main import app
    from app import security

    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            ("admin", security.hash_password("correct-horse-battery-staple"), _time.time()),
        )
        user_id = cur.lastrowid
    session_id, csrf_token = security.create_session(user_id, "127.0.0.1")
    c = TestClient(app, cookies={config.COOKIE_NAME: session_id})
    c.csrf = csrf_token
    return c


@pytest.fixture
def anon_client():
    from starlette.testclient import TestClient
    from app.main import app
    return TestClient(app)


def test_accounts_page_shows_locked_banner_when_vault_locked(client):
    res = client.get("/accounts")
    assert res.status_code == 200
    assert "YouTube Data API" in res.text
    assert "vault is locked" in res.text
    assert "Not connected" in res.text


def test_accounts_page_shows_connected_channel(client, configured, monkeypatch):
    _connect(monkeypatch)
    res = client.get("/accounts")
    assert res.status_code == 200
    assert "My Channel" in res.text
    assert "api/youtube/oauth/callback" in res.text


def test_status_route_requires_auth(anon_client):
    res = anon_client.get("/api/youtube/oauth/status")
    assert res.status_code == 401


def test_config_route_requires_csrf(client):
    res = client.post("/api/youtube/oauth/config", json={"client_id": "a", "client_secret": "b"})
    assert res.status_code == 403


def test_config_then_start_route_returns_authorize_url(client, unlocked_vault):
    res = client.post(
        "/api/youtube/oauth/config",
        json={"client_id": "id-1", "client_secret": "secret-1"},
        headers={"X-CSRF-Token": client.csrf},
    )
    assert res.status_code == 200
    assert res.json()["configured"] is True

    res = client.post("/api/youtube/oauth/start", headers={"X-CSRF-Token": client.csrf})
    assert res.status_code == 200
    assert res.json()["authorize_url"].startswith(yo.AUTH_ENDPOINT)


def test_callback_route_error_param_redirects_with_reason(client):
    res = client.get("/api/youtube/oauth/callback?error=access_denied", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/accounts?yt_oauth=error&reason=")


def test_callback_route_success_redirects_connected(client, unlocked_vault, monkeypatch):
    client.post(
        "/api/youtube/oauth/config",
        json={"client_id": "id-1", "client_secret": "secret-1"},
        headers={"X-CSRF-Token": client.csrf},
    )
    start_res = client.post("/api/youtube/oauth/start", headers={"X-CSRF-Token": client.csrf})
    authorize_url = start_res.json()["authorize_url"]
    from urllib.parse import urlparse, parse_qs
    state = parse_qs(urlparse(authorize_url).query)["state"][0]

    monkeypatch.setattr(yo.httpx, "post", lambda *a, **k: FakeResponse(200, TOKEN_OK))
    monkeypatch.setattr(yo.httpx, "get", lambda *a, **k: FakeResponse(200, CHANNEL_OK))

    res = client.get(f"/api/youtube/oauth/callback?code=auth-code&state={state}", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/accounts?yt_oauth=connected#youtube-api"
