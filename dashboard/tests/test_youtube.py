"""Tests for app/youtube.py - smart-paste parsing of every YouTube link
form, option validation, and the embed URL the kiosk navigates to."""
import pytest

from app import youtube


@pytest.mark.parametrize("text,kind,vid,lst,start", [
    ("https://www.youtube.com/watch?v=V_V6jyJ9RZ0&t=1m30s&si=abc", "video", "V_V6jyJ9RZ0", None, 90),
    ("https://youtu.be/V_V6jyJ9RZ0?t=90", "video", "V_V6jyJ9RZ0", None, 90),
    ("https://www.youtube.com/shorts/V_V6jyJ9RZ0", "video", "V_V6jyJ9RZ0", None, None),
    ("https://www.youtube.com/live/V_V6jyJ9RZ0", "video", "V_V6jyJ9RZ0", None, None),
    ("https://music.youtube.com/watch?v=V_V6jyJ9RZ0", "video", "V_V6jyJ9RZ0", None, None),
    ("https://www.youtube.com/embed/V_V6jyJ9RZ0?start=30", "video", "V_V6jyJ9RZ0", None, 30),
    ("V_V6jyJ9RZ0", "video", "V_V6jyJ9RZ0", None, None),
    ("Check this out! https://youtu.be/V_V6jyJ9RZ0?si=xyz enjoy", "video", "V_V6jyJ9RZ0", None, None),
    ("https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI", "playlist", None, "PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI", None),
    ("https://www.youtube.com/watch?v=V_V6jyJ9RZ0&list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI", "video", "V_V6jyJ9RZ0", "PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI", None),
])
def test_parse_any_ok(text, kind, vid, lst, start):
    r = youtube.parse_any(text)
    assert r["ok"], r["errors"]
    assert (r["kind"], r["video_id"], r["list_id"], r["start"]) == (kind, vid, lst, start)
    assert "si=" not in r["url"] and r["url"].startswith("https://www.youtube.com/")


def test_live_channel_forms():
    for t in ("https://www.youtube.com/@lofigirl/live", "https://www.youtube.com/channel/UCSJ4gkVC6NrvII8umztf0Ow/live", "https://www.youtube.com/c/lofigirl/live"):
        r = youtube.parse_any(t)
        assert r["ok"] and r["kind"] == "live_channel" and r["url"].endswith("/live"), t


@pytest.mark.parametrize("text,msg", [
    ("https://www.youtube.com/@lofigirl", "channel page"),
    ("https://vimeo.com/123", "isn't a YouTube link"),
    ("hello", "Couldn't find"),
    ("", "Paste a YouTube"),
    ("https://www.youtube.com/feed/subscriptions", "Couldn't find a video"),
])
def test_parse_any_errors(text, msg):
    r = youtube.parse_any(text)
    assert not r["ok"] and any(msg in e for e in r["errors"]), r["errors"]


def test_play_url(monkeypatch):
    monkeypatch.setattr(youtube.url_security, "validate_url", lambda u, t: u)
    # never an /embed/ URL: top-level embed navigation gets YouTube's "configuration error 153"
    u = youtube.to_play_url("https://www.youtube.com/watch?v=V_V6jyJ9RZ0&t=90", {"loop": True, "captions": True})
    assert u.startswith("https://www.youtube.com/watch?v=V_V6jyJ9RZ0") and "&t=90s" in u and "/embed/" not in u
    u = youtube.to_play_url("https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI", {"loop": True})
    assert u.startswith("https://www.youtube.com/watch?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI")
    u = youtube.to_play_url("https://www.youtube.com/watch?v=V_V6jyJ9RZ0", {"start": 5})
    assert "&t=5s" in u
    u = youtube.to_play_url("https://www.youtube.com/watch?v=V_V6jyJ9RZ0&list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI")
    assert "v=V_V6jyJ9RZ0&list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI" in u


def test_validate_options():
    o = youtube.validate_options({"start": "1m30s", "speed": "1.25", "loop": 1})
    assert o == {"start": 90, "loop": True, "captions": False, "speed": 1.25}
    with pytest.raises(youtube.YouTubeURLError):
        youtube.validate_options({"speed": 3})
    with pytest.raises(youtube.YouTubeURLError):
        youtube.validate_options({"start": "-5"})


def test_time_helpers():
    assert youtube.parse_time("1h2m3s") == 3723 and youtube.parse_time("90") == 90 and youtube.parse_time("2m") == 120
    assert youtube.format_time(3723) == "1:02:03" and youtube.format_time(90) == "1:30"
