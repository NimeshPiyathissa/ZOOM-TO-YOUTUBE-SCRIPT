#!/usr/bin/env bash
# Launches the Zoom Linux client, joins the configured meeting via a
# zoommtg:// deep link (no browser, no manual click-through), maximizes
# the meeting window, then babysits the real Zoom process so systemd
# knows when it has died and should rejoin.
#
# First-run note: mute state (mic/camera) and view (speaker/gallery) are
# whatever Zoom's client last used. On the very first join, connect over
# VNC and manually mute mic + stop video + pick your view once - Zoom
# remembers this per-machine for all future joins. See README.

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh
wait_for_x

# join_method=web (or auto after a fallback - see the dashboard's
# control.py) runs the meeting through Zoom's web client on the
# browser-source unit instead of this one - see that script's own
# ZOOM_JOIN_VIA guard, the mirror image of this one.
if [[ "${SOURCE_TYPE:-zoom}" == "zoom" && "${ZOOM_JOIN_VIA:-client}" == "web" ]]; then
  log "ZOOM_JOIN_VIA=web - this meeting joins via the browser-source unit, nothing to do here, exiting cleanly"
  exit 0
fi

ZOOM_BIN="$(command -v zoom || echo /opt/zoom/ZoomLauncher)"
if [[ ! -x "$ZOOM_BIN" ]]; then
  log "Zoom binary not found (looked for 'zoom' in PATH and /opt/zoom/ZoomLauncher)"
  exit 1
fi

# /j/<id> = ordinary join link; /w/<id>?tk=... = the per-registrant
# webinar link Zoom issues after registering (see the dashboard's
# app/zoomlink.py). The tk= token is what lets a registration-required
# webinar be joined at all, so it's passed straight through to the deep
# link. It is a credential (anyone holding it joins as that registrant):
# never logged here, and logs.py redacts tk= like pwd=.
CONFNO="$(grep -oP '(?<=/[jw]/)[0-9]+' <<<"${ZOOM_LINK:-}" || true)"
PWD_PARAM="$(grep -oP '(?<=[?&]pwd=)[^&]+' <<<"${ZOOM_LINK:-}" || true)"
TK_PARAM="$(grep -oP '(?<=[?&]tk=)[^&]+' <<<"${ZOOM_LINK:-}" || true)"
if [[ -z "$PWD_PARAM" ]]; then
  PWD_PARAM="${ZOOM_PASSCODE:-}"
fi
if [[ -z "$CONFNO" ]]; then
  log "Could not parse a meeting ID out of ZOOM_LINK (expected .../j/<id> or .../w/<id>) - link not logged"
  exit 1
fi
TK_SUFFIX=""
[[ -n "$TK_PARAM" ]] && TK_SUFFIX="&tk=${TK_PARAM}"
BOT_NAME="${BOT_NAME:-Stream Bot}"
ZOOM_SIGNIN_MODE="${ZOOM_SIGNIN_MODE:-guest}"

# Per-meeting rejoin policy (dashboard Zoom page, via current-source.env):
# systemd restarts this unit only on a non-zero exit (Restart=on-failure),
# so "don't rejoin" is simply a clean exit. The attempt counter lives in
# logs/ keyed by ZOOM_JOIN_EPOCH, which the dashboard stamps fresh on every
# operator-initiated join - so the cap counts *automatic* rejoins since
# the human last pressed Join, never the human's own presses. systemd's
# StartLimitBurst stays as the outer bound.
ZOOM_AUTO_REJOIN="${ZOOM_AUTO_REJOIN:-1}"
ZOOM_REJOIN_MAX="${ZOOM_REJOIN_MAX:-5}"
ZOOM_JOIN_EPOCH="${ZOOM_JOIN_EPOCH:-0}"
[[ "$ZOOM_REJOIN_MAX" =~ ^[0-9]+$ ]] || ZOOM_REJOIN_MAX=5
COUNT_FILE="$LOG_DIR/zoom-rejoin.count"
ATTEMPTS=0
if [[ -f "$COUNT_FILE" ]]; then
  read -r SAVED_EPOCH SAVED_COUNT < "$COUNT_FILE" || true
  if [[ "${SAVED_EPOCH:-}" == "$ZOOM_JOIN_EPOCH" && "${SAVED_COUNT:-}" =~ ^[0-9]+$ ]]; then
    ATTEMPTS="$SAVED_COUNT"
  fi
