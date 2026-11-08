"""Where Zoom's own UI ends and the meeting picture begins - measured on real captured frames, no fixed coordinates.

Why this exists: on the production desktop the Zoom webinar window draws a title bar (~69 px), a meeting toolbar
(~76 px) and thin frame lines INSIDE the 1920x1080 screen, and they never auto-hide. They are dark grey (luma 21-48)
with text and icons, so a pure-black border detector (ffmpeg cropdetect, app/framing.py) counts them as picture and
reports "no borders". This module peels Zoom's UI off each screen edge instead.

How a band is recognised (scanning inward from one edge of the screen, line by line):
  * a FLAT line: >= 97% of its samples are one colour (within +-6 per channel) and that colour is dark (luma <= 72)
    and neutral (grey/black: channels within 12 of each other - Zoom's UI is never coloured, a navy slide border is) -
    the empty part of a title bar, a toolbar, a frame line, a side panel's margin, or a black letterbox;
  * a UI line: the dominant colour covers >= 55% of the line and is the same colour as a flat line already peeled
    on this side - the rows/columns of that bar that carry text, icons or buttons.
  The band ends at the first line that is neither: the picture. Sides are measured on the rows between the top and
  bottom bands and vice versa, refined twice, so a side panel next to the toolbar does not hide either of them.

Safety rules - a wrong measurement must never cost lesson content:
  * only flat dark lines start a band, and a band must start at the very edge of the screen;
  * a band may not exceed 15% of the height (top/bottom) or 30% of the width (sides, room for an open chat panel);
    reaching that limit means "not sure" (a black screen, a dark slide) and nothing is removed;
  * the picture must keep at least half the screen in each direction;
  * several frames are measured and must agree (+-2 px); when they disagree the SMALLER band per side wins, so a
    measurement can only ever keep more of the picture, never less;
  * when unsure the result says so (`ok` False) and callers frame the whole screen.
Removed lines are, by construction, either flat (no picture information) or carry the same colour as Zoom's bars, so
nothing that is actually teacher video, slide or whiteboard is cut.

Pure functions over raw RGB24 frames (bytes) plus one async grab helper (a short low-priority ffmpeg x11grab, the
same way app/preview.py and app/framing.py read the screen).
"""
from __future__ import annotations

import asyncio
from collections import Counter

FLAT_COVERAGE = 0.97
UI_COVERAGE = 0.55
COLOR_TOL = 6
CHROME_LUMA_MAX = 72
NEUTRAL_SPREAD = 12          # Zoom's bars/frames/panels are grey or black; a coloured line is picture
BLACK_LUMA_MAX = 24          # a band whose colours are all at or below this is a black border, not Zoom UI
MAX_TOP_BOTTOM = 0.15
MAX_SIDE = 0.30
MIN_CONTENT = 0.5
AGREE_PX = 2
STEP = 2                     # sample every 2nd pixel along a line


def luma(c: tuple[int, int, int]) -> float:
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def _chrome_colour(c: tuple[int, int, int]) -> bool:
    return luma(c) <= CHROME_LUMA_MAX and max(c) - min(c) <= NEUTRAL_SPREAD


def _close(a, b, tol: int = COLOR_TOL) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol and abs(a[2] - b[2]) <= tol


def _stats(samples: list[bytes]) -> tuple[tuple[int, int, int], float]:
    """Dominant colour of a line and the share of samples within COLOR_TOL of it."""
    if not samples:
        return (0, 0, 0), 0.0
    counts = Counter(samples)
    modal_b, _ = counts.most_common(1)[0]
    modal = (modal_b[0], modal_b[1], modal_b[2])
    near = sum(n for k, n in counts.items() if _close((k[0], k[1], k[2]), modal))
    return modal, near / len(samples)


class Frame:
    """One RGB24 frame. Lines are sampled every STEP pixels inside [lo, hi) of the other axis."""

    def __init__(self, data: bytes, w: int, h: int):
        if len(data) < w * h * 3:
            raise ValueError("frame is shorter than %dx%d RGB24" % (w, h))
        self.data, self.w, self.h = data, w, h

    def row(self, y: int, lo: int, hi: int) -> list[bytes]:
        d, base = self.data, (y * self.w) * 3
        return [d[base + x * 3: base + x * 3 + 3] for x in range(lo, hi, STEP)]

    def col(self, x: int, lo: int, hi: int) -> list[bytes]:
        d, w = self.data, self.w
        return [d[(y * w + x) * 3: (y * w + x) * 3 + 3] for y in range(lo, hi, STEP)]


