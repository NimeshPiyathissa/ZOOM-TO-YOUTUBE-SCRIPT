"""Evidence-based Zoom state: web/desktop classification, the auto-fallback decision, and clean-feed CSS parity."""
import pathlib

import pytest

from app import zoom_cleanfeed, zoom_state

EXT = pathlib.Path(__file__).resolve().parents[2] / "extensions" / "zoom-cleanfeed"


def web(**kw):
    sig = {"on_zoom": True, "leave_present": False, "footer_present": False, "labels": [], "playing_video": False, "prejoin_form": False}
    sig.update(kw)
    return sig


# ------------------------------------------------------------------ web client

def test_hidden_footer_does_not_matter_only_presence_does():
    v = zoom_state.classify_web(web(leave_present=True, footer_present=True, playing_video=True))
    assert v["status"] == "in_meeting" and v["confidence"] == "high"


def test_not_on_zoom_is_unknown():
    assert zoom_state.classify_web(web(on_zoom=False))["status"] == "unknown"


def test_error_words_in_chat_do_not_override_strong_evidence():
    v = zoom_state.classify_web(web(leave_present=True, footer_present=True), ("expired", "x"))
    assert v["status"] == "in_meeting" and v["ignored_text_match"] == "expired"


def test_error_text_without_meeting_evidence_is_reported():
    assert zoom_state.classify_web(web(), ("expired", "This meeting link has expired"))["status"] == "expired"


def test_prejoin_form_is_prejoin_not_connecting():
    assert zoom_state.classify_web(web(prejoin_form=True))["status"] == "prejoin"


def test_bare_zoom_page_is_low_confidence_connecting():
    v = zoom_state.classify_web(web())
    assert v["status"] == "connecting" and v["confidence"] == "low" and not v["confirmed"]


def test_two_weak_signals_are_medium_in_meeting():
    v = zoom_state.classify_web(web(footer_present=True, playing_video=True))
    assert v["status"] == "in_meeting" and v["confidence"] == "medium"


# ------------------------------------------------------------------ desktop client

def desk(status, signals=(), **kw):
    return {"status": status, "signals": list(signals), "detail": "d", **kw}


def test_desktop_no_process_is_not_running():
    assert zoom_state.classify_desktop(desk("not_joined", ["process=no"]), unit_active=False)["status"] == "not_running"


def test_desktop_home_screen_guess_without_atspi_is_unknown():
    v = zoom_state.classify_desktop(desk("not_joined", ["process=yes", "atspi=pyatspi-not-installed"]), unit_active=True)
    assert v["status"] == "unknown" and v["confidence"] == "low"


def test_desktop_home_screen_with_atspi_is_not_joined():
    v = zoom_state.classify_desktop(desk("not_joined", ["process=yes", "atspi=ok"]), unit_active=True)
    assert v["status"] == "not_joined" and v["confidence"] == "medium"


@pytest.mark.parametrize("ev,conf", [("title", "high"), ("controls", "high"), ("audio", "medium"), ("media", "medium")])
def test_desktop_in_meeting_confidence_by_evidence(ev, conf):
    v = zoom_state.classify_desktop(desk("in_meeting", ["process=yes"], evidence=ev), unit_active=True)
    assert v["status"] == "in_meeting" and v["confidence"] == conf


def test_parse_signals():
    assert zoom_state.parse_signals(["process=yes", "media=udp:3", "junk"]) == {"process": "yes", "media": "udp:3"}


# ------------------------------------------------------------------ fallback decision

def D(status, conf="medium"):
    return {"status": status, "confidence": conf}


def fd(desktop, elapsed, **kw):
    return zoom_state.fallback_decision(desktop, elapsed=elapsed, auto_mode=kw.pop("auto_mode", True), **kw)["action"]


def test_in_meeting_is_done_at_any_time():
    assert fd(D("in_meeting"), 3) == "done"


@pytest.mark.parametrize("st", sorted(zoom_state.HARD_PHRASES) + ["waiting_room", "not_started"])
def test_meeting_side_refusals_never_fall_back(st):
    assert fd(D(st), 200) == "done"


def test_never_decides_before_the_minimum_wait():
    assert fd(D("not_running"), zoom_state.FALLBACK_MIN_SECONDS - 1) == "wait"
    assert fd(D("not_joined"), zoom_state.FALLBACK_MIN_SECONDS - 1) == "wait"


def test_positive_failure_evidence_falls_back_after_the_wait():
    t = zoom_state.FALLBACK_MIN_SECONDS + 1
    assert fd(D("not_running", "high"), t) == "fallback"
    assert fd(D("unknown", "low"), t, launch_error="launch failed") == "fallback"
    assert fd(D("not_joined", "medium"), t) == "fallback"


@pytest.mark.parametrize("st", ["unknown", "connecting"])
def test_unconfirmed_is_never_a_failure(st):
    assert fd(D(st, "low"), zoom_state.FALLBACK_MIN_SECONDS + 30) == "wait"
    assert fd(D(st, "low"), zoom_state.GIVE_UP_WATCHING_SECONDS + 1) == "stop_watching"   # stops watching; kills nothing


def test_non_auto_mode_never_falls_back():
    assert fd(D("not_running"), 500, auto_mode=False) == "stop_watching"


# ------------------------------------------------------------------ clean-feed parity and scoping

def test_extension_css_is_exactly_the_generated_css():
    assert (EXT / "cleanfeed.css").read_text(encoding="utf-8").replace("\r\n", "\n") == zoom_cleanfeed.CSS_TEXT


def test_every_hiding_rule_is_scoped_to_the_in_meeting_attribute():
    css = zoom_cleanfeed.CSS_TEXT
    for block in css.split("}"):
        sel = block.split("{")[0]
        if "display: none" in block or "object-fit" in block or "::-webkit-scrollbar" in sel:
            for part in [p.strip() for p in sel.split(",") if p.strip() and not p.strip().startswith("/*")]:
                assert 'html[data-zs-cleanfeed="on"]' in part, part


def test_extension_script_matches_the_shared_state_script():
    js = (EXT / "cleanfeed.js").read_text(encoding="utf-8")
    assert "data-zs-cleanfeed" in js and "MutationObserver" not in js
