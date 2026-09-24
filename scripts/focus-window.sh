#!/usr/bin/env bash
# Raises and focuses a specific window on :99 so that keyboard/pointer
# input from the interactive preview lands where the operator expects.
#
#   zoom     the Zoom *meeting/webinar* window if there is one, else
#            Zoom's main window
#   browser  the kiosk Chrome window
#
# Matches on WM_CLASS (stable), not on the window title (changes with
# the page/meeting). Never creates, closes or resizes anything. Prints a
# one-line JSON result with the window id and title actually focused.
set -uo pipefail
export DISPLAY="${DISPLAY:-:99}"

WHICH="${1:-}"
case "$WHICH" in
  zoom)
    WID="$(wmctrl -lx 2>/dev/null | awk 'tolower($3) ~ /^zoom\./ && $0 ~ /Zoom (Meeting|Webinar)/ {print $1; exit}')"
    [[ -z "$WID" ]] && WID="$(wmctrl -lx 2>/dev/null | awk 'tolower($3) ~ /^zoom\./ && $0 ~ /Zoom Workplace/ {print $1; exit}')"
    [[ -z "$WID" ]] && WID="$(wmctrl -lx 2>/dev/null | awk 'tolower($3) ~ /^zoom\./ {print $1; exit}')"
    ;;
  browser)
    WID="$(wmctrl -lx 2>/dev/null | awk 'tolower($3) ~ /(google-chrome|chromium)/ {print $1; exit}')"
    ;;
  *) echo '{"ok": false, "error": "usage: focus-window.sh zoom|browser"}'; exit 2 ;;
esac

if [[ -z "$WID" ]]; then
  echo "{\"ok\": false, \"error\": \"no ${WHICH} window on :99\"}"
  exit 1
fi
wmctrl -i -a "$WID" 2>/dev/null || true
xdotool windowactivate --sync "$WID" 2>/dev/null || true
TITLE="$(wmctrl -l 2>/dev/null | awk -v w="$WID" '$1==w {$1=$2=$3=""; sub(/^ +/,""); print; exit}')"
TITLE="${TITLE//\"/\\\"}"
echo "{\"ok\": true, \"which\": \"$WHICH\", \"window\": \"$WID\", \"title\": \"${TITLE:0:120}\"}"
