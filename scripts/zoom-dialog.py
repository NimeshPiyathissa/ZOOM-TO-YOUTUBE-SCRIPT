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
    print(json.dumps({"error": "pyatspi-not-installed", "dialogs": []})); sys.exit(0)

MAIN_TITLES = re.compile(r"^(zoom workplace|zoom meeting.*|zoom webinar.*|zoom|zoom cloud meetings|)$", re.I)
SAFE_ORDER = ["ok", "close", "got it", "dismiss", "done", "cancel", "no", "not now", "later", "leave"]
NEVER = re.compile(r"\b(join|allow|admit|share|record|start|unmute|enable|accept|sign in|open)\b", re.I)
BUTTON_ROLES = {"push button", "toggle button"}
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
        def collect(n):
            role = n.getRoleName()
            name = (n.name or "").strip()
            if role in BUTTON_ROLES and name:
                buttons.append((name, n))
            elif role == "label" and name and name != "window_ta_label":
                texts.append(re.sub(r"<br\s*/?>", " ", name).strip())
        walk(w, collect)
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
