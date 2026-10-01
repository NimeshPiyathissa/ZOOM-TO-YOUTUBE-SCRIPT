"""Google OAuth 2.0 (PKCE) for the YouTube Data API (Part 4) - creating
unlisted broadcasts and reading their health, on behalf of the operator's
own YouTube channel. Deliberately separate from accounts.py's session
verification (Part 2): that proves a specific VPS Chrome profile has a
live Google session; this proves the operator authorized this
application to call the YouTube Data API for their channel. An admin
completes the Google consent screen in their OWN browser (not noVNC -
there is no "which Chrome profile" question here, only "which channel").

What's stored where:
  - Client ID / Client Secret (from the operator's own Google Cloud
    Console OAuth client) and the refresh token: secret_store, the
    encrypted vault (Part 0) - see CLIENT_ID_KEY / CLIENT_SECRET_KEY /
    REFRESH_TOKEN_KEY below.
  - Everything else (connected channel id/title/email, granted scope,
    connected_at, last_refreshed_at, status, last error, and a non-secret
    *display hint* for the secret - last 4 characters, never enough to
    reconstruct it) - db.py's key/value settings table, as one JSON blob
    under SETTINGS_KEY. None of it is secret.
  - The short-lived access token lives only in this process's memory
    (module-level cache with its own expiry) - never persisted, never
    returned to the browser.

Client ID/Secret are validated before they're ever saved (see
set_client_credentials): a dashboard login autofilled into these fields
by a browser's password manager previously got saved as "the OAuth
client" (client_id="admin", client_secret=the admin password) and
produced Google's invalid_client error - see docs/README for the
incident. Validation here is what stops that from reaching Google at
all, rather than catching it after a failed redirect.

status is one of:
  disconnected  - never connected, or disconnected/revoked deliberately
  connected     - refresh token present and last known-good
  needs_reauth  - Google rejected the refresh token (revoked from the
                  operator's Google Account, the OAuth client's Testing
                  mode 7-day grant expired, or the consent screen was
                  reconfigured) - the stored refresh token is useless
                  and is cleared; only a fresh Connect can fix this.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import time
from urllib.parse import urlencode

import httpx

from . import db, secret_store

logger = logging.getLogger("zoom-stream.youtube_oauth")

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
TOKENINFO_ENDPOINT = "https://oauth2.googleapis.com/tokeninfo"
API_BASE = "https://www.googleapis.com/youtube/v3"
# youtube.force-ssl: the scope Google's own Live Streaming API docs ask
# for to manage broadcasts/streams - no narrower scope covers
# liveBroadcasts.insert/bind, and broader scopes like plain "youtube"
# grant nothing this feature needs beyond what force-ssl already covers.
# openid + email: only to learn WHICH Google account/channel this is (so
# accounts.py can tell the operator when it doesn't match the VPS Chrome
# profile they think it does) - not an extra permission over the channel.
SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl openid email"

SETTINGS_KEY = "youtube_oauth_state"
CLIENT_ID_KEY = "YOUTUBE_OAUTH_CLIENT_ID"
CLIENT_SECRET_KEY = "YOUTUBE_OAUTH_CLIENT_SECRET"
REFRESH_TOKEN_KEY = "YOUTUBE_OAUTH_REFRESH_TOKEN"

# A real Google OAuth Web-client ID: <numeric project ref>-<hash>.apps.googleusercontent.com.
# This is the single check that would have rejected "admin" outright.
CLIENT_ID_RE = re.compile(r"^[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com$")
# Current Google client secrets start with this; older ones (still valid)
# don't, so a mismatch is a warning, not a hard rejection.
CLIENT_SECRET_PREFIX = "GOCSPX-"

PENDING_FLOW_TTL_SECONDS = 600  # 10 minutes to complete Google's consent screen
HTTP_TIMEOUT = 15.0

_DEFAULT_STATE = {
    "status": "disconnected", "channel_id": None, "channel_title": None,
    "connected_email": None, "scope": None, "connected_at": None,
    "last_refreshed_at": None, "last_error": None, "last_error_at": None,
    "client_secret_hint": None,
}

# Shown inline on the Accounts page next to whichever Google error surfaced,
# so "invalid_client" etc. are never just dumped at the operator raw. Also
# used for the `error=` query param Google sometimes appends to the
# callback redirect itself (consent-screen-level failures).
GOOGLE_ERROR_HINTS: dict[str, str] = {
    "invalid_client": "Google rejected the Client ID/Secret pair itself. Re-check both values "
                       "against Google Cloud Console (APIs & Services -> Credentials) and Save again.",
    "redirect_uri_mismatch": "The redirect URI this request sent doesn't match what's registered for "
                              "this OAuth client. In Cloud Console, add this exact URI under Authorized "
                              "redirect URIs, then try Connect again.",
    "access_denied": "Access was denied on Google's consent screen. If this OAuth client is still in "
                      "Testing mode, make sure your Google account is added under OAuth consent screen -> "
                      "Test users, then try again.",
    "org_internal": "This OAuth client's consent screen is restricted to internal Workspace users. In "
                     "Cloud Console, change the OAuth consent screen's User type to External (Testing is fine).",
    "admin_policy_enforced": "A Google Workspace admin policy is blocking this app for this account. Use a "
                              "personal Google account, or ask the admin to allow it.",
    "disallowed_useragent": "Google blocked this request's browser. Open Connect in a normal browser tab, "
                             "not an embedded app or webview.",
    "invalid_request": "Google rejected the request as malformed - try Connect again; if it repeats, "
                        "re-save the Client ID/Secret.",
    "invalid_scope": "Google rejected the requested permissions. This is a bug in this app if it persists.",
}


class YouTubeOAuthError(Exception):
    """Base for every error this module raises - always safe to show the operator."""


class NotConfiguredError(YouTubeOAuthError):
    """No Client ID/Secret saved yet - set those before Connect."""


class NotConnectedError(YouTubeOAuthError):
    """Configured, but never connected (or disconnected) - nothing to refresh/call."""


class NeedsReauthError(YouTubeOAuthError):
    """The stored refresh token was rejected by Google - reconnect required."""


class QuotaExceededError(YouTubeOAuthError):
    """The YouTube Data API's daily quota is used up - try again later; the
    connection itself is still fine, nothing needs reconnecting."""


# ------------------------------------------------------------- pending flows

# state -> {"code_verifier": str, "session_id": str, "created_at": float}.
# In-memory only (single uvicorn worker, same pattern as deps.py's
# rate-limit buckets) - losing this on a process restart just means
# "start Connect again", not a security issue.
_pending: dict[str, dict] = {}

# Cached access token: {"token": str | None, "expires_at": float}. Never
# written to disk.
_access_cache: dict = {"token": None, "expires_at": 0.0}


def _prune_pending() -> None:
    cutoff = time.time() - PENDING_FLOW_TTL_SECONDS
    for state in [s for s, v in _pending.items() if v["created_at"] < cutoff]:
        del _pending[state]


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# ------------------------------------------------------------------ config

def is_configured() -> bool:
    if not secret_store.is_unlocked():
        return False
    return bool(secret_store.get_secret(CLIENT_ID_KEY) and secret_store.get_secret(CLIENT_SECRET_KEY))


def validate_client_id(client_id: str) -> None:
    if not CLIENT_ID_RE.match(client_id or ""):
        raise YouTubeOAuthError(
            "That doesn't look like a Google OAuth Client ID. It should look like "
            "123456789012-abc123def456.apps.googleusercontent.com - copy it from Google Cloud "
            "Console (APIs & Services -> Credentials), not a dashboard username or password."
        )


def set_client_credentials(client_id: str, client_secret: str) -> dict:
    """Validates and saves the Client ID/Secret. Returns {"warning": str|None}
    - a warning is informational (e.g. an unusual-looking secret) and does
    NOT stop the save; only a YouTubeOAuthError does that, and it means
    nothing was written to the vault."""
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not client_id or not client_secret:
        raise YouTubeOAuthError("Client ID and Client Secret are both required")
    validate_client_id(client_id)
    warning = None
    if not client_secret.startswith(CLIENT_SECRET_PREFIX):
        warning = (
            "This doesn't start with \"GOCSPX-\", which current Google client secrets do. "
            "It was still saved - double check it's really the Client Secret from Cloud Console, "
            "not something else, if Connect fails."
        )
    secret_store.set_secrets({CLIENT_ID_KEY: client_id, CLIENT_SECRET_KEY: client_secret})
    hint = client_secret[-4:] if len(client_secret) >= 4 else client_secret
    _save_state(client_secret_hint=hint)
    return {"warning": warning}


def client_secret_display() -> str:
    """Never the secret itself - a safe, non-secret hint derived from it at
    save time and stored in db.py's settings table (see set_client_credentials)."""
    if not is_configured():
        return "Not configured"
    hint = _load_state().get("client_secret_hint")
    return f"Configured, ends in ••••{hint}" if hint else "Configured"


