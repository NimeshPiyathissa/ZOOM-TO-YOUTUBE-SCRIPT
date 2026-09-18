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


# ---------------------------------------------------------------- start_source sequencing

class _Recorder:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def unit_action(self, unit, verb):
        self.calls.append((unit, verb))
        return {"ok": True, "unit": unit, "verb": verb}


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(control, "unit_action", rec.unit_action)
    monkeypatch.setattr(control, "write_current_source", lambda source: None)
    monkeypatch.setattr(control.time, "sleep", lambda *_: None)
    return rec


def test_start_source_zoom_stops_browser_source_and_starts_zoom_chain(recorder, monkeypatch):
    monkeypatch.setattr("app.env_store.write_updates", lambda updates: [])
    source = {"type": "zoom", "url": "https://zoom.us/j/123", "options": {"bot_name": "Bot", "signin_mode": "guest"}}
    control.start_source(source)

    assert ("ffmpeg-stream", "stop") == recorder.calls[0]
    assert ("browser-source", "stop") in recorder.calls
    assert ("zoom", "stop") not in recorder.calls  # zoom is the wanted producer, never stopped
    assert ("xvfb", "start") in recorder.calls
    assert ("openbox", "start") in recorder.calls
    assert ("audio-setup", "start") in recorder.calls
    assert ("zoom", "start") in recorder.calls
    assert ("x11vnc", "start") in recorder.calls
    assert recorder.calls[-1] == ("ffmpeg-stream", "start")
    # producer must be started before the encoder
    assert recorder.calls.index(("zoom", "start")) < recorder.calls.index(("ffmpeg-stream", "start"))


def test_start_source_webpage_stops_zoom_and_starts_browser_source(recorder, monkeypatch):
    source = {"type": "webpage", "url": "https://meet.google.com/abc", "options": {}}
    control.start_source(source)

    assert ("zoom", "stop") in recorder.calls
    assert ("browser-source", "stop") not in recorder.calls
    assert ("browser-source", "start") in recorder.calls
    assert recorder.calls.index(("browser-source", "start")) < recorder.calls.index(("ffmpeg-stream", "start"))


def test_start_source_direct_stops_both_producers_and_skips_display_infra(recorder, monkeypatch):
    source = {"type": "direct", "url": "https://example.com/stream.m3u8", "options": {}}
    control.start_source(source)

    assert ("zoom", "stop") in recorder.calls
    assert ("browser-source", "stop") in recorder.calls
    # a direct source needs no Xvfb/audio/producer/VNC infra at all
    started_units = {u for u, v in recorder.calls if v == "start"}
    assert started_units == {"ffmpeg-stream"}
    assert recorder.calls[0] == ("ffmpeg-stream", "stop")
    assert recorder.calls[-1] == ("ffmpeg-stream", "start")


def test_start_source_rejects_unknown_type(recorder):
    with pytest.raises(control.ControlError):
        control.start_source({"type": "carrier-pigeon", "url": "x", "options": {}})
