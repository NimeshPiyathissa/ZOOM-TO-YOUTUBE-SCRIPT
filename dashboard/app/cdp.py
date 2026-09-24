"""Minimal Chrome DevTools Protocol client for controlling the kiosk
Chrome tab that browser-source.sh launches - navigate, and run small JS
snippets to control YouTube's native <video> element directly. No new
dependency: httpx and websockets are already in requirements.txt (used
by scheduler.py's webhook alert and vnc_proxy.py respectively). Chrome's
debug port is bound to 127.0.0.1 by browser-source.sh, same trust
boundary as x11vnc/novnc-proxy - never reachable from outside this box."""
from __future__ import annotations

import asyncio
import json

import httpx
import websockets

from . import config


class CDPError(Exception):
    pass


async def _get_json(path: str) -> object:
    """GET http://127.0.0.1:<port>/json<path>, with one quick retry: Chrome
    re-binds the port within a second or two after a restart, and this is
    also what makes every control call "reconnect automatically" - there
    is no long-lived connection to lose, each call discovers the current
    tab afresh."""
    url = f"http://127.0.0.1:{config.CHROME_DEBUG_PORT}/json{path}"
    last: Exception | None = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError as exc:
            last = exc
            if attempt == 0:
                await asyncio.sleep(0.4)
    raise CDPError(
        "Chrome's DevTools port isn't reachable - is the active source a "
        f"webpage source with browser-source.sh running? ({last})"
    ) from last


async def _get_page_target() -> dict:
    targets = await _get_json("")
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        raise CDPError("No Chrome page target found - is the active source a webpage source?")
    for p in pages:
        url = (p.get("url") or "").lower()
        title = (p.get("title") or "").lower()
        if "zoom.us" in url or "zoom" in title:
            return p
    return pages[0]


async def probe() -> dict:
    """Is the kiosk Chrome reachable right now, and what is it showing?
    Raises CDPError (with the reason) when it isn't; control.py's
    browser_diagnosis() then works out *why* for the panel."""
    version = await _get_json("/version")
    targets = await _get_json("")
    pages = [t for t in targets if t.get("type") == "page"]
    first = pages[0] if pages else {}
    return {
        "connected": True,
        "browser": version.get("Browser", ""),
        "pages": len(pages),
        "title": (first.get("title") or "")[:160],
        "url": (first.get("url") or "")[:300],
    }


async def _call(ws, msg_id: int, method: str, params: dict | None = None) -> dict:
    await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        data = json.loads(raw)
        if data.get("id") == msg_id:
            if "error" in data:
                raise CDPError(data["error"].get("message", "CDP error"))
            return data.get("result", {})
        # Unsolicited event notification (Page.frameNavigated etc.) -
        # not the reply we're waiting for, keep reading.


async def navigate(url: str) -> None:
    target = await _get_page_target()
    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=2**20, open_timeout=5) as ws:
        await _call(ws, 1, "Page.navigate", {"url": url})


async def evaluate(expression: str, user_gesture: bool = False) -> dict:
    """Runs `expression` in the page's top-level JS context and returns
    Runtime.evaluate's `result` object (.value for JSON-serializable
    results). `user_gesture=True` lets the page call APIs that need a
    user activation (requestFullscreen, play() under a strict autoplay
    policy) - the panel button *is* the user's gesture."""
    target = await _get_page_target()
    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=2**20, open_timeout=5) as ws:
        result = await _call(ws, 1, "Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": False,
            "userGesture": bool(user_gesture),
        })
        res = result.get("result", {})
        if res.get("subtype") == "error":
            raise CDPError(f"Page JS error: {res.get('description') or res.get('value')}")
        if "exceptionDetails" in result:
            desc = result["exceptionDetails"].get("exception", {}).get("description") or result["exceptionDetails"].get("text", "JS exception")
            raise CDPError(f"Page JS exception: {desc}")
        return res


# Deliberately operate on the real <video> element YouTube's player
# renders (true on both a full watch page and an /embed/ page), not the
# IFrame Player API - that API only applies when YouTube is embedded
# inside a page we control via <iframe>, but here Chrome navigates its
# top-level frame directly to the YouTube URL itself.
_FIND_VIDEO = "document.querySelector('video')"

# Which Google account a google.com / youtube.com page is showing as
# signed in: the account button's accessible name ("Google Account: Name
# (email)"). Read-only DOM text, masked before it leaves the server
# (accounts.mask_email) - never a cookie or token.
JS_GOOGLE_IDENTITY = (
    "(() => { const a = document.querySelector('a[aria-label^=\"Google Account\"], "
    "button[aria-label^=\"Google Account\"], #avatar-btn'); if (!a) return null; "
    r"const m = /\(([^()\s]+@[^()\s]+)\)/.exec(a.getAttribute('aria-label') || ''); "
    "return m ? m[1] : ''; })()"
)

