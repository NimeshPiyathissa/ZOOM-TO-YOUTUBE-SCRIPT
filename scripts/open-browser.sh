#!/usr/bin/env bash
# Opens a web page (default: Google) on :99 in the Chrome profile that
# holds the operator's Google session, so the page comes up already
# signed in. Used by the dashboard's /remote "Browser" action.
#
# Profile choice mirrors browser-source.sh / open-url-with-account.sh:
# the account profile bound to the active source when there is one,
# otherwise the shared stream profile (~/.config/stream-chrome-profile).
#
# Two situations, decided by whether that profile is already open:
#   * another Chrome (the kiosk from browser-source.sh, or an earlier
#     window from here) holds the profile -> hand the URL to it. Chrome's
#     singleton opens it as a new window in that instance and this process
#     exits at once. (The dashboard prefers navigating the kiosk tab over
#     DevTools; this is the fallback when that isn't reachable.)
#   * nothing holds the profile -> launch an ordinary Chrome window (no
#     kiosk, no DevTools port - see chrome-account.sh for why a debug port
#     is avoided anywhere a Google sign-in might happen). Its pid goes to
#     $DIR/.window.pid so browser-source.sh can close it before it starts
#     the kiosk on the same profile.
#
# NO CREDENTIAL AUTOMATION: nothing here types, stores or reads a
# password, cookie or token. Signed-in state is whatever the human left
# in the profile. Prints exactly one JSON line.
set -uo pipefail
cd /
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
export DISPLAY="${DISPLAY:-:99}"

URL="${1:-https://www.google.com/}"
case "$URL" in
  http://*|https://*) ;;
  *) echo '{"ok": false, "error": "only http(s) URLs can be opened"}'; exit 1 ;;
esac

APP_DIR="$HOME/zoom-stream"
LOG_DIR="$APP_DIR/logs"
CHROME_BIN="$(command -v google-chrome-stable || command -v google-chrome || echo /usr/bin/google-chrome-stable)"
[[ -x "$CHROME_BIN" ]] || { echo '{"ok": false, "error": "Chrome binary not found"}'; exit 1; }

PROFILE_ID=""
if [[ -f "$APP_DIR/current-source.env" ]]; then
  # Same conservative KEY="value" reader as lib.sh - no source/eval.
  PROFILE_ID="$(sed -n 's/^ACCOUNT_PROFILE_ID="\{0,1\}\([a-z0-9-]*\)"\{0,1\}$/\1/p' "$APP_DIR/current-source.env" | head -n1)"
fi
if [[ -n "$PROFILE_ID" && -d "$HOME/.config/chrome-profiles/$PROFILE_ID" ]]; then
  DIR="$HOME/.config/chrome-profiles/$PROFILE_ID"
  PROFILE_LABEL="$PROFILE_ID"
else
  DIR="$HOME/.config/stream-chrome-profile"
  PROFILE_LABEL="shared"
fi
mkdir -p "$DIR" "$LOG_DIR"
PIDFILE="$DIR/.window.pid"

# Chrome leaves SingletonLock -> "host-pid" while any Chrome has the
# profile open; stale after a crash, so check the pid is alive.
holder_pid() {
  local target pid
  [[ -L "$DIR/SingletonLock" ]] || return 1
  target="$(readlink "$DIR/SingletonLock" 2>/dev/null || true)"
  pid="${target##*-}"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null && echo "$pid"
}

focus_browser() {
  # Best-effort: raise whichever Chrome window is newest so the page the
  # operator asked for is what the preview (and the stream) shows.
  local wid
  for _ in $(seq 1 20); do
    wid="$(wmctrl -lx 2>/dev/null | awk 'tolower($3) ~ /(google-chrome|chromium)/ {print $1}' | tail -n1)"
    [[ -n "$wid" ]] && break
    sleep 0.25
  done
  [[ -n "$wid" ]] || return 0
  wmctrl -i -a "$wid" 2>/dev/null || true
  xdotool windowactivate --sync "$wid" 2>/dev/null || true
}

if HOLDER="$(holder_pid)"; then
  # Hand-off: the running instance opens the URL in a new window.
  "$CHROME_BIN" --user-data-dir="$DIR" --password-store=basic --new-window "$URL" >>"$LOG_DIR/browser-open.log" 2>&1 || true
  focus_browser
  echo "{\"ok\": true, \"mode\": \"handoff\", \"profile\": \"$PROFILE_LABEL\", \"holder_pid\": $HOLDER}"
  exit 0
fi

rm -f "$DIR/SingletonLock" "$DIR/SingletonSocket" "$DIR/SingletonCookie"
nohup "$CHROME_BIN" \
  --user-data-dir="$DIR" --password-store=basic \
  \
  --no-first-run --no-default-browser-check \
  --start-maximized --window-position=0,0 \
  --noerrdialogs --disable-infobars --disable-session-crashed-bubble \
  --disable-features=Translate,TranslateUI,Notifications \
  --lang=en-US \
  "$URL" >>"$LOG_DIR/browser-open.log" 2>&1 &
PID=$!
echo "$PID" > "$PIDFILE"
sleep 1
if ! kill -0 "$PID" 2>/dev/null; then
  rm -f "$PIDFILE"
  echo '{"ok": false, "error": "Chrome exited immediately after launch (see logs/browser-open.log)"}'
  exit 1
fi
focus_browser
echo "{\"ok\": true, \"mode\": \"launched\", \"profile\": \"$PROFILE_LABEL\", \"pid\": $PID}"
