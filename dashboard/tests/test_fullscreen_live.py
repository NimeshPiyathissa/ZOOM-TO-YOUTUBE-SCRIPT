"""Fullscreen Live (app/fullscreen_live.py, scripts/lib.sh build_fullscreen_live_filter / build_capture_framing,
/api/zoom/fullscreen-live). Pure planning rules, the bash twin, the real stream.sh argv, and the API's guarantees:
the box always contains every content pixel, the output is never stretched, and nothing here ever starts, stops or
restarts the encoder."""
import asyncio
import os
import pathlib
import random
import shutil
import subprocess
import time

import pytest
from starlette.testclient import TestClient

from app import config, control, db, env_store, framing, fullscreen_live as F, preview, security, sources
import app.main as main_mod
from app.main import app

REPO = pathlib.Path(__file__).resolve().parents[2]
LIB = REPO / "scripts" / "lib.sh"
BASH = shutil.which("bash")
WIN = {"w": 1920, "h": 1080, "x": 0, "y": 0, "fullscreen": True, "display": "1920x1080"}
D, OUT = (1920, 1080), (1920, 1080)


def plan(region=None, ok=True, window=WIN, display=D, capture=D, output=OUT):
    return F.plan(display, capture, output, window, {"ok": ok, "region": region, "margins": {}})


# ---------------------------------------------------------------- planning rules

def test_meeting_filling_the_screen_is_native_with_no_filter():
    p = plan()
    assert p["state"] == "native" and p["box"] == "0:0:1920:1080" and p["filter"] == "" and not p["issues"]
    assert p["bars"] == {"left": 0, "right": 0, "top": 0, "bottom": 0}


def test_letterboxed_16x9_video_loses_only_black():
    p = plan("160:90:1600:900")
    assert p["state"] == "optimized" and p["box"] == "160:90:1600:900" and p["scale"] == 1.2
    assert p["filter"] == "crop=1600:900:160:90,scale=1920:1080:flags=lanczos,setsar=1"
    assert max(p["bars"].values()) == 0 and not p["limitations"]


def test_4x3_share_is_kept_whole_and_the_limitation_explained():
    p = plan("240:0:1440:1080")
    assert p["state"] == "limited" and p["box"] == "0:0:1920:1080" and p["filter"] == ""
    assert p["bars"]["left"] == 240 and p["bars"]["right"] == 240
    assert "4:3" in p["limitations"][0] and "no cropping" in p["limitations"][0]


def test_portrait_share_is_never_cropped_to_fill():
    p = plan("656:0:608:1080")
    assert p["box"] == "0:0:1920:1080" and p["state"] == "limited" and p["bars"]["left"] == 656


def test_small_content_is_enlarged_at_most_1_5x():
    p = plan("560:316:800:450")
    x, y, w, h = (int(v) for v in p["box"].split(":"))
    assert (w, h) == (1280, 720) and p["scale"] == 1.5
    assert x <= 560 and y <= 316 and x + w >= 1360 and y + h >= 766
    assert any("small on screen" in lim for lim in p["limitations"])


def test_unmeasurable_screen_removes_nothing():
    p = plan(ok=False)
    assert p["box"] == "0:0:1920:1080" and p["filter"] == "" and p["state"] == "limited"


def test_mismatched_capture_and_window_are_detected_and_capture_corrected():
    p = plan(capture=(1280, 720), window={**WIN, "w": 1600, "h": 900, "fullscreen": False})
    codes = [i["code"] for i in p["issues"]]
    assert codes == ["capture_mismatch", "window_geometry"]
    assert p["capture_size"] == "1920x1080"                     # corrected: capture the whole desktop
    assert plan(window=None)["issues"][0]["code"] == "window_unknown"


def test_non_16x9_desktop_is_padded_not_stretched():
    p = F.plan((1280, 1024), (1920, 1080), (1920, 1080), {**WIN, "w": 1280, "h": 1024}, {"ok": True, "region": None})
    assert p["box"] == "0:0:1280:1024" and p["state"] == "limited"
    assert "force_original_aspect_ratio=decrease" in p["filter"] and "pad=1920:1080" in p["filter"]


