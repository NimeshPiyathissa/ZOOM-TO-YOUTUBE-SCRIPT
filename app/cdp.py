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


async def _get_page_target() -> dict:
    url = f"http://127.0.0.1:{config.CHROME_DEBUG_PORT}/json"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            targets = resp.json()
    except httpx.HTTPError as exc:
        raise CDPError(
            "Chrome's DevTools port isn't reachable - is the active source a "
            f"webpage source with browser-source.sh running? ({exc})"
        ) from exc
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        raise CDPError("No Chrome page target found - is the active source a webpage source?")
    return pages[0]


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


async def evaluate(expression: str) -> dict:
    """Runs `expression` in the page's top-level JS context and returns
    Runtime.evaluate's `result` object (.value for JSON-serializable
    results)."""
    target = await _get_page_target()
    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=2**20, open_timeout=5) as ws:
        result = await _call(ws, 1, "Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": False,
        })
        return result.get("result", {})


# Deliberately operate on the real <video> element YouTube's player
# renders (true on both a full watch page and an /embed/ page), not the
# IFrame Player API - that API only applies when YouTube is embedded
# inside a page we control via <iframe>, but here Chrome navigates its
# top-level frame directly to the YouTube URL itself.
_FIND_VIDEO = "document.querySelector('video')"

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
JS_STATE = """(() => {
  const v = document.querySelector('video');
  const txt = (sel) => { const el = document.querySelector(sel); return el ? (el.innerText || '').trim() : ''; };
  const errorText = txt('.ytp-error-content-wrap-reason') || txt('.ytp-error') || '';
  const bodyText = (document.body && document.body.innerText || '').slice(0, 4000);
  const out = {
    has_video: !!v, url: location.href, title: document.title,
    error_text: errorText.slice(0, 300),
    body_hint: /sign in to confirm your age/i.test(bodyText) ? 'age'
             : /sign in/i.test(errorText + ' ' + bodyText.slice(0, 600)) && !v ? 'signin'
             : /video unavailable|an error occurred|playback error|this video is private/i.test(errorText + ' ' + bodyText.slice(0, 600)) ? 'error' : '',
  };
  if (v) Object.assign(out, {
    paused: v.paused, ended: v.ended, muted: v.muted, volume: Math.round(v.volume * 100),
    current_time: v.currentTime || 0, duration: isFinite(v.duration) ? v.duration : null,
    quality: v.videoHeight ? v.videoHeight + 'p' : null, ready_state: v.readyState,
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
