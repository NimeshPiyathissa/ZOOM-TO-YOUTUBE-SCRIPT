"""Meeting state from SEVERAL INDEPENDENT signals, with the uncertainty stated - never from one guess.

Pure functions (no I/O) so they can be unit-tested with captured snapshots. Two lessons from production (2026-11-05):

  1. The web client's footer is hidden by the clean feed (display:none), so "is the footer visible" said "connecting"
     for a whole meeting. Nothing here looks at visibility: the Leave control's PRESENCE, the toolbar markers, a playing
     video and the absence of the join form are what count.
  2. The desktop client's "in a meeting" is not readable without AT-SPI, and the auto-fallback used to treat
     "not confirmed within 40 s" as "failed" and then stopped a client that had in fact joined. Undetectable is not the
     same as failed: the fallback now needs positive evidence of failure.

Statuses: in_meeting, prejoin, connecting, waiting_room, not_started, not_joined, not_running, unknown plus the terminal
ones from zoom_web.TERMINAL. `confidence` is high/medium/low; `evidence` says why; `confirmed` is True only for high.
"""
from __future__ import annotations

HARD_PHRASES = {"expired", "ended", "removed", "locked", "passcode_required", "join_failed", "duplicate_join",
                "wrong_registrant", "signin_required", "registration_required"}
SOFT_PHRASES = {"waiting_room", "not_started", "connecting"}

MEETING_MARKERS = ("leave", "meeting information", "encryption information", "participants", "chat", "mute", "unmute",
                   "start video", "stop video", "share screen", "more meeting control", "audio setting", "security")


def _result(status, detail, confidence, evidence, **extra):
    return {"status": status, "detail": detail, "confidence": confidence, "confirmed": confidence == "high",
            "evidence": evidence, **extra}


# ------------------------------------------------------------------ web client

def classify_web(sig: dict, phrase: tuple | None = None) -> dict:
    """`sig` comes from zoom_web.JS_STATE (DOM presence only). `phrase` is (status, detail) from the visible-text
    phrase table, or None. Order: strong in-meeting evidence beats text phrases (chat can contain any words)."""
    if not sig.get("on_zoom"):
        return _result("unknown", "The browser is not on a Zoom page.", "low", ["url is not zoom.us"])
    evidence = []
    score = 0
    if sig.get("leave_present"):
        score += 2; evidence.append("Leave control present")
    if sig.get("footer_present"):
        score += 1; evidence.append("meeting toolbar present")
    markers = [m for m in (sig.get("labels") or []) if any(k in m.lower() for k in MEETING_MARKERS)]
    if len(markers) >= 2:
        score += 1; evidence.append(f"{len(markers)} meeting controls")
    if sig.get("playing_video"):
        score += 1; evidence.append("a video is playing")
    prejoin = bool(sig.get("prejoin_form"))

    if score >= 3 and not prejoin:
        out = _result("in_meeting", "Zoom's web client is in the meeting (Leave control and toolbar present).", "high", evidence)
        if phrase and phrase[0] in HARD_PHRASES | SOFT_PHRASES:
            out["ignored_text_match"] = phrase[0]            # the same words can appear in chat or a caption
        return out
    if phrase and phrase[0] in HARD_PHRASES:
        return _result(phrase[0], phrase[1], "medium", evidence + [f"page text matches '{phrase[0]}'"])
    if phrase and phrase[0] in ("waiting_room", "not_started"):
        return _result(phrase[0], phrase[1], "medium", evidence + [f"page text matches '{phrase[0]}'"])
    if prejoin:
        return _result("prejoin", "On Zoom's join page - the name/passcode/Join step has not been completed yet.", "high",
                       evidence + ["join form present"])
    if score >= 2:
        return _result("in_meeting", "Zoom's web client looks like it is in the meeting (not fully confirmed).", "medium", evidence)
    if phrase and phrase[0] == "connecting":
        return _result("connecting", phrase[1], "medium", evidence + ["page says it is loading"])
    return _result("connecting", "On a Zoom web client screen, not yet in the meeting.", "low", evidence or ["zoom.us page, no meeting markers"])


