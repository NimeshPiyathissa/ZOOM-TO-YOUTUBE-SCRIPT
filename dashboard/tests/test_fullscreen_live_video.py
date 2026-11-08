"""Fullscreen Live on REAL video: ffmpeg builds synthetic 1920x1080 "desktops" (no Zoom meeting involved), the real
measurement (app/framing.py) and the real encoder filter (scripts/lib.sh, via bash) produce an H.264 test recording,
and the decoded output pixels are checked:
  * the output is exactly the configured size,
  * all four red corner markers of the content survive (nothing cropped),
  * the green 200x200 square is still square (nothing stretched) and scaled as planned,
  * the black bars left in the output are the ones the plan reported.
The old Meeting-only Fill on the same pictures is checked too, to pin down what Fullscreen Live fixes.
Recordings are kept in $FSL_RECORDINGS_DIR when set (for a human look)."""
import asyncio
import os
import pathlib
import shutil
import subprocess

import pytest

from app import framing, fullscreen_live as F, preview

FFMPEG = shutil.which("ffmpeg")
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(FFMPEG is None or BASH is None, reason="ffmpeg and bash required")
LIB = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "lib.sh"
WIN = {"w": 1920, "h": 1080, "x": 0, "y": 0, "fullscreen": True}


def ff(*args):
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]


def desktop(path, x, y, w, h, W=1920, H=1080):
    """Black W x H screen; content (x, y, w, h) = a grey grid (never pure black) with red 40x40 corners and a green 200x200 centre."""
    content = (f"color=c=0x4a5260:s={w}x{h}:r=1,drawgrid=w=80:h=80:t=2:c=0x8890a0,format=rgb24,"
               "drawbox=x=0:y=0:w=40:h=40:color=red:t=fill,drawbox=x=iw-40:y=0:w=40:h=40:color=red:t=fill,"
               "drawbox=x=0:y=ih-40:w=40:h=40:color=red:t=fill,drawbox=x=iw-40:y=ih-40:w=40:h=40:color=red:t=fill,"
               "drawbox=x=(iw-200)/2:y=(ih-200)/2:w=200:h=200:color=lime:t=fill")
    ff("-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r=1", "-f", "lavfi", "-i", content,
       "-filter_complex", f"[0:v][1:v]overlay={x}:{y}:shortest=1,format=rgb24", "-frames:v", "1", str(path))


def bash_filter(cap, out, box):
    body = LIB.read_text(encoding="utf-8").replace("\r", "")
    start = body.index("build_fullscreen_live_filter() {")
    func = body[start:body.index("\n}\n", start) + 3]
    return subprocess.run([BASH, "-c", func + f'\nbuild_fullscreen_live_filter {cap[0]} {cap[1]} {out[0]} {out[1]} "{box}"; printf %s "$FULLSCREEN_LIVE_FILTER"'],
                          capture_output=True, text=True).stdout


