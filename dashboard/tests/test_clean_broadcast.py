"""Clean Broadcast (app/clean_broadcast.py, scripts/lib.sh clean_filter_ok / build_capture_framing,
/api/zoom/clean-broadcast). Planning rules, the bash filter check, the real stream.sh argv, and the API's guarantees:
Zoom UI is removed only where it was measured, the picture is never cropped without an explicit confirmation, never
stretched, the preview uses the encoder's exact string, and nothing here ever starts, stops or restarts the encoder."""
import asyncio
import os
import pathlib
import shutil
import subprocess
import time

import pytest
from starlette.testclient import TestClient

from app import clean_broadcast as C, config, control, db, env_store, preview, security, sources
import app.main as main_mod
from app.main import app

REPO = pathlib.Path(__file__).resolve().parents[2]
LIB = REPO / "scripts" / "lib.sh"
BASH = shutil.which("bash")
OUT = (1920, 1080)
KINDS = {"top": "zoom_ui", "bottom": "zoom_ui", "left": "zoom_ui", "right": "zoom_ui"}


def layout(bands=None, size=(1920, 1080), ok=True, kinds=None, reason="Zoom UI found"):
    b = bands or {"top": 69, "bottom": 76, "left": 4, "right": 4}
    W, H = size
    return {"ok": ok, "size": f"{W}x{H}", "bands": b, "kinds": kinds or dict(KINDS),
            "content": (b["left"], b["top"], W - b["left"] - b["right"], H - b["top"] - b["bottom"]), "reason": reason}


NO_UI = layout({"top": 0, "bottom": 0, "left": 0, "right": 0}, kinds={k: "none" for k in KINDS})


# ---------------------------------------------------------------- planning rules

def test_production_webinar_today_keeps_the_whole_picture_and_says_why_it_is_not_edge_to_edge():
    p = C.plan((1920, 1080), OUT, layout(), source="360p")
    assert p["state"] == "composed" and p["verified"]
    assert p["filter"] == "crop=1912:935:4:69,scale=1920:938:flags=lanczos,pad=1920:1080:0:71:color=0x0B0D10,setsar=1"
    assert p["bars"] == {"left": 0, "right": 0, "top": 71, "bottom": 71} and p["crop"]["lost_pct"] == 0
    codes = {w["code"]: w for w in p["warnings"]}
    assert codes["zoom_crops_camera"]["display_for_native"] == "1928x1225" and "13%" in codes["zoom_crops_camera"]["text"]
    assert "aspect" in codes and codes["low_source"]["text"].startswith("Zoom receives the teacher at 640x360")
    assert p["magnify"] == 3.0 and "no borders" not in p["label"].lower()


def test_tall_display_is_a_pure_crop_edge_to_edge_with_no_resampling():
    p = C.plan((1928, 1225), OUT, layout(size=(1928, 1225)))
    assert p["state"] == "clean" and p["filter"] == "crop=1920:1080:4:69" and p["scale"] == 1.0
    assert p["bars"] == {"left": 0, "right": 0, "top": 0, "bottom": 0} and "edge to edge" in p["label"]


def test_camera_filling_the_screen_needs_no_filter():
    p = C.plan((1920, 1080), OUT, NO_UI)
    assert p["state"] == "clean" and p["filter"] == "" and p["label"] == "Clean · edge to edge"


def test_unverified_measurement_never_crops_and_never_claims_clean():
    p = C.plan((1920, 1080), OUT, {"ok": False, "reason": "Zoom UI continues further in on the right side."})
    assert p["state"] == "unverified" and p["filter"] == "" and p["content"] == "0:0:1920:1080"
    assert "Clean" not in p["label"] and "toolbars may be visible" in p["warnings"][0]["text"]
    assert C.plan((1920, 1080), OUT, None)["state"] == "unverified"
    # a measurement from a different screen size is not trusted either
    assert C.plan((1928, 1225), OUT, layout())["state"] == "unverified"


def test_4x3_share_is_pillarboxed_never_cropped():
    b = {"top": 69, "bottom": 76, "left": 4 + 334, "right": 4 + 334}            # Zoom's black pillarbox inside the tile
    p = C.plan((1920, 1080), OUT, layout(b, kinds={"top": "zoom_ui", "bottom": "zoom_ui", "left": "zoom_ui", "right": "zoom_ui"}))
    assert p["content_size"] == "1244x935" and p["content_ratio"] == "4:3" and p["state"] == "composed"
    assert p["bars"]["top"] == 0 and 235 <= p["bars"]["left"] <= 245 and p["crop"]["lost_pct"] == 0


