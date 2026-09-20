"""Live audio level for the panel's mixer strip (Part 4). Same on-demand
pattern as preview.py: the frontend polls GET /api/audio/level every
second while the panel is open; each poll "touches" this manager, which
keeps one zoombot-side sampler (scripts/audio-level.py, one JSON line
per ~200ms from zoom_out.monitor) running only while it's been touched
recently. No sampler runs at all when nobody is looking."""
from __future__ import annotations

import asyncio
import json
import time

from . import config
from .control import SUDO, PYTHON3_BIN, _zoombot_env

SAMPLER = config.STREAM_SCRIPTS_DIR / "audio-level.py"
STALE_AFTER_SECONDS = 15      # stop sampling this long after the last poll
# Each sampler process exits after this many seconds and is restarted
# while still touched. Kept short on purpose: the sampler runs as zoombot
# via sudo, so the dashboard user can't signal it (root-owned sudo in
# between) - it stops by seeing EPIPE when we close its stdout, or by its
# own timer at worst. 6s bounds the worst case; one sudo per 6s while a
# panel is open is negligible.
SAMPLER_RUN_SECONDS = 6
LEVEL_STALE_SECONDS = 2.0     # a reading older than this is reported as not live


class AudioLevelManager:
    def __init__(self) -> None:
        self._last_touch = 0.0
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.latest: dict | None = None
        self.latest_at = 0.0

    async def touch(self) -> None:
        self._last_touch = time.time()
        async with self._lock:
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while time.time() - self._last_touch < STALE_AFTER_SECONDS:
            try:
                await self._run_sampler_once()
            except Exception:
                await asyncio.sleep(1)

    async def _run_sampler_once(self) -> None:
        argv = [SUDO, "-u", config.ZOOMBOT_USER, PYTHON3_BIN, str(SAMPLER), "--seconds", str(SAMPLER_RUN_SECONDS)]
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=_zoombot_env(),
        )
        try:
            while True:
                if time.time() - self._last_touch >= STALE_AFTER_SECONDS:
                    break
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=3)
                except asyncio.TimeoutError:
                    break
                if not raw:
                    break
                try:
                    self.latest = json.loads(raw)
                    self.latest_at = time.time()
                except json.JSONDecodeError:
                    continue
        finally:
            if proc.returncode is None:
                # Close our end of the pipe: the sampler's next write hits
                # EPIPE and it exits (taking parec with it). terminate()
                # would be EPERM against the root-owned sudo parent.
                try:
                    proc._transport.close()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.5)
                except (asyncio.TimeoutError, Exception):
                    pass  # worst case it exits on its own SAMPLER_RUN_SECONDS timer

    def snapshot(self) -> dict:
        age = time.time() - self.latest_at if self.latest else None
        live = self.latest is not None and age is not None and age < LEVEL_STALE_SECONDS
        return {
            "live": live,
            "peak_db": self.latest["peak_db"] if live and self.latest else None,
            "peak_l_db": (self.latest.get("peak_l_db") if (self.latest and "peak_l_db" in self.latest) else (self.latest["peak_db"] if live and self.latest else None)),
            "peak_r_db": (self.latest.get("peak_r_db") if (self.latest and "peak_r_db" in self.latest) else (self.latest["peak_db"] if live and self.latest else None)),
            "rms_db": self.latest["rms_db"] if live and self.latest else None,
            "rms_l_db": (self.latest.get("rms_l_db") if (self.latest and "rms_l_db" in self.latest) else (self.latest["rms_db"] if live and self.latest else None)),
            "rms_r_db": (self.latest.get("rms_r_db") if (self.latest and "rms_r_db" in self.latest) else (self.latest["rms_db"] if live and self.latest else None)),
            "age_seconds": round(age, 1) if age is not None else None,
        }


manager = AudioLevelManager()
