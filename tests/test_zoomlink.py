"""Tests for app/zoomlink.py - smart-paste parsing of every way into a
Zoom meeting, normalization, and redaction (Zoom page)."""
import pytest

from app import zoomlink

INVITE = """Nimesh is inviting you to a scheduled Zoom meeting.

Join Zoom Meeting
https://us06web.zoom.us/j/81234567890?pwd=AbC123xyz#success

Meeting ID: 812 3456 7890
Passcode: 447722
"""


@pytest.mark.parametrize("text,passcode,kind,mid,has_pass", [
    ("https://us06web.zoom.us/j/81234567890?pwd=AbC123xyz#success", None, "join_link", "81234567890", True),
    ("https://us06web.zoom.us/w/81234567890?tk=abcdefghijk.LMNOP-qrs&pwd=xyz", None, "personal_link", "81234567890", True),
    ("https://us06web.zoom.us/j/81234567890?tk=abcdefghijk.LMNOP-qrs", None, "personal_link", "81234567890", False),
    ("812 3456 7890", "s3cret", "meeting_id", "81234567890", True),
    ("81234567890", None, "meeting_id", "81234567890", False),
    ("zoommtg://zoom.us/join?action=join&confno=81234567890&pwd=Qq1&uname=Stream%20Bot", None, "deep_link", "81234567890", True),
    (INVITE, None, "invite", "81234567890", True),
    ("Topic: Town hall\nMeeting ID: 812 3456 7890\nPasscode: 447722", None, "invite", "81234567890", True),
])
def test_parse_any_ok(text, passcode, kind, mid, has_pass):
    r = zoomlink.parse_any(text, passcode)
    assert r["ok"], r["errors"]
    assert r["input_kind"] == kind
    assert r["meeting_id"] == mid
    assert r["has_passcode"] is has_pass
    assert r["url"].startswith("https://") and "/j/" in r["url"] or "/w/" in r["url"]
    assert "#" not in r["url"]
    assert "•••" in r["url_redacted"] or not ("pwd=" in r["url"] or "tk=" in r["url"])


def test_registration_page():
    r = zoomlink.parse_any("https://us06web.zoom.us/webinar/register/WN_abcDEF123#/registration")
    assert r["ok"] and r["input_kind"] == "registration" and r["meeting_kind"] == "webinar"
    assert r["registration_url"] == "https://us06web.zoom.us/webinar/register/WN_abcDEF123"
    assert r["meeting_id"] is None and any("registration form" in w for w in r["warnings"])


def test_vanity_needs_resolve():
    r = zoomlink.parse_any("https://zoom.us/my/laknath.hoki?from=addon")
    assert r["ok"] and r["input_kind"] == "vanity" and r["needs_resolve"] and r["meeting_kind"] == "pmi"
    assert r["url"] == "https://zoom.us/my/laknath.hoki"


def test_personal_link_warns_and_is_webinar():
    r = zoomlink.parse_any("https://us06web.zoom.us/w/81234567890?tk=abcdefghijk.LMNOP-qrs")
    assert r["meeting_kind"] == "webinar" and r["has_tk"]
    assert any("tied to the one person" in w for w in r["warnings"])


def test_deep_link_carries_display_name_and_passcode_separately():
    r = zoomlink.parse_any("zoommtg://zoom.us/join?action=join&confno=81234567890&uname=Bot", "pc1")
    assert r["bot_display_name"] == "Bot" and r["passcode"] == "pc1" and "pwd=" not in r["url"]


@pytest.mark.parametrize("text,msg", [
    ("https://meet.google.com/abc-defg-hij", "isn't on zoom.us"),
    ("hello there", "Couldn't find"),
    ("", "Paste a Zoom link"),
    ("zoommtg://zoom.us/join?action=join&confno=12", "confno"),
    ("https://zoom.us/s/81234567890", "isn't a join link"),
])
def test_parse_any_errors(text, msg):
    r = zoomlink.parse_any(text)
    assert not r["ok"] and any(msg in e for e in r["errors"]), r["errors"]


def test_bad_passcode_characters_rejected():
    r = zoomlink.parse_any("81234567890", "bad pass;rm")
    assert any("Passcode contains" in e for e in r["errors"])


def test_redact_and_normalize():
    assert zoomlink.redact_url("https://x.zoom.us/j/1?pwd=abc&tk=def&x=1") == "https://x.zoom.us/j/1?pwd=•••&tk=•••&x=1"
    assert zoomlink.normalize_url("https://US06WEB.zoom.us/j/81234567890/?pwd=A&_x_zm_rtaid=zzz#success") == "https://us06web.zoom.us/j/81234567890?pwd=A"
    assert zoomlink.format_meeting_id("81234567890") == "812 3456 7890"
    assert zoomlink.format_meeting_id("1234567890") == "123 456 7890"


def test_validate_joinable_rejects_vanity():
    with pytest.raises(zoomlink.ZoomLinkError):
        zoomlink.validate_joinable("https://zoom.us/my/room")