def test_fill_cuts_only_when_asked_and_reports_the_exact_loss():
    p = C.plan((1920, 1080), OUT, layout(), policy="fill")
    assert p["state"] == "cropped" and p["crop"]["horizontal_pct"] == 13.1 and p["filter"].startswith("crop=1662:935:129:69,")
    assert any(w["code"] == "fill_crops" and w["level"] == "danger" for w in p["warnings"])


def test_blur_background_keeps_the_picture_untouched_on_top():
    p = C.plan((1920, 1080), OUT, layout(), background="blur")
    f = p["filter"]
    assert f.startswith("crop=1912:935:4:69,split=2[cbfg][cbbg];") and "[cbfg]scale=1920:938:flags=lanczos[cbfront]" in f
    assert f.endswith("[cbback][cbfront]overlay=0:71,setsar=1") and C.safe_filter(f)


def test_every_emitted_filter_passes_the_safety_check():
    for size in ((1920, 1080), (1928, 1225), (1280, 720), (2560, 1440)):
        for pol in C.POLICIES:
            for bg in C.BACKGROUNDS:
                for lay in (layout(size=size), NO_UI if size == (1920, 1080) else layout(size=size), None):
                    assert C.safe_filter(C.plan(size, OUT, lay, policy=pol, background=bg)["filter"])
    for bad in ("movie=/etc/passwd", "crop=1:1;$(id)", "drawtext=text=x", "scale=1:1 -y", "crop=1:1/x", "zmq", "a\nb"):
        assert not C.safe_filter(bad)


def _bash(snippet, env=None):
    body = LIB.read_text(encoding="utf-8").replace("\r", "")
    funcs = []
    for name in ("clean_filter_ok", "build_meeting_only_filter", "build_fullscreen_live_filter", "build_capture_framing"):
        start = body.index(f"{name}() {{")
        funcs.append(body[start:body.index("\n}\n", start) + 3])
    return subprocess.run([BASH, "-c", "\n".join(funcs) + "\n" + snippet], capture_output=True, text=True,
                          env={**os.environ, **(env or {})}).stdout


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_bash_filter_check_agrees_with_python():
    cases = [C.plan((1920, 1080), OUT, layout(), background=bg)["filter"] for bg in C.BACKGROUNDS]
    cases += ["", "crop=1920:1080:4:69", "movie=/etc/passwd", "crop=1:1;$(id)", "drawtext=text=x", "scale=1:1 -y",
              "crop=1:1:0:0/x", "split[a][b];[a]sendcmd=f=x", "scale=2:2,FILE"]
    for vf in cases:
        sh = _bash('clean_filter_ok "$VF" && printf ok || printf bad', {"VF": vf})
        assert (sh == "ok") == C.safe_filter(vf), vf


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_capture_framing_prefers_clean_broadcast_and_falls_back_safely():
    snip = 'build_capture_framing 1920x1080; printf "%s|%s" "$CAPTURE_SIZE" "$CROP_FILTER"'
    vf = C.plan((1920, 1080), OUT, layout())["filter"]
    env = {"SOURCE_TYPE": "zoom", "ZOOM_CLEAN_BROADCAST": "1", "ZOOM_CLEAN_FILTER": vf, "ZOOM_CLEAN_CAPTURE": "1920x1080",
           "ZOOM_FULLSCREEN_LIVE": "1", "ZOOM_FRAMING_BOX": "160:90:1600:900", "ZOOM_CAPTURE_SIZE": "1920x1080",
           "ZOOM_MEETING_ONLY": "1", "ZOOM_MEETING_ONLY_FIT": "fill", "PATH": "/usr/bin:/bin"}
    assert _bash(snip, env) == f"1920x1080|{vf}"
    tall = C.plan((1928, 1225), OUT, layout(size=(1928, 1225)))["filter"]
    assert _bash(snip, {**env, "ZOOM_CLEAN_FILTER": tall, "ZOOM_CLEAN_CAPTURE": "1928x1225"}) == "1928x1225|crop=1920:1080:4:69"
    # a tampered filter is refused: the whole desktop is fitted instead (never a crop)
    assert _bash(snip, {**env, "ZOOM_CLEAN_FILTER": "movie=/etc/passwd"}) == "1920x1080|"
    assert _bash(snip, {**env, "ZOOM_CLEAN_FILTER": "crop=1:1", "ZOOM_CLEAN_CAPTURE": "1928x1225"}) .startswith("1928x1225|crop=1:1")
    assert _bash(snip, {**env, "ZOOM_CLEAN_BROADCAST": "0"}) == "1920x1080|crop=1600:900:160:90,scale=1920:1080:flags=lanczos,setsar=1"


