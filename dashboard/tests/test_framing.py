"""Measured meeting area (app/framing.py). The ffmpeg tests feed a SYNTHETIC picture (no Zoom meeting was involved):
they prove the detection rules and the safety limits, not how a real meeting looks."""
import asyncio
import shutil

import pytest

from app import framing, preview

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ------------------------------------------------------------------ pure decisions

def test_parse_cropdetect_takes_every_suggestion():
    text = "[Parsed_cropdetect_1 @ 0x1] x1:240 x2:1679 y1:0 y2:1079 w:1440 h:1080 x:240 y:0 pts:0 crop=1440:1080:240:0\n" \
           "[Parsed_cropdetect_1 @ 0x1] crop=1440:1080:240:0\nnoise"
    assert framing.parse_cropdetect(text) == [(1440, 1080, 240, 0), (1440, 1080, 240, 0)]
    assert framing.parse_cropdetect("") == []


def test_no_border_means_no_region():
    r = framing.decide((1920, 1080), 56, 96, (1920, 928, 0, 0))
    assert r["ok"] and r["region"] is None and "fills" in r["reason"]


def test_noise_sized_borders_are_ignored():
    assert framing.decide((1920, 1080), 56, 96, (1914, 924, 2, 2))["region"] is None


def test_pillarbox_is_measured_with_padding_and_full_height():
    r = framing.decide((1920, 1080), 56, 96, (1440, 928, 240, 0))
    assert r["ok"] and r["margins"] == {"left": 240, "top": 0, "right": 240, "bottom": 0}
    x, y, w, h = (int(v) for v in r["region"].split(":"))
    assert (x, w) == (238, 1444) and y == 56 and h == 928        # 2 px kept around the content, bands untouched


def test_mostly_black_screen_is_rejected_not_trusted():
    r = framing.decide((1920, 1080), 56, 96, (600, 300, 700, 300))
    assert not r["ok"] and r["region"] is None and "mostly black" in r["reason"]


def test_no_frame_is_reported():
    assert framing.decide((1920, 1080), 56, 96, None)["ok"] is False


def test_changed_materially():
    assert not framing.changed_materially("238:56:1444:928", "240:56:1440:928")          # within 16 px
    assert framing.changed_materially("238:56:1444:928", "0:56:1920:928")
    assert framing.changed_materially(None, "238:56:1444:928")
    assert not framing.changed_materially(None, None)
    assert framing.changed_materially("garbage", "1:2:3:4")


def test_region_flows_into_the_encoder_filter_without_distortion():
    region = framing.decide((1920, 1080), 56, 96, (1440, 928, 240, 0))["region"]
    fit = preview.meeting_only_filter(1920, 1080, "fit", 56, 96, region)
    assert fit.startswith("crop=1444:928:238:56,scale=1920:1080:force_original_aspect_ratio=decrease")
    fill = preview.meeting_only_filter(1920, 1080, "fill", 56, 96, region)
    assert fill.startswith("crop=1444:812:238:") and fill.endswith("setsar=1")      # 16:9 crop of the measured area
    # an implausible region falls back to the fixed trims instead of cropping real content
    assert preview.meeting_only_filter(1920, 1080, "fit", 56, 96, "0:0:100:100") == preview.meeting_only_filter(1920, 1080, "fit", 56, 96)


# ------------------------------------------------------------------ real ffmpeg on synthetic pictures

def synthetic(boxes):
    """A bright 1920x1080 picture with the given drawbox() black/grey areas, as ffmpeg input args."""
    vf = ",".join(boxes) if boxes else "null"
    return ["-f", "lavfi", "-i", f"testsrc2=s=1920x1080:r=4,{vf}"]


