"""Fullscreen Live: lossless 16:9 framing of the Zoom meeting for the YouTube output.

ONE rule decides the encoder's framing:
    take the smallest box of the OUTPUT's shape that contains every non-black pixel Zoom is showing,
    and scale that box uniformly to the output.
So content is never cut (the box contains all of it), nothing is stretched (box and output have the same shape, one
scale factor), and the only thing removed is pure-black border around the content (app/framing.py's measurement:
luma <= 20, union of several frames, padded). When the content itself is not 16:9 (a 4:3 slide, a portrait phone
share) the box cannot hug both sides: the remaining bars are the price of keeping the whole picture, and the plan says
so (`limitations`) instead of cropping.

Why not the existing Meeting-only modes: they trim fixed 56/96 px off the top/bottom (real picture whenever Zoom's
toolbars are hidden), then either pad back to 16:9 (Fit: black bars on YouTube) or crop the sides and enlarge (Fill:
~24% of a 1920x1080 frame lost). In a fullscreen Zoom window with the pointer parked the toolbars auto-hide and the
meeting IS the whole screen, so the native, filter-free 1920x1080 capture is the best possible output.

Everything here is pure (no I/O) - the API in app/main.py does the measuring and the window work; the encoder applies
the result through scripts/lib.sh build_fullscreen_live_filter (the twin of fullscreen_filter below).
"""
from __future__ import annotations

import math

MIN_BORDER = 8             # px; bars thinner than this are rounding/anti-aliasing, not worth reporting
MAX_UPSCALE = 1.5          # a box narrower than output/1.5 is grown (centred, still lossless) so the output never goes soft

# What Restore Original puts back (the source's framing options before Fullscreen Live was switched on).
RESTORE_KEYS = ("meeting_only", "meeting_only_fit", "meeting_only_auto", "meeting_only_region")


def ratio_name(w: int, h: int) -> str:
    if not (w > 0 and h > 0):
        return "unknown"
    ar = w / h
    named = [(16 / 9, "16:9"), (4 / 3, "4:3"), (16 / 10, "16:10"), (21 / 9, "21:9"), (3 / 2, "3:2"), (1.0, "1:1"),
             (9 / 16, "9:16"), (3 / 4, "3:4")]
    best = min(named, key=lambda n: abs(n[0] - ar))
    return best[1] if abs(best[0] - ar) / best[0] < 0.02 else f"{ar:.2f}:1"


def parse_size(size) -> tuple[int, int] | None:
    try:
        w, h = (int(v) for v in str(size or "").lower().split("x"))
    except ValueError:
        return None
    return (w, h) if w > 0 and h > 0 else None


def parse_box(box) -> tuple[int, int, int, int] | None:
    """'x:y:w:h' (or a 4-tuple) -> ints; None when it isn't a usable box."""
    if isinstance(box, (tuple, list)):
        vals = list(box)
    else:
        try:
            vals = [int(v) for v in str(box or "").split(":")]
        except ValueError:
            return None
    if len(vals) != 4 or not all(isinstance(v, int) for v in vals) or min(vals) < 0 or vals[2] == 0 or vals[3] == 0:
        return None
    return tuple(vals)


ASPECT_TOLERANCE = 1000    # same shape when |w*oh - h*ow| <= h*ow/1000: <=0.1% - a sub-pixel scale difference, invisible


def same_shape(w: int, h: int, ow: int, oh: int) -> bool:
    """Box and output close enough in shape to scale with one factor (twin of the same test in lib.sh)."""
    return abs(w * oh - h * ow) * ASPECT_TOLERANCE <= h * ow