def _peel(line, count: int, limit: int) -> dict:
    """Peel one side. `line(i)` returns the samples of the i-th line from the edge.
    The band ends on its last FLAT line: Zoom always draws a flat frame line or margin between its UI and the picture,
    so UI-like lines after the last flat one are given back to the picture (a teacher's dark shirt at the edge of the
    video looks like a "UI line" - dark, one dominant colour - but is never followed by a flat line)."""
    flat: list[tuple[int, int, int]] = []
    ui = ui_kept = 0
    end = 0                                   # lines up to and including the last flat one
    for i in range(min(count, limit + 1)):
        if i == limit:
            return {"px": i, "stop": "limit", "flat_colors": flat, "ui_lines": ui_kept}
        modal, cov = _stats(line(i))
        dark = _chrome_colour(modal)
        if dark and cov >= FLAT_COVERAGE:
            if not any(_close(modal, c) for c in flat):
                flat.append(modal)
            end, ui_kept = i + 1, ui
            continue
        if dark and cov >= UI_COVERAGE and any(_close(modal, c) for c in flat):
            ui += 1
            continue
        return {"px": end, "stop": "edge", "flat_colors": flat, "ui_lines": ui_kept}
    return {"px": count, "stop": "limit", "flat_colors": flat, "ui_lines": ui_kept}


def _kind(side: dict) -> str:
    if side["px"] == 0:
        return "none"
    if side["ui_lines"] or any(luma(c) > BLACK_LUMA_MAX for c in side["flat_colors"]):
        return "zoom_ui"
    return "black"


def detect(frame: Frame) -> dict:
    """Bands of Zoom UI / black border on each side of ONE frame and the picture inside them."""
    W, H = frame.w, frame.h
    lim_tb, lim_side = int(H * MAX_TOP_BOTTOM), int(W * MAX_SIDE)
    top = bottom = left = right = 0
    sides: dict = {}
    for _ in range(3):
        y0, y1 = (top, H - bottom) if (top or bottom) else (int(H * 0.3), int(H * 0.7))
        sides["left"] = _peel(lambda i: frame.col(i, y0, y1), W, lim_side)
        sides["right"] = _peel(lambda i: frame.col(W - 1 - i, y0, y1), W, lim_side)
        left, right = sides["left"]["px"], sides["right"]["px"]
        x0, x1 = left, max(left + 1, W - right)
        sides["top"] = _peel(lambda i: frame.row(i, x0, x1), H, lim_tb)
        sides["bottom"] = _peel(lambda i: frame.row(H - 1 - i, x0, x1), H, lim_tb)
        new = (sides["top"]["px"], sides["bottom"]["px"])
        if new == (top, bottom):
            break
        top, bottom = new
    top, bottom, left, right = (sides[k]["px"] for k in ("top", "bottom", "left", "right"))
    lines = {"left": lambda i: frame.col(i, top, H - bottom), "right": lambda i: frame.col(W - 1 - i, top, H - bottom),
             "top": lambda i: frame.row(i, left, W - right), "bottom": lambda i: frame.row(H - 1 - i, left, W - right)}
    for k, side in sides.items():
        if side["stop"] == "edge" and side["flat_colors"]:
            lim = lim_side if k in ("left", "right") else lim_tb
            side["deeper"] = _deeper_chrome_line(lines[k], side["px"] + 1, lim, side["flat_colors"])
    return _verdict(W, H, sides)


def _deeper_chrome_line(line, start: int, limit: int, colors: list) -> int | None:
    """A flat line in one of this side's Zoom colours further inside the 'picture' means the band was cut short (an open
    panel whose cards/borders stop the scan). Returns its distance from the edge, or None."""
    for i in range(start, limit):
        modal, cov = _stats(line(i))
        if cov >= FLAT_COVERAGE and any(_close(modal, c) for c in colors):
            return i
    return None