JS_PLAY = f"(() => {{ const v = {_FIND_VIDEO}; if (!v) return false; v.play(); return true; }})()"
JS_PAUSE = f"(() => {{ const v = {_FIND_VIDEO}; if (!v) return false; v.pause(); return true; }})()"
JS_ENSURE_UNMUTED = (
    f"(() => {{ const v = {_FIND_VIDEO}; if (!v) return false; "
    "v.muted = false; if (!v.volume) v.volume = 1; return true; }})()"
)


def js_set_volume(percent: int) -> str:
    frac = max(0, min(100, int(percent))) / 100
    return (
        f"(() => {{ const v = {_FIND_VIDEO}; if (!v) return false; "
        f"v.volume = {frac}; v.muted = false; return true; }})()"
    )


# ---------------------------------------------------------------- Part 3A: player state / control / diagnosis

# Everything below is read straight off the page. `quality` is the real
# rendered height of the <video> element - YouTube auto-negotiates
# quality and its setPlaybackQuality() API is a no-op on modern players,
# so this is reported, not selectable (see the plan's "can't work as
# specified" notes).
JS_STATE = r"""(() => {
  const v = document.querySelector('video');
  const txt = (sel) => { const el = document.querySelector(sel); return el ? (el.innerText || '').trim() : ''; };
  const errorText = txt('.ytp-error-content-wrap-reason') || txt('.ytp-error') || '';
  // YouTube's in-player "playability" overlay: the bot-check wall
  // ("Sign in to confirm you're not a bot"), sign-in-required and
  // age-gate all render here while a <video> element still exists
  // (readyState 0) - so it must be read explicitly, not inferred from
  // the absence of a video element.
  const playability = txt('yt-playability-error-supported-renderers') || txt('#player-error-message-container') || '';
  const bodyText = (document.body && document.body.innerText || '').slice(0, 4000);
  // Ads and YouTube's inactivity prompt ("Video paused. Continue
  // watching?") are read explicitly too - both stall a 24/7 stream.
  const player = document.querySelector('#movie_player, .html5-video-player');
  const adShowing = !!(player && (player.classList.contains('ad-showing') || player.classList.contains('ad-interrupting')));
  const skipBtn = document.querySelector('.ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern, button[class*="ytp-ad-skip"]');
  const promptEl = Array.from(document.querySelectorAll('yt-confirm-dialog-renderer, tp-yt-paper-dialog, .ytp-popup'))
    .find((el) => /continue watching|video paused/i.test(el.innerText || ''));
  const ccBtn = document.querySelector('.ytp-subtitles-button');
  const liveBadge = document.querySelector('.ytp-live-badge, .ytp-live');
  const out = {
    has_video: !!v, url: location.href, title: document.title,
    error_text: errorText.slice(0, 300),
    playability_text: playability.slice(0, 300),
    fullscreen: !!document.fullscreenElement,
    ad_showing: adShowing,
    skippable: !!(skipBtn && skipBtn.offsetParent !== null),
    continue_prompt: !!promptEl,
    captions: !!(ccBtn && ccBtn.getAttribute('aria-pressed') === 'true'),
    live: !!(liveBadge && liveBadge.offsetParent !== null && !/^\s*$/.test(liveBadge.innerText || 'live')),
    body_hint: /not a bot/i.test(playability) ? 'bot'
             : /sign in to confirm your age/i.test(playability + ' ' + bodyText) ? 'age'
             : /sign in/i.test(playability) ? 'signin'
             : /sign in/i.test(errorText + ' ' + bodyText.slice(0, 600)) && !v ? 'signin'
             : /video unavailable|an error occurred|playback error|this video is private/i.test(errorText + ' ' + bodyText.slice(0, 600)) ? 'error' : '',
  };
  if (v) Object.assign(out, {
    paused: v.paused, ended: v.ended, muted: v.muted, volume: Math.round(v.volume * 100),
    current_time: v.currentTime || 0, duration: isFinite(v.duration) ? v.duration : null,
    quality: v.videoHeight ? v.videoHeight + 'p' : null, ready_state: v.readyState,
    speed: v.playbackRate || 1,
  });
  return out;
})()"""

JS_MUTE = f"(() => {{ const v = {_FIND_VIDEO}; if (!v) return false; v.muted = true; return true; }})()"
JS_UNMUTE = f"(() => {{ const v = {_FIND_VIDEO}; if (!v) return false; v.muted = false; return true; }})()"
# YouTube's watch-page hotkey for theater mode; embed pages already fill
# the kiosk viewport, so there it's a no-op (reported as such).
JS_THEATER = """(() => {
  if (location.pathname.startsWith('/embed/')) return 'embed-fills-viewport';
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 't', code: 'KeyT', keyCode: 84, which: 84, bubbles: true }));
  return 'toggled';
})()"""