# ------------------------------------------------------------------ desktop client

def parse_signals(signals: list[str] | None) -> dict:
    """zoom-status.py prints signals like 'process=yes', 'atspi=pyatspi-not-installed', 'audio=none', 'media=udp:3'."""
    out = {}
    for s in signals or []:
        if "=" in s:
            k, _, v = s.partition("=")
            out[k.strip()] = v.strip()
    return out


def classify_desktop(status: dict, unit_active: bool) -> dict:
    """Re-expresses zoom-status.py's answer with explicit confidence. `status` is its JSON; `unit_active` is zoom.service."""
    sig = parse_signals(status.get("signals"))
    raw = status.get("status") or "unknown"
    process = sig.get("process") == "yes" or unit_active
    atspi_ok = sig.get("atspi") not in (None, "", "pyatspi-not-installed") and not str(sig.get("atspi", "")).startswith("unavailable")
    if not process:
        return _result("not_running", "The Zoom desktop client is not running.", "high", ["no zoom process", "zoom.service inactive"])
    evid = [f"{k}={v}" for k, v in sig.items() if k in ("process", "audio", "media", "atspi")]
    if raw == "in_meeting":
        ev = status.get("evidence") or ("title/controls/audio")
        conf = "high" if ev in ("title", "controls") else "medium"
        return _result("in_meeting", status.get("detail", ""), conf, evid + [f"evidence:{ev}"], desktop_raw=raw)
    if raw == "not_joined" and not atspi_ok:
        # Without AT-SPI the "home screen" claim is a guess: say so rather than present it as fact.
        return _result("unknown", "Zoom is running but its window cannot be read here, so the dashboard cannot tell the home "
                       "screen from a meeting. Look at the Remote screen.", "low", evid + ["AT-SPI unavailable"], desktop_raw=raw)
    return _result(raw, status.get("detail", ""), "medium" if raw in ("not_joined", "connecting") else "medium", evid, desktop_raw=raw)


# ------------------------------------------------------------------ desktop -> web fallback

FALLBACK_MIN_SECONDS = 45          # never decide before the client has had a fair chance
GIVE_UP_WATCHING_SECONDS = 150     # after this, stop watching WITHOUT killing anything: the state is "unconfirmed"


def fallback_decision(desktop: dict, *, elapsed: float, auto_mode: bool, launch_error: str = "") -> dict:
    """What the join_method=auto watcher should do. Returns {"action": wait|fallback|stop_watching|done, "reason": str}.
    A fallback (which stops the desktop client) needs POSITIVE evidence that the desktop path failed:
      * the client process is gone / never started, or it reported a launch error; or
      * it is readable and still at the home screen well after the join was requested.
    "Not confirmed" (unknown/connecting/unreadable) is never a failure: wait, then stop watching and say 'unconfirmed'."""
    st = desktop.get("status")
    if st == "in_meeting":
        return {"action": "done", "reason": "The desktop client is in the meeting."}
    if st in HARD_PHRASES:
        return {"action": "done", "reason": f"The meeting itself refused the join ({st}); a web fallback would hit the same wall."}
    if st in ("waiting_room", "not_started"):
        return {"action": "done", "reason": f"The desktop client reached the meeting ({st}); waiting for the host."}
    if not auto_mode:
        return {"action": "stop_watching", "reason": "join method is not auto"}
    if elapsed < FALLBACK_MIN_SECONDS:
        return {"action": "wait", "reason": "giving the desktop client time to join"}
    if st == "not_running" or launch_error:
        return {"action": "fallback", "reason": launch_error or "the desktop client is not running (it exited or never started)"}
    if st == "not_joined" and desktop.get("confidence") in ("medium", "high"):
        return {"action": "fallback", "reason": "the desktop client is readable and still on its home screen after the join request"}
    if elapsed >= GIVE_UP_WATCHING_SECONDS:
        return {"action": "stop_watching", "reason": "the desktop client is running but its state could not be confirmed - left as it is (unconfirmed)"}
    return {"action": "wait", "reason": f"desktop state is {st or 'unknown'} (not a failure) - still waiting"}
