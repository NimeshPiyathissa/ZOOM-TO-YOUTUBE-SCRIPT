#!/usr/bin/env python3
"""Reads from the Zoom client's accessibility (AT-SPI) tree - the only
local, non-guessing source of Zoom's own UI state on Linux.

  mic      -> the meeting toolbar's Mute/Unmute button
  camera   -> the Start/Stop Video button
  text     -> all visible strings (for zoom-status.py's phrase matching)
  snapshot -> text + whether the visible UI is a meeting/webinar -
              one tree walk, used by zoom-status.py
  buttons  -> every button in every *showing* Zoom window, with state -
              the calibration dump: run it once during a real meeting to
              see exactly how this Zoom build names its toolbar controls.

Requires zoom.service launched with QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
(systemd/zoom.service); without it Zoom exposes nothing at all.
Confirmed on this box (2026-09-19, Zoom 7.1.5): the tree is live - dialog
text and buttons (e.g. the "Leave meeting" error dialog's OK/Leave) are
readable and actionable. The in-meeting toolbar names below ("Mute",
"Unmute", "Start Video", "Stop Video", "Join Audio") are Zoom's visible
button labels, which Qt uses as the accessible name; they are matched
loosely and the raw name is always returned so a mismatch is visible
rather than silently wrong. State is never inferred from a keystroke.

Output (one JSON line):
  mic/camera: {"available": bool, "state": "muted"|"unmuted"|"no_audio"|
               "on"|"off"|"unknown", "name": <raw>, "description": <raw>,
               "reason": <why unavailable>}
  text:       {"available": true, "text": "<tab-joined strings>"}
  snapshot:   {"available": true, "text": ..., "in_meeting": bool,
               "kind": "meeting"|"webinar"|"", "attendee": bool,
               "controls": [<matched toolbar labels>], "windows": [<titles>]}
  buttons:    {"available": true, "windows": [{"title":..,"buttons":[..]}]}
"""
import json
import os
import re
import sys

os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS",
                      f"unix:path={os.environ.get('XDG_RUNTIME_DIR', '/run/user/0')}/bus")
QUERY = (sys.argv[1] if len(sys.argv) > 1 else "mic").lower()
if QUERY not in ("mic", "camera", "text", "buttons", "snapshot"):
    print(json.dumps({"available": False, "reason": "bad-query"})); sys.exit(2)


def out(obj):
    print(json.dumps(obj, ensure_ascii=False)); sys.exit(0)


try:
    import pyatspi
except ImportError:
    out({"available": False, "reason": "pyatspi-not-installed"})

MAX_DEPTH = 30
BUTTON_ROLES = {"push button", "toggle button", "check box", "radio button", "menu item"}
# Strip Zoom's "(Alt+A)"-style shortcut hints and surrounding noise
# before matching, keep the raw string for display.
_norm = lambda s: re.sub(r"\s*\(.*?\)\s*", " ", s or "").strip().lower()


def find_zoom_app():
    desktop = pyatspi.Registry.getDesktop(0)
    for i in range(desktop.childCount):
        app = desktop.getChildAtIndex(i)
        if app and app.name and "zoom" in app.name.lower():
            return app
    return None


def showing(node):
    try:
        return node.getState().contains(pyatspi.STATE_SHOWING)
    except Exception:
        return False


def walk(node, fn, depth=0):
    if depth > MAX_DEPTH or node is None:
        return
    try:
        if fn(node) is False:
            return
        for i in range(node.childCount):
            walk(node.getChildAtIndex(i), fn, depth + 1)
    except Exception:
        pass


def describe_button(node):
    st = node.getState()
    return {
        "role": node.getRoleName(),
        "name": node.name or "",
        "description": (node.description or "") if hasattr(node, "description") else "",
        "checked": st.contains(pyatspi.STATE_CHECKED),
        "pressed": st.contains(pyatspi.STATE_PRESSED),
        "sensitive": st.contains(pyatspi.STATE_SENSITIVE),
    }


app = find_zoom_app()
if not app:
    out({"available": False, "reason": "zoom-not-exposed"})

windows = []
for j in range(app.childCount):
    w = app.getChildAtIndex(j)
    if w is not None and showing(w):
        windows.append(w)

if QUERY == "text":
    seen, strings = set(), []
    def collect(node):
        try:
            name = (node.name or "").strip()
        except Exception:
            return
        if name and name not in seen and len(name) < 300:
            seen.add(name); strings.append(name)
    for w in windows:
        walk(w, collect)
    out({"available": True, "text": "\t".join(strings)[:6000]})

