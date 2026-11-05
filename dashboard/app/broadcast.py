"""Central broadcast authorization: the ONLY way ffmpeg-stream may be started or restarted.

Why this exists: before this module, anything that called control.unit_action("ffmpeg-stream", "start") could put the
meeting on air - a library "Join", a schedule, the pipeline-restart button, a watchdog. The "are you sure?" lived in the
browser only, so a stale tab or a direct API call skipped it. Now:

  * A grant is created by an authenticated, CSRF-protected request (POST /api/broadcast/authorize) after the operator has
    seen the confirmation (source + destination, warnings). It is short-lived (60 s), single-use, bound to the user, the
    purpose and the exact source revision, and held in server memory.
  * POST /api/stream/go-live (or /restart) must present that grant. Consuming it is atomic; a replay, a stale or foreign
    grant, a second concurrent start and a changed source are all refused with a specific code.
  * control.unit_action() refuses start/restart of the encoder unless it is handed the opaque Grant object that only this
    module can mint. Nothing else in the code base can start the encoder.
  * scripts/stream.sh independently refuses to run without a fresh single-use nonce (BROADCAST_AUTH_NONCE) in
    current-source.env, so a direct `systemctl start`, systemd's own restart policy, a boot or a stale dashboard cannot
    start a broadcast either. A crash is therefore never "healed": the encoder stays down, the diagnostics are kept and
    the operator must authorize again.

States (derived from systemd + the session record, never guessed):
  OFFLINE   nothing on air, nothing prepared        READY     a source is active and its producer is up, still OFFLINE
  STARTING  authorized, encoder coming up           LIVE      encoder running under an authorized session
  STOPPING  stop requested, encoder going down      ERROR     the encoder ended without a stop request, or failed to start
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from collections import OrderedDict

from . import db

GRANT_TTL_SECONDS = 60
EXEC_WINDOW_SECONDS = 30          # how long a consumed grant may be used to call unit_action
STARTING_GRACE_SECONDS = 25       # systemd needs a moment before the unit reports a PID
SESSION_KEY = "broadcast_session"
ERROR_KEY = "broadcast_last_error"

OFFLINE, READY, STARTING, LIVE, STOPPING, ERROR = "OFFLINE", "READY", "STARTING", "LIVE", "STOPPING", "ERROR"
PURPOSES = ("go_live", "restart")


class BroadcastError(Exception):
    """A refusal with a stable machine-readable code and an HTTP status for the API layer."""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


_ISSUER = object()                # only this module holds it: Grant objects cannot be forged elsewhere
_lock = threading.RLock()
_grants: dict[str, dict] = {}
_used: "OrderedDict[str, float]" = OrderedDict()
_expired: "OrderedDict[str, float]" = OrderedDict()          # remembered so the refusal can say "expired", not "unknown"


class Grant:
    """Opaque proof that an operator authorized THIS start. Created only by consume_grant()."""
    __slots__ = ("id", "purpose", "user", "source_id", "exec_until", "script_nonce", "script_expires", "_token")

    def __init__(self, token, gid, purpose, user, source_id, now):
        if token is not _ISSUER:
            raise TypeError("Grant objects can only be created by broadcast.consume_grant()")
        self._token = token
        self.id, self.purpose, self.user, self.source_id = gid, purpose, user, source_id
        self.exec_until = now + EXEC_WINDOW_SECONDS
        self.script_nonce = secrets.token_urlsafe(18)          # what stream.sh will verify and burn
        self.script_expires = int(now) + EXEC_WINDOW_SECONDS

    def valid(self, now: float | None = None) -> bool:
        return getattr(self, "_token", None) is _ISSUER and (now or time.time()) < self.exec_until

    def env(self) -> dict:
        """The keys written to current-source.env for scripts/stream.sh. Consumed (blanked) by the script on start."""
        return {"BROADCAST_AUTH_ID": self.id, "BROADCAST_AUTH_NONCE": self.script_nonce,
                "BROADCAST_AUTH_EXPIRES": str(self.script_expires)}


def empty_env() -> dict:
    return {"BROADCAST_AUTH_ID": "", "BROADCAST_AUTH_NONCE": "", "BROADCAST_AUTH_EXPIRES": ""}


def reset_for_tests() -> None:
    with _lock:
        _grants.clear()
        _used.clear()
        _expired.clear()


# ------------------------------------------------------------------ session record (settings table, JSON)

def _load(key: str) -> dict | None:
    try:
        raw = db.get_setting(key, "") or ""
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _store(key: str, value: dict | None) -> None:
    db.set_setting(key, json.dumps(value) if value else "")


def get_session() -> dict | None:
    return _load(SESSION_KEY)


def last_error() -> dict | None:
    return _load(ERROR_KEY)


def acknowledge_error() -> None:
    _store(ERROR_KEY, None)


def boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id") as fh:
            return fh.read().strip()
    except OSError:
        return "dev"


# ------------------------------------------------------------------ grants

def _prune(now: float) -> None:
    for k in [k for k, g in _grants.items() if g["expires"] < now]:
        _grants.pop(k, None)
        _expired[k] = now
    while len(_used) > 100:
        _used.popitem(last=False)
    while len(_expired) > 100:
        _expired.popitem(last=False)


def issue_grant(user: str, purpose: str, source: dict | None, source_rev: int, state: str, now: float | None = None) -> dict:
    """Step 1 (after the confirmation dialog): returns the grant the client must present with go-live/restart."""
    now = now or time.time()
    if purpose not in PURPOSES:
        raise BroadcastError("bad_purpose", "unknown authorization purpose", 400)
    if purpose == "go_live" and state in (LIVE, STARTING, STOPPING):
        raise BroadcastError("already_" + state.lower(), f"The broadcast is already {state.lower()} - nothing to start.")
    if purpose == "restart" and state != LIVE:
        raise BroadcastError("not_live", "The encoder is not live, so there is nothing to restart. Use Go Live.")
    with _lock:
        _prune(now)
        # One outstanding grant per user: a second dialog invalidates the first (no stockpiling, no parallel starts).
        for k in [k for k, g in _grants.items() if g["user"] == user]:
            _grants.pop(k, None)
        gid = secrets.token_urlsafe(24)
        _grants[gid] = {"user": user, "purpose": purpose, "source_id": (source or {}).get("id"), "rev": source_rev,
                        "expires": now + GRANT_TTL_SECONDS, "issued": now}
    return {"grant": gid, "expires_at": now + GRANT_TTL_SECONDS, "ttl_seconds": GRANT_TTL_SECONDS, "purpose": purpose}


def consume_grant(gid: str, user: str, purpose: str, source: dict | None, source_rev: int, now: float | None = None) -> Grant:
    """Step 2: atomically burns the grant. Every refusal is specific so the UI can say why."""
    now = now or time.time()
    with _lock:
        _prune(now)
        g = _grants.get(gid or "")
        if g is None:
            if gid in _expired:
                raise BroadcastError("grant_expired", "The confirmation expired. Press Go Live and confirm again.", 410)
            if gid in _used:
                raise BroadcastError("grant_used", "This authorization was already used. Authorize again to go live.", 409)
            raise BroadcastError("no_grant", "Going live needs a fresh confirmation. Press Go Live and confirm.", 403)
        if g["expires"] < now:
            _grants.pop(gid, None)
            raise BroadcastError("grant_expired", "The confirmation expired. Press Go Live and confirm again.", 410)
        if g["user"] != user:
            raise BroadcastError("grant_foreign", "This authorization belongs to another session.", 403)
        if g["purpose"] != purpose:
            raise BroadcastError("grant_purpose", "This authorization is for a different action.", 403)
        if (g["source_id"], g["rev"]) != ((source or {}).get("id"), source_rev):
            _grants.pop(gid, None)
            raise BroadcastError("source_changed", "The selected source changed since you confirmed. Confirm again.", 409)
        _grants.pop(gid, None)
        _used[gid] = now
        return Grant(_ISSUER, gid, purpose, user, (source or {}).get("id"), now)


def outstanding_grants() -> int:
    with _lock:
        _prune(time.time())
        return len(_grants)


def revoke_all() -> None:
    with _lock:
        _grants.clear()


# ------------------------------------------------------------------ session lifecycle

def begin_session(grant: Grant, source: dict | None, now: float | None = None) -> dict:
    now = now or time.time()
    sess = {"id": grant.id, "user": grant.user, "purpose": grant.purpose, "source_id": grant.source_id,
            "source_name": (source or {}).get("name"), "state": "starting", "requested_at": now, "started_at": None,
            "boot": boot_id(), "stop_requested": False}
    _store(SESSION_KEY, sess)
    _store(ERROR_KEY, None)                          # a fresh, deliberate start supersedes the previous failure record
    return sess


def mark_live(now: float | None = None) -> None:
    s = get_session()
    if s and s.get("state") == "starting":
        s["state"], s["started_at"] = "live", now or time.time()
        _store(SESSION_KEY, s)


def mark_stop_requested(now: float | None = None) -> None:
    s = get_session()
    if s and s.get("state") in ("starting", "live"):
        s["state"], s["stop_requested"], s["stop_at"] = "stopping", True, now or time.time()
        _store(SESSION_KEY, s)
    revoke_all()


def fail_session(reason: str, diagnostics: dict | None = None, now: float | None = None) -> None:
    """The encoder ended without a stop request (or never came up): keep what we know, show ERROR, require a new grant."""
    s = get_session() or {}
    now = now or time.time()
    _store(ERROR_KEY, {"at": now, "reason": reason, "session_id": s.get("id"), "user": s.get("user"),
                       "source_name": s.get("source_name"), "started_at": s.get("started_at"),
                       "ran_seconds": int(now - s["started_at"]) if s.get("started_at") else 0,
                       "diagnostics": diagnostics or {}})
    _store(SESSION_KEY, None)


def end_session() -> None:
    _store(SESSION_KEY, None)
    revoke_all()


# ------------------------------------------------------------------ state

def snapshot(show: dict, producer_ready: bool | None = None, now: float | None = None) -> dict:
    """Derives the broadcast state from ONE unit_show() snapshot plus the session record. Reconciles as a side effect:
    a session whose encoder vanished without a stop request becomes ERROR (kept), a stop that completed closes it."""
    now = now or time.time()
    phase = show.get("phase")
    sess = get_session()
    err = last_error()
    state, adopted = None, False

    if sess:
        st = sess.get("state")
        if sess.get("boot") not in (None, boot_id()):                  # a session never survives a reboot
            fail_session("The server rebooted; the broadcast was not resumed.", {"phase": phase}, now)
            sess, err = None, last_error()
        elif st in ("starting", "live"):
            if phase == "LIVE":
                if st == "starting":
                    mark_live(now)
                state = LIVE
            elif phase in ("STARTING", "RECONNECTING"):
                state = STARTING
            elif st == "starting" and now - sess.get("requested_at", 0) < STARTING_GRACE_SECONDS:
                state = STARTING
            else:
                diag = {"phase": phase, "active_state": show.get("active_state"), "result": show.get("result"),
                        "exit_status": show.get("exec_main_status"), "exit_code": show.get("exec_main_code")}
                fail_session("The encoder stopped without a stop request." if st == "live" else "The encoder did not start.", diag, now)
                sess, err, state = None, last_error(), ERROR
        elif st == "stopping":
            if phase in ("LIVE", "STARTING"):
                state = STOPPING
            else:
                end_session()
                sess = None

    if state is None and sess is None:
        if phase == "LIVE":
            # Running with no session record: started before this protection existed. Adopt it so the operator can
            # still see and stop it; it is never "authorized" for a restart.
            state, adopted = LIVE, True
        elif phase in ("STARTING", "RECONNECTING"):
            state = STARTING
        elif err:
            state = ERROR
        else:
            state = READY if producer_ready else OFFLINE

    return {"state": state, "session": sess, "adopted": adopted, "last_error": err,
            "encoder_phase": phase, "grants_outstanding": outstanding_grants(), "checked_at": now,
            "needs_manual_golive": state in (ERROR, OFFLINE, READY)}
