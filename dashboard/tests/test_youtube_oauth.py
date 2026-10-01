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


VALID_CLIENT_ID = "123456789012-abc123def456.apps.googleusercontent.com"
VALID_CLIENT_SECRET = "GOCSPX-abcSecret123"
SESSION_ID = "sess-test-1"


@pytest.fixture
def unlocked_vault():
    secret_store.initialize("master password", unlock_mode="prompt")
    yield


@pytest.fixture
def configured(unlocked_vault):
    yo.set_client_credentials(VALID_CLIENT_ID, VALID_CLIENT_SECRET)
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
        yo.start("https://zoom.missakaart.lk/api/youtube/oauth/callback", SESSION_ID)


def test_start_builds_pkce_url_and_records_pending(configured):
    url = yo.start("https://zoom.missakaart.lk/api/youtube/oauth/callback", SESSION_ID)
    assert url.startswith(yo.AUTH_ENDPOINT + "?")
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert f"client_id={VALID_CLIENT_ID}" in url
    assert len(yo._pending) == 1
    state = next(iter(yo._pending))
    assert f"state={state}" in url
    assert yo._pending[state]["session_id"] == SESSION_ID


# ------------------------------------------------------------- complete()

def test_complete_unknown_state_rejected(configured):
    with pytest.raises(yo.YouTubeOAuthError, match="expired or was already used"):
        yo.complete("some-code", "bogus-state", "https://zoom.missakaart.lk/cb", SESSION_ID)


def test_complete_session_mismatch_rejected(configured):
    yo.start("https://zoom.missakaart.lk/cb", SESSION_ID)
    state = next(iter(yo._pending))
    with pytest.raises(yo.YouTubeOAuthError, match="session changed"):
        yo.complete("auth-code", state, "https://zoom.missakaart.lk/cb", "a-different-session")


def test_complete_success_stores_refresh_token_channel_and_identity(configured, monkeypatch):
    yo.start("https://zoom.missakaart.lk/cb", SESSION_ID)
    state = next(iter(yo._pending))
    token_body = dict(TOKEN_OK, id_token="fake-id-token")

    def fake_post(endpoint, data=None, timeout=None):
        assert endpoint == yo.TOKEN_ENDPOINT
        assert data["grant_type"] == "authorization_code"
        assert data["code_verifier"]
        return FakeResponse(200, token_body)

    def fake_get(endpoint, params=None, headers=None, timeout=None):
        if endpoint == yo.TOKENINFO_ENDPOINT:
            assert params["id_token"] == "fake-id-token"
            return FakeResponse(200, {"email": "operator@gmail.com", "email_verified": "true"})
        assert headers["Authorization"] == "Bearer access-tok-1"
        return FakeResponse(200, CHANNEL_OK)

    monkeypatch.setattr(yo.httpx, "post", fake_post)
    monkeypatch.setattr(yo.httpx, "get", fake_get)

    result = yo.complete("auth-code", state, "https://zoom.missakaart.lk/cb", SESSION_ID)

    assert result["status"] == "connected"
    assert result["channel_id"] == "UC12345"
    assert result["channel_title"] == "My Channel"
    assert result["connected_email"] == "operator@gmail.com"
    assert secret_store.get_secret(yo.REFRESH_TOKEN_KEY) == "refresh-tok-1"
    # single-use: the state can't be replayed
    assert state not in yo._pending
    st = yo.status()
    assert st["status"] == "connected"
    assert st["channel_title"] == "My Channel"
    assert st["connected_email_masked"] == "o•••r@gmail.com"
    assert yo.connected_email_raw() == "operator@gmail.com"


def test_complete_without_refresh_token_raises_and_stores_nothing(configured, monkeypatch):
    yo.start("https://zoom.missakaart.lk/cb", SESSION_ID)
    state = next(iter(yo._pending))
    body = dict(TOKEN_OK)
    del body["refresh_token"]
    monkeypatch.setattr(yo.httpx, "post", lambda *a, **k: FakeResponse(200, body))
    with pytest.raises(yo.YouTubeOAuthError, match="didn't return a refresh token"):
        yo.complete("auth-code", state, "https://zoom.missakaart.lk/cb", SESSION_ID)
    assert secret_store.get_secret(yo.REFRESH_TOKEN_KEY) == ""
    assert yo.status()["status"] == "disconnected"