# ---------------------------------------------------------------- real stream.sh argv (fake ffmpeg, fake X)

def _posix(p):
    p = pathlib.Path(p).resolve().as_posix()
    return "/" + p[0].lower() + p[2:] if len(p) > 1 and p[1] == ":" else p


def _run_stream(tmp_path, source_env, screen="1920x1080", script="stream.sh"):
    app_dir = tmp_path / "zs"
    (app_dir / "scripts").mkdir(parents=True)
    (app_dir / "bin").mkdir()
    (app_dir / "logs").mkdir()
    for n in ("lib.sh", "stream.sh", "test-recording.sh"):
        text = (REPO / "scripts" / n).read_text(encoding="utf-8").replace("\r\n", "\n")
        (app_dir / "scripts" / n).write_text(text.replace("/usr/bin/ffmpeg", _posix(app_dir / "fake-ffmpeg")), encoding="utf-8", newline="\n")
    (app_dir / ".env").write_text('YT_STREAM_KEY="abcd-efgh-ijkl"\nRESOLUTION="1920x1080"\nFPS="30"\n', encoding="utf-8", newline="\n")
    (app_dir / "fake-ffmpeg").write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > '{_posix(app_dir)}/argv'\nexit 0\n", encoding="utf-8", newline="\n")
    (app_dir / "bin" / "xdpyinfo").write_text(f"#!/usr/bin/env bash\necho 'dimensions:    {screen} pixels'\n", encoding="utf-8", newline="\n")
    for f in (app_dir / "fake-ffmpeg", app_dir / "bin" / "xdpyinfo"):
        os.chmod(f, 0o755)
    exp = int(time.time()) + 60
    body = "".join(f'{k}="{v}"\n' for k, v in source_env.items())
    (app_dir / "current-source.env").write_text(
        body + f'BROADCAST_AUTH_ID="g"\nBROADCAST_AUTH_NONCE="n0nce"\nBROADCAST_AUTH_EXPIRES="{exp}"\n', encoding="utf-8", newline="\n")
    env = dict(os.environ, APP_DIR=_posix(app_dir), HOME=_posix(app_dir), DISPLAY=":99", LOG_DIR=_posix(app_dir / "logs"),
               PATH=_posix(app_dir / "bin") + os.pathsep + os.environ.get("PATH", ""), TEST_DURATION="1")
    subprocess.run([BASH, _posix(app_dir / "scripts" / script)], capture_output=True, text=True, env=env, timeout=60)
    return (app_dir / "argv").read_text().split("\n")


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("script", ["stream.sh", "test-recording.sh"])
def test_stream_and_test_recording_apply_the_exact_planned_filter(tmp_path, script):
    vf = C.plan((1920, 1080), OUT, layout(), background="blur")["filter"]
    argv = _run_stream(tmp_path, {"SOURCE_TYPE": "zoom", "ZOOM_CLEAN_BROADCAST": "1", "ZOOM_CLEAN_FILTER": vf,
                                  "ZOOM_CLEAN_CAPTURE": "1920x1080"}, script=script)
    assert argv[argv.index("-video_size") + 1] == "1920x1080"
    assert argv[argv.index("-vf") + 1] == vf


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_stream_sh_refuses_a_stale_crop_after_the_screen_changed_size(tmp_path):
    vf = C.plan((1920, 1080), OUT, layout())["filter"]
    argv = _run_stream(tmp_path, {"SOURCE_TYPE": "zoom", "ZOOM_CLEAN_BROADCAST": "1", "ZOOM_CLEAN_FILTER": vf,
                                  "ZOOM_CLEAN_CAPTURE": "1920x1080"}, screen="1928x1225")
    assert argv[argv.index("-video_size") + 1] == "1928x1225"
    assert "crop=1912" not in "\n".join(argv)                       # never the old coordinates on the new screen


# ---------------------------------------------------------------- API