fi
if (( ATTEMPTS > 0 )); then
  # This start is an automatic rejoin (attempt ATTEMPTS+1 for this epoch).
  if [[ "$ZOOM_AUTO_REJOIN" != "1" ]]; then
    log "Zoom exited and auto-rejoin is off for this meeting - not rejoining (exit 0 so systemd leaves it stopped)"
    exit 0
  fi
  if (( ATTEMPTS > ZOOM_REJOIN_MAX )); then
    log "Auto-rejoin limit reached (${ZOOM_REJOIN_MAX} rejoins since the last operator join) - not rejoining"
    exit 0
  fi
  log "Auto-rejoin ${ATTEMPTS}/${ZOOM_REJOIN_MAX}"
fi
printf '%s %s\n' "$ZOOM_JOIN_EPOCH" "$((ATTEMPTS + 1))" > "$COUNT_FILE"

if [[ "$ZOOM_SIGNIN_MODE" == "google" ]]; then
  # Signed-in join: no &uname= override, so Zoom uses the signed-in
  # account's own display name instead of the guest bot name. If the
  # client isn't actually signed in yet (sign-in wasn't completed over
  # noVNC first), Zoom falls back to prompting for a guest name here,
  # same as the default flow - visible and fixable over noVNC, not a
  # silent failure.
  DEEP_LINK="zoommtg://zoom.us/join?action=join&confno=${CONFNO}&pwd=${PWD_PARAM}${TK_SUFFIX}"
  log "Launching Zoom (signed-in join): confno=${CONFNO}${TK_PARAM:+ (registrant token present)}"
else
  UNAME_ENC="${BOT_NAME// /%20}"
  DEEP_LINK="zoommtg://zoom.us/join?action=join&confno=${CONFNO}&pwd=${PWD_PARAM}&uname=${UNAME_ENC}${TK_SUFFIX}"
  log "Launching Zoom (guest join): confno=${CONFNO} uname=${BOT_NAME}${TK_PARAM:+ (registrant token present)}"
fi

"$ZOOM_BIN" "$DEEP_LINK" >>"$LOG_DIR/zoom.log" 2>&1 &
disown

ZOOM_REAL_PID=""
for _ in $(seq 1 30); do
  ZOOM_REAL_PID="$(pgrep -u "$(id -u)" -x zoom | head -n1 || true)"
  [[ -n "$ZOOM_REAL_PID" ]] && break
  sleep 1
done
if [[ -z "$ZOOM_REAL_PID" ]]; then
  log "Zoom process never appeared after launch; giving up this attempt"
  exit 1
fi
log "Zoom running as PID $ZOOM_REAL_PID"

# Best-effort: maximize the meeting window once it appears. Zoom's window
# titles vary by version/locale, so this tries a couple of common patterns
# and otherwise leaves the window as-is (fix once by hand over VNC, Zoom
# remembers window placement after that).
(
  sleep 6
  for _ in $(seq 1 15); do
    WID="$(wmctrl -l 2>/dev/null | awk '/Zoom Meeting|Zoom Workplace|Zoom - /{print $1; exit}')"
    if [[ -n "$WID" ]]; then
      wmctrl -i -r "$WID" -b add,maximized_vert,maximized_horz
      log "Maximized Zoom window $WID"
      break
    fi
    sleep 2
  done
) &

while kill -0 "$ZOOM_REAL_PID" 2>/dev/null; do
  sleep 5
done
log "Zoom process exited; exiting so systemd restarts and rejoins"
exit 1
