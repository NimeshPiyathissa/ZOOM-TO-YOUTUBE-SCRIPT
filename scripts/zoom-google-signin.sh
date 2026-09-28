#!/usr/bin/env bash
# Launches the Zoom client to its own sign-in screen - no zoommtg://
# guest deep link, no automation of any kind - so a human can complete
# Google OAuth by hand over noVNC. Never reads, stores, or logs a
# Google password, 2FA code, or OAuth token; this script's only job is
# to put the Zoom window on screen.
#
# NOTE: the exact first-run screen the Zoom Linux client shows (and
# whether it needs an extra flag to land on "Sign In" rather than a
# guest-join prompt) depends on the installed Zoom client version. This
# was written against the general Zoom Linux client behavior and should
# be confirmed once against the real installed version - see the
# deployment notes for how that was verified on this VPS.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh
wait_for_x

ZOOM_BIN="$(command -v zoom || echo /opt/zoom/ZoomLauncher)"
if [[ ! -x "$ZOOM_BIN" ]]; then
  log "Zoom binary not found (looked for 'zoom' in PATH and /opt/zoom/ZoomLauncher)"
  exit 1
fi

if pgrep -u "$(id -u)" -x zoom >/dev/null 2>&1; then
  log "Zoom is already running - stop the current meeting/session from the dashboard first, then retry sign-in"
  exit 1
fi

# Make Chrome (the same profile used for webpage sources) zoombot's
# default browser first, so Zoom's SSO/Google redirect opens there and
# the zoommtg:// callback hands control back to Zoom - see
# install-dashboard.sh for the one-time xdg-settings/desktop-file setup
# this depends on.
xdg-settings set default-web-browser google-chrome-stable.desktop >/dev/null 2>&1 || true

log "Launching Zoom for interactive sign-in (no deep link - use the Zoom window's own Sign In button over noVNC)"
"$ZOOM_BIN" >>"$LOG_DIR/zoom.log" 2>&1 &
disown

log "Zoom launched. Open the Remote GUI tab in the dashboard, click Sign In, then 'Sign in with SSO' or the Google option, and complete it in the browser window that appears."
