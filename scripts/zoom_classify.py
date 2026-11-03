"""The "is Zoom in a meeting?" decision, kept free of subprocess/X/AT-SPI
calls so it can be unit-tested with captured titles and snapshots.

Zoom 7.x titles its main window "Zoom Workplace" at the home screen AND
(observed on a live webinar, 2026-11-03) while a webinar is running, so a
title alone can never mean "not in a meeting". Order of trust:
  1. an explicit "Zoom Meeting"/"Zoom Webinar" title
  2. meeting-only toolbar controls in the AT-SPI tree (Leave, Raise hand,
     Q&A, Mute, Start Video, ...) - zoom-atspi.py's snapshot query
  3. home-screen text ("Join a meeting", "Sign in", ...) => not joined
  4. a "Zoom Workplace" window whose readable UI has none of the
     home-screen text => treated as a meeting, flagged as inferred
"""
import re

HOME_MARKERS = re.compile(r"join a meeting|new meeting|sign in|sign up|^version:|whiteboards", re.I | re.M)


def classify_window(titles, texts, snap):
    """Returns (status, detail, action, extra) or None when nothing
    recognizable is on screen (caller decides connecting/unknown)."""
    snap = snap or {}
    kind_word = lambda k: "webinar" if k == "webinar" else "meeting"
    meeting_titles = [t for t in titles if re.search(r"zoom (meeting|webinar)", t, re.I)]
    if meeting_titles:
        kind = "webinar" if re.search(r"webinar", meeting_titles[0], re.I) else snap.get("kind") or "meeting"
        return ("in_meeting", f"A Zoom {kind_word(kind)} window is open.", "",
                {"kind": kind, "attendee": bool(snap.get("attendee")), "evidence": "title"})
    if snap.get("in_meeting"):
        kind = snap.get("kind") or "meeting"
        return ("in_meeting", f"Zoom is in a {kind_word(kind)} (toolbar: {', '.join(snap.get('controls', [])[:4])}).", "",
                {"kind": kind, "attendee": bool(snap.get("attendee")), "evidence": "controls"})
    if HOME_MARKERS.search(texts or ""):
        return ("not_joined", "Zoom is open at its home screen - not in a meeting.", "Use Join / Rejoin.", {})
    if any(t.strip() == "Zoom Workplace" for t in titles):
        if snap.get("available") and (texts or "").strip():
            return ("in_meeting", "Zoom's main window is up and shows no home-screen UI - treating it as a meeting/webinar.", "",
                    {"kind": "webinar" if re.search(r"webinar", texts, re.I) else "meeting",
                     "attendee": False, "evidence": "inferred"})
        return ("not_joined", "Zoom is open at its home screen - not in a meeting.", "Use Join / Rejoin.", {})
    return None
