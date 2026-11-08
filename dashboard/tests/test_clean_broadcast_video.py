"""Clean Broadcast on REAL video, compared with the two older modes on the same Zoom screens.

Each screen is a 1920x1080 (or taller) Zoom webinar desktop: the real Zoom 7.2.1 bars/frames from production (anonymised
fixtures) around a synthetic meeting picture with red 40x40 corner markers and a green 200x200 square. The real
detector measures it, the real planner builds the filter, the encoder's own bash check accepts it, ffmpeg encodes an
H.264 test recording like test-recording.sh, and the decoded output pixels are checked:
  * output is exactly 1920x1080,
  * all four corner markers survive (no lesson content cut),
  * the green square is still square (no stretching),
  * no Zoom bar grey (42,43,45) / toolbar black-with-icons is left at the top or bottom of the frame.
The same screens through Meeting-only Fill and Fullscreen Live pin down what those modes did wrong.
Recordings + a comparison table are kept in $CB_RECORDINGS_DIR when set (for a human look)."""
import json
import os
import pathlib
import shutil
import subprocess

import pytest

from app import clean_broadcast as C, fullscreen_live as F, preview, zoom_layout as Z

FFMPEG = shutil.which("ffmpeg")
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(FFMPEG is None or BASH is None, reason="ffmpeg and bash required")
REPO = pathlib.Path(__file__).resolve().parents[2]
FIX = pathlib.Path(__file__).resolve().parent / "fixtures" / "zoom"
LIB = REPO / "scripts" / "lib.sh"
RESULTS: dict = {}


def ff(*args):
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, timeout=180)
    assert r.returncode == 0, r.stderr.decode(errors="replace")[-1500:]
    return r.stdout


@pytest.fixture(scope="module")
def out_dir(tmp_path_factory):
    d = os.environ.get("CB_RECORDINGS_DIR")
    if d:
        p = pathlib.Path(d)
        p.mkdir(parents=True, exist_ok=True)
        yield p
        (p / "comparison.json").write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")
        return
    yield tmp_path_factory.mktemp("cb")


