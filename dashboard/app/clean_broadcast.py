"""CLEAN BROADCAST: the YouTube frame contains the meeting picture and nothing else.

ONE rule decides the framing:
    capture the whole desktop, cut away ONLY the Zoom UI that app/zoom_layout.py measured on real frames (title bar,
    meeting toolbar, frame lines, an open side panel), and fit what is left into the output with ONE uniform scale.
So no lesson pixel is cut (nothing inside the measured picture is ever removed), nothing is stretched (one scale
factor), and the encoder, the Live Studio preview and the dashboard's own preview all use the exact same filter string
(stored in current-source.env as ZOOM_CLEAN_FILTER; scripts/lib.sh only validates it).

When the picture is not 16:9 it cannot fill a 16:9 frame without cutting something. The default ("preserve") keeps
the whole picture and fills the rest with a background: plain dark, or a blurred, darkened copy of the picture behind
it (a common broadcast look; the copy is decoration, the real picture is on top, untouched). "fill" crops to 16:9 and
is only allowed with the exact loss shown to the operator first.

When the measurement is not trustworthy (blank or dark screen, an open panel, a layout that moved while measuring),
nothing is cut: the whole screen is fitted, and the status says the Zoom UI may be visible - it never claims "clean".

Pure planning (no I/O). The API in app/main.py grabs the frames and stores the result.
"""
from __future__ import annotations

import re

POLICIES = ("preserve", "fill")
BACKGROUNDS = ("dark", "blur", "black")
DARK_COLOR = "0x0B0D10"
SAME_SHAPE = 0.002           # aspect difference below 0.2% = 2 px on 1080p: one uniform scale to the exact output size
BAR_TOLERANCE = 2            # px of rounding a "no borders" frame may have
UPSCALE_NOTE = 1.05          # captured picture enlarged more than 5% -> mention it
LOW_SOURCE_X = 1.5           # source enlarged more than this -> visible softness, warn
SOURCES = {"unknown": None, "360p": (640, 360), "540p": (960, 540), "720p": (1280, 720), "1080p": (1920, 1080)}

# Everything the encoder needs, kept so Restore Original can put it back.
RESTORE_KEYS = ("meeting_only", "meeting_only_fit", "meeting_only_auto", "meeting_only_region",
                "fullscreen_live", "fullscreen_box", "fullscreen_capture", "fullscreen_plan", "fullscreen_restore")

_SAFE = re.compile(r"^[A-Za-z0-9=:,;\[\]._\-]*$")
_FORBIDDEN = ("movie", "amovie", "file", "sendcmd", "zmq", "drawtext", "subtitles", "ass=", "lavfi", "frei0r",
              "ladspa", "lv2", "opencl", "vulkan", "http")


def safe_filter(vf: str) -> bool:
    """The filter strings this module builds use only these characters/filters; lib.sh applies the same test before
    handing ZOOM_CLEAN_FILTER to ffmpeg (no file/network/command filters can sneak in through the env file)."""
    low = (vf or "").lower()
    return bool(_SAFE.match(vf or "")) and not any(f in low for f in _FORBIDDEN)


def ratio_name(w: int, h: int) -> str:
    if not (w > 0 and h > 0):
        return "unknown"
    ar = w / h
    named = [(16 / 9, "16:9"), (4 / 3, "4:3"), (16 / 10, "16:10"), (21 / 9, "21:9"), (3 / 2, "3:2"), (1.0, "1:1"),
             (9 / 16, "9:16"), (3 / 4, "3:4")]
    best = min(named, key=lambda n: abs(n[0] - ar))
    return best[1] if abs(best[0] - ar) / best[0] < 0.015 else f"{ar:.2f}:1"


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def _bg_graph(background: str, crop: str, sw: int, sh: int, ow: int, oh: int, px: int, py: int) -> str:
    if background == "blur":
        # Small blurred copy behind (cheap: blur at 384x216, then one upscale), real picture on top, untouched.
        return (f"{crop}split=2[cbfg][cbbg];"
                "[cbbg]scale=384:216:force_original_aspect_ratio=increase,crop=384:216,boxblur=10:2,"
                f"eq=brightness=-0.16:saturation=0.75,scale={ow}:{oh}[cbback];"
                f"[cbfg]scale={sw}:{sh}:flags=lanczos[cbfront];[cbback][cbfront]overlay={px}:{py},setsar=1")
    color = "black" if background == "black" else DARK_COLOR
    return f"{crop}scale={sw}:{sh}:flags=lanczos,pad={ow}:{oh}:{px}:{py}:color={color},setsar=1"