def test_complete_token_endpoint_error(configured, monkeypatch):
    yo.start("https://zoom.missakaart.lk/cb", SESSION_ID)
    state = next(iter(yo._pending))
    monkeypatch.setattr(
        yo.httpx, "post",
        lambda *a, **k: FakeResponse(400, {"error": "invalid_grant", "error_description": "Bad code"}),
    )
    with pytest.raises(yo.YouTubeOAuthError, match="expired or already used"):
        yo.complete("auth-code", state, "https://zoom.missakaart.lk/cb", SESSION_ID)


def test_complete_invalid_client_error_uses_hint(configured, monkeypatch):
    yo.start("https://zoom.missakaart.lk/cb", SESSION_ID)
    state = next(iter(yo._pending))
    monkeypatch.setattr(
        yo.httpx, "post",
        lambda *a, **k: FakeResponse(400, {"error": "invalid_client"}),
    )
    with pytest.raises(yo.YouTubeOAuthError, match="Client ID/Secret pair itself"):
        yo.complete("auth-code", state, "https://zoom.missakaart.lk/cb", SESSION_ID)


# ------------------------------------------------------- access token cache

def _connect(monkeypatch):
    yo.start("https://zoom.missakaart.lk/cb", SESSION_ID)
    state = next(iter(yo._pending))
    monkeypatch.setattr(yo.httpx, "post", lambda *a, **k: FakeResponse(200, TOKEN_OK))
    monkeypatch.setattr(yo.httpx, "get", lambda *a, **k: FakeResponse(200, CHANNEL_OK))
    yo.complete("auth-code", state, "https://zoom.missakaart.lk/cb", SESSION_ID)


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


# ----------------------------------------------------- check_connection()

def test_check_connection_makes_a_real_channels_list_call(configured, monkeypatch):
    """Part 2's "Test connection" requirement: not just a token refresh -
    an actual channels.list(mine=true) call, and the result (channel name)
    is what the operator sees."""
    _connect(monkeypatch)
    monkeypatch.setattr(yo.httpx, "post", lambda *a, **k: FakeResponse(200, {"access_token": "access-tok-2", "expires_in": 3600}))
    monkeypatch.setattr(yo.httpx, "get", lambda *a, **k: FakeResponse(200, {"items": [{"id": "UC999", "snippet": {"title": "Renamed Channel"}}]}))
    st = yo.check_connection()
    assert st["status"] == "connected"
    assert st["channel_title"] == "Renamed Channel"
    assert st["channel_id"] == "UC999"


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


# -------------------------------------------- root-cause regression tests
# A dashboard login autofilled into these fields by a browser password
# manager previously got saved as the OAuth client (client_id="admin",
# secret=the admin password) and reached Google as invalid_client. These
# prove that can't happen again: a malformed Client ID is rejected before
# anything is ever written to the vault.

def test_set_client_credentials_rejects_non_google_client_id(unlocked_vault):
    with pytest.raises(yo.YouTubeOAuthError, match="doesn't look like a Google OAuth Client ID"):
        yo.set_client_credentials("admin", "whatever-the-dashboard-password-was")
    assert not yo.is_configured()


def test_set_client_credentials_rejects_malformed_but_nonempty_id(unlocked_vault):
    for bad in ("123456", "not-an-id.apps.googleusercontent.com", "123-abc.example.com", ""):
        with pytest.raises(yo.YouTubeOAuthError):
            yo.set_client_credentials(bad, "GOCSPX-something")
    assert not yo.is_configured()


def test_set_client_credentials_accepts_real_looking_id(unlocked_vault):
    result = yo.set_client_credentials(VALID_CLIENT_ID, VALID_CLIENT_SECRET)
    assert result["warning"] is None
    assert yo.is_configured()
    assert yo.status()["client_id"] == VALID_CLIENT_ID


