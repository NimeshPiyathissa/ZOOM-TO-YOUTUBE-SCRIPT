#!/usr/bin/env python3
"""Best-effort Zoom meeting status from what is visible on :99.

Signals, in order of trust: (1) whether a zoom process exists at all,
(2) window titles from wmctrl (Zoom's titles are reasonably stable:
"Zoom Meeting", "Zoom Webinar", dialog titles for waiting room /
passcode / ended), (3) when the client was launched with AT-SPI enabled
(see zoom-atspi.py), the visible text of its windows for phrases like
"Please wait for the host to start this meeting".

Nothing here is authoritative - Zoom has no local API for this. The
dashboard shows the status with that caveat and the raw signals, so a
wrong guess is inspectable rather than silently trusted.

Prints one JSON line: {"status": ..., "detail": ..., "signals": [...],
"authoritative": false, "action": "<what the admin should do>"}
"""
import json
import os
import re
import subprocess

os.environ.setdefault("DISPLAY", ":99")

PHRASES = [
    # (status, regex on lowercased title/text, detail, suggested action)
    # Calibrated against the real client on this box (2026-09-18): an
    # expired webinar showed a dialog titled "Leave meeting" with the
    # text "Unable to join this webinar / The webinar has expired.
    # (Error code: 3038) / Meeting ID: ..." while the main window was at
    # the home screen, titled "Zoom Workplace" (not "Zoom Meeting").
    ("expired",      r"(meeting|webinar) has expired|error code: ?3038",
                     "The webinar/meeting link has expired (Zoom error 3038) - the event is over, or the host hasn't opened it yet.",
                     "Dismiss the dialog and quit Zoom (it's just covering the canvas). Rejoin when the host's event is actually live, or pick a fresh link."),
    # duplicate_join/wrong_registrant: added for per-registrant (tk=)
    # links, uncalibrated - unlike the phrases above, these haven't been
    # matched against a real Zoom dialog yet. Falls through to
    # join_failed until verified against a real duplicate/mismatched
    # join; still actionable meanwhile, just less specific.
    ("duplicate_join", r"already (joined|in|used) this|you('| a)re already in this meeting|link (has|was) already been used",
                     "Zoom says this link/token has already been used to join - a registrant link is single-use.",
                     "Re-register for a fresh personal link rather than reusing this one."),
    ("wrong_registrant", r"not (a valid|the correct) registrant|registrant information does not match|invitation is not valid for you",
                     "Zoom doesn't recognize this session as the registrant this link was issued for.",
                     "Check the link was saved for the right registrant/Gmail address (Zoom page shows a warning if they don't match); re-register if needed."),
    ("join_failed",  r"unable to join|error code: ?\d+|invalid meeting id|meeting id is not valid",
                     "Zoom could not join - it is showing an error dialog.",
                     "Check the link/passcode, dismiss the dialog, then Rejoin."),
    ("removed",      r"removed (you )?from (the|this) meeting|host has removed you",
                     "The host removed the bot from the meeting.",
                     "Ask the host to re-admit the bot, then Rejoin."),
    ("ended",        r"meeting has (been )?ended|this meeting has ended|webinar has ended|host ended",
                     "The meeting/webinar has ended.",
                     "Nothing to do unless it's restarting - then Rejoin."),
    ("passcode_required", r"enter (the )?(meeting )?passcode|passcode is (incorrect|invalid)|wrong passcode",
                     "Zoom is asking for a passcode (or rejected the one it was given).",
                     "Set the correct passcode on this source (or use a link with pwd=), then Rejoin."),
    ("locked",       r"meeting is locked|has locked the meeting|locked by the host",
                     "The host has locked the meeting - nobody else can join right now.",
                     "Ask the host to unlock it (or admit the bot), then Rejoin."),
    ("signin_required", r"sign in to join|signed.in users|authenticated (users|attendees) only|requires (you )?to sign in|only authorized attendees|sign in with the email",
                     "This meeting only admits signed-in Zoom users.",
                     "Set this meeting to join with a Google account (Accounts page), complete the sign-in on the remote screen, then Rejoin."),
    ("registration_required", r"registration is required|register for this (webinar|meeting)|please register",
                     "This webinar requires registration - the link used isn't a registrant link.",
                     "Open the registration page, complete it, and save the personal join link on this source."),
    ("waiting_room", r"waiting room|please wait,? the (meeting )?host will let you in|host will let you in soon",
                     "The bot is in the waiting room.",
                     "Ask the host to admit the bot."),
    ("not_started",  r"waiting for the host to start|wait for the host to start|host has not started|hasn.t started",
                     "The host hasn't started the meeting yet.",
                     "Wait - Zoom joins automatically when the host starts. Rejoin if it doesn't."),
    ("connecting",   r"^connecting|joining meeting|joining webinar|please wait\.\.\.",
                     "Zoom is still connecting.",
                     "Give it a few seconds."),
]