if QUERY == "snapshot":
    # Webinars (and the 7.x client generally) keep the main window titled
    # "Zoom Workplace" while in a meeting, so the title can't be what says
    # "in a meeting". The toolbar can: these labels only exist once a
    # meeting/webinar UI is up. "Chat"/"Settings"/"More" are deliberately
    # absent - the home screen's left rail has all three.
    MEETING_CONTROLS = re.compile(
        r"^(leave( meeting| webinar)?|end( meeting| webinar)?( for all)?|"
        r"raise hand|lower hand|q ?& ?a|"
        r"(un)?mute( (my )?(audio|mic(rophone)?))?|(start|stop)( my)? video|join audio|"
        r"reactions?|participants?|attendees?|panelists?)$")
    # Mic/camera exist for panelists and meeting participants, never for
    # a webinar attendee - their absence next to Q&A/Raise hand is how an
    # attendee view is told apart from a toolbar that merely isn't readable.
    AV_CONTROLS = re.compile(r"^((un)?mute( (my )?(audio|mic(rophone)?))?|(start|stop)( my)? video|join audio)$")
    seen, strings, controls = set(), [], []
    def collect_snap(node):
        try:
            name = (node.name or "").strip()
            role = node.getRoleName()
        except Exception:
            return
        if not name or len(name) >= 300:
            return
        if name not in seen:
            seen.add(name); strings.append(name)
        if role in BUTTON_ROLES and MEETING_CONTROLS.match(_norm(name)) and name not in controls:
            controls.append(name)
    for w in windows:
        walk(w, collect_snap)
    norm = [_norm(c) for c in controls]
    has_av = any(AV_CONTROLS.match(n) for n in norm)
    webinar_ui = any(re.match(r"^(q ?& ?a|raise hand|lower hand)$", n) for n in norm)
    text = "	".join(strings)
    in_meeting = bool(controls)
    kind = ""
    if in_meeting:
        kind = "webinar" if (webinar_ui and not has_av) or re.search(r"\bwebinar\b", text.lower()) else "meeting"
    out({"available": True, "text": text[:6000], "in_meeting": in_meeting, "kind": kind,
         "attendee": in_meeting and webinar_ui and not has_av,
         "controls": controls[:20], "windows": [w.name or "" for w in windows]})

if QUERY == "buttons":
    dump = []
    for w in windows:
        entry = {"title": w.name or "", "role": w.getRoleName(), "buttons": []}
        def collect(node, entry=entry):
            try:
                if node.getRoleName() in BUTTON_ROLES and (node.name or ""):
                    entry["buttons"].append(describe_button(node))
            except Exception:
                pass
        walk(w, collect)
        dump.append(entry)
    out({"available": True, "windows": dump})

# --- mic / camera: scan buttons in showing windows, first match wins.
# Ordered so the *meeting* window is preferred over the home window.
MATCH = {
    "mic": [
        (re.compile(r"^unmute( (my )?(audio|mic(rophone)?))?$"), "muted"),
        (re.compile(r"^mute( (my )?(audio|mic(rophone)?))?$"), "unmuted"),
        (re.compile(r"^join audio"), "no_audio"),
        (re.compile(r"currently (un)?muted"), None),   # description-style
    ],
    "camera": [
        (re.compile(r"^start( my)? video$"), "off"),
        (re.compile(r"^stop( my)? video$"), "on"),
    ],
}[QUERY]

found = []
def check(node):
    try:
        if node.getRoleName() not in BUTTON_ROLES:
            return
        raw = node.name or ""
        desc = node.description if hasattr(node, "description") else ""
        for rx, state in MATCH:
            for candidate in (_norm(raw), _norm(desc)):
                if candidate and rx.search(candidate):
                    if state is None:  # "currently muted/unmuted" phrasing
                        state = "muted" if "unmuted" not in candidate else "unmuted"
                    found.append((state, raw, desc, node))
                    return False
    except Exception:
        return

def window_rank(w):
    t = (w.name or "").lower()
    return 0 if re.search(r"zoom (meeting|webinar)", t) else (2 if t == "zoom workplace" else 1)

for w in sorted(windows, key=window_rank):
    walk(w, check)
    if found:
        break

if not found:
    out({"available": False, "reason": "control-not-found",
         "detail": "no Mute/Unmute/Start Video/Stop Video button in any visible Zoom window - "
                   "not in a meeting, or the toolbar is hidden"})

state, raw, desc, node = found[0]
res = {"available": True, "state": state, "name": raw, "description": desc or ""}
# A toggle-style button carries CHECKED/PRESSED; report it so the label
# reading can be cross-checked from the dashboard.
try:
    st = node.getState()
    res["checked"] = st.contains(pyatspi.STATE_CHECKED)
    res["pressed"] = st.contains(pyatspi.STATE_PRESSED)
except Exception:
    pass
out(res)
