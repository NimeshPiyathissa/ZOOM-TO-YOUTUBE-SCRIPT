#!/usr/bin/env bash
# "Meeting only" full-frame mode, window-level half: true window-manager
# fullscreen on the Zoom meeting window (Openbox honors EWMH
# _NET_WM_STATE_FULLSCREEN via wmctrl), which removes the WM's own
# title bar/border - distinct from Zoom's in-app "Alt+F" fullscreen,
# which only resizes Zoom's own window and isn't reliably read back.
# wmctrl's add/remove (not toggle) make this idempotent: calling "on"
# twice, or "off" when already off, is a harmless no-op either way.
#
# "on" also parks the pointer on an inert spot: Zoom keeps its floating
# toolbar / top bar up for as long as the pointer rests on them (the VNC
# session's last pointer position was right on the bottom toolbar), and
# only auto-hides them once the pointer is elsewhere. The encoder crop
# (stream.sh) is the backstop for whatever Zoom still draws.
#
# What this does NOT do: force-close the chat/participants panels, or
# touch Zoom's "Always show meeting controls" setting - see
# app/control.py's zoom_set_meeting_only() docstring for why (no
# reliable readable state for either, so a blind shortcut could easily
# make things worse, not better).
set -euo pipefail
export DISPLAY="${DISPLAY:-:99}"
ACTION="${1:-}"

if [[ "$ACTION" != "on" && "$ACTION" != "off" && "$ACTION" != "park" ]]; then
  echo "usage: $(basename "$0") on|off|park" >&2
  exit 2
fi

# The main window, not a dialog: Zoom 7.x keeps the title "Zoom Workplace"
# at the home screen and (webinars included) in a meeting, so match on the
# window class and take the largest one big enough to be the main window,
# preferring an explicit "Zoom Meeting"/"Zoom Webinar" title if one exists.
WID="$(wmctrl -lGx 2>/dev/null | awk '
  tolower($7) ~ /^zoom\./ && $5 >= 800 && $6 >= 500 {
    area = $5 * $6
    if ($0 ~ /Zoom (Meeting|Webinar)/) area += 1e9
    if (area > best) { best = area; id = $1 }
  }
  END { if (id != "") print id }')"
if [[ -z "$WID" ]]; then
  echo "Zoom meeting window not found - is zoom.service running and joined?" >&2
  exit 1
fi

is_fullscreen() {
  xprop -id "$WID" _NET_WM_STATE 2>/dev/null | grep -q "_NET_WM_STATE_FULLSCREEN"
}

park_pointer() {
  # Left edge, mid-height: away from the bottom toolbar, the top bar and
  # the right-hand panel arrow, so Zoom's auto-hide timers can run out.
  local h; h="$(xdotool getdisplaygeometry 2>/dev/null | awk '{print int($2/2)}')"
  xdotool mousemove 2 "${h:-540}" 2>/dev/null && echo "pointer parked" || echo "pointer not moved" >&2
}

if [[ "$ACTION" == "park" ]]; then
  park_pointer
  exit 0
fi

if [[ "$ACTION" == "on" ]]; then
  if is_fullscreen; then
    echo "already fullscreen"
  else
    wmctrl -i -r "$WID" -b add,fullscreen
    sleep 0.2
    if is_fullscreen; then
      echo "fullscreen on"
    else
      echo "sent fullscreen request, but window manager did not confirm _NET_WM_STATE_FULLSCREEN" >&2
      exit 1
    fi
  fi
  park_pointer
else
  if is_fullscreen; then
    wmctrl -i -r "$WID" -b remove,fullscreen
    sleep 0.2
    if is_fullscreen; then
      echo "sent un-fullscreen request, but window manager still reports fullscreen" >&2
      exit 1
    fi
    echo "fullscreen off"
  else
    echo "already not fullscreen"
  fi
fi