def test_lower_output_resolution_scales_the_same_box():
    p = plan("160:90:1600:900", output=(1280, 720))
    assert p["filter"] == "crop=1600:900:160:90,scale=1280:720:flags=lanczos,setsar=1"


@pytest.mark.parametrize("seed", range(400))
def test_invariants_box_contains_all_content_exact_16x9_inside_screen(seed):
    rnd = random.Random(seed)
    W, H = rnd.choice([(1920, 1080), (1280, 720), (2560, 1440), (1366, 768)])
    w = rnd.randint(W // 3, W)
    h = rnd.randint(H // 3, H)
    x, y = rnd.randint(0, W - w), rnd.randint(0, H - h)
    p = F.plan((W, H), (W, H), (1920, 1080), {**WIN, "w": W, "h": H}, {"ok": True, "region": f"{x}:{y}:{w}:{h}"})
    bx, by, bw, bh = (int(v) for v in p["box"].split(":"))
    assert bx <= x and by <= y and bx + bw >= x + w and by + bh >= y + h, "content cut"
    assert bx >= 0 and by >= 0 and bx + bw <= W and by + bh <= H, "box outside the screen"
    if (bx, by, bw, bh) != (0, 0, W, H):
        assert abs(bw / bh - 16 / 9) <= 16 / 9 / 1000, "would stretch (more than 0.1%)"
        assert bw - w <= 2 or bh - h <= 2 or bw == 1280, "box not hugging the content (1280 = the 1.5x upscale floor)"
    assert 1920 / bw <= F.MAX_UPSCALE + 1e-9 or bw == W


def test_filter_never_crops_with_an_unusable_box():
    for bad in ("", "garbage", "0:0:0:0", "100:100:1920:1080", "-1:0:10:10", None):
        assert F.fullscreen_filter((1920, 1080), (1920, 1080), bad) == ""


# ---------------------------------------------------------------- bash twin

GOLDEN = [((1920, 1080), (1920, 1080), "0:0:1920:1080"), ((1920, 1080), (1920, 1080), "160:90:1600:900"),
          ((1920, 1080), (1280, 720), "160:90:1600:900"), ((1280, 1024), (1920, 1080), "0:0:1280:1024"),
          ((1920, 1080), (1920, 1080), "0:0:1440:1080"), ((1920, 1080), (1920, 1080), "garbage"),
          ((1920, 1080), (1920, 1080), "100:100:1920:1080"), ((1920, 1080), (1920, 1080), ""),
          ((1280, 720), (1920, 1080), "0:0:1280:720"), ((2560, 1440), (1920, 1080), "320:180:1920:1080")]


def _bash(snippet, env=None):
    body = LIB.read_text(encoding="utf-8").replace("\r", "")
    funcs = []
    for name in ("build_meeting_only_filter", "build_fullscreen_live_filter", "build_capture_framing"):
        start = body.index(f"{name}() {{")
        funcs.append(body[start:body.index("\n}\n", start) + 3])
    script = "\n".join(funcs) + "\n" + snippet
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True, env={**os.environ, **(env or {})}).stdout


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("cap,out,box", GOLDEN)
def test_python_filter_matches_bash(cap, out, box):
    sh = _bash(f'build_fullscreen_live_filter {cap[0]} {cap[1]} {out[0]} {out[1]} "{box}"; printf %s "$FULLSCREEN_LIVE_FILTER"')
    assert sh == F.fullscreen_filter(cap, out, box)


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_capture_framing_picks_fullscreen_live_over_meeting_only_and_keeps_old_modes_identical():
    snip = 'build_capture_framing 1920x1080; printf "%s|%s" "$CAPTURE_SIZE" "$CROP_FILTER"'
    fsl = {"SOURCE_TYPE": "zoom", "ZOOM_FULLSCREEN_LIVE": "1", "ZOOM_FRAMING_BOX": "160:90:1600:900",
           "ZOOM_CAPTURE_SIZE": "1920x1080", "ZOOM_MEETING_ONLY": "1", "ZOOM_MEETING_ONLY_FIT": "fill"}
    assert _bash(snip, fsl) == "1920x1080|crop=1600:900:160:90,scale=1920:1080:flags=lanczos,setsar=1"
    # whole desktop captured even when the output is smaller (the old capture grabbed only the top-left 1280x720)
    assert _bash('build_capture_framing 1280x720; printf "%s|%s" "$CAPTURE_SIZE" "$CROP_FILTER"',
                 {**fsl, "ZOOM_FRAMING_BOX": "0:0:1920:1080"}) == "1920x1080|scale=1280:720:flags=lanczos,setsar=1"
    assert _bash(snip, {**fsl, "ZOOM_CAPTURE_SIZE": "1920x1080;rm"}) .startswith("1920x1080|crop=1600")   # junk size ignored
    mo = {**fsl, "ZOOM_FULLSCREEN_LIVE": "0"}
    assert _bash(snip, mo) == "1920x1080|" + preview.meeting_only_filter(1920, 1080, "fill", 56, 96)       # unchanged
    assert _bash(snip, {**mo, "ZOOM_MEETING_ONLY": "0"}) == "1920x1080|"
    assert _bash(snip, {**fsl, "SOURCE_TYPE": "webpage"}) == "1920x1080|"


# ---------------------------------------------------------------- real stream.sh argv (fake ffmpeg, fake X)

def _posix(p):
    p = pathlib.Path(p).resolve().as_posix()
    return "/" + p[0].lower() + p[2:] if len(p) > 1 and p[1] == ":" else p


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("fsl,expect_size,expect_vf", [
    (True, "1920x1080", "crop=1600:900:160:90,scale=1280:720:flags=lanczos,setsar=1"),
    (False, "1280x720", None),
])
def test_stream_sh_captures_whole_desktop_and_applies_the_box(tmp_path, fsl, expect_size, expect_vf):
    app_dir = tmp_path / "zs"
    (app_dir / "scripts").mkdir(parents=True)
    (app_dir / "bin").mkdir()
    for n in ("lib.sh", "stream.sh"):
        text = (REPO / "scripts" / n).read_text(encoding="utf-8").replace("\r\n", "\n")
        (app_dir / "scripts" / n).write_text(text.replace("/usr/bin/ffmpeg", _posix(app_dir / "fake-ffmpeg")), encoding="utf-8", newline="\n")
    (app_dir / ".env").write_text('YT_STREAM_KEY="abcd-efgh-ijkl"\nRESOLUTION="1280x720"\nFPS="30"\n', encoding="utf-8", newline="\n")
    (app_dir / "fake-ffmpeg").write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > '{_posix(app_dir)}/argv'\nexit 0\n", encoding="utf-8", newline="\n")
    (app_dir / "bin" / "xdpyinfo").write_text("#!/usr/bin/env bash\necho 'dimensions:    1920x1080 pixels'\n", encoding="utf-8", newline="\n")
    for f in (app_dir / "fake-ffmpeg", app_dir / "bin" / "xdpyinfo"):
        os.chmod(f, 0o755)
    exp = int(time.time()) + 60
    (app_dir / "current-source.env").write_text(
        'SOURCE_TYPE="zoom"\nZOOM_MEETING_ONLY="0"\n'
        f'ZOOM_FULLSCREEN_LIVE="{1 if fsl else 0}"\nZOOM_FRAMING_BOX="160:90:1600:900"\nZOOM_CAPTURE_SIZE="1920x1080"\n'
        f'BROADCAST_AUTH_ID="g"\nBROADCAST_AUTH_NONCE="n0nce"\nBROADCAST_AUTH_EXPIRES="{exp}"\n', encoding="utf-8", newline="\n")
    env = dict(os.environ, APP_DIR=_posix(app_dir), HOME=_posix(app_dir), DISPLAY=":99",
               PATH=_posix(app_dir / "bin") + os.pathsep + os.environ.get("PATH", ""))
    subprocess.run([BASH, _posix(app_dir / "scripts" / "stream.sh")], capture_output=True, text=True, env=env, timeout=60)
    argv = (app_dir / "argv").read_text().split("\n")
    assert argv[argv.index("-video_size") + 1] == expect_size
    if expect_vf:
        assert argv[argv.index("-vf") + 1] == expect_vf
    else:
        assert "-vf" not in argv


# ---------------------------------------------------------------- API

LINK = "https://us06web.zoom.us/j/81234567890?pwd=AbC123xyz"


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "f.db")
    db.init_db()
    with db.get_conn() as conn:
        uid = conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                           ("admin", security.hash_password("correct-horse-battery-staple"), time.time())).lastrowid
    sid, csrf = security.create_session(uid, "127.0.0.1")
    c = TestClient(app, cookies={config.COOKIE_NAME: sid})
    c.csrf = {"x-csrf-token": csrf}
    from app import deps
    deps._rate_buckets.clear()
    main_mod._fsl_pending.update(content=None, count=0)
    st = {"phase": "STOPPED", "window": dict(WIN, fullscreen=False), "measure": {"ok": True, "region": None, "margins": {}},
          "window_calls": [], "unit_calls": [], "noted": [], "written": [],
          "chrome": {"ok": True, "size": "1920x1080", "bands": {"top": 0, "bottom": 0, "left": 0, "right": 0},
                     "kinds": {"top": "none", "bottom": "none", "left": "none", "right": "none"},
                     "content": (0, 0, 1920, 1080), "reason": "No Zoom UI at the screen edges."}}
    monkeypatch.setattr(main_mod, "_FSL_SETTLE_SECONDS", 0)
    monkeypatch.setattr(control, "read_current_source", lambda: {})
    monkeypatch.setattr(control, "write_current_source", lambda s: st["written"].append(s))
    monkeypatch.setattr(control, "unit_show", lambda u: {"phase": st["phase"], "uptime_seconds": 10})
    monkeypatch.setattr(control, "unit_action", lambda *a, **k: st["unit_calls"].append(a))
    monkeypatch.setattr(control, "encoder_filter_stale", lambda: st["phase"] == "LIVE" and bool(st["noted"]))
    monkeypatch.setattr(control, "note_encoder_filter_changed", lambda: st["noted"].append(1))

    def set_window(on):
        st["window_calls"].append(on)
        st["window"]["fullscreen"] = on
        return {"applied": True, "enabled": on}
    monkeypatch.setattr(control, "zoom_set_meeting_only", set_window)
    monkeypatch.setattr(control, "zoom_window_geometry", lambda: dict(st["window"]))
    monkeypatch.setattr(env_store, "read_parsed", lambda: {"RESOLUTION": "1920x1080"})

    async def disp():
        return "1920x1080"
    monkeypatch.setattr(preview, "_display_size", disp)

    async def meas(*a, **k):
        return dict(st["measure"])
    monkeypatch.setattr(framing, "measure", meas)

    async def chrome(display):
        return dict(st["chrome"])
    monkeypatch.setattr(main_mod, "_cb_measure", chrome)

    async def no_start(*a, **k):
        raise AssertionError("Fullscreen Live must never start or restart the encoder")
    monkeypatch.setattr(main_mod, "_start_encoder_authorized", no_start)
    s = sources.create_source("Class", "zoom", LINK, {"passcode": "1", "meeting_only": True, "meeting_only_fit": "fill"})
    sources.set_active_source_id(s)
    c.sid, c.st = s, st
    return c