LINK = "https://us06web.zoom.us/j/81234567890?pwd=AbC123xyz"


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "c.db")
    db.init_db()
    with db.get_conn() as conn:
        uid = conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                           ("admin", security.hash_password("correct-horse-battery-staple"), time.time())).lastrowid
    sid, csrf = security.create_session(uid, "127.0.0.1")
    c = TestClient(app, cookies={config.COOKIE_NAME: sid})
    c.csrf = {"x-csrf-token": csrf}
    from app import deps
    deps._rate_buckets.clear()
    main_mod._cb_pending.update(content=None, count=0)
    st = {"phase": "STOPPED", "layout": layout(), "window": {"w": 1920, "h": 1080, "x": 0, "y": 0, "fullscreen": False},
          "window_calls": [], "unit_calls": [], "noted": [], "display": "1920x1080"}
    monkeypatch.setattr(main_mod, "_CB_SETTLE_SECONDS", 0)
    monkeypatch.setattr(control, "read_current_source", lambda: {})
    monkeypatch.setattr(control, "write_current_source", lambda s: None)
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
        return st["display"]
    monkeypatch.setattr(preview, "_display_size", disp)

    async def measure(display):
        return dict(st["layout"])
    monkeypatch.setattr(main_mod, "_cb_measure", measure)

    async def no_start(*a, **k):
        raise AssertionError("Clean Broadcast must never start or restart the encoder")
    monkeypatch.setattr(main_mod, "_start_encoder_authorized", no_start)
    s = sources.create_source("Class", "zoom", LINK, {"passcode": "1", "meeting_only": True, "meeting_only_fit": "fill"})
    sources.set_active_source_id(s)
    c.sid, c.st = s, st
    return c


def opts(c):
    return sources.get_source(c.sid)["options"]


def post(c, **body):
    return c.post("/api/zoom/clean-broadcast", json=body, headers=c.csrf)


def test_apply_removes_zoom_ui_keeps_picture_and_never_touches_the_encoder(api):
    d = post(api, action="apply").json()
    assert d["state"] == "composed" and d["applies"].startswith("Ready") and d["plan"]["verified"]
    o = opts(api)
    assert o["clean_broadcast"] is True and o["clean_capture"] == "1920x1080" and o["clean_filter"].startswith("crop=1912:935:4:69,")
    assert o["meeting_only"] is False and o["fullscreen_live"] is False                     # replaced, restorable
    assert o["clean_restore"]["meeting_only"] is True and o["clean_restore"]["meeting_only_fit"] == "fill"
    env = control._source_env_lines(sources.get_source(api.sid))
    assert env["ZOOM_CLEAN_BROADCAST"] == "1" and env["ZOOM_CLEAN_FILTER"] == o["clean_filter"] and env["ZOOM_MEETING_ONLY"] == "0"
    assert api.st["window_calls"] == [True] and api.st["unit_calls"] == [] and api.st["noted"]


def test_apply_while_live_is_pending_and_never_restarts(api):
    api.st["phase"] = "LIVE"
    d = post(api, action="apply").json()
    assert d["pending_restart"] is True and "never done automatically" in d["applies"] and api.st["unit_calls"] == []
    assert api.get("/api/zoom/clean-broadcast").json()["pending_restart"] is True


def test_settings_replan_from_the_saved_measurement_without_grabbing_again(api):
    post(api, action="apply")
    calls = []

    async def counting(display):
        calls.append(display)
        return layout()
    main_mod._cb_measure, saved = counting, main_mod._cb_measure
    try:
        d = post(api, action="settings", background="blur", source="720p").json()
    finally:
        main_mod._cb_measure = saved
    assert calls == [] and d["plan"]["background"] == "blur" and "[cbback]" in opts(api)["clean_filter"]
    assert opts(api)["clean_source"] == "720p" and d["prefs"] == {"policy": "preserve", "background": "blur", "source": "720p"}


def test_fill_needs_explicit_confirmation_with_the_loss_shown(api):
    post(api, action="apply")
    r = post(api, action="settings", policy="fill")
    assert r.status_code == 409 and "13.1%" in r.json()["detail"] and opts(api)["clean_policy"] == "preserve"
    d = post(api, action="settings", policy="fill", confirm_crop=True).json()
    assert d["state"] == "cropped" and opts(api)["clean_filter"].startswith("crop=1662:935:129:69,")


def test_uncertain_remeasure_keeps_the_last_verified_framing(api):
    post(api, action="apply")
    good = opts(api)["clean_filter"]
    api.st["layout"] = {"ok": False, "reason": "Zoom UI continues further in on the right side (an open panel?)."}
    d = post(api, action="apply").json()
    assert d["kept"] is True and opts(api)["clean_filter"] == good and "open panel" in d["message"]


def test_first_apply_on_an_unclear_screen_frames_everything_and_says_so(api):
    api.st["layout"] = {"ok": False, "reason": "Could not find where the picture starts."}
    d = post(api, action="apply").json()
    assert d["state"] == "unverified" and opts(api)["clean_filter"] == "" and "may be visible" in d["message"]


