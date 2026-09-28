#!/usr/bin/env bash
# Launches Chrome in kiosk mode against the active webpage source's URL,
# on :99, with its audio routed to the same null sink FFmpeg captures
# from. Uses a dedicated persistent profile so logins (including a Zoom
# Google sign-in redirect, if Chrome is zoombot's default browser -
# see zoom-google-signin.sh) survive restarts. No credential entry is
# ever automated here - if a page needs a login, that's done by hand
# over noVNC, same as Zoom's Google sign-in.
#
# "Click to start" for players that block autoplay even with the policy
# flags below: also handled by hand over noVNC - nothing here simulates
# a click.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh
wait_for_x

# Also runs a Zoom source whose join_method is "web" (or "auto" after a
# client-side fallback - see the dashboard's control.py): same Chrome
# kiosk mechanism, pointed at Zoom's own web client instead of a generic
# page. ZOOM_JOIN_VIA is written by the dashboard into current-source.env
# alongside SOURCE_TYPE=zoom; it's "client" (the default - nothing to do
# here, join-zoom.sh handles it) unless this unit is actually wanted.
ZOOM_WEB_MODE=0
if [[ "$SOURCE_TYPE" == "zoom" && "${ZOOM_JOIN_VIA:-client}" == "web" ]]; then
  ZOOM_WEB_MODE=1
elif [[ "$SOURCE_TYPE" != "webpage" ]]; then
  log "SOURCE_TYPE is '$SOURCE_TYPE' (join_via=${ZOOM_JOIN_VIA:-client}), not for this unit - nothing to do, exiting cleanly"
  # Not an error: the dashboard stops this unit when switching away from
  # a source it serves, but systemd may race a leftover start. Exit 0 so
  # Restart=always doesn't loop-crash on a source that simply changed.
  exit 0
fi

if [[ "$ZOOM_WEB_MODE" == "1" ]]; then
  # Same ZOOM_LINK/ZOOM_PASSCODE .env values join-zoom.sh already parses
  # for the desktop deep link (see that script's comment on the grep
  # patterns and app/zoomlink.py for the parsing they mirror) - rebuilt
  # here as a wc/join URL instead of a zoommtg:// one. No separate secret
  # storage: this is derived fresh from .env on every start, never
  # written to current-source.env (which isn't a secret file).
  CONFNO="$(grep -oP '(?<=/[jw]/)[0-9]+' <<<"${ZOOM_LINK:-}" || true)"
  PWD_PARAM="$(grep -oP '(?<=[?&]pwd=)[^&]+' <<<"${ZOOM_LINK:-}" || true)"
  TK_PARAM="$(grep -oP '(?<=[?&]tk=)[^&]+' <<<"${ZOOM_LINK:-}" || true)"
  [[ -z "$PWD_PARAM" ]] && PWD_PARAM="${ZOOM_PASSCODE:-}"
  if [[ -z "$CONFNO" ]]; then
    log "SOURCE_TYPE=zoom, join_via=web, but no meeting ID found in ZOOM_LINK - refusing to start"
    exit 1
  fi
  ZOOM_HOST="$(grep -oP '(?<=https://)[^/]+' <<<"${ZOOM_LINK:-}" || echo "zoom.us")"
  TARGET_URL="https://${ZOOM_HOST}/wc/join/${CONFNO}"
  [[ -n "$PWD_PARAM" ]] && TARGET_URL="${TARGET_URL}?pwd=${PWD_PARAM}"
  [[ -n "$TK_PARAM" ]] && TARGET_URL="${TARGET_URL}$([[ "$TARGET_URL" == *\?* ]] && echo '&' || echo '?')tk=${TK_PARAM}"
else
  if [[ -z "${WEBPAGE_URL:-}" ]]; then
    log "SOURCE_TYPE=webpage but WEBPAGE_URL is empty - refusing to start"
    exit 1
  fi
  case "$WEBPAGE_URL" in
    -*) log "WEBPAGE_URL looks like a flag, refusing: $WEBPAGE_URL"; exit 1 ;;
  esac
  TARGET_URL="$WEBPAGE_URL"
fi

CHROME_BIN="$(command -v google-chrome-stable || command -v google-chrome || echo /usr/bin/google-chrome-stable)"
if [[ ! -x "$CHROME_BIN" ]]; then
  log "Chrome binary not found ($CHROME_BIN)"
  exit 1
fi

