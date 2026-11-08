"""Output test kit: Zoom-like mock screens -> the real detector/planner/encoder filter -> local H.264 recordings -> checks.

No Zoom meeting and no X display needed: each scene is a short lossless "screen recording" of a Zoom desktop (the real
Zoom 7.2.1 title bar / toolbar / frame lines cut from tests/fixtures/zoom/webinar_speaker.png, anonymised) around
synthetic lesson content with large and small text, coloured small text, 1-px lines, a circle, red corner markers and a
moving block. It is fed to ffmpeg exactly where x11grab would be.

Used by tests/output/test_output_kit.py (pytest) and tests/output/run_kit.py (CLI that keeps recordings, before/after
sheets and report.json for a human look). Needs ffmpeg + ffprobe on PATH; text needs a TrueType font (found below)."""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
DASH = HERE.parents[1]
FIX = DASH / "tests" / "fixtures" / "zoom" / "webinar_speaker.png"
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
FPS = 30
SECONDS = 2
OUT = (1920, 1080)
ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
          "-b:v", "6000k", "-maxrate", "6000k", "-bufsize", "12000k", "-pix_fmt", "yuv420p"]   # = stream.sh defaults
BG = (0xEC, 0xEF, 0xF4)
RED, GREEN = "0xE61414", "0x14DC28"


def font() -> str | None:
    for f in (r"C:\Windows\Fonts\arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/TTF/DejaVuSans.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"):
        if os.path.exists(f):
            return f
    return None


def _fontarg() -> str:
    f = font()
    if not f:
        return ""
    return "fontfile='" + f.replace("\\", "/").replace(":", "\\:") + "'"


def ff(*args, capture=False):
    r = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args],
                       capture_output=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace")[-2000:])
    return r.stdout if capture else None


# ---------------------------------------------------------------- lesson content

