"""Meeting detection for Zoom 7.x, where the main window is titled
"Zoom Workplace" at the home screen *and* inside a webinar, and the
Meeting-only encoder filter."""
import pathlib
import shutil
import subprocess
import sys

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from zoom_classify import classify_window  # noqa: E402

HOME_TEXT = "Home\tChat\tPhone\tSign in\tJoin a meeting\tSign up\tVersion: 7.2.1 (5760)"


def status(titles, texts="", snap=None):
    r = classify_window(titles, texts, snap)
    return r and r[0]


def test_home_screen_is_not_joined():
    snap = {"available": True, "in_meeting": False, "controls": []}
    assert status(["Zoom Workplace"], HOME_TEXT, snap) == "not_joined"


def test_webinar_with_workplace_title_is_in_meeting_by_controls():
    snap = {"available": True, "in_meeting": True, "kind": "webinar", "attendee": True,
            "controls": ["Q&A", "Raise Hand", "Reactions", "Leave"]}
    r = classify_window(["Zoom Workplace"], "Q&A\tRaise Hand\tLeave", snap)
    assert r[0] == "in_meeting"
    assert r[3]["kind"] == "webinar" and r[3]["attendee"] is True


def test_regular_meeting_with_workplace_title():
    snap = {"available": True, "in_meeting": True, "kind": "meeting", "attendee": False,
            "controls": ["Mute", "Stop Video", "Leave"]}
    r = classify_window(["Zoom Workplace"], "", snap)
    assert r[0] == "in_meeting" and r[3]["attendee"] is False


def test_explicit_meeting_and_webinar_titles_still_work():
    assert status(["Zoom Meeting"]) == "in_meeting"
    r = classify_window(["Zoom Webinar"], "", None)
    assert r[0] == "in_meeting" and r[3]["kind"] == "webinar"


def test_workplace_window_without_home_ui_is_inferred_meeting():
    snap = {"available": True, "in_meeting": False, "controls": []}
    r = classify_window(["Zoom Workplace"], "Speaker\tGallery\tHost", snap)
    assert r[0] == "in_meeting" and r[3]["evidence"] == "inferred"


def test_workplace_title_with_unreadable_tree_stays_not_joined():
    assert status(["Zoom Workplace"], "", {"available": False}) == "not_joined"


def test_nothing_recognizable():
    assert classify_window(["Some Dialog"], "", {"available": True}) is None
