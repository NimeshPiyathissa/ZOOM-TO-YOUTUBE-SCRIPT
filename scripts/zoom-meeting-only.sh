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

if [[ "$ACTION" != "on" && "$ACTION" != "off" && "$ACTION" != "park" && "$ACTION" != "geometry" ]]; then
  echo "usage: $(basename "$0") on|off|park|geometry" >&2
  exit 2
fi
# geometry: READ-ONLY report of the meeting window for Fullscreen Live / Clean Broadcast (dashboard
# control.zoom_window_geometry): "w=.. h=.. x=.. y=.. fullscreen=0|1 display=WxH meeting=0|1 top=0|1" - meeting=1 when a
# real meeting window was found, top=1 when it is the topmost large Zoom window (what x11grab sees). Changes nothing.

# Which window is the MEETING. Zoom 7.x keeps its home window ("Zoom Workplace - <account>") open next to the meeting
# window, both full-size; the meeting window is titled "Webinar", "Meeting", "Zoom Meeting"/"Zoom Webinar", the meeting
# topic, or (older 7.x) a bare "Zoom Workplace". Picking the home window was a real bug (2026-11-08): fullscreening it
# raised it over the webinar and Clean Broadcast measured Zoom's home screen. Ranking, among zoom.* windows >= 800x500:
#   3 = explicit meeting/webinar title, 1 = any other title (topic / bare "Zoom Workplace"), 0 = home window;
#   Settings/Chat/Participants/Q&A/Polls/Whiteboard/... pop-outs are never picked. Ties: larger, then higher in the stack.
# MEETING=0 means only the home window exists (no meeting): callers may still act on it, but must not trust it as one.
STACK="$(xprop -root _NET_CLIENT_LIST_STACKING 2>/dev/null | sed 's/.*#//; s/,/ /g')"
read -r WID MEETING TOPID < <(wmctrl -lGx 2>/dev/null | awk -v stack="$STACK" '
  function norm(id) { id = tolower(id); sub(/^0x0*/, "0x", id); return id }
  BEGIN { n = split(stack, s, " "); for (i = 1; i <= n; i++) order[norm(s[i])] = i }
  tolower($7) ~ /^zoom\./ && $5 >= 800 && $6 >= 500 {
    title = $9; for (i = 10; i <= NF; i++) title = title " " $i
    t = tolower(title); id = norm($1); o = (id in order) ? order[id] : 0
    if (o >= topo) { topo = o; top = $1 }
    if (t ~ /^(settings|chat|participants|q&a|polls?|breakout rooms|whiteboards?|share screen|annotation|reactions)( |$)/) next
    score = 1
    if (t ~ /^zoom workplace - /) score = 0
    if (t ~ /^(zoom )?(meeting|webinar)$/) score = 3
    key = score * 1e12 + $5 * $6 * 100 + o
    if (key > best) { best = key; pick = $1; sc = score }
  }
  END { if (pick != "") print pick, (sc > 0 ? 1 : 0), top }') || true   # no window: read hits EOF
WID="${WID:-}"; MEETING="${MEETING:-0}"; TOPID="${TOPID:-}"
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

if [[ "$ACTION" == "geometry" ]]; then
  read -r gx gy gw gh < <(wmctrl -lG 2>/dev/null | awk -v id="$WID" '$1 == id {print $3, $4, $5, $6; exit}')
  fs=0; is_fullscreen && fs=1
  disp="$(xdotool getdisplaygeometry 2>/dev/null | awk '{print $1 "x" $2}')"
  top=0; [[ "$WID" == "$TOPID" ]] && top=1
  echo "w=${gw:-0} h=${gh:-0} x=${gx:-0} y=${gy:-0} fullscreen=$fs display=${disp:-unknown} meeting=$MEETING top=$top"
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
  if [[ "$MEETING" == 1 ]]; then
    # Raise + focus the meeting window: fullscreening (or the home window) may have left something else on top.
    wmctrl -i -a "$WID" 2>/dev/null && echo "meeting window raised" || echo "could not raise the meeting window" >&2
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