def build_filter(capture: tuple[int, int], output: tuple[int, int], content: tuple[int, int, int, int],
                 policy: str = "preserve", background: str = "dark") -> dict:
    """Filter + geometry for showing `content` (x, y, w, h of the captured desktop) in `output`."""
    W, H = capture
    ow, oh = output
    x, y, w, h = content
    lost = {"horizontal_pct": 0.0, "vertical_pct": 0.0, "lost_pct": 0.0}
    if policy == "fill" and abs(w * oh - h * ow) > SAME_SHAPE * h * ow:
        if w * oh > h * ow:                                     # wider than 16:9: cut the sides
            nw = int(round(h * ow / oh))
            lost["horizontal_pct"] = round((1 - nw / w) * 100, 1)
            x, w = x + (w - nw) // 2, nw
        else:                                                   # taller: cut top and bottom
            nh = int(round(w * oh / ow))
            lost["vertical_pct"] = round((1 - nh / h) * 100, 1)
            y, h = y + (h - nh) // 2, nh
        lost["lost_pct"] = round(100 - (100 - lost["horizontal_pct"]) * (100 - lost["vertical_pct"]) / 100, 1)
    crop = "" if (x, y, w, h) == (0, 0, W, H) else f"crop={w}:{h}:{x}:{y},"
    scale = min(ow / w, oh / h)
    if abs(w * oh - h * ow) <= SAME_SHAPE * h * ow:
        sw, sh = ow, oh
    else:
        sw, sh = min(ow, _even(w * scale)), min(oh, _even(h * scale))
    px, py = (ow - sw) // 2, (oh - sh) // 2
    if (sw, sh) == (ow, oh):
        if (w, h) == (ow, oh):
            vf = crop.rstrip(",")                               # exact size: pure crop, no resampling at all
        else:
            vf = f"{crop}scale={ow}:{oh}:flags=lanczos,setsar=1"
    else:
        vf = _bg_graph(background, crop, sw, sh, ow, oh, px, py)
    return {"filter": vf, "box": (x, y, w, h), "scaled": (sw, sh), "offset": (px, py), "scale": round(scale, 3),
            "bars": {"left": px, "right": ow - sw - px, "top": py, "bottom": oh - sh - py}, "crop": lost}


