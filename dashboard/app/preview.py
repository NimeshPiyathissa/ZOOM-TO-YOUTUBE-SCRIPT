"""Low-rate JPEG preview of display :99. Only runs while at least one
dashboard viewer has the Overview page open, at reduced resolution and
idle I/O + CPU priority, so it never competes with the encoder. Runs as
the dashboard user directly against Xvfb (the display has no xauth
cookie configured - see README security notes) rather than through
zoombot, since it's a pure read of the framebuffer, not a control action."""
from __future__ import annotations

import asyncio
import re
import time

from . import config

INTERVAL_SECONDS = 3
PREVIEW_PATH = config.DATA_DIR / "preview.jpg"
STALE_AFTER_SECONDS = 15

# Bug this fixes: the capture below used to hardcode -video_size
# 1920x1080, but Xvfb :99 actually runs at whatever RESOLUTION is
# currently configured (e.g. 1280x720) - x11grab refuses to grab an area
# larger than the real screen ("Capture area 1920x1080 ... outside the
# screen size 1280x720") and exits 1 immediately, every time, which is
# why the preview never worked. Query the real size instead of assuming
# one. Re-checked on each capture loop start (cheap, and correct even if
# RESOLUTION changes without a dashboard restart) rather than cached
# forever.
_XDPYINFO_DIMENSIONS_RE = re.compile(rb"dimensions:\s+(\d+)x(\d+) pixels")


async def _display_size() -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/xdpyinfo", "-display", config.DISPLAY_NUM,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        m = _XDPYINFO_DIMENSIONS_RE.search(out)
        if m:
            return f"{int(m.group(1))}x{int(m.group(2))}"
    except (asyncio.TimeoutError, FileNotFoundError):
        pass
    return "1280x720"  # last-resort fallback, matches the documented default


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
        video_size = await _display_size()
        argv = [
            "/usr/bin/nice", "-n", "19",
            "/usr/bin/ionice", "-c", "3",
            "/usr/bin/ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "x11grab", "-video_size", video_size, "-draw_mouse", "0",
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