# -------------------------------------------------------------------- state

def _load_state() -> dict:
    raw = db.get_setting(SETTINGS_KEY)
    if not raw:
        return dict(_DEFAULT_STATE)
    try:
        state = json.loads(raw)
    except ValueError:
        return dict(_DEFAULT_STATE)
    merged = dict(_DEFAULT_STATE)
    merged.update(state)
    return merged


def _save_state(**updates) -> dict:
    state = _load_state()
    state.update(updates)
    db.set_setting(SETTINGS_KEY, json.dumps(state))
    return state


def _mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        return f"{local[0]}•••@{domain}"
    return f"{local[0]}•••{local[-1]}@{domain}"


def status() -> dict:
    """Everything the /accounts page needs to render the YouTube API card -
    safe to return to the browser as-is (no secrets in here). client_id is
    shown in full: Google client IDs aren't secret (they appear in the
    browser's own address bar during consent) - only the secret is masked."""
    vault_locked = not secret_store.is_unlocked()
    state = _load_state()
    return {
        "vault_locked": vault_locked,
        "configured": False if vault_locked else is_configured(),
        "client_id": "" if vault_locked else (secret_store.get_secret(CLIENT_ID_KEY) or ""),
        "client_secret_display": "Vault is locked" if vault_locked else client_secret_display(),
        "status": state["status"],
        "channel_id": state.get("channel_id"),
        "channel_title": state.get("channel_title"),
        "connected_email_masked": _mask_email(state.get("connected_email")),
        "scope": state.get("scope"),
        "connected_at": state.get("connected_at"),
        "last_refreshed_at": state.get("last_refreshed_at"),
        "last_error": state.get("last_error"),
        "last_error_at": state.get("last_error_at"),
    }