# Part 3: play as the Google account bound to this source (its own
# profile from chrome-account.sh, where the human signed in over noVNC)
# when one is bound; otherwise the shared stream profile as before.
PROFILE_DIR="$HOME/.config/stream-chrome-profile"
if [[ -n "${ACCOUNT_PROFILE_ID:-}" && "${ACCOUNT_PROFILE_ID}" =~ ^[a-z0-9][a-z0-9-]{0,39}$ \
      && -d "$HOME/.config/chrome-profiles/${ACCOUNT_PROFILE_ID}" ]]; then
  PROFILE_DIR="$HOME/.config/chrome-profiles/${ACCOUNT_PROFILE_ID}"
  log "Using bound account profile ${ACCOUNT_PROFILE_ID}"
fi
mkdir -p "$PROFILE_DIR"

# An ordinary (non-kiosk) Chrome window opened on this same profile by
# open-browser.sh (the /remote "Browser" action) or chrome-account.sh
# would make the kiosk launch below hand its URL to that instance and
# exit immediately - which systemd reads as a crash loop. Close it first;
# SIGTERM so Chrome flushes cookies/session to disk.
for stray in "$PROFILE_DIR/.window.pid" "$PROFILE_DIR/.signin.pid"; do
  [[ -f "$stray" ]] || continue
  SP="$(cat "$stray" 2>/dev/null || true)"
  if [[ "$SP" =~ ^[0-9]+$ ]] && kill -0 "$SP" 2>/dev/null; then
    log "Closing stray Chrome window (pid $SP) holding $PROFILE_DIR before starting the kiosk"
    kill -TERM "$SP" 2>/dev/null || true
    for _ in $(seq 1 40); do kill -0 "$SP" 2>/dev/null || break; sleep 0.25; done
    kill -0 "$SP" 2>/dev/null && kill -KILL "$SP" 2>/dev/null || true
  fi
  rm -f "$stray"
done

RESOLUTION="${RESOLUTION:-1920x1080}"
if [[ "$ZOOM_WEB_MODE" == "1" ]]; then
  # Zoom's own web client controls its own layout/zoom; the
  # WEBPAGE_ZOOM/WEBPAGE_RELOAD_SECONDS options don't apply to it (a
  # forced reload would just leave the meeting).
  ZOOM_LEVEL="1.0"
  RELOAD_SECONDS="0"
else
  ZOOM_LEVEL="${WEBPAGE_ZOOM:-1.0}"
  RELOAD_SECONDS="${WEBPAGE_RELOAD_SECONDS:-0}"
fi

# Cursor hiding: unclutter, best-effort (not fatal if missing - the
# --start-fullscreen kiosk window hides most of it anyway, this just
# covers the idle-mouse case if someone was just driving it over VNC).
if command -v unclutter >/dev/null 2>&1; then
  unclutter -idle 0 -root >/dev/null 2>&1 &
  disown
fi

if [[ "$ZOOM_WEB_MODE" == "1" ]]; then
  # TARGET_URL carries pwd=/tk= for a zoom-web join - never logged, same
  # discipline as join-zoom.sh's deep link (see its comment on this).
  log "Starting Chrome kiosk: mode=zoom-web confno=${CONFNO}${TK_PARAM:+ (registrant token present)}"
else
  log "Starting Chrome kiosk: mode=webpage url=${TARGET_URL} zoom=${ZOOM_LEVEL} reload=${RELOAD_SECONDS}s"
fi

# PULSE_SINK routes this process's default audio output to the zoom_out
# null sink FFmpeg reads from - same mechanism Zoom uses via
# audio-setup.sh's set-default-sink, just scoped to this one process via
# env instead of the system default, so it doesn't fight a concurrent
# Zoom session using the same sink for a different source type.
# --remote-debugging-port lets the dashboard's touch remote (Part 3)
# navigate/control this same tab via the DevTools protocol (app/cdp.py)
# instead of restarting Chrome to switch what's showing. Bound to
# 127.0.0.1 explicitly - never reachable off this box, same trust
# boundary as x11vnc/novnc-proxy. Must match app/config.py's
# CHROME_DEBUG_PORT in the dashboard repo.
CLEANFEED_EXT="${STREAM_APP_DIR:-/home/zoombot/zoom-stream}/extensions/zoom-cleanfeed"
EXT_FLAG=()
[[ -d "$CLEANFEED_EXT" ]] && EXT_FLAG=(--load-extension="$CLEANFEED_EXT" --disable-extensions-except="$CLEANFEED_EXT")

