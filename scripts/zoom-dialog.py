#!/usr/bin/env python3
"""Lists or dismisses Zoom's pop-up dialogs ("The webinar has expired",
"You have been removed", passcode prompts, ...) so a dead dialog never
just sits on the canvas.

  list     -> JSON: the dialogs currently showing, their text and buttons
  dismiss  -> presses each dialog's own neutral button (OK / Close /
              Got it / Dismiss / Done / Cancel, then Leave) through the
              AT-SPI Action interface - no coordinates, no guessing where
              the button is. Falls back to Escape, then a WM close request.

Only windows that are *not* the main Zoom window / meeting window are
touched, and only "safe" buttons are pressed: a "Leave" is used only
when it is the dialog's sole way out (an error dialog), never to leave
a live meeting - "Leave Meeting" confirmation dialogs are closed with
Cancel. Never presses anything named Join/Allow/Admit/Share/Record.

Output (one JSON line): {"dialogs": [...], "dismissed": [...], "remaining": [...]}
"""
import json
import os
import re
import subprocess
import sys
import time

os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS",
                      f"unix:path={os.environ.get('XDG_RUNTIME_DIR', '/run/user/0')}/bus")
os.environ.setdefault("DISPLAY", ":99")

ACTION = (sys.argv[1] if len(sys.argv) > 1 else "list").lower()
if ACTION not in ("list", "dismiss"):
    print(json.dumps({"error": "usage: zoom-dialog.py list|dismiss"})); sys.exit(2)

try:
    import pyatspi
except ImportError:
    pyatspi = None   # window-manager-only fallback below

MAIN_TITLES = re.compile(r"^(zoom workplace|zoom meeting.*|zoom webinar.*|zoom|zoom cloud meetings|)$", re.I)
SAFE_ORDER = ["ok", "close", "got it", "dismiss", "done", "cancel", "no", "not now", "later", "leave"]
NEVER = re.compile(r"\b(join|allow|admit|share|record|start|unmute|enable|accept|sign in|open)\b", re.I)
BUTTON_ROLES = {"push button", "toggle button"}
# Buttons that only exist on a live meeting/webinar UI. A secondary Zoom
# window carrying any of them (floating toolbar, Q&A/participants panel)
# is part of the meeting, not a dialog - its "Leave"/"Close" must never be
# pressed as if it were an error pop-up.
MEETING_UI = re.compile(r"^(raise hand|lower hand|q ?& ?a|(un)?mute( (my )?(audio|mic(rophone)?))?|(start|stop)( my)? video|"
                        r"join audio|reactions?|participants?|attendees?|panelists?|share( screen)?|end( meeting| webinar)?( for all)?)$")
_norm = lambda s: re.sub(r"\s*\(.*?\)\s*", " ", s or "").strip().lower()


def find_zoom_app():
    d = pyatspi.Registry.getDesktop(0)
    for i in range(d.childCount):
        a = d.getChildAtIndex(i)
        if a and a.name and "zoom" in a.name.lower():
            return a
    return None


def showing(n):
    try:
        return n.getState().contains(pyatspi.STATE_SHOWING)
    except Exception:
        return False


def walk(node, fn, depth=0):
    if depth > 30 or node is None:
        return
    try:
        fn(node)
        for i in range(node.childCount):
            walk(node.getChildAtIndex(i), fn, depth + 1)
    except Exception:
        pass


def scan():
    app = find_zoom_app()
    if not app:
        return []
    dialogs = []
    for j in range(app.childCount):
        w = app.getChildAtIndex(j)
        if w is None or not showing(w):
            continue
        title = w.name or ""
        if MAIN_TITLES.match(title.strip()):
            continue
        texts, buttons = [], []
        meeting_ui = []
        def collect(n):
            role = n.getRoleName()
            name = (n.name or "").strip()
            if role in BUTTON_ROLES and name:
                buttons.append((name, n))
                if MEETING_UI.match(_norm(name)):
                    meeting_ui.append(name)
            elif role == "label" and name and name != "window_ta_label":
                texts.append(re.sub(r"<br\s*/?>", " ", name).strip())
        walk(w, collect)
        if meeting_ui:
            continue
        dialogs.append({"title": title, "text": " / ".join(dict.fromkeys(texts))[:400],
                        "buttons": list(dict.fromkeys(b[0] for b in buttons)), "_nodes": buttons})
    return dialogs