def connected_email_raw() -> str | None:
    """Unmasked connected identity email, for server-side comparison only
    (accounts.py matching an OAuth connection to an account) - never
    returned from status() / the API."""
    return _load_state().get("connected_email")


def linking_info() -> dict:
    """The minimal, server-internal view accounts.py needs to decide
    whether this OAuth connection belongs to a given account - deliberately
    separate from status() so nothing ever has to remember to mask an email
    before it reaches a template."""
    state = _load_state()
    return {
        "configured": is_configured(),
        "status": state["status"],
        "connected_email": state.get("connected_email"),
    }


# --------------------------------------------------------------- the flow

def start(redirect_uri: str, session_id: str) -> str:
    """Begins a PKCE authorization-code flow. Returns the URL to send the
    operator's browser to - a normal top-level redirect; Google's consent
    screen is never framed (this app's CSP wouldn't allow it either).
    `session_id` binds the flow to the dashboard session that started it,
    so completing someone else's Connect link (login-CSRF) can't happen."""
    if not is_configured():
        raise NotConfiguredError("Save a Client ID and Client Secret first")
    _prune_pending()
    client_id = secret_store.get_secret(CLIENT_ID_KEY)
    code_verifier = _b64url(secrets.token_bytes(64))
    code_challenge = _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
    state = _b64url(secrets.token_bytes(32))
    _pending[state] = {"code_verifier": code_verifier, "session_id": session_id, "created_at": time.time()}
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        # Forces Google to hand back a refresh_token even on a repeat
        # consent (normally only issued on the very first grant) - this
        # feature is useless without one, so always ask.
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def _channel_info(access_token: str) -> dict:
    r = httpx.get(
        f"{API_BASE}/channels",
        params={"part": "snippet", "mine": "true"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    items = r.json().get("items") or []
    if not items:
        return {"channel_id": None, "channel_title": None}
    ch = items[0]
    return {"channel_id": ch.get("id"), "channel_title": (ch.get("snippet") or {}).get("title")}


def _identity_email(id_token: str | None) -> str | None:
    """Verifies the id_token with Google's own tokeninfo endpoint (Google
    does the signature/audience/expiry checks server-side and hands back
    the claims) rather than decoding and verifying a JWT locally - avoids
    pulling in a JWT/crypto dependency for one field. Never raises: a
    failure here just means the identity email is unknown, not that the
    connection itself failed."""
    if not id_token:
        return None
    try:
        r = httpx.get(TOKENINFO_ENDPOINT, params={"id_token": id_token}, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return None
        claims = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    return claims.get("email") if claims.get("email_verified") in ("true", True) else claims.get("email")


def _token_error_message(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return f"Google rejected the request (HTTP {r.status_code})"
    err = body.get("error", "")
    if err == "invalid_grant":
        return "Google rejected the authorization code (expired or already used) - try Connect again"
    if err in GOOGLE_ERROR_HINTS:
        return GOOGLE_ERROR_HINTS[err]
    desc = body.get("error_description") or err or f"HTTP {r.status_code}"
    return f"Google rejected the request: {desc}"


def complete(code: str, state: str, redirect_uri: str, session_id: str) -> dict:
    """Exchanges Google's authorization code for tokens, looks up the
    connected channel and identity, and persists the refresh token +
    metadata. Raises YouTubeOAuthError with a message that's safe to show
    the operator - never includes the code, tokens, or client secret."""
    _prune_pending()
    pending = _pending.pop(state, None)
    if pending is None:
        raise YouTubeOAuthError("This sign-in link expired or was already used - start Connect again")
    if pending.get("session_id") and pending["session_id"] != session_id:
        raise YouTubeOAuthError(
            "This browser session changed since Connect started - sign in to the dashboard again and retry"
        )
    if not is_configured():
        raise NotConfiguredError("Client ID/Secret went missing mid-flow - save them again and retry")
    client_id = secret_store.get_secret(CLIENT_ID_KEY)
    client_secret = secret_store.get_secret(CLIENT_SECRET_KEY)
    try:
        r = httpx.post(
            TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": pending["code_verifier"],
            },
            timeout=HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise YouTubeOAuthError("Couldn't reach Google to complete sign-in - try again") from exc
    if r.status_code != 200:
        raise YouTubeOAuthError(_token_error_message(r))
    tok = r.json()
    refresh_token = tok.get("refresh_token")
    access_token = tok.get("access_token")
    if not refresh_token:
        raise YouTubeOAuthError(
            "Google didn't return a refresh token. Remove this app's access at "
            "https://myaccount.google.com/permissions and try Connect again."
        )
    scope = tok.get("scope", SCOPE)
    _access_cache["token"] = access_token
    _access_cache["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    try:
        info = _channel_info(access_token)
    except httpx.HTTPError:
        info = {"channel_id": None, "channel_title": None}
    email = _identity_email(tok.get("id_token"))
    secret_store.set_secrets({REFRESH_TOKEN_KEY: refresh_token})
    now = time.time()
    return _save_state(
        status="connected", channel_id=info["channel_id"], channel_title=info["channel_title"],
        connected_email=email, scope=scope, connected_at=now, last_refreshed_at=now,
        last_error=None, last_error_at=None,
    )


# --------------------------------------------------------------- access token

def _refresh_access_token() -> str:
    """Mints a fresh access token from the stored refresh token. Raises
    NeedsReauthError (and clears the stored refresh token + flips status)
    if Google rejects it as revoked/expired."""
    if not is_configured():
        raise NotConfiguredError("Save a Client ID and Client Secret first")
    refresh_token = secret_store.get_secret(REFRESH_TOKEN_KEY)
    if not refresh_token:
        raise NotConnectedError("Not connected yet - use Connect first")
    client_id = secret_store.get_secret(CLIENT_ID_KEY)
    client_secret = secret_store.get_secret(CLIENT_SECRET_KEY)
    try:
        r = httpx.post(
            TOKEN_ENDPOINT,
            data={
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
            },
            timeout=HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise YouTubeOAuthError("Couldn't reach Google to refresh the connection") from exc
    if r.status_code != 200:
        body: dict = {}
        try:
            body = r.json()
        except ValueError:
            pass
        if body.get("error") == "invalid_grant":
            secret_store.set_secrets({REFRESH_TOKEN_KEY: ""})
            msg = ("Google revoked or expired this connection - reconnect required. If this OAuth "
                   "client's consent screen is still in Testing mode, Google expires refresh tokens "
                   "after 7 days; publish the consent screen (or reconnect weekly) to avoid this.")
            _save_state(status="needs_reauth", last_error=msg, last_error_at=time.time())
            raise NeedsReauthError(msg)
        msg = _token_error_message(r)
        _save_state(last_error=msg, last_error_at=time.time())
        raise YouTubeOAuthError(msg)
    tok = r.json()
    access_token = tok["access_token"]
    _access_cache["token"] = access_token
    _access_cache["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    _save_state(last_refreshed_at=time.time(), last_error=None, last_error_at=None)
    return access_token


def get_access_token() -> str:
    """Cached in-process; refreshes from the stored refresh token once it's
    within 60s of expiry (or missing)."""
    if _access_cache["token"] and time.time() < _access_cache["expires_at"]:
        return _access_cache["token"]
    return _refresh_access_token()


def check_connection() -> dict:
    """"Test connection" action: forces a real refresh-token grant call
    right now (bypassing the access-token cache), then makes an actual
    YouTube Data API call (channels.list mine=true) so a stale "connected"
    badge can't hide a revoked/expired connection, insufficient scope, or a
    channel that no longer resolves. Raises the same errors as any other
    call that needs a fresh token."""
    access_token = _refresh_access_token()
    try:
        info = _channel_info(access_token)
    except httpx.HTTPError as exc:
        resp = getattr(exc, "response", None)
        if resp is not None and resp.status_code == 403 and "quotaExceeded" in resp.text:
            raise QuotaExceededError("YouTube API daily quota exceeded - try again later") from exc
        if resp is not None and resp.status_code == 403:
            raise YouTubeOAuthError(
                "The connected token doesn't have permission to call channels.list - disconnect "
                "and Connect again to grant the youtube.force-ssl scope"
            ) from exc
        raise YouTubeOAuthError("Couldn't reach the YouTube Data API to test the connection") from exc
    _save_state(channel_id=info["channel_id"], channel_title=info["channel_title"])
    return status()


def disconnect() -> None:
    """Best-effort revoke with Google, then clears the stored refresh
    token and resets status regardless of whether the revoke call
    succeeded - a network hiccup shouldn't leave a "connected" account
    the operator just asked to disconnect."""
    refresh_token = secret_store.get_secret(REFRESH_TOKEN_KEY) if secret_store.is_unlocked() else ""
    if refresh_token:
        try:
            httpx.post(REVOKE_ENDPOINT, params={"token": refresh_token}, timeout=HTTP_TIMEOUT)
        except httpx.HTTPError:
            logger.warning("YouTube OAuth revoke call failed (continuing to disconnect locally)")
    if secret_store.is_unlocked():
        secret_store.set_secrets({REFRESH_TOKEN_KEY: ""})
    _access_cache["token"] = None
    _access_cache["expires_at"] = 0.0
    _save_state(status="disconnected", channel_id=None, channel_title=None, connected_email=None,
                scope=None, connected_at=None, last_error=None, last_error_at=None)


# ------------------------------------------------------------- the API itself

def _api_error_message(r: httpx.Response) -> str:
    try:
        body = r.json()
        err = body.get("error", {})
        errors = err.get("errors") or []
        reason = errors[0].get("reason") if errors else None
        if reason == "insufficientPermissions":
            return ("The connected token doesn't have the youtube.force-ssl scope needed for this - "
                    "disconnect and Connect again to grant it")
        return err.get("message") or f"YouTube API error (HTTP {r.status_code})"
    except ValueError:
        return f"YouTube API error (HTTP {r.status_code})"


def _api_call(method: str, path: str, **kwargs) -> dict:
    token = get_access_token()
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    try:
        r = httpx.request(method, f"{API_BASE}/{path}", headers=headers, timeout=HTTP_TIMEOUT, **kwargs)
    except httpx.HTTPError as exc:
        raise YouTubeOAuthError("Couldn't reach the YouTube Data API") from exc
    if r.status_code == 403 and "quotaExceeded" in r.text:
        raise QuotaExceededError("YouTube API daily quota exceeded - try again later")
    if r.status_code == 401:
        # The cached access token was invalidated server-side between
        # calls (get_access_token()'s own expiry math didn't know) - one
        # forced-refresh retry before giving up.
        _access_cache["token"] = None
        token = get_access_token()
        headers["Authorization"] = f"Bearer {token}"
        try:
            r = httpx.request(method, f"{API_BASE}/{path}", headers=headers, timeout=HTTP_TIMEOUT, **kwargs)
        except httpx.HTTPError as exc:
            raise YouTubeOAuthError("Couldn't reach the YouTube Data API") from exc
    if r.status_code >= 400:
        raise YouTubeOAuthError(_api_error_message(r))
    return r.json() if r.content else {}


def create_unlisted_broadcast(title: str, description: str = "") -> dict:
    """liveBroadcasts.insert + liveStreams.insert + bind, all set to
    unlisted. Returns {broadcast_id, stream_id, watch_url,
    ingestion_address, stream_name} - the ingestion_address/stream_name
    are the RTMP URL and stream key an operator could paste into
    YT_STREAM_KEY instead of one created by hand in YouTube Studio."""
    title = (title or "").strip()
    if not title:
        raise YouTubeOAuthError("Give the broadcast a title")
    broadcast = _api_call(
        "POST", "liveBroadcasts",
        params={"part": "snippet,status,contentDetails"},
        json={
            "snippet": {"title": title[:150], "description": description[:5000]},
            "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False},
            "contentDetails": {"enableAutoStart": True, "enableAutoStop": True},
        },
    )
    stream = _api_call(
        "POST", "liveStreams",
        params={"part": "snippet,cdn"},
        json={
            "snippet": {"title": title[:150]},
            "cdn": {"frameRate": "variable", "ingestionType": "rtmp", "resolution": "variable"},
        },
    )
    _api_call(
        "POST", "liveBroadcasts/bind",
        params={"id": broadcast["id"], "part": "id,contentDetails", "streamId": stream["id"]},
    )
    ingestion = (stream.get("cdn") or {}).get("ingestionInfo") or {}
    return {
        "broadcast_id": broadcast["id"],
        "stream_id": stream["id"],
        "watch_url": f"https://www.youtube.com/watch?v={broadcast['id']}",
        "ingestion_address": ingestion.get("ingestionAddress"),
        "stream_name": ingestion.get("streamName"),
    }


def get_broadcast_health(broadcast_id: str) -> dict:
    """liveBroadcasts.list -> status.lifeCycleStatus, plus the bound
    stream's healthStatus (liveStreams.list) - the two things an
    operator actually wants to know: is it live, and is the ingest
    healthy."""
    broadcast_id = (broadcast_id or "").strip()
    if not broadcast_id:
        raise YouTubeOAuthError("Broadcast id is required")
    b = _api_call("GET", "liveBroadcasts", params={"part": "status,contentDetails", "id": broadcast_id})
    items = b.get("items") or []
    if not items:
        raise YouTubeOAuthError("No broadcast found with that id")
    bd = items[0]
    life_cycle = (bd.get("status") or {}).get("lifeCycleStatus")
    stream_id = (bd.get("contentDetails") or {}).get("boundStreamId")
    health = {"status": None, "configuration_issues": []}
    if stream_id:
        s = _api_call("GET", "liveStreams", params={"part": "status", "id": stream_id})
        s_items = s.get("items") or []
        if s_items:
            hs = (s_items[0].get("status") or {}).get("healthStatus") or {}
            health = {"status": hs.get("status"), "configuration_issues": hs.get("configurationIssues", [])}
    return {"life_cycle_status": life_cycle, "stream_health": health}
