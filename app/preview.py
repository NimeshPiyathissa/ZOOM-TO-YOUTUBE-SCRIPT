"""Low-rate JPEG preview of display :99. Only runs while at least one
dashboard viewer has the Overview page open, at reduced resolution and
idle I/O + CPU priority, so it never competes with the encoder. Runs as
the dashboard user directly against Xvfb (the display has no xauth
cookie configured - see README security notes) rather than through
zoombot, since it's a pure read of the framebuffer, not a control action."""
from __future__ import annotations

import asyncio
import time

from . import config

INTERVAL_SECONDS = 3
PREVIEW_PATH = config.DATA_DIR / "preview.jpg"
STALE_AFTER_SECONDS = 15


class PreviewManager:
    """The frontend just polls GET /api/preview.jpg every few seconds
    while the Overview tab is visible. Each poll 'touches' this manager;
    the capture loop keeps running only as long as it's been touched
    recently, and stops itself otherwise - no explicit connect/disconnect
    tracking needed."""

    def __init__(self) -> None:
        self._last_touch = 0.0
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def touch(self) -> None:
        self._last_touch = time.time()
        async with self._lock:
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        while time.time() - self._last_touch < STALE_AFTER_SECONDS:
            await self._capture_once()
            await asyncio.sleep(INTERVAL_SECONDS)

    async def _capture_once(self) -> None:
        tmp_path = PREVIEW_PATH.with_suffix(".tmp.jpg")
        argv = [
            "/usr/bin/nice", "-n", "19",
            "/usr/bin/ionice", "-c", "3",
            "/usr/bin/ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "x11grab", "-video_size", "1920x1080", "-draw_mouse", "0",
            "-i", config.DISPLAY_NUM,
            "-vframes", "1", "-vf", "scale=640:-1",
            "-q:v", "6", "-y", str(tmp_path),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await asyncio.wait_for(proc.wait(), timeout=8)
            if proc.returncode == 0 and tmp_path.exists():
                tmp_path.replace(PREVIEW_PATH)
        except (asyncio.TimeoutError, FileNotFoundError):
            pass

    def latest_bytes(self) -> bytes | None:
        if not PREVIEW_PATH.exists():
            return None
        if time.time() - PREVIEW_PATH.stat().st_mtime > STALE_AFTER_SECONDS:
            return None
        return PREVIEW_PATH.read_bytes()


manager = PreviewManager()
