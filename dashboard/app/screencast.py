"""Event-driven preview of the EduMix player (DevTools `Page.startScreencast`), shared by every open dashboard tab.

Why this replaces screenshot polling: Chrome pushes a JPEG only when the page actually repaints, we acknowledge each
frame, and ONE capture serves any number of viewers. With no viewer connected (the dashboard tab is hidden or closed)
the screencast is stopped; the source page keeps playing untouched, because the preview is only an observer.

The preview is cosmetic and read-only. It is not a capture path for broadcasting: it is low-resolution, lossy and
rate-limited, EDUMIX_CAPTURE_ENABLED stays False, and a DRM-protected player simply renders blank in it - nothing
here tries to defeat that.

Player-only: `edumix.set_player_only()` makes the page's own player element fill Chrome's viewport (reversible
CSS), so the screencast frame IS the player - no cropping, no sidebars.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import deque

from . import cdp, config, edumix

log = logging.getLogger("zoomdash.edumix.screencast")

MAX_VIEWERS = 4
IDLE_STOP_SECONDS = 3.0          # keep the capture briefly after the last viewer leaves (tab switches, reloads)
MIN_FPS, MAX_FPS = 1, 30
MIN_Q, MAX_Q = 30, 85
MIN_W, MAX_W = 480, 1920
SOURCE_FPS = 30                  # compositor rate measured for a 30 fps player; everyNthFrame = SOURCE_FPS / wanted fps


def clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


class Viewer:
    """One connected dashboard stream. Holds at most ONE pending frame: a slow client drops frames (counted) instead
    of building a queue, so memory stays bounded however slow the network is."""

    def __init__(self, fps: int, quality: int, width: int):
        self.fps, self.quality, self.width = fps, quality, width
        self.event = asyncio.Event()
        self.frame: bytes | None = None
        self.last_sent = 0.0
        self.dropped = 0
        self.sent = 0

    def offer(self, jpg: bytes, now: float) -> None:
        if now - self.last_sent < 0.8 / self.fps:                # per-viewer frame-rate cap (20% slack: Chrome's frame arrival jitters)
            return
        if self.frame is not None:
            self.dropped += 1                                    # previous frame was never picked up
        self.frame = jpg
        self.last_sent = now
        self.event.set()

    async def next(self, timeout: float) -> bytes | None:
        try:
            await asyncio.wait_for(self.event.wait(), timeout)
        except asyncio.TimeoutError:
            return None
        self.event.clear()
        jpg, self.frame = self.frame, None
        if jpg is not None:
            self.sent += 1
        return jpg


class Hub:
    def __init__(self, connect=None):
        self._connect = connect                      # injectable for tests: async (url) -> websocket-like
        self.viewers: set[Viewer] = set()
        self.task: asyncio.Task | None = None
        self.player_only = False
        self.last_frame: bytes | None = None
        self.geometry = {"w": 0, "h": 0}
        self.stats = {"frames": 0, "bytes": 0, "reconnects": 0, "last_frame_at": None, "last_error": "",
                      "player_only_applied": None}
        self._window: deque = deque(maxlen=120)      # (t, bytes) of recent frames for fps / kbps
        self._msg_id = 100
        self._stop_at: float | None = None

    # ------------------------------------------------------------ viewers
    def add_viewer(self, fps=12, quality=55, width=1280) -> Viewer:
        if len(self.viewers) >= MAX_VIEWERS:
            raise RuntimeError("too many preview viewers")
        v = Viewer(clamp(fps, MIN_FPS, MAX_FPS, 12), clamp(quality, MIN_Q, MAX_Q, 55), clamp(width, MIN_W, MAX_W, 1280))
        self.viewers.add(v)
        self._stop_at = None
        if self.last_frame is not None:               # a static page repaints nothing: show the last frame at once
            v.offer(self.last_frame, time.monotonic() + 1.0)
        self._ensure_task()
        return v

    def remove_viewer(self, v: Viewer) -> None:
        self.viewers.discard(v)
        if not self.viewers:
            self._stop_at = time.monotonic() + IDLE_STOP_SECONDS

    def _ensure_task(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.ensure_future(self._run())

    # ------------------------------------------------------------ settings the capture runs with
    def _params(self) -> dict:
        fps = max((v.fps for v in self.viewers), default=MIN_FPS)
        q = max((v.quality for v in self.viewers), default=MIN_Q)
        w = max((v.width for v in self.viewers), default=MIN_W)
        return {"format": "jpeg", "quality": q, "maxWidth": w, "maxHeight": int(w * 9 / 16) + 1,
                "everyNthFrame": max(1, min(15, round(SOURCE_FPS / fps)))}      # ask Chrome for ~fps frames, not more

    # ------------------------------------------------------------ the capture loop
    async def _run(self) -> None:
        attempt = 0
        while True:
            if not self.viewers:
                if self._stop_at is None:
                    self._stop_at = time.monotonic() + IDLE_STOP_SECONDS
                if time.monotonic() >= self._stop_at:
                    return                             # nobody is watching: no capture, no CPU
                await asyncio.sleep(0.25)
                continue
            try:
                await self._session()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:                  # bounded reconnect with backoff, never a hot loop
                attempt += 1
                self.stats["reconnects"] += 1
                self.stats["last_error"] = edumix.redact(f"{type(exc).__name__}: {exc}")[:160]
                delay = edumix.backoff_delay(attempt, base=1.0, cap=10.0)
                log.warning("screencast session ended (%s); retry in %.1fs", self.stats["last_error"], delay)
                await asyncio.sleep(delay)

    async def _open(self):
        target = await cdp._get_page_target()
        if self._connect is not None:
            return await self._connect(target["webSocketDebuggerUrl"])
        import websockets
        return await websockets.connect(target["webSocketDebuggerUrl"], max_size=16 * 2 ** 20, open_timeout=5)

    async def _send(self, ws, method: str, params: dict | None = None) -> None:
        self._msg_id += 1
        await ws.send(json.dumps({"id": self._msg_id, "method": method, "params": params or {}}))

    async def _session(self) -> None:
        ws = await self._open()
        try:
            await self._send(ws, "Page.enable")
            await self._send(ws, "Page.startScreencast", self._params())
            applied_params = self._params()
            if self.player_only:
                asyncio.ensure_future(self._apply_player_only())
            while self.viewers or (self._stop_at and time.monotonic() < self._stop_at):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    if self._params() != applied_params and self.viewers:      # a viewer asked for different quality
                        await self._send(ws, "Page.stopScreencast")
                        applied_params = self._params()
                        await self._send(ws, "Page.startScreencast", applied_params)
                    continue
                msg = json.loads(raw)
                method = msg.get("method")
                if method == "Page.screencastFrame":
                    p = msg["params"]
                    await self._send(ws, "Page.screencastFrameAck", {"sessionId": p["sessionId"]})   # keeps frames coming
                    self._publish(base64.b64decode(p["data"]), p.get("metadata") or {})
                elif method == "Page.frameNavigated" and not (msg.get("params", {}).get("frame", {}).get("parentId")):
                    if self.player_only:                  # a full navigation drops our injected CSS: re-apply
                        asyncio.get_event_loop().call_later(1.5, lambda: asyncio.ensure_future(self._apply_player_only()))
            try:
                await self._send(ws, "Page.stopScreencast")
            except Exception:
                pass
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    def _publish(self, jpg: bytes, meta: dict) -> None:
        now = time.monotonic()
        self.last_frame = jpg
        self.stats["frames"] += 1
        self.stats["bytes"] += len(jpg)
        self.stats["last_frame_at"] = time.time()
        self._window.append((now, len(jpg)))
        if meta.get("deviceWidth"):
            self.geometry = {"w": int(meta["deviceWidth"]), "h": int(meta.get("deviceHeight") or 0)}
        for v in list(self.viewers):
            v.offer(jpg, now)

    async def _apply_player_only(self) -> None:
        try:
            res = await edumix.set_player_only(True)
            self.stats["player_only_applied"] = bool(res.get("applied"))
        except Exception as exc:
            self.stats["player_only_applied"] = False
            log.debug("player-only not applied: %s", edumix.redact(str(exc))[:120])

    # ------------------------------------------------------------ reporting
    def snapshot(self) -> dict:
        now = time.monotonic()
        recent = [(t, b) for t, b in self._window if now - t <= 5.0]
        span = (recent[-1][0] - recent[0][0]) if len(recent) > 1 else 0
        fps = round((len(recent) - 1) / span, 1) if span > 0 else 0.0
        kbps = round(sum(b for _, b in recent) * 8 / 1000 / span, 1) if span > 0 else 0.0
        return {"viewers": len(self.viewers), "active": bool(self.task and not self.task.done()),
                "fps": fps, "kbps": kbps, "frames": self.stats["frames"], "reconnects": self.stats["reconnects"],
                "dropped": sum(v.dropped for v in self.viewers), "geometry": self.geometry,
                "player_only": self.player_only, "player_only_applied": self.stats["player_only_applied"],
                "last_frame_age": round(time.time() - self.stats["last_frame_at"], 1) if self.stats["last_frame_at"] else None,
                "last_error": self.stats["last_error"], "capture_enabled": bool(config.EDUMIX_CAPTURE_ENABLED)}


hub = Hub()


def multipart_part(jpg: bytes) -> bytes:
    return b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n"
