#!/usr/bin/env bash
# $BROWSER handler for zoombot's desktop session (set via
# Environment=BROWSER= in systemd/zoom.service). Zoom's "Sign in with
# Google" / SSO hands its OAuth URL to xdg-open, which honors $BROWSER
# when no desktop environment is running (this Openbox session). Without
# this, xdg-open picked google-chrome-stable.desktop - i.e. Chrome's
# *default* profile, a third profile unrelated to any dashboard account.
#
# Opens the URL in the Chrome profile of the account bound to the active
# source (ACCOUNT_PROFILE_ID in current-source.env, written by the
# dashboard), falling back to the shared stream profile if none is bound.
# No debug port here either - see chrome-account.sh for why.
set -uo pipefail
cd /
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
URL="${1:-}"
case "$URL" in
  http://*|https://*) ;;
  *) exit 1 ;;   # only web URLs; never zoommtg:// or file:// through here
esac

APP_DIR="$HOME/zoom-stream"
PROFILE_ID=""
if [[ -f "$APP_DIR/current-source.env" ]]; then
  # Same conservative KEY="value" reader as lib.sh - no source/eval.
  PROFILE_ID="$(sed -n 's/^ACCOUNT_PROFILE_ID="\{0,1\}\([a-z0-9-]*\)"\{0,1\}$/\1/p' "$APP_DIR/current-source.env" | head -n1)"
fi
if [[ -n "$PROFILE_ID" && -d "$HOME/.config/chrome-profiles/$PROFILE_ID" ]]; then
  DIR="$HOME/.config/chrome-profiles/$PROFILE_ID"
else
  DIR="$HOME/.config/stream-chrome-profile"
fi

CHROME_BIN="$(command -v google-chrome-stable || command -v google-chrome || echo /usr/bin/google-chrome-stable)"
exec "$CHROME_BIN" --user-data-dir="$DIR" --password-store=basic \
  --no-first-run --no-default-browser-check \
  --noerrdialogs --disable-infobars "$URL"