# Player fullscreen (needs evaluate(..., user_gesture=True)). Prefers
# YouTube's own fullscreen button so the player UI follows; falls back
# to the Fullscreen API on the player element. The kiosk window is
# already fullscreen, so this only matters on a watch page where the
# player is boxed - on /embed/ it already fills the viewport.
JS_FULLSCREEN = """(() => {
  if (document.fullscreenElement) { document.exitFullscreen(); return 'exited'; }
  const btn = document.querySelector('.ytp-fullscreen-button');
  if (btn) { btn.click(); return 'clicked-yt-button'; }
  const v = document.querySelector('video'); if (!v) return 'no-video';
  const el = v.closest('#movie_player') || v;
  if (el.requestFullscreen) { el.requestFullscreen().catch(() => {}); return 'requested'; }
  return 'unsupported';
})()"""


# Skip a skippable ad via YouTube's own button (never a synthetic
# seek); dismiss the inactivity prompt via its own confirm button.
JS_SKIP_AD = """(() => {
  const b = document.querySelector('.ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern, button[class*="ytp-ad-skip"]');
  if (!b || b.offsetParent === null) return false; b.click(); return true;
})()"""
JS_DISMISS_PROMPT = """(() => {
  const dlg = Array.from(document.querySelectorAll('yt-confirm-dialog-renderer, tp-yt-paper-dialog, .ytp-popup'))
    .find((el) => /continue watching|video paused/i.test(el.innerText || ''));
  if (!dlg) return false;
  const btn = dlg.querySelector('#confirm-button, button, yt-button-renderer');
  if (btn) { btn.click(); }
  const v = document.querySelector('video'); if (v && v.paused) v.play();
  return true;
})()"""
JS_TOGGLE_CAPTIONS = """(() => {
  const b = document.querySelector('.ytp-subtitles-button');
  if (!b) return 'no-captions';
  b.click(); return b.getAttribute('aria-pressed') === 'true' ? 'on' : 'off';
})()"""
JS_REPLAY = f"(() => {{ const v = {_FIND_VIDEO}; if (!v) return false; v.currentTime = 0; v.play(); return true; }})()"


def js_apply_options(loop: bool, captions: bool, speed: float) -> str:
    """After a watch page has its <video>: loop flag, captions to the
    wanted state (YouTube's own button, only when it differs), speed.
    Returns what was applied."""
    r = max(0.25, min(2.0, float(speed)))
    want_loop = "true" if loop else "false"
    want_cc = "true" if captions else "false"
    return (
        f"(() => {{ const v = {_FIND_VIDEO}; if (!v) return null; "
        f"v.loop = {want_loop}; v.playbackRate = {r}; v.muted = false; if (!v.volume) v.volume = 1; "
        "const cc = document.querySelector('.ytp-subtitles-button'); let ccState = 'none'; "
        f"if (cc) {{ const on = cc.getAttribute('aria-pressed') === 'true'; if (on !== {want_cc}) cc.click(); ccState = cc.getAttribute('aria-pressed'); }} "
        "return { loop: v.loop, speed: v.playbackRate, captions: ccState }; })()"
    )


def js_set_speed(rate: float) -> str:
    r = max(0.25, min(2.0, float(rate)))
    return f"(() => {{ const v = {_FIND_VIDEO}; if (!v) return false; v.playbackRate = {r}; return v.playbackRate; }})()"


def js_seek(seconds: float) -> str:
    s = max(0.0, float(seconds))
    return f"(() => {{ const v = {_FIND_VIDEO}; if (!v) return false; v.currentTime = {s}; return true; }})()"


def diagnose(state: dict) -> dict | None:
    """Turns raw page state into one of the spec's three diagnoses, each
    with what to do about it. None when playback looks fine."""
    if not state:
        return None
    hint = state.get("body_hint") or ""
    err = (state.get("error_text") or "").lower()
    if hint == "bot":
        return {"kind": "bot_check",
                "message": "YouTube is showing \"Sign in to confirm you're not a bot\" - it won't play from this datacenter IP without a signed-in session.",
                "fix": "Turn on Interact on the preview (or open the remote desktop), tap Sign in and sign into a Google account in this browser profile once - Chrome remembers it. Or bind this source to an already signed-in account on the Accounts page."}
    if hint == "age" or "confirm your age" in err:
        return {"kind": "age_restricted",
                "message": "YouTube wants a signed-in, age-verified account for this video.",
                "fix": "Bind this source to a signed-in account (Accounts page), then switch to it again."}
    if hint == "signin" or ("sign in" in err and not state.get("has_video")):
        return {"kind": "sign_in_required",
                "message": "YouTube is asking for a sign-in before it will play this.",
                "fix": "Sign in to the bound account on the Accounts page (Re-authenticate if it expired), then switch to this source again."}
    if hint == "error" or (err and not state.get("has_video")) or (state.get("has_video") and state.get("ready_state", 4) == 0 and err):
        return {"kind": "playback_error",
                "message": (state.get("error_text") or "The player reported an error.")[:200],
                "fix": "Check the link still works in a normal browser; if it does, try the source again - transient errors usually clear on reload."}
    return None