def box(x, y, w, h, color="black"):
    return f"drawbox=x={x}:y={y}:w={w}:h={h}:color={color}:t=fill"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_real_pillarbox_is_found():
    r = run(framing.measure("1920x1080", 56, 96, input_args=synthetic([box(0, 0, 240, 1080), box(1680, 0, 240, 1080)])))
    assert r["ok"] and r["region"] and r["samples"] >= 3
    x, y, w, h = (int(v) for v in r["region"].split(":"))
    assert 232 <= x <= 240 and 1440 <= w <= 1456 and y == 56 and h == 928


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_real_letterbox_inside_the_band_is_found():
    r = run(framing.measure("1920x1080", 56, 96, input_args=synthetic([box(0, 0, 1920, 200), box(0, 880, 1920, 200)])))
    assert r["ok"] and r["region"]
    x, y, w, h = (int(v) for v in r["region"].split(":"))
    assert x == 0 and w == 1920 and 190 <= y <= 200 and 676 <= h <= 692


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_real_picture_without_borders_gives_no_region():
    r = run(framing.measure("1920x1080", 56, 96, input_args=synthetic([])))
    assert r["ok"] and r["region"] is None


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_dark_grey_ui_is_not_mistaken_for_black():
    r = run(framing.measure("1920x1080", 56, 96, input_args=synthetic([box(0, 0, 300, 1080, "0x303030")])))
    assert r["ok"] and r["region"] is None          # luma 48 > limit 12: that is content, never cropped


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_all_black_screen_is_rejected():
    r = run(framing.measure("1920x1080", 56, 96, input_args=synthetic([box(0, 0, 1920, 1080)])))
    assert r["region"] is None


def test_missing_ffmpeg_is_reported_not_raised(monkeypatch):
    async def boom(*a, **k):
        raise FileNotFoundError("ffmpeg")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    r = run(framing.measure("1920x1080", 56, 96, input_args=["-i", "x"]))
    assert r["ok"] is False and r["region"] is None


# ------------------------------------------------------------------ API + watcher (framing.measure is stubbed)

import time

from starlette.testclient import TestClient

from app import config, control, db, security, sources
from app.main import app, _framing_watch_tick, _framing_pending

LINK = "https://us06web.zoom.us/j/81234567890?pwd=AbC123xyz"
REGION = "238:56:1444:928"
MEASURED = {"ok": True, "region": REGION, "size": "1920x1080", "samples": 6, "reason": "Black borders found",
            "margins": {"left": 240, "top": 0, "right": 240, "bottom": 0}}


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
    _framing_pending.update(region=None, count=0)
    written = []
    monkeypatch.setattr(control, "read_current_source", lambda: {})
    monkeypatch.setattr(control, "write_current_source", lambda s: written.append(s))
    monkeypatch.setattr(control, "zoom_set_meeting_only", lambda on: {"applied": True})
    monkeypatch.setattr(control, "encoder_filter_stale", lambda: False)
    box = {"m": dict(MEASURED)}

    async def fake_measure(*a, **k):
        return dict(box["m"])
    monkeypatch.setattr(framing, "measure", fake_measure)
    sid_ = sources.create_source("Town hall", "zoom", LINK, {"passcode": "1"})
    sources.set_active_source_id(sid_)
    c.sid, c.written, c.box = sid_, written, box
    return c


def opts(c):
    return sources.get_source(c.sid)["options"]


def test_turning_auto_on_measures_and_saves_the_region(api):
    r = api.post("/api/zoom/meeting-only", json={"enabled": True, "fit": "fill", "auto": True}, headers=api.csrf)
    assert r.status_code == 200 and r.json()["region"] == REGION and r.json()["auto"] is True
    assert opts(api)["meeting_only_auto"] is True and opts(api)["meeting_only_region"] == REGION
    assert api.written, "current-source.env must be rewritten so the encoder sees it"


def test_auto_off_and_reset_return_to_the_fixed_trims(api):
    api.post("/api/zoom/meeting-only", json={"enabled": True, "fit": "fill", "auto": True}, headers=api.csrf)
    r = api.post("/api/zoom/meeting-only", json={"enabled": True, "fit": "fill", "auto": False}, headers=api.csrf)
    assert r.json()["region"] == "" and opts(api)["meeting_only_region"] == "" and opts(api)["meeting_only_fit"] == "fill"
    api.post("/api/zoom/meeting-only", json={"enabled": True, "fit": "fill", "auto": True}, headers=api.csrf)
    r = api.post("/api/zoom/meeting-only", json={"enabled": True, "fit": "fill", "reset": True}, headers=api.csrf)
    o = opts(api)
    assert (o["meeting_only_fit"], o["meeting_only_auto"], o["meeting_only_region"]) == ("fit", False, "")