def test_restore_puts_back_the_previous_framing(api):
    post(api, action="apply")
    api.st["window_calls"].clear()
    d = post(api, action="restore").json()
    o = opts(api)
    assert not o["clean_broadcast"] and o["meeting_only"] is True and o["meeting_only_fit"] == "fill" and o["clean_filter"] == ""
    assert api.st["window_calls"] == [] and d["message"].startswith("Original framing restored")
    assert control._source_env_lines(sources.get_source(api.sid))["ZOOM_CLEAN_BROADCAST"] == "0"


def test_meeting_only_or_fullscreen_live_by_hand_end_clean_broadcast(api):
    post(api, action="apply")
    api.post("/api/zoom/meeting-only", json={"enabled": True, "fit": "fit"}, headers=api.csrf)
    assert opts(api)["clean_broadcast"] is False and opts(api)["clean_filter"] == ""


def test_non_zoom_source_auth_csrf_and_bad_action(api):
    assert TestClient(app).post("/api/zoom/clean-broadcast", json={"action": "apply"}).status_code in (401, 403)
    assert api.post("/api/zoom/clean-broadcast", json={"action": "apply"}).status_code == 403
    assert post(api, action="golive").status_code == 400
    w = sources.create_source("Page", "webpage", "https://example.com/", {})
    sources.set_active_source_id(w)
    r = post(api, action="apply")
    assert r.status_code == 400 and "Zoom" in r.json()["detail"]
    assert api.get("/api/zoom/clean-broadcast").json()["zoom_source"] is False


def test_live_studio_preview_uses_the_encoders_exact_string(monkeypatch):
    vf = C.plan((1920, 1080), OUT, layout(), background="blur")["filter"]
    src = {"SOURCE_TYPE": "zoom", "ZOOM_CLEAN_BROADCAST": "1", "ZOOM_CLEAN_FILTER": vf, "ZOOM_CLEAN_CAPTURE": "1920x1080",
           "ZOOM_FULLSCREEN_LIVE": "1", "ZOOM_FRAMING_BOX": "160:90:1600:900"}
    monkeypatch.setattr(control, "read_current_source", lambda: src)
    assert asyncio.run(preview.PreviewManager()._meeting_only("1920x1080")) == vf
    assert asyncio.run(preview.PreviewManager()._meeting_only("1928x1225")) == ""     # screen changed: show it whole


def test_watcher_replans_only_after_two_confident_sightings_and_never_restarts(api):
    post(api, action="apply")
    api.st["layout"] = layout({"top": 69, "bottom": 76, "left": 4, "right": 445})       # a panel opened
    run = asyncio.new_event_loop().run_until_complete
    run(main_mod._framing_watch_tick())
    assert opts(api)["clean_plan"]["content"] == "4:69:1912:935"
    run(main_mod._framing_watch_tick())
    assert opts(api)["clean_plan"]["content"] == "4:69:1471:935" and api.st["unit_calls"] == []
    api.st["layout"] = {"ok": False, "reason": "dark"}                                   # unsure: nothing changes
    run(main_mod._framing_watch_tick()); run(main_mod._framing_watch_tick())
    assert opts(api)["clean_plan"]["content"] == "4:69:1471:935"


def test_watcher_never_starts_cutting_the_picture_on_its_own(api):
    post(api, action="apply")
    o = dict(opts(api)); o["clean_policy"] = "fill"                                      # stale pref, never confirmed
    sources.update_source(api.sid, "Class", "zoom", LINK, o)
    api.st["layout"] = layout({"top": 69, "bottom": 76, "left": 4, "right": 445})
    run = asyncio.new_event_loop().run_until_complete
    run(main_mod._framing_watch_tick()); run(main_mod._framing_watch_tick())
    assert opts(api)["clean_plan"]["crop"]["lost_pct"] == 0


def test_controls_are_on_remote_gui_and_live_studio_and_never_go_live(api):
    vnc, studio = api.get("/vnc").text, api.get("/studio").text
    for html in (vnc, studio):
        assert "data-cb-apply" in html and "data-cb-restore" in html and "clean-broadcast.js" in html
        assert "data-fsl-apply" not in html                                              # replaced, not doubled
    for marker in ("data-cb-policy", "data-cb-background", "data-cb-source", "data-cb-output", "data-cb-screen",
                   'data-cb-fact="cpu"', "Preserve full content", "Fill 16:9", "Fit 16:9 background"):
        assert marker in studio
    src = (REPO / "dashboard" / "static" / "js" / "clean-broadcast.js").read_text(encoding="utf-8")
    for forbidden in ("/api/stream", "/api/broadcast", "go_live", "goLive", "grant"):
        assert forbidden not in src
