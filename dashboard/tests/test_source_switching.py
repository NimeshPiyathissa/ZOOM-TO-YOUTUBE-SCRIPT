"""Tests for app/control.py's source-switching logic (Change 1):
current-source.env content generation, and the stop/start sequencing
start_source() uses to swap producers. No real subprocess/sudo/systemd
calls are made - run_as_zoombot and unit_action are monkeypatched."""
import socket

import pytest

from app import control, config
from app.url_security import URLSecurityError


def _mock_public_dns(monkeypatch):
    def fake_getaddrinfo(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    monkeypatch.setattr(control.url_security.socket, "getaddrinfo", fake_getaddrinfo)


@pytest.fixture(autouse=True)
def _no_real_zoombot_calls(monkeypatch):
    """_source_env_lines() reads current-source.env (to carry watermark
    config forward across a source switch - see control.py) via
    run_as_zoombot; keep this file's "no real subprocess/sudo" invariant
    true by default. Empty result = no pre-existing watermark config,
    which is what every test here that doesn't care about it wants."""
    monkeypatch.setattr(control, "read_current_source", lambda: {})


# ---------------------------------------------------------------- current-source.env content

def test_source_env_lines_zoom_type_has_no_type_specific_keys():
    source = {"type": "zoom", "url": "https://zoom.us/j/123", "options": {}}
    values = control._source_env_lines(source)
    assert values["SOURCE_TYPE"] == "zoom"
    assert values["WEBPAGE_URL"] == ""
    assert values["DIRECT_URL"] == ""


def test_source_env_lines_webpage(monkeypatch):
    _mock_public_dns(monkeypatch)
    source = {
        "type": "webpage", "url": "https://meet.google.com/abc-defg-hij",
        "options": {"zoom_level": 1.5, "reload_seconds": 600, "click_to_start": True},
    }
    values = control._source_env_lines(source)
    assert values["SOURCE_TYPE"] == "webpage"
    assert values["WEBPAGE_URL"] == "https://meet.google.com/abc-defg-hij"
    assert values["WEBPAGE_ZOOM"] == "1.5"
    assert values["WEBPAGE_RELOAD_SECONDS"] == "600"
    assert values["WEBPAGE_CLICK_TO_START"] == "1"


def test_source_env_lines_direct(monkeypatch):
    _mock_public_dns(monkeypatch)
    source = {
        "type": "direct", "url": "https://example.com/stream.m3u8",
        "options": {"mode": "copy", "loop": True, "reconnect": False},
    }
    values = control._source_env_lines(source)
    assert values["SOURCE_TYPE"] == "direct"
    assert values["DIRECT_URL"] == "https://example.com/stream.m3u8"
    assert values["DIRECT_MODE"] == "copy"
    assert values["DIRECT_LOOP"] == "1"
    assert values["DIRECT_RECONNECT"] == "0"


def test_source_env_lines_revalidates_url_at_write_time(monkeypatch):
    """The TOCTOU gate: even if app/sources.py validated this URL when it
    was saved, _source_env_lines() (called immediately before
    current-source.env is written) must independently re-reject a URL
    that now resolves somewhere unsafe."""
    def fake_getaddrinfo(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]
    monkeypatch.setattr(control.url_security.socket, "getaddrinfo", fake_getaddrinfo)
    source = {"type": "webpage", "url": "https://rebound-domain.example/", "options": {}}
    with pytest.raises(URLSecurityError):
        control._source_env_lines(source)


# ---------------------------------------------------------------- start_source sequencing (Part 3: RTMP-preserving)

class _Recorder:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def unit_action(self, unit, verb):
        self.calls.append((unit, verb))
        return {"ok": True, "unit": unit, "verb": verb}


@pytest.fixture
def recorder(monkeypatch):
    """No real subprocess/sudo/systemd: unit actions are recorded, the
    slate is a no-op, and the two state reads (what's the current source
    type, is the encoder up) are pinned per test via `pin`."""
    rec = _Recorder()
    monkeypatch.setattr(control, "unit_action", rec.unit_action)
    monkeypatch.setattr(control, "write_current_source", lambda source: None)
    monkeypatch.setattr(control, "_set_slate", lambda: {"ok": True, "action": "slate"})
    monkeypatch.setattr(control.time, "sleep", lambda *_: None)
    monkeypatch.setattr("app.env_store.write_updates", lambda updates: [])

    def pin(current_type="", ffmpeg_up=False):
        monkeypatch.setattr(control, "read_current_source", lambda: {"SOURCE_TYPE": current_type} if current_type else {})
        monkeypatch.setattr(control, "_ffmpeg_is_up", lambda: ffmpeg_up)
    rec.pin = pin
    pin()
    return rec


ZOOM = {"type": "zoom", "url": "https://zoom.us/j/1234567890", "options": {"bot_name": "Bot", "signin_mode": "guest"}}
WEB = {"type": "webpage", "url": "https://meet.google.com/abc", "options": {}}
DIRECT = {"type": "direct", "url": "https://example.com/stream.m3u8", "options": {}}


def test_zoom_from_cold_starts_chain_then_encoder(recorder):
    recorder.pin(current_type="", ffmpeg_up=False)
    out = control.start_source(ZOOM)
    assert ("browser-source", "stop") in recorder.calls
    assert ("zoom", "stop") not in recorder.calls            # zoom is the wanted producer
    for u in ("xvfb", "openbox", "audio-setup", "x11vnc"):
        assert (u, "start") in recorder.calls
    assert ("zoom", "restart") in recorder.calls              # restart, so a same-type switch actually rejoins
    assert recorder.calls[-1] == ("ffmpeg-stream", "start")   # encoder was down: switching = going live
    assert ("ffmpeg-stream", "stop") not in recorder.calls
    assert out["rtmp_dropped"] is False and out["hot_swapped"] is False


def test_zoom_to_webpage_while_live_keeps_encoder_running(recorder):
    recorder.pin(current_type="zoom", ffmpeg_up=True)
    out = control.start_source(WEB)
    assert ("zoom", "stop") in recorder.calls
    assert ("browser-source", "restart") in recorder.calls
    # The whole point: ffmpeg is neither stopped nor (re)started.
    assert not [c for c in recorder.calls if c[0] == "ffmpeg-stream"]
    assert out["rtmp_dropped"] is False and out["hot_swapped"] is False


def test_webpage_to_zoom_while_live_keeps_encoder_running(recorder):
    recorder.pin(current_type="webpage", ffmpeg_up=True)
    out = control.start_source(ZOOM)
    assert ("browser-source", "stop") in recorder.calls
    assert ("zoom", "restart") in recorder.calls
    assert not [c for c in recorder.calls if c[0] == "ffmpeg-stream"]
    assert out["rtmp_dropped"] is False


def test_direct_while_live_restarts_encoder_and_reports_drop(recorder):
    recorder.pin(current_type="zoom", ffmpeg_up=True)
    out = control.start_source(DIRECT)
    assert ("ffmpeg-stream", "stop") in recorder.calls
    assert ("zoom", "stop") in recorder.calls and ("browser-source", "stop") in recorder.calls
    started = {u for u, v in recorder.calls if v in ("start", "restart")}
    assert started == {"ffmpeg-stream"}                       # no display infra for a direct source
    assert recorder.calls[-1] == ("ffmpeg-stream", "start")
    assert out["rtmp_dropped"] is True


def test_leaving_direct_while_live_also_restarts_encoder(recorder):
    recorder.pin(current_type="direct", ffmpeg_up=True)
    out = control.start_source(WEB)
    assert ("ffmpeg-stream", "stop") in recorder.calls
    assert recorder.calls.index(("browser-source", "restart")) < recorder.calls.index(("ffmpeg-stream", "start"))
    assert out["rtmp_dropped"] is True


def test_direct_from_cold_does_not_report_drop(recorder):
    recorder.pin(current_type="", ffmpeg_up=False)
    out = control.start_source(DIRECT)
    assert recorder.calls[0] == ("ffmpeg-stream", "stop")     # harmless when already down
    assert recorder.calls[-1] == ("ffmpeg-stream", "start")
    assert out["rtmp_dropped"] is False


def test_webpage_to_webpage_while_live_navigates_in_place(recorder, monkeypatch):
    recorder.pin(current_type="webpage", ffmpeg_up=True)
    _mock_public_dns(monkeypatch)
    monkeypatch.setattr(control, "unit_show", lambda unit: {"phase": control.PHASE_LIVE})
    navigated = []

    async def fake_navigate(url):
        navigated.append(url)
    monkeypatch.setattr("app.cdp.navigate", fake_navigate)

    out = control.start_source(WEB)
    assert navigated == [WEB["url"]]
    assert recorder.calls == []                               # nothing restarted at all
    assert out["hot_swapped"] is True and out["rtmp_dropped"] is False


def test_webpage_to_webpage_falls_back_to_producer_restart_when_cdp_unavailable(recorder, monkeypatch):
    recorder.pin(current_type="webpage", ffmpeg_up=True)
    _mock_public_dns(monkeypatch)
    monkeypatch.setattr(control, "unit_show", lambda unit: {"phase": control.PHASE_LIVE})

    async def no_port(url):
        from app.cdp import CDPError
        raise CDPError("no debug port")
    monkeypatch.setattr("app.cdp.navigate", no_port)

    out = control.start_source(WEB)
    assert ("browser-source", "restart") in recorder.calls
    assert not [c for c in recorder.calls if c[0] == "ffmpeg-stream"]  # still no RTMP drop
    assert out["hot_swapped"] is False and out["rtmp_dropped"] is False
    assert out["results"][0]["action"] == "cdp-navigate" and out["results"][0]["ok"] is False


def test_zoom_registration_page_without_join_link_is_refused(recorder):
    recorder.pin()
    reg = {"type": "zoom", "url": "https://us06web.zoom.us/webinar/register/WN_abcdefgh", "options": {}}
    with pytest.raises(control.ControlError):
        control.start_source(reg)
    assert recorder.calls == []


def test_start_source_rejects_unknown_type(recorder):
    with pytest.raises(control.ControlError):
        control.start_source({"type": "carrier-pigeon", "url": "x", "options": {}})


# ---------------------------------------------------------------- zoom link classification

def test_zoomlink_classification():
    from app import zoomlink
    assert zoomlink.classify("https://zoom.us/j/1234567890?pwd=abc")["kind"] == "meeting"
    p = zoomlink.classify("https://us06web.zoom.us/w/8123456789?tk=abcdef1234567890&pwd=xyz")
    assert p["kind"] == "personal" and p["has_tk"] and p["meeting_id"] == "8123456789"
    r = zoomlink.classify("https://us06web.zoom.us/webinar/register/WN_abcdEFGH1234")
    assert r["kind"] == "registration" and r["registration_id"] == "WN_abcdEFGH1234"
    assert zoomlink.classify("https://example.com/j/1234567890")["kind"] == "unknown"
    with pytest.raises(zoomlink.ZoomLinkError):
        zoomlink.validate_joinable("https://us06web.zoom.us/webinar/register/WN_x1234567")


# ---------------------------------------------------------- Part 4: watermark

def test_source_env_lines_carries_watermark_config_forward(monkeypatch):
    """Switching sources must not silently clear the watermark - it's
    independent of which source is active."""
    monkeypatch.setattr(control, "read_current_source", lambda: {
        "WATERMARK_ENABLED": "1", "WATERMARK_MODE": "text", "WATERMARK_TEXT": "LIVE",
        "WATERMARK_FONT": "inter", "WATERMARK_ANCHOR": "bottom-right",
        "WATERMARK_MARGIN_X": "24", "WATERMARK_MARGIN_Y": "24",
        "WATERMARK_SIZE": "28", "WATERMARK_OPACITY": "100", "WATERMARK_IMAGE_PATH": "",
    })
    source = {"type": "zoom", "url": "https://zoom.us/j/123", "options": {}}
    values = control._source_env_lines(source)
    assert values["WATERMARK_ENABLED"] == "1"
    assert values["WATERMARK_TEXT"] == "LIVE"


def test_source_env_lines_defaults_watermark_when_never_configured(monkeypatch):
    monkeypatch.setattr(control, "read_current_source", lambda: {})
    source = {"type": "zoom", "url": "https://zoom.us/j/123", "options": {}}
    values = control._source_env_lines(source)
    assert values["WATERMARK_ENABLED"] == ""


def test_write_watermark_config_updates_only_watermark_keys(monkeypatch):
    monkeypatch.setattr(control, "read_current_source", lambda: {
        "SOURCE_TYPE": "webpage", "WEBPAGE_URL": "https://example.com", "WATERMARK_ENABLED": "0",
    })
    calls = []
    monkeypatch.setattr(control, "_write_source_env_values", lambda values: calls.append(values))

    control.write_watermark_config({
        "visible": True, "mode": "text", "text": "ACME Corp", "encoder_font": "jetbrains-mono",
        "anchor": "top-center", "margin_x": 10, "margin_y": 12, "font_size": 32, "font_opacity": 80,
    })

    assert len(calls) == 1
    written = calls[0]
    assert written["SOURCE_TYPE"] == "webpage"  # untouched
    assert written["WEBPAGE_URL"] == "https://example.com"  # untouched
    assert written["WATERMARK_ENABLED"] == "1"
    assert written["WATERMARK_MODE"] == "text"
    assert written["WATERMARK_TEXT"] == "ACME Corp"
    assert written["WATERMARK_FONT"] == "jetbrains-mono"
    assert written["WATERMARK_ANCHOR"] == "top-center"
    assert written["WATERMARK_MARGIN_X"] == "10"
    assert written["WATERMARK_SIZE"] == "32"
    assert written["WATERMARK_OPACITY"] == "80"


def test_write_watermark_config_image_mode_uses_image_fields(monkeypatch):
    monkeypatch.setattr(control, "read_current_source", lambda: {})
    calls = []
    monkeypatch.setattr(control, "_write_source_env_values", lambda values: calls.append(values))

    control.write_watermark_config({
        "visible": True, "mode": "image", "image_scale_pct": 20.0, "image_opacity": 60,
        "image_path": "/home/zoombot/zoom-stream/watermarks/logo.png",
    })

    written = calls[0]
    assert written["WATERMARK_MODE"] == "image"
    assert written["WATERMARK_SIZE"] == "20.0"
    assert written["WATERMARK_OPACITY"] == "60"
    assert written["WATERMARK_IMAGE_PATH"] == "/home/zoombot/zoom-stream/watermarks/logo.png"


def test_watermark_is_running_false_when_no_ffmpeg(monkeypatch):
    import subprocess as sp
    monkeypatch.setattr(control.subprocess, "run", lambda *a, **k: sp.CompletedProcess(a, 1, stdout=b""))
    assert control.watermark_is_running() is False


def test_watermark_is_running_true_when_drawtext_present(monkeypatch):
    import subprocess as sp

    def fake_run(*a, **k):
        return sp.CompletedProcess(a, 0, stdout=b"1234 /usr/bin/ffmpeg ... -vf drawtext=fontfile=... -f flv rtmps://a.rtmps.youtube.com:443/live2/SECRETKEY")
    monkeypatch.setattr(control.subprocess, "run", fake_run)
    assert control.watermark_is_running() is True


def test_watermark_is_running_false_when_ffmpeg_has_no_watermark_filter(monkeypatch):
    import subprocess as sp

    def fake_run(*a, **k):
        return sp.CompletedProcess(a, 0, stdout=b"1234 /usr/bin/ffmpeg -f x11grab -i :99 -f flv rtmps://a.rtmps.youtube.com:443/live2/SECRETKEY")
    monkeypatch.setattr(control.subprocess, "run", fake_run)
    assert control.watermark_is_running() is False