def fit_box(display: tuple[int, int], content: tuple[int, int, int, int], output: tuple[int, int],
            min_w: int = 0) -> tuple[int, int, int, int] | None:
    """Smallest output-shaped box (to within 0.1%, see same_shape) that contains `content` (x, y, w, h), is at least
    `min_w` wide and lies inside the display. None when no such box fits (the display is not that shape).
    Pixel-exact sizes (the capture is RGB, so odd sizes/offsets are fine): stepping in exact 16:9 units (32x18 px for
    even sizes) left up to ~15 px bars around content that was already 16:9."""
    W, H = display
    ow, oh = output
    cx, cy, cw, ch = content
    bw = max(cw, -(-ch * ow // oh), min_w, 1)
    bh = -(-bw * oh // ow)
    if bh < ch:                                       # rounding: grow until the height covers the content too
        bh = ch
        bw = max(bw, -(-bh * ow // oh))
    if bw > W and same_shape(W, bh, ow, oh):
        bw = W
    if bh > H and same_shape(bw, H, ow, oh):
        bh = H
    if bw > W or bh > H or not same_shape(bw, bh, ow, oh):
        return None
    x = min(max(cx + cw // 2 - bw // 2, 0), W - bw)
    y = min(max(cy + ch // 2 - bh // 2, 0), H - bh)
    if x > cx or y > cy or x + bw < cx + cw or y + bh < cy + ch:
        return None
    return (x, y, bw, bh)


def fullscreen_filter(capture: tuple[int, int], output: tuple[int, int], box) -> str:
    """The encoder's video filter for a box. Twin of scripts/lib.sh build_fullscreen_live_filter (pinned by tests).
    "" = no filter at all: the capture already IS the output (native 1:1)."""
    W, H = capture
    ow, oh = output
    b = parse_box(box)
    if b is None or b[0] + b[2] > W or b[1] + b[3] > H:
        b = (0, 0, W, H)                               # an unusable box never crops anything
    x, y, w, h = b
    crop = "" if (x, y, w, h) == (0, 0, W, H) else f"crop={w}:{h}:{x}:{y},"
    if not crop and (W, H) == (ow, oh):
        return ""
    if same_shape(w, h, ow, oh):
        return f"{crop}scale={ow}:{oh}:flags=lanczos,setsar=1"
    return (f"{crop}scale={ow}:{oh}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={ow}:{oh}:(ow-iw)/2:(oh-ih)/2:black,setsar=1")


def output_bars(content: tuple[int, int, int, int], box: tuple[int, int, int, int], output: tuple[int, int]) -> dict:
    """Black (non-content) pixels left on each side of the OUTPUT frame."""
    ow, oh = output
    bx, by, bw, bh = box
    cx, cy, cw, ch = content
    sc = min(ow / bw, oh / bh)
    padx, pady = (ow - bw * sc) / 2, (oh - bh * sc) / 2
    return {"left": round(padx + (cx - bx) * sc), "right": round(padx + (bx + bw - cx - cw) * sc),
            "top": round(pady + (cy - by) * sc), "bottom": round(pady + (by + bh - cy - ch) * sc)}


def plan(display: tuple[int, int], capture: tuple[int, int], output: tuple[int, int],
         window: dict | None, measured: dict | None, chrome: dict | None = None) -> dict:
    """Decide the framing. `measured` = app/framing.py measure()/decide() over the WHOLE screen (band 0/0);
    `window` = control.zoom_window_geometry() ({"w","h","x","y","fullscreen"}) or None;
    `chrome` = app/zoom_layout.py's verdict. The black-border measurement cannot see Zoom's dark-GREY title bar and
    toolbar (that is how the first version reported "Native 16:9 - no borders" with both on screen), so when chrome is
    given, "native"/"optimized" are only reported if Zoom UI was positively ruled out."""
    W, H = display
    ow, oh = output
    issues: list[dict] = []
    limitations: list[str] = []
    if tuple(capture) != (W, H):
        what = "part of the screen was never captured" if capture[0] < W or capture[1] < H else "it asked for more than the screen"
        issues.append({"code": "capture_mismatch",
                       "detail": f"The encoder was set to capture {capture[0]}x{capture[1]} of a {W}x{H} desktop ({what}).",
                       "fix": f"Corrected: Fullscreen Live captures the whole {W}x{H} desktop and scales it to {ow}x{oh}."})
    if window is None:
        issues.append({"code": "window_unknown", "detail": "No Zoom meeting window was found to check.",
                       "fix": "Join the meeting, then press Fullscreen Live again."})
    elif not window.get("fullscreen") or (window.get("w"), window.get("h"), window.get("x"), window.get("y")) != (W, H, 0, 0):
        issues.append({"code": "window_geometry",
                       "detail": f"The Zoom window is {window.get('w')}x{window.get('h')} at +{window.get('x')}+{window.get('y')}"
                                 f"{'' if window.get('fullscreen') else ' and not fullscreen'} on the {W}x{H} desktop.",
                       "fix": "Fullscreen was requested from the window manager; press Fullscreen Live again if this stays."})
    if W * oh != H * ow:
        limitations.append(f"The desktop is {W}x{H} ({ratio_name(W, H)}), not {ratio_name(ow, oh)}. Changing the desktop size "
                           "needs a display restart, which Fullscreen Live never does - the picture is kept whole.")

    full = (0, 0, W, H)
    measured_ok = bool(measured and measured.get("ok"))
    content = full
    if measured_ok and measured.get("region"):
        b = parse_box(_region_xywh(measured["region"]))
        if b and b[0] + b[2] <= W and b[1] + b[3] <= H:
            content = b
    if not measured_ok:
        limitations.append("The screen could not be measured right now (blank, dark or no frame), so nothing is removed: "
                           "the whole desktop is framed.")

    box = fit_box((W, H), content, (ow, oh), min_w=min(W, math.ceil(ow / MAX_UPSCALE)))
    if box is None:
        box = full                                   # e.g. a 4:3 desktop: frame all of it, scaled and padded
    bars = output_bars(content, box, (ow, oh))
    cx, cy, cw, ch = content
    if max(bars.values()) > MIN_BORDER and content != full:
        sides = (f"{bars['top']} px top and {bars['bottom']} px bottom" if bars["top"] + bars["bottom"] >= bars["left"] + bars["right"]
                 else f"{bars['left']} px left and {bars['right']} px right")
        limitations.append(f"The meeting content is {cw}x{ch} ({ratio_name(cw, ch)}), not {ratio_name(ow, oh)}. To keep the whole "
                           f"picture - no cropping, no stretching - the output keeps {sides} of black.")
    if box != full and box[2] * MAX_UPSCALE <= ow + 1 and (box[2] > cw + 2 * MIN_BORDER and box[3] > ch + 2 * MIN_BORDER):
        limitations.append(f"The meeting content is small on screen ({cw}x{ch}); it is enlarged at most {MAX_UPSCALE}x so it stays "
                           "sharp. Speaker view or a fullscreen share gives a bigger picture.")

    zoom_ui: dict = {}
    if chrome is not None:
        if chrome.get("ok"):
            zoom_ui = {k: v for k, v in (chrome.get("bands") or {}).items()
                       if (chrome.get("kinds") or {}).get(k) == "zoom_ui" and v > 0}
            if zoom_ui:
                where = ", ".join(f"{k} {v} px" for k, v in zoom_ui.items())
                limitations.insert(0, f"Zoom's own controls are on screen ({where}) and are part of this frame - they would "
                                      "be broadcast. Fullscreen Live keeps the whole screen; Clean Broadcast removes them.")
        else:
            limitations.insert(0, "Zoom's toolbars could not be ruled out (" + (chrome.get("reason") or "no measurement")
                               + ") - this framing is not verified as border-free.")
    if box == full and (W, H) == (ow, oh):
        state = "native"
    else:
        state = "optimized"
    if limitations:
        state = "limited"
    return {"state": state, "box": "%d:%d:%d:%d" % box, "capture_size": f"{W}x{H}", "output": f"{ow}x{oh}",
            "display": f"{W}x{H}", "content": "%d:%d:%d:%d" % content, "content_ratio": ratio_name(cw, ch),
            "scale": round(min(ow / box[2], oh / box[3]), 3), "bars": bars, "issues": issues,
            "limitations": limitations, "measured": measured_ok, "chrome_checked": chrome is not None, "zoom_ui": zoom_ui,
            "filter": fullscreen_filter((W, H), (ow, oh), box)}


def _region_xywh(region: str) -> str:
    """framing.decide() regions are 'x:y:w:h' already; kept as a seam in case that ever changes."""
    return region


def summary(p: dict) -> str:
    """One sentence for the UI/audit log."""
    if p["state"] == "native":
        return f"Native {p['output']}: the Zoom meeting fills the frame - no borders, no crop, no scaling."
    if p["state"] == "optimized":
        return (f"Black border removed: the {p['content_ratio']} meeting area is scaled {p['scale']}x to fill {p['output']} "
                "- nothing cropped, nothing stretched.")
    return "Framed without cropping or stretching. " + (p["limitations"][0] if p["limitations"] else "")
