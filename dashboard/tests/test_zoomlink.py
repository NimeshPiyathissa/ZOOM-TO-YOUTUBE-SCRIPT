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


# ---------------------------------------------------------------- wc/join (web client) links

def test_wc_join_classifies_same_as_j():
    a = zoomlink.classify("https://us06web.zoom.us/j/81234567890?pwd=AbC.123-xyz_1")
    b = zoomlink.classify("https://us06web.zoom.us/wc/join/81234567890?pwd=AbC.123-xyz_1")
    assert a["kind"] == b["kind"] == "meeting"
    assert a["meeting_id"] == b["meeting_id"] == "81234567890"
    assert a["has_pwd"] and b["has_pwd"]


def test_wc_join_with_tk_is_personal():
    info = zoomlink.classify("https://us06web.zoom.us/wc/join/81234567890?tk=abcdefghijk.LMNOP-qrs")
    assert info["kind"] == "personal" and info["has_tk"]


def test_build_wc_join_url_roundtrips():
    url = zoomlink.build_wc_join_url("81234567890", "AbC.123-xyz_1", "tok.EN-1_2", host="us06web.zoom.us")
    info = zoomlink.classify(url)
    creds = zoomlink.extract_credentials(url)
    assert info["kind"] == "personal" and info["meeting_id"] == "81234567890"
    assert creds == {"meeting_id": "81234567890", "pwd": "AbC.123-xyz_1", "tk": "tok.EN-1_2", "host": "us06web.zoom.us"}


# ---------------------------------------------------------------- round-trip: parse -> rebuild -> same meeting
#
# For each shape: parse_any() understands it, and the url it returns
# (or one rebuilt from extract_credentials()) resolves to the identical
# meeting_id/pwd/tk, verbatim - no decode/re-encode/truncate, even for
# tokens containing dots, underscores and hyphens together.

_PWD_WITH_PUNCTUATION = "AbC.123-xyz_1"
_TK_WITH_PUNCTUATION = "abcDEF.123-xyz_1.ghi"

ROUND_TRIP_CASES = [
    ("public /j/ link, 11-digit id", f"https://us06web.zoom.us/j/81234567890?pwd={_PWD_WITH_PUNCTUATION}", "81234567890", _PWD_WITH_PUNCTUATION, None),
    ("public /j/ link, 10-digit id", f"https://zoom.us/j/8123456789?pwd={_PWD_WITH_PUNCTUATION}", "8123456789", _PWD_WITH_PUNCTUATION, None),
    ("public /j/ link, 9-digit id", f"https://zoom.us/j/812345678?pwd={_PWD_WITH_PUNCTUATION}", "812345678", _PWD_WITH_PUNCTUATION, None),
    ("web client /wc/join/ link", f"https://us06web.zoom.us/wc/join/81234567890?pwd={_PWD_WITH_PUNCTUATION}", "81234567890", _PWD_WITH_PUNCTUATION, None),
    ("vanity subdomain, /j/", f"https://company.zoom.us/j/81234567890?pwd={_PWD_WITH_PUNCTUATION}", "81234567890", _PWD_WITH_PUNCTUATION, None),
    ("per-registrant /w/ link with tk", f"https://us06web.zoom.us/w/81234567890?tk={_TK_WITH_PUNCTUATION}&pwd={_PWD_WITH_PUNCTUATION}", "81234567890", _PWD_WITH_PUNCTUATION, _TK_WITH_PUNCTUATION),
    ("per-registrant /wc/join/ link with tk", f"https://us06web.zoom.us/wc/join/81234567890?tk={_TK_WITH_PUNCTUATION}", "81234567890", None, _TK_WITH_PUNCTUATION),
    ("zoommtg:// deep link", f"zoommtg://zoom.us/join?action=join&confno=81234567890&pwd={_PWD_WITH_PUNCTUATION}&tk={_TK_WITH_PUNCTUATION}&uname=Bot", "81234567890", _PWD_WITH_PUNCTUATION, _TK_WITH_PUNCTUATION),
    ("wrapped in email invite text", f"Join Zoom Meeting\nhttps://us06web.zoom.us/j/81234567890?pwd={_PWD_WITH_PUNCTUATION}#success\n\nMeeting ID: 812 3456 7890", "81234567890", _PWD_WITH_PUNCTUATION, None),
    ("safelink-wrapped redirect", f"https://nam04.safelinks.protection.outlook.com/?url=https%3A%2F%2Fus06web.zoom.us%2Fj%2F81234567890%3Fpwd%3D{_PWD_WITH_PUNCTUATION}&data=xyz", "81234567890", _PWD_WITH_PUNCTUATION, None),
]


@pytest.mark.parametrize("label,text,mid,pwd,tk", ROUND_TRIP_CASES, ids=[c[0] for c in ROUND_TRIP_CASES])
def test_round_trip_parse_then_rebuild(label, text, mid, pwd, tk):
    r = zoomlink.parse_any(text)
    assert r["ok"], (label, r["errors"])
    assert r["meeting_id"] == mid, label
    creds = zoomlink.extract_credentials(r["url"])
    assert creds["meeting_id"] == mid, label
    assert creds["pwd"] == pwd, label
    assert creds["tk"] == tk, label
    # Rebuilding either shape from the extracted credentials must still
    # resolve to the same meeting, with the same secrets, verbatim.
    rebuilt_deep = zoomlink.build_join_url(mid, pwd, tk, host=creds["host"])
    rebuilt_wc = zoomlink.build_wc_join_url(mid, pwd, tk, host=creds["host"])
    assert zoomlink.extract_credentials(rebuilt_deep) == creds, label
    assert zoomlink.extract_credentials(rebuilt_wc) == creds, label
    # Never decoded/truncated/re-encoded into something different.
    if pwd:
        assert pwd in r["url"] and "%" not in pwd
    if tk:
        assert tk in r["url"] and "%" not in tk


def test_safelink_wrapped_registration_page_not_unwrapped_into_join_link():
    # A registration page wrapped the same way must still classify as
    # registration, not be mistaken for a joinable link.
    wrapped = "https://safelinks.example.com/?url=https%3A%2F%2Fus06web.zoom.us%2Fwebinar%2Fregister%2FWN_abcDEF123"
    r = zoomlink.parse_any(wrapped)
    assert r["ok"] and r["input_kind"] == "registration"
