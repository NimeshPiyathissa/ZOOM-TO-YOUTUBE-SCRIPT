"""EduMix / MJPhysics live source (https://mjphysics.lk/).

What this module is - and is not:

* The operator logs in on the REAL site, typing their phone number and OTP
  themselves into the interactive preview (noVNC over /vnc/ws). Nothing in
  this file ever sees, stores or logs a phone number, an OTP, a password or
  a cookie. The session lives only in the isolated Chrome profile
  (config.EDUMIX_PROFILE_ID) owned by zoombot, mode 700.
* It never bypasses OTP, CAPTCHA, DRM/EME, HDCP or any access control. A
  protected player is reported as "protected" and left alone.
* It does not assume which player the site uses. The page probe below only
  reads generic, non-sensitive facts (route, whether a login form is
  present, every <video> element's playback state, which hosts the iframes
  point at). A concrete player adapter can be added later behind
  `classify()` without touching the GUI or the auth flow.
* Media capture / YouTube retransmission stay disabled
  (config.EDUMIX_CAPTURE_ENABLED) until a real live class has been
  inspected - see capture_status().

Pure logic (URL validation, classify, backoff, reconnect policy, redaction)
is kept free of I/O so it is unit-testable; only status()/navigate_*() talk
to Chrome over app/cdp.py.
"""
from __future__ import annotations

import json
import random
import re
import time
from urllib.parse import urlsplit, urlunsplit

from . import config


class EduMixError(Exception):
    pass


# ------------------------------------------------------------------ URLs

_CONTROL_RE = re.compile(r"[\x00-\x20\x7f]")


