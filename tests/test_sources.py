"""Tests for app/sources.py - source-type auto-detection and the
polymorphic validation dispatch (Change 1)."""
import socket

import pytest

from app import sources, config
from app.env_store import ValidationError


# ---------------------------------------------------------------- detect_type

@pytest.mark.parametrize("url,expected", [
    ("https://zoom.us/j/1234567890?pwd=abc", "zoom"),
    ("https://us02web.zoom.us/j/1234567890", "zoom"),
    ("http://zoom.us/j/123", "zoom"),
    ("https://example.com/live/stream.m3u8", "direct"),
    ("https://example.com/video.mp4", "direct"),
    ("https://example.com/video.mkv", "direct"),
    ("rtmp://example.com/live/key", "direct"),
    ("rtmps://example.com/live/key", "direct"),
    ("srt://example.com:9000?streamid=abc", "direct"),
    ("https://meet.google.com/abc-defg-hij", "webpage"),
    ("https://teams.microsoft.com/l/meetup-join/abc", "webpage"),
    ("https://example.com/some/slideshow", "webpage"),
    ("", "webpage"),
])
def test_detect_type(url, expected):
    assert sources.detect_type(url) == expected


# ---------------------------------------------------------------- validate_source: zoom

def test_validate_zoom_source_accepts_valid_link():
    url, options = sources.validate_source("zoom", "https://zoom.us/j/1234567890?pwd=abc123", {
        "bot_name": "Stream Bot", "signin_mode": "guest",
    })
    assert url == "https://zoom.us/j/1234567890?pwd=abc123"
    assert options["signin_mode"] == "guest"
    assert options["bot_name"] == "Stream Bot"


def test_validate_zoom_source_rejects_bad_link():
    with pytest.raises(ValidationError):
        sources.validate_source("zoom", "https://example.com/not-zoom", {})


def test_validate_zoom_source_rejects_bad_signin_mode():
    with pytest.raises(ValidationError):
        sources.validate_source("zoom", "https://zoom.us/j/1234567890", {"signin_mode": "carrier-pigeon"})


def test_validate_zoom_source_defaults_signin_mode_to_guest():
    _, options = sources.validate_source("zoom", "https://zoom.us/j/1234567890", {})
    assert options["signin_mode"] == "guest"


# ---------------------------------------------------------------- validate_source: webpage

def test_validate_webpage_source(monkeypatch):
    _mock_public_dns(monkeypatch)
    url, options = sources.validate_source("webpage", "https://meet.google.com/abc-defg-hij", {
        "zoom_level": 1.25, "reload_seconds": 300, "click_to_start": True,
    })
    assert url == "https://meet.google.com/abc-defg-hij"
    assert options == {"zoom_level": 1.25, "reload_seconds": 300, "click_to_start": True}


def test_validate_webpage_source_rejects_out_of_range_zoom(monkeypatch):
    _mock_public_dns(monkeypatch)
    with pytest.raises(ValidationError):
        sources.validate_source("webpage", "https://example.com/", {"zoom_level": 10.0})


def test_validate_webpage_source_rejects_private_url():
    with pytest.raises(ValidationError):
        sources.validate_source("webpage", "http://169.254.169.254/", {})


def test_validate_webpage_source_rejects_rtmp_scheme(monkeypatch):
    _mock_public_dns(monkeypatch)
    with pytest.raises(ValidationError):
        sources.validate_source("webpage", "rtmp://example.com/live", {})


# ---------------------------------------------------------------- validate_source: direct

def test_validate_direct_source_defaults(monkeypatch):
    _mock_public_dns(monkeypatch)
    url, options = sources.validate_source("direct", "https://example.com/stream.m3u8", {})
    assert options == {"mode": "reencode", "loop": False, "reconnect": True}


def test_validate_direct_source_copy_mode(monkeypatch):
    _mock_public_dns(monkeypatch)
    _, options = sources.validate_source("direct", "https://example.com/stream.m3u8", {"mode": "copy"})
    assert options["mode"] == "copy"


def test_validate_direct_source_rejects_bad_mode(monkeypatch):
    _mock_public_dns(monkeypatch)
    with pytest.raises(ValidationError):
        sources.validate_source("direct", "https://example.com/stream.m3u8", {"mode": "transcode-magically"})


def test_validate_direct_source_rejects_metadata_ip():
    with pytest.raises(ValidationError):
        sources.validate_source("direct", "http://169.254.169.254/latest/meta-data/", {})


# ---------------------------------------------------------------- validate_source: type

def test_validate_source_rejects_unknown_type():
    with pytest.raises(ValidationError):
        sources.validate_source("carrier-pigeon", "https://example.com/", {})


def _mock_public_dns(monkeypatch):
    def fake_getaddrinfo(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    monkeypatch.setattr(sources.url_security.socket, "getaddrinfo", fake_getaddrinfo)