def record_and_decode(src_png, vf, out_dir, name, out_size):
    """Encode a 1 s H.264 'test recording' like stream.sh does, then decode its middle frame to raw RGB."""
    mp4 = out_dir / f"{name}.mp4"
    args = ["-loop", "1", "-framerate", "30", "-i", str(src_png), "-t", "1"]
    if vf:
        args += ["-vf", vf]
    ff(*args, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-crf", "12", str(mp4))
    probe = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", str(mp4)], capture_output=True, text=True).stdout.strip()
    raw = out_dir / f"{name}.rgb"
    ff("-ss", "0.5", "-i", str(mp4), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", str(raw))
    ff("-ss", "0.5", "-i", str(mp4), "-frames:v", "1", str(out_dir / f"{name}.png"))
    return probe, raw.read_bytes()


def analyse(buf, W, H):
    """Bounding boxes of the green square and of all non-black pixels, and red marker presence per quadrant."""
    def px(x, y):
        o = (y * W + x) * 3
        return buf[o], buf[o + 1], buf[o + 2]
    gx0 = gy0 = 10 ** 9
    gx1 = gy1 = -1
    nx0 = ny0 = 10 ** 9
    nx1 = ny1 = -1
    red = {"tl": False, "tr": False, "bl": False, "br": False}
    for y in range(0, H, 2):
        for x in range(0, W, 2):
            r, g, b = px(x, y)
            if r > 24 or g > 24 or b > 24:
                nx0, ny0, nx1, ny1 = min(nx0, x), min(ny0, y), max(nx1, x), max(ny1, y)
            if g > 200 and r < 80 and b < 80:
                gx0, gy0, gx1, gy1 = min(gx0, x), min(gy0, y), max(gx1, x), max(gy1, y)
            if r > 200 and g < 70 and b < 70:
                red[("t" if y < H // 2 else "b") + ("l" if x < W // 2 else "r")] = True
    return {"green": (gx0, gy0, gx1 - gx0 + 2, gy1 - gy0 + 2), "content": (nx0, ny0, nx1 - nx0 + 2, ny1 - ny0 + 2), "red": red}


SCENARIOS = [
    # name, content x, y, w, h, output size
    ("speaker_view_fullscreen", 0, 0, 1920, 1080, (1920, 1080)),
    ("letterboxed_16x9_video", 160, 90, 1600, 900, (1920, 1080)),
    ("screen_share_4x3", 240, 0, 1440, 1080, (1920, 1080)),
    ("whiteboard_16x10", 96, 0, 1728, 1080, (1920, 1080)),
    ("letterboxed_to_720p_output", 160, 90, 1600, 900, (1280, 720)),
]


@pytest.fixture(scope="module")
def out_dir(tmp_path_factory):
    d = os.environ.get("FSL_RECORDINGS_DIR")
    if d:
        p = pathlib.Path(d)
        p.mkdir(parents=True, exist_ok=True)
        return p
    return tmp_path_factory.mktemp("fsl")


@pytest.mark.parametrize("name,x,y,w,h,out", SCENARIOS)
def test_fullscreen_live_output_keeps_every_pixel_of_content_unstretched(out_dir, name, x, y, w, h, out):
    src = out_dir / f"{name}_desktop.png"
    desktop(src, x, y, w, h)
    measured = asyncio.new_event_loop().run_until_complete(
        framing.measure("1920x1080", 0, 0, input_args=["-loop", "1", "-framerate", "4", "-i", str(src)]))
    assert measured["ok"]
    p = F.plan((1920, 1080), out, out, WIN, measured)
    vf = bash_filter((1920, 1080), out, p["box"])
    assert vf == p["filter"]                                          # the encoder's string == the planner's
    size, buf = record_and_decode(src, vf, out_dir, f"{name}_fullscreen_live", out)
    OW, OH = out
    assert size == f"{OW},{OH}"
    a = analyse(buf, OW, OH)
    assert all(a["red"].values()), f"content corner cropped: {a['red']}"
    gw, gh = a["green"][2], a["green"][3]
    assert abs(gw - gh) <= 4, f"stretched: green {gw}x{gh}"
    assert abs(gw - 200 * p["scale"]) <= 6, f"scale {gw / 200:.3f} != planned {p['scale']}"
    cx, cy, cw, ch = a["content"]
    bars = {"left": cx, "top": cy, "right": OW - cx - cw, "bottom": OH - cy - ch}
    for side, v in p["bars"].items():
        assert abs(bars[side] - v) <= 6, f"{side} bar {bars[side]} != planned {v}"
    if w * 9 == h * 16:
        assert max(bars.values()) <= 6, f"16:9 content must fill the output edge to edge: {bars}"


def test_old_meeting_only_fill_cropped_and_enlarged_the_same_meeting(out_dir):
    """What was on production before (Meeting-only Fill, no measured area): pins the bug Fullscreen Live fixes."""
    src = out_dir / "speaker_view_fullscreen_desktop.png"
    if not src.exists():
        desktop(src, 0, 0, 1920, 1080)
    vf = preview.meeting_only_filter(1920, 1080, "fill", 56, 96)
    _, buf = record_and_decode(src, vf, out_dir, "speaker_view_old_meeting_only_fill", (1920, 1080))
    a = analyse(buf, 1920, 1080)
    assert not any(a["red"].values())                                  # every content corner was cut away
    assert a["green"][2] > 230                                         # and the picture was zoomed in (~1.165x)