def _host_ok(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    base = config.EDUMIX_HOST
    return host == base or host == "www." + base or host.endswith("." + base)


def validate_class_url(url: str) -> str:
    """Returns the normalized https URL (fragment dropped) or raises
    EduMixError. Only the real site is accepted: https, host exactly
    mjphysics.lk (or a subdomain), no userinfo, default port, no control
    characters or backslashes (host-confusion tricks)."""
    raw = (url or "").strip()
    if not raw:
        raise EduMixError("Paste an MJPhysics class URL")
    if len(raw) > 2048:
        raise EduMixError("URL is too long")
    if _CONTROL_RE.search(raw) or "\\" in raw:
        raise EduMixError("URL must not contain spaces, control characters or backslashes")
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise EduMixError("That is not a valid URL")
    if (parts.scheme or "").lower() != "https":
        raise EduMixError("Only https:// links are accepted")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise EduMixError("URLs with embedded credentials are not accepted")
    if not parts.hostname or not _host_ok(parts.hostname):
        raise EduMixError(f"Only {config.EDUMIX_HOST} links are accepted")
    if port not in (None, 443):
        raise EduMixError("Custom ports are not accepted")
    path = parts.path or "/"
    return urlunsplit(("https", parts.hostname.lower(), path, parts.query, ""))


def login_url() -> str:
    return f"https://{config.EDUMIX_HOST}/"


# ------------------------------------------------------------------ page probe

# Runs inside the kiosk page via CDP Runtime.evaluate. Returns only
# non-sensitive facts: it never reads input values, cookies or storage.
JS_PROBE = r"""(() => {
  const q = (s) => document.querySelector(s);
  const vis = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const text = (document.body && document.body.innerText || "").slice(0, 6000).toLowerCase();
  const videos = Array.from(document.querySelectorAll("video")).slice(0, 4).map((v) => ({
    paused: v.paused, ended: v.ended, readyState: v.readyState, networkState: v.networkState,
    error: v.error ? v.error.code : 0, time: Math.round(v.currentTime || 0),
    w: v.videoWidth || 0, h: v.videoHeight || 0, protected: !!v.mediaKeys,
    muted: v.muted, volume: v.volume,
  }));
  const frames = Array.from(document.querySelectorAll("iframe")).slice(0, 6).map((f) => {
    try { return new URL(f.src, location.href).hostname; } catch (e) { return ""; }
  }).filter(Boolean);
  const tel = Array.from(document.querySelectorAll("input")).filter((i) => vis(i));
  return JSON.stringify({
    url: location.origin + location.pathname,
    title: (document.title || "").slice(0, 120),
    ready: document.readyState,
    online: navigator.onLine,
    login_form: tel.some((i) => /tel|number/.test(i.type) || /phone|mobile/i.test((i.name || "") + (i.placeholder || "") + (i.id || ""))),
    otp_form: tel.some((i) => /otp|code|verif/i.test((i.name || "") + (i.placeholder || "") + (i.id || "") + (i.autocomplete || ""))),
    markers: {
      session_expired: /session expired/.test(text),
      no_live: /no classes found|no upcoming live/.test(text),
      error_text: /something went wrong|failed to load|try again later/.test(text),
      drm_text: /protected content|drm|not supported on this device/.test(text),
    },
    videos: videos, frames: frames,
  });
})()"""


# ------------------------------------------------------------------ state machine

# state -> (level, label, hint). `level` drives the GUI's tone:
# ok / info / warn / bad / idle.
STATES: dict[str, tuple[str, str, str]] = {
    "stopped":         ("idle", "Not running", "The EduMix browser isn't running. Open a class to start it."),
    "browser_down":    ("bad", "Browser unreachable", "Chrome isn't answering. It is restarted automatically; use Reconnect if it stays down."),
    "network_down":    ("bad", "Network offline", "The browser reports no network. Reconnecting with backoff."),
    "loading":         ("info", "Loading", "The page is still loading."),
    "login_required":  ("warn", "Sign-in needed", "Log in with your phone number in the live view below."),
    "awaiting_otp":    ("warn", "Enter the OTP", "Type the code you received into the live view. It is never stored."),
    "authenticated":   ("ok", "Signed in", "Session is valid."),
    "class_open":      ("ok", "Class page open", "Signed in. Waiting for a player on this page."),
    "no_live":         ("info", "No live class", "The site lists no live class right now."),
    "session_expired": ("warn", "Session expired", "The site says this lesson's access window or your session has ended. Re-authenticate, or request more time on the site yourself."),
    "player_loading":  ("info", "Player loading", "A player is present but has not started."),
    "buffering":       ("info", "Buffering", "The player is waiting for data."),
    "playing":         ("ok", "Playing", "The player is playing."),
    "paused":          ("info", "Paused", "The player is paused."),
    "ended":           ("info", "Ended", "The class has ended."),
    "protected":       ("bad", "Protected playback", "This player uses protected playback. Capture is not available and is not attempted."),
    "player_error":    ("bad", "Player error", "The player reported an error."),
    "error":           ("bad", "Page error", "The page reported a problem."),
}

# Which states a person must resolve - the supervisor never acts on these.
NEEDS_HUMAN = {"login_required", "awaiting_otp", "session_expired", "protected"}
# Which states the supervisor retries with backoff.
RECOVERABLE = {"browser_down", "network_down", "player_error", "error"}


def classify(snap: dict | None) -> str:
    """Pure: probe snapshot -> state. None means the browser can't be
    reached. Order matters: a hard failure beats a soft one."""
    if snap is None:
        return "browser_down"
    if snap.get("online") is False:
        return "network_down"
    path = urlsplit(snap.get("url") or "").path or "/"
    if snap.get("ready") == "loading":
        return "loading"
    markers = snap.get("markers") or {}
    videos = snap.get("videos") or []

    if snap.get("otp_form"):
        return "awaiting_otp"
    if snap.get("login_form") or path.rstrip("/") in ("/login", "/auth", "/sign-in", "/signin"):
        return "login_required"
    if markers.get("drm_text") or any(v.get("protected") for v in videos):
        return "protected"
    if videos:
        v = videos[0]
        if v.get("error"):
            return "player_error"
        if v.get("ended"):
            return "ended"
        if not v.get("paused") and v.get("readyState", 0) < 3:
            return "buffering"
        if not v.get("paused"):
            return "playing"
        if v.get("readyState", 0) == 0 and v.get("networkState", 0) in (0, 2):
            return "player_loading"
        return "paused"
    if snap.get("frames") and "/class/" in path:
        return "player_loading"
    if markers.get("session_expired"):
        return "session_expired"
    if markers.get("error_text"):
        return "error"
    if markers.get("no_live") or path.rstrip("/") == "/live-class":
        return "no_live"
    if "/class/" in path or "/live" in path:
        return "class_open"
    return "authenticated"


def describe(state: str) -> dict:
    level, label, hint = STATES.get(state, ("info", state, ""))
    return {"state": state, "level": level, "label": label, "hint": hint,
            "needs_human": state in NEEDS_HUMAN}


# ------------------------------------------------------------------ reconnect policy

def backoff_delay(attempt: int, *, base: float | None = None, cap: float | None = None,
                  rand: float | None = None) -> float:
    """Exponential backoff: base * 2**(attempt-1), capped, +-20% jitter.
    `rand` (0..1) is injectable so tests are deterministic."""
    base = config.EDUMIX_BACKOFF_BASE_SECONDS if base is None else base
    cap = config.EDUMIX_BACKOFF_MAX_SECONDS if cap is None else cap
    raw = min(base * (2 ** max(0, attempt - 1)), cap)
    r = random.random() if rand is None else rand
    return round(raw * (0.8 + 0.4 * r), 3)


class Reconnector:
    """Decides - purely from observed states and the clock - when to retry.
    `step(state, now)` returns None (do nothing) or an action:
    "reload" (navigate to the class again), "restart_browser" (browser
    unreachable for a while). It never acts on states a human must fix."""

    def __init__(self, *, restart_after_attempts: int = 3, max_attempts: int = 12):
        self.attempt = 0
        self.next_at = 0.0
        self.restart_after = restart_after_attempts
        self.max_attempts = max_attempts

    def reset(self) -> None:
        self.attempt = 0
        self.next_at = 0.0

    def snapshot(self, now: float) -> dict:
        return {"attempt": self.attempt, "next_in": max(0.0, round(self.next_at - now, 1)) if self.attempt else 0.0,
                "gave_up": self.attempt >= self.max_attempts}

    def step(self, state: str, now: float, *, rand: float | None = None) -> str | None:
        if state not in RECOVERABLE:
            if state in ("playing", "paused", "authenticated", "class_open", "no_live", "buffering", "player_loading"):
                self.reset()
            return None
        if self.attempt >= self.max_attempts:
            return None
        if now < self.next_at:
            return None
        self.attempt += 1
        self.next_at = now + backoff_delay(self.attempt, rand=rand)
        if state == "browser_down" and self.attempt >= self.restart_after:
            return "restart_browser"
        if state == "browser_down":
            return None  # wait for systemd's own Restart= first
        return "reload"


def should_auto_navigate(state: str, current_path: str, target_path: str, auto: bool, done: bool) -> bool:
    """Once login completes the site lands on its own page (e.g. /dashboard);
    take the operator to the class they pasted - once per login episode, and
    only from a signed-in state that isn't already on the target."""
    if not auto or done or not target_path or target_path == "/":
        return False
    if state not in ("authenticated", "no_live", "class_open"):
        return False
    return (current_path or "/").rstrip("/") != target_path.rstrip("/")


# ------------------------------------------------------------------ capture gate

def capture_status() -> dict:
    """Capture/retransmission stays off until a live class has been
    inspected and an authorized method confirmed."""
    if config.EDUMIX_CAPTURE_ENABLED:
        return {"enabled": True, "reason": ""}
    return {
        "enabled": False,
        "reason": "Media capture and YouTube broadcasting for EduMix are disabled until a real live class "
                  "has been inspected and an authorized capture method is confirmed.",
    }


def capture_blocked(source_type: str | None) -> bool:
    return source_type == "edumix" and not config.EDUMIX_CAPTURE_ENABLED


# ------------------------------------------------------------------ redaction

_PHONE_RE = re.compile(r"(?<![\w.])\+?\d[\d \-]{7,}\d(?![\w])")
_OTP_RE = re.compile(r"(?i)\b(otp|code|pin|token|password|pwd|session|cookie|authorization)\b(\s*[:=]\s*)(\S+)")


def redact(text: str) -> str:
    """For anything that might be logged near the login flow: masks phone
    numbers and key=value credentials. Defence in depth - the module never
    handles these values in the first place."""
    out = _OTP_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}•••", text or "")
    return _PHONE_RE.sub("•••", out)


