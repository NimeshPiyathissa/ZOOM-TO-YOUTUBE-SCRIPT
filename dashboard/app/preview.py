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


def meeting_only_filter(out_w: int, out_h: int, fit: str = "fit", top: int = 56, bottom: int = 96,
                        region: str | tuple | None = None) -> str:
    """Python twin of scripts/lib.sh build_meeting_only_filter(): the crop/scale the ENCODER applies in
    "Meeting only" mode, so the dashboard preview shows what the broadcast will actually look like (same
    arithmetic, same even-number rounding). tests/test_meeting_filter.py pins the two to the same strings.
    `region` (x:y:w:h, a MEASURED meeting area from app/framing.py) replaces the fixed top/bottom band when it is
    plausible; an implausible one is ignored, so a bad measurement can never crop real content away."""
    max_trim = out_h // 6                          # never trim away more than a sixth per edge
    top, bottom = min(top, max_trim), min(bottom, max_trim)
    top, bottom = top // 2 * 2, bottom // 2 * 2
    rx, ry, rw, rh = 0, top, out_w, out_h - top - bottom
    parsed = parse_region(region)
    if parsed:
        mx, my, mw, mh = (v // 2 * 2 for v in parsed)
        if mw * 3 >= out_w and mh * 3 >= out_h and mx + mw <= out_w and my + mh <= out_h:
            rx, ry, rw, rh = mx, my, mw, mh
    rh = rh // 2 * 2
    if fit == "fill":
        if rw * out_h > rh * out_w:                # meeting area wider than the output: trim the sides
            ch = rh
            cw = rh * out_w // out_h
        else:                                      # taller: trim top/bottom
            cw = rw
            ch = rw * out_h // out_w
        cw, ch = cw // 2 * 2, ch // 2 * 2
        cx = rx + (rw - cw) // 2 // 2 * 2
        cy = ry + (rh - ch) // 2 // 2 * 2
        return f"crop={cw}:{ch}:{cx}:{cy},scale={out_w}:{out_h}:flags=lanczos,setsar=1"
    return (f"crop={rw}:{rh}:{rx}:{ry},scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1")


def parse_region(region) -> tuple | None:
    """'x:y:w:h' (or a 4-tuple) -> ints, else None."""
    if region is None or region == "":
        return None
    if isinstance(region, str):
        m = re.fullmatch(r"(\d+):(\d+):(\d+):(\d+)", region.strip())
        return tuple(int(g) for g in m.groups()) if m else None
    try:
        x, y, w, h = (int(v) for v in region)
        return (x, y, w, h) if min(x, y, w, h) >= 0 else None
    except (TypeError, ValueError):
        return None


def _trim(value, default: int) -> int:
    try:
        return int(value) if str(value).strip().isdigit() else default
    except (TypeError, ValueError):
        return default


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
        self._mo_cache: tuple[float, dict] = (0.0, {})

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

    async def _meeting_only(self, video_size: str) -> str:
        """The encoder's Meeting-only crop for the current Zoom source ("" when it is off), so the preview
        matches the broadcast output. Reads current-source.env at most every 10 s."""
        now = time.time()
        if now - self._mo_cache[0] > 10:
            try:
                from . import control
                src = await asyncio.get_event_loop().run_in_executor(None, control.read_current_source)
            except Exception:
                src = {}
            self._mo_cache = (now, src)
        src = self._mo_cache[1]
        if src.get("SOURCE_TYPE") != "zoom":
            return ""
        try:
            w, h = (int(v) for v in video_size.split("x"))
        except ValueError:
            return ""
        if src.get("ZOOM_FULLSCREEN_LIVE") == "1":
            # Same box the encoder crops (lib.sh build_capture_framing); a 16:9 preview of the capture's width shows
            # exactly the broadcast's framing (the box is exact 16:9 and every RESOLUTION preset is 16:9).
            from . import fullscreen_live
            return fullscreen_live.fullscreen_filter((w, h), (w, w * 9 // 16 // 2 * 2), src.get("ZOOM_FRAMING_BOX"))
        if src.get("ZOOM_MEETING_ONLY") != "1":
            return ""
        fit = "fill" if src.get("ZOOM_MEETING_ONLY_FIT") == "fill" else "fit"
        return meeting_only_filter(w, h, fit, _trim(src.get("ZOOM_MEETING_ONLY_TRIM_TOP"), 56),
                                   _trim(src.get("ZOOM_MEETING_ONLY_TRIM_BOTTOM"), 96),
                                   src.get("ZOOM_MEETING_ONLY_REGION"))

    async def _capture_once(self) -> None:
        tmp_path = PREVIEW_PATH.with_suffix(".tmp.jpg")
        video_size = await _display_size()
        vf = "scale=640:-1"
        mo = await self._meeting_only(video_size)
        if mo:
            vf = f"{mo},{vf}"
        argv = [
            "/usr/bin/nice", "-n", "19",
            "/usr/bin/ionice", "-c", "3",
            "/usr/bin/ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "x11grab", "-video_size", video_size, "-draw_mouse", "0",
            "-i", config.DISPLAY_NUM,
            "-vframes", "1", "-vf", vf,
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