def _verdict(W: int, H: int, sides: dict) -> dict:
    bands = {k: sides[k]["px"] for k in ("top", "bottom", "left", "right")}
    kinds = {k: _kind(sides[k]) for k in bands}
    cw, ch = W - bands["left"] - bands["right"], H - bands["top"] - bands["bottom"]
    out = {"ok": False, "size": f"{W}x{H}", "bands": bands, "kinds": kinds,
           "content": (bands["left"], bands["top"], max(cw, 0), max(ch, 0)), "reason": "", "frames": 1}
    limited = [k for k in bands if sides[k]["stop"] == "limit"]
    if limited:
        out["reason"] = (f"Could not find where the picture starts on the {', '.join(limited)} side (dark or blank screen). "
                         "Nothing is removed.")
        return out
    deeper = [k for k in bands if sides[k].get("deeper") is not None]
    if deeper:
        out["reason"] = (f"Zoom UI continues further in on the {', '.join(deeper)} side (an open panel, e.g. Statistics or "
                         "chat?). Close it and measure again. Nothing is removed until the layout is clear.")
        out["panel_suspected"] = {k: sides[k]["deeper"] for k in deeper}
        return out
    if cw < W * MIN_CONTENT or ch < H * MIN_CONTENT:
        out["reason"] = "The measured picture would be under half the screen - not trusted. Nothing is removed."
        return out
    out["ok"] = True
    ui = [k for k in bands if kinds[k] == "zoom_ui"]
    out["reason"] = ("Zoom UI found on the " + ", ".join(ui) + " side(s)." if ui
                     else "No Zoom UI at the screen edges.")
    return out


def combine(results: list[dict]) -> dict:
    """Several frames -> one verdict. All must be confident and agree within AGREE_PX; the smallest band per side wins."""
    if not results:
        return {"ok": False, "reason": "No frame could be read from the screen.", "bands": {}, "kinds": {}, "frames": 0}
    if not all(r["ok"] for r in results):
        bad = next(r for r in results if not r["ok"])
        return dict(bad, ok=False, frames=len(results))
    first = results[0]
    W, H = (int(v) for v in first["size"].split("x"))
    bands = {k: min(r["bands"][k] for r in results) for k in first["bands"]}
    spread = max(max(r["bands"][k] for r in results) - bands[k] for k in bands)
    kinds = {k: ("zoom_ui" if any(r["kinds"][k] == "zoom_ui" for r in results) else first["kinds"][k]) for k in bands}
    for k in bands:
        if bands[k] == 0:
            kinds[k] = "none"
    out = {"ok": spread <= AGREE_PX, "size": first["size"], "bands": bands, "kinds": kinds, "frames": len(results),
           "content": (bands["left"], bands["top"], W - bands["left"] - bands["right"], H - bands["top"] - bands["bottom"]),
           "spread": spread}
    if out["ok"]:
        ui = [k for k in bands if kinds[k] == "zoom_ui"]
        out["reason"] = ("Zoom UI found on the " + ", ".join(ui) + " side(s), stable over %d frames." % len(results) if ui
                         else "No Zoom UI at the screen edges (%d frames)." % len(results))
    else:
        out["reason"] = (f"The layout changed while measuring (bands moved {spread} px) - for example a panel opened. "
                         "Nothing is removed; measure again when the meeting view is steady.")
    return out


def detect_frames(frames: list[bytes], w: int, h: int) -> dict:
    return combine([detect(Frame(f, w, h)) for f in frames])


async def grab_frames(size: str, count: int = 2, gap_fps: float = 2.0, *, display: str | None = None,
                      input_args: list[str] | None = None) -> tuple[list[bytes], int, int]:
    """`count` raw RGB24 frames of the screen, 1/gap_fps s apart (one short nice-19 ffmpeg grab). Tests pass
    `input_args` (e.g. a looped PNG) instead of x11grab."""
    w, h = (int(v) for v in size.split("x"))
    if input_args is None:
        from . import config
        src = ["-f", "x11grab", "-video_size", size, "-framerate", str(gap_fps), "-draw_mouse", "0",
               "-i", display or config.DISPLAY_NUM]
        argv = ["/usr/bin/nice", "-n", "19", "/usr/bin/ionice", "-c", "3", "ffmpeg"]
    else:
        src, argv = input_args, ["ffmpeg"]
    argv += ["-hide_banner", "-nostdin", "-loglevel", "error", *src, "-frames:v", str(count),
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        return [], w, h
    n = w * h * 3
    return [out[i * n:(i + 1) * n] for i in range(len(out) // n)], w, h


async def measure(size: str, count: int = 2, **kw) -> dict:
    frames, w, h = await grab_frames(size, count, **kw)
    return detect_frames(frames, w, h)
