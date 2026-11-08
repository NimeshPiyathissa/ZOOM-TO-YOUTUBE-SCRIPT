"""Zoom UI detection (app/zoom_layout.py) on real Zoom chrome.

Fixtures (tests/fixtures/zoom/*.png) are production screenshots of the Zoom 7.2.1 Linux webinar window, 2026-11-08,
anonymised: Zoom's bars, frame lines and panels are pixel-exact; the title text is scrambled (per-row colour statistics
unchanged); the meeting picture is synthetic (red corner markers, green square, strokes touching every edge, and in
webinar_dark_edge a dark shirt touching the right edge). Synthetic screens (native camera, black screen, letterbox, a
taller or smaller display, a dark slide) are composed with ffmpeg from the same chrome."""
import asyncio
import pathlib
import shutil
import subprocess

import pytest

from app import zoom_layout as Z

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg required to decode the fixtures")
FIX = pathlib.Path(__file__).resolve().parent / "fixtures" / "zoom"
UI = {"top": 69, "bottom": 76, "left": 4, "right": 4}


def ff(*args, out=None):
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr.decode(errors="replace")[-1500:]
    return r.stdout


def rgb(path, w, h):
    data = ff("-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-")
    assert len(data) == w * h * 3
    return data


def detect(path, w=1920, h=1080):
    return Z.detect(Z.Frame(rgb(path, w, h), w, h))


@pytest.mark.parametrize("name", ["webinar_speaker", "webinar_long_title"])
def test_webinar_title_bar_toolbar_and_frame_are_found_exactly(name):
    r = detect(FIX / f"{name}.png")
    assert r["ok"] and r["bands"] == UI and r["content"] == (4, 69, 1912, 935)
    assert set(r["kinds"].values()) == {"zoom_ui"}


def test_open_settings_panel_is_removed_with_the_bars():
    r = detect(FIX / "webinar_settings_panel.png")
    assert r["ok"] and r["bands"] == {"top": 69, "bottom": 76, "left": 4, "right": 445}
    assert r["content"] == (4, 69, 1471, 935)


def test_dark_shirt_at_the_picture_edge_is_never_taken_for_zoom_ui():
    """Regression (found on a real class screenshot): the teacher's dark shirt touching the right edge of the video looks
    like a Zoom UI line (dark, one dominant colour close to Zoom's frame colour). Bands must end on a flat line, so the
    shirt stays in the picture."""
    data = rgb(FIX / "webinar_dark_edge.png", 1920, 1080)
    f = Z.Frame(data, 1920, 1080)
    trap = 0
    for i in range(4, 250):                         # the fixture really contains the trap...
        modal, cov = Z._stats(f.col(1919 - i, 69, 1004))
        trap += Z.luma(modal) <= Z.CHROME_LUMA_MAX and Z.UI_COVERAGE <= cov < Z.FLAT_COVERAGE and Z._close(modal, (19, 22, 25))
    assert trap > 200
    r = Z.detect(f)                                 # ...and the detector does not fall into it
    assert r["ok"] and r["bands"] == UI


def test_statistics_panel_whose_cards_stop_the_scan_is_reported_uncertain():
    r = detect(FIX / "webinar_statistics_panel.png")
    assert not r["ok"] and "right" in r["panel_suspected"] and "panel" in r["reason"]


def _compose(tmp_path, name, W, H, picture, chrome_src=FIX / "webinar_speaker.png", ui=True, inner=None):
    """A W x H screen: optional Zoom chrome (title bar = rows 0-68, toolbar = last 76 rows, 4 px sides, cut from the
    real fixture and stretched only along the bar) around `picture` (an ffmpeg lavfi source)."""
    out = tmp_path / f"{name}.png"
    if not ui:
        ff("-f", "lavfi", "-i", picture.format(s=f"{W}x{H}"), "-frames:v", "1", "-pix_fmt", "rgb24", str(out))
        return out
    cw, ch = W - 8, H - 145
    pic = picture.format(s=f"{cw}x{ch}")
    ov = "overlay=format=gbrp:x"                      # composite in RGB: no chroma blur across the bar edges
    graph = (f"[0:v]format=gbrp,split=4[s0][s1][s2][s3];[s0]crop=1920:69:0:0,scale={W}:69:flags=neighbor[top];"
             f"[s1]crop=1920:76:0:1004,scale={W}:76:flags=neighbor[bot];"
             f"[s2]crop=4:935:0:69,scale=4:{ch}:flags=neighbor[l];[s3]crop=4:935:1916:69,scale=4:{ch}:flags=neighbor[r];"
             f"[1:v]format=gbrp[pic];color=c=black:s={W}x{H},format=gbrp[bg];[bg][top]{ov}=0:y=0[a];"
             f"[a][bot]{ov}=0:y={H - 76}[b];[b][l]{ov}=0:y=69[c];[c][r]{ov}={W - 4}:y=69[d];[d][pic]{ov}=4:y=69,format=rgb24")
    ff("-i", str(chrome_src), "-f", "lavfi", "-i", pic, "-filter_complex", graph, "-frames:v", "1", str(out))
    return out