def opts(c):
    return sources.get_source(c.sid)["options"]


def test_apply_frames_natively_saves_for_next_start_and_never_touches_the_encoder(api):
    r = api.post("/api/zoom/fullscreen-live", json={"action": "apply"}, headers=api.csrf)
    d = r.json()
    assert r.status_code == 200 and d["state"] == "native" and d["applies"].startswith("Ready")
    o = opts(api)
    assert o["fullscreen_live"] is True and o["fullscreen_box"] == "0:0:1920:1080" and o["fullscreen_capture"] == "1920x1080"
    assert o["fullscreen_restore"] == {"meeting_only": True, "meeting_only_fit": "fill", "meeting_only_auto": False, "window_fullscreen": False}
    assert api.st["window_calls"] == [True] and api.st["unit_calls"] == []
    env = control._source_env_lines(sources.get_source(api.sid))
    assert env["ZOOM_FULLSCREEN_LIVE"] == "1" and env["ZOOM_FRAMING_BOX"] == "0:0:1920:1080" and env["ZOOM_CAPTURE_SIZE"] == "1920x1080"
    assert api.st["noted"]                                             # the framing changed vs Meeting-only Fill


def test_apply_while_live_reports_pending_and_does_not_restart(api):
    api.st["phase"] = "LIVE"
    d = api.post("/api/zoom/fullscreen-live", json={"action": "apply"}, headers=api.csrf).json()
    assert d["pending_restart"] is True and "never done automatically" in d["applies"]
    assert api.st["unit_calls"] == []
    assert api.get("/api/zoom/fullscreen-live").json()["pending_restart"] is True


