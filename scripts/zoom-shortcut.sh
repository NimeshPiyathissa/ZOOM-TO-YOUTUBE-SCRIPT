#!/usr/bin/env bash
# Sends one of Zoom's own keyboard shortcuts to the Zoom meeting window
# specifically (never a blind global keypress - that could land on
# whatever else has focus). Allowlisted actions only; the dashboard
# reads back the resulting state separately (zoom-atspi.py) rather than
# assuming the shortcut took.
#
#   mic           Alt+A   mute / unmute microphone
#   camera        Alt+V   start / stop video
#   view-speaker  Alt+F1  switch to speaker view
#   view-gallery  Alt+F2  switch to gallery view
set -euo pipefail
export DISPLAY="${DISPLAY:-:99}"

case "${1:-}" in
  mic)          KEY="alt+a" ;;
  camera)       KEY="alt+v" ;;
  view-speaker) KEY="alt+F1" ;;
  view-gallery) KEY="alt+F2" ;;
  *) echo "usage: $(basename "$0") mic|camera|view-speaker|view-gallery" >&2; exit 2 ;;
esac

WID="$(wmctrl -l 2>/dev/null | awk '/Zoom Meeting|Zoom Webinar|Zoom Workplace|Zoom - /{print $1; exit}')"
if [[ -z "$WID" ]]; then
  echo "Zoom meeting window not found - is zoom.service running and joined?" >&2
  exit 1
fi

xdotool windowactivate --sync "$WID"
sleep 0.15
xdotool key --window "$WID" --clearmodifiers "$KEY"
echo "sent $KEY to window $WID"