def pattern(path: pathlib.Path, w: int, h: int, label: str) -> dict:
    """Static lesson picture w x h. Returns the regions the checks use (relative to the picture)."""
    fa = _fontarg()
    d = max(80, min(w, h) // 4) // 2 * 2                     # circle diameter
    cx, cy = w // 2 - d // 2, h // 2 - d // 2
    tx, ty, tw, th = 60, 70, min(760, w - 120), 150          # small-text block (SSIM region)
    parts = [f"color=c=0x{BG[0]:02X}{BG[1]:02X}{BG[2]:02X}:s={w}x{h}:r=1,format=rgb24",
             "drawgrid=w=64:h=64:t=1:c=0x9AA3B5",            # 1-px lines
             f"drawbox=x=0:y=0:w=40:h=40:c={RED}:t=fill", f"drawbox=x=iw-40:y=0:w=40:h=40:c={RED}:t=fill",
             f"drawbox=x=0:y=ih-40:w=40:h=40:c={RED}:t=fill", f"drawbox=x=iw-40:y=ih-40:w=40:h=40:c={RED}:t=fill"]
    if fa:
        small = ["Small text 14px - the quick brown fox jumps over the lazy dog 0123456789",
                 "F = m a    v = u + a t    s = u t + 1/2 a t^2    E = m c^2    pV = nRT",
                 "Thin text should stay readable on YouTube at 1080p and 720p outputs."]
        for i, t in enumerate(small):
            parts.append(f"drawtext={fa}:text='{t}':fontsize=14:fontcolor=0x111111:x={tx}:y={ty + i * 22}")
        parts.append(f"drawtext={fa}:text='red 13px  blue 13px  chroma check':fontsize=13:fontcolor=0xC81E1E:x={tx}:y={ty + 70}")
        parts.append(f"drawtext={fa}:text='{label}':fontsize=64:fontcolor=0x1B2A4A:x=(w-text_w)/2:y=h-160")
    ff("-f", "lavfi", "-i", ",".join(parts), "-frames:v", "1", str(path.with_suffix(".base.png")))
    # circle (alpha mask via geq) composited onto the base
    ff("-i", str(path.with_suffix(".base.png")), "-f", "lavfi", "-i",
       f"color=c={GREEN}:s={d}x{d}:r=1,format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
       f"a='if(lte(hypot(X-{d / 2 - 0.5},Y-{d / 2 - 0.5}),{d / 2 - 0.5}),255,0)'",
       "-filter_complex", f"[0:v][1:v]overlay={cx}:{cy}:format=auto,format=rgb24", "-frames:v", "1", str(path))
    path.with_suffix(".base.png").unlink()
    return {"circle": (cx, cy, d, d), "text": (tx, ty, tw, th), "size": (w, h)}


# ---------------------------------------------------------------- scenes

def _chrome_graph(W: int, H: int, out: str) -> str:
    """Zoom's real bars cut from the fixture, stretched only along their length, on a W x H black screen ([out])."""
    ch = H - 145
    return (f"[0:v]format=gbrp,split=4[s0][s1][s2][s3];[s0]crop=1920:69:0:0,scale={W}:69:flags=neighbor[top];"
            f"[s1]crop=1920:76:0:1004,scale={W}:76:flags=neighbor[bot];"
            f"[s2]crop=4:935:0:69,scale=4:{ch}:flags=neighbor[l];[s3]crop=4:935:1916:69,scale=4:{ch}:flags=neighbor[r];"
            f"color=c=black:s={W}x{H}:r={FPS},format=gbrp[bg];[bg][top]overlay=format=gbrp:x=0:y=0[a];"
            f"[a][bot]overlay=format=gbrp:x=0:y={H - 76}[b];[b][l]overlay=format=gbrp:x=0:y=69[c];"
            f"[c][r]overlay=format=gbrp:x={W - 4}:y=69[{out}]")


SCENES = {
    # name: screen W, H, zoom chrome?, picture placement inside Zoom's area (fn of area w,h) -> (x, y, w, h), backdrop
    "meeting_16x9": dict(W=1920, H=1080, chrome=True, place=lambda aw, ah: (0, 0, aw, ah), backdrop="black",
                         note="Speaker view: Zoom fills its 2.04:1 area with the camera"),
    "slide_4x3": dict(W=1920, H=1080, chrome=True, place=lambda aw, ah: ((aw - ah * 4 // 3) // 2, 0, ah * 4 // 3, ah),
                      backdrop="black", note="4:3 slide share, pillarboxed by Zoom"),
    "share_16x10": dict(W=1920, H=1080, chrome=True, place=lambda aw, ah: ((aw - ah * 16 // 10) // 2, 0, ah * 16 // 10, ah),
                        backdrop="black", note="16:10 laptop screen share, pillarboxed by Zoom"),
    "gallery_2x2": dict(W=1920, H=1080, chrome=True, place=None, backdrop="0x1A1A1A",
                        note="Gallery view: 2x2 16:9 tiles on Zoom's dark background"),
    "letterboxed_video": dict(W=1920, H=1080, chrome=True, place=lambda aw, ah: (0, (ah - aw * 100 // 239) // 2, aw, aw * 100 // 239),
                              backdrop="black", note="2.39:1 video with black bars inside the picture"),
    "native_1928x1225": dict(W=1928, H=1225, chrome=True, place=lambda aw, ah: (0, 0, aw, ah), backdrop="black",
                             note="Step B display: Zoom's area is exactly 1920x1080"),
    "web_client_fullscreen": dict(W=1920, H=1080, chrome=False, place=lambda aw, ah: (0, 0, aw, ah), backdrop="black",
                                  note="Zoom web client with the clean-feed extension: picture fills the screen"),
}


def make_scene(name: str, d: pathlib.Path) -> dict:
    """Writes <name>.mkv (lossless RGB, SECONDS long) and returns the ground truth: where the picture is on screen."""
    sc = SCENES[name]
    W, H = sc["W"], sc["H"]
    ax, ay, aw, ah = (4, 69, W - 8, H - 145) if sc["chrome"] else (0, 0, W, H)
    pics = []
    if sc["place"] is not None:
        px, py, pw, ph = sc["place"](aw, ah)
        pics.append((ax + px, ay + py, pw, ph))
    else:                                                     # gallery: 2x2 16:9 tiles with 8 px gaps, centred
        th = (ah - 3 * 8) // 2
        tw = th * 16 // 9
        gx = ax + (aw - (2 * tw + 8)) // 2
        for r in range(2):
            for c in range(2):
                pics.append((gx + c * (tw + 8), ay + 8 + r * (th + 8), tw, th))
    regions = []
    inputs = ["-i", str(FIX)]
    for i, (x, y, w, h) in enumerate(pics):
        png = d / f"{name}_pic{i}.png"
        reg = pattern(png, w, h, f"{name} {i + 1}" if len(pics) > 1 else name.replace("_", " "))
        regions.append({"pos": (x, y), **reg})
        inputs += ["-loop", "1", "-framerate", str(FPS), "-i", str(png)]
    n = 0
    cur = "s0"
    g = _chrome_graph(W, H, cur) if sc["chrome"] else f"color=c=black:s={W}x{H}:r={FPS},format=gbrp[{cur}]"

    def over(src: str, x, y, extra: str = "") -> str:
        nonlocal n, cur
        n += 1
        nxt = f"s{n}"
        step = f";[{cur}][{src}]overlay=format=gbrp:x={x}:y={y}{extra}[{nxt}]"
        cur = nxt
        return step
    if sc["backdrop"] != "black":
        g += f";color=c={sc['backdrop']}:s={aw}x{ah}:r={FPS},format=gbrp[bd]" + over("bd", ax, ay)
    for i, (x, y, w, h) in enumerate(pics):
        g += f";[{i + 1}:v]format=gbrp[p{i}]" + over(f"p{i}", x, y, ":shortest=1")
    # moving element: an orange 60x60 block sliding across the first picture (proves motion survives the pipeline)
    x0, y0, w0, h0 = pics[0]
    g += f";color=c=0xFF8A00:s=60x60:r={FPS},format=gbrp[mv]" + over(
        "mv", f"'{x0 + 50}+mod(t*300\\,{max(1, w0 - 160)})'", y0 + h0 // 2 + 100, ":shortest=1")
    g += f";[{cur}]format=gbrp[out]"
    mkv = d / f"{name}.mkv"
    ff(*inputs, "-filter_complex", g, "-map", "[out]", "-t", str(SECONDS), "-c:v", "ffv1", "-pix_fmt", "gbrp", str(mkv))
    ys, xs = [p[1] for p in pics], [p[0] for p in pics]
    bbox = (min(xs), min(ys), max(p[0] + p[2] for p in pics) - min(xs), max(p[1] + p[3] for p in pics) - min(ys))
    return {"name": name, "screen": (W, H), "pictures": pics, "picture_bbox": bbox, "regions": regions,
            "note": sc["note"], "video": str(mkv)}


# ---------------------------------------------------------------- measurement helpers

def frame_rgb(video: str, t: float, size: tuple[int, int]) -> bytes:
    data = ff("-ss", str(t), "-i", video, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-", capture=True)
    assert len(data) == size[0] * size[1] * 3, (len(data), size)
    return data


def png_frame(video: str, t: float, png: pathlib.Path, scale_w: int | None = None):
    vf = ["-vf", f"scale={scale_w}:-2:flags=lanczos"] if scale_w else []
    ff("-ss", str(t), "-i", video, "-frames:v", "1", *vf, str(png))


def encode(scene: dict, vf: str, mp4: pathlib.Path, color_tags: bool) -> None:
    args = ["-i", scene["video"]]
    if vf:
        args += ["-vf", vf]
    tags = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"] if color_tags else []
    ff(*args, *ENCODE, *tags, "-an", str(mp4))


def probe(mp4: pathlib.Path) -> dict:
    r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,pix_fmt,color_space,color_primaries,color_transfer,nb_read_frames",
                        "-count_frames", "-of", "json", str(mp4)], capture_output=True, text=True, timeout=120)
    return json.loads(r.stdout)["streams"][0]


def _px(buf, W, x, y):
    o = (y * W + x) * 3
    return buf[o], buf[o + 1], buf[o + 2]


def analyse(buf: bytes, W: int, H: int) -> dict:
    """Red corner markers per quadrant, green circle bbox, orange block bbox, Zoom-grey rows, near-black edge rows/cols."""
    red = {"tl": False, "tr": False, "bl": False, "br": False}
    g = [10 ** 9, 10 ** 9, -1, -1]
    o = [10 ** 9, 10 ** 9, -1, -1]
    for y in range(0, H, 2):
        for x in range(0, W, 2):
            r, gg, b = _px(buf, W, x, y)
            if r > 180 and gg < 90 and b < 90:
                red[("t" if y < H // 2 else "b") + ("l" if x < W // 2 else "r")] = True
            if gg > 170 and r < 110 and b < 110:
                g = [min(g[0], x), min(g[1], y), max(g[2], x), max(g[3], y)]
            if r > 200 and 100 < gg < 180 and b < 80:
                o = [min(o[0], x), min(o[1], y), max(o[2], x), max(o[3], y)]

    def row_kind(y):
        grey = black = 0
        n = 0
        for x in range(0, W, 8):
            p = _px(buf, W, x, y)
            n += 1
            grey += all(abs(a - b) <= 6 for a, b in zip(p, (42, 43, 45)))
            black += max(p) <= 20
        return grey / n, black / n

    def col_black(x):
        n = black = 0
        for y in range(0, H, 8):
            n += 1
            black += max(_px(buf, W, x, y)) <= 20
        return black / n
    zoom_rows = sum(1 for y in list(range(0, 30)) + list(range(H - 30, H)) if row_kind(y)[0] > 0.6)
    black_edges = {"top": row_kind(2)[1] > 0.9, "bottom": row_kind(H - 3)[1] > 0.9,
                   "left": col_black(2) > 0.9, "right": col_black(W - 3) > 0.9}
    return {"corners": red, "circle": (g[2] - g[0] + 2, g[3] - g[1] + 2) if g[2] >= 0 else None,
            "moving": ((o[0] + o[2]) // 2, (o[1] + o[3]) // 2) if o[2] >= 0 else None,
            "zoom_ui_rows": zoom_rows, "black_edges": black_edges}


def ssim_psnr(out_mp4: pathlib.Path, ref_png: pathlib.Path, out_crop: tuple, ref_crop: tuple, t: float) -> dict:
    """SSIM/PSNR of a region of the encoded output against the same region of the ideal (codec-free) frame."""
    ox, oy, w, h = out_crop
    rx, ry, _, _ = ref_crop
    vals = {}
    for metric, rx_ in (("ssim", r"All:([0-9.]+)"), ("psnr", r"average:([0-9.]+|inf)")):
        r = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-ss", str(t), "-i", str(out_mp4), "-i", str(ref_png),
                            "-filter_complex", f"[0:v]crop={w}:{h}:{ox}:{oy},format=yuv444p[a];"
                                               f"[1:v]crop={w}:{h}:{rx}:{ry},format=yuv444p[b];[a][b]{metric}",
                            "-frames:v", "1", "-f", "null", "-"], capture_output=True, text=True, timeout=120)
        m = re.search(rx_, r.stderr)
        vals[metric] = (99.0 if m and m.group(1) == "inf" else round(float(m.group(1)), 4)) if m else None
    return vals


def sheet(before_png: pathlib.Path, after_png: pathlib.Path, out_png: pathlib.Path, label: str):
    fa = _fontarg()
    txt = (f",drawtext={fa}:text='{label}':fontsize=22:fontcolor=white:x=12:y=8:box=1:boxcolor=0x000000AA"
           if fa else "")
    ff("-i", str(before_png), "-i", str(after_png), "-filter_complex",
       "[0:v]scale=960:-2:flags=lanczos,pad=960:610:0:(oh-ih)/2:color=0x202020[a];"
       f"[1:v]scale=960:-2:flags=lanczos,pad=960:610:0:(oh-ih)/2:color=0x202020[b];[a][b]hstack,pad=iw+0:ih+40:0:40:color=0x202020{txt}",
       "-frames:v", "1", str(out_png))


# ---------------------------------------------------------------- one case: scene x mode -> recording + checks

def _import_app():
    import sys
    if str(DASH) not in sys.path:
        sys.path.insert(0, str(DASH))
    from app import clean_broadcast, zoom_layout
    return clean_broadcast, zoom_layout


def _roi_bbox(buf, W, H, roi, test):
    x0, y0, x1, y1 = (max(0, roi[0]), max(0, roi[1]), min(W, roi[2]), min(H, roi[3]))
    b = [10 ** 9, 10 ** 9, -1, -1]
    for y in range(y0, y1):
        for x in range(x0, x1):
            if test(*_px(buf, W, x, y)):
                b = [min(b[0], x), min(b[1], y), max(b[2], x), max(b[3], y)]
    return None if b[2] < 0 else (b[0], b[1], b[2] - b[0] + 1, b[3] - b[1] + 1)


def laplacian_var(buf, W, region) -> float:
    """Sharpness of a region: variance of the 4-neighbour Laplacian of luma (higher = crisper edges)."""
    x, y, w, h = region

    def lum(xx, yy):
        r, g, b = _px(buf, W, xx, yy)
        return 0.299 * r + 0.587 * g + 0.114 * b
    vals = [4 * lum(xx, yy) - lum(xx - 1, yy) - lum(xx + 1, yy) - lum(xx, yy - 1) - lum(xx, yy + 1)
            for yy in range(y + 1, y + h - 1) for xx in range(x + 1, x + w - 1)]
    m = sum(vals) / len(vals)
    return sum((v - m) ** 2 for v in vals) / len(vals)


def _red(r, g, b):
    return r > 170 and g < 100 and b < 100


def _green(r, g, b):
    return g > 170 and r < 110 and b < 110


def run_case(scene: dict, mode: str, d: pathlib.Path, *, vf_override: str | None = None, label: str | None = None,
             background: str = "blur") -> dict:
    """mode: preserve (Auto) / fit / fill, or 'custom' with vf_override (old modes, for comparison)."""
    C, Z = _import_app()
    W, H = scene["screen"]
    frames = [frame_rgb(scene["video"], t, (W, H)) for t in (0.2, 1.2)]
    layout = Z.detect_frames(frames, W, H)
    tag = label or f"{scene['name']}__{mode}"
    p = None
    if vf_override is None:
        p = C.plan((W, H), OUT, layout, policy=mode, background=background)
        vf = p["filter"]
    else:
        vf = vf_override
    mp4 = d / f"{tag}.mp4"
    encode(scene, vf, mp4, color_tags="out_color_matrix=bt709" in (vf or ""))
    info = probe(mp4)
    OW, OH = int(info["width"]), int(info["height"])
    out1 = frame_rgb(str(mp4), 1.0, (OW, OH))
    a = analyse(out1, OW, OH)
    res = {"case": tag, "scene": scene["name"], "mode": mode, "filter": vf, "output": f"{OW}x{OH}",
           "pix_fmt": info.get("pix_fmt"), "color_space": info.get("color_space"),
           "layout_ok": layout.get("ok"), "detected": list(layout.get("content") or ()),
           "truth": list(scene["picture_bbox"]), "state": p["state"] if p else "old-mode",
           "label": p["label"] if p else label, "zoom_ui_rows": a["zoom_ui_rows"], "black_edges": a["black_edges"]}
    if p:
        bx, by, bw, bh = (int(v) for v in p["broadcast_box"].split(":"))
        sw, sh = (int(v) for v in p["scaled"].split("x"))
        px, py = p["bars"]["left"], p["bars"]["top"]
        sx, sy = sw / bw, sh / bh
        res.update(scale=p["scale"], resampled=p["resampled"], bars=p["bars"], crop_pct=p["crop"]["lost_pct"])

        def to_out(X, Y):
            return round((X - bx) * sx + px), round((Y - by) * sy + py)
        X0, Y0, Wb, Hb = scene["picture_bbox"]
        found = {}
        for k, (X, Y) in {"tl": (X0 + 20, Y0 + 20), "tr": (X0 + Wb - 21, Y0 + 20), "bl": (X0 + 20, Y0 + Hb - 21),
                          "br": (X0 + Wb - 21, Y0 + Hb - 21)}.items():
            ox, oy = to_out(X, Y)
            found[k] = 0 <= ox < OW and 0 <= oy < OH and any(
                _red(*_px(out1, OW, min(OW - 1, max(0, ox + dx)), min(OH - 1, max(0, oy + dy))))
                for dx in (-6, 0, 6) for dy in (-6, 0, 6))
        res["corners_kept"] = found
        reg = scene["regions"][0]
        cx, cy, cd, _ = reg["circle"]
        ox0, oy0 = to_out(reg["pos"][0] + cx, reg["pos"][1] + cy)
        ox1, oy1 = to_out(reg["pos"][0] + cx + cd, reg["pos"][1] + cy + cd)
        cb = _roi_bbox(out1, OW, OH, (ox0 - 20, oy0 - 20, ox1 + 20, oy1 + 20), _green)
        res["circle_wh"] = list(cb[2:]) if cb else None
        tx, ty, tw, th = reg["text"]
        ax0, ay0 = to_out(reg["pos"][0] + tx, reg["pos"][1] + ty)
        ow_, oh_ = max(8, round(tw * sx)) // 2 * 2, max(8, round(th * sy)) // 2 * 2
        if font() and ax0 >= 0 and ay0 >= 0 and ax0 + ow_ <= OW and ay0 + oh_ <= OH:
            pic = d / f"{scene['name']}_pic0.png"
            # the ideal = the very same crop + scale the pipeline does, without the codec (pixel-registered)
            ideal = d / f"{tag}_ideal.png"
            sc_ = f",scale={sw}:{sh}:flags=lanczos" if (sw, sh) != (bw, bh) else ""
            ff("-ss", "1.0", "-i", scene["video"], "-vf", f"crop={bw}:{bh}:{bx}:{by}{sc_}", "-frames:v", "1", str(ideal))
            res.update(ssim_psnr(mp4, ideal, (ax0, ay0, ow_, oh_), (ax0 - px, ay0 - py, ow_, oh_), 1.0))
            srcbuf = ff("-i", str(pic), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-", capture=True)
            src_sharp = laplacian_var(srcbuf, reg["size"][0], (tx, ty, tw, th))
            res["text_sharpness"] = round(laplacian_var(out1, OW, (ax0, ay0, ow_, oh_)) / src_sharp, 3)
    else:
        res["corners_kept"] = a["corners"]
    m0 = analyse(frame_rgb(str(mp4), 0.4, (OW, OH)), OW, OH)["moving"]
    m1 = analyse(frame_rgb(str(mp4), 1.4, (OW, OH)), OW, OH)["moving"]
    res["motion_px"] = abs(m1[0] - m0[0]) if m0 and m1 else 0
    before, after = d / f"{scene['name']}_before.png", d / f"{tag}_after.png"
    if not before.exists():
        png_frame(scene["video"], 1.0, before)
    png_frame(str(mp4), 1.0, after)
    sheet(before, after, d / f"{tag}_sheet.png", f"{scene['name']} - {res['label'] or mode}".replace(":", " "))
    return res


def verdict(r: dict) -> dict:
    """PASS criteria for Auto: exact 1920x1080 yuv420p BT.709, all 4 picture corners, round circle, no black edge, no
    Zoom UI, small-text SSIM >= 0.95 vs the ideal (lesson picture at the planned size), motion kept."""
    checks = {
        "output_1920x1080": r["output"] == "1920x1080",
        "yuv420p_bt709": r["pix_fmt"] == "yuv420p" and r.get("color_space") == "bt709",
        "no_content_cropped": all(r["corners_kept"].values()),
        "not_stretched": bool(r.get("circle_wh")) and abs(r["circle_wh"][0] - r["circle_wh"][1]) <= max(3, 0.03 * r["circle_wh"][0]),
        "no_black_bars": not any(r["black_edges"].values()),
        "no_zoom_ui": r["zoom_ui_rows"] == 0,
        "small_text_ssim_0.95": (r.get("ssim") or 0) >= 0.95 if font() else True,
        "motion_kept": r["motion_px"] > 50,
    }
    return {"pass": all(checks.values()), "checks": checks}
