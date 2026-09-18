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