DEVTOOLS_PORT=9222
env PULSE_SINK=zoom_out "$CHROME_BIN" \
  "${EXT_FLAG[@]}" \
  --kiosk --app="$TARGET_URL" \
  --remote-debugging-port="$DEVTOOLS_PORT" --remote-debugging-address=127.0.0.1 \
  --window-position=0,0 --window-size="${RESOLUTION/x/,}" \
  --user-data-dir="$PROFILE_DIR" \
  --force-device-scale-factor="$ZOOM_LEVEL" --high-dpi-support=1 \
  --autoplay-policy=no-user-gesture-required \
  --noerrdialogs --disable-infobars --disable-session-crashed-bubble \
  --disable-translate --disable-notifications --disable-popup-blocking \
  --disable-features=Translate,TranslateUI,Notifications \
  --overscroll-history-navigation=0 \
  --disable-gpu --lang=en-US --password-store=basic \
  --no-first-run --no-default-browser-check \
  >>"$LOG_DIR/browser.log" 2>&1 &
disown
CHROME_PID=$!

for _ in $(seq 1 30); do
  kill -0 "$CHROME_PID" 2>/dev/null || break
  pgrep -P "$CHROME_PID" >/dev/null 2>&1 && break
  sleep 1
done
if ! kill -0 "$CHROME_PID" 2>/dev/null; then
  log "Chrome process exited immediately after launch"
  exit 1
fi
log "Chrome running as PID $CHROME_PID"

# DevTools readiness gate (Part 5). The dashboard drives this tab over
# the DevTools protocol (app/cdp.py): navigate, play/pause, volume,
# player state. A Chrome that is up but not listening on the debug port
# is invisible to it, so "no port" is treated as a failed start - exit
# non-zero and let systemd restart us - rather than a browser that
# silently can't be controlled (incident 2026-09-18/19: a Chrome
# launched by an older copy of this script ran for 15h with no port,
# every YouTube control in the panel dead, unit happily "active").
# This gate can't live in the unit's ExecStartPre=: ExecStartPre runs
# *before* ExecStart, and Chrome - the thing that opens the port - is
# ExecStart. devtools-port-check.sh (the actual ExecStartPre) checks
# the port is free of foreign listeners instead.
DEVTOOLS_VERSION=""
for _ in $(seq 1 60); do
  if DEVTOOLS_VERSION="$(curl -fsS -m 1 "http://127.0.0.1:${DEVTOOLS_PORT}/json/version" 2>/dev/null)"; then
    break
  fi
  DEVTOOLS_VERSION=""
  kill -0 "$CHROME_PID" 2>/dev/null || break
  sleep 0.5
done
if [[ -z "$DEVTOOLS_VERSION" ]]; then
  log "Chrome is running but DevTools never answered on 127.0.0.1:${DEVTOOLS_PORT} within 30s - exiting so systemd restarts it"
  kill "$CHROME_PID" 2>/dev/null || true
  exit 1
fi
log "DevTools ready on 127.0.0.1:${DEVTOOLS_PORT} ($(grep -oE '"Browser": *"[^"]+"' <<<"$DEVTOOLS_VERSION" | head -n1 | cut -d'"' -f4))"

# Best-effort page-load signal for the Overview page: once a browser
# window appears and its title stops being the generic "about:blank"/
# loading state, touch a marker file the dashboard reads directly
# (zoom-stream/logs is group-readable by the dashboard user). This is a
# heuristic, same spirit as join-zoom.sh's window-maximize best-effort.
(
  sleep 3
  for _ in $(seq 1 20); do
    TITLE="$(wmctrl -l 2>/dev/null | awk -v pid="$CHROME_PID" '{$1=$2=$3="";print}' | head -n1)"
    if [[ -n "$TITLE" && "$TITLE" != *"about:blank"* && "$TITLE" != *"New Tab"* ]]; then
      touch "$LOG_DIR/browser-loaded"
      log "Page appears loaded: ${TITLE}"
      break
    fi
    sleep 2
  done
) &

# Optional periodic reload (Ctrl+R via the window manager, not a
# simulated click - just a key event to the whole display, harmless if
# there's nothing else with focus in this single-app kiosk session).
if [[ "$RELOAD_SECONDS" -gt 0 ]]; then
  (
    while kill -0 "$CHROME_PID" 2>/dev/null; do
      sleep "$RELOAD_SECONDS"
      kill -0 "$CHROME_PID" 2>/dev/null || break
      xdotool key --clearmodifiers ctrl+r 2>/dev/null || true
      touch "$LOG_DIR/browser-loaded"
    done
  ) &
fi

# Keepalive touch so a long-running, never-reloaded page doesn't read as
# "stale" on the Overview page (see stats.webpage_health's staleness
# window) purely because nothing has re-touched the marker in a while.
(
  while kill -0 "$CHROME_PID" 2>/dev/null; do
    sleep 1800
    [[ -f "$LOG_DIR/browser-loaded" ]] && touch "$LOG_DIR/browser-loaded"
  done
) &

while kill -0 "$CHROME_PID" 2>/dev/null; do
  sleep 5
done
log "Chrome process exited; exiting so systemd restarts it"
exit 1