WHITEBOARD = "color=c=0xe8e4ec:s={s},drawgrid=w=64:h=64:t=3:c=0x203070"


def test_camera_filling_the_whole_screen_has_no_bands(tmp_path):
    r = detect(_compose(tmp_path, "native", 1920, 1080, WHITEBOARD, ui=False))
    assert r["ok"] and r["bands"] == {"top": 0, "bottom": 0, "left": 0, "right": 0}
    assert set(r["kinds"].values()) == {"none"} and r["content"] == (0, 0, 1920, 1080)


def test_black_screen_is_not_trusted(tmp_path):
    r = detect(_compose(tmp_path, "black", 1920, 1080, "color=c=black:s={s}", ui=False))
    assert not r["ok"] and "Nothing is removed" in r["reason"]


def test_plain_black_letterbox_is_reported_as_black_not_zoom_ui(tmp_path):
    src = _compose(tmp_path, "lb_pic", 1600, 900, WHITEBOARD, ui=False)
    out = tmp_path / "lb.png"
    ff("-i", str(src), "-vf", "format=gbrp,pad=1920:1080:160:90:black,format=rgb24", "-frames:v", "1", str(out))
    r = detect(out)
    assert r["ok"] and r["bands"] == {"top": 90, "bottom": 90, "left": 160, "right": 160}
    assert set(r["kinds"].values()) == {"black"}


@pytest.mark.parametrize("W,H", [(1928, 1225), (1280, 720), (2560, 1440)])
def test_other_window_sizes_measure_the_same_chrome(tmp_path, W, H):
    r = detect(_compose(tmp_path, f"size{W}", W, H, WHITEBOARD), W, H)
    assert r["ok"] and r["bands"] == UI and r["content"] == (4, 69, W - 8, H - 145)


def test_tall_display_gives_zoom_an_exact_1920x1080_picture(tmp_path):
    r = detect(_compose(tmp_path, "tall", 1928, 1225, WHITEBOARD), 1928, 1225)
    assert r["content"] == (4, 69, 1920, 1080)


def test_dark_slide_share_keeps_every_visible_stroke(tmp_path):
    """A dark-theme slide: black background with light text, including text right at its edges. Only flat black
    margins may go - never a column or row that carries text."""
    slide = ("color=c=black:s={s},drawbox=x=0:y=200:w=300:h=8:c=white:t=fill,drawbox=x=iw-300:y=400:w=300:h=8:c=white:t=fill,"
             "drawbox=x=600:y=0:w=8:h=120:c=white:t=fill,drawbox=x=900:y=ih-120:w=8:h=120:c=white:t=fill")
    r = detect(_compose(tmp_path, "darkslide", 1920, 1080, slide))
    if r["ok"]:
        x, y, w, h = r["content"]
        assert x <= 4 and y <= 69 and x + w >= 1916 and y + h >= 1004, r   # the edge strokes are all inside


def test_frames_that_disagree_keep_the_larger_picture_and_are_not_trusted():
    a = {"ok": True, "size": "1920x1080", "bands": dict(UI), "kinds": {k: "zoom_ui" for k in UI}}
    b = {"ok": True, "size": "1920x1080", "bands": dict(UI, right=445), "kinds": {k: "zoom_ui" for k in UI}}
    c = Z.combine([a, b])
    assert not c["ok"] and c["bands"]["right"] == 4 and "changed while measuring" in c["reason"]
    same = Z.combine([a, dict(a)])
    assert same["ok"] and same["content"] == (4, 69, 1912, 935) and same["frames"] == 2
    assert not Z.combine([])["ok"]
    assert not Z.combine([a, {"ok": False, "reason": "x", "size": "1920x1080", "bands": {}, "kinds": {}}])["ok"]


def test_grab_frames_reads_several_frames_through_ffmpeg():
    frames, w, h = asyncio.run(Z.grab_frames("1920x1080", 2, input_args=[
        "-loop", "1", "-framerate", "2", "-i", str(FIX / "webinar_speaker.png")]))
    assert (w, h) == (1920, 1080) and len(frames) == 2
    r = Z.detect_frames(frames, w, h)
    assert r["ok"] and r["bands"] == UI and r["frames"] == 2