def test_set_client_credentials_warns_but_saves_non_gocspx_secret(unlocked_vault):
    result = yo.set_client_credentials(VALID_CLIENT_ID, "an-older-style-secret")
    assert result["warning"] is not None
    assert "GOCSPX-" in result["warning"]
    # a warning doesn't block the save - Connect is still usable
    assert yo.is_configured()


def test_client_secret_display_never_exposes_the_secret(configured):
    display = yo.client_secret_display()
    assert "123" in display  # last 4 chars of VALID_CLIENT_SECRET as a hint
    assert VALID_CLIENT_SECRET not in display
    assert "ends in" in display


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


def test_config_route_rejects_malformed_client_id(client, unlocked_vault):
    """End-to-end root-cause proof: posting client_id="admin" (what an
    autofilled dashboard login looks like) through the actual route is
    rejected inline - nothing is saved, and /start can't be reached from
    this state since configured stays False."""
    res = client.post(
        "/api/youtube/oauth/config",
        json={"client_id": "admin", "client_secret": "whatever-the-password-was"},
        headers={"X-CSRF-Token": client.csrf},
    )
    assert res.status_code == 400
    assert "doesn't look like a Google OAuth Client ID" in res.json()["error"]

    res = client.post("/api/youtube/oauth/start", headers={"X-CSRF-Token": client.csrf})
    assert res.status_code == 400  # NotConfiguredError - never got far enough to redirect to Google


def test_config_then_start_route_returns_authorize_url(client, unlocked_vault):
    res = client.post(
        "/api/youtube/oauth/config",
        json={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET},
        headers={"X-CSRF-Token": client.csrf},
    )
    assert res.status_code == 200
    assert res.json()["configured"] is True
    assert res.json()["client_secret_display"].endswith(VALID_CLIENT_SECRET[-4:])

    res = client.post("/api/youtube/oauth/start", headers={"X-CSRF-Token": client.csrf})
    assert res.status_code == 200
    assert res.json()["authorize_url"].startswith(yo.AUTH_ENDPOINT)


def test_callback_route_error_param_redirects_with_reason(client):
    from urllib.parse import unquote
    res = client.get("/api/youtube/oauth/callback?error=access_denied", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/accounts?yt_oauth=error&reason=")
    assert "Test users" in unquote(res.headers["location"])  # GOOGLE_ERROR_HINTS, not a bare error code


def test_callback_route_no_session_rejected(anon_client):
    """A GET to the callback with no dashboard session at all (cookie
    expired mid-flow) must not attempt complete() - just a clear redirect
    asking to log in and retry, never a crash or a silent no-op."""
    from urllib.parse import unquote
    res = anon_client.get("/api/youtube/oauth/callback?code=auth-code&state=whatever", follow_redirects=False)
    assert res.status_code == 303
    assert "session expired" in unquote(res.headers["location"])


def test_callback_route_success_redirects_connected(client, unlocked_vault, monkeypatch):
    client.post(
        "/api/youtube/oauth/config",
        json={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET},
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


def test_callback_route_rejects_a_different_sessions_state(client, unlocked_vault, monkeypatch):
    """Login-CSRF proof: the state issued to one dashboard session can't be
    completed by a request carrying a different session's cookie."""
    client.post(
        "/api/youtube/oauth/config",
        json={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET},
        headers={"X-CSRF-Token": client.csrf},
    )
    start_res = client.post("/api/youtube/oauth/start", headers={"X-CSRF-Token": client.csrf})
    authorize_url = start_res.json()["authorize_url"]
    from urllib.parse import urlparse, parse_qs
    state = parse_qs(urlparse(authorize_url).query)["state"][0]

    import time as _time
    from app import security as security_mod
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            ("someone-else", security_mod.hash_password("another-password"), _time.time()),
        )
        other_user_id = cur.lastrowid
    other_session_id, _ = security_mod.create_session(other_user_id, "127.0.0.1")

    from starlette.testclient import TestClient
    from app.main import app
    other_client = TestClient(app, cookies={config.COOKIE_NAME: other_session_id})

    from urllib.parse import unquote
    res = other_client.get(f"/api/youtube/oauth/callback?code=auth-code&state={state}", follow_redirects=False)
    assert res.status_code == 303
    assert "session changed" in unquote(res.headers["location"])