def test_apply_with_4x3_content_explains_the_limit(api):
    api.st["measure"] = {"ok": True, "region": "240:0:1440:1080", "margins": {"left": 240}}
    d = api.post("/api/zoom/fullscreen-live", json={"action": "apply"}, headers=api.csrf).json()
    assert d["state"] == "limited" and "4:3" in d["message"] and d["plan"]["filter"] == ""


def test_restore_puts_back_the_original_framing_and_window(api):
    api.post("/api/zoom/fullscreen-live", json={"action": "apply"}, headers=api.csrf)
    api.st["window_calls"].clear()
    d = api.post("/api/zoom/fullscreen-live", json={"action": "restore"}, headers=api.csrf).json()
    o = opts(api)
    assert d["state"] == "original" and not o["fullscreen_live"] and o["meeting_only"] is True and o["meeting_only_fit"] == "fill"
    assert api.st["window_calls"] == []                               # Meeting-only still wants the fullscreen window
    env = control._source_env_lines(sources.get_source(api.sid))
    assert env["ZOOM_FULLSCREEN_LIVE"] == "0" and env["ZOOM_FRAMING_BOX"] == "" and env["ZOOM_MEETING_ONLY_FIT"] == "fill"
    assert api.st["unit_calls"] == []


def test_restore_turns_window_fullscreen_off_when_it_was_off_before(api):
    o = dict(opts(api)); o.update(meeting_only=False)
    sources.update_source(api.sid, "Class", "zoom", LINK, o)
    api.post("/api/zoom/fullscreen-live", json={"action": "apply"}, headers=api.csrf)
    api.post("/api/zoom/fullscreen-live", json={"action": "restore"}, headers=api.csrf)
    assert api.st["window_calls"] == [True, False]


