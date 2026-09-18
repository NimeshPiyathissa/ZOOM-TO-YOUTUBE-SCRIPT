"""Auth-gated WebSocket bridge: browser <-> this app <-> websockify
(127.0.0.1:6080) <-> x11vnc (127.0.0.1:5900). Neither 5900 nor 6080 is
ever exposed outside loopback; the only way in is through a logged-in
dashboard session."""
from __future__ import annotations

import asyncio

import websockets
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import config, security

WEBSOCKIFY_URL = "ws://127.0.0.1:6080/"


async def proxy(websocket: WebSocket) -> None:
    session_id = websocket.cookies.get(config.COOKIE_NAME)
    session = security.load_session(session_id) if session_id else None
    if not session:
        await websocket.close(code=4401)
        return

    await websocket.accept(subprotocol="binary")
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
        try:
            await websocket.close()
        except Exception:
            pass