# ------------------------------------------------------------------ Chrome (CDP)

async def snapshot() -> dict | None:
    """Probe the kiosk page. None = Chrome/DevTools unreachable."""
    from . import cdp
    try:
        res = await cdp.evaluate(JS_PROBE)
    except cdp.CDPError:
        return None
    try:
        return json.loads(res.get("value") or "null")
    except (TypeError, ValueError):
        return None


async def status(reconnector: Reconnector | None = None) -> dict:
    snap = await snapshot()
    state = classify(snap)
    out = describe(state)
    out.update({
        "checked_at": time.time(),
        "page": {"path": urlsplit((snap or {}).get("url") or "").path, "title": (snap or {}).get("title", "")},
        "player": _player_summary(snap),
        "capture": capture_status(),
    })
    if reconnector is not None:
        out["reconnect"] = reconnector.snapshot(time.time())
    return out


def _player_summary(snap: dict | None) -> dict:
    vids = (snap or {}).get("videos") or []
    if not vids:
        return {"present": False, "frames": (snap or {}).get("frames") or []}
    v = vids[0]
    return {"present": True, "width": v.get("w", 0), "height": v.get("h", 0), "time": v.get("time", 0),
            "muted": v.get("muted"), "volume": v.get("volume"), "frames": (snap or {}).get("frames") or []}


async def navigate_class(url: str) -> None:
    from . import cdp
    await cdp.navigate(validate_class_url(url))


async def navigate_login() -> None:
    from . import cdp
    await cdp.navigate(login_url())


async def reload_page() -> None:
    from . import cdp
    await cdp.evaluate("location.reload()")


_FIT_MODES = {"fit": "contain", "fill": "cover", "reset": ""}


async def set_video_fit(mode: str) -> dict:
    """Fit / Fill / Reset on the page's own <video> elements (object-fit),
    preserving aspect ratio. Only top-level <video> elements are reachable;
    a player inside a cross-origin iframe is reported as not applied rather
    than guessed at."""
    from . import cdp
    if mode not in _FIT_MODES:
        raise EduMixError("mode must be fit, fill or reset")
    val = json.dumps(_FIT_MODES[mode])
    js = (f"(() => {{ const v = document.querySelectorAll('video'); v.forEach(e => {{ e.style.objectFit = {val}; "
          f"e.style.background = {val} ? '#000' : ''; }}); return v.length; }})()")
    res = await cdp.evaluate(js)
    n = int(res.get("value") or 0)
    return {"applied": n > 0, "videos": n, "mode": mode,
            "note": "" if n else "No <video> element on the page itself (the player may live in an iframe) - use the preview's own Fit/Fill."}
