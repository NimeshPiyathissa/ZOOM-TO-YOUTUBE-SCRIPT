"""Measured meeting area: finds the pure-black borders around what Zoom is actually showing.

Why measure instead of using fixed crop numbers: Zoom's own layout changes with the meeting (speaker view, gallery,
a shared 4:3 slide, a window that is not the full screen), so black borders appear and disappear. This looks at the
real screen and returns the largest area that contains anything other than pure black.

Safety rules - a wrong measurement must never cost real content:
  * only PURE black counts as border (cropdetect limit 20 of 255 = video-range black plus noise), never dark-grey UI or a dark video;
  * the result is the UNION of several frames (cropdetect never shrinks its box), so one moving face is enough to keep
    the area it moves through;
  * borders under MIN_BORDER px are noise and are ignored;
  * a measured area smaller than half the band in either direction is rejected (a blank/dark screen, a "waiting for
    the host" page) - the fixed trims stay in charge;
  * the region is padded by PAD px on every side.
If black is part of the actual source video (a letterboxed recording being shared, say) it is reported as such:
the caller can show the numbers and the operator decides; nothing is hidden silently.

Read-only: one short `ffmpeg x11grab` at nice 19 as the dashboard user (the same way preview.py reads the screen).
"""
from __future__ import annotations

import asyncio
import re
import time

from . import config

LIMIT = 20            # luma (0-255) at or below which a pixel is "black": video-range black is 16, so 20 = pure black
                      # plus a little encoding noise; dark-grey UI (Zoom's bars are ~28+) and dark video stay content
MIN_BORDER = 8        # px; thinner borders are encoding noise / anti-aliasing
PAD = 2               # px of safety margin kept around the detected content
SAMPLE_SECONDS = 1.6
SAMPLE_FPS = 4
_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


def parse_cropdetect(stderr: str) -> list[tuple[int, int, int, int]]:
    """All `crop=w:h:x:y` suggestions ffmpeg's cropdetect printed, in order."""
    return [tuple(int(g) for g in m.groups()) for m in _CROP_RE.finditer(stderr or "")]


def decide(size: tuple[int, int], band_top: int, band_bottom: int, rect: tuple[int, int, int, int] | None, *,
           samples: int = 1) -> dict:
    """`rect` = (w, h, x, y) measured INSIDE the band (full width, y from band_top to H - band_bottom).
    Returns the verdict as plain data (no I/O) so it can be unit-tested exhaustively."""
    W, H = size
    band_h = H - band_top - band_bottom
    out = {"ok": False, "region": None, "size": f"{W}x{H}", "samples": samples,
           "margins": {"left": 0, "top": 0, "right": 0, "bottom": 0}, "reason": ""}
    if rect is None or band_h <= 0:
        out["reason"] = "No picture could be measured (no frame from the screen)."
        return out
    w, h, x, y = rect
    margins = {"left": x, "top": y, "right": W - (x + w), "bottom": band_h - (y + h)}
    out["margins"] = margins
    if w < W // 2 or h < band_h // 2:
        out["reason"] = ("The screen is mostly black or blank right now, so a border measurement would not be "
                         "trustworthy. The fixed top/bottom trims stay in use.")
        return out
    if max(margins.values()) < MIN_BORDER:
        out["ok"] = True
        out["reason"] = "No black borders: the meeting fills the screen area."
        return out
    # Pad (never crop closer than PAD px to content) and clamp to the screen.
    left, right = max(0, x - PAD), min(W, x + w + PAD)
    top, bottom = max(band_top, band_top + y - PAD), min(H - band_bottom, band_top + y + h + PAD)
    rx, ry, rw, rh = left, top, right - left, bottom - top
    rx, ry, rw, rh = rx // 2 * 2, ry // 2 * 2, rw // 2 * 2, rh // 2 * 2
    out.update(ok=True, region=f"{rx}:{ry}:{rw}:{rh}",
               reason="Black borders found (left %d, top %d, right %d, bottom %d px). If these are part of the "
                      "source video itself, leave Auto off." % (margins["left"], margins["top"], margins["right"], margins["bottom"]))
    return out


def fill_crop(size: tuple[int, int], band_top: int, band_bottom: int, region: str | None, target: tuple[int, int] = (16, 9)) -> dict:
    """What Fill throws away: Fill scales the picked area to COVER the 16:9 output, so whatever does not match that shape is
    cropped. Returns percentages of the area's width/height lost (0 when the shape already matches), so the UI can warn
    before a whiteboard, slide edge or the teacher is cut."""
    W, H = size
    try:
        rw, rh = (int(v) for v in region.split(":")[2:4]) if region else (W, H - band_top - band_bottom)
    except ValueError:
        rw, rh = W, H - band_top - band_bottom
    if rw <= 0 or rh <= 0:
        return {"horizontal_pct": 0.0, "vertical_pct": 0.0, "lost_pct": 0.0}
    ar, tar = rw / rh, target[0] / target[1]
    h = round(max(0.0, 1 - tar / ar) * 100, 1) if ar > tar else 0.0
    v = round(max(0.0, 1 - ar / tar) * 100, 1) if ar < tar else 0.0
    return {"horizontal_pct": h, "vertical_pct": v, "lost_pct": round(100 - (100 - h) * (100 - v) / 100, 1)}


FILL_WARN_PCT = 5.0                    # above this the UI shows a visible "Fill crops N%" warning


def changed_materially(old: str | None, new: str | None, tolerance: int = 16) -> bool:
    """True when two 'x:y:w:h' regions differ by more than `tolerance` px on any value (or one is missing)."""
    if (old or None) == (new or None):
        return False
    try:
        a = [int(v) for v in (old or "").split(":")]
        b = [int(v) for v in (new or "").split(":")]
    except ValueError:
        return True
    if len(a) != 4 or len(b) != 4:
        return True
    return any(abs(p - q) > tolerance for p, q in zip(a, b))


async def measure(size: str | None = None, band_top: int = 56, band_bottom: int = 96, *,
                  input_args: list[str] | None = None) -> dict:
    """One measurement of the live screen. `input_args` replaces the x11grab input (tests feed a synthetic picture)."""
    if size is None:
        from . import preview
        size = await preview._display_size()
    try:
        W, H = (int(v) for v in size.split("x"))
    except ValueError:
        return decide((0, 0), band_top, band_bottom, None)
    max_trim = H // 6
    band_top, band_bottom = min(band_top, max_trim) // 2 * 2, min(band_bottom, max_trim) // 2 * 2
    band_h = H - band_top - band_bottom
    src = input_args or ["-f", "x11grab", "-video_size", size, "-framerate", str(SAMPLE_FPS), "-draw_mouse", "0",
                         "-i", config.DISPLAY_NUM]
    argv = ["/usr/bin/nice", "-n", "19", "/usr/bin/ionice", "-c", "3", "ffmpeg", "-hide_banner", "-nostdin",
            "-loglevel", "info", *src, "-t", str(SAMPLE_SECONDS),
            "-vf", f"crop={W}:{band_h}:0:{band_top},cropdetect=limit={LIMIT}:round=2", "-f", "null", "-"]
    if input_args is not None:                                   # tests: no nice/ionice wrappers needed
        argv = argv[argv.index("ffmpeg"):]
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), timeout=12)
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        return decide((W, H), band_top, band_bottom, None)
    found = parse_cropdetect(err.decode(errors="replace"))
    res = decide((W, H), band_top, band_bottom, found[-1] if found else None, samples=len(found))
    res["measured_at"] = time.time()
    return res
