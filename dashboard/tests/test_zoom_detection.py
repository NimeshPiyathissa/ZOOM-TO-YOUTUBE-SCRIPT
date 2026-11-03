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


def test_audio_alone_proves_a_meeting_when_atspi_is_missing():
    # prod 2026-11-03: no python3-pyatspi, title "Zoom Workplace", no text
    r = classify_window(["Zoom Workplace"], "", {"available": False, "reason": "pyatspi-not-installed"}, audio=True)
    assert r[0] == "in_meeting" and r[3]["evidence"] == "audio"
    assert classify_window(["Zoom Workplace"], "", {"available": False}, audio=False)[0] == "not_joined"


def test_nothing_recognizable():
    assert classify_window(["Some Dialog"], "", {"available": True}) is None


@pytest.mark.skipif(not (shutil.which("bash") and shutil.which("ffmpeg")), reason="needs bash + ffmpeg")
@pytest.mark.parametrize("fit", ["fit", "fill"])
def test_meeting_only_filter_never_stretches(fit, tmp_path):
    lib = (SCRIPTS / "lib.sh").read_text(encoding="utf-8")
    fn = lib[lib.index("build_meeting_only_filter() {"):]
    script = tmp_path / "fn.sh"
    script.write_text(fn, encoding="utf-8", newline="\n")
    out = subprocess.run(["bash", "-c", f'source "{script.as_posix()}"; build_meeting_only_filter 1920 1080 {fit} 56 96; echo "$MEETING_ONLY_FILTER"'],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out.startswith("crop=")
    pic = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=1",
                          "-vf", out, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    assert len(pic) == 1920 * 1080 * 3          # always exactly the output size
    if fit == "fill":
        # no black bars: first and last rows both carry picture content
        assert any(pic[:1920 * 3]) and any(pic[-1920 * 3:])
    else:
        # letterboxed: top and bottom rows are pad (black), middle is picture
        assert not any(pic[:1920 * 3]) and not any(pic[-1920 * 3:])


def test_encoder_filter_stale_tracks_encoder_start(monkeypatch):
    """restart_needed means "the running encoder started before the last
    Fit/Fill change" - not "the setting differs from some default"."""
    import time as _t
    from app import control
    monkeypatch.setattr(control, "_encoder_filter_changed_at", 0.0)
    live = {"phase": control.PHASE_LIVE, "uptime_seconds": 600}
    monkeypatch.setattr(control, "unit_show", lambda unit: dict(live))
    assert control.encoder_filter_stale() is False          # nothing changed yet
    control.note_encoder_filter_changed()
    assert control.encoder_filter_stale() is True           # started 10 min before the change
    live["uptime_seconds"] = 0                              # restarted just now
    assert control.encoder_filter_stale() is False
    live.update(phase=control.PHASE_STOPPED, uptime_seconds=None)
    control.note_encoder_filter_changed()
    assert control.encoder_filter_stale() is False          # stopped encoder picks it up on next start