def screen(out_dir, name, W=1920, H=1080, picture_ratio=None):
    """Zoom chrome from the real fixture around a synthetic picture. picture_ratio=(4,3) puts a 4:3 picture inside Zoom's
    video area with Zoom-style black pillarbox (a 4:3 share)."""
    out = out_dir / f"{name}_screen.png"
    cw, ch = W - 8, H - 145
    pw, ph = (cw, ch) if not picture_ratio else (ch * picture_ratio[0] // picture_ratio[1], ch)
    pic = (f"color=c=0xe6e2ea:s={pw}x{ph},drawgrid=w=96:h=96:t=2:c=0x7080c0,format=gbrp,"
           "drawbox=x=0:y=0:w=40:h=40:color=red:t=fill,drawbox=x=iw-40:y=0:w=40:h=40:color=red:t=fill,"
           "drawbox=x=0:y=ih-40:w=40:h=40:color=red:t=fill,drawbox=x=iw-40:y=ih-40:w=40:h=40:color=red:t=fill,"
           "drawbox=x=(iw-200)/2:y=(ih-200)/2:w=200:h=200:color=lime:t=fill,"
           f"pad={cw}:{ch}:({cw}-iw)/2:0:black")
    ov = "overlay=format=gbrp:x"
    graph = (f"[0:v]format=gbrp,split=4[s0][s1][s2][s3];[s0]crop=1920:69:0:0,scale={W}:69:flags=neighbor[top];"
             f"[s1]crop=1920:76:0:1004,scale={W}:76:flags=neighbor[bot];"
             f"[s2]crop=4:935:0:69,scale=4:{ch}:flags=neighbor[l];[s3]crop=4:935:1916:69,scale=4:{ch}:flags=neighbor[r];"
             f"[1:v]format=gbrp[pic];color=c=black:s={W}x{H},format=gbrp[bg];[bg][top]{ov}=0:y=0[a];"
             f"[a][bot]{ov}=0:y={H - 76}[b];[b][l]{ov}=0:y=69[c];[c][r]{ov}={W - 4}:y=69[d];[d][pic]{ov}=4:y=69,format=rgb24")
    ff("-i", str(FIX / "webinar_speaker.png"), "-f", "lavfi", "-i", pic, "-filter_complex", graph, "-frames:v", "1", str(out))
    return out


def raw(png, W, H):
    return ff("-i", str(png), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-")


def bash_accepts(vf):
    body = LIB.read_text(encoding="utf-8").replace("\r", "")
    start = body.index("clean_filter_ok() {")
    func = body[start:body.index("\n}\n", start) + 3]
    return subprocess.run([BASH, "-c", func + '\nclean_filter_ok "$VF" && printf ok'], capture_output=True, text=True,
                          env={**os.environ, "VF": vf}).stdout == "ok"


def record(out_dir, src, vf, name):
    """1 s H.264 test recording (test-recording.sh's encoder settings), middle frame decoded to RGB."""
    mp4 = out_dir / f"{name}.mp4"
    args = ["-loop", "1", "-framerate", "30", "-i", str(src), "-t", "1"]
    if vf:
        args += ["-vf", vf]
    ff(*args, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-crf", "16", str(mp4))
    ff("-ss", "0.5", "-i", str(mp4), "-frames:v", "1", str(out_dir / f"{name}.png"))
    size = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream=width,height", "-of", "csv=p=0", str(mp4)], capture_output=True, text=True).stdout.strip()
    w, h = (int(v) for v in size.split(","))
    return (w, h), ff("-ss", "0.5", "-i", str(mp4), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-")


def analyse(buf, W, H):
    def px(x, y):
        o = (y * W + x) * 3
        return buf[o], buf[o + 1], buf[o + 2]
    red = {"tl": False, "tr": False, "bl": False, "br": False}
    gx0 = gy0 = 10 ** 9
    gx1 = gy1 = -1
    for y in range(0, H, 2):
        for x in range(0, W, 2):
            r, g, b = px(x, y)
            if r > 190 and g < 80 and b < 80:
                red[("t" if y < H // 2 else "b") + ("l" if x < W // 2 else "r")] = True
            if g > 190 and r < 90 and b < 90:
                gx0, gy0, gx1, gy1 = min(gx0, x), min(gy0, y), max(gx1, x), max(gy1, y)

    def zoom_grey_rows(rows):
        n = 0
        for y in rows:
            hits = sum(1 for x in range(0, W, 8) if all(abs(a - b) <= 6 for a, b in zip(px(x, y), (42, 43, 45))))
            n += hits > (W // 8) * 0.6
        return n
    return {"red": red, "green": (gx1 - gx0 + 2, gy1 - gy0 + 2),
            "zoom_bar_rows_top": zoom_grey_rows(range(0, min(40, H))),
            "zoom_bar_rows_bottom": zoom_grey_rows(range(max(0, H - 6), H))}


CASES = [
    # name, screen W, H, picture ratio inside Zoom's area, background
    ("webinar_today_dark", 1920, 1080, None, "dark"),
    ("webinar_today_blur", 1920, 1080, None, "blur"),
    ("webinar_4x3_share", 1920, 1080, (4, 3), "dark"),
    ("tall_display_native", 1928, 1225, None, "dark"),
]


@pytest.mark.parametrize("name,W,H,ratio,bg", CASES)
def test_clean_broadcast_output_has_no_zoom_ui_keeps_every_corner_and_is_unstretched(out_dir, name, W, H, ratio, bg):
    src = screen(out_dir, name, W, H, ratio)
    lay = Z.detect_frames([raw(src, W, H)] * 2, W, H)
    assert lay["ok"], lay["reason"]
    p = C.plan((W, H), (1920, 1080), lay, background=bg)
    assert bash_accepts(p["filter"]) and p["crop"]["lost_pct"] == 0
    size, buf = record(out_dir, src, p["filter"], f"{name}_clean_broadcast")
    assert size == (1920, 1080)
    a = analyse(buf, 1920, 1080)
    assert all(a["red"].values()), f"lesson content cut: {a['red']}"
    gw, gh = a["green"]
    assert abs(gw - gh) <= 4, f"stretched: {gw}x{gh}"
    assert a["zoom_bar_rows_top"] == 0 and a["zoom_bar_rows_bottom"] == 0, "Zoom's title bar/toolbar is in the frame"
    if name == "tall_display_native":
        assert p["state"] == "clean" and p["filter"].startswith("crop=1920:1080:4:69,scale=out_color_matrix=bt709")
        assert abs(gw - 200) <= 4                                                                   # 1:1, no resample
    RESULTS[f"{name} / Clean Broadcast"] = {"state": p["state"], "filter": p["filter"], "bars": p["bars"],
                                            "corners_kept": sum(a["red"].values()), "green": a["green"],
                                            "zoom_ui_rows": a["zoom_bar_rows_top"] + a["zoom_bar_rows_bottom"]}


def test_old_modes_on_todays_webinar_screen_show_what_was_wrong(out_dir):
    src = screen(out_dir, "webinar_today_dark", 1920, 1080)
    # Meeting-only Fill (production before): fixed 56/96 trims + crop to cover 16:9
    size, buf = record(out_dir, src, preview.meeting_only_filter(1920, 1080, "fill", 56, 96), "webinar_today_meeting_only_fill")
    fill = analyse(buf, 1920, 1080)
    assert not any(fill["red"].values())                 # every picture corner cut away
    assert fill["zoom_bar_rows_top"] > 0                 # and part of Zoom's title bar is still there (69 px bar, 56 px trim)
    assert fill["green"][0] > 225                        # zoomed in ~1.16x
    # Fullscreen Live (first version): saw no black, so "native" = the whole screen, Zoom UI included
    measured = {"ok": True, "region": None}
    fsl = F.plan((1920, 1080), (1920, 1080), (1920, 1080), None, measured)
    size, buf = record(out_dir, src, fsl["filter"], "webinar_today_fullscreen_live")
    fl = analyse(buf, 1920, 1080)
    assert fsl["filter"] == "" and fl["zoom_bar_rows_top"] > 0 and all(fl["red"].values())
    RESULTS["webinar_today / Meeting-only Fill"] = {"corners_kept": sum(fill["red"].values()), "green": fill["green"],
                                                     "zoom_ui_rows": fill["zoom_bar_rows_top"] + fill["zoom_bar_rows_bottom"]}
    RESULTS["webinar_today / Fullscreen Live v1"] = {"corners_kept": sum(fl["red"].values()), "green": fl["green"],
                                                      "zoom_ui_rows": fl["zoom_bar_rows_top"] + fl["zoom_bar_rows_bottom"]}