def plan(display: tuple[int, int], output: tuple[int, int], layout: dict | None, *, policy: str = "preserve",
         background: str = "dark", source: str = "unknown") -> dict:
    """Decide the clean framing. `layout` = app/zoom_layout.combine() over the WHOLE desktop (or None)."""
    W, H = display
    ow, oh = output
    policy = policy if policy in POLICIES else "preserve"
    background = background if background in BACKGROUNDS else "dark"
    warnings: list[dict] = []
    verified = bool(layout and layout.get("ok") and _size(layout) == (W, H))
    if verified:
        content = tuple(layout["content"])
        removed = {k: {"px": layout["bands"][k], "kind": layout["kinds"][k]} for k in ("top", "bottom", "left", "right")}
    else:
        content = (0, 0, W, H)
        removed = {k: {"px": 0, "kind": "unknown"} for k in ("top", "bottom", "left", "right")}
        warnings.append({"code": "unverified", "level": "warn",
                         "text": ((layout or {}).get("reason") or "The screen has not been measured.")
                         + " The whole screen is framed, so Zoom's toolbars may be visible on YouTube."})
    x, y, w, h = content
    g = build_filter((W, H), (ow, oh), content, policy, background)
    bars = g["bars"]
    no_bars = max(bars.values()) <= BAR_TOLERANCE
    ar = w / h if h else 0
    ui = [k for k, v in removed.items() if v["kind"] == "zoom_ui"]

    if verified and ui and ar > 16 / 9 * 1.04 and removed["top"]["kind"] == "zoom_ui":
        hidden = round((1 - (16 / 9) / ar) * 100)
        want = f"{W - w + ow}x{H - h + oh}"
        warnings.append({"code": "zoom_crops_camera", "level": "warn",
                         "text": f"Zoom shows the meeting in a {ratio_name(w, h)} area ({w}x{h}). A 16:9 camera is trimmed "
                                 f"by Zoom itself to fill it (about {hidden}% of its height) before it reaches this server - "
                                 f"that part cannot be recovered here. Native fix: a {want} virtual display makes Zoom's "
                                 f"video area exactly {ow}x{oh} (display restart, maintenance window only).",
                         "display_for_native": want})
    if g["crop"]["lost_pct"] > 0:
        warnings.append({"code": "fill_crops", "level": "danger",
                         "text": f"Fill 16:9 cuts {g['crop']['horizontal_pct'] or g['crop']['vertical_pct']}% of the picture "
                                 f"({'left/right' if g['crop']['horizontal_pct'] else 'top/bottom'}). Whiteboard or slide "
                                 "edges may be lost."})
    elif not no_bars:
        side = "top/bottom" if bars["top"] + bars["bottom"] > bars["left"] + bars["right"] else "left/right"
        size = max(bars["top"], bars["left"])
        warnings.append({"code": "aspect", "level": "info",
                         "text": f"The picture is {ratio_name(w, h)}, not {ratio_name(ow, oh)}. To keep all of it, {size} px "
                                 f"{side} are filled with the {background} background (Fill 16:9 would cut instead)."})
    if g["scale"] > UPSCALE_NOTE:
        warnings.append({"code": "upscale", "level": "info",
                         "text": f"The captured picture ({w}x{h}) is enlarged x{g['scale']} to reach the output."})
    src = SOURCES.get(source)
    magnify = None
    if src:
        magnify = round(max(g["scaled"][0] / src[0], g["scaled"][1] / src[1]), 2)   # Zoom may trim one axis: the larger
        if magnify > LOW_SOURCE_X:
            warnings.append({"code": "low_source", "level": "warn",
                             "text": f"Zoom receives the teacher at {src[0]}x{src[1]}; on YouTube it is shown {magnify}x "
                                     "enlarged, so it will look soft. Upscaling cannot add detail - the host/teacher must "
                                     "send HD video (720p or 1080p) for a sharper picture."})
    if layout and layout.get("panel_suspected"):
        warnings.append({"code": "panel_open", "level": "warn", "text": "A Zoom panel seems to be open - close it."})

    if not verified:
        state, label = "unverified", "Not verified · whole screen framed"
    elif g["crop"]["lost_pct"] > 0:
        state, label = "cropped", f"Fill 16:9 · cuts {g['crop']['lost_pct']}%"
    elif no_bars:
        state = "clean"
        label = "Clean · edge to edge · no Zoom UI" if ui else "Clean · edge to edge"
    else:
        state, label = "composed", f"Clean · whole {ratio_name(w, h)} picture on {background} background"
    return {"state": state, "label": label, "policy": policy, "background": background, "source": source,
            "verified": verified, "display": f"{W}x{H}", "capture_size": f"{W}x{H}", "output": f"{ow}x{oh}",
            "content": "%d:%d:%d:%d" % content, "content_size": f"{w}x{h}", "content_ratio": ratio_name(w, h),
            "removed": removed, "scale": g["scale"], "scaled": "%dx%d" % g["scaled"], "bars": bars,
            "crop": g["crop"], "magnify": magnify, "warnings": warnings, "filter": g["filter"],
            "broadcast_box": "%d:%d:%d:%d" % g["box"], "layout_reason": (layout or {}).get("reason", "")}


def _size(layout: dict) -> tuple[int, int]:
    try:
        w, h = (int(v) for v in str(layout.get("size", "")).split("x"))
        return w, h
    except ValueError:
        return (0, 0)


def summary(p: dict) -> str:
    if p["state"] == "clean":
        removed = ", ".join(f"{k} {v['px']} px" for k, v in p["removed"].items() if v["px"])
        return (f"Clean: the {p['content_size']} meeting picture fills {p['output']} edge to edge"
                + (f" - Zoom UI removed ({removed})." if removed else ".") + " Nothing cropped, nothing stretched.")
    if p["state"] == "composed":
        return (f"Clean: Zoom UI removed; the whole {p['content_ratio']} picture is kept and centred on a {p['background']} "
                f"background ({max(p['bars'].values())} px bars) - nothing cropped, nothing stretched.")
    if p["state"] == "cropped":
        return f"Fill 16:9: Zoom UI removed, but {p['crop']['lost_pct']}% of the picture is cut to fill the frame."
    return "Not verified: " + (p["warnings"][0]["text"] if p["warnings"] else "the screen could not be measured.")
