"""Output test kit as tests: every mock Zoom scene through the real detector, planner, encoder filter check and an H.264
recording, then the decoded output is checked (see outputkit.verdict). The CLI tests/output/run_kit.py runs the same
cases and keeps recordings + before/after sheets for a human look."""
import pathlib
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import outputkit as K  # noqa: E402

pytestmark = pytest.mark.skipif(K.FFMPEG is None or K.FFPROBE is None or shutil.which("bash") is None,
                                reason="ffmpeg, ffprobe and bash required")
LIB = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "lib.sh"


@pytest.fixture(scope="module")
def kitdir(tmp_path_factory):
    return tmp_path_factory.mktemp("outkit")


@pytest.fixture(scope="module")
def scenes(kitdir):
    return {}


def scene(scenes, kitdir, name):
    if name not in scenes:
        scenes[name] = K.make_scene(name, kitdir)
    return scenes[name]


def bash_accepts(vf):
    body = LIB.read_text(encoding="utf-8").replace("\r", "")
    start = body.index("clean_filter_ok() {")
    func = body[start:body.index("\n}\n", start) + 3]
    return subprocess.run(["bash", "-c", func + '\nclean_filter_ok "$VF" && printf ok'], capture_output=True, text=True,
                          env={"VF": vf, "PATH": "/usr/bin:/bin"}).stdout == "ok"


@pytest.mark.parametrize("name", list(K.SCENES))
def test_auto_keeps_every_corner_no_ui_no_black_bars_round_circles_sharp_text(scenes, kitdir, name):
    sc = scene(scenes, kitdir, name)
    r = K.run_case(sc, "preserve", kitdir)
    assert bash_accepts(r["filter"]), r["filter"]
    assert r["layout_ok"], r
    v = K.verdict(r)
    assert v["pass"], (r["case"], {k: x for k, x in v["checks"].items() if not x}, r)
    # the detected picture is where the scene put it (Zoom bars + Zoom's own black/dark padding removed, nothing else)
    assert all(abs(a - b) <= 2 for a, b in zip(r["detected"], sc["picture_bbox"])), (r["detected"], sc["picture_bbox"])
    if name in ("meeting_16x9", "letterboxed_video", "native_1928x1225", "web_client_fullscreen"):
        assert r["resampled"] is False and r["scale"] == 1.0                     # near-native: 1:1 pixels, no resample


def test_fit_shows_the_whole_screen_and_fill_reports_its_crop(scenes, kitdir):
    sc = scene(scenes, kitdir, "meeting_16x9")
    fit = K.run_case(sc, "fit", kitdir)
    assert fit["state"] == "fit" and all(fit["corners_kept"].values()) and fit["zoom_ui_rows"] > 0   # UI kept, as labelled
    fill = K.run_case(sc, "fill", kitdir)
    assert fill["state"] == "cropped" and fill["crop_pct"] > 10 and not all(fill["corners_kept"].values())


def test_the_previous_x1_004_resample_was_softer_than_1_to_1(scenes, kitdir):
    """The old Clean Broadcast scaled the 1912x935 picture by 1.004: almost nothing, but every pixel resampled."""
    sc = scene(scenes, kitdir, "meeting_16x9")
    new = K.run_case(sc, "preserve", kitdir)
    old_vf = "crop=1912:935:4:69,scale=1920:938:flags=lanczos,pad=1920:1080:0:71:color=0x0B0D10,setsar=1"
    K.run_case(sc, "custom", kitdir, vf_override=old_vf, label="meeting_16x9__old_resample")
    pic = kitdir / "meeting_16x9_pic0.png"
    src = K.ff("-i", str(pic), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-", capture=True)
    s = K.laplacian_var(src, 1912, (60, 70, 760, 150))
    old = K.frame_rgb(str(kitdir / "meeting_16x9__old_resample.mp4"), 1.0, (1920, 1080))
    old_sharp = K.laplacian_var(old, 1920, (60, 141, 762, 150)) / s
    assert new["text_sharpness"] > 0.95 and old_sharp < 0.85 and new["text_sharpness"] - old_sharp > 0.15
