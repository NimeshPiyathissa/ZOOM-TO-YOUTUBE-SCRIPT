"""Auth-gated WebSocket bridge: browser <-> this app <-> websockify
(127.0.0.1:6080) <-> x11vnc (127.0.0.1:5900). Neither 5900 nor 6080 is
ever exposed outside loopback; the only way in is through a logged-in
dashboard session.

VNC authentication is handled *here*, server-side, not by the browser
(see app/vncauth.py). Access is already gated by an authenticated
dashboard session over a loopback-only socket, so making every viewer
also type x11vnc's separate VNC password was pure friction - and it
handed the display password to the client. Incident 2026-09-19: an
admin who didn't have that password found the whole interactive preview
dead, because noVNC's password modal covered the panel and blocked every
tap. Now the proxy answers x11vnc's challenge itself and presents the
browser the "None" security type, so no password ever reaches (or is
needed by) the client. The RFB handshake is intercepted only through
SecurityResult; ClientInit onward is bridged untouched.

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

from . import config, control, security, vncauth

WEBSOCKIFY_URL = "ws://127.0.0.1:6080/"
log = logging.getLogger("vnc_proxy")

RFB_VERSION = b"RFB 003.008\n"
SEC_NONE = 1
SEC_VNC_AUTH = 2


class _WSByteReader:
    """Reads an exact number of bytes off a websocket, buffering across
    frame boundaries - the RFB handshake needs precise byte counts, but a
    WebSocket delivers arbitrarily-chunked messages."""

    def __init__(self, recv):
        self._recv = recv
        self._buf = bytearray()

    async def read(self, n: int, timeout: float = 10) -> bytes:
        while len(self._buf) < n:
            msg = await asyncio.wait_for(self._recv(), timeout)
            self._buf.extend(msg if isinstance(msg, (bytes, bytearray)) else msg.encode())
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    @property
    def leftover(self) -> bytes:
        out = bytes(self._buf)
        self._buf.clear()
        return out


class AuthError(Exception):
    pass


async def _authenticate_upstream(upstream) -> None:
    """Complete RFB 3.8 version + VNC-auth negotiation with x11vnc (via
    websockify) using the server-held password, leaving the connection
    exactly at the ClientInit boundary."""
    reader = _WSByteReader(upstream.recv)
    server_ver = await reader.read(12)
    if not server_ver.startswith(b"RFB 003."):
        raise AuthError(f"unexpected RFB version from x11vnc: {server_ver!r}")
    await upstream.send(RFB_VERSION)
    ntypes = (await reader.read(1))[0]
    if ntypes == 0:
        reason_len = int.from_bytes(await reader.read(4), "big")
        raise AuthError("x11vnc refused connection: " + (await reader.read(reason_len)).decode(errors="replace"))
    types = await reader.read(ntypes)
    if SEC_VNC_AUTH in types:
        await upstream.send(bytes([SEC_VNC_AUTH]))
        challenge = await reader.read(16)
        password = vncauth.current_password()
        if not password:
            raise AuthError("x11vnc wants a VNC password but none is configured (.env VNC_PASSWORD)")
        await upstream.send(vncauth.challenge_response(challenge, password))
        result = int.from_bytes(await reader.read(4), "big")
        if result != 0:
            raise AuthError("x11vnc rejected the stored VNC password (rotate it on the Configuration page)")
    elif SEC_NONE in types:
        await upstream.send(bytes([SEC_NONE]))
        # RFB 3.8 sends a SecurityResult even for None.
        int.from_bytes(await reader.read(4), "big")
    else:
        raise AuthError(f"x11vnc offered no security type we support: {list(types)}")
    if reader.leftover:
        raise AuthError("x11vnc sent data past SecurityResult before ClientInit")


async def _authenticate_downstream(websocket: WebSocket, reader: "_WSByteReader") -> None:
    """Present the (already dashboard-authenticated) browser the RFB 3.8
    handshake with only the None security type, leaving it at ClientInit."""
    await websocket.send_bytes(RFB_VERSION)
    await reader.read(12)                       # client version - we require 3.x either way
    await websocket.send_bytes(bytes([1, SEC_NONE]))
    chosen = (await reader.read(1))[0]
    if chosen != SEC_NONE:
        raise AuthError(f"client selected security type {chosen}, only None is offered")
    await websocket.send_bytes((0).to_bytes(4, "big"))   # SecurityResult OK

_sessions = 0

# Poll-rate policy is CLIENT-driven (interact.js POSTs /api/vnc/rate fast
# when Interact connects, slow when it turns off), because that is
# deterministic - it maps exactly to "the operator is interacting". The
# proxy keeps only a safety net: when the last VNC session closes it
# forces slow, so a client that vanished without saying slow (phone
# killed, network dropped) can't leave x11vnc polling fast forever. The
# proxy never sets *fast* itself; that avoids the fast/slow race on
# x11vnc's single control property that made the screen stick at the slow
# 10fps rate mid-session (the "remote screen is laggy" bug)."""


async def _session_opened() -> None:
    global _sessions
    _sessions += 1


async def _session_closed() -> None:
    global _sessions
    _sessions = max(0, _sessions - 1)
    if _sessions == 0:
        try:
            await run_in_threadpool(control.set_vnc_rate, "slow")
        except control.ControlError as exc:
            log.warning("could not restore slow x11vnc rate on last close: %s", exc)


def active_sessions() -> int:
    return _sessions


async def proxy(websocket: WebSocket) -> None:
    session_id = websocket.cookies.get(config.COOKIE_NAME)
    session = security.load_session(session_id) if session_id else None
    if not session:
        await websocket.close(code=4401)
        return

    # Only echo a subprotocol the client actually offered: a server that
    # names a subprotocol the client never requested makes the browser
    # abort the WebSocket handshake with code 1006 (the "VNC disconnected
    # unexpectedly" bug). noVNC is asked to request "binary"
    # (vnc-embed.js); accept that when present, otherwise accept with no
    # subprotocol rather than forcing one.
    offered = websocket.scope.get("subprotocols") or []
    subprotocol = "binary" if "binary" in offered else (offered[0] if offered else None)
    await websocket.accept(subprotocol=subprotocol)
    await _session_opened()
    try:
        async with websockets.connect(WEBSOCKIFY_URL, subprotocols=["binary"], max_size=None) as upstream:

            # A recv() over the starlette websocket that yields bytes and
            # raises WebSocketDisconnect at close - used by the handshake
            # byte-reader below.
            async def client_recv():
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    raise WebSocketDisconnect()
                data = msg.get("bytes")
                if data is not None:
                    return data
                text = msg.get("text")
                return text.encode() if text is not None else b""

            # RFB auth translation: answer x11vnc ourselves, show the
            # browser only "None". After this both sides sit at ClientInit.
            try:
                await _authenticate_upstream(upstream)
                client_reader = _WSByteReader(client_recv)
                await _authenticate_downstream(websocket, client_reader)
            except (AuthError, asyncio.TimeoutError) as exc:
                log.warning("VNC auth translation failed: %s", exc)
                await websocket.close(code=1011)
                return
            # Any browser bytes already buffered past the handshake
            # (ClientInit and beyond) go upstream before the pump starts.
            pending = client_reader.leftover
            if pending:
                await upstream.send(pending)

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
        # Never silent: a broken bridge used to look identical to a user
        # closing the page (incident 2026-09-19, "VNC disconnected
        # unexpectedly" with nothing in any log).
        log.exception("vnc proxy session ended with an error")
    finally:
        await _session_closed()
        try:
            await websocket.close()
        except Exception:
            pass