def run(argv, timeout=8):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def emit(status, detail, action, **extra):
    base = {"status": status, "detail": detail, "signals": signals, "authoritative": False,
            "action": action, "dialogs": dialogs, "terminal": status in TERMINAL}
    base.update(extra)
    print(json.dumps(base, ensure_ascii=False))
    raise SystemExit(0)


# States where Zoom is *not going to recover by itself* - the dashboard
# surfaces these on the program panel with the action, whatever source
# is active, instead of leaving a dialog on the canvas.
TERMINAL = {"expired", "duplicate_join", "wrong_registrant", "join_failed", "removed", "ended",
            "passcode_required", "registration_required", "waiting_room", "not_started", "locked",
            "signin_required"}

signals = []
dialogs = []
zoom_running = bool(run(["pgrep", "-x", "zoom"]).strip())
signals.append(f"process={'yes' if zoom_running else 'no'}")
if not zoom_running:
    emit("not_joined", "Zoom isn't running.", "Use Join to start it.")

# Window titles, and which of them are Zoom pop-up dialogs (class zoom,
# title that isn't the home/meeting window) - those are what "dismiss
# dialog" acts on.
titles = []
for line in run(["wmctrl", "-lx"]).splitlines():
    parts = line.split(None, 4)
    if len(parts) == 5:
        wid, _, wclass, _, title = parts
        titles.append(title)
        if wclass.lower().startswith("zoom") and not re.match(r"^(zoom workplace|zoom meeting|zoom webinar|zoom)\b", title.strip(), re.I):
            dialogs.append(title.strip())
signals.append("titles=" + " | ".join(titles)[:300])

texts = ""
atspi_raw = run(["/usr/bin/python3", os.path.join(os.path.dirname(__file__), "zoom-atspi.py"), "text"], timeout=12)
try:
    atspi = json.loads(atspi_raw.strip().splitlines()[-1]) if atspi_raw.strip() else {}
except (json.JSONDecodeError, IndexError):
    atspi = {}
if atspi.get("available"):
    texts = atspi.get("text", "")
    signals.append("atspi=yes")
else:
    signals.append(f"atspi={atspi.get('reason') or 'no'}")

haystack = " || ".join(titles + [texts]).lower()
for status, rx, detail, action in PHRASES:
    m = re.search(rx, haystack)
    if m:
        # Quote the actual dialog sentence(s) around the match so the
        # admin sees Zoom's own words (error code, meeting id), not just
        # our label for it.
        quote = " / ".join(
            re.sub(r"<br\s*/?>", " ", chunk).strip()
            for chunk in texts.split("\t") if re.search(rx, chunk.lower())
        )[:240]
        emit(status, detail + (f" Zoom says: \"{quote}\"" if quote else ""), action)

if any(re.search(r"zoom (meeting|webinar)", t, re.I) for t in titles):
    emit("in_meeting", "A Zoom meeting/webinar window is open.", "")
elif re.search(r"join a meeting|new meeting", texts, re.I) or any(t.strip() == "Zoom Workplace" for t in titles):
    emit("not_joined", "Zoom is open at its home screen - not in a meeting.", "Use Join / Rejoin.")
elif titles:
    emit("unknown", "Zoom is running but no meeting window is recognizable.",
         "Look at the remote desktop - a dialog may be waiting for input.")
else:
    emit("connecting", "Zoom is running with no window yet.", "Give it a few seconds.")
