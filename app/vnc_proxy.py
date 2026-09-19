"""Auth-gated WebSocket bridge: browser <-> this app <-> websockify
(127.0.0.1:6080) <-> x11vnc (127.0.0.1:5900). Neither 5900 nor 6080 is
ever exposed outside loopback; the only way in is through a logged-in
dashboard session.

Frame-rate policy lives here too: x11vnc is started at a 100ms screen
poll (~10 fps cap, start-vnc.sh) so that an idle viewer costs next to
nothing. While at least one VNC session is open through this proxy -
the interactive preview on /remote, the Remote GUI page, or the
sign-in overlay - it is switched to the 40ms poll (~25 fps) via
scripts/set-vnc-rate.sh, and back to 100ms when the *last* session
closes. Tying it to the connection rather than a client-side API call
means a page that dies mid-session (phone locked, tab killed, network
dropped) can never leave x11vnc running fast."""
from __future__ import annotations

import asyncio
import logging

import websockets
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import config, control, security

WEBSOCKIFY_URL = "ws://127.0.0.1:6080/"
log = logging.getLogger("vnc_proxy")

_sessions = 0
_rate_lock = asyncio.Lock()


async def _apply_rate(mode: str) -> None:
    """Best-effort: a failure here (x11vnc not running yet, sudo hiccup)
    must never break the VNC session itself."""
    async with _rate_lock:
        try:
            result = await run_in_threadpool(control.set_vnc_rate, mode)
            log.info("x11vnc poll rate -> %s (%s)", mode, result)
        except control.ControlError as exc:
            log.warning("could not set x11vnc rate to %s: %s", mode, exc)


async def _session_opened() -> None:
    global _sessions
    _sessions += 1
    if _sessions == 1:
        asyncio.create_task(_apply_rate("fast"))


async def _session_closed() -> None:
    global _sessions
    _sessions = max(0, _sessions - 1)
    if _sessions == 0:
        asyncio.create_task(_apply_rate("slow"))


def active_sessions() -> int:
    return _sessions


async def proxy(websocket: WebSocket) -> None:
    session_id = websocket.cookies.get(config.COOKIE_NAME)
    session = security.load_session(session_id) if session_id else None
    if not session:
        await websocket.close(code=4401)
        return

    await websocket.accept(subprotocol="binary")
    await _session_opened()
    try:
        async with websockets.connect(WEBSOCKIFY_URL, subprotocols=["binary"], max_size=None) as upstream:

            async def client_to_upstream():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg.get("type") == "websocket.disconnect":
                            return
                        data = msg.get("bytes")
                        if data is not None:
                            await upstream.send(data)
                        elif msg.get("text") is not None:
                            await upstream.send(msg["text"])
                except WebSocketDisconnect:
                    return

            async def upstream_to_client():
                async for message in upstream:
                    if isinstance(message, (bytes, bytearray)):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except Exception:
        pass
    finally:
        await _session_closed()
        try:
            await websocket.close()
        except Exception:
            pass