def public(d):
    return {k: v for k, v in d.items() if not k.startswith("_")}


def press(node):
    try:
        act = node.queryAction()
        for i in range(act.nActions):
            if act.getName(i).lower() in ("press", "click", "activate"):
                return bool(act.doAction(i))
        return bool(act.doAction(0)) if act.nActions else False
    except Exception:
        return False


def wm_windows_titled(title):
    try:
        out = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    ids = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[3].strip() == title.strip():
            ids.append(parts[0])
    return ids


# ---- window-manager-only fallback (no python3-pyatspi): list and close
# small non-main Zoom windows with Escape, then a WM close request. Cannot
# read dialog text or press a specific button, so it is deliberately
# narrower: dialog-sized windows only, and never anything that looks like
# part of a live meeting (panels, floating toolbar/video).
MEETING_PANEL = re.compile(r"chat|participants|attendees|panelists|q ?& ?a|poll|reaction|share|whiteboard|caption|toolbar|float|video|control|notes|summary", re.I)


def scan_wm():
    try:
        out = subprocess.run(["wmctrl", "-lGx"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    found = []
    for line in out.splitlines():
        parts = line.split(None, 8)
        if len(parts) < 9 or not parts[6].lower().startswith("zoom"):
            continue
        title = parts[8].strip()
        w, h = int(parts[4]), int(parts[5])
        if MAIN_TITLES.match(title) or MEETING_PANEL.search(title) or w > 900 or h > 700:
            continue
        found.append({"title": title, "text": "", "buttons": [], "_wid": parts[0], "_nodes": []})
    return found


def dismiss_wm(d):
    subprocess.run(["xdotool", "key", "--window", d["_wid"], "Escape"], capture_output=True, timeout=5)
    time.sleep(0.4)
    if any(x["_wid"] == d["_wid"] for x in scan_wm()):
        subprocess.run(["wmctrl", "-i", "-c", d["_wid"]], capture_output=True, timeout=5)
        time.sleep(0.4)
        via = "wm:close"
    else:
        via = "key:Escape"
    return via, any(x["_wid"] == d["_wid"] for x in scan_wm())


if pyatspi is None:
    dialogs = scan_wm()
    result = {"dialogs": [public(d) for d in dialogs], "dismissed": [], "remaining": [], "mode": "wm-only (python3-pyatspi not installed)"}
    if ACTION == "dismiss":
        for d in dialogs:
            via, still = dismiss_wm(d)
            (result["remaining"] if still else result["dismissed"]).append({"title": d["title"], "via": via})
    print(json.dumps(result, ensure_ascii=False)); sys.exit(0)

dialogs = scan()
result = {"dialogs": [public(d) for d in dialogs], "dismissed": [], "remaining": []}
if ACTION == "list" or not dialogs:
    print(json.dumps(result, ensure_ascii=False)); sys.exit(0)

for d in dialogs:
    via = None
    # 1) the dialog's own neutral button, safest first
    for want in SAFE_ORDER:
        for name, node in d["_nodes"]:
            n = _norm(name)
            if n == want and not (want == "leave" and any(_norm(b) in ("cancel", "no") for b, _ in d["_nodes"])):
                if NEVER.search(n) and n != "leave":
                    continue
                if press(node):
                    via = f"button:{name}"
                    break
        if via:
            break
    # 2) Escape to the window, 3) WM close request
    if not via:
        for wid in wm_windows_titled(d["title"]):
            subprocess.run(["xdotool", "key", "--window", wid, "Escape"], capture_output=True, timeout=5)
            via = "key:Escape"
    time.sleep(0.4)
    still = [x for x in scan() if x["title"] == d["title"]]
    if still and via and via.startswith("key"):
        for wid in wm_windows_titled(d["title"]):
            subprocess.run(["wmctrl", "-i", "-c", wid], capture_output=True, timeout=5)
        via = "wm:close"
        time.sleep(0.4)
        still = [x for x in scan() if x["title"] == d["title"]]
    (result["remaining"] if still else result["dismissed"]).append({"title": d["title"], "via": via})

print(json.dumps(result, ensure_ascii=False))