def test_choosing_meeting_only_by_hand_ends_fullscreen_live(api):
    api.post("/api/zoom/fullscreen-live", json={"action": "apply"}, headers=api.csrf)
    api.post("/api/zoom/meeting-only", json={"enabled": True, "fit": "fit"}, headers=api.csrf)
    assert opts(api)["fullscreen_live"] is False and opts(api)["fullscreen_restore"] is None and opts(api)["fullscreen_box"] == ""


def test_a_normal_zoom_form_save_keeps_fullscreen_live(api):
    api.post("/api/zoom/fullscreen-live", json={"action": "apply"}, headers=api.csrf)
    sources.update_source(api.sid, "Class renamed", "zoom", LINK, {"passcode": "1", "view": "gallery"})
    o = opts(api)
    assert o["fullscreen_live"] is True and o["fullscreen_box"] == "0:0:1920:1080" and o["view"] == "gallery"


def test_non_zoom_source_is_refused_with_a_reason(api):
    w = sources.create_source("Page", "webpage", "https://example.com/", {})
    sources.set_active_source_id(w)
    r = api.post("/api/zoom/fullscreen-live", json={"action": "apply"}, headers=api.csrf)
    assert r.status_code == 400 and "Zoom" in r.json()["detail"]
    assert api.get("/api/zoom/fullscreen-live").json()["zoom_source"] is False


def test_auth_csrf_and_bad_action(api):
    assert TestClient(app).post("/api/zoom/fullscreen-live", json={"action": "apply"}).status_code in (401, 403)
    assert api.post("/api/zoom/fullscreen-live", json={"action": "apply"}).status_code == 403
    assert api.post("/api/zoom/fullscreen-live", json={"action": "golive"}, headers=api.csrf).status_code == 400


def test_preview_uses_the_same_box_as_the_encoder(monkeypatch):
    src = {"SOURCE_TYPE": "zoom", "ZOOM_FULLSCREEN_LIVE": "1", "ZOOM_FRAMING_BOX": "160:90:1600:900", "ZOOM_MEETING_ONLY": "1"}
    monkeypatch.setattr(control, "read_current_source", lambda: src)
    vf = asyncio.run(preview.PreviewManager()._meeting_only("1920x1080"))
    assert vf == F.fullscreen_filter((1920, 1080), (1920, 1080), "160:90:1600:900")