def test_region_reaches_the_encoder_env_only_while_auto_is_on(api):
    api.post("/api/zoom/meeting-only", json={"enabled": True, "fit": "fit", "auto": True}, headers=api.csrf)
    env = control._source_env_lines(sources.get_source(api.sid))
    assert env["ZOOM_MEETING_ONLY_REGION"] == REGION
    api.post("/api/zoom/meeting-only", json={"enabled": True, "fit": "fit", "auto": False}, headers=api.csrf)
    assert control._source_env_lines(sources.get_source(api.sid))["ZOOM_MEETING_ONLY_REGION"] == ""


def test_bad_region_is_rejected_at_save(api):
    with pytest.raises(Exception):
        sources.update_source(api.sid, "Town hall", "zoom", LINK, {"meeting_only_region": "../../etc"})


def test_framing_endpoint_is_read_only_and_reports_the_filter(api):
    api.post("/api/zoom/meeting-only", json={"enabled": True, "fit": "fit", "auto": True}, headers=api.csrf)
    before = dict(opts(api))
    d = api.get("/api/zoom/framing").json()
    assert d["measure"]["region"] == REGION and d["filter"].startswith("crop=1444:928:238:56,") and d["auto"] is True
    assert opts(api) == before                                                  # nothing changed
    assert TestClient(app).get("/api/zoom/framing").status_code == 401


def test_watcher_needs_two_consistent_changes_then_saves_for_the_next_start(api, monkeypatch):
    noted = []
    monkeypatch.setattr(control, "note_encoder_filter_changed", lambda: noted.append(1))
    api.post("/api/zoom/meeting-only", json={"enabled": True, "fit": "fit", "auto": True}, headers=api.csrf)
    noted.clear()                                                               # enabling Auto itself noted a change
    api.box["m"]["region"] = "0:56:1920:928"                                    # the borders went away
    run(_framing_watch_tick())
    assert opts(api)["meeting_only_region"] == REGION and not noted             # one sample is not enough
    run(_framing_watch_tick())
    assert opts(api)["meeting_only_region"] == "0:56:1920:928" and noted        # saved; the encoder is NOT restarted


def test_watcher_ignores_a_blank_screen_and_does_nothing_when_auto_is_off(api, monkeypatch):
    api.post("/api/zoom/meeting-only", json={"enabled": True, "fit": "fit", "auto": True}, headers=api.csrf)
    api.box["m"] = {"ok": False, "region": None, "reason": "mostly black", "size": "1920x1080", "samples": 3, "margins": {}}
    for _ in range(3):
        run(_framing_watch_tick())
    assert opts(api)["meeting_only_region"] == REGION                           # a dark screen never overwrites it
    api.post("/api/zoom/meeting-only", json={"enabled": True, "fit": "fit", "auto": False}, headers=api.csrf)
    calls = []
    async def spy(*a, **k):
        calls.append(1); return dict(MEASURED)
    monkeypatch.setattr(framing, "measure", spy)
    run(_framing_watch_tick())
    assert not calls                                                           # no measuring when Auto is off


# ---------------------------------------------------------------- Fill-crop warning
def test_fill_crop_matching_shape_loses_nothing():
    c = framing.fill_crop((1920, 1080), 56, 96, "0:56:1920:1080", (1920, 1080))
    assert c["lost_pct"] == 0.0


def test_fill_crop_wide_area_loses_sides_and_tall_area_loses_top_bottom():
    wide = framing.fill_crop((1920, 1080), 56, 96, "0:0:1920:800", (1920, 1080))     # 2.4:1 into 16:9
    assert wide["horizontal_pct"] > 25 and wide["vertical_pct"] == 0
    tall = framing.fill_crop((1920, 1080), 56, 96, "0:0:1200:1080", (1920, 1080))    # 1.11:1 into 16:9
    assert tall["vertical_pct"] > 30 and tall["horizontal_pct"] == 0


def test_fill_crop_default_band_and_bad_region_do_not_crash():
    assert framing.fill_crop((1920, 1080), 56, 96, None, (1920, 1080))["lost_pct"] >= 0
    assert framing.fill_crop((1920, 1080), 56, 96, "junk", (1920, 1080))["lost_pct"] >= 0
    assert framing.FILL_WARN_PCT > 0
