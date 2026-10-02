#!/usr/bin/env bash
# "Meeting only" full-frame mode, window-level half: true window-manager
# fullscreen on the Zoom meeting window (Openbox honors EWMH
# _NET_WM_STATE_FULLSCREEN via wmctrl), which removes the WM's own
# title bar/border - distinct from Zoom's in-app "Alt+F" fullscreen,
# which only resizes Zoom's own window and isn't reliably read back.
# wmctrl's add/remove (not toggle) make this idempotent: calling "on"
# twice, or "off" when already off, is a harmless no-op either way.
#
# What this does NOT do: force-close the chat/participants panels, or
# touch Zoom's "Always show meeting controls" setting - see
# app/control.py's zoom_set_meeting_only() docstring for why (no
# reliable readable state for either, so a blind shortcut could easily
# make things worse, not better).
set -euo pipefail
export DISPLAY="${DISPLAY:-:99}"
ACTION="${1:-}"

if [[ "$ACTION" != "on" && "$ACTION" != "off" ]]; then
  echo "usage: $(basename "$0") on|off" >&2
  exit 2
fi

WID="$(wmctrl -l 2>/dev/null | awk '/Zoom Meeting|Zoom Webinar|Zoom Workplace|Zoom - /{print $1; exit}')"
if [[ -z "$WID" ]]; then
  echo "Zoom meeting window not found - is zoom.service running and joined?" >&2
  exit 1
fi

is_fullscreen() {
  xprop -id "$WID" _NET_WM_STATE 2>/dev/null | grep -q "_NET_WM_STATE_FULLSCREEN"
}

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