def test_watcher_only_grows_the_box_after_two_sightings_and_never_restarts(api):
    api.st["measure"] = {"ok": True, "region": "160:90:1600:900", "margins": {}}
    api.post("/api/zoom/fullscreen-live", json={"action": "apply"}, headers=api.csrf)
    assert opts(api)["fullscreen_box"] == "160:90:1600:900"
    run = asyncio.new_event_loop().run_until_complete
    api.st["measure"] = {"ok": True, "region": "0:90:1920:900", "margins": {}}     # a wider share appeared
    run(main_mod._framing_watch_tick())
    assert opts(api)["fullscreen_box"] == "160:90:1600:900"                         # once is not enough
    run(main_mod._framing_watch_tick())
    assert opts(api)["fullscreen_box"] == "0:0:1920:1080" and api.st["unit_calls"] == []
    api.st["measure"] = {"ok": True, "region": "400:300:800:450", "margins": {}}   # smaller content: never shrinks
    run(main_mod._framing_watch_tick()); run(main_mod._framing_watch_tick())
    assert opts(api)["fullscreen_box"] == "0:0:1920:1080"


def test_button_is_replaced_by_clean_broadcast_on_remote_gui_and_live_studio(api):
    """Clean Broadcast (tests/test_clean_broadcast.py) supersedes the Fullscreen Live button on both pages; the
    Fullscreen Live API stays for saved sources and reports honestly."""
    for path in ("/vnc", "/studio"):
        html = api.get(path).text
        assert "data-fsl-apply" not in html and "fullscreen-live.js" not in html
        assert "data-cb-apply" in html and "clean-broadcast.js" in html
    src = (REPO / "dashboard" / "static" / "js" / "fullscreen-live.js").read_text(encoding="utf-8")
    for forbidden in ("/api/stream", "/api/broadcast", "go_live", "grant"):
        assert forbidden not in src


# ---------------------------------------------------------------- the "Native 16:9 - no borders" bug (2026-11-08)

ZOOM_UI = {"ok": True, "size": "1920x1080", "bands": {"top": 69, "bottom": 76, "left": 4, "right": 4},
           "kinds": {"top": "zoom_ui", "bottom": "zoom_ui", "left": "zoom_ui", "right": "zoom_ui"},
           "content": (4, 69, 1912, 935), "reason": "Zoom UI found"}


def test_visible_zoom_toolbars_are_never_reported_as_native(api):
    """Production 2026-11-08: Zoom's grey title bar + toolbar were on screen, cropdetect saw no black, and the
    dashboard said "Native 16:9 - no borders". With the Zoom-UI check the same screen is 'limited' and says why."""
    api.st["chrome"] = dict(ZOOM_UI)
    d = api.post("/api/zoom/fullscreen-live", json={"action": "apply"}, headers=api.csrf).json()
    assert d["state"] == "limited" and d["plan"]["zoom_ui"] == {"top": 69, "bottom": 76, "left": 4, "right": 4}
    assert "Zoom's own controls are on screen" in d["message"] and "Clean Broadcast" in d["message"]


def test_unmeasurable_zoom_ui_is_not_native_either(api):
    api.st["chrome"] = {"ok": False, "reason": "blank screen"}
    d = api.post("/api/zoom/fullscreen-live", json={"action": "apply"}, headers=api.csrf).json()
    assert d["state"] == "limited" and "not verified" in d["message"]


def test_plan_saved_by_the_first_version_is_reported_unverified(api):
    o = dict(opts(api))
    o.update(fullscreen_live=True, fullscreen_box="0:0:1920:1080", fullscreen_capture="1920x1080",
             fullscreen_plan={"state": "native", "box": "0:0:1920:1080", "summary": "Native 1920x1080: ... no borders"})
    sources.update_source(api.sid, "Class", "zoom", LINK, o)
    plan = api.get("/api/zoom/fullscreen-live").json()["plan"]
    assert plan["state"] == "unverified" and "not verified" in plan["summary"]
